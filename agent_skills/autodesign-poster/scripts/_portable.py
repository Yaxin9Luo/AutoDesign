#!/usr/bin/env python3
"""Portable evidence, run-state, review, and finalization primitives.

This module deliberately uses only the Python standard library.  Callers pass
an explicit run directory to every mutating operation; installed Skill files
are read only for snapshotting and drift verification.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.util
import json
import math
import os
import re
import secrets
import shutil
import struct
import subprocess
import tempfile
import threading
import unicodedata
import zlib
from collections import Counter
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from stat import S_IFMT, S_ISDIR, S_ISLNK, S_ISREG
from typing import Any, Iterable, Iterator, Mapping, Sequence


FORMAT_VERSION = 1
RELEASED_RUN_FORMAT_VERSION = 1
AGENT_FIRST_RUN_FORMAT_VERSION = 2
MAIN_STATES = (
    "initialized",
    "planned",
    "authoring",
    "deterministic_passed",
    "semantic_passed",
    "finalized",
)
SIDE_STATES = ("blocked", "failed", "needs_visual_review")
_TRANSITIONS = dict(zip(MAIN_STATES, MAIN_STATES[1:]))
_RUNTIME_TOP_LEVEL = {"SKILL.md", "scripts", "references", "assets"}
_GENERATED_CACHE_DIRS = {
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__",
    "build", "dist", "node_modules", "out", "output", "outputs", "runs",
    "sessions", "venv",
}
_GENERATED_CACHE_FILES = {".DS_Store"}
_GENERATED_CACHE_SUFFIXES = {".pyc", ".pyo"}
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|authorization|cookie|credential|password|private[_-]?key|secret|token)",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*[:=]\s*"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SK_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_SENSITIVE_HEADER = re.compile(
    r"(?im)\b(?P<name>Set-Cookie|Cookie|Authorization)\s*:\s*[^\r\n]*(?P<cr>\r?)$"
)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,}")
_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.])[-+]?(?:(?:\d{1,3}(?:,\d{3})+)(?:\.\d+)?|"
    r"\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?%?"
)
_NUMERIC_TRANSLATION = str.maketrans({"\u2212": "-", "\ufe63": "-", "\uff0d": "-"})
_STOPWORDS = {
    "about", "after", "also", "among", "and", "are", "because", "been",
    "before", "being", "between", "both", "but", "can", "could", "does",
    "for", "from", "had", "has", "have", "into", "its", "more", "most",
    "not", "our", "paper", "that", "the", "their", "then", "there", "these",
    "this", "those", "through", "under", "using", "was", "were", "will", "with",
}
_DEFAULT_VISUAL_ROLES = (
    "background", "comparison", "context", "method", "overview", "result", "supporting"
)
_THREAD_RUN_LOCKS: dict[str, threading.Lock] = {}
_THREAD_RUN_LOCKS_GUARD = threading.Lock()


class PortableError(RuntimeError):
    """Base error for portable harness operations."""


class PathSafetyError(PortableError):
    """A path escaped its declared root or traversed a symlink."""


class IntegrityError(PortableError):
    """A persisted hash-bound contract no longer matches its files."""


class StateError(PortableError):
    """A requested run-state transition is invalid."""


class ContractError(PortableError):
    """Structured evidence, review, or visual input is incomplete or invalid."""


class SimulatedCrash(PortableError):
    """Test-only crash boundary used to prove durable recovery semantics."""


def sha256_bytes(data: bytes) -> str:
    """Return the lowercase SHA-256 digest of *data*."""

    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path | str) -> str:
    """Hash a regular non-symlink file."""

    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise PathSafetyError(f"expected regular non-symlink file: {source}")
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _canonical_hash(value: Any) -> str:
    return sha256_bytes(_canonical_json_bytes(value))


def _stored_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def safe_path(root: Path | str, relative: Path | str, *, must_exist: bool = False) -> Path:
    """Resolve a relative path beneath *root*, rejecting traversal and symlinks."""

    base = Path(root).absolute()
    if base.is_symlink():
        raise PathSafetyError(f"root must not be a symlink: {base}")
    candidate = Path(relative)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise PathSafetyError(f"unsafe relative path: {relative}")
    current = base
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            raise PathSafetyError(f"symlink path is not allowed: {current}")
    try:
        current.resolve(strict=False).relative_to(base.resolve(strict=False))
    except ValueError as error:
        raise PathSafetyError(f"path escapes root: {relative}") from error
    if must_exist and not current.exists():
        raise PathSafetyError(f"path does not exist: {current}")
    return current


def atomic_write_bytes(path: Path | str, data: bytes) -> None:
    """Atomically replace *path* with bytes, flushing file and parent directory."""

    target = Path(path)
    if target.is_symlink():
        raise PathSafetyError(f"refusing to replace symlink: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.tmp-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path | str, value: Any) -> None:
    """Write deterministic, indented JSON through an atomic replacement."""

    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write_bytes(path, data)


def append_jsonl(path: Path | str, value: Any) -> None:
    """Append one complete compact JSON object using an O_APPEND write."""

    target = Path(path)
    if target.is_symlink():
        raise PathSafetyError(f"refusing to append through symlink: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    data = _canonical_json_bytes(value)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        written = os.write(descriptor, data)
        if written != len(data):
            raise OSError(f"short append to {target}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def redact_secrets(value: Any) -> Any:
    """Recursively redact credential-shaped keys and values before persistence."""

    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _SECRET_KEY.search(str(key)) else redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        text = _SENSITIVE_HEADER.sub(
            lambda match: f"{match.group('name')}: [REDACTED]{match.group('cr')}",
            value,
        )

        def redact_assignment(match: re.Match[str]) -> str:
            if match.group("value") == "[REDACTED]" or not _SECRET_KEY.search(
                match.group("key")
            ):
                return match.group(0)
            return f"{match.group('key')}=[REDACTED]"

        text = _SECRET_ASSIGNMENT.sub(
            redact_assignment,
            text,
        )
        text = _BEARER.sub("Bearer [REDACTED]", text)
        return _SK_TOKEN.sub("[REDACTED]", text)
    return value


def tree_hash(root: Path | str) -> str:
    """Hash names and contents of a regular-file tree, rejecting symlinks."""

    base = Path(root)
    if base.is_symlink() or not base.is_dir():
        raise PathSafetyError(f"expected regular directory: {base}")
    digest = hashlib.sha256()
    for current, directories, files in os.walk(base, followlinks=False):
        current_path = Path(current)
        for name in sorted(directories):
            if (current_path / name).is_symlink():
                raise PathSafetyError(f"symlink is not allowed: {current_path / name}")
        for name in sorted(files):
            path = current_path / name
            if path.is_symlink():
                raise PathSafetyError(f"symlink is not allowed: {path}")
            relative = path.relative_to(base).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def _runtime_files(skill_root: Path) -> list[Path]:
    if skill_root.is_symlink() or not skill_root.is_dir():
        raise PathSafetyError(f"invalid Skill root: {skill_root}")
    selected: list[Path] = []
    for current, directories, files in os.walk(skill_root, followlinks=False):
        current_path = Path(current)
        relative_dir = current_path.relative_to(skill_root)
        if relative_dir.parts and relative_dir.parts[0] not in _RUNTIME_TOP_LEVEL:
            directories[:] = []
            continue
        for directory in list(directories):
            path = current_path / directory
            if path.is_symlink():
                raise PathSafetyError(f"symlink is not allowed: {path}")
            if directory in _GENERATED_CACHE_DIRS:
                directories.remove(directory)
                continue
            top = (relative_dir / directory).parts[0]
            if top not in _RUNTIME_TOP_LEVEL:
                directories.remove(directory)
        for name in files:
            path = current_path / name
            relative = path.relative_to(skill_root)
            if relative.parts[0] not in _RUNTIME_TOP_LEVEL:
                continue
            if path.is_symlink() or not path.is_file():
                raise PathSafetyError(f"runtime file must be regular: {path}")
            if (
                path.name in _GENERATED_CACHE_FILES
                or path.suffix.lower() in _GENERATED_CACHE_SUFFIXES
            ):
                continue
            selected.append(path)
    return sorted(selected, key=lambda path: path.relative_to(skill_root).as_posix())


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise IntegrityError(f"invalid JSON contract: {path}") from error
    if not isinstance(value, dict):
        raise IntegrityError(f"JSON contract must be an object: {path}")
    return value


def inspect_run_format(run_dir: Path | str) -> int:
    """Read only the top-level run contract and report its known format."""

    run = Path(run_dir).absolute()
    if run.is_symlink() or not run.is_dir():
        raise PathSafetyError(f"run directory must be a regular directory: {run}")
    state_path = run / "run.json"
    if state_path.is_symlink() or not state_path.is_file():
        raise PathSafetyError(f"run contract must be a regular file: {state_path}")
    if state_path.stat().st_nlink != 1:
        raise PathSafetyError(f"run contract must not be hardlinked: {state_path}")
    state = _read_json(state_path)
    artifact_version = state.get("format_version")
    if "run_format_version" not in state:
        if (
            isinstance(artifact_version, int)
            and not isinstance(artifact_version, bool)
            and artifact_version == FORMAT_VERSION
        ):
            return RELEASED_RUN_FORMAT_VERSION
        raise IntegrityError(
            f"unknown or missing legacy format version: {artifact_version!r}"
        )
    run_version = state.get("run_format_version")
    if (
        isinstance(run_version, int)
        and not isinstance(run_version, bool)
        and run_version == AGENT_FIRST_RUN_FORMAT_VERSION
        and isinstance(artifact_version, int)
        and not isinstance(artifact_version, bool)
        and artifact_version == FORMAT_VERSION
    ):
        return AGENT_FIRST_RUN_FORMAT_VERSION
    raise IntegrityError(
        "Agent-first run contract requires run_format_version=2 and format_version=1"
    )


def diagnose_v1_run(run_dir: Path | str) -> dict[str, Any]:
    """Report inert legacy metadata without traversing or loading its snapshot."""

    run = Path(run_dir).absolute()
    if inspect_run_format(run) != RELEASED_RUN_FORMAT_VERSION:
        raise StateError("diagnose_v1_run requires a version-1 run")
    state = _read_json(run / "run.json")
    source_path = run / "evidence" / "source_manifest.json"
    source_status: str | None = None
    source_manifest_sha256: str | None = None
    source_input_path: str | None = None
    if source_path.exists():
        if (
            source_path.is_symlink()
            or not source_path.is_file()
            or source_path.stat().st_nlink != 1
        ):
            raise PathSafetyError(f"unsafe legacy source contract: {source_path}")
        source = _read_json(source_path)
        source_status = source.get("status") if isinstance(source.get("status"), str) else None
        source_manifest_sha256 = sha256_file(source_path)
        if isinstance(source.get("input_path"), str):
            try:
                safe_path(run, source["input_path"])
            except PathSafetyError as error:
                raise IntegrityError("legacy source path is unsafe") from error
            source_input_path = source["input_path"]
    event_count = 0
    events_path = run / "events.jsonl"
    if events_path.exists():
        if (
            events_path.is_symlink()
            or not events_path.is_file()
            or events_path.stat().st_nlink != 1
        ):
            raise PathSafetyError(f"unsafe legacy event log: {events_path}")
        try:
            lines = events_path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise IntegrityError("cannot read legacy event log") from error
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise IntegrityError("legacy event log contains invalid JSON") from error
            if not isinstance(event, dict):
                raise IntegrityError("legacy event log entry must be an object")
            event_count += 1
    active_attempt = state.get("active_attempt")
    active_attempt_path = (
        f"attempts/{active_attempt}" if isinstance(active_attempt, str) else None
    )
    return {
        "mode": "read_only",
        "run_format_version": RELEASED_RUN_FORMAT_VERSION,
        "run_path": ".",
        "state": state.get("state"),
        "active_attempt": active_attempt,
        "active_attempt_path": active_attempt_path,
        "attempt_count": state.get("attempt_count"),
        "source_manifest_path": "evidence/source_manifest.json",
        "source_input_path": source_input_path,
        "source_status": source_status,
        "source_manifest_sha256": source_manifest_sha256,
        "event_log_path": "events.jsonl",
        "event_count": event_count,
    }


def _load_agent_first_run(run_dir: Path | str) -> tuple[Path, dict[str, Any]]:
    if inspect_run_format(run_dir) != AGENT_FIRST_RUN_FORMAT_VERSION:
        raise StateError("version-1 runs do not support Agent-first source APIs")
    run, state = _load_run(run_dir)
    _regular_tree_inventory(run)
    return run, state


def _open_run_lock(path: Path) -> int:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise PathSafetyError(f"unsafe run advisory lock: {path}")
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        published = path.lstat()
        if (
            not S_ISREG(opened.st_mode)
            or not S_ISREG(published.st_mode)
            or opened.st_nlink != 1
            or published.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (published.st_dev, published.st_ino)
        ):
            raise PathSafetyError(f"unsafe run advisory lock: {path}")
        if opened.st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        elif opened.st_size != 1:
            raise PathSafetyError(f"unsafe run advisory lock size: {path}")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _thread_run_lock(lock_path: Path) -> threading.Lock:
    key = str(lock_path)
    with _THREAD_RUN_LOCKS_GUARD:
        return _THREAD_RUN_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def _advisory_lock(lock_path: Path) -> Iterator[None]:
    thread_lock = _thread_run_lock(lock_path)
    with thread_lock:
        descriptor = _open_run_lock(lock_path)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


@contextmanager
def _run_lock(run: Path) -> Iterator[None]:
    with _advisory_lock(run / ".run.lock"):
        yield


def _agent_first_initialization_lock(run: Path) -> Path:
    return run.parent / f".{run.name}.v2-init.lock"


@contextmanager
def _agent_first_mutation_lock(run: Path) -> Iterator[None]:
    with _advisory_lock(_agent_first_initialization_lock(run)):
        with _run_lock(run):
            yield


def _event(run_dir: Path, event: str, **payload: Any) -> None:
    append_jsonl(run_dir / "events.jsonl", redact_secrets({"event": event, **payload}))


def _write_run(run_dir: Path, state: dict[str, Any]) -> None:
    atomic_write_json(run_dir / "run.json", redact_secrets(state))


def _load_run(run_dir: Path | str) -> tuple[Path, dict[str, Any]]:
    root = Path(run_dir).absolute()
    if root.is_symlink() or not root.is_dir():
        raise PathSafetyError(f"run directory must be a regular directory: {root}")
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            if (current_path / name).is_symlink():
                raise PathSafetyError(
                    f"run tree must not contain symlinks: {current_path / name}"
                )
        for name in files:
            if (current_path / name).is_symlink():
                raise PathSafetyError(
                    f"run tree must not contain symlinks: {current_path / name}"
                )
    state = _read_json(root / "run.json")
    return root, state


def _regular_tree_inventory(path: Path) -> tuple[set[str], set[str]]:
    """List every regular file and directory, rejecting all other entry types."""

    try:
        root_details = path.lstat()
    except OSError as error:
        raise PathSafetyError(f"unsafe staging directory: {path}") from error
    if (
        S_ISLNK(root_details.st_mode)
        or not S_ISDIR(root_details.st_mode)
        or getattr(root_details, "st_file_attributes", 0) & 0x400
    ):
        raise PathSafetyError(f"unsafe staging directory: {path}")
    files: set[str] = set()
    directories: set[str] = set()
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as error:
            raise PathSafetyError(f"unsafe staging directory: {current}") from error
        for entry in entries:
            child = current / entry.name
            try:
                cached = entry.stat(follow_symlinks=False)
                details = os.stat(child, follow_symlinks=False)
            except OSError as error:
                raise PathSafetyError(f"unsafe staging entry: {child}") from error
            relative = child.relative_to(path).as_posix()
            if S_IFMT(cached.st_mode) != S_IFMT(details.st_mode):
                raise PathSafetyError(f"staging entry type changed: {child}")
            if (
                cached.st_dev
                and cached.st_ino
                and (cached.st_dev, cached.st_ino) != (details.st_dev, details.st_ino)
            ):
                raise PathSafetyError(f"staging entry identity changed: {child}")
            if (
                S_ISLNK(details.st_mode)
                or getattr(details, "st_file_attributes", 0) & 0x400
            ):
                raise PathSafetyError(f"unsafe staging symlink: {child}")
            if S_ISDIR(details.st_mode):
                directories.add(relative)
                pending.append(child)
            elif S_ISREG(details.st_mode):
                if details.st_nlink != 1:
                    raise PathSafetyError(f"hardlinked staging file: {child}")
                files.add(relative)
            else:
                raise PathSafetyError(f"nonregular staging entry: {child}")
    return files, directories


def _remove_regular_tree(path: Path) -> None:
    _regular_tree_inventory(path)
    shutil.rmtree(path)


def _unused_sibling(parent: Path, prefix: str) -> Path:
    for _attempt in range(16):
        candidate = parent / f"{prefix}-{secrets.token_hex(16)}"
        try:
            candidate.lstat()
        except FileNotFoundError:
            return candidate
        except OSError as error:
            raise PathSafetyError(f"cannot inspect quarantine path: {candidate}") from error
    raise IntegrityError(f"cannot allocate an unused quarantine path beneath {parent}")


def _read_event_log(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise PathSafetyError(f"unsafe event log: {path}")
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise IntegrityError(f"invalid event log: {path}") from error
        if not isinstance(event, dict):
            raise IntegrityError(f"event log entry must be an object: {path}")
        events.append(event)
    return events


def _agent_first_initial_state(stage: Path) -> dict[str, Any]:
    return {
        "format_version": FORMAT_VERSION,
        "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
        "state": "initialized",
        "active_attempt": None,
        "attempt_count": 0,
        "active_curation_revision": None,
        "active_curation_sha256": None,
        "active_plan_revision": None,
        "active_plan_sha256": None,
        "skill_snapshot_manifest_sha256": sha256_file(
            stage / "skill_snapshot" / "manifest.json"
        ),
        "source_manifest_sha256": sha256_file(
            stage / "evidence" / "source_manifest.json"
        ),
    }


_AGENT_FIRST_INITIAL_DIRECTORIES = (
    "input",
    "evidence/pages",
    "evidence/assets",
    "evidence/reference_images",
    "skill_snapshot/files",
    "attempts",
    "provenance",
    "source-assets/files",
    "source-assets/receipts",
    "source-reviews",
    "curations",
    "plans",
)
_INITIALIZATION_SEAL = ".initialization-seal.json"


def _initialization_seal_payload(
    run: Path,
    *,
    generation_id: str,
) -> dict[str, Any]:
    files, directories = _regular_tree_inventory(run)
    files.discard(_INITIALIZATION_SEAL)
    return {
        "format_version": FORMAT_VERSION,
        "generation_id": generation_id,
        "directories": sorted(directories),
        "files": [
            {"path": relative, "sha256": sha256_file(run / relative)}
            for relative in sorted(files)
        ],
    }


def _seal_agent_first_initialization(run: Path) -> dict[str, Any]:
    seal_path = run / _INITIALIZATION_SEAL
    if seal_path.exists() or seal_path.is_symlink():
        return _validate_agent_first_initialization_seal(run)
    seal = _initialization_seal_payload(run, generation_id=secrets.token_hex(16))
    atomic_write_json(seal_path, seal)
    return _validate_agent_first_initialization_seal(run)


def _validate_agent_first_initialization_seal(run: Path) -> dict[str, Any]:
    seal_path = run / _INITIALIZATION_SEAL
    seal = _read_json(seal_path)
    if seal_path.read_bytes() != _stored_json_bytes(seal):
        raise IntegrityError("Agent-first initialization seal is not canonical JSON")
    generation_id = seal.get("generation_id")
    if (
        set(seal) != {"format_version", "generation_id", "directories", "files"}
        or seal.get("format_version") != FORMAT_VERSION
        or not isinstance(generation_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", generation_id) is None
        or seal != _initialization_seal_payload(run, generation_id=generation_id)
    ):
        raise IntegrityError("Agent-first initialization seal does not match its tree")
    return seal


def _populate_agent_first_run(
    stage: Path,
    skill: Path,
    *,
    release_version: str,
    archive_sha256: str | None,
    fail_at: str | None,
) -> None:
    stage.mkdir()
    for relative in _AGENT_FIRST_INITIAL_DIRECTORIES:
        safe_path(stage, relative).mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for source in _runtime_files(skill):
        relative = source.relative_to(skill).as_posix()
        target = safe_path(stage / "skill_snapshot" / "files", relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(target, source.read_bytes())
        entries.append(
            {
                "path": relative,
                "sha256": sha256_file(source),
                "size": source.stat().st_size,
            }
        )
    atomic_write_json(
        stage / "skill_snapshot" / "manifest.json",
        {
            "format_version": FORMAT_VERSION,
            "release_version": release_version,
            "archive_sha256": archive_sha256,
            "files": entries,
        },
    )
    atomic_write_json(
        stage / "evidence" / "source_manifest.json",
        {"format_version": FORMAT_VERSION, "status": "not_prepared"},
    )
    atomic_write_bytes(stage / "evidence" / "evidence.jsonl", b"")
    atomic_write_json(
        stage / "evidence" / "source_visuals.json",
        {"format_version": FORMAT_VERSION, "visuals": []},
    )
    atomic_write_bytes(stage / "provenance" / "supersessions.jsonl", b"")
    atomic_write_bytes(stage / "events.jsonl", b"")
    atomic_write_bytes(stage / ".run.lock", b"\0")
    if fail_at == "after_init_outputs_staged":
        raise SimulatedCrash("after Agent-first initialization outputs staged")
    _write_run(stage, _agent_first_initial_state(stage))
    if fail_at == "after_init_run_write":
        raise SimulatedCrash("after Agent-first initialization run contract write")


def _validate_agent_first_initialization(
    run: Path,
    skill: Path,
    *,
    release_version: str,
    archive_sha256: str | None,
    require_seal: bool | None = None,
    validate_request: bool = True,
    validate_installed_skill: bool = True,
) -> dict[str, Any]:
    run, state = _load_agent_first_run(run)
    manifest = verify_skill_snapshot(
        run, skill_root=skill if validate_installed_skill else None
    )
    if validate_request and manifest.get("release_version") != release_version:
        raise IntegrityError("requested release version differs from the run snapshot")
    if validate_request and manifest.get("archive_sha256") != archive_sha256:
        raise IntegrityError("requested archive hash differs from the run snapshot")
    expected = _agent_first_initial_state(run)
    if state != expected:
        raise IntegrityError("Agent-first initialized run state is noncanonical")
    source_manifest = _read_json(run / "evidence" / "source_manifest.json")
    if source_manifest != {"format_version": FORMAT_VERSION, "status": "not_prepared"}:
        raise IntegrityError("Agent-first initial source manifest is noncanonical")
    if (run / "evidence" / "evidence.jsonl").read_bytes() != b"":
        raise IntegrityError("Agent-first initial evidence log is not empty")
    if _read_json(run / "evidence" / "source_visuals.json") != {
        "format_version": FORMAT_VERSION,
        "visuals": [],
    }:
        raise IntegrityError("Agent-first initial source visuals are noncanonical")
    expected_files = {
        ".run.lock",
        "run.json",
        "events.jsonl",
        "skill_snapshot/manifest.json",
        "evidence/source_manifest.json",
        "evidence/evidence.jsonl",
        "evidence/source_visuals.json",
        "provenance/supersessions.jsonl",
        *(
            f"skill_snapshot/files/{entry['path']}"
            for entry in manifest["files"]
        ),
    }
    actual_files, actual_directories = _regular_tree_inventory(run)
    has_seal = _INITIALIZATION_SEAL in actual_files
    if require_seal is True and not has_seal:
        raise IntegrityError("Agent-first initialization seal is missing")
    if require_seal is False and has_seal:
        raise IntegrityError("Agent-first initialization was sealed too early")
    if has_seal:
        expected_files.add(_INITIALIZATION_SEAL)
    if actual_files != expected_files:
        raise IntegrityError("Agent-first initialization staging file set is not exact")
    expected_directories: set[str] = set(_AGENT_FIRST_INITIAL_DIRECTORIES)
    for relative in (*_AGENT_FIRST_INITIAL_DIRECTORIES, *expected_files):
        parent = Path(relative).parent
        while parent != Path("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    if actual_directories != expected_directories:
        raise IntegrityError(
            "Agent-first initialization staging directory set is not exact"
        )
    if has_seal:
        _validate_agent_first_initialization_seal(run)
    return state


def _promote_agent_first_initialization(
    stage: Path,
    run: Path,
    skill: Path,
    *,
    release_version: str,
    archive_sha256: str | None,
    fail_at: str | None,
) -> dict[str, Any]:
    seal = _seal_agent_first_initialization(stage)
    state = _validate_agent_first_initialization(
        stage,
        skill,
        release_version=release_version,
        archive_sha256=archive_sha256,
        require_seal=True,
    )
    events = _read_event_log(stage / "events.jsonl")
    if events != [{"event": "run_initialized", "state": "initialized"}]:
        raise IntegrityError("Agent-first initialization event is not exact-once")
    quarantine = run.parent / (
        f".{run.name}.v2-init-quarantine-{seal['generation_id']}"
    )
    if quarantine.exists() or quarantine.is_symlink():
        raise IntegrityError(f"conflicting initialization quarantine: {quarantine}")
    os.replace(stage, run)
    _fsync_directory(run.parent)
    try:
        state = _validate_agent_first_initialization(
            run,
            skill,
            release_version=release_version,
            archive_sha256=archive_sha256,
            require_seal=True,
        )
        if _read_event_log(run / "events.jsonl") != events:
            raise IntegrityError("promoted initialization event log changed")
    except Exception:
        quarantine_target = quarantine
        if quarantine_target.exists() or quarantine_target.is_symlink():
            quarantine_target = _unused_sibling(
                run.parent, f".{run.name}.v2-init-quarantine"
            )
        try:
            os.replace(run, quarantine_target)
            _fsync_directory(run.parent)
        except OSError as error:
            raise PathSafetyError(
                "unsafe promoted initialization could not be quarantined"
            ) from error
        raise
    if fail_at == "after_init_promotion":
        raise SimulatedCrash("after Agent-first initialization promotion")
    return state


def _initialize_agent_first_run(
    run: Path,
    skill: Path,
    *,
    release_version: str,
    archive_sha256: str | None,
    fail_at: str | None,
) -> dict[str, Any]:
    run.parent.mkdir(parents=True, exist_ok=True)
    if run.parent.is_symlink() or not run.parent.is_dir():
        raise PathSafetyError(f"run parent must be a regular directory: {run.parent}")
    stage = run.parent / f".{run.name}.v2-init-staging"
    with _advisory_lock(_agent_first_initialization_lock(run)):
        if run.is_symlink():
            raise PathSafetyError(f"run directory must not be a symlink: {run}")
        if run.exists():
            if not run.is_dir():
                raise PathSafetyError(f"run path must be a directory: {run}")
            preview = _read_json(run / "run.json")
            if preview.get("state") == "initialized":
                try:
                    state = _validate_agent_first_initialization(
                        run,
                        skill,
                        release_version=release_version,
                        archive_sha256=archive_sha256,
                        require_seal=True,
                        validate_request=False,
                        validate_installed_skill=False,
                    )
                except Exception:
                    quarantine = _unused_sibling(
                        run.parent, f".{run.name}.v2-init-quarantine"
                    )
                    try:
                        os.replace(run, quarantine)
                        _fsync_directory(run.parent)
                    except OSError as error:
                        raise PathSafetyError(
                            "unsafe live initialization could not be quarantined"
                        ) from error
                    raise
                manifest = verify_skill_snapshot(run, skill_root=skill)
                if manifest.get("release_version") != release_version:
                    raise IntegrityError(
                        "requested release version differs from the run snapshot"
                    )
                if manifest.get("archive_sha256") != archive_sha256:
                    raise IntegrityError(
                        "requested archive hash differs from the run snapshot"
                    )
                events = _read_event_log(run / "events.jsonl")
                if events != [{"event": "run_initialized", "state": "initialized"}]:
                    raise IntegrityError(
                        "Agent-first initialization event is not exact-once"
                    )
                return state
            run, state = _load_agent_first_run(run)
            manifest = verify_skill_snapshot(run, skill_root=skill)
            if manifest.get("release_version") != release_version:
                raise IntegrityError(
                    "requested release version differs from the run snapshot"
                )
            if manifest.get("archive_sha256") != archive_sha256:
                raise IntegrityError(
                    "requested archive hash differs from the run snapshot"
                )
            events = _read_event_log(run / "events.jsonl")
            if sum(event.get("event") == "run_initialized" for event in events) != 1:
                raise IntegrityError("Agent-first initialization event is not exact-once")
            return state
        if stage.exists() or stage.is_symlink():
            if stage.is_symlink() or not stage.is_dir():
                raise PathSafetyError(f"unsafe initialization staging path: {stage}")
            if not (stage / "run.json").is_file():
                _remove_regular_tree(stage)
            else:
                state = _validate_agent_first_initialization(
                    stage,
                    skill,
                    release_version=release_version,
                    archive_sha256=archive_sha256,
                    require_seal=None,
                )
                events = _read_event_log(stage / "events.jsonl")
                if events not in ([], [{"event": "run_initialized", "state": "initialized"}]):
                    raise IntegrityError("Agent-first initialization event log is noncanonical")
                if not events:
                    _event(stage, "run_initialized", state="initialized")
                if fail_at == "after_init_event_append":
                    raise SimulatedCrash("after Agent-first initialization event append")
                return _promote_agent_first_initialization(
                    stage,
                    run,
                    skill,
                    release_version=release_version,
                    archive_sha256=archive_sha256,
                    fail_at=fail_at,
                )
        _populate_agent_first_run(
            stage,
            skill,
            release_version=release_version,
            archive_sha256=archive_sha256,
            fail_at=fail_at,
        )
        state = _validate_agent_first_initialization(
            stage,
            skill,
            release_version=release_version,
            archive_sha256=archive_sha256,
            require_seal=False,
        )
        _event(stage, "run_initialized", state="initialized")
        if fail_at == "after_init_event_append":
            raise SimulatedCrash("after Agent-first initialization event append")
        return _promote_agent_first_initialization(
            stage,
            run,
            skill,
            release_version=release_version,
            archive_sha256=archive_sha256,
            fail_at=fail_at,
        )


def initialize_run(
    run_dir: Path | str,
    skill_root: Path | str,
    *,
    release_version: str,
    archive_sha256: str | None = None,
    run_format_version: int = RELEASED_RUN_FORMAT_VERSION,
    fail_at: str | None = None,
) -> dict[str, Any]:
    """Create a run and immutable snapshot of all bundled runtime inputs."""

    if (
        not isinstance(run_format_version, int)
        or isinstance(run_format_version, bool)
        or run_format_version
        not in {RELEASED_RUN_FORMAT_VERSION, AGENT_FIRST_RUN_FORMAT_VERSION}
    ):
        raise ContractError(f"unknown run format version: {run_format_version!r}")

    run = Path(run_dir).absolute()
    skill = Path(skill_root).absolute()
    try:
        run.resolve(strict=False).relative_to(skill.resolve(strict=True))
    except ValueError:
        pass
    else:
        raise PathSafetyError("run directory must be outside the installed Skill")
    allowed_failures = {
        None,
        "after_init_outputs_staged",
        "after_init_run_write",
        "after_init_event_append",
        "after_init_promotion",
    }
    if fail_at not in allowed_failures:
        raise ContractError(f"unknown initialization crash boundary: {fail_at}")
    if run_format_version == AGENT_FIRST_RUN_FORMAT_VERSION:
        return _initialize_agent_first_run(
            run,
            skill,
            release_version=release_version,
            archive_sha256=archive_sha256,
            fail_at=fail_at,
        )
    if fail_at is not None:
        raise ContractError("initialization crash boundaries require run format 2")
    if run.exists():
        if (run / "run.json").is_file():
            if inspect_run_format(run) != run_format_version:
                raise IntegrityError("requested run format differs from the existing run")
            manifest = verify_skill_snapshot(run, skill_root=skill)
            if manifest.get("release_version") != release_version:
                raise IntegrityError("requested release version differs from the run snapshot")
            if manifest.get("archive_sha256") != archive_sha256:
                raise IntegrityError("requested archive hash differs from the run snapshot")
            return _read_json(run / "run.json")
        if any(run.iterdir()):
            raise StateError(f"run directory is not empty: {run}")
    run.mkdir(parents=True, exist_ok=True)
    directories = [
        "input", "evidence/pages", "evidence/assets", "evidence/reference_images", "skill_snapshot/files",
        "attempts", "provenance",
    ]
    if run_format_version == AGENT_FIRST_RUN_FORMAT_VERSION:
        directories.extend(
            (
                "source-assets/files",
                "source-assets/receipts",
                "source-reviews",
                "curations",
                "plans",
            )
        )
    for relative in directories:
        safe_path(run, relative).mkdir(parents=True, exist_ok=True)

    entries: list[dict[str, Any]] = []
    for source in _runtime_files(skill):
        relative = source.relative_to(skill).as_posix()
        target = safe_path(run / "skill_snapshot" / "files", relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(target, source.read_bytes())
        entries.append({"path": relative, "sha256": sha256_file(source), "size": source.stat().st_size})
    manifest = {
        "format_version": FORMAT_VERSION,
        "release_version": release_version,
        "archive_sha256": archive_sha256,
        "files": entries,
    }
    manifest_path = run / "skill_snapshot" / "manifest.json"
    atomic_write_json(manifest_path, manifest)
    source_manifest = {"format_version": FORMAT_VERSION, "status": "not_prepared"}
    atomic_write_json(run / "evidence" / "source_manifest.json", source_manifest)
    atomic_write_bytes(run / "evidence" / "evidence.jsonl", b"")
    atomic_write_json(
        run / "evidence" / "source_visuals.json",
        {"format_version": FORMAT_VERSION, "visuals": []},
    )
    if run_format_version == RELEASED_RUN_FORMAT_VERSION:
        state = {
            "format_version": FORMAT_VERSION,
            "state": "initialized",
            "active_attempt": None,
            "attempt_count": 0,
            "skill_snapshot_manifest_sha256": sha256_file(manifest_path),
            "source_manifest_sha256": sha256_file(run / "evidence" / "source_manifest.json"),
        }
    else:
        atomic_write_bytes(run / "provenance" / "supersessions.jsonl", b"")
        state = {
            "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
            "state": "initialized",
            "active_attempt": None,
            "attempt_count": 0,
            "active_curation_revision": None,
            "active_curation_sha256": None,
            "active_plan_revision": None,
            "active_plan_sha256": None,
            "skill_snapshot_manifest_sha256": sha256_file(manifest_path),
            "source_manifest_sha256": sha256_file(run / "evidence" / "source_manifest.json"),
        }
    _write_run(run, state)
    _event(run, "run_initialized", state="initialized")
    return state


def verify_skill_snapshot(run_dir: Path | str, *, skill_root: Path | str | None = None) -> dict[str, Any]:
    """Verify snapshot bytes and, when supplied, installed-Skill drift."""

    run, state = _load_run(run_dir)
    manifest_path = run / "skill_snapshot" / "manifest.json"
    if sha256_file(manifest_path) != state.get("skill_snapshot_manifest_sha256"):
        raise IntegrityError("Skill snapshot manifest hash mismatch")
    manifest = _read_json(manifest_path)
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise IntegrityError("Skill snapshot manifest has no files")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise IntegrityError("invalid Skill snapshot entry")
        relative = entry["path"]
        if relative in seen:
            raise IntegrityError(f"duplicate Skill snapshot entry: {relative}")
        seen.add(relative)
        try:
            snapshot = safe_path(run / "skill_snapshot" / "files", relative, must_exist=True)
        except PathSafetyError as error:
            raise IntegrityError(str(error)) from error
        if snapshot.is_symlink() or sha256_file(snapshot) != entry.get("sha256"):
            raise IntegrityError(f"Skill snapshot file hash mismatch: {relative}")
    snapshot_root = run / "skill_snapshot" / "files"
    actual_files = {
        path.relative_to(snapshot_root).as_posix()
        for path in snapshot_root.rglob("*")
        if path.is_file()
    }
    if actual_files != seen:
        raise IntegrityError("Skill snapshot contains unlisted or missing files")
    if skill_root is not None:
        skill = Path(skill_root).absolute()
        current = {
            path.relative_to(skill).as_posix(): sha256_file(path) for path in _runtime_files(skill)
        }
        expected = {entry["path"]: entry["sha256"] for entry in entries}
        if current != expected:
            raise IntegrityError("installed Skill drifted from the run snapshot")
    return manifest


def transition_state(run_dir: Path | str, new_state: str, **updates: Any) -> dict[str, Any]:
    """Advance one main-state edge; side states use :func:`mark_side_state`."""

    run, state = _load_run(run_dir)
    current = state.get("state")
    if _TRANSITIONS.get(current) != new_state:
        raise StateError(f"invalid state transition: {current} -> {new_state}")
    state.update(redact_secrets(updates))
    state["state"] = new_state
    state.pop("reason", None)
    _write_run(run, state)
    _event(run, "state_transition", previous=current, state=new_state)
    return state


def mark_side_state(run_dir: Path | str, side_state: str, *, reason: str) -> dict[str, Any]:
    """Persist an explicit blocked, failed, or visual-review side state."""

    if side_state not in SIDE_STATES:
        raise StateError(f"unknown side state: {side_state}")
    run, state = _load_run(run_dir)
    previous = state.get("state")
    if previous == "finalized" or (run / "final").exists():
        raise StateError("finalized is a terminal state")
    resume_from = state.get("resume_from") if previous in SIDE_STATES else previous
    state.update({"state": side_state, "reason": redact_secrets(reason), "resume_from": resume_from})
    _write_run(run, state)
    _event(run, "side_state", previous=previous, state=side_state, reason=reason)
    return state


def save_plan(run_dir: Path | str, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Persist a non-empty plan and advance initialized -> planned."""

    run, state = _load_run(run_dir)
    source_manifest = _verify_source_contract(run, state)
    if source_manifest.get("status") != "ready":
        raise StateError("planning requires a fully prepared source")
    if state.get("state") == "planned" and (run / "plan.json").is_file():
        existing = _read_json(run / "plan.json")
        if existing != dict(plan):
            raise StateError("refusing to overwrite an existing plan")
        return existing
    if state.get("state") != "initialized" or not plan:
        raise StateError("a non-empty plan requires initialized state")
    clean = redact_secrets(dict(plan))
    atomic_write_json(run / "plan.json", clean)
    transition_state(run, "planned")
    return clean


