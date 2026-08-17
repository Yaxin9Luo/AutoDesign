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
SOURCE_IMPORTANCE = ("essential", "supporting")
REPAIR_ROUTE_ORDER = {
    "layout_repair": 0,
    "content_replan": 1,
    "source_reingest": 2,
}
_SOURCE_REVIEW_DIMENSIONS = (
    "importance",
    "crop_completeness",
    "caption_claim_match",
    "label_axis_legend_readability",
    "duplicate_or_ornamental_content",
    "method_result_coverage",
    "poster_area_fit",
)
_SOURCE_REVIEWER_KINDS = ("fresh_subagent", "host_fresh_pass")
_SOURCE_STORY_KEYS = ("central_method", "primary_result")
_STRUCTURAL_SOURCE_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
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


def _plan_authorized_assets(
    plan: Mapping[str, Any], catalog: Mapping[str, Any]
) -> list[dict[str, str]]:
    if plan.get("artifact_type") != "poster":
        raise ContractError("plan revision requires artifact_type=poster")
    allocations = plan.get("visual_allocations")
    if not isinstance(allocations, list):
        raise ContractError("plan revision visual_allocations must be a list")
    catalog_assets = catalog.get("assets")
    if not isinstance(catalog_assets, list) or any(
        not isinstance(item, Mapping) for item in catalog_assets
    ):
        raise IntegrityError("reviewed catalog assets are invalid")
    by_id: dict[str, Mapping[str, Any]] = {}
    for item in catalog_assets:
        asset_id = item.get("asset_id")
        digest = item.get("sha256")
        if (
            not isinstance(asset_id, str)
            or not asset_id
            or asset_id in by_id
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or item.get("trust") != "reviewed"
            or item.get("eligible") is not True
        ):
            raise IntegrityError("reviewed catalog asset binding is invalid")
        by_id[asset_id] = item
    authorized: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in allocations:
        if not isinstance(raw, Mapping):
            raise ContractError("plan revision visual allocation must be an object")
        asset_id = raw.get("visual_id")
        if (
            not isinstance(asset_id, str)
            or not asset_id
            or asset_id != asset_id.strip()
            or asset_id in seen
        ):
            raise ContractError(
                "plan revision visual IDs must be unique canonical strings"
            )
        asset = by_id.get(asset_id)
        if asset is None:
            raise ContractError("plan revision references an unreviewed source asset")
        seen.add(asset_id)
        authorized.append({"asset_id": asset_id, "sha256": str(asset["sha256"])})
    return authorized


def _plan_documents(
    run: Path,
    state: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> tuple[int, dict[str, dict[str, Any]]]:
    parent_revision = state.get("active_plan_revision")
    parent_sha256 = state.get("active_plan_sha256")
    if parent_revision is None:
        if parent_sha256 is not None:
            raise IntegrityError("active plan parent binding is incomplete")
        revision = 1
    elif (
        not isinstance(parent_revision, int)
        or isinstance(parent_revision, bool)
        or parent_revision < 1
        or not isinstance(parent_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", parent_sha256) is None
    ):
        raise IntegrityError("active plan parent binding is invalid")
    else:
        revision = parent_revision + 1
    if revision > 999:
        raise StateError("plan revision namespace is exhausted")
    curation_revision = state.get("active_curation_revision")
    curation_sha256 = state.get("active_curation_sha256")
    if (
        not isinstance(curation_revision, int)
        or isinstance(curation_revision, bool)
        or curation_revision < 1
        or not isinstance(curation_sha256, str)
    ):
        raise IntegrityError("plan revision requires an active reviewed catalog")
    curation = _load_curation_revision(run, curation_revision)
    if curation["manifest.json"]["catalog_sha256"] != curation_sha256:
        raise IntegrityError("active reviewed catalog hash pointer is stale")
    clean_plan = redact_secrets(dict(plan))
    authorized = _plan_authorized_assets(clean_plan, curation["catalog.json"])
    plan_sha256 = sha256_bytes(_stored_json_bytes(clean_plan))
    if (
        parent_revision is not None
        and state.get("pending_supersession_operation_id") is not None
        and state.get("repair_route") == "content_replan"
        and plan_sha256 == parent_sha256
    ):
        raise StateError("content replan must change canonical plan bytes")
    operation_id = _canonical_hash(
        {
            "operation": "save_plan_revision",
            "parent_revision": parent_revision,
            "parent_plan_sha256": parent_sha256,
            "catalog_revision": curation_revision,
            "catalog_sha256": curation_sha256,
            "plan_sha256": plan_sha256,
            "authorized_assets": authorized,
        }
    )
    manifest = {
        "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
        "revision": revision,
        "parent_revision": parent_revision,
        "parent_plan_sha256": parent_sha256,
        "catalog_revision": curation_revision,
        "catalog_sha256": curation_sha256,
        "plan_sha256": plan_sha256,
        "authorized_assets": authorized,
        "operation_id": operation_id,
    }
    commit = {
        "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
        "operation_id": operation_id,
        "parent_revision": parent_revision,
        "parent_sha256": parent_sha256,
        "target_revision": revision,
        "content_sha256": plan_sha256,
        "status": "prepared",
    }
    return revision, {
        "plan.json": clean_plan,
        "manifest.json": manifest,
        "COMMIT.json": commit,
    }


def _load_plan_revision(
    run: Path,
    revision: int,
    *,
    directory: Path | None = None,
    require_catalog_lineage: bool = True,
) -> dict[str, dict[str, Any]]:
    root = directory or (run / "plans" / f"{revision:03d}")
    files, directories = _regular_tree_inventory(root)
    expected = {"plan.json", "manifest.json", "COMMIT.json"}
    if files != expected or directories:
        raise IntegrityError("plan revision file set is not exact")
    values: dict[str, dict[str, Any]] = {}
    for name in expected:
        path = root / name
        value = _read_json(path)
        if path.read_bytes() != _stored_json_bytes(value):
            raise IntegrityError("plan revision contains noncanonical JSON bytes")
        values[name] = value
    manifest = values["manifest.json"]
    commit = values["COMMIT.json"]
    if set(manifest) != {
        "run_format_version",
        "revision",
        "parent_revision",
        "parent_plan_sha256",
        "catalog_revision",
        "catalog_sha256",
        "plan_sha256",
        "authorized_assets",
        "operation_id",
    } or set(commit) != {
        "run_format_version",
        "operation_id",
        "parent_revision",
        "parent_sha256",
        "target_revision",
        "content_sha256",
        "status",
    }:
        raise IntegrityError("plan revision metadata schema is invalid")
    if (
        manifest["run_format_version"] != AGENT_FIRST_RUN_FORMAT_VERSION
        or manifest["revision"] != revision
        or commit["run_format_version"] != AGENT_FIRST_RUN_FORMAT_VERSION
        or commit["operation_id"] != manifest["operation_id"]
        or commit["parent_revision"] != manifest["parent_revision"]
        or commit["parent_sha256"] != manifest["parent_plan_sha256"]
        or commit["target_revision"] != revision
        or commit["content_sha256"] != manifest["plan_sha256"]
        or commit["status"] != "prepared"
        or sha256_file(root / "plan.json") != manifest["plan_sha256"]
    ):
        raise IntegrityError("plan revision hash or commit binding is stale")
    catalog_revision = manifest["catalog_revision"]
    if (
        not isinstance(catalog_revision, int)
        or isinstance(catalog_revision, bool)
        or catalog_revision < 1
    ):
        raise IntegrityError("plan revision catalog binding is invalid")
    curation = _load_curation_revision(
        run,
        catalog_revision,
        require_committed_lineage=require_catalog_lineage,
    )
    if curation["manifest.json"]["catalog_sha256"] != manifest["catalog_sha256"]:
        raise IntegrityError("plan revision catalog hash binding is stale")
    try:
        authorized = _plan_authorized_assets(
            values["plan.json"], curation["catalog.json"]
        )
    except ContractError as error:
        raise IntegrityError("persisted plan authorization is invalid") from error
    if manifest["authorized_assets"] != authorized:
        raise IntegrityError("plan revision authorized asset binding is stale")
    expected_operation = _canonical_hash(
        {
            "operation": "save_plan_revision",
            "parent_revision": manifest["parent_revision"],
            "parent_plan_sha256": manifest["parent_plan_sha256"],
            "catalog_revision": catalog_revision,
            "catalog_sha256": manifest["catalog_sha256"],
            "plan_sha256": manifest["plan_sha256"],
            "authorized_assets": authorized,
        }
    )
    if manifest["operation_id"] != expected_operation:
        raise IntegrityError("plan revision operation binding is stale")
    return values


def _plan_event(run: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event": "plan_revision_committed",
        "operation_id": manifest["operation_id"],
        "revision": manifest["revision"],
    }


def _bound_event_present(
    run: Path,
    expected: Mapping[str, Any],
    *,
    identity_fields: Sequence[str],
) -> bool:
    event = dict(expected)
    events = _read_event_log(run / "events.jsonl")
    candidates = [
        item
        for item in events
        if item.get("event") == event["event"]
        and any(item.get(field) == event[field] for field in identity_fields)
    ]
    if any(item != event for item in candidates):
        raise IntegrityError("bound commit event payload is conflicting")
    count = candidates.count(event)
    if count > 1:
        raise IntegrityError("bound commit event is duplicated")
    return count == 1


def _ensure_bound_event(
    run: Path,
    expected: Mapping[str, Any],
    *,
    identity_fields: Sequence[str],
) -> None:
    if not _bound_event_present(
        run, expected, identity_fields=identity_fields
    ):
        append_jsonl(run / "events.jsonl", dict(expected))


def _event_log_bytes_match_binding(
    data: bytes, binding: Mapping[str, Any]
) -> bool:
    if set(binding) != {"path", "sha256", "size", "entry_count"} or binding.get(
        "path"
    ) != "events.jsonl":
        return False
    size = binding.get("size")
    entry_count = binding.get("entry_count")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(entry_count, int)
        or isinstance(entry_count, bool)
        or entry_count < 0
        or len(data) < size
    ):
        return False
    prefix = data[:size]
    return (
        sha256_bytes(prefix) == binding.get("sha256")
        and (not prefix or prefix.endswith(b"\n"))
        and len(prefix.splitlines()) == entry_count
    )


def _recover_bound_event_in_order(
    run: Path,
    expected: Mapping[str, Any],
    *,
    identity_fields: Sequence[str],
    parent_bindings: Sequence[Mapping[str, Any]],
) -> None:
    if _bound_event_present(
        run, expected, identity_fields=identity_fields
    ):
        return
    _canonical_jsonl_binding(run, "events.jsonl")
    events = _read_event_log(run / "events.jsonl")
    current_bytes = b"".join(_canonical_json_bytes(event) for event in events)
    invalid_bindings = [
        binding
        for binding in parent_bindings
        if not _event_log_bytes_match_binding(current_bytes, binding)
    ]
    if not invalid_bindings:
        append_jsonl(run / "events.jsonl", dict(expected))
        return
    candidates: list[bytes] = []
    for index in range(len(events) + 1):
        candidate_events = [
            *events[:index],
            dict(expected),
            *events[index:],
        ]
        candidate_bytes = b"".join(
            _canonical_json_bytes(event) for event in candidate_events
        )
        if all(
            _event_log_bytes_match_binding(candidate_bytes, binding)
            for binding in invalid_bindings
        ):
            candidates.append(candidate_bytes)
    if len(candidates) != 1:
        raise IntegrityError("bound event recovery order is ambiguous")
    atomic_write_bytes(run / "events.jsonl", candidates[0])


def _plan_event_present(run: Path, manifest: Mapping[str, Any]) -> bool:
    expected = _plan_event(run, manifest)
    return _bound_event_present(
        run,
        expected,
        identity_fields=("operation_id", "revision"),
    )


def _plan_registry(
    run: Path,
    state: Mapping[str, Any],
    *,
    allow_missing_active_event: bool = False,
) -> tuple[list[int], list[Path]]:
    root = run / "plans"
    revisions: list[int] = []
    stages: list[Path] = []
    for path in root.iterdir():
        if path.is_symlink() or not path.is_dir():
            raise PathSafetyError(f"unsafe plan registry entry: {path}")
        if re.fullmatch(r"[0-9]{3}", path.name):
            revisions.append(int(path.name))
        elif re.fullmatch(r"\.plan-staging-[0-9a-f]{24}", path.name):
            stages.append(path)
        else:
            raise IntegrityError("plan registry contains an unknown directory")
    revisions.sort()
    if len(stages) > 1:
        raise IntegrityError("plan registry contains multiple staging transactions")
    active = state.get("active_plan_revision")
    active_hash = state.get("active_plan_sha256")
    if active is None:
        if active_hash is not None or revisions not in ([], [1]):
            raise IntegrityError("plan registry and active pointer disagree")
    elif (
        not isinstance(active, int)
        or isinstance(active, bool)
        or active < 1
        or not isinstance(active_hash, str)
        or revisions not in (
            list(range(1, active + 1)),
            list(range(1, active + 2)),
        )
    ):
        raise IntegrityError("plan registry and active pointer disagree")
    previous_revision: int | None = None
    previous_hash: str | None = None
    for revision in revisions:
        values = _load_plan_revision(run, revision)
        manifest = values["manifest.json"]
        if (
            manifest["parent_revision"] != previous_revision
            or manifest["parent_plan_sha256"] != previous_hash
        ):
            raise IntegrityError("plan revision graph is not contiguous")
        previous_revision = revision
        previous_hash = manifest["plan_sha256"]
        if revision == active:
            if active_hash != previous_hash:
                raise IntegrityError("active plan hash pointer is stale")
            if not _plan_event_present(run, manifest) and not allow_missing_active_event:
                raise IntegrityError("active plan revision commit event is missing")
    return revisions, stages


def _plan_documents_match(
    run: Path,
    revision: int,
    documents: Mapping[str, Mapping[str, Any]],
    *,
    directory: Path | None = None,
) -> bool:
    actual = _load_plan_revision(
        run,
        revision,
        directory=directory or (run / "plans" / f"{revision:03d}"),
    )
    return all(actual[name] == document for name, document in documents.items())


def save_plan_revision(
    run_dir: Path | str,
    plan: Mapping[str, Any],
    *,
    fail_at: str | None = None,
) -> dict[str, Any]:
    """Append one immutable plan bound to the active reviewed catalog."""

    allowed_failures = {
        None,
        "after_plan_staging_write",
        "after_plan_promotion",
        "after_plan_pointer_write",
        "after_plan_event_write",
    }
    if fail_at not in allowed_failures:
        raise ContractError(f"unknown plan revision crash boundary: {fail_at}")
    if inspect_run_format(run_dir) != AGENT_FIRST_RUN_FORMAT_VERSION:
        raise StateError("version-1 runs do not support Agent-first plan revisions")
    run = Path(run_dir).absolute()
    with _agent_first_mutation_lock(run):
        run, state = _load_agent_first_run(run)
        revisions, stages = _plan_registry(
            run, state, allow_missing_active_event=True
        )
        if state.get("state") == "planned" and isinstance(
            state.get("active_plan_revision"), int
        ):
            revision = int(state["active_plan_revision"])
            values = _load_plan_revision(run, revision)
            if values["plan.json"] != redact_secrets(dict(plan)):
                raise StateError("changed plan bytes require an authorized replan")
            manifest = values["manifest.json"]
            if stages or revisions[-1] != revision:
                raise IntegrityError("committed plan has an incomplete successor")
            if not _plan_event_present(run, manifest):
                _event(
                    run,
                    "plan_revision_committed",
                    operation_id=manifest["operation_id"],
                    revision=revision,
                )
            return {
                "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
                "plan_revision": revision,
                "plan_sha256": manifest["plan_sha256"],
                "operation_id": manifest["operation_id"],
            }
        if state.get("state") != "curated" or not plan:
            raise StateError("a non-empty plan revision requires curated state")
        revision, documents = _plan_documents(run, state, plan)
        manifest = documents["manifest.json"]
        target = run / "plans" / f"{revision:03d}"
        stage = run / "plans" / f".plan-staging-{manifest['operation_id'][:24]}"
        if stages and stages != [stage]:
            raise IntegrityError("plan registry contains a conflicting transaction")
        if target.exists() and not _plan_documents_match(
            run, revision, documents
        ):
            raise IntegrityError("plan revision conflicts with this plan")
        if not target.exists() and revisions and revisions[-1] >= revision:
            raise IntegrityError("plan target revision is occupied")
        if stage.exists() and not _plan_documents_match(
            run, revision, documents, directory=stage
        ):
            raise IntegrityError("plan staging bytes are conflicting")
        if target.exists():
            if stage.exists():
                _remove_regular_tree(stage)
        else:
            if not stage.exists():
                stage.mkdir()
                for name, document in documents.items():
                    atomic_write_json(stage / name, document)
                _load_plan_revision(run, revision, directory=stage)
                if fail_at == "after_plan_staging_write":
                    raise SimulatedCrash("after plan revision staging write")
            os.replace(stage, target)
            _fsync_directory(target.parent)
            _load_plan_revision(run, revision)
            if fail_at == "after_plan_promotion":
                raise SimulatedCrash("after plan revision promotion")
        current = _read_json(run / "run.json")
        if current.get("active_plan_revision") == revision:
            if (
                current.get("active_plan_sha256") != manifest["plan_sha256"]
                or current.get("state") != "planned"
            ):
                raise IntegrityError("active plan pointer is stale")
        else:
            if (
                current.get("state") != "curated"
                or current.get("active_plan_revision")
                != manifest["parent_revision"]
                or current.get("active_plan_sha256")
                != manifest["parent_plan_sha256"]
                or current.get("active_curation_revision")
                != manifest["catalog_revision"]
                or current.get("active_curation_sha256")
                != manifest["catalog_sha256"]
            ):
                raise IntegrityError("plan revision parent CAS mismatch")
            current["active_plan_revision"] = revision
            current["active_plan_sha256"] = manifest["plan_sha256"]
            current["state"] = "planned"
            _write_run(run, current)
            if fail_at == "after_plan_pointer_write":
                raise SimulatedCrash("after plan revision pointer write")
        if not _plan_event_present(run, manifest):
            _event(
                run,
                "plan_revision_committed",
                operation_id=manifest["operation_id"],
                revision=revision,
            )
        if fail_at == "after_plan_event_write":
            raise SimulatedCrash("after plan revision event write")
        return {
            "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
            "plan_revision": revision,
            "plan_sha256": manifest["plan_sha256"],
            "operation_id": manifest["operation_id"],
        }


def load_active_plan(run_dir: Path | str) -> dict[str, Any]:
    """Load the canonical active v2 plan revision."""

    if inspect_run_format(run_dir) != AGENT_FIRST_RUN_FORMAT_VERSION:
        raise StateError("version-1 runs do not support Agent-first plan revisions")
    run, state = _load_agent_first_run(run_dir)
    _plan_registry(run, state)
    revision = state.get("active_plan_revision")
    if not isinstance(revision, int):
        raise StateError("run has no active plan revision")
    return _load_plan_revision(run, revision)["plan.json"]


def _ledger_prefix_matches(run: Path, binding: Mapping[str, Any]) -> bool:
    if set(binding) != {"path", "sha256", "size", "entry_count"} or binding.get(
        "path"
    ) != "provenance/supersessions.jsonl":
        return False
    size = binding.get("size")
    entry_count = binding.get("entry_count")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(entry_count, int)
        or isinstance(entry_count, bool)
        or entry_count < 0
    ):
        return False
    data = (run / "provenance" / "supersessions.jsonl").read_bytes()
    prefix = data[:size]
    if len(prefix) != size or sha256_bytes(prefix) != binding.get("sha256"):
        return False
    if prefix and not prefix.endswith(b"\n"):
        return False
    try:
        entries = [json.loads(line) for line in prefix.splitlines()]
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return len(entries) == entry_count and all(
        isinstance(entry, dict) for entry in entries
    )