def begin_attempt(run_dir: Path | str) -> str:
    """Start, or idempotently return, the active authoring attempt."""

    run, state = _load_run(run_dir)
    if state.get("state") == "authoring" and state.get("active_attempt"):
        return str(state["active_attempt"])
    repair = state.get("state") == "failed" and isinstance(state.get("active_attempt"), str)
    if state.get("state") != "planned" and not repair:
        raise StateError(f"cannot begin attempt from {state.get('state')}")
    attempt_number = int(state.get("attempt_count", 0)) + 1
    attempt_id = f"{attempt_number:02d}"
    attempt_root = safe_path(run / "attempts", attempt_id)
    if attempt_root.exists():
        raise IntegrityError(f"attempt directory already exists: {attempt_id}")
    (attempt_root / "artifact").mkdir(parents=True)
    (attempt_root / "qa" / "previews").mkdir(parents=True)
    if repair:
        previous_attempt = state["active_attempt"]
        state.update(
            {
                "state": "authoring",
                "active_attempt": attempt_id,
                "attempt_count": attempt_number,
                "repair_of": previous_attempt,
            }
        )
        state.pop("reason", None)
        state.pop("resume_from", None)
        _write_run(run, state)
        _event(run, "repair_attempt_started", attempt_id=attempt_id, repair_of=previous_attempt)
    else:
        transition_state(run, "authoring", active_attempt=attempt_id, attempt_count=attempt_number)
    _event(run, "attempt_started", attempt_id=attempt_id)
    return attempt_id