def _validate_attempt_context(
    run: Path,
    attempt_id: str,
    *,
    require_revision_lineage: bool = True,
) -> dict[str, Any]:
    attempt = safe_path(run / "attempts", attempt_id, must_exist=True)
    context_path = attempt / "attempt-context.json"
    catalog_path = attempt / "catalog-snapshot.json"
    plan_path = attempt / "plan-snapshot.json"
    context = _read_json(context_path)
    catalog = _read_json(catalog_path)
    plan = _read_json(plan_path)
    for path, value in (
        (context_path, context),
        (catalog_path, catalog),
        (plan_path, plan),
    ):
        if path.read_bytes() != _stored_json_bytes(value):
            raise IntegrityError("attempt snapshot contains noncanonical JSON bytes")
    required = {
        "run_format_version",
        "attempt_id",
        "source_manifest_sha256",
        "catalog_revision",
        "catalog_sha256",
        "plan_revision",
        "plan_sha256",
        "authorized_assets",
        "parent_attempt",
        "supersession_ledger",
    }
    if (
        set(context) != required
        or context.get("run_format_version") != AGENT_FIRST_RUN_FORMAT_VERSION
        or context.get("attempt_id") != attempt_id
    ):
        raise IntegrityError("attempt context has an unknown or incomplete schema")
    state = _read_json(run / "run.json")
    _verify_source_contract(run, state)
    if context["source_manifest_sha256"] != sha256_file(
        run / "evidence" / "source_manifest.json"
    ):
        raise IntegrityError("attempt context source binding is stale")
    catalog_revision = context["catalog_revision"]
    plan_revision = context["plan_revision"]
    if (
        not isinstance(catalog_revision, int)
        or isinstance(catalog_revision, bool)
        or not isinstance(plan_revision, int)
        or isinstance(plan_revision, bool)
    ):
        raise IntegrityError("attempt context revision numbers are invalid")
    curation = _load_curation_revision(
        run,
        catalog_revision,
        require_committed_lineage=require_revision_lineage,
    )
    plan_values = _load_plan_revision(
        run,
        plan_revision,
        require_catalog_lineage=require_revision_lineage,
    )
    manifest = plan_values["manifest.json"]
    if (
        catalog != curation["catalog.json"]
        or plan != plan_values["plan.json"]
        or context["catalog_sha256"]
        != curation["manifest.json"]["catalog_sha256"]
        or context["plan_sha256"] != manifest["plan_sha256"]
        or manifest["catalog_revision"] != catalog_revision
        or manifest["catalog_sha256"] != context["catalog_sha256"]
        or context["authorized_assets"] != manifest["authorized_assets"]
    ):
        raise IntegrityError("attempt revision snapshot binding is stale")
    parent = context["parent_attempt"]
    if parent is not None and (
        not isinstance(parent, str)
        or parent >= attempt_id
        or not (run / "attempts" / parent / "attempt-context.json").is_file()
    ):
        raise IntegrityError("attempt parent binding is invalid")
    if not _ledger_prefix_matches(run, context["supersession_ledger"]):
        raise IntegrityError("attempt supersession ledger prefix is stale")
    return context