def _text_evidence(text: str, *, markdown: bool) -> list[dict[str, Any]]:
    lines = text.splitlines()
    segments: list[tuple[str | None, int, int, str]] = []
    if markdown:
        headings: list[tuple[int, str]] = []
        for index, line in enumerate(lines, start=1):
            match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
            if match:
                headings.append((index, match.group(1).strip()))
        if headings:
            preamble_end = headings[0][0] - 1
            preamble_lines = lines[:preamble_end]
            while preamble_lines and not preamble_lines[0].strip():
                preamble_lines.pop(0)
            while preamble_lines and not preamble_lines[-1].strip():
                preamble_lines.pop()
            if preamble_lines:
                preamble_start = next(
                    index
                    for index, line in enumerate(lines[:preamble_end], start=1)
                    if line.strip()
                )
                segments.append(
                    (
                        None,
                        preamble_start,
                        preamble_start + len(preamble_lines) - 1,
                        "\n".join(preamble_lines),
                    )
                )
            for position, (line_number, heading) in enumerate(headings):
                end = (
                    headings[position + 1][0] - 1
                    if position + 1 < len(headings)
                    else len(lines)
                )
                section_lines = lines[line_number - 1 : end]
                while section_lines and not section_lines[-1].strip():
                    section_lines.pop()
                    end -= 1
                if section_lines:
                    segments.append(
                        (heading, line_number, end, "\n".join(section_lines))
                    )
    if not segments:
        start: int | None = None
        buffer: list[str] = []
        for index, line in enumerate(lines + [""], start=1):
            if line.strip():
                if start is None:
                    start = index
                buffer.append(line)
            elif buffer and start is not None:
                segments.append((None, start, index - 1, "\n".join(buffer).strip()))
                start, buffer = None, []
    evidence: list[dict[str, Any]] = []
    for index, (heading, start, end, body) in enumerate(segments, start=1):
        anchor: dict[str, Any] = {"line_start": start, "line_end": end}
        if heading is not None:
            anchor.update({"type": "markdown_section", "heading": heading})
        else:
            anchor["type"] = "text_lines"
        evidence.append(
            {
                "id": f"ev-{index:03d}", "kind": "text", "text": body,
                "safe_to_quote": True, "anchor": anchor,
                "sha256": sha256_bytes(body.encode("utf-8")),
            }
        )
    return evidence


def _pdf_evidence(text: str) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for page_number, page_text in enumerate(text.split("\f"), start=1):
        page_items = _text_evidence(page_text, markdown=False)
        for paragraph_number, item in enumerate(page_items, start=1):
            item["id"] = f"ev-{len(evidence) + 1:03d}"
            item["anchor"] = {
                **item["anchor"],
                "type": "pdf_page_text",
                "page": page_number,
                "paragraph": paragraph_number,
            }
            evidence.append(item)
    return evidence


def _write_evidence(run: Path, evidence: Sequence[Mapping[str, Any]]) -> None:
    data = b"".join(_canonical_json_bytes(dict(item)) for item in evidence)
    atomic_write_bytes(run / "evidence" / "evidence.jsonl", data)


def _visual_record(
    visual_id: str,
    relative_path: str,
    digest: str,
    *,
    origin: str,
    page: int | None = None,
    pdf_image_num: int | None = None,
) -> dict[str, Any]:
    eligible = origin == "explicit_asset"
    style_only = origin == "style_reference"
    return {
        "id": visual_id, "path": relative_path, "sha256": digest, "origin": origin,
        "page": page, "pdf_image_num": pdf_image_num, "bbox": None,
        "caption_evidence_id": None,
        "crop": False, "compound": False, "vlm_review": None,
        "eligibility": "eligible" if eligible else ("style_only" if style_only else "review_required"),
        "allowed_content_roles": list(_DEFAULT_VISUAL_ROLES) if eligible else [],
        "max_reuse": 1,
    }


def _clear_stale_source_outputs(run: Path) -> None:
    for directory, pattern in (
        (run / "evidence" / "pages", "*"),
        (run / "evidence" / "assets", "pdf-image*"),
    ):
        for path in directory.glob(pattern):
            if path.is_symlink():
                raise PathSafetyError(f"source output must not be a symlink: {path}")
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    for name in (
        "source.txt",
        "pdfinfo.txt",
        "pdfimages-list.txt",
        "host-vlm-visual-review.json",
    ):
        path = run / "evidence" / name
        if path.is_symlink():
            raise PathSafetyError(f"source output must not be a symlink: {path}")
        path.unlink(missing_ok=True)


def _parse_pdfimages_list(text: str) -> list[tuple[int, int]]:
    mappings: list[tuple[int, int]] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        try:
            page = int(fields[0])
            number = int(fields[1])
        except ValueError:
            continue
        if page > 0 and number >= 0:
            mappings.append((page, number))
    return mappings


def _route_poppler(
    run: Path, input_source: Path, resolved: Mapping[str, str]
) -> tuple[Path, dict[str, subprocess.CompletedProcess[str]]]:
    """Run the verified Poppler command set against one immutable run input."""

    text_path = run / "evidence" / "source.txt"
    info = subprocess.run(
        [resolved["pdfinfo"], str(input_source)],
        text=True,
        capture_output=True,
        check=False,
    )
    atomic_write_bytes(run / "evidence" / "pdfinfo.txt", info.stdout.encode("utf-8"))
    text_result = subprocess.run(
        [resolved["pdftotext"], str(input_source), str(text_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    page_result = subprocess.run(
        [
            resolved["pdftoppm"],
            "-png",
            "-r",
            "144",
            str(input_source),
            str(run / "evidence" / "pages" / "page"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    image_list = subprocess.run(
        [resolved["pdfimages"], "-list", str(input_source)],
        text=True,
        capture_output=True,
        check=False,
    )
    atomic_write_bytes(
        run / "evidence" / "pdfimages-list.txt", image_list.stdout.encode("utf-8")
    )
    image_result = subprocess.run(
        [
            resolved["pdfimages"],
            "-png",
            str(input_source),
            str(run / "evidence" / "assets" / "pdf-image"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    return text_path, {
        "pdfinfo": info,
        "pdftotext": text_result,
        "pdftoppm": page_result,
        "pdfimages_list": image_list,
        "pdfimages_extract": image_result,
    }


def _run_relative_diagnostic(run: Path, text: str, *additional_roots: Path) -> str:
    redacted = str(redact_secrets(text))
    for root in (run, *additional_roots):
        for prefix in {str(root), root.as_posix()}:
            redacted = redacted.replace(f"{prefix}{os.sep}", "")
            redacted = redacted.replace(f"{prefix}/", "")
            redacted = redacted.replace(prefix, ".")
    return redacted


def _png_dimensions(path: Path) -> tuple[int, int]:
    """Validate one complete PNG stream and return its bound dimensions."""

    maximum_file_bytes = 256 * 1024 * 1024
    maximum_chunk_bytes = 256 * 1024 * 1024
    maximum_image_bytes = 512 * 1024 * 1024
    try:
        published = path.lstat()
    except OSError as error:
        raise IntegrityError(f"cannot read rendered page: {path}") from error
    if (
        S_ISLNK(published.st_mode)
        or not S_ISREG(published.st_mode)
        or published.st_nlink != 1
    ):
        raise PathSafetyError(f"rendered page must be a single-link regular file: {path}")
    if published.st_size > maximum_file_bytes:
        raise IntegrityError(f"rendered PNG file is too large: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise IntegrityError(f"cannot read rendered page: {path}") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (published.st_dev, published.st_ino)
        ):
            raise PathSafetyError(
                f"rendered page identity changed before validation: {path}"
            )
        if opened.st_size != published.st_size or opened.st_size > maximum_file_bytes:
            raise IntegrityError(f"rendered PNG file size changed before validation: {path}")
        remaining = opened.st_size
        parts: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise IntegrityError(f"rendered PNG was truncated while reading: {path}")
            parts.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise IntegrityError(f"rendered PNG grew while reading: {path}")
        final = os.fstat(descriptor)
        if (
            final.st_size != opened.st_size
            or final.st_nlink != 1
            or (final.st_dev, final.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise IntegrityError(f"rendered PNG changed while reading: {path}")
        data = b"".join(parts)
    except OSError as error:
        raise IntegrityError(f"cannot read rendered page: {path}") from error
    finally:
        os.close(descriptor)
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise IntegrityError(f"rendered page is not a canonical PNG: {path}")

    offset = 8
    chunk_index = 0
    header: tuple[int, int, int, int, int, int, int] | None = None
    palette_entries: int | None = None
    seen_idat = False
    idat_closed = False
    seen_iend = False
    compressed_parts: list[bytes] = []
    compressed_size = 0
    while offset < len(data):
        if len(data) - offset < 12:
            raise IntegrityError(f"rendered PNG has a truncated chunk: {path}")
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        if length > maximum_chunk_bytes or length > len(data) - offset - 12:
            raise IntegrityError(f"rendered PNG has an invalid chunk length: {path}")
        kind = data[offset + 4 : offset + 8]
        payload_start = offset + 8
        payload_end = payload_start + length
        payload = data[payload_start:payload_end]
        stored_crc = struct.unpack(">I", data[payload_end : payload_end + 4])[0]
        if (
            len(kind) != 4
            or any(not (65 <= byte <= 90 or 97 <= byte <= 122) for byte in kind)
            or kind[2] & 0x20
        ):
            raise IntegrityError(f"rendered PNG has an invalid chunk type: {path}")
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != stored_crc:
            raise IntegrityError(f"rendered PNG has a chunk CRC mismatch: {path}")
        offset = payload_end + 4

        if chunk_index == 0 and kind != b"IHDR":
            raise IntegrityError(f"rendered PNG does not start with IHDR: {path}")
        if kind == b"IHDR":
            if chunk_index != 0 or header is not None or length != 13:
                raise IntegrityError(f"rendered PNG has an invalid IHDR: {path}")
            header = struct.unpack(">IIBBBBB", payload)
            width, height, bit_depth, color_type, compression, filtering, interlace = header
            valid_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if (
                width < 1
                or height < 1
                or color_type not in valid_depths
                or bit_depth not in valid_depths[color_type]
                or compression != 0
                or filtering != 0
                or interlace not in {0, 1}
            ):
                raise IntegrityError(f"rendered PNG has invalid IHDR parameters: {path}")
        elif header is None:
            raise IntegrityError(f"rendered PNG is missing IHDR: {path}")
        elif kind == b"PLTE":
            if seen_idat or palette_entries is not None or length == 0 or length % 3:
                raise IntegrityError(f"rendered PNG has an invalid palette: {path}")
            palette_entries = length // 3
            if (
                palette_entries > 256
                or header[3] in {0, 4}
                or (header[3] == 3 and palette_entries > 2 ** header[2])
            ):
                raise IntegrityError(f"rendered PNG has an invalid palette: {path}")
        elif kind == b"IDAT":
            if idat_closed or (header[3] == 3 and palette_entries is None):
                raise IntegrityError(f"rendered PNG has out-of-order image data: {path}")
            seen_idat = True
            compressed_size += length
            if compressed_size > maximum_chunk_bytes:
                raise IntegrityError(f"rendered PNG image data is too large: {path}")
            compressed_parts.append(payload)
        elif kind == b"IEND":
            if length != 0 or not seen_idat:
                raise IntegrityError(f"rendered PNG has an invalid IEND: {path}")
            seen_iend = True
            if offset != len(data):
                raise IntegrityError(f"rendered PNG has trailing data: {path}")
            break
        else:
            if seen_idat:
                idat_closed = True
            if kind[0] & 0x20 == 0:
                raise IntegrityError(f"rendered PNG has an unknown critical chunk: {path}")
        chunk_index += 1

    if header is None or not seen_iend or (header[3] == 3 and palette_entries is None):
        raise IntegrityError(f"rendered PNG is structurally incomplete: {path}")
    width, height, bit_depth, color_type, _compression, _filtering, interlace = header
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    bits_per_pixel = channels * bit_depth
    passes = (
        ((0, 0, 1, 1),)
        if interlace == 0
        else (
            (0, 0, 8, 8),
            (4, 0, 8, 8),
            (0, 4, 4, 8),
            (2, 0, 4, 4),
            (0, 2, 2, 4),
            (1, 0, 2, 2),
            (0, 1, 1, 2),
        )
    )
    rows: list[tuple[int, int]] = []
    expected_size = 0
    for x_start, y_start, x_step, y_step in passes:
        pass_width = 0 if width <= x_start else (width - x_start + x_step - 1) // x_step
        pass_height = 0 if height <= y_start else (height - y_start + y_step - 1) // y_step
        if pass_width == 0 or pass_height == 0:
            continue
        row_bytes = (pass_width * bits_per_pixel + 7) // 8
        rows.append((pass_height, row_bytes))
        expected_size += pass_height * (row_bytes + 1)
        if expected_size > maximum_image_bytes:
            raise IntegrityError(f"rendered PNG expands beyond the safety bound: {path}")
    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(b"".join(compressed_parts), expected_size + 1)
        if decompressor.unconsumed_tail:
            raise IntegrityError(f"rendered PNG expands beyond its dimensions: {path}")
        raw += decompressor.flush()
    except zlib.error as error:
        raise IntegrityError(f"rendered PNG has invalid compressed image data: {path}") from error
    if (
        not decompressor.eof
        or decompressor.unused_data
        or len(raw) != expected_size
    ):
        raise IntegrityError(f"rendered PNG image data does not match its dimensions: {path}")
    raw_offset = 0
    for row_count, row_bytes in rows:
        for _row in range(row_count):
            if raw[raw_offset] > 4:
                raise IntegrityError(f"rendered PNG has an invalid row filter: {path}")
            raw_offset += row_bytes + 1
    return width, height


def _canonicalize_rendered_pages(run: Path) -> list[dict[str, Any]]:
    pages_root = run / "evidence" / "pages"
    numbered: dict[int, Path] = {}
    for path in pages_root.iterdir():
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise PathSafetyError(f"PDF renderer produced an unsafe page output: {path}")
        match = re.fullmatch(r"page-(\d+)\.png", path.name)
        if match is None:
            raise IntegrityError(f"PDF renderer produced an unexpected page: {path.name}")
        number = int(match.group(1))
        if number < 1 or number in numbered:
            raise IntegrityError("PDF renderer produced duplicate or invalid page numbers")
        numbered[number] = path
    if sorted(numbered) != list(range(1, len(numbered) + 1)):
        raise IntegrityError("PDF renderer page numbers are not contiguous")
    pages: list[dict[str, Any]] = []
    for number in sorted(numbered):
        source = numbered[number]
        target = pages_root / f"page-{number:04d}.png"
        if source != target:
            if target.exists() or target.is_symlink():
                raise IntegrityError(f"canonical PDF page already exists: {target.name}")
            source.rename(target)
        width, height = _png_dimensions(target)
        pages.append(
            {
                "page": number,
                "path": target.relative_to(run).as_posix(),
                "sha256": sha256_file(target),
                "width": width,
                "height": height,
                "renderer": "pdftoppm",
                "dpi": 144,
                "pdf_page_box": "poppler_default",
                "effective_rotation": 0,
            }
        )
    return pages


def _pdfimage_hints(run: Path, image_list: str) -> list[dict[str, Any]]:
    mappings = _parse_pdfimages_list(image_list)
    assets_root = run / "evidence" / "assets"
    extracted = sorted(assets_root.glob("pdf-image*"))
    if any(
        path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1
        for path in extracted
    ):
        raise PathSafetyError("pdfimages produced an unsafe extraction hint")
    if len(extracted) != len(mappings):
        raise IntegrityError("PDF image extraction could not be mapped to page/object metadata")
    return [
        {
            "path": path.relative_to(run).as_posix(),
            "sha256": sha256_file(path),
            "page": page,
            "object_number": object_number,
            "trust": "untrusted_hint",
            "eligible": False,
        }
        for path, (page, object_number) in zip(extracted, mappings)
    ]


def _prepare_source_v2(
    run: Path,
    state: dict[str, Any],
    source_path: Path | str,
    *,
    extra_assets: Sequence[Path | str],
    reference_images: Sequence[Path | str],
    tool_paths: Mapping[str, str | Path | None] | None,
    poppler_input: Path | None = None,
) -> dict[str, Any]:
    if state.get("state") not in {"initialized", "blocked"}:
        raise StateError("source preparation must occur before curation")
    existing_manifest = _read_json(run / "evidence" / "source_manifest.json")
    if existing_manifest.get("status") == "ready":
        raise StateError("a ready source cannot be replaced; initialize a new run")
    source = Path(source_path).absolute()
    if source.is_symlink() or not source.is_file() or source.stat().st_nlink != 1:
        raise PathSafetyError(f"source must be a regular non-symlink file: {source}")
    suffix = source.suffix.lower()
    source_type = {
        ".md": "markdown",
        ".markdown": "markdown",
        ".txt": "text",
        ".pdf": "pdf",
    }.get(suffix)
    if source_type is None:
        raise ContractError(f"unsupported source type: {suffix or '<none>'}")
    if extra_assets or reference_images:
        raise ContractError("Agent-first source preparation accepts only the primary source")
    input_name = f"source{suffix}"
    input_source = run / "input" / input_name
    input_entries = list((run / "input").iterdir())
    if input_entries:
        if (
            input_entries != [input_source]
            or input_source.is_symlink()
            or not input_source.is_file()
            or input_source.stat().st_nlink != 1
            or sha256_file(input_source) != sha256_file(source)
        ):
            raise StateError("an Agent-first run cannot replace its immutable source")
    else:
        atomic_write_bytes(input_source, source.read_bytes())

    _clear_stale_source_outputs(run)
    for name in ("page-manifest.json", "pdfimages-hints.json"):
        path = run / "evidence" / name
        if path.is_symlink():
            raise PathSafetyError(f"source output must not be a symlink: {path}")
        path.unlink(missing_ok=True)
    evidence: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "status": "ready",
        "source_type": source_type,
        "input_path": f"input/{input_name}",
        "source_sha256": sha256_file(input_source),
        "source_size": input_source.stat().st_size,
        "tools": {},
    }

    if source_type in {"markdown", "text"}:
        text = input_source.read_text(encoding="utf-8")
        atomic_write_bytes(run / "evidence" / "source.txt", text.encode("utf-8"))
        evidence = _text_evidence(text, markdown=source_type == "markdown")
    else:
        required = ("pdftotext", "pdfinfo", "pdftoppm", "pdfimages")
        resolved: dict[str, str | None] = {}
        for name in required:
            configured = tool_paths.get(name) if tool_paths is not None else shutil.which(name)
            resolved[name] = str(configured) if configured else None
        missing = sorted(name for name, path in resolved.items() if path is None)
        manifest["tools"] = {
            name: {
                "available": path is not None,
                "identity": Path(path).name if path is not None else None,
            }
            for name, path in resolved.items()
        }
        if missing:
            manifest.update({"status": "blocked", "missing_tools": missing})
            _write_evidence(run, [])
            atomic_write_json(
                run / "evidence" / "source_visuals.json",
                {"format_version": FORMAT_VERSION, "visuals": []},
            )
            _persist_source_manifest(run, manifest)
            mark_side_state(
                run,
                "blocked",
                reason=f"missing required PDF tools: {', '.join(missing)}",
            )
            return manifest
        routed = {name: path for name, path in resolved.items() if path is not None}
        text_path, commands = _route_poppler(
            run, poppler_input if poppler_input is not None else input_source, routed
        )
        failed = sorted(
            name for name, result in commands.items() if result.returncode != 0
        )
        if not list((run / "evidence" / "pages").iterdir()):
            failed.append("pdftoppm_output")
        if not text_path.is_file():
            failed.append("pdftotext_output")
        diagnostic_roots = (
            () if poppler_input is None else (poppler_input.parent.parent,)
        )
        manifest["commands"] = {
            name: {
                "returncode": result.returncode,
                "stderr": _run_relative_diagnostic(
                    run, result.stderr, *diagnostic_roots
                ),
            }
            for name, result in commands.items()
        }
        if failed:
            manifest.update({"status": "blocked", "failed_commands": sorted(set(failed))})
            _write_evidence(run, [])
            atomic_write_json(
                run / "evidence" / "source_visuals.json",
                {"format_version": FORMAT_VERSION, "visuals": []},
            )
            _persist_source_manifest(run, manifest)
            mark_side_state(
                run,
                "blocked",
                reason=f"PDF preparation failed: {', '.join(manifest['failed_commands'])}",
            )
            return manifest
        pages = _canonicalize_rendered_pages(run)
        hints = _pdfimage_hints(run, commands["pdfimages_list"].stdout)
        page_manifest = {
            "format_version": FORMAT_VERSION,
            "source_path": manifest["input_path"],
            "source_sha256": manifest["source_sha256"],
            "pages": pages,
        }
        hints_manifest = {
            "format_version": FORMAT_VERSION,
            "source_path": manifest["input_path"],
            "source_sha256": manifest["source_sha256"],
            "hints": hints,
        }
        page_manifest_path = run / "evidence" / "page-manifest.json"
        hints_manifest_path = run / "evidence" / "pdfimages-hints.json"
        atomic_write_json(page_manifest_path, page_manifest)
        atomic_write_json(hints_manifest_path, hints_manifest)
        manifest.update(
            {
                "page_manifest_path": "evidence/page-manifest.json",
                "page_manifest_sha256": sha256_file(page_manifest_path),
                "pdfimages_hints_path": "evidence/pdfimages-hints.json",
                "pdfimages_hints_sha256": sha256_file(hints_manifest_path),
                "rendered_pages": {page["path"]: page["sha256"] for page in pages},
            }
        )
        text = text_path.read_text(encoding="utf-8", errors="replace")
        evidence = _pdf_evidence(text)

    _write_evidence(run, evidence)
    atomic_write_json(
        run / "evidence" / "source_visuals.json",
        {"format_version": FORMAT_VERSION, "visuals": []},
    )
    manifest.update(
        {
            "source_text_sha256": sha256_file(run / "evidence" / "source.txt"),
            "evidence_sha256": sha256_file(run / "evidence" / "evidence.jsonl"),
            "source_visuals_sha256": sha256_file(
                run / "evidence" / "source_visuals.json"
            ),
            "evidence_count": len(evidence),
            "visual_count": 0,
        }
    )
    _persist_source_manifest(run, manifest)
    current = _read_json(run / "run.json")
    current["state"] = "curating"
    current.pop("reason", None)
    current.pop("resume_from", None)
    _write_run(run, current)
    if state.get("state") == "blocked":
        _event(run, "source_blocker_resolved", state="curating")
    _event(
        run,
        "source_prepared",
        source_type=source_type,
        evidence_count=len(evidence),
        visual_count=0,
    )
    return manifest


_SOURCE_TRANSACTION_KEYS = {
    "format_version",
    "operation",
    "previous_run_sha256",
    "previous_source_manifest_sha256",
    "source_sha256",
    "source_suffix",
    "files",
    "final_run_sha256",
    "final_source_manifest_sha256",
    "previous_events",
    "new_events",
    "transaction_sha256",
}
_SOURCE_SINGLE_FILES = {
    "evidence/source.txt",
    "evidence/pdfinfo.txt",
    "evidence/pdfimages-list.txt",
    "evidence/page-manifest.json",
    "evidence/pdfimages-hints.json",
    "evidence/evidence.jsonl",
    "evidence/source_visuals.json",
    "evidence/source_manifest.json",
}
_SOURCE_STAGE_DIRECTORIES = {
    "input",
    "evidence",
    "evidence/pages",
    "evidence/assets",
    "evidence/reference_images",
}
_SOURCE_SEED_CLAIM = "source-seed-claim.json"
_SOURCE_SEED_CLAIM_KEYS = {
    "format_version",
    "operation",
    "generation_id",
    "previous_run_sha256",
    "previous_source_manifest_sha256",
    "source_sha256",
    "source_suffix",
    "claim_sha256",
}


def _source_pretransaction_file_is_expected(relative: str) -> bool:
    return (
        relative in {"run.json", "events.jsonl", _SOURCE_SEED_CLAIM}
        or relative in _SOURCE_SINGLE_FILES
        or re.fullmatch(r"input/source(?:\.md|\.markdown|\.txt|\.pdf)", relative)
        is not None
        or re.fullmatch(r"evidence/pages/page-\d+\.png", relative) is not None
        or re.fullmatch(r"evidence/assets/pdf-image[^/]+", relative) is not None
    )


def _source_seed_claim(
    run: Path,
    state: Mapping[str, Any],
    source: Path,
) -> dict[str, Any]:
    claim: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "operation": "prepare_source_seed",
        "generation_id": secrets.token_hex(16),
        "previous_run_sha256": sha256_file(run / "run.json"),
        "previous_source_manifest_sha256": state["source_manifest_sha256"],
        "source_sha256": sha256_file(source),
        "source_suffix": source.suffix.lower(),
    }
    claim["claim_sha256"] = _canonical_hash(claim)
    return claim


def _validate_source_seed_claim(
    stage: Path,
    source: Path,
    *,
    previous_run_sha256: str,
    previous_source_manifest_sha256: str,
) -> dict[str, Any]:
    claim_path = stage / _SOURCE_SEED_CLAIM
    claim = _read_json(claim_path)
    if claim_path.read_bytes() != _stored_json_bytes(claim):
        raise IntegrityError("source seed claim is not canonical JSON")
    unsigned = dict(claim)
    stored_hash = unsigned.pop("claim_sha256", None)
    generation_id = claim.get("generation_id")
    if (
        set(claim) != _SOURCE_SEED_CLAIM_KEYS
        or claim.get("format_version") != FORMAT_VERSION
        or claim.get("operation") != "prepare_source_seed"
        or not isinstance(generation_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", generation_id) is None
        or not isinstance(stored_hash, str)
        or stored_hash != _canonical_hash(unsigned)
        or claim.get("previous_run_sha256") != previous_run_sha256
        or claim.get("previous_source_manifest_sha256")
        != previous_source_manifest_sha256
        or claim.get("source_suffix") != source.suffix.lower()
        or claim.get("source_sha256") != sha256_file(source)
    ):
        raise IntegrityError("source seed claim does not match its request")
    return claim


def _discard_incomplete_source_stage(
    run: Path,
    state: Mapping[str, Any],
    stage: Path,
    source: Path,
) -> None:
    files, directories = _regular_tree_inventory(stage)
    if not files and not directories:
        try:
            stage.rmdir()
            _fsync_directory(run)
        except OSError as error:
            raise PathSafetyError(
                "empty source staging directory changed before removal"
            ) from error
        return
    if _SOURCE_SEED_CLAIM not in files:
        raise IntegrityError("incomplete source staging tree has no seed claim")
    claim = _validate_source_seed_claim(
        stage,
        source,
        previous_run_sha256=sha256_file(run / "run.json"),
        previous_source_manifest_sha256=state["source_manifest_sha256"],
    )
    unexpected_files = {
        relative for relative in files if not _source_pretransaction_file_is_expected(relative)
    }
    if unexpected_files or not directories.issubset(_SOURCE_STAGE_DIRECTORIES):
        raise IntegrityError("incomplete source staging tree is not process-owned")
    quarantine = run / f".source-prep-quarantine-{claim['generation_id']}"
    if quarantine.exists() or quarantine.is_symlink():
        raise IntegrityError(f"conflicting source staging quarantine: {quarantine}")
    os.replace(stage, quarantine)
    _fsync_directory(run)
    quarantined_files, quarantined_directories = _regular_tree_inventory(quarantine)
    _validate_source_seed_claim(
        quarantine,
        source,
        previous_run_sha256=sha256_file(run / "run.json"),
        previous_source_manifest_sha256=state["source_manifest_sha256"],
    )
    unexpected_files = {
        relative
        for relative in quarantined_files
        if not _source_pretransaction_file_is_expected(relative)
    }
    if (
        unexpected_files
        or not quarantined_directories.issubset(_SOURCE_STAGE_DIRECTORIES)
    ):
        raise IntegrityError("quarantined source staging tree changed before cleanup")
    _remove_regular_tree(quarantine)


def _source_stage_files(stage: Path) -> dict[str, str]:
    staged_files, _directories = _regular_tree_inventory(stage)
    files: dict[str, str] = {}
    for relative in sorted(staged_files):
        if not relative.startswith(("input/", "evidence/")):
            continue
        if (
            relative not in _SOURCE_SINGLE_FILES
            and not relative.startswith("input/")
            and re.fullmatch(r"evidence/pages/page-\d{4}\.png", relative) is None
            and re.fullmatch(r"evidence/assets/pdf-image[^/]+", relative) is None
        ):
            raise IntegrityError(f"unexpected source staging file: {relative}")
        files[relative] = sha256_file(stage / relative)
    return files


def _seed_source_stage(
    run: Path,
    state: Mapping[str, Any],
    stage: Path,
    source: Path,
    *,
    fail_at: str | None,
) -> None:
    stage.mkdir()
    if fail_at == "after_source_stage_mkdir":
        raise SimulatedCrash("after source staging directory creation")
    atomic_write_json(stage / _SOURCE_SEED_CLAIM, _source_seed_claim(run, state, source))
    for relative in (
        "input",
        "evidence/pages",
        "evidence/assets",
        "evidence/reference_images",
    ):
        safe_path(stage, relative).mkdir(parents=True, exist_ok=True)
    for source in sorted((run / "input").iterdir()):
        if source.is_symlink() or not source.is_file() or source.stat().st_nlink != 1:
            raise PathSafetyError(f"unsafe Agent-first source input: {source}")
        atomic_write_bytes(stage / "input" / source.name, source.read_bytes())
    if fail_at == "after_source_seed_input":
        raise SimulatedCrash("after source staging input seed")
    for relative in (
        "evidence/source_manifest.json",
        "evidence/evidence.jsonl",
        "evidence/source_visuals.json",
    ):
        source = safe_path(run, relative, must_exist=True)
        atomic_write_bytes(safe_path(stage, relative), source.read_bytes())
    _write_run(stage, dict(state))
    atomic_write_bytes(stage / "events.jsonl", (run / "events.jsonl").read_bytes())


def _create_source_transaction(
    run: Path,
    stage: Path,
    source: Path,
    *,
    previous_state: Mapping[str, Any],
    previous_events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    _validate_source_seed_claim(
        stage,
        source,
        previous_run_sha256=sha256_file(run / "run.json"),
        previous_source_manifest_sha256=previous_state[
            "source_manifest_sha256"
        ],
    )
    staged_events = _read_event_log(stage / "events.jsonl")
    previous = [dict(event) for event in previous_events]
    if staged_events[: len(previous)] != previous:
        raise IntegrityError("source staging event prefix changed")
    transaction: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "operation": "prepare_source",
        "previous_run_sha256": sha256_file(run / "run.json"),
        "previous_source_manifest_sha256": previous_state[
            "source_manifest_sha256"
        ],
        "source_sha256": sha256_file(source),
        "source_suffix": source.suffix.lower(),
        "files": _source_stage_files(stage),
        "final_run_sha256": sha256_file(stage / "run.json"),
        "final_source_manifest_sha256": sha256_file(
            stage / "evidence" / "source_manifest.json"
        ),
        "previous_events": previous,
        "new_events": staged_events[len(previous) :],
    }
    transaction["transaction_sha256"] = _canonical_hash(transaction)
    atomic_write_json(stage / "transaction.json", transaction)
    return transaction


def _validate_source_transaction(
    run: Path,
    stage: Path,
    source: Path,
) -> dict[str, Any]:
    actual_stage_files, actual_stage_directories = _regular_tree_inventory(stage)
    transaction_path = stage / "transaction.json"
    transaction = _read_json(transaction_path)
    if transaction_path.read_bytes() != _stored_json_bytes(transaction):
        raise IntegrityError("source transaction is not canonical JSON")
    if set(transaction) != _SOURCE_TRANSACTION_KEYS:
        raise IntegrityError("source transaction has invalid keys")
    stored_hash = transaction.get("transaction_sha256")
    unsigned = dict(transaction)
    unsigned.pop("transaction_sha256", None)
    if not isinstance(stored_hash, str) or stored_hash != _canonical_hash(unsigned):
        raise IntegrityError("source transaction hash mismatch")
    if (
        transaction.get("format_version") != FORMAT_VERSION
        or transaction.get("operation") != "prepare_source"
        or transaction.get("source_suffix") != source.suffix.lower()
        or transaction.get("source_sha256") != sha256_file(source)
    ):
        raise IntegrityError("source transaction request binding mismatch")
    _validate_source_seed_claim(
        stage,
        source,
        previous_run_sha256=transaction["previous_run_sha256"],
        previous_source_manifest_sha256=transaction[
            "previous_source_manifest_sha256"
        ],
    )
    files = transaction.get("files")
    if not isinstance(files, dict) or files != _source_stage_files(stage):
        raise IntegrityError("source transaction staged file set mismatch")
    input_paths = sorted(relative for relative in files if relative.startswith("input/"))
    if input_paths != [f"input/source{transaction['source_suffix']}"]:
        raise IntegrityError("source transaction must bind one immutable input")
    expected_stage_files = {
        *files,
        "run.json",
        "events.jsonl",
        _SOURCE_SEED_CLAIM,
        "transaction.json",
    }
    if actual_stage_files != expected_stage_files:
        raise IntegrityError("source staging file set is not exact")
    expected_stage_directories = set(_SOURCE_STAGE_DIRECTORIES)
    for relative in expected_stage_files:
        parent = Path(relative).parent
        while parent != Path("."):
            expected_stage_directories.add(parent.as_posix())
            parent = parent.parent
    if actual_stage_directories != expected_stage_directories:
        raise IntegrityError("source staging directory set is not exact")
    live_input = safe_path(run, input_paths[0], must_exist=True)
    if sha256_file(live_input) != transaction.get("source_sha256"):
        raise IntegrityError("live immutable source input changed")
    if transaction.get("final_run_sha256") != sha256_file(stage / "run.json"):
        raise IntegrityError("source transaction final run hash mismatch")
    if transaction.get("final_source_manifest_sha256") != sha256_file(
        stage / "evidence" / "source_manifest.json"
    ):
        raise IntegrityError("source transaction final manifest hash mismatch")
    previous_events = transaction.get("previous_events")
    new_events = transaction.get("new_events")
    if (
        not isinstance(previous_events, list)
        or not all(isinstance(event, dict) for event in previous_events)
        or not isinstance(new_events, list)
        or not all(isinstance(event, dict) for event in new_events)
        or _read_event_log(stage / "events.jsonl") != previous_events + new_events
    ):
        raise IntegrityError("source transaction event sequence mismatch")
    _stage_run, final_state = _load_agent_first_run(stage)
    if final_state.get("source_manifest_sha256") != transaction.get(
        "final_source_manifest_sha256"
    ):
        raise IntegrityError("source transaction final state pointer mismatch")
    _verify_source_contract(stage, final_state)
    live_run_hash = sha256_file(run / "run.json")
    live_manifest_hash = sha256_file(run / "evidence" / "source_manifest.json")
    if live_run_hash not in {
        transaction.get("previous_run_sha256"),
        transaction.get("final_run_sha256"),
    }:
        raise IntegrityError("live run changed outside the source transaction")
    if live_manifest_hash not in {
        transaction.get("previous_source_manifest_sha256"),
        transaction.get("final_source_manifest_sha256"),
    }:
        raise IntegrityError("live source manifest changed outside the transaction")
    if (
        live_run_hash == transaction.get("final_run_sha256")
        and live_manifest_hash != transaction.get("final_source_manifest_sha256")
    ):
        raise IntegrityError("source transaction state precedes its manifest")
    live_events = _read_event_log(run / "events.jsonl")
    expected_events = previous_events + new_events
    if live_events != expected_events[: len(live_events)]:
        raise IntegrityError("live source events are not a transaction prefix")
    return transaction


def _remove_stale_live_source_outputs(run: Path, expected: set[str]) -> None:
    for root_relative in ("input", "evidence/pages", "evidence/assets"):
        root = safe_path(run, root_relative, must_exist=True)
        for path in list(root.iterdir()):
            relative = path.relative_to(run).as_posix()
            if path.is_symlink():
                raise PathSafetyError(f"unsafe live source output: {path}")
            if path.is_dir():
                shutil.rmtree(path)
            elif relative not in expected:
                path.unlink()
    for relative in _SOURCE_SINGLE_FILES:
        path = safe_path(run, relative)
        if relative not in expected:
            path.unlink(missing_ok=True)


def _commit_source_transaction(
    run: Path,
    stage: Path,
    source: Path,
    *,
    fail_at: str | None,
) -> dict[str, Any]:
    transaction = _validate_source_transaction(run, stage, source)
    files = transaction["files"]
    expected = set(files)
    _remove_stale_live_source_outputs(run, expected)
    for relative in sorted(expected - {"evidence/source_manifest.json"}):
        staged = safe_path(stage, relative, must_exist=True)
        target = safe_path(run, relative)
        atomic_write_bytes(target, staged.read_bytes())
    manifest_source = stage / "evidence" / "source_manifest.json"
    atomic_write_bytes(
        run / "evidence" / "source_manifest.json", manifest_source.read_bytes()
    )
    if fail_at == "after_source_manifest_promotion":
        raise SimulatedCrash("after source manifest promotion")
    atomic_write_bytes(run / "run.json", (stage / "run.json").read_bytes())
    if fail_at == "after_source_run_update":
        raise SimulatedCrash("after source run pointer update")

    previous_events = transaction["previous_events"]
    new_events = transaction["new_events"]
    live_events = _read_event_log(run / "events.jsonl")
    if live_events != (previous_events + new_events)[: len(live_events)]:
        raise IntegrityError("live source events changed during commit")
    for event in (previous_events + new_events)[len(live_events) :]:
        append_jsonl(run / "events.jsonl", event)
        if (
            event.get("event") == "source_blocker_resolved"
            and fail_at == "after_source_blocker_resolved_event"
        ):
            raise SimulatedCrash("after source blocker-resolved event")
        if (
            event.get("event") == "source_prepared"
            and fail_at == "after_source_prepared_event"
        ):
            raise SimulatedCrash("after source-prepared event")
    manifest = _read_json(run / "evidence" / "source_manifest.json")
    _remove_regular_tree(stage)
    return manifest


def _prepare_source_v2_transaction(
    run: Path,
    state: Mapping[str, Any],
    source_path: Path | str,
    *,
    extra_assets: Sequence[Path | str],
    reference_images: Sequence[Path | str],
    tool_paths: Mapping[str, str | Path | None] | None,
    fail_at: str | None,
) -> dict[str, Any]:
    source = Path(source_path).absolute()
    if source.is_symlink() or not source.is_file() or source.stat().st_nlink != 1:
        raise PathSafetyError(f"source must be a regular non-symlink file: {source}")
    stage = run / ".source-prep-staging"
    if stage.exists() or stage.is_symlink():
        staged_files, _staged_directories = _regular_tree_inventory(stage)
        if "transaction.json" in staged_files:
            return _commit_source_transaction(run, stage, source, fail_at=fail_at)
        _discard_incomplete_source_stage(run, state, stage, source)
    manifest_path = run / "evidence" / "source_manifest.json"
    if sha256_file(manifest_path) != state.get("source_manifest_sha256"):
        raise IntegrityError("source manifest hash mismatch")
    if state.get("state") not in {"initialized", "blocked"}:
        raise StateError("source preparation must occur before curation")
    if _read_json(manifest_path).get("status") == "ready":
        raise StateError("a ready source cannot be replaced; initialize a new run")
    suffix = source.suffix.lower()
    if suffix not in {".md", ".markdown", ".txt", ".pdf"}:
        raise ContractError(f"unsupported source type: {suffix or '<none>'}")
    if extra_assets or reference_images:
        raise ContractError("Agent-first source preparation accepts only the primary source")
    input_source = run / "input" / f"source{suffix}"
    input_entries = list((run / "input").iterdir())
    if input_entries:
        if (
            input_entries != [input_source]
            or input_source.is_symlink()
            or not input_source.is_file()
            or input_source.stat().st_nlink != 1
            or sha256_file(input_source) != sha256_file(source)
        ):
            raise StateError("an Agent-first run cannot replace its immutable source")
    else:
        atomic_write_bytes(input_source, source.read_bytes())
    previous_events = _read_event_log(run / "events.jsonl")
    try:
        _seed_source_stage(run, state, stage, source, fail_at=fail_at)
        _prepare_source_v2(
            stage,
            dict(state),
            source,
            extra_assets=extra_assets,
            reference_images=reference_images,
            tool_paths=tool_paths,
            poppler_input=input_source if suffix == ".pdf" else None,
        )
        _create_source_transaction(
            run,
            stage,
            source,
            previous_state=state,
            previous_events=previous_events,
        )
    except SimulatedCrash:
        raise
    except Exception:
        if stage.exists() and not stage.is_symlink():
            _discard_incomplete_source_stage(run, state, stage, source)
        raise
    if fail_at == "after_source_outputs_staged":
        raise SimulatedCrash("after source outputs staged")
    return _commit_source_transaction(run, stage, source, fail_at=fail_at)


def prepare_source(
    run_dir: Path | str,
    source_path: Path | str,
    *,
    extra_assets: Sequence[Path | str] = (),
    reference_images: Sequence[Path | str] = (),
    tool_paths: Mapping[str, str | Path | None] | None = None,
    fail_at: str | None = None,
) -> dict[str, Any]:
    """Hash and index text/Markdown, or route verified PDF ingest via Poppler."""

    allowed_failures = {
        None,
        "after_source_stage_mkdir",
        "after_source_seed_input",
        "after_source_outputs_staged",
        "after_source_manifest_promotion",
        "after_source_run_update",
        "after_source_blocker_resolved_event",
        "after_source_prepared_event",
    }
    if fail_at not in allowed_failures:
        raise ContractError(f"unknown source-preparation crash boundary: {fail_at}")
    if inspect_run_format(run_dir) == AGENT_FIRST_RUN_FORMAT_VERSION:
        run = Path(run_dir).absolute()
        with _agent_first_mutation_lock(run):
            run, state = _load_agent_first_run(run)
            return _prepare_source_v2_transaction(
                run,
                state,
                source_path,
                extra_assets=extra_assets,
                reference_images=reference_images,
                tool_paths=tool_paths,
                fail_at=fail_at,
            )
    if fail_at is not None:
        raise ContractError("source-preparation crash boundaries require run format 2")
    run, state = _load_run(run_dir)
    if state.get("state") not in {"initialized", "blocked"}:
        raise StateError("source preparation must occur before planning")
    existing_manifest = _read_json(run / "evidence" / "source_manifest.json")
    if existing_manifest.get("status") == "ready":
        raise StateError("a ready source cannot be replaced; initialize a new run")
    _clear_stale_source_outputs(run)
    source = Path(source_path).absolute()
    if source.is_symlink() or not source.is_file():
        raise PathSafetyError(f"source must be a regular non-symlink file: {source}")
    suffix = source.suffix.lower()
    source_type = {".md": "markdown", ".markdown": "markdown", ".txt": "text", ".pdf": "pdf"}.get(suffix)
    if source_type is None:
        raise ContractError(f"unsupported source type: {suffix or '<none>'}")
    input_name = f"source{suffix}"
    input_source = run / "input" / input_name
    atomic_write_bytes(input_source, source.read_bytes())
    evidence: list[dict[str, Any]] = []
    visuals: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {
        "format_version": FORMAT_VERSION, "status": "ready", "source_type": source_type,
        "input_path": f"input/{input_name}", "source_sha256": sha256_file(input_source),
        "source_size": input_source.stat().st_size, "tools": {},
    }

    if source_type in {"markdown", "text"}:
        text = input_source.read_text(encoding="utf-8")
        atomic_write_bytes(run / "evidence" / "source.txt", text.encode("utf-8"))
        evidence = _text_evidence(text, markdown=source_type == "markdown")
    else:
        required = ("pdftotext", "pdfinfo", "pdftoppm", "pdfimages")
        resolved: dict[str, str | None] = {}
        for name in required:
            configured = tool_paths.get(name) if tool_paths is not None else shutil.which(name)
            resolved[name] = str(configured) if configured else None
        missing = sorted(name for name, path in resolved.items() if path is None)
        manifest["tools"] = resolved
        if missing:
            manifest.update({"status": "blocked", "missing_tools": missing})
            _write_evidence(run, [])
            atomic_write_json(
                run / "evidence" / "source_visuals.json",
                {"format_version": FORMAT_VERSION, "visuals": []},
            )
            _persist_source_manifest(run, manifest)
            mark_side_state(run, "blocked", reason=f"missing required PDF tools: {', '.join(missing)}")
            return manifest
        assert all(resolved.values())
        routed = {name: str(path) for name, path in resolved.items() if path is not None}
        text_path, commands = _route_poppler(run, input_source, routed)
        image_list = commands["pdfimages_list"]
        failed = sorted(name for name, result in commands.items() if result.returncode != 0)
        rendered_page_paths = sorted((run / "evidence" / "pages").iterdir())
        if any(path.is_symlink() or not path.is_file() for path in rendered_page_paths):
            raise PathSafetyError("PDF renderer produced a non-regular page output")
        rendered_pages = {
            path.relative_to(run).as_posix(): sha256_file(path)
            for path in rendered_page_paths
        }
        if not rendered_pages:
            failed.append("pdftoppm_output")
        manifest["commands"] = {name: {"returncode": result.returncode, "stderr": redact_secrets(result.stderr)} for name, result in commands.items()}
        if failed or not text_path.is_file():
            manifest.update({"status": "blocked", "failed_commands": failed or ["pdftotext_output"]})
            _write_evidence(run, [])
            atomic_write_json(run / "evidence" / "source_visuals.json", {"format_version": FORMAT_VERSION, "visuals": []})
            _persist_source_manifest(run, manifest)
            mark_side_state(run, "blocked", reason=f"PDF preparation failed: {', '.join(manifest['failed_commands'])}")
            return manifest
        manifest["rendered_pages"] = rendered_pages
        text = text_path.read_text(encoding="utf-8", errors="replace")
        evidence = _pdf_evidence(text)
        image_mappings = _parse_pdfimages_list(image_list.stdout)
        extracted_images = [
            path
            for path in sorted((run / "evidence" / "assets").glob("pdf-image*"))
            if path.is_file() and not path.is_symlink()
        ]
        if len(extracted_images) != len(image_mappings):
            manifest.update(
                {"status": "blocked", "failed_commands": ["pdfimages_mapping"]}
            )
            _write_evidence(run, evidence)
            atomic_write_json(
                run / "evidence" / "source_visuals.json",
                {"format_version": FORMAT_VERSION, "visuals": []},
            )
            _persist_source_manifest(run, manifest)
            mark_side_state(
                run,
                "blocked",
                reason="PDF image extraction could not be mapped to page/object metadata",
            )
            return manifest
        for index, image in enumerate(extracted_images, start=1):
            if image.is_file() and not image.is_symlink():
                page, pdf_image_num = (
                    image_mappings[index - 1]
                    if index <= len(image_mappings)
                    else (None, None)
                )
                visuals.append(
                    _visual_record(
                        f"vis-{index:03d}",
                        f"assets/{image.name}",
                        sha256_file(image),
                        origin="pdf_extracted",
                        page=page,
                        pdf_image_num=pdf_image_num,
                    )
                )

    for asset in extra_assets:
        source_asset = Path(asset).absolute()
        if source_asset.is_symlink() or not source_asset.is_file():
            raise PathSafetyError(f"asset must be a regular non-symlink file: {source_asset}")
        index = len(visuals) + 1
        name = f"asset-{index:03d}{source_asset.suffix.lower()}"
        target = run / "evidence" / "assets" / name
        atomic_write_bytes(target, source_asset.read_bytes())
        visuals.append(_visual_record(f"vis-{index:03d}", f"assets/{name}", sha256_file(target), origin="explicit_asset"))
    for reference in reference_images:
        source_reference = Path(reference).absolute()
        if source_reference.is_symlink() or not source_reference.is_file():
            raise PathSafetyError(
                f"reference image must be a regular non-symlink file: {source_reference}"
            )
        index = len(visuals) + 1
        name = f"reference-{index:03d}{source_reference.suffix.lower()}"
        target = run / "evidence" / "reference_images" / name
        atomic_write_bytes(target, source_reference.read_bytes())
        visuals.append(
            _visual_record(
                f"vis-{index:03d}",
                f"reference_images/{name}",
                sha256_file(target),
                origin="style_reference",
            )
        )
    _write_evidence(run, evidence)
    atomic_write_json(
        run / "evidence" / "source_visuals.json",
        {"format_version": FORMAT_VERSION, "visuals": visuals},
    )
    manifest.update(
        {
            "source_text_sha256": sha256_file(run / "evidence" / "source.txt"),
            "evidence_sha256": sha256_file(run / "evidence" / "evidence.jsonl"),
            "source_visuals_sha256": sha256_file(run / "evidence" / "source_visuals.json"),
            "evidence_count": len(evidence), "visual_count": len(visuals),
        }
    )
    _persist_source_manifest(run, manifest)
    if state.get("state") == "blocked":
        current = _read_json(run / "run.json")
        current["state"] = "initialized"
        current.pop("reason", None)
        current.pop("resume_from", None)
        _write_run(run, current)
        _event(run, "source_blocker_resolved", state="initialized")
    _event(run, "source_prepared", source_type=source_type, evidence_count=len(evidence), visual_count=len(visuals))
    return manifest


def _persist_source_manifest(run: Path, manifest: Mapping[str, Any]) -> None:
    path = run / "evidence" / "source_manifest.json"
    atomic_write_json(path, dict(manifest))
    state = _read_json(run / "run.json")
    state["source_manifest_sha256"] = sha256_file(path)
    _write_run(run, state)


def _verify_source_contract(run: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    manifest_path = run / "evidence" / "source_manifest.json"
    if sha256_file(manifest_path) != state.get("source_manifest_sha256"):
        raise IntegrityError("source manifest hash mismatch")
    manifest = _read_json(manifest_path)
    if manifest.get("status") == "not_prepared":
        return manifest
    input_path = manifest.get("input_path")
    if not isinstance(input_path, str):
        raise IntegrityError("source manifest has no input path")
    source = safe_path(run, input_path, must_exist=True)
    if sha256_file(source) != manifest.get("source_sha256"):
        raise IntegrityError("source input hash mismatch")
    if manifest.get("status") != "ready":
        return manifest
    expected = {
        "source_text_sha256": run / "evidence" / "source.txt",
        "evidence_sha256": run / "evidence" / "evidence.jsonl",
        "source_visuals_sha256": run / "evidence" / "source_visuals.json",
    }
    for field, path in expected.items():
        if sha256_file(path) != manifest.get(field):
            raise IntegrityError(f"source contract hash mismatch: {field}")
    if manifest.get("source_type") == "pdf":
        rendered_pages = manifest.get("rendered_pages")
        if not isinstance(rendered_pages, dict) or not rendered_pages:
            raise IntegrityError("PDF source manifest has no rendered page bindings")
        pages_root = run / "evidence" / "pages"
        page_entries = list(pages_root.iterdir())
        if any(path.is_symlink() or not path.is_file() for path in page_entries):
            raise IntegrityError("PDF rendered pages contain an unexpected entry")
        actual_pages = {
            path.relative_to(run).as_posix()
            for path in page_entries
        }
        if actual_pages != set(rendered_pages):
            raise IntegrityError("PDF rendered page set differs from source manifest")
        for relative, digest in rendered_pages.items():
            page = safe_path(run, relative, must_exist=True)
            if page.parent != pages_root or sha256_file(page) != digest:
                raise IntegrityError(f"PDF rendered page hash mismatch: {relative}")
    load_evidence(run)
    visuals = _load_visuals(run)
    for visual in visuals["visuals"]:
        relative = visual.get("path")
        if not isinstance(relative, str):
            raise IntegrityError("visual contract has no path")
        path = safe_path(run / "evidence", relative, must_exist=True)
        if sha256_file(path) != visual.get("sha256"):
            raise IntegrityError(f"source visual hash mismatch: {visual.get('id')}")
    _verify_vlm_history(run, manifest, visuals)
    return manifest


def _load_v2_pdf_registries(
    run: Path, manifest: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    page_manifest_value = manifest.get("page_manifest_path")
    hint_manifest_value = manifest.get("pdfimages_hints_path")
    if not isinstance(page_manifest_value, str) or not isinstance(hint_manifest_value, str):
        raise IntegrityError("Agent-first PDF source is missing canonical registries")
    page_manifest_path = safe_path(run, page_manifest_value, must_exist=True)
    hint_manifest_path = safe_path(run, hint_manifest_value, must_exist=True)
    if (
        page_manifest_path.parent != run / "evidence"
        or page_manifest_path.name != "page-manifest.json"
        or hint_manifest_path.parent != run / "evidence"
        or hint_manifest_path.name != "pdfimages-hints.json"
    ):
        raise IntegrityError("Agent-first PDF registry path is noncanonical")
    if sha256_file(page_manifest_path) != manifest.get("page_manifest_sha256"):
        raise IntegrityError("page manifest hash mismatch")
    if sha256_file(hint_manifest_path) != manifest.get("pdfimages_hints_sha256"):
        raise IntegrityError("pdfimages hints hash mismatch")
    page_manifest = _read_json(page_manifest_path)
    hint_manifest = _read_json(hint_manifest_path)
    source_binding = {
        "source_path": manifest.get("input_path"),
        "source_sha256": manifest.get("source_sha256"),
    }
    for value, name in (
        (page_manifest, "page manifest"),
        (hint_manifest, "pdfimages hints"),
    ):
        if value.get("format_version") != FORMAT_VERSION or any(
            value.get(key) != expected for key, expected in source_binding.items()
        ):
            raise IntegrityError(f"{name} source binding mismatch")
    pages = page_manifest.get("pages")
    if not isinstance(pages, list) or not pages:
        raise IntegrityError("page manifest has no pages")
    expected_page_keys = {
        "page",
        "path",
        "sha256",
        "width",
        "height",
        "renderer",
        "dpi",
        "pdf_page_box",
        "effective_rotation",
    }
    for number, page in enumerate(pages, start=1):
        if not isinstance(page, dict) or set(page) != expected_page_keys:
            raise IntegrityError("page manifest entry has an unknown or incomplete schema")
        expected_path = f"evidence/pages/page-{number:04d}.png"
        if (
            page.get("page") != number
            or page.get("path") != expected_path
            or page.get("renderer") != "pdftoppm"
            or page.get("dpi") != 144
            or page.get("pdf_page_box") != "poppler_default"
            or page.get("effective_rotation") != 0
        ):
            raise IntegrityError("page manifest metadata is noncanonical")
        path = safe_path(run, expected_path, must_exist=True)
        if path.stat().st_nlink != 1 or sha256_file(path) != page.get("sha256"):
            raise IntegrityError(f"rendered page hash mismatch: {expected_path}")
        width, height = _png_dimensions(path)
        if page.get("width") != width or page.get("height") != height:
            raise IntegrityError(f"rendered page dimensions mismatch: {expected_path}")
    pages_root = run / "evidence" / "pages"
    if {path.name for path in pages_root.iterdir()} != {
        f"page-{number:04d}.png" for number in range(1, len(pages) + 1)
    }:
        raise IntegrityError("rendered page registry is not an exact set")

    hints = hint_manifest.get("hints")
    if not isinstance(hints, list):
        raise IntegrityError("pdfimages hints must be a list")
    expected_hint_keys = {
        "path",
        "sha256",
        "page",
        "object_number",
        "trust",
        "eligible",
    }
    expected_hint_paths: set[str] = set()
    for hint in hints:
        if not isinstance(hint, dict) or set(hint) != expected_hint_keys:
            raise IntegrityError("pdfimages hint has an unknown or incomplete schema")
        relative = hint.get("path")
        if (
            not isinstance(relative, str)
            or hint.get("trust") != "untrusted_hint"
            or hint.get("eligible") is not False
            or not isinstance(hint.get("page"), int)
            or isinstance(hint.get("page"), bool)
            or not 1 <= hint["page"] <= len(pages)
            or not isinstance(hint.get("object_number"), int)
            or isinstance(hint.get("object_number"), bool)
            or hint["object_number"] < 0
        ):
            raise IntegrityError("pdfimages hint metadata is invalid")
        path = safe_path(run, relative, must_exist=True)
        if path.parent != run / "evidence" / "assets":
            raise IntegrityError("pdfimages hint is outside its registry")
        if path.stat().st_nlink != 1 or sha256_file(path) != hint.get("sha256"):
            raise IntegrityError("pdfimages hint hash mismatch")
        if relative in expected_hint_paths:
            raise IntegrityError("duplicate pdfimages hint path")
        expected_hint_paths.add(relative)
    actual_hint_paths = {
        path.relative_to(run).as_posix()
        for path in (run / "evidence" / "assets").glob("pdf-image*")
    }
    if actual_hint_paths != expected_hint_paths:
        raise IntegrityError("pdfimages hint registry is not an exact set")
    return [dict(page) for page in pages], [dict(hint) for hint in hints]


def inspect_source(run_dir: Path | str) -> dict[str, Any]:
    """Return verified Agent-first source, page, and extraction-hint bindings."""

    run, state = _load_agent_first_run(run_dir)
    manifest = _verify_source_contract(run, state)
    source: dict[str, Any] | None = None
    if isinstance(manifest.get("input_path"), str):
        source = {
            "path": manifest["input_path"],
            "sha256": manifest.get("source_sha256"),
            "source_type": manifest.get("source_type"),
        }
    pages: list[dict[str, Any]] = []
    hints: list[dict[str, Any]] = []
    if manifest.get("status") == "ready" and manifest.get("source_type") == "pdf":
        pages, raw_hints = _load_v2_pdf_registries(run, manifest)
        hints = [
            {
                **hint,
                "trust": "untrusted",
            }
            for hint in raw_hints
        ]
    return {
        "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
        "source": source,
        "source_manifest_sha256": state.get("source_manifest_sha256"),
        "page_manifest_path": manifest.get("page_manifest_path"),
        "page_manifest_sha256": manifest.get("page_manifest_sha256"),
        "pages": pages,
        "extraction_hints": hints,
        "active_curation_revision": state.get("active_curation_revision"),
        "active_plan_revision": state.get("active_plan_revision"),
        "next_action": (
            "prepare_source"
            if manifest.get("status") == "not_prepared"
            else "curate_source"
        ),
    }


def _validate_crop_request(
    run: Path, state: Mapping[str, Any], request: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest = _verify_source_contract(run, state)
    if manifest.get("status") != "ready" or manifest.get("source_type") != "pdf":
        raise StateError("source cropping requires a ready PDF")
    required = {
        "run_format_version",
        "source_sha256",
        "page_manifest_sha256",
        "page",
        "page_sha256",
        "bbox_normalized",
        "role",
        "claim",
        "max_reuse",
    }
    if not isinstance(request, Mapping) or set(request) != required:
        raise ContractError("crop request has an unknown or incomplete schema")
    value = dict(request)
    if value.get("run_format_version") != AGENT_FIRST_RUN_FORMAT_VERSION:
        raise ContractError("crop request targets the wrong run format")
    page_number = value.get("page")
    if (
        not isinstance(page_number, int)
        or isinstance(page_number, bool)
        or page_number < 1
    ):
        raise ContractError("crop page must be a positive integer")
    bbox = value.get("bbox_normalized")
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise ContractError("crop bbox_normalized must contain four coordinates")
    coordinates: list[float] = []
    for coordinate in bbox:
        if (
            not isinstance(coordinate, (int, float))
            or isinstance(coordinate, bool)
            or not math.isfinite(coordinate)
            or not 0 <= coordinate <= 1
        ):
            raise ContractError("crop coordinates must be finite numbers in [0, 1]")
        coordinates.append(float(coordinate))
    if not coordinates[0] < coordinates[2] or not coordinates[1] < coordinates[3]:
        raise ContractError("crop bbox must have positive width and height")
    value["bbox_normalized"] = coordinates
    for field in ("role", "claim"):
        field_value = value.get(field)
        if not isinstance(field_value, str) or not field_value.strip():
            raise ContractError(f"crop {field} must be non-empty")
        value[field] = field_value.strip()
    max_reuse = value.get("max_reuse")
    if (
        not isinstance(max_reuse, int)
        or isinstance(max_reuse, bool)
        or max_reuse < 1
    ):
        raise ContractError("crop max_reuse must be a positive integer")

    pages, _hints = _load_v2_pdf_registries(run, manifest)
    if page_number > len(pages):
        raise ContractError("crop page is outside the rendered page set")
    page = pages[page_number - 1]
    for field, expected in (
        ("source_sha256", manifest.get("source_sha256")),
        ("page_manifest_sha256", manifest.get("page_manifest_sha256")),
        ("page_sha256", page.get("sha256")),
    ):
        supplied = value.get(field)
        if (
            not isinstance(supplied, str)
            or re.fullmatch(r"[0-9a-f]{64}", supplied) is None
        ):
            raise ContractError(f"crop {field} must be a lowercase SHA-256 digest")
        if supplied != expected:
            raise IntegrityError(f"crop {field} binding is stale")
    return value, dict(manifest), page


def _crop_result(receipt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
        "asset_id": receipt["asset_id"],
        "asset_path": receipt["asset_path"],
        "asset_sha256": receipt["asset_sha256"],
        "receipt_path": receipt["receipt_path"],
        "receipt_sha256": receipt["receipt_sha256"],
        "bbox_pixels": receipt["bbox_pixels"],
    }


def _validate_crop_receipt_document(
    run: Path,
    state: Mapping[str, Any],
    receipt: dict[str, Any],
    *,
    asset_path: Path,
) -> dict[str, Any]:
    required = {
        "run_format_version",
        "asset_id",
        "operation_id",
        "asset_path",
        "asset_sha256",
        "receipt_path",
        "receipt_sha256",
        "source_manifest_sha256",
        "source_path",
        "source_sha256",
        "page_manifest_path",
        "page_manifest_sha256",
        "page",
        "page_path",
        "page_sha256",
        "page_width",
        "page_height",
        "renderer",
        "dpi",
        "pdf_page_box",
        "effective_rotation",
        "bbox_normalized",
        "bbox_pixels",
        "semantic_request",
    }
    if set(receipt) != required or receipt.get("run_format_version") != AGENT_FIRST_RUN_FORMAT_VERSION:
        raise IntegrityError("crop receipt has an unknown or incomplete schema")
    receipt_without_hash = dict(receipt)
    receipt_hash = receipt_without_hash.pop("receipt_sha256", None)
    if receipt_hash != _canonical_hash(receipt_without_hash):
        raise IntegrityError("crop receipt hash mismatch")
    asset_id = receipt.get("asset_id")
    operation_id = receipt.get("operation_id")
    if (
        not isinstance(asset_id, str)
        or re.fullmatch(r"src-[0-9a-f]{24}", asset_id) is None
        or not isinstance(operation_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", operation_id) is None
        or asset_id != f"src-{operation_id[:24]}"
    ):
        raise IntegrityError("crop receipt operation identity is invalid")
    expected_asset = f"source-assets/files/{asset_id}.png"
    expected_receipt = f"source-assets/receipts/{asset_id}.json"
    if (
        receipt.get("asset_path") != expected_asset
        or receipt.get("receipt_path") != expected_receipt
    ):
        raise IntegrityError("crop receipt registry path is noncanonical")
    if (
        asset_path.is_symlink()
        or not asset_path.is_file()
        or asset_path.stat().st_nlink != 1
        or sha256_file(asset_path) != receipt.get("asset_sha256")
    ):
        raise IntegrityError("crop output hash mismatch")
    manifest = _verify_source_contract(run, state)
    if (
        receipt.get("source_manifest_sha256") != state.get("source_manifest_sha256")
        or receipt.get("source_path") != manifest.get("input_path")
        or receipt.get("source_sha256") != manifest.get("source_sha256")
        or receipt.get("page_manifest_path") != manifest.get("page_manifest_path")
        or receipt.get("page_manifest_sha256") != manifest.get("page_manifest_sha256")
    ):
        raise IntegrityError("crop receipt source binding is stale")
    pages, _hints = _load_v2_pdf_registries(run, manifest)
    page_number = receipt.get("page")
    if (
        not isinstance(page_number, int)
        or isinstance(page_number, bool)
        or not 1 <= page_number <= len(pages)
    ):
        raise IntegrityError("crop receipt page is invalid")
    page = pages[page_number - 1]
    expected_page_fields = {
        "page_path": "path",
        "page_sha256": "sha256",
        "page_width": "width",
        "page_height": "height",
        "renderer": "renderer",
        "dpi": "dpi",
        "pdf_page_box": "pdf_page_box",
        "effective_rotation": "effective_rotation",
    }
    if any(
        receipt.get(receipt_field) != page.get(page_field)
        for receipt_field, page_field in expected_page_fields.items()
    ):
        raise IntegrityError("crop receipt page binding is stale")
    bbox = receipt.get("bbox_normalized")
    pixels = receipt.get("bbox_pixels")
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or not 0 <= value <= 1
            for value in bbox
        )
        or not bbox[0] < bbox[2]
        or not bbox[1] < bbox[3]
        or not isinstance(pixels, list)
        or len(pixels) != 4
        or any(not isinstance(value, int) or isinstance(value, bool) for value in pixels)
    ):
        raise IntegrityError("crop receipt bbox binding is invalid")
    expected_pixels = [
        math.floor(bbox[0] * page["width"]),
        math.floor(bbox[1] * page["height"]),
        math.ceil(bbox[2] * page["width"]),
        math.ceil(bbox[3] * page["height"]),
    ]
    if pixels != expected_pixels:
        raise IntegrityError("crop receipt pixel bbox binding is stale")
    semantic = receipt.get("semantic_request")
    if not isinstance(semantic, dict) or set(semantic) != {"role", "claim", "max_reuse"}:
        raise IntegrityError("crop receipt semantic request is invalid")
    if (
        any(
            not isinstance(semantic.get(field), str)
            or not semantic[field].strip()
            or semantic[field] != semantic[field].strip()
            for field in ("role", "claim")
        )
        or not isinstance(semantic.get("max_reuse"), int)
        or isinstance(semantic.get("max_reuse"), bool)
        or semantic["max_reuse"] < 1
    ):
        raise IntegrityError("crop receipt semantic request values are invalid")
    request = {
        "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
        "source_sha256": receipt["source_sha256"],
        "page_manifest_sha256": receipt["page_manifest_sha256"],
        "page": page_number,
        "page_sha256": receipt["page_sha256"],
        "bbox_normalized": [float(value) for value in bbox],
        "role": semantic["role"],
        "claim": semantic["claim"],
        "max_reuse": semantic["max_reuse"],
    }
    expected_operation = _canonical_hash(
        {
            "operation": "crop_source",
            "source_manifest_sha256": state["source_manifest_sha256"],
            "source_sha256": manifest["source_sha256"],
            "page_manifest_sha256": manifest["page_manifest_sha256"],
            "page_sha256": page["sha256"],
            "request": request,
        }
    )
    if operation_id != expected_operation:
        raise IntegrityError("crop receipt operation binding is stale")
    return receipt


def _read_crop_receipt(
    run: Path,
    state: Mapping[str, Any],
    receipt_path: Path,
    *,
    asset_path: Path,
) -> dict[str, Any]:
    if (
        receipt_path.is_symlink()
        or not receipt_path.is_file()
        or receipt_path.stat().st_nlink != 1
    ):
        raise PathSafetyError(f"unsafe crop receipt: {receipt_path}")
    receipt = _read_json(receipt_path)
    if receipt_path.read_bytes() != _stored_json_bytes(receipt):
        raise IntegrityError("crop receipt contains noncanonical JSON bytes")
    return _validate_crop_receipt_document(run, state, receipt, asset_path=asset_path)


def _crop_event_exists(run: Path, operation_id: str) -> bool:
    events_path = run / "events.jsonl"
    for line in events_path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise IntegrityError("event log contains invalid JSON") from error
        if not isinstance(event, dict):
            raise IntegrityError("event log entry must be an object")
        if (
            event.get("event") == "source_crop_registered"
            and event.get("operation_id") == operation_id
        ):
            return True
    return False


def _record_crop_event(run: Path, receipt: Mapping[str, Any]) -> None:
    if not _crop_event_exists(run, str(receipt["operation_id"])):
        _event(
            run,
            "source_crop_registered",
            asset_id=receipt["asset_id"],
            operation_id=receipt["operation_id"],
        )


def _stage_entries(stage: Path) -> set[str]:
    if stage.is_symlink() or not stage.is_dir():
        raise PathSafetyError(f"unsafe crop staging path: {stage}")
    entries = list(stage.iterdir())
    if any(
        path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1
        for path in entries
    ):
        raise PathSafetyError(f"unsafe crop staging entry: {stage}")
    return {path.name for path in entries}


def _load_crop_png_module() -> Any:
    """Resolve the Poster-only helper only when a crop actually executes."""

    try:
        if __package__:
            from . import portable_png

            return portable_png
    except (ImportError, ValueError):
        pass
    path = Path(__file__).resolve().with_name("portable_png.py")
    try:
        spec = importlib.util.spec_from_file_location("_autodesign_portable_png", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load bundled PNG helper: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (ImportError, OSError, ValueError) as error:
        raise IntegrityError("Agent-first crop requires the bundled portable PNG helper") from error


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _promote_crop_pair(
    run: Path,
    state: Mapping[str, Any],
    stage: Path,
    asset_path: Path,
    receipt_path: Path,
    *,
    fail_at: str | None,
) -> dict[str, Any]:
    asset_id = asset_path.stem
    staged_asset = stage / f"{asset_id}.png"
    staged_receipt = stage / f"{asset_id}.json"
    expected = {staged_asset.name, staged_receipt.name}
    if _stage_entries(stage) != expected:
        raise IntegrityError("crop staging directory is not an exact pair")
    receipt = _read_crop_receipt(
        run, state, staged_receipt, asset_path=staged_asset
    )
    if asset_path.exists() or receipt_path.exists():
        raise IntegrityError("crop target appeared during promotion")
    os.replace(staged_asset, asset_path)
    _fsync_directory(asset_path.parent)
    if fail_at == "after_asset_promotion":
        raise SimulatedCrash("after crop asset promotion")
    os.replace(staged_receipt, receipt_path)
    _fsync_directory(receipt_path.parent)
    _record_crop_event(run, receipt)
    shutil.rmtree(stage)
    return receipt


def crop_source(
    run_dir: Path | str,
    request: Mapping[str, Any],
    *,
    fail_at: str | None = None,
) -> dict[str, Any]:
    """Register one deterministic crop from a verified complete PDF page."""

    aliases = {
        "after_asset_write": "after_staged_asset_write",
        "after_receipt_write": "after_staged_receipt_write",
        "between_promotion_steps": "after_asset_promotion",
    }
    fail_at = aliases.get(fail_at, fail_at)
    if fail_at not in {
        None,
        "after_staged_asset_write",
        "after_staged_receipt_write",
        "after_asset_promotion",
    }:
        raise ContractError(f"unknown crop crash boundary: {fail_at}")
    if inspect_run_format(run_dir) != AGENT_FIRST_RUN_FORMAT_VERSION:
        raise StateError("version-1 runs do not support Agent-first source APIs")
    run = Path(run_dir).absolute()
    with _agent_first_mutation_lock(run):
        run, state = _load_agent_first_run(run)
        if state.get("state") != "curating":
            raise StateError("source cropping requires curating state")
        value, manifest, page = _validate_crop_request(run, state, request)
        operation_id = _canonical_hash(
            {
                "operation": "crop_source",
                "source_manifest_sha256": state["source_manifest_sha256"],
                "source_sha256": manifest["source_sha256"],
                "page_manifest_sha256": manifest["page_manifest_sha256"],
                "page_sha256": page["sha256"],
                "request": value,
            }
        )
        asset_id = f"src-{operation_id[:24]}"
        asset_path = run / "source-assets" / "files" / f"{asset_id}.png"
        receipt_path = run / "source-assets" / "receipts" / f"{asset_id}.json"
        stage = run / "source-assets" / f".crop-staging-{asset_id}"

        if asset_path.exists() and receipt_path.exists():
            receipt = _read_crop_receipt(
                run, state, receipt_path, asset_path=asset_path
            )
            if receipt.get("operation_id") != operation_id:
                raise IntegrityError("crop operation ID collision")
            if stage.exists() or stage.is_symlink():
                _stage_entries(stage)
                shutil.rmtree(stage)
            _record_crop_event(run, receipt)
            return _crop_result(receipt)

        if asset_path.exists() and not receipt_path.exists():
            if stage.exists() and _stage_entries(stage) == {f"{asset_id}.json"}:
                receipt = _read_crop_receipt(
                    run, state, stage / f"{asset_id}.json", asset_path=asset_path
                )
                if receipt.get("operation_id") != operation_id:
                    raise IntegrityError("crop recovery operation mismatch")
                os.replace(stage / f"{asset_id}.json", receipt_path)
                _fsync_directory(receipt_path.parent)
                shutil.rmtree(stage)
                _record_crop_event(run, receipt)
                return _crop_result(receipt)
            if asset_path.is_symlink() or not asset_path.is_file():
                raise PathSafetyError(f"unsafe partial crop output: {asset_path}")
            asset_path.unlink()
        elif receipt_path.exists() and not asset_path.exists():
            if receipt_path.is_symlink() or not receipt_path.is_file():
                raise PathSafetyError(f"unsafe partial crop receipt: {receipt_path}")
            receipt_path.unlink()

        if stage.exists() or stage.is_symlink():
            entries = _stage_entries(stage)
            if entries == {f"{asset_id}.png", f"{asset_id}.json"}:
                receipt = _promote_crop_pair(
                    run,
                    state,
                    stage,
                    asset_path,
                    receipt_path,
                    fail_at=fail_at,
                )
                return _crop_result(receipt)
            shutil.rmtree(stage)

        png = _load_crop_png_module()
        stage.mkdir()
        staged_asset = stage / f"{asset_id}.png"
        staged_receipt = stage / f"{asset_id}.json"
        width = int(page["width"])
        height = int(page["height"])
        bbox = value["bbox_normalized"]
        pixels = [
            math.floor(bbox[0] * width),
            math.floor(bbox[1] * height),
            math.ceil(bbox[2] * width),
            math.ceil(bbox[3] * height),
        ]
        if not pixels[0] < pixels[2] or not pixels[1] < pixels[3]:
            shutil.rmtree(stage)
            raise ContractError("normalized crop collapses at page resolution")
        page_path = safe_path(run, page["path"], must_exist=True)
        try:
            cropped = png.crop_png(page_path.read_bytes(), tuple(pixels))
        except ValueError as error:
            shutil.rmtree(stage)
            raise IntegrityError("registered source page is not a supported PNG") from error
        atomic_write_bytes(staged_asset, cropped)
        if fail_at == "after_staged_asset_write":
            raise SimulatedCrash("after staged crop asset write")
        receipt: dict[str, Any] = {
            "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
            "asset_id": asset_id,
            "operation_id": operation_id,
            "asset_path": asset_path.relative_to(run).as_posix(),
            "asset_sha256": sha256_file(staged_asset),
            "receipt_path": receipt_path.relative_to(run).as_posix(),
            "source_manifest_sha256": state["source_manifest_sha256"],
            "source_path": manifest["input_path"],
            "source_sha256": manifest["source_sha256"],
            "page_manifest_path": manifest["page_manifest_path"],
            "page_manifest_sha256": manifest["page_manifest_sha256"],
            "page": value["page"],
            "page_path": page["path"],
            "page_sha256": page["sha256"],
            "page_width": width,
            "page_height": height,
            "renderer": page["renderer"],
            "dpi": page["dpi"],
            "pdf_page_box": page["pdf_page_box"],
            "effective_rotation": page["effective_rotation"],
            "bbox_normalized": bbox,
            "bbox_pixels": pixels,
            "semantic_request": {
                "role": value["role"],
                "claim": value["claim"],
                "max_reuse": value["max_reuse"],
            },
        }
        receipt["receipt_sha256"] = _canonical_hash(receipt)
        atomic_write_json(staged_receipt, receipt)
        if fail_at == "after_staged_receipt_write":
            raise SimulatedCrash("after staged crop receipt write")
        try:
            promoted = _promote_crop_pair(
                run,
                state,
                stage,
                asset_path,
                receipt_path,
                fail_at=fail_at,
            )
        except SimulatedCrash:
            raise
        except Exception:
            if asset_path.exists() and not receipt_path.exists():
                asset_path.unlink()
            if receipt_path.exists() and not asset_path.exists():
                receipt_path.unlink()
            if stage.exists() and not stage.is_symlink():
                shutil.rmtree(stage)
            raise
        return _crop_result(promoted)


def crop_source_from_file(
    run_dir: Path | str,
    request_path: Path | str,
    *,
    fail_at: str | None = None,
) -> dict[str, Any]:
    """Validate canonical request-file bytes before Mapping-level dispatch."""

    if inspect_run_format(run_dir) != AGENT_FIRST_RUN_FORMAT_VERSION:
        raise StateError("version-1 runs do not support Agent-first source APIs")
    path = Path(request_path).absolute()
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise PathSafetyError(f"crop request must be a regular file: {path}")
    request = _read_json(path)
    if path.read_bytes() != _stored_json_bytes(request):
        raise ContractError("crop request file must contain canonical JSON bytes")
    return crop_source(run_dir, request, fail_at=fail_at)


def _load_derived_source_assets(
    run: Path, state: Mapping[str, Any]
) -> list[dict[str, Any]]:
    assets_root = run / "source-assets" / "files"
    receipts_root = run / "source-assets" / "receipts"
    receipts: list[dict[str, Any]] = []
    for receipt_path in sorted(receipts_root.iterdir()):
        if re.fullmatch(r"src-[0-9a-f]{24}\.json", receipt_path.name) is None:
            raise IntegrityError(f"unexpected crop receipt registry entry: {receipt_path.name}")
        asset_path = assets_root / f"{receipt_path.stem}.png"
        receipts.append(
            _read_crop_receipt(run, state, receipt_path, asset_path=asset_path)
        )
    expected_assets = {f"{receipt['asset_id']}.png" for receipt in receipts}
    actual_assets = {path.name for path in assets_root.iterdir()}
    if actual_assets != expected_assets:
        raise IntegrityError("crop asset and receipt registries differ")
    if any(
        path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1
        for path in assets_root.iterdir()
    ):
        raise PathSafetyError("crop asset registry contains an unsafe entry")
    return receipts


def list_source_assets(run_dir: Path | str) -> dict[str, Any]:
    """Separate untrusted extraction hints from unreviewed derived crops."""

    run, state = _load_agent_first_run(run_dir)
    manifest = _verify_source_contract(run, state)
    hints: list[dict[str, Any]] = []
    if manifest.get("status") == "ready" and manifest.get("source_type") == "pdf":
        _pages, raw_hints = _load_v2_pdf_registries(run, manifest)
        hints = [dict(hint) for hint in raw_hints]
    derived = [
        {
            "asset_id": receipt["asset_id"],
            "path": receipt["asset_path"],
            "sha256": receipt["asset_sha256"],
            "receipt_path": receipt["receipt_path"],
            "receipt_sha256": receipt["receipt_sha256"],
            "page": receipt["page"],
            "bbox_normalized": receipt["bbox_normalized"],
            "semantic_request": receipt["semantic_request"],
            "trust": "agent_derived_unreviewed",
            "eligible": False,
        }
        for receipt in _load_derived_source_assets(run, state)
    ]
    return {
        "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
        "extraction_hints": hints,
        "derived_assets": derived,
        "active_curation_revision": state.get("active_curation_revision"),
    }


def load_evidence(run_dir: Path | str) -> list[dict[str, Any]]:
    """Load and validate the append-free evidence JSONL contract."""

    run, _state = _load_run(run_dir)
    path = run / "evidence" / "evidence.jsonl"
    evidence: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise IntegrityError(f"cannot read evidence: {path}") from error
    seen: set[str] = set()
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError as error:
            raise IntegrityError("invalid evidence JSONL") from error
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise IntegrityError("invalid evidence item")
        if item["id"] in seen:
            raise IntegrityError(f"duplicate evidence ID: {item['id']}")
        seen.add(item["id"])
        if sha256_bytes(str(item.get("text", "")).encode("utf-8")) != item.get("sha256"):
            raise IntegrityError(f"evidence text hash mismatch: {item['id']}")
        evidence.append(item)
    return evidence


def _tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    words: list[str] = []
    for word in _WORD.findall(normalized):
        if word in _STOPWORDS:
            continue
        for ending in ("ements", "ement", "ingly", "edly", "ing", "ed", "es", "s"):
            if word.endswith(ending) and len(word) - len(ending) >= 4:
                word = word[: -len(ending)]
                break
        words.append(word)
    unicode_run: list[str] = []

    def flush_unicode_run() -> None:
        if not unicode_run:
            return
        token = "".join(unicode_run)
        if len(token) == 1:
            words.append(token)
        else:
            words.extend(token[index : index + 2] for index in range(len(token) - 1))
        unicode_run.clear()

    for character in normalized:
        if not character.isascii() and (
            character.isalnum() or unicodedata.category(character).startswith("M")
        ):
            unicode_run.append(character)
        else:
            flush_unicode_run()
    flush_unicode_run()
    return words


def lexical_retrieve(
    evidence: Sequence[Mapping[str, Any]], query: str, *, limit: int = 5
) -> list[dict[str, Any]]:
    """Rank evidence with deterministic token-frequency overlap."""

    if limit < 1:
        return []
    query_counts = Counter(_tokens(query))
    ranked: list[tuple[float, str, dict[str, Any]]] = []
    for item in evidence:
        item_counts = Counter(_tokens(str(item.get("text", ""))))
        overlap = sum(min(count, item_counts[token]) for token, count in query_counts.items())
        if overlap:
            score = overlap / math.sqrt(max(1, sum(item_counts.values())))
            ranked.append((-score, str(item.get("id", "")), dict(item)))
    ranked.sort(key=lambda value: (value[0], value[1]))
    return [item for _, _, item in ranked[:limit]]


def _normalized_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _numbers(text: str) -> set[str]:
    values: set[str] = set()
    for token in _NUMBER.findall(_normalize_numeric_text(text)):
        try:
            values.add(_canonical_number(token))
        except InvalidOperation:
            continue
    return values


def _canonical_number(token: str) -> str:
    normalized = _normalize_numeric_text(token).strip()
    percent = normalized.endswith("%")
    numeric = normalized.removesuffix("%").replace(",", "")
    value = Decimal(numeric)
    if not value.is_finite():
        raise InvalidOperation
    if value == value.to_integral():
        result = str(int(value))
    else:
        result = format(value.normalize(), "f")
    return result + ("%" if percent else "")


def _normalize_numeric_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_NUMERIC_TRANSLATION)


def _number_without_unit(value: str) -> str:
    return value.removesuffix("%")


def _evaluate_arithmetic(expression: str) -> float:
    operators = {
        ast.Add: lambda left, right: left + right,
        ast.Sub: lambda left, right: left - right,
        ast.Mult: lambda left, right: left * right,
        ast.Div: lambda left, right: left / right,
    }

    def evaluate(node: ast.AST, depth: int = 0) -> float:
        if depth > 12:
            raise ValueError("formula is too deep")
        if isinstance(node, ast.Expression):
            return evaluate(node.body, depth + 1)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = evaluate(node.operand, depth + 1)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and type(node.op) in operators:
            return operators[type(node.op)](
                evaluate(node.left, depth + 1), evaluate(node.right, depth + 1)
            )
        raise ValueError("formula contains an unsupported operation")

    return evaluate(ast.parse(expression, mode="eval"))


def _formula_is_correct(expression: str, result: str) -> bool:
    parts = _normalize_numeric_text(expression).split("=")
    if len(parts) not in {1, 2}:
        return False
    try:
        calculated = _evaluate_arithmetic(parts[0].strip())
        declared = float(result.removesuffix("%"))
        if len(parts) == 2 and not math.isclose(
            _evaluate_arithmetic(parts[1].strip()), declared, rel_tol=1e-9, abs_tol=1e-9
        ):
            return False
        return math.isclose(calculated, declared, rel_tol=1e-9, abs_tol=1e-9)
    except (SyntaxError, TypeError, ValueError, ZeroDivisionError):
        return False


def validate_grounding(
    claims: Sequence[Mapping[str, Any]], evidence: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Check quote, number/formula, citation, and lexical grounding contracts."""

    by_id = {str(item.get("id")): item for item in evidence}
    errors: list[dict[str, str]] = []
    for claim in claims:
        claim_id = str(claim.get("id", ""))
        text = str(claim.get("text", ""))
        source_ids = claim.get("source_ids")
        if not claim_id or not text or not isinstance(source_ids, list) or not source_ids:
            errors.append({"claim_id": claim_id, "code": "incomplete_claim"})
            continue
        missing = [str(source_id) for source_id in source_ids if str(source_id) not in by_id]
        if missing:
            errors.append({"claim_id": claim_id, "code": "unknown_source_id"})
            continue
        cited = [by_id[str(source_id)] for source_id in source_ids]
        cited_text = "\n".join(str(item.get("text", "")) for item in cited)
        direct_quote = claim.get("direct_quote")
        if direct_quote is not None:
            quote = str(direct_quote)
            quote_sources = [item for item in cited if item.get("safe_to_quote")]
            if not quote or not any(
                _normalized_text(quote) in _normalized_text(str(item.get("text", "")))
                for item in quote_sources
            ):
                errors.append({"claim_id": claim_id, "code": "quote_not_found"})
        else:
            claim_tokens = set(_tokens(text))
            evidence_tokens = set(_tokens(cited_text))
            if claim_tokens and not claim_tokens.intersection(evidence_tokens):
                errors.append({"claim_id": claim_id, "code": "insufficient_lexical_overlap"})
        supported_numbers = _numbers(cited_text)
        formula = claim.get("derived_formula")
        derived_result: set[str] = set()
        if formula is not None:
            if not isinstance(formula, Mapping) or not all(
                key in formula for key in ("expression", "inputs", "result")
            ):
                errors.append({"claim_id": claim_id, "code": "invalid_derived_formula"})
            else:
                try:
                    inputs = {_canonical_number(str(value)) for value in formula["inputs"]}
                    result = _canonical_number(str(formula["result"]))
                except (InvalidOperation, ValueError):
                    inputs = set()
                    result = ""
                if not inputs or not inputs.issubset(supported_numbers):
                    errors.append({"claim_id": claim_id, "code": "unsupported_formula_input"})
                expression_numbers = _numbers(str(formula["expression"]))
                expression_values = {_number_without_unit(value) for value in expression_numbers}
                expression_parts = _normalize_numeric_text(
                    str(formula["expression"])
                ).split("=")
                input_values = {_number_without_unit(value) for value in inputs}
                left_values = (
                    {
                        _number_without_unit(value)
                        for value in _numbers(expression_parts[0])
                    }
                    if len(expression_parts) in {1, 2}
                    else set()
                )
                right_matches_result = True
                if len(expression_parts) == 2:
                    try:
                        right_matches_result = _number_without_unit(
                            _canonical_number(expression_parts[1].strip())
                        ) == _number_without_unit(result)
                    except InvalidOperation:
                        right_matches_result = False
                unit_signatures = {
                    value.endswith("%") for value in inputs | ({result} if result else set())
                }
                if (
                    not result
                    or len(unit_signatures) != 1
                    or left_values != input_values
                    or not right_matches_result
                    or _number_without_unit(result) not in expression_values
                    or not {
                        _number_without_unit(value) for value in inputs
                    }.issubset(expression_values)
                    or not _formula_is_correct(str(formula["expression"]), result)
                ):
                    errors.append({"claim_id": claim_id, "code": "invalid_derived_formula"})
                else:
                    derived_result.add(result)
        unsupported = _numbers(text) - supported_numbers - derived_result
        if unsupported:
            errors.append({"claim_id": claim_id, "code": "unsupported_numeric"})
    return {"format_version": FORMAT_VERSION, "valid": not errors, "errors": errors}


def _load_visuals(run: Path) -> dict[str, Any]:
    contract = _read_json(run / "evidence" / "source_visuals.json")
    if not isinstance(contract.get("visuals"), list):
        raise IntegrityError("source_visuals.json requires a visuals list")
    return contract


def _validate_vlm_review_batch(review: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "reviewer_mode", "source_manifest_sha256", "source_visuals_sha256", "matches"
    }
    batch = dict(review)
    if set(batch) != required:
        raise ContractError("visual review batch has an unknown or incomplete schema")
    if batch.get("reviewer_mode") not in {"fresh_host_vlm", "fresh_subagent"}:
        raise ContractError("visual review requires a fresh vision-capable reviewer")
    if not isinstance(batch.get("source_manifest_sha256"), str) or not isinstance(
        batch.get("source_visuals_sha256"), str
    ):
        raise ContractError("visual review requires source hash bindings")
    matches = batch.get("matches")
    if not isinstance(matches, list) or not matches:
        raise ContractError("visual review requires matches")
    match_fields = {
        "visual_id", "visual_sha256", "caption_evidence_id",
        "caption_evidence_sha256", "confidence", "allowed_content_roles",
    }
    for match in matches:
        if not isinstance(match, Mapping) or set(match) != match_fields:
            raise ContractError("visual review match has an unknown or incomplete schema")
        confidence = match.get("confidence")
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not math.isfinite(confidence)
            or not 0.8 <= confidence <= 1
        ):
            raise ContractError("visual review confidence must be finite and in range")
        roles = match.get("allowed_content_roles")
        if (
            not isinstance(roles, list)
            or not roles
            or any(role not in _DEFAULT_VISUAL_ROLES for role in roles)
        ):
            raise ContractError("visual review contains invalid content roles")
    return batch


def _validate_vlm_history_value(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    if set(value) != {"format_version", "batches"} or value.get(
        "format_version"
    ) != FORMAT_VERSION:
        raise ContractError("visual review history has an unknown or incomplete schema")
    batches = value.get("batches")
    if not isinstance(batches, list) or not batches:
        raise ContractError("visual review history requires at least one batch")
    return [_validate_vlm_review_batch(batch) for batch in batches]


def _verify_vlm_history(
    run: Path, manifest: Mapping[str, Any], visuals: Mapping[str, Any]
) -> None:
    path = run / "evidence" / "host-vlm-visual-review.json"
    manifest_hash = manifest.get("host_vlm_review_sha256")
    visuals_hash = visuals.get("host_vlm_review_sha256")
    if manifest_hash is None and visuals_hash is None and not path.exists():
        return
    if (
        not isinstance(manifest_hash, str)
        or visuals_hash != manifest_hash
        or not path.is_file()
        or path.is_symlink()
        or sha256_file(path) != manifest_hash
    ):
        raise IntegrityError("host-VLM review history hash mismatch")
    try:
        _validate_vlm_history_value(_read_json(path))
    except PortableError as error:
        raise IntegrityError("invalid host-VLM review history") from error


def bind_host_vlm_visuals(run_dir: Path | str, review: Mapping[str, Any]) -> dict[str, Any]:
    """Bind PDF visual candidates to caption evidence after host-VLM inspection."""

    run, state = _load_run(run_dir)
    manifest = _verify_source_contract(run, state)
    if state.get("state") != "initialized" or manifest.get("status") != "ready":
        raise StateError("visual review binding requires an initialized ready source")
    batch = _validate_vlm_review_batch(review)
    source_manifest_sha256 = sha256_file(run / "evidence" / "source_manifest.json")
    source_visuals_sha256 = sha256_file(run / "evidence" / "source_visuals.json")
    if batch.get("source_manifest_sha256") != source_manifest_sha256:
        raise ContractError("visual review is bound to a different source manifest")
    if batch.get("source_visuals_sha256") != source_visuals_sha256:
        raise ContractError("visual review is bound to a different visual catalog")
    matches = batch["matches"]
    evidence_by_id = {item["id"]: item for item in load_evidence(run)}
    contract = _load_visuals(run)
    by_id = {item.get("id"): item for item in contract["visuals"]}
    for match in matches:
        visual_id = match.get("visual_id")
        visual = by_id.get(visual_id)
        caption_id = match.get("caption_evidence_id")
        caption = evidence_by_id.get(caption_id)
        confidence = match.get("confidence")
        roles = match.get("allowed_content_roles")
        if visual is None or visual.get("origin") != "pdf_extracted":
            raise ContractError(f"unknown PDF visual: {visual_id}")
        if (
            caption is None
            or match.get("visual_sha256") != visual.get("sha256")
            or match.get("caption_evidence_sha256") != caption.get("sha256")
        ):
            raise ContractError(f"insufficient visual-caption binding: {visual_id}")
        visual.update(
            {
                "caption_evidence_id": caption_id,
                "vlm_review": {"reviewer_mode": batch["reviewer_mode"], "confidence": confidence},
                "eligibility": "eligible",
                "allowed_content_roles": sorted(set(roles)),
            }
        )
    sidecar_path = run / "evidence" / "host-vlm-visual-review.json"
    history: list[dict[str, Any]] = []
    if sidecar_path.is_file():
        history = _validate_vlm_history_value(_read_json(sidecar_path))
    sidecar = {
        "format_version": FORMAT_VERSION,
        "batches": [*history, redact_secrets(batch)],
    }
    atomic_write_json(sidecar_path, sidecar)
    review_history_sha256 = sha256_file(sidecar_path)
    contract["host_vlm_review_sha256"] = review_history_sha256
    atomic_write_json(run / "evidence" / "source_visuals.json", contract)
    manifest = _read_json(run / "evidence" / "source_manifest.json")
    manifest["host_vlm_review_sha256"] = review_history_sha256
    manifest["source_visuals_sha256"] = sha256_file(
        run / "evidence" / "source_visuals.json"
    )
    _persist_source_manifest(run, manifest)
    return contract


def validate_visual_plan(
    run_dir: Path | str, allocations: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Validate eligible visual roles and per-visual reuse limits."""

    run, _state = _load_run(run_dir)
    visuals = _load_visuals(run)["visuals"]
    by_id = {item.get("id"): item for item in visuals}
    counts: Counter[str] = Counter()
    errors: list[dict[str, str]] = []
    for allocation in allocations:
        visual_id = str(allocation.get("visual_id", ""))
        role = str(allocation.get("role", ""))
        visual = by_id.get(visual_id)
        if visual is None:
            errors.append({"visual_id": visual_id, "code": "unknown_visual"})
            continue
        if visual.get("eligibility") != "eligible":
            errors.append({"visual_id": visual_id, "code": "visual_not_eligible"})
        if role not in visual.get("allowed_content_roles", []):
            errors.append({"visual_id": visual_id, "code": "visual_role_not_allowed"})
        counts[visual_id] += 1
        if counts[visual_id] > int(visual.get("max_reuse", 1)):
            errors.append({"visual_id": visual_id, "code": "visual_reuse_limit"})
    result = {"format_version": FORMAT_VERSION, "valid": not errors, "errors": errors}
    if errors and any(error["code"] in {"visual_not_eligible", "visual_role_not_allowed"} for error in errors):
        raise ContractError("visual plan contains an ineligible visual or role")
    return result


def write_source_map(
    run_dir: Path | str,
    attempt_id: str,
    claims: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate claim bindings and persist an attempt/source-manifest-bound map."""

    run, state = _load_run(run_dir)
    if state.get("active_attempt") != attempt_id or state.get("state") != "authoring":
        raise StateError("source map must target the active attempt")
    _verify_source_contract(run, state)
    contract = {
        "format_version": FORMAT_VERSION,
        "attempt_id": attempt_id,
        "source_manifest_sha256": sha256_file(run / "evidence" / "source_manifest.json"),
        "claims": [dict(claim) for claim in claims],
    }
    contract["grounding"] = validate_grounding(contract["claims"], load_evidence(run))
    _validate_source_map_contract(run, attempt_id, contract)
    attempt = safe_path(run / "attempts", attempt_id, must_exist=True)
    destination = safe_path(attempt, "provenance/source-map.json")
    if destination.exists():
        existing = _read_json(destination)
        if existing != contract:
            raise IntegrityError("refusing to overwrite an existing source map")
        return existing
    atomic_write_json(destination, contract)
    return contract


def _validate_source_map_contract(
    run: Path, attempt_id: str, contract: Mapping[str, Any]
) -> dict[str, Any]:
    required = {
        "format_version", "attempt_id", "source_manifest_sha256", "claims", "grounding"
    }
    value = dict(contract)
    if set(value) != required or value.get("format_version") != FORMAT_VERSION:
        raise ContractError("source map has an unknown or incomplete schema")
    if value.get("attempt_id") != attempt_id:
        raise ContractError("source map targets the wrong attempt")
    if value.get("source_manifest_sha256") != sha256_file(
        run / "evidence" / "source_manifest.json"
    ):
        raise ContractError("source map source binding is stale")
    claims = value.get("claims")
    if not isinstance(claims, list) or any(
        not isinstance(claim, Mapping) for claim in claims
    ):
        raise ContractError("source map claims must be a list of objects")
    grounding = validate_grounding(claims, load_evidence(run))
    if not grounding["valid"]:
        codes = ", ".join(error["code"] for error in grounding["errors"])
        raise ContractError(f"source map grounding failed: {codes}")
    if value.get("grounding") != grounding:
        raise ContractError("source map grounding result is stale")
    return value


def _hash_paths(root: Path, paths: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in sorted(set(paths)):
        path = safe_path(root, relative, must_exist=True)
        if not path.is_file():
            raise ContractError(f"hash-bound path is not a file: {relative}")
        result[relative] = sha256_file(path)
    return result


def record_deterministic_result(
    run_dir: Path | str,
    attempt_id: str,
    *,
    passed: bool,
    checks: Sequence[Mapping[str, Any]],
    artifact_paths: Sequence[str],
    preview_paths: Mapping[str, str],
    fail_after_write: bool = False,
) -> dict[str, Any]:
    """Write a hash-bound deterministic report, then advance on pass."""

    run, state = _load_run(run_dir)
    if state.get("state") != "authoring" or state.get("active_attempt") != attempt_id:
        raise StateError("deterministic result does not match active authoring attempt")
    attempt = safe_path(run / "attempts", attempt_id, must_exist=True)
    source_map = attempt / "provenance" / "source-map.json"
    if not source_map.is_file():
        raise StateError("deterministic QA requires a preexisting source map")
    _validate_source_map_contract(run, attempt_id, _read_json(source_map))
    if not artifact_paths:
        raise ContractError("deterministic result requires artifact paths")
    artifacts = _hash_paths(attempt, artifact_paths)
    previews: dict[str, dict[str, str]] = {}
    for frame_id, relative in sorted(preview_paths.items()):
        path = safe_path(attempt, relative, must_exist=True)
        previews[str(frame_id)] = {"path": relative, "sha256": sha256_file(path)}
    report = {
        "format_version": FORMAT_VERSION, "attempt_id": attempt_id, "passed": bool(passed),
        "checks": [dict(check) for check in checks], "artifact_hashes": artifacts,
        "previews": previews,
    }
    atomic_write_json(attempt / "qa" / "deterministic.json", report)
    if fail_after_write:
        raise SimulatedCrash("after deterministic QA write")
    if passed:
        transition_state(run, "deterministic_passed")
    else:
        mark_side_state(run, "failed", reason="deterministic checks failed")
    return report


def _validate_deterministic_report(
    run: Path, attempt_id: str, *, require_pass: bool = True
) -> dict[str, Any]:
    attempt = safe_path(run / "attempts", attempt_id, must_exist=True)
    source_map = attempt / "provenance" / "source-map.json"
    if not source_map.is_file():
        raise IntegrityError("deterministic report has no source map")
    _validate_source_map_contract(run, attempt_id, _read_json(source_map))
    report = _read_json(attempt / "qa" / "deterministic.json")
    if report.get("format_version") != FORMAT_VERSION or report.get("attempt_id") != attempt_id:
        raise IntegrityError("deterministic report attempt mismatch")
    if not isinstance(report.get("passed"), bool):
        raise IntegrityError("deterministic report has no boolean verdict")
    if require_pass and report.get("passed") is not True:
        raise IntegrityError("deterministic report is not passing")
    artifacts = report.get("artifact_hashes")
    previews = report.get("previews")
    if not isinstance(artifacts, dict) or not artifacts or not isinstance(previews, dict):
        raise IntegrityError("incomplete deterministic report")
    actual_artifacts = {
        f"artifact/{path.relative_to(attempt / 'artifact').as_posix()}"
        for path in (attempt / "artifact").rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if actual_artifacts != set(artifacts):
        raise IntegrityError("attempt artifact set differs from deterministic report")
    for relative, digest in artifacts.items():
        if not relative.startswith("artifact/"):
            raise IntegrityError(f"deterministic artifact is outside artifact directory: {relative}")
        if sha256_file(safe_path(attempt, relative, must_exist=True)) != digest:
            raise IntegrityError(f"stale artifact: {relative}")
    for frame_id, item in previews.items():
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise IntegrityError(f"invalid preview binding: {frame_id}")
        if sha256_file(safe_path(attempt, item["path"], must_exist=True)) != item["sha256"]:
            raise IntegrityError(f"stale preview: {frame_id}")
    return report


def create_review_context(
    run_dir: Path | str, attempt_id: str, *, rubric: Mapping[str, Any]
) -> dict[str, Any]:
    """Create the exact immutable context a semantic reviewer must echo."""

    run, state = _load_run(run_dir)
    if state.get("state") != "deterministic_passed" or state.get("active_attempt") != attempt_id:
        raise StateError("review context requires the active deterministic-passed attempt")
    report = _validate_deterministic_report(run, attempt_id)
    if not report["previews"]:
        raise ContractError("semantic review requires at least one rendered preview")
    attempt = safe_path(run / "attempts", attempt_id, must_exist=True)
    source_manifest_path = run / "evidence" / "source_manifest.json"
    source_map_path = attempt / "provenance" / "source-map.json"
    _validate_source_map_contract(run, attempt_id, _read_json(source_map_path))
    context: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "attempt_id": attempt_id,
        "artifact_hashes": dict(report["artifact_hashes"]),
        "preview_hashes": {frame_id: item["sha256"] for frame_id, item in report["previews"].items()},
        "preview_paths": {frame_id: item["path"] for frame_id, item in report["previews"].items()},
        "source_manifest_sha256": sha256_file(source_manifest_path),
        "source_map_sha256": sha256_file(source_map_path),
        "rubric": dict(rubric),
        "rubric_sha256": _canonical_hash(dict(rubric)),
    }
    context["context_sha256"] = _canonical_hash(context)
    atomic_write_json(attempt / "qa" / "review-context.json", context)
    return context


def _validate_review_context(run: Path, attempt_id: str) -> dict[str, Any]:
    attempt = safe_path(run / "attempts", attempt_id, must_exist=True)
    context = _read_json(attempt / "qa" / "review-context.json")
    required = {
        "format_version", "attempt_id", "artifact_hashes", "preview_hashes",
        "preview_paths", "source_manifest_sha256", "source_map_sha256", "rubric",
        "rubric_sha256", "context_sha256",
    }
    if set(context) != required or context.get("format_version") != FORMAT_VERSION:
        raise IntegrityError("review context has an unknown or incomplete schema")
    context_hash = context.pop("context_sha256", None)
    if context_hash != _canonical_hash(context):
        raise IntegrityError("review context hash mismatch")
    context["context_sha256"] = context_hash
    report = _validate_deterministic_report(run, attempt_id)
    current_artifacts = report["artifact_hashes"]
    current_previews = {key: value["sha256"] for key, value in report["previews"].items()}
    if context.get("attempt_id") != attempt_id or context.get("artifact_hashes") != current_artifacts:
        raise IntegrityError("review context artifact binding is stale")
    if context.get("preview_hashes") != current_previews:
        raise IntegrityError("review context preview binding is stale")
    if context.get("source_manifest_sha256") != sha256_file(run / "evidence" / "source_manifest.json"):
        raise IntegrityError("review context source binding is stale")
    source_map = attempt / "provenance" / "source-map.json"
    _validate_source_map_contract(run, attempt_id, _read_json(source_map))
    if context.get("source_map_sha256") != sha256_file(source_map):
        raise IntegrityError("review context source-map binding is stale")
    if context.get("rubric_sha256") != _canonical_hash(context.get("rubric")):
        raise IntegrityError("review context rubric binding is stale")
    return context


def _validate_semantic_review_value(
    context: Mapping[str, Any], attempt_id: str, review: Mapping[str, Any]
) -> dict[str, Any]:
    value = dict(review)
    required = {
        "format_version", "attempt_id", "review_context_sha256", "artifact_hashes",
        "preview_hashes", "reviewed_frame_ids", "source_manifest_sha256", "rubric_sha256",
        "source_map_sha256", "reviewer_mode", "dimension_scores", "blockers",
        "localized_repairs", "verdict", "complete",
    }
    if set(value) != required or value.get("format_version") != FORMAT_VERSION or value.get("complete") is not True:
        raise ContractError("semantic review is partial or has an unknown schema")
    if value.get("attempt_id") != attempt_id:
        raise ContractError("semantic review targets the wrong attempt")
    if value.get("review_context_sha256") != context["context_sha256"]:
        raise ContractError("semantic review context hash is stale")
    for field in (
        "artifact_hashes", "preview_hashes", "source_manifest_sha256",
        "source_map_sha256", "rubric_sha256",
    ):
        if value.get(field) != context[field]:
            raise ContractError(f"semantic review has stale {field}")
    if value.get("reviewed_frame_ids") != sorted(context["preview_hashes"]):
        raise ContractError("semantic review did not inspect every required frame")
    if value.get("reviewer_mode") not in {"fresh_subagent", "fresh_host_vlm"}:
        raise ContractError("semantic review requires an independent reviewer mode")
    if not isinstance(value.get("dimension_scores"), dict) or not value["dimension_scores"]:
        raise ContractError("semantic review requires dimension scores")
    rubric = context.get("rubric")
    expected_dimensions = rubric.get("dimensions") if isinstance(rubric, dict) else None
    if (
        not isinstance(expected_dimensions, list)
        or not expected_dimensions
        or set(value["dimension_scores"]) != set(expected_dimensions)
        or any(
            not isinstance(score, (int, float)) or isinstance(score, bool) or not 1 <= score <= 5
            for score in value["dimension_scores"].values()
        )
    ):
        raise ContractError("semantic review scores do not match the bound rubric")
    if not isinstance(value.get("blockers"), list) or not isinstance(value.get("localized_repairs"), list):
        raise ContractError("semantic review blockers and repairs must be lists")
    if value.get("verdict") not in {"pass", "fail", "needs_visual_review"}:
        raise ContractError("invalid semantic review verdict")
    return value


def _apply_semantic_review_state(run: Path, value: Mapping[str, Any]) -> None:
    if value["verdict"] == "pass" and not value["blockers"]:
        transition_state(run, "semantic_passed")
    elif value["verdict"] == "needs_visual_review":
        mark_side_state(run, "needs_visual_review", reason="semantic review requires vision")
    else:
        mark_side_state(run, "failed", reason="semantic review failed")


def _read_validated_semantic_review(run: Path, attempt_id: str) -> dict[str, Any]:
    context = _validate_review_context(run, attempt_id)
    review_path = safe_path(
        run / "attempts" / attempt_id,
        "qa/semantic-review.json",
        must_exist=True,
    )
    return _validate_semantic_review_value(context, attempt_id, _read_json(review_path))


def record_semantic_review(
    run_dir: Path | str,
    attempt_id: str,
    review: Mapping[str, Any],
    *,
    fail_after_write: bool = False,
) -> dict[str, Any]:
    """Validate a complete hash-bound review and advance only a passing verdict."""

    run, state = _load_run(run_dir)
    if state.get("state") != "deterministic_passed" or state.get("active_attempt") != attempt_id:
        raise StateError("semantic review requires active deterministic-passed attempt")
    context = _validate_review_context(run, attempt_id)
    value = _validate_semantic_review_value(context, attempt_id, review)
    attempt = safe_path(run / "attempts", attempt_id, must_exist=True)
    review_path = safe_path(attempt, "qa/semantic-review.json")
    atomic_write_json(review_path, redact_secrets(value))
    if fail_after_write:
        raise SimulatedCrash("after semantic QA write")
    _apply_semantic_review_state(run, value)
    return value


def _delivery_manifest(stage: Path, attempt_id: str, status: str) -> dict[str, Any]:
    files: dict[str, str] = {}
    for current, directories, names in os.walk(stage, followlinks=False):
        current_path = Path(current)
        for name in sorted(directories):
            if (current_path / name).is_symlink():
                raise PathSafetyError("final staging contains a symlink")
        for name in sorted(names):
            path = current_path / name
            if path.name == "delivery-manifest.json":
                continue
            if path.is_symlink() or not path.is_file():
                raise PathSafetyError("final staging contains a non-regular file")
            files[path.relative_to(stage).as_posix()] = sha256_file(path)
    return {
        "format_version": FORMAT_VERSION, "attempt_id": attempt_id,
        "verification_status": status, "files": files,
    }


def _verify_delivery(directory: Path) -> dict[str, Any]:
    if directory.is_symlink():
        raise PathSafetyError(f"delivery directory must not be a symlink: {directory}")
    manifest_path = directory / "delivery-manifest.json"
    if manifest_path.is_symlink():
        raise PathSafetyError(f"delivery manifest must not be a symlink: {manifest_path}")
    manifest = _read_json(manifest_path)
    files = manifest.get("files")
    if manifest.get("format_version") != FORMAT_VERSION or not isinstance(files, dict) or not files:
        raise IntegrityError("invalid delivery manifest")
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for current, directories, names in os.walk(directory, followlinks=False):
        current_path = Path(current)
        for name in directories:
            path = current_path / name
            if path.is_symlink():
                raise PathSafetyError(f"delivery directory contains a symlink: {path}")
            actual_directories.add(path.relative_to(directory).as_posix())
        for name in names:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise PathSafetyError(f"delivery contains a non-regular file: {path}")
            relative = path.relative_to(directory).as_posix()
            if relative != "delivery-manifest.json":
                actual_files.add(relative)
    expected_files = set(files)
    if actual_files != expected_files:
        raise IntegrityError("delivery contents do not exactly match the manifest")
    expected_directories: set[str] = set()
    for relative in expected_files:
        parent = Path(relative).parent
        while parent != Path("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    if actual_directories != expected_directories:
        raise IntegrityError("delivery contains unlisted directories")
    for relative, digest in files.items():
        if sha256_file(safe_path(directory, relative, must_exist=True)) != digest:
            raise IntegrityError(f"final delivery hash mismatch: {relative}")
    return manifest


def _validate_attempt_for_finalization(
    run: Path, state: Mapping[str, Any], attempt_id: str
) -> tuple[str, dict[str, Any], Path]:
    if state.get("active_attempt") != attempt_id or state.get("state") not in {
        "semantic_passed", "needs_visual_review", "finalized"
    }:
        raise StateError("finalization requires the active reviewed attempt")
    report = _validate_deterministic_report(run, attempt_id)
    review = _read_validated_semantic_review(run, attempt_id)
    if review.get("verdict") == "pass" and not review.get("blockers"):
        status = "verified"
        expected_state = "semantic_passed"
    elif review.get("verdict") == "needs_visual_review":
        status = "needs_visual_review"
        expected_state = "needs_visual_review"
    else:
        raise IntegrityError("semantic review does not authorize finalization")
    if state.get("state") not in {expected_state, "finalized"}:
        raise IntegrityError("run state disagrees with semantic review verdict")
    attempt = safe_path(run / "attempts", attempt_id, must_exist=True)
    source_map = attempt / "provenance" / "source-map.json"
    if not source_map.is_file():
        raise IntegrityError("finalization requires the active attempt source map")
    _validate_source_map_contract(run, attempt_id, _read_json(source_map))
    return status, report, source_map


def _verify_attempt_delivery(
    run: Path,
    state: Mapping[str, Any],
    attempt_id: str,
    directory: Path,
) -> dict[str, Any]:
    status, report, source_map = _validate_attempt_for_finalization(
        run, state, attempt_id
    )
    manifest = _verify_delivery(directory)
    expected_files = {
        relative.removeprefix("artifact/"): digest
        for relative, digest in report["artifact_hashes"].items()
    }
    expected_files["provenance/source-map.json"] = sha256_file(source_map)
    if (
        manifest.get("attempt_id") != attempt_id
        or manifest.get("verification_status") != status
        or manifest.get("files") != expected_files
    ):
        raise IntegrityError("delivery is not bound to the reviewed attempt")
    delivered_source_map = directory / "provenance" / "source-map.json"
    _validate_source_map_contract(run, attempt_id, _read_json(delivered_source_map))
    if sha256_file(delivered_source_map) != sha256_file(source_map):
        raise IntegrityError("delivery source map differs from the reviewed attempt")
    return manifest


def finalize_attempt(
    run_dir: Path | str,
    attempt_id: str,
    *,
    fail_at: str | None = None,
) -> dict[str, Any]:
    """Stage and atomically promote a verified attempt without overwriting final."""

    run, state = _load_run(run_dir)
    _verify_source_contract(run, state)
    final = run / "final"
    if final.exists() or final.is_symlink():
        existing_manifest = _verify_delivery(final)
        if existing_manifest.get("attempt_id") != attempt_id:
            raise IntegrityError("refusing to overwrite a final from another attempt")
        manifest = _verify_attempt_delivery(run, state, attempt_id, final)
        if state.get("state") != "finalized":
            state["state"] = "finalized"
            _write_run(run, state)
        return manifest
    status, report, source_map = _validate_attempt_for_finalization(
        run, state, attempt_id
    )
    attempt = safe_path(run / "attempts", attempt_id, must_exist=True)
    stage = run / f".final.staging-{attempt_id}"
    if stage.exists() or stage.is_symlink():
        shutil.rmtree(stage)
    stage.mkdir()
    try:
        for relative in sorted(report["artifact_hashes"]):
            source = safe_path(attempt, relative, must_exist=True)
            target = safe_path(stage, relative.removeprefix("artifact/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(target, source.read_bytes())
        target = stage / "provenance" / "source-map.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(target, source_map.read_bytes())
        if fail_at == "after_copy":
            raise SimulatedCrash("after final staging copy")
        manifest = _delivery_manifest(stage, attempt_id, status)
        atomic_write_json(stage / "delivery-manifest.json", manifest)
        _verify_attempt_delivery(run, state, attempt_id, stage)
        if fail_at == "after_manifest":
            raise SimulatedCrash("after delivery manifest write")
        if final.exists():
            raise IntegrityError("refusing to overwrite final directory")
        stage.rename(final)
        if fail_at == "after_rename":
            raise SimulatedCrash("after final staging rename")
    except SimulatedCrash:
        raise
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    transition_state(run, "finalized")
    _event(run, "attempt_finalized", attempt_id=attempt_id, verification_status=status)
    return manifest


def resume_run(run_dir: Path | str, *, skill_root: Path | str) -> dict[str, Any]:
    """Verify durable contracts, recover completed writes, and name the next action."""

    run, state = _load_run(run_dir)
    verify_skill_snapshot(run, skill_root=skill_root)
    _verify_source_contract(run, state)
    if state.get("state") in {"semantic_passed", "needs_visual_review"}:
        active_review_attempt = state.get("active_attempt")
        if not isinstance(active_review_attempt, str):
            raise IntegrityError("reviewed state has no active attempt")
        _read_validated_semantic_review(run, active_review_attempt)
    if (run / "final").exists():
        active_attempt = state.get("active_attempt")
        if not isinstance(active_attempt, str):
            raise IntegrityError("finalized run has no active attempt")
        _verify_attempt_delivery(run, state, active_attempt, run / "final")
        if state.get("state") != "finalized":
            state["state"] = "finalized"
            _write_run(run, state)
            _event(run, "crash_recovered", boundary="final_rename")
        return {**state, "next_action": "complete"}
    active_attempt = state.get("active_attempt")
    if isinstance(active_attempt, str):
        stage = run / f".final.staging-{active_attempt}"
        stage_manifest = stage / "delivery-manifest.json"
        if stage_manifest.is_file():
            if state.get("state") not in {"semantic_passed", "needs_visual_review"}:
                raise IntegrityError("complete staging exists in an invalid run state")
            _verify_attempt_delivery(run, state, active_attempt, stage)
            stage.rename(run / "final")
            state["state"] = "finalized"
            _write_run(run, state)
            _event(run, "crash_recovered", boundary="delivery_manifest_write")
            return {**state, "next_action": "complete"}
    current = state.get("state")
    if current == "finalized":
        raise IntegrityError("finalized run is missing its final delivery")
    if current in SIDE_STATES:
        action = {
            "blocked": "resolve_blocker", "failed": "repair", "needs_visual_review": "visual_review_or_finalize",
        }[current]
        return {**state, "next_action": action}
    if current == "initialized":
        source_manifest = _read_json(run / "evidence" / "source_manifest.json")
        action = "prepare_source" if source_manifest.get("status") == "not_prepared" else "plan"
        return {**state, "next_action": action}
    if current == "planned":
        return {**state, "next_action": "begin_attempt"}
    attempt_id = state.get("active_attempt")
    if not isinstance(attempt_id, str):
        raise IntegrityError("active state has no attempt ID")
    attempt = safe_path(run / "attempts", attempt_id, must_exist=True)
    deterministic = attempt / "qa" / "deterministic.json"
    if current == "authoring" and deterministic.is_file():
        report = _validate_deterministic_report(run, attempt_id, require_pass=False)
        if report["passed"]:
            state["state"] = "deterministic_passed"
            _write_run(run, state)
            _event(run, "crash_recovered", boundary="deterministic_qa_write")
            current = "deterministic_passed"
        else:
            mark_side_state(run, "failed", reason="deterministic checks failed")
            state = _read_json(run / "run.json")
            _event(run, "crash_recovered", boundary="failed_deterministic_qa_write")
            return {**state, "next_action": "repair"}
    if current == "authoring":
        has_artifact = any(path.is_file() for path in (attempt / "artifact").rglob("*"))
        return {**state, "next_action": "validate" if has_artifact else "author"}
    if current == "deterministic_passed":
        _validate_deterministic_report(run, attempt_id)
        semantic_path = safe_path(attempt, "qa/semantic-review.json")
        if semantic_path.is_file():
            context = _validate_review_context(run, attempt_id)
            review = _validate_semantic_review_value(
                context, attempt_id, _read_json(semantic_path)
            )
            _apply_semantic_review_state(run, review)
            state = _read_json(run / "run.json")
            _event(run, "crash_recovered", boundary="semantic_qa_write")
            if state["state"] == "semantic_passed":
                return {**state, "next_action": "finalize"}
            action = "visual_review_or_finalize" if state["state"] == "needs_visual_review" else "repair"
            return {**state, "next_action": action}
        return {**state, "next_action": "semantic_review"}
    if current == "semantic_passed":
        _validate_review_context(run, attempt_id)
        return {**state, "next_action": "finalize"}
    if current == "finalized":
        raise IntegrityError("run is finalized but final directory is missing")
    raise IntegrityError(f"unknown run state: {current}")


__all__ = [
    "AGENT_FIRST_RUN_FORMAT_VERSION", "ContractError", "IntegrityError", "MAIN_STATES",
    "PathSafetyError", "PortableError", "RELEASED_RUN_FORMAT_VERSION",
    "SIDE_STATES", "SimulatedCrash", "StateError", "append_jsonl", "atomic_write_bytes",
    "atomic_write_json", "begin_attempt", "bind_host_vlm_visuals", "create_review_context",
    "crop_source", "crop_source_from_file", "diagnose_v1_run", "finalize_attempt",
    "initialize_run", "inspect_run_format", "inspect_source", "lexical_retrieve",
    "list_source_assets", "load_evidence", "mark_side_state",
    "prepare_source", "record_deterministic_result", "record_semantic_review", "redact_secrets",
    "resume_run", "safe_path", "save_plan", "sha256_bytes", "sha256_file", "transition_state",
    "tree_hash", "validate_grounding", "validate_visual_plan", "verify_skill_snapshot",
    "write_source_map",
]