def _attempt_stage_matches(
    stage: Path,
    context: Mapping[str, Any],
    catalog: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> bool:
    files, directories = _regular_tree_inventory(stage)
    if files != {
        "attempt-context.json",
        "catalog-snapshot.json",
        "plan-snapshot.json",
    } or directories != {"artifact", "qa", "qa/previews"}:
        raise IntegrityError("attempt staging file set is not exact")
    expected_values = {
        "attempt-context.json": dict(context),
        "catalog-snapshot.json": dict(catalog),
        "plan-snapshot.json": dict(plan),
    }
    for name, expected in expected_values.items():
        path = stage / name
        actual = _read_json(path)
        if path.read_bytes() != _stored_json_bytes(actual) or actual != expected:
            return False
    return True


def _attempt_started_event(context: Mapping[str, Any]) -> dict[str, Any]:
    operation_id = _canonical_hash(
        {"operation": "begin_attempt", "context": context}
    )
    return {
        "event": "attempt_started",
        "operation_id": operation_id,
        "attempt_id": context["attempt_id"],
        "parent_attempt": context["parent_attempt"],
        "catalog_revision": context["catalog_revision"],
        "plan_revision": context["plan_revision"],
    }


def _ensure_attempt_started_event(run: Path, context: Mapping[str, Any]) -> None:
    event = _attempt_started_event(context)
    _ensure_bound_event(
        run,
        event,
        identity_fields=("operation_id", "attempt_id"),
    )


def _begin_attempt_v2(
    run: Path,
    *,
    fail_at: str | None,
) -> str:
    run, state = _load_agent_first_run(run)
    _plan_registry(run, state)
    _curation_registry(run, state)
    if state.get("state") == "authoring" and state.get("active_attempt"):
        attempt_id = str(state["active_attempt"])
        context = _validate_attempt_context(run, attempt_id)
        _ensure_attempt_started_event(run, context)
        return attempt_id
    if (
        state.get("state") == "failed"
        and state.get("failure_origin") != "semantic_review"
        and isinstance(state.get("active_attempt"), str)
    ):
        attempt_id = str(state["active_attempt"])
        _validate_attempt_context(run, attempt_id)
        state["state"] = "authoring"
        for field in ("reason", "resume_from"):
            state.pop(field, None)
        _write_run(run, state)
        _event(run, "attempt_runtime_retry", attempt_id=attempt_id)
        return attempt_id
    repair = (
        state.get("state") == "failed"
        and state.get("failure_origin") == "semantic_review"
        and state.get("repair_route") == "layout_repair"
        and isinstance(state.get("active_attempt"), str)
    )
    if state.get("state") != "planned" and not repair:
        raise StateError(f"cannot begin attempt from {state.get('state')}")
    plan_revision = state.get("active_plan_revision")
    catalog_revision = state.get("active_curation_revision")
    if not isinstance(plan_revision, int) or not isinstance(catalog_revision, int):
        raise IntegrityError("attempt requires active catalog and plan revisions")
    plan_values = _load_plan_revision(run, plan_revision)
    curation = _load_curation_revision(run, catalog_revision)
    manifest = plan_values["manifest.json"]
    if manifest["catalog_revision"] != catalog_revision:
        raise IntegrityError("active plan is not bound to the active catalog")
    attempt_number = int(state.get("attempt_count", 0)) + 1
    attempt_id = f"{attempt_number:02d}"
    parent_attempt = state.get("pending_parent_attempt")
    if parent_attempt is None and repair:
        parent_attempt = state.get("active_attempt")
    ledger = _canonical_jsonl_binding(run, "provenance/supersessions.jsonl")
    pending_ledger = state.get("pending_supersession_ledger")
    if pending_ledger is not None and pending_ledger != ledger:
        raise IntegrityError("supersession ledger changed after curation reopened")
    context = {
        "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
        "attempt_id": attempt_id,
        "source_manifest_sha256": sha256_file(
            run / "evidence" / "source_manifest.json"
        ),
        "catalog_revision": catalog_revision,
        "catalog_sha256": curation["manifest.json"]["catalog_sha256"],
        "plan_revision": plan_revision,
        "plan_sha256": manifest["plan_sha256"],
        "authorized_assets": manifest["authorized_assets"],
        "parent_attempt": parent_attempt,
        "supersession_ledger": ledger,
    }
    operation_id = _canonical_hash(
        {"operation": "begin_attempt", "context": context}
    )
    target = run / "attempts" / attempt_id
    stage = run / "attempts" / f".attempt-staging-{attempt_id}-{operation_id[:24]}"
    other_stages = [
        path
        for path in (run / "attempts").iterdir()
        if path.name.startswith(".attempt-staging-") and path != stage
    ]
    if other_stages:
        raise IntegrityError("attempt registry contains a conflicting transaction")
    if target.exists():
        if _validate_attempt_context(run, attempt_id) != context:
            raise IntegrityError("attempt directory conflicts with active revisions")
    else:
        if stage.exists():
            if not _attempt_stage_matches(
                stage, context, curation["catalog.json"], plan_values["plan.json"]
            ):
                raise IntegrityError("attempt staging bytes are conflicting")
        else:
            (stage / "artifact").mkdir(parents=True)
            (stage / "qa" / "previews").mkdir(parents=True)
            atomic_write_json(stage / "attempt-context.json", context)
            atomic_write_json(stage / "catalog-snapshot.json", curation["catalog.json"])
            atomic_write_json(stage / "plan-snapshot.json", plan_values["plan.json"])
            _attempt_stage_matches(
                stage, context, curation["catalog.json"], plan_values["plan.json"]
            )
            if fail_at == "after_attempt_staging_write":
                raise SimulatedCrash("after attempt staging write")
        os.replace(stage, target)
        _fsync_directory(target.parent)
        _validate_attempt_context(run, attempt_id)
        if fail_at == "after_attempt_promotion":
            raise SimulatedCrash("after attempt promotion")
    current = _read_json(run / "run.json")
    if current.get("active_attempt") == attempt_id:
        if (
            current.get("state") != "authoring"
            or current.get("attempt_count") != attempt_number
        ):
            raise IntegrityError("active attempt pointer is stale")
    else:
        if (
            current.get("state") not in {"planned", "failed"}
            or current.get("attempt_count") != attempt_number - 1
            or current.get("active_plan_revision") != plan_revision
            or current.get("active_plan_sha256") != manifest["plan_sha256"]
            or current.get("active_curation_revision") != catalog_revision
            or current.get("active_curation_sha256")
            != curation["manifest.json"]["catalog_sha256"]
        ):
            raise IntegrityError("attempt start compare-and-set failed")
        if current.get("state") == "failed" and not repair:
            raise IntegrityError("failed run does not authorize this attempt")
        current.update(
            {
                "state": "authoring",
                "active_attempt": attempt_id,
                "attempt_count": attempt_number,
            }
        )
        if parent_attempt is not None:
            current["repair_of"] = parent_attempt
        for field in (
            "reason",
            "resume_from",
            "failure_origin",
            "repair_route",
            "semantic_review_sha256",
            "pending_parent_attempt",
            "pending_supersession_ledger",
            "pending_supersession_operation_id",
        ):
            current.pop(field, None)
        _write_run(run, current)
        if fail_at == "after_attempt_pointer_write":
            raise SimulatedCrash("after attempt pointer write")
    _ensure_attempt_started_event(run, context)
    if fail_at == "after_attempt_event_write":
        raise SimulatedCrash("after attempt event write")
    return attempt_id


def _begin_attempt_v1(run_dir: Path | str) -> str:
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


def begin_attempt(
    run_dir: Path | str,
    *,
    fail_at: str | None = None,
) -> str:
    """Start an authoring attempt under the run format's durable contract."""

    if inspect_run_format(run_dir) != AGENT_FIRST_RUN_FORMAT_VERSION:
        if fail_at is not None:
            raise ContractError("attempt crash boundaries require run format 2")
        return _begin_attempt_v1(run_dir)
    allowed_failures = {
        None,
        "after_attempt_staging_write",
        "after_attempt_promotion",
        "after_attempt_pointer_write",
        "after_attempt_event_write",
    }
    if fail_at not in allowed_failures:
        raise ContractError(f"unknown attempt crash boundary: {fail_at}")
    run = Path(run_dir).absolute()
    with _agent_first_mutation_lock(run):
        return _begin_attempt_v2(run, fail_at=fail_at)


def load_attempt_plan(run_dir: Path | str, attempt_id: str) -> dict[str, Any]:
    """Load the immutable plan snapshot for a v2 attempt."""

    if inspect_run_format(run_dir) != AGENT_FIRST_RUN_FORMAT_VERSION:
        raise StateError("version-1 runs do not support Agent-first attempt snapshots")
    run, _state = _load_agent_first_run(run_dir)
    _validate_attempt_load_lineage(run, attempt_id)
    return _read_json(run / "attempts" / attempt_id / "plan-snapshot.json")


def load_attempt_visual_catalog(
    run_dir: Path | str, attempt_id: str
) -> dict[str, Any]:
    """Load the immutable reviewed-catalog snapshot for a v2 attempt."""

    if inspect_run_format(run_dir) != AGENT_FIRST_RUN_FORMAT_VERSION:
        raise StateError("version-1 runs do not support Agent-first attempt snapshots")
    run, _state = _load_agent_first_run(run_dir)
    _validate_attempt_load_lineage(run, attempt_id)
    return _read_json(run / "attempts" / attempt_id / "catalog-snapshot.json")


_SUPERSESSION_ENTRY_KEYS = {
    "run_format_version",
    "operation_id",
    "attempt_id",
    "semantic_review_sha256",
    "repair_route",
    "reason",
    "finding_ids",
    "curation_revision",
    "plan_revision",
    "previous_entry_sha256",
    "entry_sha256",
}


def _decode_supersession_entries(data: bytes) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    previous_hash: str | None = None
    operation_ids: set[str] = set()
    for line in data.splitlines():
        value = json.loads(line)
        if not isinstance(value, dict) or set(value) != _SUPERSESSION_ENTRY_KEYS:
            raise IntegrityError("supersession ledger entry schema is invalid")
        entry = dict(value)
        entry_hash = entry.pop("entry_sha256")
        finding_ids = entry.get("finding_ids")
        request = {
            "run_format_version": entry.get("run_format_version"),
            "attempt_id": entry.get("attempt_id"),
            "semantic_review_sha256": entry.get("semantic_review_sha256"),
            "repair_route": entry.get("repair_route"),
            "reason": entry.get("reason"),
            "finding_ids": finding_ids,
            "expected_curation_revision": entry.get("curation_revision"),
            "expected_plan_revision": entry.get("plan_revision"),
        }
        expected_operation_id = _canonical_hash(
            {"operation": "reopen_curation", "request": request}
        )
        if (
            entry.get("run_format_version") != AGENT_FIRST_RUN_FORMAT_VERSION
            or entry.get("repair_route") not in {
                "content_replan",
                "source_reingest",
            }
            or not isinstance(entry.get("operation_id"), str)
            or re.fullmatch(r"[0-9a-f]{64}", entry["operation_id"]) is None
            or entry["operation_id"] in operation_ids
            or entry["operation_id"] != expected_operation_id
            or not isinstance(entry.get("attempt_id"), str)
            or not entry["attempt_id"]
            or not isinstance(entry.get("semantic_review_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", entry["semantic_review_sha256"])
            is None
            or not isinstance(entry.get("reason"), str)
            or not entry["reason"].strip()
            or entry["reason"] != entry["reason"].strip()
            or not isinstance(finding_ids, list)
            or any(
                not isinstance(item, str)
                or not item
                or item != item.strip()
                for item in finding_ids
            )
            or len(set(finding_ids)) != len(finding_ids)
            or not isinstance(entry.get("curation_revision"), int)
            or isinstance(entry["curation_revision"], bool)
            or entry["curation_revision"] < 1
            or not isinstance(entry.get("plan_revision"), int)
            or isinstance(entry["plan_revision"], bool)
            or entry["plan_revision"] < 1
            or entry.get("previous_entry_sha256") != previous_hash
            or not isinstance(entry_hash, str)
            or entry_hash != _canonical_hash(entry)
        ):
            raise IntegrityError("supersession ledger hash chain is invalid")
        entry["entry_sha256"] = entry_hash
        entries.append(entry)
        operation_ids.add(entry["operation_id"])
        previous_hash = entry_hash
    return entries


def _load_supersession_entries(run: Path) -> list[dict[str, Any]]:
    binding = _canonical_jsonl_binding(run, "provenance/supersessions.jsonl")
    return _decode_supersession_entries((run / binding["path"]).read_bytes())


def _load_bound_supersession_entries(
    run: Path, binding: Mapping[str, Any]
) -> list[dict[str, Any]]:
    if not _ledger_prefix_matches(run, binding):
        raise IntegrityError("attempt supersession ledger prefix is stale")
    path = run / str(binding["path"])
    entries = _decode_supersession_entries(path.read_bytes()[: binding["size"]])
    if len(entries) != binding["entry_count"]:
        raise IntegrityError("attempt supersession ledger entry count is stale")
    return entries


def _validate_attempt_load_lineage(run: Path, attempt_id: str) -> None:
    leaf = _validate_attempt_context(run, attempt_id)
    catalog_revision = int(leaf["catalog_revision"])
    plan_revision = int(leaf["plan_revision"])

    previous_revision: int | None = None
    previous_hash: str | None = None
    for revision in range(1, catalog_revision + 1):
        if not (run / "curations" / f"{revision:03d}").is_dir():
            raise IntegrityError("attempt catalog ancestry is not contiguous")
        values = _load_curation_revision(run, revision)
        manifest = values["manifest.json"]
        if (
            manifest["parent_revision"] != previous_revision
            or manifest["parent_catalog_sha256"] != previous_hash
        ):
            raise IntegrityError("attempt catalog ancestry is not contiguous")
        previous_revision = revision
        previous_hash = manifest["catalog_sha256"]

    previous_revision = None
    previous_hash = None
    for revision in range(1, plan_revision + 1):
        if not (run / "plans" / f"{revision:03d}").is_dir():
            raise IntegrityError("attempt plan ancestry is not contiguous")
        values = _load_plan_revision(run, revision)
        manifest = values["manifest.json"]
        if (
            manifest["parent_revision"] != previous_revision
            or manifest["parent_plan_sha256"] != previous_hash
            or not _plan_event_present(run, manifest)
        ):
            raise IntegrityError("attempt plan ancestry is not contiguous")
        previous_revision = revision
        previous_hash = manifest["plan_sha256"]

    if re.fullmatch(r"[0-9]{2}", attempt_id) is None or attempt_id == "00":
        raise IntegrityError("attempt ancestry target is invalid")
    previous_attempt: str | None = None
    for number in range(1, int(attempt_id) + 1):
        current_id = f"{number:02d}"
        if not (run / "attempts" / current_id).is_dir():
            raise IntegrityError("attempt ancestry is not contiguous")
        context = _validate_attempt_context(run, current_id)
        if context["parent_attempt"] != previous_attempt:
            raise IntegrityError("attempt ancestry is not contiguous")
        if not _bound_event_present(
            run,
            _attempt_started_event(context),
            identity_fields=("operation_id", "attempt_id"),
        ):
            raise IntegrityError("attempt start event lineage is incomplete")
        previous_attempt = current_id

    for entry in _load_bound_supersession_entries(
        run, leaf["supersession_ledger"]
    ):
        event = {
            "event": "curation_reopened",
            "operation_id": entry["operation_id"],
            "attempt_id": entry["attempt_id"],
            "repair_route": entry["repair_route"],
        }
        if not _bound_event_present(
            run,
            event,
            identity_fields=("operation_id", "attempt_id"),
        ):
            raise IntegrityError("reopen event lineage is incomplete")


def _validate_reopen_request(request: Mapping[str, Any]) -> dict[str, Any]:
    value = _exact_mapping(
        request,
        {
            "run_format_version",
            "attempt_id",
            "semantic_review_sha256",
            "repair_route",
            "reason",
            "finding_ids",
            "expected_curation_revision",
            "expected_plan_revision",
        },
        label="reopen curation request",
    )
    if value["run_format_version"] != AGENT_FIRST_RUN_FORMAT_VERSION:
        raise ContractError("reopen curation request targets the wrong run format")
    if value["repair_route"] not in {"content_replan", "source_reingest"}:
        raise ContractError("reopen curation requires content_replan or source_reingest")
    for field in ("attempt_id", "semantic_review_sha256", "reason"):
        item = value[field]
        if (
            not isinstance(item, str)
            or not item.strip()
            or item != item.strip()
        ):
            raise ContractError(f"reopen curation {field} must be canonical text")
    if re.fullmatch(r"[0-9a-f]{64}", value["semantic_review_sha256"]) is None:
        raise ContractError("reopen curation semantic review hash is invalid")
    value["finding_ids"] = _unique_nonempty_strings(
        value["finding_ids"], label="reopen curation finding_ids"
    )
    for field in ("expected_curation_revision", "expected_plan_revision"):
        revision = value[field]
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
        ):
            raise ContractError(f"reopen curation {field} is invalid")
    return value


def _supersession_entry(
    request: Mapping[str, Any],
    *,
    operation_id: str,
    previous_entry_sha256: str | None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
        "operation_id": operation_id,
        "attempt_id": request["attempt_id"],
        "semantic_review_sha256": request["semantic_review_sha256"],
        "repair_route": request["repair_route"],
        "reason": request["reason"],
        "finding_ids": request["finding_ids"],
        "curation_revision": request["expected_curation_revision"],
        "plan_revision": request["expected_plan_revision"],
        "previous_entry_sha256": previous_entry_sha256,
    }
    entry["entry_sha256"] = _canonical_hash(entry)
    return entry


def reopen_curation(
    run_dir: Path | str,
    request: Mapping[str, Any],
    *,
    fail_at: str | None = None,
) -> dict[str, Any]:
    """Persist one reviewed replan/reingest supersession authorization."""

    allowed_failures = {
        None,
        "after_supersession_append",
        "after_reopen_pointer_write",
        "after_reopen_event_write",
    }
    if fail_at not in allowed_failures:
        raise ContractError(f"unknown reopen curation crash boundary: {fail_at}")
    if inspect_run_format(run_dir) != AGENT_FIRST_RUN_FORMAT_VERSION:
        raise StateError("version-1 runs do not support reopening curation")
    run = Path(run_dir).absolute()
    with _agent_first_mutation_lock(run):
        run, state = _load_agent_first_run(run)
        value = _validate_reopen_request(request)
        attempt_id = value["attempt_id"]
        operation_id = _canonical_hash(
            {"operation": "reopen_curation", "request": value}
        )
        pending_operation = state.get("pending_supersession_operation_id")
        initial_state = (
            state.get("state") == "failed"
            and state.get("failure_origin") == "semantic_review"
        )
        retry_state = (
            pending_operation == operation_id
            and state.get("state")
            in {
                "curated" if value["repair_route"] == "content_replan" else "curating"
            }
        )
        if not initial_state and not retry_state:
            raise StateError("reopen curation requires a failed semantic review")
        if state.get("active_attempt") != attempt_id:
            raise StateError("reopen curation must target the active failed attempt")
        if (
            state.get("active_curation_revision")
            != value["expected_curation_revision"]
            or state.get("active_plan_revision")
            != value["expected_plan_revision"]
        ):
            raise IntegrityError("reopen curation revision CAS mismatch")
        review_path = run / "attempts" / attempt_id / "qa" / "semantic-review.json"
        if (
            not review_path.is_file()
            or sha256_file(review_path) != value["semantic_review_sha256"]
            or state.get("semantic_review_sha256")
            not in {None, value["semantic_review_sha256"]}
        ):
            raise IntegrityError("reopen curation semantic review hash mismatch")
        review = _read_validated_semantic_review(run, attempt_id)
        if review["verdict"] != "fail" or review["repair_route"] != value[
            "repair_route"
        ]:
            raise ContractError("reopen curation route differs from semantic review")
        expected_findings = {
            finding["finding_id"] for finding in review["route_findings"]
        }
        if set(value["finding_ids"]) != expected_findings:
            raise ContractError("reopen curation findings differ from semantic review")
        entries = _load_supersession_entries(run)
        matching = [
            entry for entry in entries if entry["operation_id"] == operation_id
        ]
        if len(matching) > 1:
            raise IntegrityError("supersession operation is duplicated")
        if matching:
            index = entries.index(matching[0])
            expected_entry = _supersession_entry(
                value,
                operation_id=operation_id,
                previous_entry_sha256=(
                    entries[index - 1]["entry_sha256"] if index else None
                ),
            )
            if matching[0] != expected_entry:
                raise IntegrityError("supersession operation bytes are conflicting")
        else:
            expected_entry = _supersession_entry(
                value,
                operation_id=operation_id,
                previous_entry_sha256=(
                    entries[-1]["entry_sha256"] if entries else None
                ),
            )
            append_jsonl(run / "provenance" / "supersessions.jsonl", expected_entry)
            entries = _load_supersession_entries(run)
        if fail_at == "after_supersession_append":
            raise SimulatedCrash("after supersession ledger append")
        ledger_binding = _canonical_jsonl_binding(
            run, "provenance/supersessions.jsonl"
        )
        current = _read_json(run / "run.json")
        target_state = (
            "curated" if value["repair_route"] == "content_replan" else "curating"
        )
        if current.get("pending_supersession_operation_id") == operation_id:
            if (
                current.get("state") != target_state
                or current.get("pending_parent_attempt") != attempt_id
                or current.get("pending_supersession_ledger") != ledger_binding
            ):
                raise IntegrityError("reopen curation state pointer is stale")
        else:
            if (
                current.get("state") != "failed"
                or current.get("failure_origin") != "semantic_review"
                or current.get("active_attempt") != attempt_id
                or current.get("active_curation_revision")
                != value["expected_curation_revision"]
                or current.get("active_plan_revision")
                != value["expected_plan_revision"]
            ):
                raise IntegrityError("reopen curation compare-and-set failed")
            current["state"] = target_state
            current["pending_parent_attempt"] = attempt_id
            current["pending_supersession_ledger"] = ledger_binding
            current["pending_supersession_operation_id"] = operation_id
            for field in ("reason", "resume_from", "failure_origin"):
                current.pop(field, None)
            _write_run(run, current)
            if fail_at == "after_reopen_pointer_write":
                raise SimulatedCrash("after reopen curation pointer write")
        event = {
            "event": "curation_reopened",
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "repair_route": value["repair_route"],
        }
        _ensure_bound_event(
            run,
            event,
            identity_fields=("operation_id", "attempt_id"),
        )
        if fail_at == "after_reopen_event_write":
            raise SimulatedCrash("after reopen curation event write")
        return {
            "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
            "repair_route": value["repair_route"],
            "state": target_state,
            "operation_id": operation_id,
            "next_action": "plan" if target_state == "curated" else "curate_source",
        }


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


def _exact_mapping(
    value: Any,
    keys: set[str],
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ContractError(f"{label} has an unknown or incomplete schema")
    return {str(key): item for key, item in value.items()}


def _unique_nonempty_strings(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str)
        or not item.strip()
        or item != item.strip()
        for item in value
    ):
        raise ContractError(f"{label} must be a list of non-empty canonical strings")
    if len(set(value)) != len(value):
        raise ContractError(f"{label} must not contain duplicates")
    return list(value)


def _regular_file_binding(run: Path, relative: str) -> dict[str, Any]:
    path = safe_path(run, relative, must_exist=True)
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise PathSafetyError(f"unsafe bound file: {path}")
    return {"path": relative, "sha256": sha256_file(path)}


def _canonical_jsonl_binding(run: Path, relative: str) -> dict[str, Any]:
    path = safe_path(run, relative, must_exist=True)
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise PathSafetyError(f"unsafe append-only ledger: {path}")
    data = path.read_bytes()
    entries: list[dict[str, Any]] = []
    if data:
        if not data.endswith(b"\n"):
            raise IntegrityError(f"append-only ledger is truncated: {path}")
        for raw in data.splitlines():
            try:
                entry = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise IntegrityError(f"append-only ledger is invalid: {path}") from error
            if not isinstance(entry, dict) or raw + b"\n" != _canonical_json_bytes(entry):
                raise IntegrityError(f"append-only ledger is noncanonical: {path}")
            entries.append(entry)
    return {
        "path": relative,
        "sha256": sha256_bytes(data),
        "size": len(data),
        "entry_count": len(entries),
    }


def _validate_source_story(
    value: Any,
    *,
    selected_ids: set[str],
    evidence_ids: set[str],
) -> dict[str, Any]:
    story = _exact_mapping(
        value,
        set(_SOURCE_STORY_KEYS),
        label="source_story",
    )
    clean: dict[str, Any] = {}
    for story_key in _SOURCE_STORY_KEYS:
        entry = _exact_mapping(
            story[story_key],
            {"status", "asset_ids", "evidence_ids", "rationale"},
            label=f"source_story {story_key}",
        )
        assets = _unique_nonempty_strings(
            entry["asset_ids"], label=f"source_story {story_key} asset_ids"
        )
        evidence = _unique_nonempty_strings(
            entry["evidence_ids"], label=f"source_story {story_key} evidence_ids"
        )
        rationale = entry["rationale"]
        if (
            not isinstance(rationale, str)
            or not rationale.strip()
            or rationale != rationale.strip()
        ):
            raise ContractError(f"source_story {story_key} requires a canonical rationale")
        if not set(assets).issubset(selected_ids) or not set(evidence).issubset(
            evidence_ids
        ):
            raise ContractError(f"source_story {story_key} references an unbound ID")
        status = entry["status"]
        if status == "covered":
            if not assets or not evidence:
                raise ContractError(
                    f"covered source_story {story_key} requires asset and evidence IDs"
                )
        elif status == "not_applicable":
            if assets or not evidence:
                raise ContractError(
                    f"not_applicable source_story {story_key} requires evidence and no assets"
                )
        else:
            raise ContractError(f"source_story {story_key} has an invalid status")
        clean[story_key] = {
            "status": status,
            "asset_ids": assets,
            "evidence_ids": evidence,
            "rationale": rationale,
        }
    return clean


def _validate_source_review_selection(
    run: Path,
    state: Mapping[str, Any],
    selection: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    value = _exact_mapping(
        selection,
        {"run_format_version", "assets", "source_story"},
        label="source review selection",
    )
    if value["run_format_version"] != AGENT_FIRST_RUN_FORMAT_VERSION:
        raise ContractError("source review selection targets the wrong run format")
    raw_assets = value["assets"]
    if not isinstance(raw_assets, list) or any(
        not isinstance(item, Mapping) for item in raw_assets
    ):
        raise ContractError("source review assets must be a list of objects")
    receipts = {
        receipt["asset_id"]: receipt
        for receipt in _load_derived_source_assets(run, state)
    }
    selected: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    for raw in raw_assets:
        item = _exact_mapping(
            raw,
            {"asset_id", "roles", "max_reuse", "importance"},
            label="source review asset",
        )
        asset_id = item["asset_id"]
        if (
            not isinstance(asset_id, str)
            or asset_id not in receipts
            or asset_id in selected_ids
        ):
            raise ContractError(
                "source review selection contains an unknown or duplicate crop"
            )
        roles = _unique_nonempty_strings(item["roles"], label="source review roles")
        if not roles or any(_STRUCTURAL_SOURCE_TOKEN.fullmatch(role) is None for role in roles):
            raise ContractError("source review role is not a structural token")
        max_reuse = item["max_reuse"]
        if (
            not isinstance(max_reuse, int)
            or isinstance(max_reuse, bool)
            or max_reuse < 1
        ):
            raise ContractError("source review max_reuse must be a positive integer")
        importance = item["importance"]
        if importance not in SOURCE_IMPORTANCE:
            raise ContractError("source review importance is invalid")
        receipt = receipts[asset_id]
        receipt_path = safe_path(run, receipt["receipt_path"], must_exist=True)
        binding = {
            "asset_id": asset_id,
            "asset_path": receipt["asset_path"],
            "asset_sha256": receipt["asset_sha256"],
            "receipt_path": receipt["receipt_path"],
            "receipt_sha256": receipt["receipt_sha256"],
            "receipt_file_sha256": sha256_file(receipt_path),
        }
        selected_ids.add(asset_id)
        selected.append(
            {
                "asset_id": asset_id,
                "roles": roles,
                "max_reuse": max_reuse,
                "importance": importance,
            }
        )
        bindings.append(binding)
    evidence = load_evidence(run)
    available_evidence = {item["id"] for item in evidence}
    story = _validate_source_story(
        value["source_story"],
        selected_ids=selected_ids,
        evidence_ids=available_evidence,
    )
    bound_evidence = sorted(
        {
            evidence_id
            for story_key in _SOURCE_STORY_KEYS
            for evidence_id in story[story_key]["evidence_ids"]
        }
    )
    return (
        {
            "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
            "assets": selected,
            "source_story": story,
        },
        bindings,
        bound_evidence,
    )


def _source_review_operation_payload(context: Mapping[str, Any]) -> dict[str, Any]:
    bindings = []
    for raw in context["asset_bindings"]:
        binding = dict(raw)
        binding.pop("preview_path", None)
        binding.pop("preview_sha256", None)
        bindings.append(binding)
    return {
        "operation": "create_source_review_context",
        "source_manifest": context["source_manifest"],
        "page_manifest": context["page_manifest"],
        "evidence_ledger": context["evidence_ledger"],
        "supersession_ledger": context["supersession_ledger"],
        "event_log_parent": context["event_log_parent"],
        "current_catalog_parent": context["current_catalog_parent"],
        "selection": context["selection"],
        "evidence_ids": context["evidence_ids"],
        "asset_bindings": bindings,
    }


def _source_review_context_file(run: Path, value: Path | str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            relative = candidate.relative_to(run)
        except ValueError as error:
            raise PathSafetyError("source review context must be inside the run") from error
    else:
        relative = candidate
    path = safe_path(run, relative, must_exist=True)
    if (
        path.name != "context.json"
        or path.parent.parent != run / "source-reviews"
        or re.fullmatch(r"review-[0-9a-f]{12}-[0-9]{3}", path.parent.name) is None
    ):
        raise PathSafetyError("source review context is outside its canonical registry")
    return path


def _load_source_review_context(
    run: Path,
    state: Mapping[str, Any],
    context_path: Path | str,
) -> dict[str, Any]:
    path = _source_review_context_file(run, context_path)
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise PathSafetyError(f"unsafe source review context: {path}")
    context = _read_json(path)
    if path.read_bytes() != _stored_json_bytes(context):
        raise IntegrityError("source review context contains noncanonical JSON bytes")
    required = {
        "run_format_version",
        "operation_id",
        "sequence",
        "context_path",
        "source_manifest",
        "page_manifest",
        "evidence_ledger",
        "supersession_ledger",
        "event_log_parent",
        "current_catalog_parent",
        "selection",
        "evidence_ids",
        "asset_bindings",
        "rubric",
        "context_sha256",
    }
    if set(context) != required or context.get("run_format_version") != AGENT_FIRST_RUN_FORMAT_VERSION:
        raise IntegrityError("source review context has an unknown or incomplete schema")
    payload = dict(context)
    context_sha256 = payload.pop("context_sha256", None)
    if context_sha256 != _canonical_hash(payload):
        raise IntegrityError("source review context hash mismatch")
    if context["context_path"] != path.relative_to(run).as_posix():
        raise IntegrityError("source review context path binding is stale")
    match = re.fullmatch(r"review-([0-9a-f]{12})-([0-9]{3})", path.parent.name)
    assert match is not None
    if (
        not isinstance(context["sequence"], int)
        or isinstance(context["sequence"], bool)
        or context["sequence"] != int(match.group(2))
        or not isinstance(context["operation_id"], str)
        or context["operation_id"][:12] != match.group(1)
        or context["operation_id"] != _canonical_hash(
            _source_review_operation_payload(context)
        )
    ):
        raise IntegrityError("source review context operation binding is stale")
    if context["rubric"] != {
        "dimensions": list(_SOURCE_REVIEW_DIMENSIONS),
        "reviewer_kinds": list(_SOURCE_REVIEWER_KINDS),
        "pass_scores": [4, 5],
    }:
        raise IntegrityError("source review context rubric binding is stale")
    selection, expected_bindings, expected_evidence = _validate_source_review_selection(
        run, state, context["selection"]
    )
    if selection != context["selection"] or expected_evidence != context["evidence_ids"]:
        raise IntegrityError("source review context selection binding is stale")
    bindings = context["asset_bindings"]
    if not isinstance(bindings, list) or len(bindings) != len(expected_bindings):
        raise IntegrityError("source review context asset bindings are incomplete")
    expected_files = {"context.json"}
    has_review = (path.parent / "review.json").exists()
    if has_review:
        expected_files.add("review.json")
    for binding, expected in zip(bindings, expected_bindings):
        if not isinstance(binding, dict):
            raise IntegrityError("source review context asset binding is invalid")
        preview_relative = f"previews/{expected['asset_id']}.png"
        if binding != {
            **expected,
            "preview_path": preview_relative,
            "preview_sha256": expected["asset_sha256"],
        }:
            raise IntegrityError("source review context crop binding is stale")
        preview_path = path.parent / preview_relative
        if (
            preview_path.is_symlink()
            or not preview_path.is_file()
            or preview_path.stat().st_nlink != 1
            or sha256_file(preview_path) != binding["preview_sha256"]
        ):
            raise IntegrityError("source review context preview binding is stale")
        expected_files.add(preview_relative)
    files, directories = _regular_tree_inventory(path.parent)
    if files != expected_files or directories != {"previews"}:
        raise IntegrityError("source review context file set is not exact")
    source_binding = _regular_file_binding(run, "evidence/source_manifest.json")
    if (
        context["source_manifest"] != source_binding
        or source_binding["sha256"] != state.get("source_manifest_sha256")
    ):
        raise IntegrityError("source review context source binding is stale")
    manifest = _verify_source_contract(run, state)
    if manifest.get("page_manifest_path") is None:
        page_binding = {"path": None, "sha256": None}
    else:
        page_binding = _regular_file_binding(run, manifest["page_manifest_path"])
    if context["page_manifest"] != page_binding:
        raise IntegrityError("source review context page binding is stale")
    if context["evidence_ledger"] != _canonical_jsonl_binding(
        run, "evidence/evidence.jsonl"
    ):
        raise IntegrityError("source review context evidence ledger binding is stale")
    if has_review:
        review_path = path.parent / "review.json"
        if review_path.is_symlink() or not review_path.is_file() or review_path.stat().st_nlink != 1:
            raise PathSafetyError(f"unsafe source review record: {review_path}")
        review = _read_json(review_path)
        if review_path.read_bytes() != _stored_json_bytes(review):
            raise IntegrityError("source review record contains noncanonical JSON bytes")
        _validate_source_review_value(context, review)
    return context


def _source_review_registry_sequences(
    run: Path,
    state: Mapping[str, Any],
    operation_prefix: str,
) -> list[int]:
    root = run / "source-reviews"
    files, directories = _regular_tree_inventory(root)
    immediate_files = {relative for relative in files if "/" not in relative}
    if immediate_files:
        raise IntegrityError("source review registry contains an unexpected file")
    immediate_directories = {
        relative for relative in directories if "/" not in relative
    }
    sequences: list[int] = []
    for name in immediate_directories:
        match = re.fullmatch(r"review-([0-9a-f]{12})-([0-9]{3})", name)
        if match is None:
            raise IntegrityError("source review registry contains an unknown directory")
        _load_source_review_context(run, state, root / name / "context.json")
        if match.group(1) == operation_prefix:
            sequences.append(int(match.group(2)))
    sequences.sort()
    if sequences and sequences != list(range(1, max(sequences) + 1)):
        raise IntegrityError("source review context sequence contains a gap")
    return sequences


def create_source_review_context(
    run_dir: Path | str,
    selection: Mapping[str, Any],
) -> dict[str, Any]:
    """Create one immutable review package over selected source crops."""

    if inspect_run_format(run_dir) != AGENT_FIRST_RUN_FORMAT_VERSION:
        raise StateError("version-1 runs do not support Agent-first source APIs")
    run = Path(run_dir).absolute()
    with _agent_first_mutation_lock(run):
        run, state = _load_agent_first_run(run)
        if state.get("state") != "curating":
            raise StateError("source review context requires curating state")
        revisions, stages = _curation_registry(run, state)
        if stages or (
            revisions
            and revisions[-1] != state.get("active_curation_revision")
        ):
            raise IntegrityError("source review cannot append over an orphan curation")
        manifest = _verify_source_contract(run, state)
        if manifest.get("status") != "ready":
            raise StateError("source review requires a ready source")
        clean_selection, bindings, evidence_ids = _validate_source_review_selection(
            run, state, selection
        )
        source_binding = _regular_file_binding(run, "evidence/source_manifest.json")
        if manifest.get("page_manifest_path") is None:
            page_binding = {"path": None, "sha256": None}
        else:
            page_binding = _regular_file_binding(run, manifest["page_manifest_path"])
        base_context: dict[str, Any] = {
            "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
            "source_manifest": source_binding,
            "page_manifest": page_binding,
            "evidence_ledger": _canonical_jsonl_binding(
                run, "evidence/evidence.jsonl"
            ),
            "supersession_ledger": _canonical_jsonl_binding(
                run, "provenance/supersessions.jsonl"
            ),
            "event_log_parent": _canonical_jsonl_binding(run, "events.jsonl"),
            "current_catalog_parent": {
                "revision": state.get("active_curation_revision"),
                "sha256": state.get("active_curation_sha256"),
            },
            "selection": clean_selection,
            "evidence_ids": evidence_ids,
            "asset_bindings": bindings,
        }
        operation_id = _canonical_hash(
            {
                "operation": "create_source_review_context",
                **{
                    key: value
                    for key, value in base_context.items()
                    if key != "run_format_version"
                },
            }
        )
        prefix = operation_id[:12]
        sequences = _source_review_registry_sequences(run, state, prefix)
        sequence = (max(sequences) + 1) if sequences else 1
        if sequence > 999:
            raise StateError("source review context sequence is exhausted")
        review_id = f"review-{prefix}-{sequence:03d}"
        root = run / "source-reviews"
        target = root / review_id
        stage = root / f".{review_id}.staging"
        if target.exists() or target.is_symlink() or stage.exists() or stage.is_symlink():
            raise IntegrityError("source review context target already exists")
        try:
            (stage / "previews").mkdir(parents=True)
            complete_bindings: list[dict[str, Any]] = []
            for binding in bindings:
                source = safe_path(run, binding["asset_path"], must_exist=True)
                preview_relative = f"previews/{binding['asset_id']}.png"
                preview = stage / preview_relative
                atomic_write_bytes(preview, source.read_bytes())
                complete_bindings.append(
                    {
                        **binding,
                        "preview_path": preview_relative,
                        "preview_sha256": sha256_file(preview),
                    }
                )
            context: dict[str, Any] = {
                **base_context,
                "operation_id": operation_id,
                "sequence": sequence,
                "context_path": f"source-reviews/{review_id}/context.json",
                "asset_bindings": complete_bindings,
                "rubric": {
                    "dimensions": list(_SOURCE_REVIEW_DIMENSIONS),
                    "reviewer_kinds": list(_SOURCE_REVIEWER_KINDS),
                    "pass_scores": [4, 5],
                },
            }
            context["context_sha256"] = _canonical_hash(context)
            atomic_write_json(stage / "context.json", context)
            expected_files = {
                "context.json",
                *(f"previews/{binding['asset_id']}.png" for binding in bindings),
            }
            files, directories = _regular_tree_inventory(stage)
            if files != expected_files or directories != {"previews"}:
                raise IntegrityError("source review staging file set is not exact")
            os.replace(stage, target)
            _fsync_directory(root)
            return _load_source_review_context(
                run, state, target / "context.json"
            )
        except Exception:
            if stage.exists() and not stage.is_symlink():
                with contextlib.suppress(PortableError, OSError):
                    _remove_regular_tree(stage)
            raise


def _canonical_review_string(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
    ):
        raise ContractError(f"{label} must be a non-empty canonical string")
    return value


def _validate_source_review_value(
    context: Mapping[str, Any],
    review: Mapping[str, Any],
) -> dict[str, Any]:
    value = _exact_mapping(
        review,
        {
            "run_format_version",
            "source_review_context_sha256",
            "reviewer_kind",
            "dimension_scores",
            "asset_findings",
            "coverage_findings",
            "blockers",
            "localized_repairs",
            "verdict",
            "complete",
        },
        label="source review",
    )
    if value["run_format_version"] != AGENT_FIRST_RUN_FORMAT_VERSION:
        raise ContractError("source review targets the wrong run format")
    if value["source_review_context_sha256"] != context["context_sha256"]:
        raise ContractError("source review context hash is stale")
    if value["reviewer_kind"] not in _SOURCE_REVIEWER_KINDS:
        raise ContractError("source review requires a fresh reviewer kind")
    if value["complete"] is not True:
        raise ContractError("source review is incomplete")
    scores = value["dimension_scores"]
    if not isinstance(scores, Mapping) or set(scores) != set(_SOURCE_REVIEW_DIMENSIONS):
        raise ContractError("source review scores do not match the bound rubric")
    clean_scores: dict[str, int] = {}
    for dimension in _SOURCE_REVIEW_DIMENSIONS:
        score = scores[dimension]
        if (
            not isinstance(score, int)
            or isinstance(score, bool)
            or not 1 <= score <= 5
        ):
            raise ContractError("source review scores must be integers from 1 to 5")
        clean_scores[dimension] = score
    verdict = value["verdict"]
    if verdict not in {"pass", "fail"}:
        raise ContractError("source review verdict must be pass or fail")
    selected_ids = {
        item["asset_id"] for item in context["selection"]["assets"]
    }
    asset_findings = value["asset_findings"]
    if not isinstance(asset_findings, list):
        raise ContractError("source review asset_findings must be a list")
    clean_asset_findings: list[dict[str, Any]] = []
    for raw in asset_findings:
        finding = _exact_mapping(
            raw,
            {"asset_id", "dimension", "finding"},
            label="source review asset finding",
        )
        if finding["asset_id"] not in selected_ids:
            raise ContractError("source review asset finding is not bound to a selected crop")
        if finding["dimension"] not in _SOURCE_REVIEW_DIMENSIONS:
            raise ContractError("source review asset finding dimension is invalid")
        clean_asset_findings.append(
            {
                "asset_id": finding["asset_id"],
                "dimension": finding["dimension"],
                "finding": _canonical_review_string(
                    finding["finding"], label="source review asset finding"
                ),
            }
        )
    coverage_findings = value["coverage_findings"]
    if not isinstance(coverage_findings, list):
        raise ContractError("source review coverage_findings must be a list")
    clean_coverage_findings: list[dict[str, Any]] = []
    for raw in coverage_findings:
        finding = _exact_mapping(
            raw,
            {"story_key", "evidence_ids", "finding"},
            label="source review coverage finding",
        )
        evidence_ids = _unique_nonempty_strings(
            finding["evidence_ids"], label="source review coverage evidence_ids"
        )
        story_key = finding["story_key"]
        if story_key not in _SOURCE_STORY_KEYS:
            raise ContractError("source review coverage finding is not context-bound")
        story_evidence = set(
            context["selection"]["source_story"][story_key]["evidence_ids"]
        )
        if not evidence_ids or not set(evidence_ids).issubset(story_evidence):
            raise ContractError("source review coverage finding is not context-bound")
        clean_coverage_findings.append(
            {
                "story_key": story_key,
                "evidence_ids": evidence_ids,
                "finding": _canonical_review_string(
                    finding["finding"], label="source review coverage finding"
                ),
            }
        )
    blockers = value["blockers"]
    if not isinstance(blockers, list):
        raise ContractError("source review blockers must be a list")
    clean_blockers: list[dict[str, Any]] = []
    for raw in blockers:
        blocker = _exact_mapping(
            raw,
            {"code", "finding"},
            label="source review blocker",
        )
        code = _canonical_review_string(blocker["code"], label="source review blocker code")
        if _STRUCTURAL_SOURCE_TOKEN.fullmatch(code) is None:
            raise ContractError("source review blocker code is not structural")
        clean_blockers.append(
            {
                "code": code,
                "finding": _canonical_review_string(
                    blocker["finding"], label="source review blocker finding"
                ),
            }
        )
    repairs = value["localized_repairs"]
    if not isinstance(repairs, list):
        raise ContractError("source review localized_repairs must be a list")
    clean_repairs: list[dict[str, Any]] = []
    valid_targets = {
        *(f"asset:{asset_id}" for asset_id in selected_ids),
        *_SOURCE_STORY_KEYS,
        "selection_set",
    }
    for raw in repairs:
        repair = _exact_mapping(
            raw,
            {"target", "instruction"},
            label="source review localized repair",
        )
        if repair["target"] not in valid_targets:
            raise ContractError("source review localized repair target is unbound")
        clean_repairs.append(
            {
                "target": repair["target"],
                "instruction": _canonical_review_string(
                    repair["instruction"], label="source review repair instruction"
                ),
            }
        )
    if verdict == "pass" and (
        clean_blockers or any(score not in {4, 5} for score in clean_scores.values())
    ):
        raise ContractError(
            "passing source review requires all scores in [4,5] and no blockers"
        )
    if verdict == "fail" and not (
        clean_asset_findings or clean_coverage_findings or clean_blockers
    ):
        raise ContractError("failing source review requires a bound finding or blocker")
    return {
        "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
        "source_review_context_sha256": context["context_sha256"],
        "reviewer_kind": value["reviewer_kind"],
        "dimension_scores": clean_scores,
        "asset_findings": clean_asset_findings,
        "coverage_findings": clean_coverage_findings,
        "blockers": clean_blockers,
        "localized_repairs": clean_repairs,
        "verdict": verdict,
        "complete": True,
    }


def _review_operation_id(
    context: Mapping[str, Any], review: Mapping[str, Any]
) -> str:
    return _canonical_hash(
        {
            "operation": "record_source_review",
            "source_review_context_sha256": context["context_sha256"],
            "source_review_sha256": sha256_bytes(_stored_json_bytes(review)),
            "parent": context["current_catalog_parent"],
            "verdict": review["verdict"],
        }
    )


def _source_review_jsonl_suffix(
    run: Path,
    binding: Any,
    *,
    label: str,
) -> list[dict[str, Any]]:
    if not isinstance(binding, Mapping) or set(binding) != {
        "path", "sha256", "size", "entry_count"
    }:
        raise IntegrityError(f"{label} binding is invalid")
    relative = binding["path"]
    if not isinstance(relative, str):
        raise IntegrityError(f"{label} path binding is invalid")
    _canonical_jsonl_binding(run, relative)
    path = safe_path(run, relative, must_exist=True)
    data = path.read_bytes()
    size = binding["size"]
    entry_count = binding["entry_count"]
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(entry_count, int)
        or isinstance(entry_count, bool)
        or entry_count < 0
        or len(data) < size
        or sha256_bytes(data[:size]) != binding["sha256"]
        or (size > 0 and data[size - 1:size] != b"\n")
        or len(data[:size].splitlines()) != entry_count
    ):
        raise IntegrityError(f"{label} bound prefix was rewritten")
    return [json.loads(raw) for raw in data[size:].splitlines()]


def _review_event_phase(
    run: Path,
    context: Mapping[str, Any],
    event: Mapping[str, Any],
) -> str:
    entries = _source_review_jsonl_suffix(
        run,
        context["event_log_parent"],
        label="source review event log",
    )
    if not entries:
        return "parent"
    if entries != [redact_secrets(dict(event))]:
        raise IntegrityError("source review event log has a conflicting suffix")
    return "committed"


def _validate_context_ledger_parents(
    run: Path,
    context: Mapping[str, Any],
) -> None:
    if context["supersession_ledger"] != _canonical_jsonl_binding(
        run, "provenance/supersessions.jsonl"
    ):
        raise IntegrityError("source review supersession ledger was rewritten")


def _curation_documents(
    context: Mapping[str, Any],
    review: Mapping[str, Any],
) -> tuple[int, dict[str, dict[str, Any]]]:
    parent = context["current_catalog_parent"]
    if not isinstance(parent, dict) or set(parent) != {"revision", "sha256"}:
        raise IntegrityError("source review catalog parent binding is invalid")
    parent_revision = parent["revision"]
    parent_sha256 = parent["sha256"]
    if parent_revision is None:
        if parent_sha256 is not None:
            raise IntegrityError("source review catalog parent is incomplete")
        revision = 1
    elif (
        not isinstance(parent_revision, int)
        or isinstance(parent_revision, bool)
        or parent_revision < 1
        or not isinstance(parent_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", parent_sha256) is None
    ):
        raise IntegrityError("source review catalog parent is invalid")
    else:
        revision = parent_revision + 1
    if revision > 999:
        raise StateError("curation revision namespace is exhausted")
    selected = {
        item["asset_id"]: item for item in context["selection"]["assets"]
    }
    catalog_assets = []
    for binding in context["asset_bindings"]:
        item = selected[binding["asset_id"]]
        catalog_assets.append(
            {
                "asset_id": binding["asset_id"],
                "path": binding["asset_path"],
                "sha256": binding["asset_sha256"],
                "receipt_path": binding["receipt_path"],
                "receipt_sha256": binding["receipt_sha256"],
                "receipt_file_sha256": binding["receipt_file_sha256"],
                "roles": item["roles"],
                "max_reuse": item["max_reuse"],
                "importance": item["importance"],
                "trust": "reviewed",
                "eligible": True,
            }
        )
    review_sha256 = sha256_bytes(_stored_json_bytes(review))
    catalog: dict[str, Any] = {
        "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
        "revision": revision,
        "parent": {"revision": parent_revision, "sha256": parent_sha256},
        "source_manifest": context["source_manifest"],
        "page_manifest": context["page_manifest"],
        "source_review_context_path": context["context_path"],
        "source_review_context_sha256": context["context_sha256"],
        "source_review_sha256": review_sha256,
        "assets": catalog_assets,
        "source_story": context["selection"]["source_story"],
    }
    catalog_sha256 = sha256_bytes(_stored_json_bytes(catalog))
    operation_id = _review_operation_id(context, review)
    manifest = {
        "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
        "revision": revision,
        "parent_revision": parent_revision,
        "parent_catalog_sha256": parent_sha256,
        "source_manifest_sha256": context["source_manifest"]["sha256"],
        "page_manifest_sha256": context["page_manifest"]["sha256"],
        "source_review_context_path": context["context_path"],
        "source_review_context_sha256": context["context_sha256"],
        "catalog_sha256": catalog_sha256,
        "review_sha256": review_sha256,
        "operation_id": operation_id,
    }
    commit = {
        "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
        "operation_id": operation_id,
        "parent_revision": parent_revision,
        "parent_sha256": parent_sha256,
        "target_revision": revision,
        "content_sha256": catalog_sha256,
        "status": "prepared",
    }
    return revision, {
        "catalog.json": catalog,
        "review.json": dict(review),
        "manifest.json": manifest,
        "COMMIT.json": commit,
    }


def _catalog_asset_binding_set(catalog: Mapping[str, Any]) -> set[tuple[str, str]]:
    assets = catalog.get("assets")
    if not isinstance(assets, list):
        raise IntegrityError("reviewed catalog asset list is invalid")
    bindings: set[tuple[str, str]] = set()
    for item in assets:
        if not isinstance(item, Mapping):
            raise IntegrityError("reviewed catalog asset binding is invalid")
        asset_id = item.get("asset_id")
        digest = item.get("sha256")
        if not isinstance(asset_id, str) or not isinstance(digest, str):
            raise IntegrityError("reviewed catalog asset binding is invalid")
        binding = (asset_id, digest)
        if binding in bindings:
            raise IntegrityError("reviewed catalog asset binding is duplicated")
        bindings.add(binding)
    return bindings


def _validate_committed_curation_lineage(
    run: Path,
    revision: int,
    manifest: Mapping[str, Any],
    context: Mapping[str, Any],
    review: Mapping[str, Any],
    *,
    allow_missing_pass_event: bool = False,
) -> None:
    review_relative = Path(str(context["context_path"])).with_name(
        "review.json"
    ).as_posix()
    original_path = safe_path(run, review_relative)
    if (
        original_path.is_symlink()
        or not original_path.is_file()
        or original_path.stat().st_nlink != 1
    ):
        raise IntegrityError("committed curation original review is missing or unsafe")
    original_review = _read_json(original_path)
    if (
        not isinstance(original_review, dict)
        or original_path.read_bytes() != _stored_json_bytes(original_review)
        or original_review != review
        or sha256_file(original_path) != manifest["review_sha256"]
    ):
        raise IntegrityError("committed curation original review does not match")
    _source_review_jsonl_suffix(
        run,
        context["supersession_ledger"],
        label="source review supersession ledger",
    )
    event = {
        "event": "source_review_passed",
        "operation_id": manifest["operation_id"],
        "revision": revision,
    }
    events = _source_review_jsonl_suffix(
        run,
        context["event_log_parent"],
        label="source review event log",
    )
    if not events and allow_missing_pass_event:
        return
    if not events or events[0] != event or events.count(event) != 1:
        raise IntegrityError("committed curation pass event lineage is invalid")


def _load_curation_revision(
    run: Path,
    revision: int,
    *,
    directory: Path | None = None,
    require_committed_lineage: bool = True,
    allow_missing_pass_event: bool = False,
) -> dict[str, dict[str, Any]]:
    root = directory or (run / "curations" / f"{revision:03d}")
    files, directories = _regular_tree_inventory(root)
    expected = {"catalog.json", "review.json", "manifest.json", "COMMIT.json"}
    if files != expected or directories:
        raise IntegrityError("curation revision file set is not exact")
    values: dict[str, dict[str, Any]] = {}
    for name in expected:
        path = root / name
        value = _read_json(path)
        if not isinstance(value, dict) or path.read_bytes() != _stored_json_bytes(value):
            raise IntegrityError("curation revision contains noncanonical JSON bytes")
        values[name] = value
    manifest = values["manifest.json"]
    commit = values["COMMIT.json"]
    if set(manifest) != {
        "run_format_version",
        "revision",
        "parent_revision",
        "parent_catalog_sha256",
        "source_manifest_sha256",
        "page_manifest_sha256",
        "source_review_context_path",
        "source_review_context_sha256",
        "catalog_sha256",
        "review_sha256",
        "operation_id",
    } or set(commit) != {
        "run_format_version",
        "operation_id",
        "parent_revision",
        "parent_sha256",
        "target_revision",
        "content_sha256",
        "status",
    }:
        raise IntegrityError("curation revision metadata schema is invalid")
    if (
        manifest["run_format_version"] != AGENT_FIRST_RUN_FORMAT_VERSION
        or manifest["revision"] != revision
        or commit["run_format_version"] != AGENT_FIRST_RUN_FORMAT_VERSION
        or commit["operation_id"] != manifest["operation_id"]
        or commit["parent_revision"] != manifest["parent_revision"]
        or commit["parent_sha256"] != manifest["parent_catalog_sha256"]
        or commit["target_revision"] != revision
        or commit["content_sha256"] != manifest["catalog_sha256"]
        or commit["status"] != "prepared"
        or sha256_file(root / "catalog.json") != manifest["catalog_sha256"]
        or sha256_file(root / "review.json") != manifest["review_sha256"]
    ):
        raise IntegrityError("curation revision hash or commit binding is stale")
    state = _read_json(run / "run.json")
    context = _load_source_review_context(
        run,
        state,
        manifest["source_review_context_path"],
    )
    review = _validate_source_review_value(context, values["review.json"])
    if review["verdict"] != "pass":
        raise IntegrityError("curation revision is bound to a failing review")
    expected_revision, expected = _curation_documents(context, review)
    if expected_revision != revision or any(
        values[name] != document for name, document in expected.items()
    ):
        raise IntegrityError("curation revision provenance binding is stale")
    if require_committed_lineage:
        _validate_committed_curation_lineage(
            run,
            revision,
            manifest,
            context,
            review,
            allow_missing_pass_event=allow_missing_pass_event,
        )
    return values


def _curation_registry(
    run: Path,
    state: Mapping[str, Any],
    *,
    allow_incomplete_active_event: bool = False,
) -> tuple[list[int], list[Path]]:
    root = run / "curations"
    immediate = list(root.iterdir())
    revisions: list[int] = []
    stages: list[Path] = []
    for path in immediate:
        if path.is_symlink() or not path.is_dir():
            raise PathSafetyError(f"unsafe curation registry entry: {path}")
        if re.fullmatch(r"[0-9]{3}", path.name):
            revisions.append(int(path.name))
        elif re.fullmatch(r"\.curation-staging-[0-9a-f]{24}", path.name):
            stages.append(path)
        else:
            raise IntegrityError("curation registry contains an unknown directory")
    revisions.sort()
    if len(stages) > 1:
        raise IntegrityError("curation registry contains multiple staging transactions")
    active = state.get("active_curation_revision")
    active_hash = state.get("active_curation_sha256")
    if active is None:
        if active_hash is not None or revisions not in ([], [1]):
            raise IntegrityError("curation registry and active pointer disagree")
    elif (
        not isinstance(active, int)
        or isinstance(active, bool)
        or active < 1
        or not isinstance(active_hash, str)
        or revisions not in (
            list(range(1, active + 1)),
            list(range(1, active + 2)),
        )
    ):
        raise IntegrityError("curation registry and active pointer disagree")
    previous_revision: int | None = None
    previous_hash: str | None = None
    for revision in revisions:
        values = _load_curation_revision(
            run,
            revision,
            require_committed_lineage=(
                active is not None and revision <= active
            ),
            allow_missing_pass_event=(
                allow_incomplete_active_event and revision == active
            ),
        )
        manifest = values["manifest.json"]
        if (
            manifest["parent_revision"] != previous_revision
            or manifest["parent_catalog_sha256"] != previous_hash
        ):
            raise IntegrityError("curation revision graph is not contiguous")
        previous_revision = revision
        previous_hash = manifest["catalog_sha256"]
        if revision == active and active_hash != previous_hash:
            raise IntegrityError("active curation hash pointer is stale")
    return revisions, stages


def _curation_documents_match(
    run: Path,
    revision: int,
    documents: Mapping[str, Mapping[str, Any]],
    *,
    directory: Path | None = None,
) -> bool:
    root = directory or (run / "curations" / f"{revision:03d}")
    actual = _load_curation_revision(
        run,
        revision,
        directory=root,
        require_committed_lineage=False,
    )
    return all(actual[name] == value for name, value in documents.items())


def _record_source_review_result(
    run: Path,
    context: Mapping[str, Any],
    review: Mapping[str, Any],
    *,
    revision: int | None,
) -> dict[str, Any]:
    result = {
        "run_format_version": AGENT_FIRST_RUN_FORMAT_VERSION,
        "verdict": review["verdict"],
        "state": "curated" if review["verdict"] == "pass" else "curating",
        "context_path": context["context_path"],
        "context_sha256": context["context_sha256"],
        "review_path": str(Path(context["context_path"]).with_name("review.json")),
    }
    if revision is not None:
        manifest = _load_curation_revision(run, revision)["manifest.json"]
        result.update(
            {
                "curation_revision": revision,
                "curation_sha256": manifest["catalog_sha256"],
            }
        )
    return result


def record_source_review(
    run_dir: Path | str,
    context_path: Path | str,
    review: Mapping[str, Any],
    *,
    fail_at: str | None = None,
) -> dict[str, Any]:
    """Record a fresh review and atomically publish a passing catalog."""

    allowed_failures = {
        None,
        "after_review_staging_write",
        "after_curation_promotion",
        "after_curation_pointer_write",
        "after_curation_event_write",
    }
    if fail_at not in allowed_failures:
        raise ContractError(f"unknown source review crash boundary: {fail_at}")
    if inspect_run_format(run_dir) != AGENT_FIRST_RUN_FORMAT_VERSION:
        raise StateError("version-1 runs do not support Agent-first source APIs")
    run = Path(run_dir).absolute()
    with _agent_first_mutation_lock(run):
        run, state = _load_agent_first_run(run)
        revisions, stages = _curation_registry(
            run,
            state,
            allow_incomplete_active_event=True,
        )
        context = _load_source_review_context(run, state, context_path)
        _source_review_registry_sequences(
            run, state, str(context["operation_id"])[:12]
        )
        value = _validate_source_review_value(context, review)
        if value["verdict"] == "fail" and fail_at is not None:
            raise ContractError("curation crash boundaries require a passing review")
        active_revision = state.get("active_curation_revision")
        has_orphan = bool(revisions) and revisions[-1] != active_revision
        if value["verdict"] == "fail" and (stages or has_orphan):
            raise IntegrityError("failing source review conflicts with an incomplete curation")
        review_path = (run / context["context_path"]).with_name("review.json")
        operation_id = _review_operation_id(context, value)
        revision: int | None = None
        documents: dict[str, dict[str, Any]] | None = None
        target: Path | None = None
        stage: Path | None = None
        if value["verdict"] == "pass":
            revision, documents = _curation_documents(context, value)
            if (
                state.get("pending_supersession_operation_id") is not None
                and state.get("repair_route") == "source_reingest"
            ):
                parent_revision = documents["manifest.json"]["parent_revision"]
                if not isinstance(parent_revision, int):
                    raise IntegrityError(
                        "source reingest has no parent catalog revision"
                    )
                parent_catalog = _load_curation_revision(
                    run, parent_revision
                )["catalog.json"]
                if not (
                    _catalog_asset_binding_set(documents["catalog.json"])
                    - _catalog_asset_binding_set(parent_catalog)
                ):
                    raise StateError(
                        "source reingest must select a new immutable asset binding"
                    )
            target = run / "curations" / f"{revision:03d}"
            stage = run / "curations" / f".curation-staging-{operation_id[:24]}"
            if stages and stages != [stage]:
                raise IntegrityError("curation registry contains a conflicting transaction")
            if target.exists() and not _curation_documents_match(
                run, revision, documents
            ):
                raise IntegrityError("curation revision conflicts with this source review")
            if not target.exists() and revisions and revisions[-1] >= revision:
                raise IntegrityError("curation target revision is occupied")
            if stage.exists() and not _curation_documents_match(
                run, revision, documents, directory=stage
            ):
                raise IntegrityError("curation staging bytes are conflicting")
        event = {
            "event": (
                "source_review_failed"
                if value["verdict"] == "fail"
                else "source_review_passed"
            ),
            "operation_id": operation_id,
        }
        if revision is not None:
            event["revision"] = revision
        event_phase = _review_event_phase(run, context, event)
        if review_path.exists() or review_path.is_symlink():
            if review_path.is_symlink() or not review_path.is_file() or review_path.stat().st_nlink != 1:
                raise PathSafetyError(f"unsafe source review record: {review_path}")
            persisted = _read_json(review_path)
            if review_path.read_bytes() != _stored_json_bytes(persisted):
                raise IntegrityError("source review record contains noncanonical JSON bytes")
            if persisted != value:
                raise StateError("refusing to overwrite an existing source review")
        else:
            if state.get("state") != "curating":
                raise StateError("a new source review requires curating state")
            parent = context["current_catalog_parent"]
            if (
                state.get("active_curation_revision") != parent["revision"]
                or state.get("active_curation_sha256") != parent["sha256"]
            ):
                raise IntegrityError("source review catalog parent CAS mismatch")
            _validate_context_ledger_parents(run, context)
            if event_phase != "parent":
                raise IntegrityError("source review event appeared before its record")
            atomic_write_json(review_path, value)

        _validate_context_ledger_parents(run, context)
        if event_phase == "committed":
            if value["verdict"] == "pass":
                assert revision is not None
                assert documents is not None
                assert target is not None
                catalog_sha256 = documents["manifest.json"]["catalog_sha256"]
                if (
                    not target.exists()
                    or stages
                    or state.get("state") != "curated"
                    or state.get("active_curation_revision") != revision
                    or state.get("active_curation_sha256") != catalog_sha256
                ):
                    raise IntegrityError(
                        "committed source review event lacks catalog state"
                    )
            return _record_source_review_result(
                run, context, value, revision=revision
            )
        if _review_event_phase(run, context, event) != "parent":
            raise IntegrityError("source review event changed before catalog commit")
        if value["verdict"] == "fail":
            _event(run, "source_review_failed", operation_id=operation_id)
            return _record_source_review_result(
                run, context, value, revision=None
            )

        assert revision is not None
        assert documents is not None
        assert target is not None
        assert stage is not None
        if target.exists():
            if stage.exists():
                if not _curation_documents_match(
                    run, revision, documents, directory=stage
                ):
                    raise IntegrityError("curation staging conflicts with its target")
                _remove_regular_tree(stage)
        else:
            if not stage.exists():
                stage.mkdir()
                for name, document in documents.items():
                    atomic_write_json(stage / name, document)
                _load_curation_revision(
                    run,
                    revision,
                    directory=stage,
                    require_committed_lineage=False,
                )
                if fail_at == "after_review_staging_write":
                    raise SimulatedCrash("after source review curation staging write")
            os.replace(stage, target)
            _fsync_directory(target.parent)
            _load_curation_revision(
                run,
                revision,
                require_committed_lineage=False,
            )
            if fail_at == "after_curation_promotion":
                raise SimulatedCrash("after source review curation promotion")

        current = _read_json(run / "run.json")
        parent = context["current_catalog_parent"]
        catalog_sha256 = documents["manifest.json"]["catalog_sha256"]
        if current.get("active_curation_revision") == revision:
            if (
                current.get("active_curation_sha256") != catalog_sha256
                or current.get("state") != "curated"
            ):
                raise IntegrityError("active curation pointer is stale")
        else:
            if (
                current.get("state") != "curating"
                or current.get("active_curation_revision") != parent["revision"]
                or current.get("active_curation_sha256") != parent["sha256"]
            ):
                raise IntegrityError("source review catalog parent CAS mismatch")
            current["active_curation_revision"] = revision
            current["active_curation_sha256"] = catalog_sha256
            current["state"] = "curated"
            _write_run(run, current)
            if fail_at == "after_curation_pointer_write":
                raise SimulatedCrash("after source review curation pointer write")
        phase = _review_event_phase(run, context, event)
        if phase == "parent":
            _event(
                run,
                "source_review_passed",
                operation_id=operation_id,
                revision=revision,
            )
        if fail_at == "after_curation_event_write":
            raise SimulatedCrash("after source review curation event write")
        return _record_source_review_result(
            run, context, value, revision=revision
        )


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
    context: Mapping[str, Any],
    attempt_id: str,
    review: Mapping[str, Any],
    *,
    run_format_version: int = RELEASED_RUN_FORMAT_VERSION,
) -> dict[str, Any]:
    value = dict(review)
    required = {
        "format_version", "attempt_id", "review_context_sha256", "artifact_hashes",
        "preview_hashes", "reviewed_frame_ids", "source_manifest_sha256", "rubric_sha256",
        "source_map_sha256", "reviewer_mode", "dimension_scores", "blockers",
        "localized_repairs", "verdict", "complete",
    }
    if run_format_version == AGENT_FIRST_RUN_FORMAT_VERSION:
        required.update({"repair_route", "route_findings"})
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
    allowed_verdicts = (
        {"pass", "fail"}
        if run_format_version == AGENT_FIRST_RUN_FORMAT_VERSION
        else {"pass", "fail", "needs_visual_review"}
    )
    if value.get("verdict") not in allowed_verdicts:
        raise ContractError("invalid semantic review verdict")
    if run_format_version == AGENT_FIRST_RUN_FORMAT_VERSION:
        route = value.get("repair_route")
        findings = value.get("route_findings")
        if route is not None and route not in REPAIR_ROUTE_ORDER:
            raise ContractError("semantic review repair route is invalid")
        if not isinstance(findings, list):
            raise ContractError("semantic review route_findings must be a list")
        clean_findings: list[dict[str, Any]] = []
        finding_ids: set[str] = set()
        for raw in findings:
            finding = _exact_mapping(
                raw,
                {"finding_id", "code", "minimum_route", "block_id", "message"},
                label="semantic repair finding",
            )
            for field in ("finding_id", "code", "block_id", "message"):
                item = finding[field]
                if (
                    not isinstance(item, str)
                    or not item.strip()
                    or item != item.strip()
                ):
                    raise ContractError(
                        f"semantic repair finding {field} must be canonical text"
                    )
            if finding["finding_id"] in finding_ids:
                raise ContractError("semantic repair finding IDs must be unique")
            minimum_route = finding["minimum_route"]
            if minimum_route not in REPAIR_ROUTE_ORDER:
                raise ContractError("semantic repair finding minimum route is invalid")
            finding_ids.add(finding["finding_id"])
            clean_findings.append(finding)
        value["route_findings"] = clean_findings
        if value["verdict"] == "pass":
            if route is not None or clean_findings or value["blockers"]:
                raise ContractError("passing semantic review cannot request repair")
        else:
            if route is None or (not clean_findings and not value["blockers"]):
                raise ContractError(
                    "failing semantic review requires a route and finding or blocker"
                )
            if any(
                REPAIR_ROUTE_ORDER[route]
                < REPAIR_ROUTE_ORDER[finding["minimum_route"]]
                for finding in clean_findings
            ):
                raise ContractError("semantic review repair route is a downgrade")
    return value


def _apply_semantic_review_state(
    run: Path,
    value: Mapping[str, Any],
    *,
    run_format_version: int = RELEASED_RUN_FORMAT_VERSION,
    review_sha256: str | None = None,
) -> None:
    if run_format_version == AGENT_FIRST_RUN_FORMAT_VERSION:
        if review_sha256 is None:
            raise IntegrityError("v2 semantic review state requires a review hash")
        if value["verdict"] == "pass":
            transition_state(
                run,
                "semantic_passed",
                semantic_review_sha256=review_sha256,
            )
            return
        current = _read_json(run / "run.json")
        previous = current.get("state")
        if previous != "deterministic_passed":
            raise IntegrityError("v2 semantic review state parent is invalid")
        current.update(
            {
                "state": "failed",
                "reason": "semantic review failed",
                "resume_from": previous,
                "failure_origin": "semantic_review",
                "repair_route": value["repair_route"],
                "semantic_review_sha256": review_sha256,
            }
        )
        _write_run(run, current)
        _event(
            run,
            "side_state",
            previous=previous,
            state="failed",
            reason="semantic review failed",
        )
        return
    if value["verdict"] == "pass" and not value["blockers"]:
        transition_state(run, "semantic_passed")
    elif value["verdict"] == "needs_visual_review":
        mark_side_state(run, "needs_visual_review", reason="semantic review requires vision")
    else:
        mark_side_state(run, "failed", reason="semantic review failed")


def _repair_split_v2_semantic_failure(
    run: Path,
    state: Mapping[str, Any],
    attempt_id: str,
    review: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        state.get("state") != "failed"
        or state.get("resume_from") != "deterministic_passed"
        or state.get("reason") != "semantic review failed"
        or any(
            state.get(field) is not None
            for field in (
                "failure_origin",
                "repair_route",
                "semantic_review_sha256",
            )
        )
        or review.get("verdict") != "fail"
    ):
        raise IntegrityError("persisted semantic review and run state disagree")
    review_path = run / "attempts" / attempt_id / "qa" / "semantic-review.json"
    repaired = dict(state)
    repaired.update(
        {
            "failure_origin": "semantic_review",
            "repair_route": review["repair_route"],
            "semantic_review_sha256": sha256_file(review_path),
        }
    )
    _write_run(run, repaired)
    return repaired


def _read_validated_semantic_review(run: Path, attempt_id: str) -> dict[str, Any]:
    run_format_version = inspect_run_format(run)
    if run_format_version == AGENT_FIRST_RUN_FORMAT_VERSION:
        _validate_attempt_context(run, attempt_id)
    context = _validate_review_context(run, attempt_id)
    review_path = safe_path(
        run / "attempts" / attempt_id,
        "qa/semantic-review.json",
        must_exist=True,
    )
    return _validate_semantic_review_value(
        context,
        attempt_id,
        _read_json(review_path),
        run_format_version=run_format_version,
    )


def record_semantic_review(
    run_dir: Path | str,
    attempt_id: str,
    review: Mapping[str, Any],
    *,
    fail_after_write: bool = False,
) -> dict[str, Any]:
    """Validate a complete hash-bound review and advance only a passing verdict."""

    if inspect_run_format(run_dir) == AGENT_FIRST_RUN_FORMAT_VERSION:
        run = Path(run_dir).absolute()
        with _agent_first_mutation_lock(run):
            run, state = _load_agent_first_run(run)
            review_path = safe_path(
                run / "attempts" / attempt_id, "qa/semantic-review.json"
            )
            if state.get("state") != "deterministic_passed" or state.get(
                "active_attempt"
            ) != attempt_id:
                if review_path.is_file():
                    persisted = _read_validated_semantic_review(run, attempt_id)
                    requested = _validate_semantic_review_value(
                        _validate_review_context(run, attempt_id),
                        attempt_id,
                        review,
                        run_format_version=AGENT_FIRST_RUN_FORMAT_VERSION,
                    )
                    if persisted != requested:
                        raise IntegrityError(
                            "refusing to overwrite an existing semantic review"
                        )
                    if (
                        state.get("state") == "failed"
                        and state.get("failure_origin") is None
                    ):
                        _repair_split_v2_semantic_failure(
                            run, state, attempt_id, persisted
                        )
                    return persisted
                raise StateError(
                    "semantic review requires active deterministic-passed attempt"
                )
            _validate_attempt_context(run, attempt_id)
            context = _validate_review_context(run, attempt_id)
            value = _validate_semantic_review_value(
                context,
                attempt_id,
                review,
                run_format_version=AGENT_FIRST_RUN_FORMAT_VERSION,
            )
            if review_path.exists() or review_path.is_symlink():
                if review_path.is_symlink() or not review_path.is_file():
                    raise PathSafetyError(
                        f"unsafe semantic review record: {review_path}"
                    )
                persisted = _read_json(review_path)
                if persisted != redact_secrets(value):
                    raise IntegrityError(
                        "refusing to overwrite an existing semantic review"
                    )
            else:
                atomic_write_json(review_path, redact_secrets(value))
            if fail_after_write:
                raise SimulatedCrash("after semantic QA write")
            _apply_semantic_review_state(
                run,
                value,
                run_format_version=AGENT_FIRST_RUN_FORMAT_VERSION,
                review_sha256=sha256_file(review_path),
            )
            return value
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
    if inspect_run_format(run) == AGENT_FIRST_RUN_FORMAT_VERSION:
        review_path = run / "attempts" / attempt_id / "qa" / "semantic-review.json"
        if state.get("semantic_review_sha256") != sha256_file(review_path):
            raise IntegrityError("semantic review hash disagrees with run state")
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


def _resume_run_v1(run_dir: Path | str, *, skill_root: Path | str) -> dict[str, Any]:
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


def _committed_curation_event_log_bindings(
    run: Path, state: Mapping[str, Any]
) -> list[dict[str, Any]]:
    active = state.get("active_curation_revision")
    active_hash = state.get("active_curation_sha256")
    if active is None:
        if active_hash is not None:
            raise IntegrityError("active curation pointer is incomplete")
        return []
    if (
        not isinstance(active, int)
        or isinstance(active, bool)
        or active < 1
        or not isinstance(active_hash, str)
    ):
        raise IntegrityError("active curation pointer is invalid")
    bindings: list[dict[str, Any]] = []
    previous_revision: int | None = None
    previous_hash: str | None = None
    for revision in range(1, active + 1):
        if not (run / "curations" / f"{revision:03d}").is_dir():
            raise IntegrityError("committed curation ancestry is not contiguous")
        values = _load_curation_revision(
            run,
            revision,
            require_committed_lineage=False,
        )
        manifest = values["manifest.json"]
        if (
            manifest["parent_revision"] != previous_revision
            or manifest["parent_catalog_sha256"] != previous_hash
        ):
            raise IntegrityError("committed curation ancestry is not contiguous")
        context = _load_source_review_context(
            run, state, manifest["source_review_context_path"]
        )
        binding = context["event_log_parent"]
        if not isinstance(binding, Mapping):
            raise IntegrityError("source review event log binding is invalid")
        bindings.append(dict(binding))
        previous_revision = revision
        previous_hash = manifest["catalog_sha256"]
    if previous_hash != active_hash:
        raise IntegrityError("active curation hash pointer is stale")
    return bindings


def _recover_prerequisite_bound_events(run: Path) -> None:
    state = _read_json(run / "run.json")
    parent_bindings = _committed_curation_event_log_bindings(run, state)

    active_plan = state.get("active_plan_revision")
    active_plan_hash = state.get("active_plan_sha256")
    if active_plan is None:
        if active_plan_hash is not None:
            raise IntegrityError("active plan pointer is incomplete")
    elif (
        not isinstance(active_plan, int)
        or isinstance(active_plan, bool)
        or active_plan < 1
        or not isinstance(active_plan_hash, str)
    ):
        raise IntegrityError("active plan pointer is invalid")
    else:
        previous_revision: int | None = None
        previous_hash: str | None = None
        for revision in range(1, active_plan + 1):
            if not (run / "plans" / f"{revision:03d}").is_dir():
                raise IntegrityError("committed plan ancestry is not contiguous")
            values = _load_plan_revision(
                run,
                revision,
                require_catalog_lineage=False,
            )
            manifest = values["manifest.json"]
            if (
                manifest["parent_revision"] != previous_revision
                or manifest["parent_plan_sha256"] != previous_hash
            ):
                raise IntegrityError("committed plan ancestry is not contiguous")
            _recover_bound_event_in_order(
                run,
                _plan_event(run, manifest),
                identity_fields=("operation_id", "revision"),
                parent_bindings=parent_bindings,
            )
            previous_revision = revision
            previous_hash = manifest["plan_sha256"]
        if previous_hash != active_plan_hash:
            raise IntegrityError("active plan hash pointer is stale")

    attempt_count = state.get("attempt_count")
    if (
        not isinstance(attempt_count, int)
        or isinstance(attempt_count, bool)
        or attempt_count < 0
    ):
        raise IntegrityError("attempt count is invalid")
    previous_attempt: str | None = None
    for number in range(1, attempt_count + 1):
        attempt_id = f"{number:02d}"
        if not (run / "attempts" / attempt_id).is_dir():
            raise IntegrityError("committed attempt ancestry is not contiguous")
        context = _validate_attempt_context(
            run,
            attempt_id,
            require_revision_lineage=False,
        )
        if context["parent_attempt"] != previous_attempt:
            raise IntegrityError("committed attempt ancestry is not contiguous")
        _recover_bound_event_in_order(
            run,
            _attempt_started_event(context),
            identity_fields=("operation_id", "attempt_id"),
            parent_bindings=parent_bindings,
        )
        previous_attempt = attempt_id

    for entry in _load_supersession_entries(run):
        event = {
            "event": "curation_reopened",
            "operation_id": entry["operation_id"],
            "attempt_id": entry["attempt_id"],
            "repair_route": entry["repair_route"],
        }
        _recover_bound_event_in_order(
            run,
            event,
            identity_fields=("operation_id", "attempt_id"),
            parent_bindings=parent_bindings,
        )


def _recover_curation_transactions(run: Path) -> None:
    state = _read_json(run / "run.json")
    _revisions, stages = _curation_registry(
        run, state, allow_incomplete_active_event=True
    )
    if stages:
        stage = stages[0]
        files, directories = _regular_tree_inventory(stage)
        expected_files = {
            "catalog.json",
            "review.json",
            "manifest.json",
            "COMMIT.json",
        }
        if files != expected_files or directories:
            if files.issubset(expected_files) and not directories:
                _remove_regular_tree(stage)
                stages = []
            else:
                raise IntegrityError("curation staging file set is conflicting")
        if stages:
            raw_manifest = _read_json(stage / "manifest.json")
            revision = raw_manifest.get("revision")
            if (
                not isinstance(revision, int)
                or isinstance(revision, bool)
                or revision < 1
            ):
                raise IntegrityError("curation staging revision is invalid")
            staged_values = _load_curation_revision(
                run,
                revision,
                directory=stage,
                require_committed_lineage=False,
            )
            manifest = staged_values["manifest.json"]
            active_revision = state.get("active_curation_revision")
            expected_revision = (
                active_revision + 1
                if isinstance(active_revision, int)
                and not isinstance(active_revision, bool)
                else 1
            )
            if (
                state.get("state") != "curating"
                or revision != expected_revision
                or manifest["parent_revision"] != active_revision
                or manifest["parent_catalog_sha256"]
                != state.get("active_curation_sha256")
                or manifest["source_manifest_sha256"]
                != state.get("source_manifest_sha256")
            ):
                raise IntegrityError("curation staging parent CAS mismatch")
            target = run / "curations" / f"{revision:03d}"
            if target.exists():
                if not _curation_documents_match(
                    run, revision, staged_values
                ):
                    raise IntegrityError("curation staging conflicts with its target")
                _remove_regular_tree(stage)
            else:
                os.replace(stage, target)
                _fsync_directory(target.parent)

    state = _read_json(run / "run.json")
    revisions, _stages = _curation_registry(
        run, state, allow_incomplete_active_event=True
    )
    active_revision = state.get("active_curation_revision")
    orphans = [
        revision
        for revision in revisions
        if active_revision is None or revision > active_revision
    ]
    if len(orphans) > 1:
        raise IntegrityError("curation registry contains multiple orphan revisions")
    if orphans:
        revision = orphans[0]
        values = _load_curation_revision(
            run, revision, require_committed_lineage=False
        )
        manifest = values["manifest.json"]
        if (
            state.get("state") != "curating"
            or manifest["parent_revision"] != active_revision
            or manifest["parent_catalog_sha256"]
            != state.get("active_curation_sha256")
            or manifest["source_manifest_sha256"]
            != state.get("source_manifest_sha256")
        ):
            raise IntegrityError("orphan curation revision parent CAS mismatch")
        state["active_curation_revision"] = revision
        state["active_curation_sha256"] = manifest["catalog_sha256"]
        state["state"] = "curated"
        _write_run(run, state)

    state = _read_json(run / "run.json")
    active_revision = state.get("active_curation_revision")
    if isinstance(active_revision, int) and not isinstance(active_revision, bool):
        values = _load_curation_revision(
            run, active_revision, allow_missing_pass_event=True
        )
        manifest = values["manifest.json"]
        context = _load_source_review_context(
            run, state, manifest["source_review_context_path"]
        )
        event = {
            "event": "source_review_passed",
            "operation_id": manifest["operation_id"],
            "revision": active_revision,
        }
        later_events = _source_review_jsonl_suffix(
            run,
            context["event_log_parent"],
            label="source review event log",
        )
        if not later_events:
            append_jsonl(run / "events.jsonl", event)
    _curation_registry(run, _read_json(run / "run.json"))


def _recover_plan_transactions(run: Path) -> None:
    state = _read_json(run / "run.json")
    _revisions, stages = _plan_registry(
        run, state, allow_missing_active_event=True
    )
    if stages:
        stage = stages[0]
        files, directories = _regular_tree_inventory(stage)
        expected_files = {"plan.json", "manifest.json", "COMMIT.json"}
        if files != expected_files or directories:
            if files.issubset(expected_files) and not directories:
                _remove_regular_tree(stage)
                stages = []
            else:
                raise IntegrityError("plan staging file set is conflicting")
        if stages:
            staged_values = _load_plan_revision(
                run, int(_read_json(stage / "manifest.json")["revision"]), directory=stage
            )
            manifest = staged_values["manifest.json"]
            active_revision = state.get("active_plan_revision")
            expected_revision = (
                int(active_revision) + 1
                if isinstance(active_revision, int)
                else 1
            )
            if (
                manifest["revision"] != expected_revision
                or manifest["parent_revision"] != active_revision
                or manifest["parent_plan_sha256"]
                != state.get("active_plan_sha256")
                or manifest["catalog_revision"]
                != state.get("active_curation_revision")
                or manifest["catalog_sha256"]
                != state.get("active_curation_sha256")
                or state.get("state") != "curated"
            ):
                raise IntegrityError("plan staging parent CAS mismatch")
            target = run / "plans" / f"{expected_revision:03d}"
            if target.exists():
                if not _plan_documents_match(
                    run, expected_revision, staged_values
                ):
                    raise IntegrityError("plan staging conflicts with its target")
                _remove_regular_tree(stage)
            else:
                os.replace(stage, target)
                _fsync_directory(target.parent)
    state = _read_json(run / "run.json")
    revisions, _stages = _plan_registry(
        run, state, allow_missing_active_event=True
    )
    active_revision = state.get("active_plan_revision")
    orphans = [
        revision
        for revision in revisions
        if active_revision is None or revision > active_revision
    ]
    if len(orphans) > 1:
        raise IntegrityError("plan registry contains multiple orphan revisions")
    if orphans:
        revision = orphans[0]
        manifest = _load_plan_revision(run, revision)["manifest.json"]
        if (
            state.get("state") != "curated"
            or manifest["parent_revision"] != active_revision
            or manifest["parent_plan_sha256"]
            != state.get("active_plan_sha256")
            or manifest["catalog_revision"]
            != state.get("active_curation_revision")
            or manifest["catalog_sha256"]
            != state.get("active_curation_sha256")
        ):
            raise IntegrityError("orphan plan revision parent CAS mismatch")
        state["active_plan_revision"] = revision
        state["active_plan_sha256"] = manifest["plan_sha256"]
        state["state"] = "planned"
        _write_run(run, state)
    state = _read_json(run / "run.json")
    revisions, _stages = _plan_registry(
        run, state, allow_missing_active_event=True
    )
    for revision in revisions:
        manifest = _load_plan_revision(run, revision)["manifest.json"]
        _ensure_bound_event(
            run,
            _plan_event(run, manifest),
            identity_fields=("operation_id", "revision"),
        )
    _plan_registry(run, _read_json(run / "run.json"))


def _recover_attempt_transactions(run: Path) -> None:
    attempts = run / "attempts"
    stages: list[Path] = []
    numeric: list[int] = []
    for path in attempts.iterdir():
        if path.is_symlink() or not path.is_dir():
            raise PathSafetyError(f"unsafe attempt registry entry: {path}")
        if re.fullmatch(r"[0-9]{2}", path.name):
            numeric.append(int(path.name))
        elif re.fullmatch(
            r"\.attempt-staging-[0-9]{2}-[0-9a-f]{24}", path.name
        ):
            stages.append(path)
        else:
            raise IntegrityError("attempt registry contains an unknown directory")
    numeric.sort()
    if len(stages) > 1:
        raise IntegrityError("attempt registry contains multiple staging transactions")
    expected_files = {
        "attempt-context.json",
        "catalog-snapshot.json",
        "plan-snapshot.json",
    }
    expected_directories = {"artifact", "qa", "qa/previews"}
    if stages:
        stage = stages[0]
        files, directories = _regular_tree_inventory(stage)
        if files != expected_files or directories != expected_directories:
            if files.issubset(expected_files) and directories.issubset(
                expected_directories
            ):
                _remove_regular_tree(stage)
            else:
                raise IntegrityError("attempt staging file set is conflicting")
        else:
            _begin_attempt_v2(run, fail_at=None)
    state = _read_json(run / "run.json")
    numeric = sorted(
        int(path.name)
        for path in attempts.iterdir()
        if path.is_dir() and re.fullmatch(r"[0-9]{2}", path.name)
    )
    count = state.get("attempt_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise IntegrityError("attempt count is invalid")
    if numeric not in (list(range(1, count + 1)), list(range(1, count + 2))):
        raise IntegrityError("attempt registry and attempt count disagree")
    if numeric == list(range(1, count + 2)):
        _begin_attempt_v2(run, fail_at=None)
        state = _read_json(run / "run.json")
    if state.get("state") == "authoring" and isinstance(
        state.get("active_attempt"), str
    ):
        _begin_attempt_v2(run, fail_at=None)
    numeric = sorted(
        int(path.name)
        for path in attempts.iterdir()
        if path.is_dir() and re.fullmatch(r"[0-9]{2}", path.name)
    )
    for number in numeric:
        context = _validate_attempt_context(run, f"{number:02d}")
        _ensure_attempt_started_event(run, context)


def _recover_reopen_transaction(run: Path) -> None:
    state = _read_json(run / "run.json")
    entries = _load_supersession_entries(run)
    pending_operation = state.get("pending_supersession_operation_id")
    entry: dict[str, Any] | None = None
    if pending_operation is not None:
        matches = [
            item for item in entries if item["operation_id"] == pending_operation
        ]
        if len(matches) != 1:
            raise IntegrityError("pending supersession operation is missing or duplicated")
        entry = matches[0]
        if state.get("pending_supersession_ledger") != _canonical_jsonl_binding(
            run, "provenance/supersessions.jsonl"
        ):
            raise IntegrityError("pending supersession ledger pointer is stale")
    elif (
        state.get("state") == "failed"
        and state.get("failure_origin") == "semantic_review"
        and isinstance(state.get("active_attempt"), str)
    ):
        matches = [
            item
            for item in entries
            if item["attempt_id"] == state["active_attempt"]
            and item["semantic_review_sha256"]
            == state.get("semantic_review_sha256")
            and item["repair_route"] == state.get("repair_route")
            and item["curation_revision"]
            == state.get("active_curation_revision")
            and item["plan_revision"] == state.get("active_plan_revision")
        ]
        if len(matches) > 1:
            raise IntegrityError("failed review has multiple supersession commits")
        if matches:
            entry = matches[0]
            review = _read_validated_semantic_review(
                run, str(state["active_attempt"])
            )
            if set(entry["finding_ids"]) != {
                finding["finding_id"] for finding in review["route_findings"]
            }:
                raise IntegrityError("supersession findings differ from semantic review")
            target_state = (
                "curated"
                if entry["repair_route"] == "content_replan"
                else "curating"
            )
            state["state"] = target_state
            state["pending_parent_attempt"] = entry["attempt_id"]
            state["pending_supersession_ledger"] = _canonical_jsonl_binding(
                run, "provenance/supersessions.jsonl"
            )
            state["pending_supersession_operation_id"] = entry["operation_id"]
            for field in ("reason", "resume_from", "failure_origin"):
                state.pop(field, None)
            _write_run(run, state)
    for committed in entries:
        event = {
            "event": "curation_reopened",
            "operation_id": committed["operation_id"],
            "attempt_id": committed["attempt_id"],
            "repair_route": committed["repair_route"],
        }
        _ensure_bound_event(
            run,
            event,
            identity_fields=("operation_id", "attempt_id"),
        )


def _recover_v2_task4_transactions(run: Path) -> None:
    _recover_prerequisite_bound_events(run)
    _recover_curation_transactions(run)
    _recover_plan_transactions(run)
    _recover_attempt_transactions(run)
    _recover_reopen_transaction(run)


def _resume_run_v2(run_dir: Path | str, *, skill_root: Path | str) -> dict[str, Any]:
    run, state = _load_agent_first_run(run_dir)
    verify_skill_snapshot(run, skill_root=skill_root)
    _verify_source_contract(run, state)
    _recover_v2_task4_transactions(run)
    run, state = _load_agent_first_run(run)
    _verify_source_contract(run, state)
    _curation_registry(run, state)
    _plan_registry(run, state)
    if (run / "final").exists():
        attempt_id = state.get("active_attempt")
        if not isinstance(attempt_id, str):
            raise IntegrityError("finalized run has no active attempt")
        _verify_attempt_delivery(run, state, attempt_id, run / "final")
        if state.get("state") != "finalized":
            state["state"] = "finalized"
            _write_run(run, state)
            _event(run, "crash_recovered", boundary="final_rename")
        return {**state, "next_action": "complete"}
    current = state.get("state")
    if current == "finalized":
        raise IntegrityError("finalized run is missing its final delivery")
    if current == "blocked":
        return {**state, "next_action": "resolve_blocker"}
    if current == "needs_visual_review":
        return {**state, "next_action": "resolve_blocker"}
    if current == "failed":
        attempt_id = state.get("active_attempt")
        if (
            state.get("failure_origin") is None
            and state.get("resume_from") == "deterministic_passed"
            and isinstance(attempt_id, str)
        ):
            semantic_path = (
                run / "attempts" / attempt_id / "qa" / "semantic-review.json"
            )
            if semantic_path.is_file():
                review = _read_validated_semantic_review(run, attempt_id)
                state = _repair_split_v2_semantic_failure(
                    run, state, attempt_id, review
                )
        if state.get("failure_origin") == "semantic_review":
            attempt_id = state.get("active_attempt")
            if not isinstance(attempt_id, str):
                raise IntegrityError("failed semantic review has no active attempt")
            review_path = run / "attempts" / attempt_id / "qa" / "semantic-review.json"
            review = _read_validated_semantic_review(run, attempt_id)
            route = state.get("repair_route")
            if (
                review["verdict"] != "fail"
                or review["repair_route"] != route
                or state.get("semantic_review_sha256")
                != sha256_file(review_path)
            ):
                raise IntegrityError(
                    "failed semantic review disagrees with persisted run state"
                )
            if route == "layout_repair":
                return {**state, "next_action": "author"}
            if route in {"content_replan", "source_reingest"}:
                return {**state, "next_action": "reopen_curation"}
            raise IntegrityError("failed semantic review has no valid repair route")
        return {**state, "next_action": "retry_current_attempt"}
    if current == "initialized":
        source = _read_json(run / "evidence" / "source_manifest.json")
        if source.get("status") != "not_prepared":
            raise IntegrityError("initialized v2 run has an inconsistent source state")
        return {**state, "next_action": "prepare_source"}
    if current == "curating":
        pending_reviews = []
        for path in sorted((run / "source-reviews").glob("*/context.json")):
            if not path.with_name("review.json").exists():
                pending_reviews.append(path)
        if pending_reviews:
            return {**state, "next_action": "source_review"}
        has_crops = any((run / "source-assets" / "receipts").iterdir())
        return {
            **state,
            "next_action": "curate_source" if has_crops else "inspect_source",
        }
    if current == "curated":
        return {**state, "next_action": "plan"}
    if current == "planned":
        return {**state, "next_action": "author"}
    attempt_id = state.get("active_attempt")
    if not isinstance(attempt_id, str):
        raise IntegrityError("active v2 state has no attempt ID")
    _validate_attempt_context(run, attempt_id)
    attempt = safe_path(run / "attempts", attempt_id, must_exist=True)
    active_stage = run / f".final.staging-{attempt_id}"
    if active_stage.is_dir() and (active_stage / "delivery-manifest.json").is_file():
        if current != "semantic_passed":
            raise IntegrityError("complete final staging exists in an invalid v2 state")
        _verify_attempt_delivery(run, state, attempt_id, active_stage)
        os.replace(active_stage, run / "final")
        state["state"] = "finalized"
        _write_run(run, state)
        _event(run, "crash_recovered", boundary="delivery_manifest_write")
        return {**state, "next_action": "complete"}
    deterministic_path = attempt / "qa" / "deterministic.json"
    if current == "authoring" and deterministic_path.is_file():
        report = _validate_deterministic_report(
            run, attempt_id, require_pass=False
        )
        if report["passed"]:
            state["state"] = "deterministic_passed"
            _write_run(run, state)
            _event(run, "crash_recovered", boundary="deterministic_qa_write")
            current = "deterministic_passed"
        else:
            state["state"] = "failed"
            state["reason"] = "deterministic checks failed"
            state["resume_from"] = "authoring"
            _write_run(run, state)
            _event(run, "crash_recovered", boundary="failed_deterministic_qa_write")
            return {**state, "next_action": "retry_current_attempt"}
    if current == "authoring":
        has_artifact = any(
            path.is_file() for path in (attempt / "artifact").rglob("*")
        )
        if not has_artifact:
            return {**state, "next_action": "author"}
        if not (attempt / "qa" / "dom-audit.json").is_file():
            return {**state, "next_action": "dom_audit"}
        return {**state, "next_action": "validate"}
    if current == "deterministic_passed":
        _validate_deterministic_report(run, attempt_id)
        semantic_path = attempt / "qa" / "semantic-review.json"
        if semantic_path.is_file():
            review = _read_validated_semantic_review(run, attempt_id)
            _apply_semantic_review_state(
                run,
                review,
                run_format_version=AGENT_FIRST_RUN_FORMAT_VERSION,
                review_sha256=sha256_file(semantic_path),
            )
            state = _read_json(run / "run.json")
            _event(run, "crash_recovered", boundary="semantic_qa_write")
            if state["state"] == "semantic_passed":
                return {**state, "next_action": "finalize"}
            return {
                **state,
                "next_action": (
                    "author"
                    if state.get("repair_route") == "layout_repair"
                    else "reopen_curation"
                ),
            }
        return {**state, "next_action": "semantic_review"}
    if current == "semantic_passed":
        review = _read_validated_semantic_review(run, attempt_id)
        review_path = run / "attempts" / attempt_id / "qa" / "semantic-review.json"
        if (
            review["verdict"] != "pass"
            or state.get("semantic_review_sha256") != sha256_file(review_path)
        ):
            raise IntegrityError(
                "passing semantic review disagrees with persisted run state"
            )
        return {**state, "next_action": "finalize"}
    raise IntegrityError(f"unknown v2 run state: {current}")


def resume_run(run_dir: Path | str, *, skill_root: Path | str) -> dict[str, Any]:
    """Verify durable contracts and name one format-stable next action."""

    if inspect_run_format(run_dir) == AGENT_FIRST_RUN_FORMAT_VERSION:
        run = Path(run_dir).absolute()
        with _agent_first_mutation_lock(run):
            return _resume_run_v2(run, skill_root=skill_root)
    return _resume_run_v1(run_dir, skill_root=skill_root)


__all__ = [
    "AGENT_FIRST_RUN_FORMAT_VERSION", "ContractError", "IntegrityError", "MAIN_STATES",
    "PathSafetyError", "PortableError", "RELEASED_RUN_FORMAT_VERSION", "REPAIR_ROUTE_ORDER",
    "SIDE_STATES", "SimulatedCrash", "StateError", "append_jsonl", "atomic_write_bytes",
    "atomic_write_json", "begin_attempt", "bind_host_vlm_visuals", "create_review_context",
    "crop_source", "crop_source_from_file", "diagnose_v1_run", "finalize_attempt",
    "initialize_run", "inspect_run_format", "inspect_source", "lexical_retrieve",
    "list_source_assets", "load_active_plan", "load_attempt_plan",
    "load_attempt_visual_catalog", "load_evidence", "mark_side_state",
    "prepare_source", "record_deterministic_result", "record_semantic_review", "redact_secrets",
    "reopen_curation", "resume_run", "safe_path", "save_plan", "save_plan_revision",
    "sha256_bytes", "sha256_file", "transition_state",
    "tree_hash", "validate_grounding", "validate_visual_plan", "verify_skill_snapshot",
    "write_source_map",
]
