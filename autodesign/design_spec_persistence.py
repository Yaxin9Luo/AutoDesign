"""Failure-atomic persistence for canonical DesignSpec revisions."""

from __future__ import annotations

import errno
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import threading
from typing import Any, BinaryIO, Callable, Iterable, Iterator, Literal, MutableMapping

from .util.io import atomic_write_json
from .util.design_spec_fingerprint import design_spec_sha256


StateSnapshot = dict[str, tuple[bool, Any]]
_RUNTIME_OS_NAME = os.name
_LOCK_OS_NAME = os.name
_WINDOWS_REPARSE_POINT = 0x00000400
_WINDOWS_DIRECTORY = 0x00000010
_CANONICAL_LOCKS: dict[str, threading.RLock] = {}
_CANONICAL_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class _ArchivePublication:
    device: int
    inode: int
    size: int
    modified_ns: int
    descriptor: int | None = None
    owned: bool = True


@dataclass(frozen=True)
class _PosixCanonicalNamespace:
    canonical_path: Path
    requested_canonical_path: Path
    directory: int
    parent_metadata: os.stat_result
    canonical_name: str
    lock_name: str
    lock_descriptor: int


@dataclass(frozen=True)
class _PosixDesignSpecNamespace:
    canonical: _PosixCanonicalNamespace
    archive_path: Path
    archive_directory: int
    archive_name: str


@dataclass(frozen=True)
class _CanonicalSnapshot:
    descriptor: int
    metadata: os.stat_result
    data: bytes


@dataclass(frozen=True)
class _CanonicalPublication:
    descriptor: int
    metadata: os.stat_result
    temporary_name: str


@dataclass(frozen=True)
class _WindowsArchiveMetadata:
    device: int
    inode: int
    size: int
    modified_ns: int
    links: int
    attributes: int


@dataclass(frozen=True)
class DesignSpecCommitResult:
    revision: int
    design_spec_sha256: str
    canonical_path: Path
    archive_path: Path


@dataclass(frozen=True)
class DesignSpecCanonicalState:
    revision: int
    design_spec_sha256: str
    design_spec: dict[str, Any]
    artifact_type: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class _ValidatedEnvelope:
    revision: int
    design_spec_sha256: str
    design_spec: dict[str, Any]
    artifact_type: str
    payload: dict[str, Any]
    parent_revision: int | None
    parent_design_spec_sha256: str | None
    has_parent_metadata: bool


@dataclass(frozen=True)
class _ArchiveRecord:
    revision: int
    data: bytes
    envelope: _ValidatedEnvelope | None


_ARCHIVE_NAME_PATTERN = re.compile(r"^design_spec_([0-9]+)\.json$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class _WindowsArchiveIO:
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _DELETE = 0x00010000
    _FILE_READ_ATTRIBUTES = 0x00000080
    _FILE_LIST_DIRECTORY = 0x00000001
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _OPEN_EXISTING = 3
    _OPEN_ALWAYS = 4
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_DISPOSITION_INFO_CLASS = 4
    _ERROR_INVALID_FUNCTION = 1
    _ERROR_INVALID_HANDLE = 6

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes
        import msvcrt

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.msvcrt = msvcrt
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class _FileDispositionInfo(ctypes.Structure):
            _fields_ = [("DeleteFile", ctypes.c_ubyte)]

        self._file_disposition_info = _FileDispositionInfo
        self.invalid_handle = ctypes.c_void_p(-1).value

        self.kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.kernel32.CreateFileW.restype = wintypes.HANDLE
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.SetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self.kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
        self.kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        self.kernel32.FlushFileBuffers.restype = wintypes.BOOL

    def _last_error(self, operation: str) -> OSError:
        code = self.ctypes.get_last_error()
        if code in {2, 3}:
            return FileNotFoundError(code, operation)
        return self.ctypes.WinError(code)

    def _open(
        self,
        path: Path,
        *,
        access: int,
        flags: int,
        share_access: int | None = None,
        creation_disposition: int | None = None,
    ) -> int:
        if share_access is None:
            share_access = (
                self._FILE_SHARE_READ
                | self._FILE_SHARE_WRITE
                | self._FILE_SHARE_DELETE
            )
        if creation_disposition is None:
            creation_disposition = self._OPEN_EXISTING
        handle = self.kernel32.CreateFileW(
            str(path),
            access,
            share_access,
            None,
            creation_disposition,
            flags,
            None,
        )
        value = getattr(handle, "value", handle)
        raw_handle = int(value) if value is not None else self.invalid_handle
        if raw_handle == self.invalid_handle:
            raise self._last_error(f"CreateFileW({path})")
        return raw_handle

    def open_file(self, path: Path, *, delete_access: bool = False) -> int:
        access = self._GENERIC_READ | self._FILE_READ_ATTRIBUTES
        if delete_access:
            access |= self._DELETE
        raw_handle = self._open(
            path,
            access=access,
            flags=self._FILE_FLAG_OPEN_REPARSE_POINT,
            share_access=(self._FILE_SHARE_READ if delete_access else None),
        )
        try:
            return self.msvcrt.open_osfhandle(
                raw_handle,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
        except BaseException:
            self.kernel32.CloseHandle(raw_handle)
            raise

    def open_lock(self, path: Path) -> int:
        raw_handle = self._open(
            path,
            access=(
                self._GENERIC_READ
                | self._GENERIC_WRITE
                | self._FILE_READ_ATTRIBUTES
            ),
            flags=self._FILE_FLAG_OPEN_REPARSE_POINT,
            share_access=(self._FILE_SHARE_READ | self._FILE_SHARE_WRITE),
            creation_disposition=self._OPEN_ALWAYS,
        )
        try:
            return self.msvcrt.open_osfhandle(
                raw_handle,
                os.O_RDWR | getattr(os, "O_BINARY", 0),
            )
        except BaseException:
            self.kernel32.CloseHandle(raw_handle)
            raise

    def lock(self, handle: int) -> None:
        if os.fstat(handle).st_size == 0:
            os.lseek(handle, 0, os.SEEK_SET)
            os.write(handle, b"\0")
            os.fsync(handle)
        os.lseek(handle, 0, os.SEEK_SET)
        self.msvcrt.locking(handle, self.msvcrt.LK_LOCK, 1)

    def unlock(self, handle: int) -> None:
        os.lseek(handle, 0, os.SEEK_SET)
        self.msvcrt.locking(handle, self.msvcrt.LK_UNLCK, 1)

    def metadata(self, handle: int) -> _WindowsArchiveMetadata:
        return _windows_metadata_from_stat(os.fstat(handle))

    def read_bytes(self, handle: int) -> bytes:
        os.lseek(handle, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while chunk := os.read(handle, 1 << 20):
            chunks.append(chunk)
        return b"".join(chunks)

    def stat_path(self, path: Path) -> _WindowsArchiveMetadata:
        return _windows_metadata_from_stat(os.lstat(path))

    def mark_delete(self, handle: int) -> None:
        info = self._file_disposition_info(DeleteFile=1)
        raw_handle = self.msvcrt.get_osfhandle(handle)
        if not self.kernel32.SetFileInformationByHandle(
            raw_handle,
            self._FILE_DISPOSITION_INFO_CLASS,
            self.ctypes.byref(info),
            self.ctypes.sizeof(info),
        ):
            raise self._last_error("SetFileInformationByHandle(delete)")

    def close(self, handle: int) -> None:
        os.close(handle)

    def flush_parent(self, parent: Path) -> None:
        raw_handle = self._open(
            parent,
            access=(
                self._GENERIC_READ
                | self._GENERIC_WRITE
                | self._FILE_LIST_DIRECTORY
                | self._FILE_READ_ATTRIBUTES
            ),
            flags=(
                self._FILE_FLAG_BACKUP_SEMANTICS
                | self._FILE_FLAG_OPEN_REPARSE_POINT
            ),
        )
        try:
            handle = self.msvcrt.open_osfhandle(raw_handle, os.O_RDONLY)
        except BaseException:
            self.kernel32.CloseHandle(raw_handle)
            raise
        try:
            metadata = _windows_metadata_from_stat(os.fstat(handle))
            if (
                metadata.attributes & _WINDOWS_REPARSE_POINT
                or not metadata.attributes & _WINDOWS_DIRECTORY
            ):
                raise OSError(errno.EPERM, "unsafe DesignSpec archive parent")
            if not self.kernel32.FlushFileBuffers(raw_handle):
                code = self.ctypes.get_last_error()
                if code not in {
                    self._ERROR_INVALID_FUNCTION,
                    self._ERROR_INVALID_HANDLE,
                }:
                    raise self._last_error("FlushFileBuffers(directory)")
        finally:
            self.close(handle)


def _windows_metadata_from_stat(metadata: os.stat_result) -> _WindowsArchiveMetadata:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    if stat.S_ISLNK(metadata.st_mode):
        attributes |= _WINDOWS_REPARSE_POINT
    if stat.S_ISDIR(metadata.st_mode):
        attributes |= _WINDOWS_DIRECTORY
    return _WindowsArchiveMetadata(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        links=metadata.st_nlink,
        attributes=attributes,
    )


def _windows_archive_io_factory() -> _WindowsArchiveIO:
    return _WindowsArchiveIO()


class DesignSpecPersistenceError(RuntimeError):
    """A DesignSpec commit failed before its caller-visible state was installed."""

    def __init__(self, phase: str, path: Path, cause: Exception) -> None:
        self.phase = phase
        self.path = path
        self.cause = cause
        super().__init__(
            f"DesignSpec {phase} persistence failed for {path}: "
            f"{type(cause).__name__}: {cause}"
        )


class _QuarantinedArchiveMismatch(OSError):
    def __init__(self, path: Path, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(
            getattr(errno, "ESTALE", errno.EIO),
            f"DesignSpec rollback moved a foreign entry: {detail}",
            os.fspath(path),
        )


class _QuarantinedEntryVerificationError(OSError):
    def __init__(
        self,
        path: Path,
        detail: str,
        verification_error: Exception,
        *,
        classification_error: Exception | None = None,
    ) -> None:
        self.path = path
        self.verification_error = verification_error
        self.classification_error = classification_error
        message = (
            f"DesignSpec rollback post-quarantine verification failed: {detail}; "
            "original verification error: "
            f"{type(verification_error).__name__}: {verification_error}"
        )
        if classification_error is not None:
            message += (
                "; identity classification error: "
                f"{type(classification_error).__name__}: {classification_error}"
            )
        super().__init__(
            getattr(errno, "ESTALE", errno.EIO),
            message,
            os.fspath(path),
        )


@dataclass(frozen=True)
class _QuarantinedEntryIdentity:
    state: Literal["owned", "mismatch", "ambiguous"]
    error: Exception | None = None


class _ArchivePublishRollbackError(OSError):
    def __init__(self, path: Path, cause: Exception) -> None:
        self.path = path
        self.cause = cause
        super().__init__(
            getattr(errno, "ESTALE", errno.EIO),
            "DesignSpec archive publication rollback failed",
            os.fspath(path),
        )


class _CanonicalReplaceRollbackError(OSError):
    def __init__(self, path: Path, cause: Exception) -> None:
        self.path = path
        self.cause = cause
        super().__init__(
            getattr(errno, "ESTALE", errno.EIO),
            "DesignSpec canonical replacement rollback failed",
            os.fspath(path),
        )


class _RollbackStepErrors(RuntimeError):
    def __init__(self, errors: list[Exception], *, default_path: Path) -> None:
        if not errors:
            raise ValueError("rollback error aggregation requires at least one error")
        self.errors = tuple(errors)
        self.path = _exception_path(errors[0], default_path)
        super().__init__(
            "; ".join(f"{type(error).__name__}: {error}" for error in errors)
        )


class _TransactionRollbackError(RuntimeError):
    def __init__(
        self,
        primary: Exception,
        rollback_errors: list[Exception],
        *,
        default_path: Path,
    ) -> None:
        if not rollback_errors:
            raise ValueError("transaction rollback requires at least one rollback error")
        self.primary = primary
        self.rollback_errors = tuple(rollback_errors)
        self.path = _exception_path(rollback_errors[0], default_path)
        rollback_detail = "; ".join(
            f"{type(error).__name__}: {error}" for error in rollback_errors
        )
        super().__init__(
            f"primary transaction error: {type(primary).__name__}: {primary}; "
            f"rollback errors: {rollback_detail}"
        )


def _exception_path(error: Exception, default: Path) -> Path:
    path = getattr(error, "path", None)
    if path is None and isinstance(error, OSError):
        path = error.filename
    return Path(path) if path is not None else default


def capture_state_keys(
    state: MutableMapping[str, Any],
    keys: Iterable[str],
    *,
    deep_copy_keys: Iterable[str] = (),
) -> StateSnapshot:
    """Capture exact key presence and values for a bounded state transaction."""

    from copy import deepcopy

    copied = set(deep_copy_keys)
    snapshot: StateSnapshot = {}
    for key in keys:
        if key not in state:
            snapshot[key] = (False, None)
            continue
        value = state[key]
        snapshot[key] = (True, deepcopy(value) if key in copied else value)
    return snapshot


def install_state_snapshot(
    state: MutableMapping[str, Any],
    snapshot: StateSnapshot,
) -> None:
    """Install a captured state while preserving absent-versus-present keys."""

    for key, (present, value) in snapshot.items():
        if present:
            state[key] = value
        else:
            state.pop(key, None)


def load_design_spec_canonical(
    canonical_path: Path,
) -> DesignSpecCanonicalState | None:
    """Load only the active, validated canonical DesignSpec revision."""

    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _canonical_transaction_lock(canonical_path) as locked:
            if isinstance(locked, _PosixCanonicalNamespace):
                snapshot = _capture_canonical_snapshot(locked)
                if snapshot is None:
                    return None
                try:
                    envelope = _validate_design_spec_envelope(snapshot.data)
                finally:
                    os.close(snapshot.descriptor)
            else:
                envelope, _data = _read_validated_canonical_windows(locked)
                if envelope is None:
                    return None
            return DesignSpecCanonicalState(
                revision=envelope.revision,
                design_spec_sha256=envelope.design_spec_sha256,
                design_spec=envelope.design_spec,
                artifact_type=envelope.artifact_type,
                payload=envelope.payload,
            )
    except DesignSpecPersistenceError:
        raise
    except Exception as exc:
        raise DesignSpecPersistenceError(
            "canonical_integrity",
            canonical_path,
            exc,
        ) from exc


def commit_design_spec_revision(
    *,
    canonical_path: Path,
    artifact_type: str,
    design_spec: dict[str, Any],
    is_revision: bool,
    expected_base_revision: int | None,
    expected_base_sha256: str | None,
    before_archive_publish: Callable[[Path], None] | None = None,
    phase_hook: Callable[[str], None] | None = None,
) -> DesignSpecCommitResult:
    """CAS-commit a DesignSpec and return the revision allocated under lock."""

    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    target_spec = dict(design_spec)
    target_hash = design_spec_sha256(target_spec)
    before_publish = before_archive_publish or (lambda _path: None)
    observe_phase = phase_hook or (lambda _phase: None)
    try:
        with _canonical_transaction_lock(canonical_path) as locked:
            namespace: _PosixDesignSpecNamespace | None = None
            try:
                bound_archive = _bind_archive_to_locked_canonical_parent(
                    canonical_path=canonical_path,
                    locked_canonical_path=locked,
                    archive_path=canonical_path.parent / "specs" / "design_spec_01.json",
                )
                if isinstance(locked, _PosixCanonicalNamespace):
                    if not isinstance(bound_archive, _PosixDesignSpecNamespace):
                        raise OSError(errno.EIO, "POSIX DesignSpec namespace binding failed")
                    namespace = bound_archive
                    current, current_bytes = _read_validated_canonical_locked(locked)
                    records = _scan_posix_archive_namespace(namespace)
                else:
                    if not isinstance(bound_archive, Path):
                        raise OSError(errno.EIO, "Windows DesignSpec namespace binding failed")
                    current, current_bytes = _read_validated_canonical_windows(locked)
                    records = _scan_windows_archive_namespace(bound_archive.parent)
            except Exception as exc:
                if isinstance(exc, DesignSpecPersistenceError):
                    raise
                raise DesignSpecPersistenceError(
                    "namespace_scan",
                    canonical_path.parent / "specs",
                    exc,
                ) from exc

            try:
                by_revision = {record.revision: record for record in records}

                if (
                    current is not None
                    and current.design_spec_sha256 == target_hash
                    and current.artifact_type == artifact_type
                ):
                    current_record = by_revision.get(current.revision)
                    if (
                        current_record is None
                        or current_record.envelope is None
                        or (
                            current_record.data != current_bytes
                            if isinstance(locked, _PosixCanonicalNamespace)
                            else current_record.envelope.payload != current.payload
                        )
                    ):
                        raise DesignSpecPersistenceError(
                            "canonical_integrity",
                            canonical_path,
                            OSError(
                                errno.EIO,
                                "active DesignSpec canonical has no identical immutable archive",
                                os.fspath(canonical_path),
                            ),
                        )
                    return DesignSpecCommitResult(
                        revision=current.revision,
                        design_spec_sha256=current.design_spec_sha256,
                        canonical_path=canonical_path,
                        archive_path=(
                            canonical_path.parent
                            / "specs"
                            / f"design_spec_{current.revision:02d}.json"
                        ),
                    )

                _require_expected_design_spec_base(
                    current=current,
                    expected_revision=expected_base_revision,
                    expected_sha256=expected_base_sha256,
                    canonical_path=canonical_path,
                )
                parent_revision = current.revision if current is not None else None
                parent_hash = (
                    current.design_spec_sha256 if current is not None else None
                )
                revision_flag = bool(is_revision or current is not None)
                occupied = {record.revision for record in records}
                allocation_floor = max(
                    occupied | ({parent_revision} if parent_revision is not None else {0})
                )
                selected_revision: int | None = None
                selected_payload: dict[str, Any] | None = None

                for record in sorted(records, key=lambda item: item.revision):
                    envelope = record.envelope
                    if (
                        record.revision <= (parent_revision or 0)
                        or envelope is None
                        or not envelope.has_parent_metadata
                        or envelope.parent_revision != parent_revision
                        or envelope.parent_design_spec_sha256 != parent_hash
                        or envelope.design_spec_sha256 != target_hash
                        or envelope.artifact_type != artifact_type
                    ):
                        continue
                    candidate = _design_spec_revision_payload(
                        artifact_type=artifact_type,
                        is_revision=revision_flag,
                        revision=record.revision,
                        design_spec_sha256_value=target_hash,
                        design_spec=target_spec,
                        parent_revision=parent_revision,
                        parent_design_spec_sha256=parent_hash,
                    )
                    if record.data == _encode_design_spec_payload(candidate):
                        selected_revision = record.revision
                        selected_payload = candidate
                        break

                if selected_revision is None:
                    selected_revision = allocation_floor + 1
                    selected_payload = _design_spec_revision_payload(
                        artifact_type=artifact_type,
                        is_revision=revision_flag,
                        revision=selected_revision,
                        design_spec_sha256_value=target_hash,
                        design_spec=target_spec,
                        parent_revision=parent_revision,
                        parent_design_spec_sha256=parent_hash,
                    )

                selected_archive = (
                    canonical_path.parent
                    / "specs"
                    / f"design_spec_{selected_revision:02d}.json"
                )
                selected_namespace: Path | _PosixDesignSpecNamespace
                if isinstance(locked, _PosixCanonicalNamespace):
                    assert namespace is not None
                    selected_namespace = _PosixDesignSpecNamespace(
                        canonical=locked,
                        archive_path=selected_archive,
                        archive_directory=namespace.archive_directory,
                        archive_name=selected_archive.name,
                    )
                else:
                    selected_namespace = selected_archive
                _persist_design_spec_payload_locked(
                    canonical_path=locked,
                    archive_path=selected_namespace,
                    payload=selected_payload,
                    before_archive_publish=before_publish,
                    phase_hook=observe_phase,
                )
                return DesignSpecCommitResult(
                    revision=selected_revision,
                    design_spec_sha256=target_hash,
                    canonical_path=canonical_path,
                    archive_path=selected_archive,
                )
            finally:
                if namespace is not None:
                    os.close(namespace.archive_directory)
    except DesignSpecPersistenceError:
        raise
    except Exception as exc:
        raise DesignSpecPersistenceError("canonical_lock", canonical_path, exc) from exc


def _read_validated_canonical_locked(
    namespace: _PosixCanonicalNamespace,
) -> tuple[_ValidatedEnvelope | None, bytes | None]:
    snapshot = _capture_canonical_snapshot(namespace)
    if snapshot is None:
        return None, None
    try:
        try:
            return _validate_design_spec_envelope(snapshot.data), snapshot.data
        except Exception as exc:
            raise DesignSpecPersistenceError(
                "canonical_integrity",
                namespace.canonical_path,
                exc,
            ) from exc
    finally:
        os.close(snapshot.descriptor)


def _read_windows_regular_file(path: Path) -> bytes | None:
    native = _windows_archive_io_factory()
    try:
        handle = native.open_file(path)
    except FileNotFoundError:
        return None
    try:
        opened = native.metadata(handle)
        if not _is_private_windows_file(opened):
            raise OSError(errno.EPERM, "unsafe DesignSpec file", os.fspath(path))
        data = native.read_bytes(handle)
        current = native.stat_path(path)
        if (
            not _is_private_windows_file(current)
            or not _same_windows_file(opened, current)
        ):
            raise OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "DesignSpec file changed during retained read",
                os.fspath(path),
            )
        return data
    finally:
        native.close(handle)


def _read_validated_canonical_windows(
    canonical_path: Path,
) -> tuple[_ValidatedEnvelope | None, bytes | None]:
    data = _read_windows_regular_file(canonical_path)
    if data is None:
        return None, None
    try:
        return _validate_design_spec_envelope(data), data
    except Exception as exc:
        raise DesignSpecPersistenceError(
            "canonical_integrity",
            canonical_path,
            exc,
        ) from exc


def _scan_windows_archive_namespace(archive_parent: Path) -> list[_ArchiveRecord]:
    records: list[_ArchiveRecord] = []
    native = _windows_archive_io_factory()
    try:
        parent_metadata = native.stat_path(archive_parent)
        if (
            parent_metadata.attributes & _WINDOWS_REPARSE_POINT
            or not parent_metadata.attributes & _WINDOWS_DIRECTORY
        ):
            raise OSError(
                errno.EPERM,
                "unsafe DesignSpec archive parent",
                os.fspath(archive_parent),
            )
        seen_revisions: set[int] = set()
        for path in archive_parent.iterdir():
            match = _ARCHIVE_NAME_PATTERN.fullmatch(path.name)
            if match is None:
                continue
            revision = int(match.group(1))
            if revision in seen_revisions:
                raise OSError(
                    errno.EEXIST,
                    "duplicate numeric DesignSpec archive revision",
                    path.name,
                )
            seen_revisions.add(revision)
            data = _read_windows_regular_file(path)
            if data is None:
                raise OSError(
                    getattr(errno, "ESTALE", errno.EIO),
                    "DesignSpec archive disappeared during namespace scan",
                    os.fspath(path),
                )
            try:
                parsed = _validate_design_spec_envelope(data)
                envelope = parsed if parsed.revision == revision else None
            except Exception:
                envelope = None
            records.append(
                _ArchiveRecord(revision=revision, data=data, envelope=envelope)
            )
        return records
    except DesignSpecPersistenceError:
        raise
    except Exception as exc:
        raise DesignSpecPersistenceError("namespace_scan", archive_parent, exc) from exc


def _scan_posix_archive_namespace(
    namespace: _PosixDesignSpecNamespace,
) -> list[_ArchiveRecord]:
    records: list[_ArchiveRecord] = []
    try:
        _validate_posix_design_spec_namespace(namespace)
        names = os.listdir(namespace.archive_directory)
        seen_revisions: set[int] = set()
        for name in names:
            match = _ARCHIVE_NAME_PATTERN.fullmatch(name)
            if match is None:
                continue
            revision = int(match.group(1))
            if revision in seen_revisions:
                raise OSError(
                    errno.EEXIST,
                    "duplicate numeric DesignSpec archive revision",
                    name,
                )
            seen_revisions.add(revision)
            descriptor = _open_regular_file_at(
                namespace.archive_directory,
                name,
                allow_missing=False,
            )
            assert descriptor is not None
            try:
                metadata, data = _read_stable_descriptor(
                    descriptor,
                    allowed_links=frozenset({1}),
                )
                current = os.stat(
                    name,
                    dir_fd=namespace.archive_directory,
                    follow_symlinks=False,
                )
                if not _same_posix_file(metadata, current):
                    raise OSError(
                        getattr(errno, "ESTALE", errno.EIO),
                        "DesignSpec archive changed during namespace scan",
                        name,
                    )
            finally:
                os.close(descriptor)
            envelope: _ValidatedEnvelope | None
            try:
                parsed = _validate_design_spec_envelope(data)
                envelope = parsed if parsed.revision == revision else None
            except Exception:
                envelope = None
            records.append(
                _ArchiveRecord(revision=revision, data=data, envelope=envelope)
            )
        return records
    except DesignSpecPersistenceError:
        raise
    except Exception as exc:
        raise DesignSpecPersistenceError(
            "namespace_scan",
            namespace.archive_path.parent,
            exc,
        ) from exc


def _validate_design_spec_envelope(data: bytes) -> _ValidatedEnvelope:
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("DesignSpec envelope must be a JSON object")
    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise ValueError("DesignSpec revision must be a positive integer")
    spec = payload.get("design_spec")
    if not isinstance(spec, dict):
        raise ValueError("DesignSpec envelope is missing an object design_spec")
    spec_hash = payload.get("design_spec_sha256")
    if not isinstance(spec_hash, str) or design_spec_sha256(spec) != spec_hash:
        raise ValueError("DesignSpec envelope hash does not match its design_spec")
    artifact_type = payload.get("artifact_type")
    if not isinstance(artifact_type, str) or not artifact_type:
        raise ValueError("DesignSpec envelope is missing artifact_type")
    if spec.get("artifact_type") != artifact_type:
        raise ValueError("DesignSpec envelope artifact_type does not match design_spec")
    if not isinstance(payload.get("is_revision"), bool):
        raise ValueError("DesignSpec envelope is missing boolean is_revision")
    has_parent_revision = "parent_revision" in payload
    has_parent_hash = "parent_design_spec_sha256" in payload
    if has_parent_revision != has_parent_hash:
        raise ValueError("DesignSpec parent revision/hash metadata must be paired")
    parent_revision = payload.get("parent_revision") if has_parent_revision else None
    parent_hash = (
        payload.get("parent_design_spec_sha256") if has_parent_hash else None
    )
    if parent_revision is not None and (
        isinstance(parent_revision, bool)
        or not isinstance(parent_revision, int)
        or parent_revision <= 0
        or parent_revision >= revision
    ):
        raise ValueError("DesignSpec parent revision is invalid")
    if parent_hash is not None and (
        not isinstance(parent_hash, str)
        or _SHA256_PATTERN.fullmatch(parent_hash) is None
    ):
        raise ValueError("DesignSpec parent hash is invalid")
    if (parent_revision is None) != (parent_hash is None):
        raise ValueError("DesignSpec parent revision/hash values must both be null or set")
    return _ValidatedEnvelope(
        revision=revision,
        design_spec_sha256=spec_hash,
        design_spec=spec,
        artifact_type=artifact_type,
        payload=payload,
        parent_revision=parent_revision,
        parent_design_spec_sha256=parent_hash,
        has_parent_metadata=has_parent_revision,
    )


def _require_expected_design_spec_base(
    *,
    current: _ValidatedEnvelope | None,
    expected_revision: int | None,
    expected_sha256: str | None,
    canonical_path: Path,
) -> None:
    if isinstance(expected_revision, bool):
        normalized_revision = -1
    else:
        normalized_revision = int(expected_revision or 0)
    current_revision = current.revision if current is not None else 0
    current_hash = current.design_spec_sha256 if current is not None else None
    if normalized_revision == current_revision and expected_sha256 == current_hash:
        return
    raise DesignSpecPersistenceError(
        "cas",
        canonical_path,
        OSError(
            getattr(errno, "ESTALE", errno.EIO),
            "DesignSpec canonical base changed before commit",
            os.fspath(canonical_path),
        ),
    )


def _design_spec_revision_payload(
    *,
    artifact_type: str,
    is_revision: bool,
    revision: int,
    design_spec_sha256_value: str,
    design_spec: dict[str, Any],
    parent_revision: int | None,
    parent_design_spec_sha256: str | None,
) -> dict[str, Any]:
    return {
        "artifact_type": artifact_type,
        "is_revision": is_revision,
        "revision": revision,
        "parent_revision": parent_revision,
        "parent_design_spec_sha256": parent_design_spec_sha256,
        "design_spec_sha256": design_spec_sha256_value,
        "design_spec": design_spec,
    }


def _encode_design_spec_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def persist_design_spec_payload(
    *,
    canonical_path: Path,
    archive_path: Path,
    payload: dict[str, Any],
    before_archive_publish: Callable[[Path], None],
) -> None:
    """Publish an immutable archive first, then replace the canonical snapshot."""

    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _canonical_transaction_lock(canonical_path) as locked_canonical_path:
            locked_archive_path = _bind_archive_to_locked_canonical_parent(
                canonical_path=canonical_path,
                locked_canonical_path=locked_canonical_path,
                archive_path=archive_path,
            )
            try:
                _persist_design_spec_payload_locked(
                    canonical_path=locked_canonical_path,
                    archive_path=locked_archive_path,
                    payload=payload,
                    before_archive_publish=before_archive_publish,
                )
            finally:
                if isinstance(locked_archive_path, _PosixDesignSpecNamespace):
                    os.close(locked_archive_path.archive_directory)
    except DesignSpecPersistenceError:
        raise
    except Exception as exc:
        raise DesignSpecPersistenceError(
            "canonical_lock",
            canonical_path,
            exc,
        ) from exc


def _bind_archive_to_locked_canonical_parent(
    *,
    canonical_path: Path,
    locked_canonical_path: Path | _PosixCanonicalNamespace,
    archive_path: Path,
) -> Path | _PosixDesignSpecNamespace:
    lexical_parent = Path(os.path.abspath(os.fspath(canonical_path.parent)))
    lexical_archive = Path(os.path.abspath(os.fspath(archive_path)))
    try:
        relative_archive = lexical_archive.relative_to(lexical_parent)
    except ValueError as exc:
        raise OSError(
            errno.EPERM,
            "DesignSpec archive escapes the canonical parent",
            os.fspath(archive_path),
        ) from exc
    if (
        len(relative_archive.parts) != 2
        or relative_archive.parts[0] != "specs"
        or relative_archive.name in {"", ".", ".."}
    ):
        raise OSError(
            errno.EPERM,
            "DesignSpec archive is outside the expected specs subtree",
            os.fspath(archive_path),
        )

    if isinstance(locked_canonical_path, _PosixCanonicalNamespace):
        _require_posix_no_replace_rename()
        created_archive_directory = False
        try:
            os.mkdir("specs", mode=0o700, dir_fd=locked_canonical_path.directory)
            created_archive_directory = True
        except FileExistsError:
            pass
        if created_archive_directory:
            os.fsync(locked_canonical_path.directory)
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        archive_directory = os.open(
            "specs",
            directory_flags,
            dir_fd=locked_canonical_path.directory,
        )
        try:
            parent_metadata = os.fstat(archive_directory)
            if not _is_safe_directory(parent_metadata):
                raise OSError(
                    errno.EPERM,
                    "unsafe DesignSpec archive parent",
                    os.fspath(archive_path.parent),
                )
            return _PosixDesignSpecNamespace(
                canonical=locked_canonical_path,
                archive_path=lexical_archive,
                archive_directory=archive_directory,
                archive_name=relative_archive.name,
            )
        except BaseException:
            os.close(archive_directory)
            raise

    locked_archive = locked_canonical_path.parent / relative_archive
    try:
        locked_archive.parent.mkdir(mode=0o700)
    except FileExistsError:
        pass
    parent_metadata = os.lstat(locked_archive.parent)
    if not _is_safe_directory(parent_metadata):
        raise OSError(
            errno.EPERM,
            "unsafe DesignSpec archive parent",
            os.fspath(locked_archive.parent),
        )
    return locked_archive


def _persist_design_spec_payload_locked(
    *,
    canonical_path: Path | _PosixCanonicalNamespace,
    archive_path: Path | _PosixDesignSpecNamespace,
    payload: dict[str, Any],
    before_archive_publish: Callable[[Path], None],
    phase_hook: Callable[[str], None] | None = None,
) -> None:
    if (
        isinstance(canonical_path, _PosixCanonicalNamespace)
        and isinstance(archive_path, _PosixDesignSpecNamespace)
        and _RUNTIME_OS_NAME != "nt"
    ):
        _persist_design_spec_payload_posix(
            namespace=archive_path,
            payload=payload,
            before_archive_publish=before_archive_publish,
            phase_hook=phase_hook,
        )
        return
    if not isinstance(canonical_path, Path) or not isinstance(archive_path, Path):
        canonical_path = canonical_path.canonical_path
        archive_path = archive_path.archive_path
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    canonical_encoded = (
        encoded.replace(b"\n", b"\r\n")
        if _RUNTIME_OS_NAME == "nt"
        else encoded
    )
    try:
        publication = _publish_archive_no_replace(
            archive_path,
            encoded,
            before_publish=before_archive_publish,
        )
    except _ArchivePublishRollbackError as exc:
        raise DesignSpecPersistenceError(
            "archive_rollback",
            exc.path,
            exc.cause,
        ) from exc
    except Exception as exc:
        raise DesignSpecPersistenceError("archive", archive_path, exc) from exc

    try:
        atomic_write_json(canonical_path, payload)
    except Exception as exc:
        if _canonical_payload_is_installed(canonical_path, canonical_encoded):
            return
        if publication is not None:
            try:
                released = _release_owned_archive_if_unchanged(
                    archive_path,
                    encoded,
                    publication,
                )
                if not released:
                    raise OSError(
                        getattr(errno, "ESTALE", errno.EIO),
                        "DesignSpec archive rollback lost ownership",
                        os.fspath(archive_path),
                    )
            except Exception as cleanup_exc:
                cleanup_path = getattr(cleanup_exc, "path", archive_path)
                raise DesignSpecPersistenceError(
                    "canonical_rollback",
                    cleanup_path,
                    cleanup_exc,
                ) from exc
        raise DesignSpecPersistenceError("canonical", canonical_path, exc) from exc


def _persist_design_spec_payload_posix(
    *,
    namespace: _PosixDesignSpecNamespace,
    payload: dict[str, Any],
    before_archive_publish: Callable[[Path], None],
    phase_hook: Callable[[str], None] | None,
) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    snapshot: _CanonicalSnapshot | None = None
    archive: _ArchivePublication | None = None
    canonical: _CanonicalPublication | None = None
    try:
        _validate_posix_design_spec_namespace(namespace)
        snapshot = _capture_canonical_snapshot(namespace.canonical)
        try:
            archive = _publish_archive_no_replace_posix(
                namespace,
                encoded,
                before_publish=before_archive_publish,
            )
            if phase_hook is not None:
                try:
                    phase_hook("after_archive_fsync")
                except Exception as exc:
                    if archive.owned:
                        try:
                            if not _quarantine_archive_publication(
                                namespace,
                                archive,
                                encoded,
                            ):
                                raise OSError(
                                    getattr(errno, "ESTALE", errno.EIO),
                                    "DesignSpec archive phase-hook rollback lost ownership",
                                    os.fspath(namespace.archive_path),
                                )
                        except Exception as rollback_exc:
                            raise _ArchivePublishRollbackError(
                                getattr(rollback_exc, "path", namespace.archive_path),
                                rollback_exc,
                            ) from exc
                    raise
        except _ArchivePublishRollbackError as exc:
            raise DesignSpecPersistenceError(
                "archive_rollback",
                exc.path,
                exc.cause,
            ) from exc
        except Exception as exc:
            raise DesignSpecPersistenceError(
                "archive",
                namespace.archive_path,
                exc,
            ) from exc

        try:
            _validate_posix_design_spec_namespace(namespace)
            canonical = _replace_canonical_posix(
                namespace.canonical,
                encoded,
                expected=snapshot,
                phase_hook=phase_hook,
            )
        except Exception as exc:
            rollback_errors: list[Exception] = []
            if archive.owned:
                try:
                    if not _quarantine_archive_publication(namespace, archive, encoded):
                        raise OSError(
                            getattr(errno, "ESTALE", errno.EIO),
                            "DesignSpec archive rollback lost ownership",
                            os.fspath(namespace.archive_path),
                        )
                except Exception as cleanup_exc:
                    rollback_errors.append(cleanup_exc)
            if isinstance(exc, _CanonicalReplaceRollbackError):
                primary = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
                rollback_errors.insert(0, exc.cause)
                aggregate = _TransactionRollbackError(
                    primary,
                    rollback_errors,
                    default_path=namespace.canonical.canonical_path,
                )
                raise DesignSpecPersistenceError(
                    "canonical_rollback",
                    aggregate.path,
                    aggregate,
                ) from exc
            if rollback_errors:
                aggregate = _TransactionRollbackError(
                    exc,
                    rollback_errors,
                    default_path=namespace.archive_path,
                )
                raise DesignSpecPersistenceError(
                    "canonical_rollback",
                    aggregate.path,
                    aggregate,
                ) from exc
            raise DesignSpecPersistenceError(
                "canonical",
                namespace.canonical.canonical_path,
                exc,
            ) from exc

        try:
            _validate_posix_design_spec_namespace(namespace)
            _verify_posix_entry(
                directory=namespace.archive_directory,
                name=namespace.archive_name,
                descriptor=archive.descriptor,
                publication=archive,
                data=encoded,
            )
            _verify_posix_entry(
                directory=namespace.canonical.directory,
                name=namespace.canonical.canonical_name,
                descriptor=canonical.descriptor,
                publication=_publication_from_stat(
                    canonical.metadata,
                    descriptor=canonical.descriptor,
                ),
                data=encoded,
            )
            os.fsync(namespace.archive_directory)
            os.fsync(namespace.canonical.directory)
            _validate_posix_design_spec_namespace(namespace)
            if phase_hook is not None:
                phase_hook("after_both_directory_fsyncs")
        except Exception as exc:
            rollback_errors = []
            try:
                _rollback_canonical_posix(
                    namespace.canonical,
                    snapshot=snapshot,
                    publication=canonical,
                    data=encoded,
                )
            except Exception as cleanup_exc:
                rollback_errors.append(cleanup_exc)
            if archive.owned:
                try:
                    if not _quarantine_archive_publication(namespace, archive, encoded):
                        raise OSError(
                            getattr(errno, "ESTALE", errno.EIO),
                            "DesignSpec archive rollback lost ownership",
                            os.fspath(namespace.archive_path),
                        )
                except Exception as cleanup_exc:
                    rollback_errors.append(cleanup_exc)
            if rollback_errors:
                aggregate = _TransactionRollbackError(
                    exc,
                    rollback_errors,
                    default_path=namespace.archive_path,
                )
                raise DesignSpecPersistenceError(
                    "canonical_rollback",
                    aggregate.path,
                    aggregate,
                ) from exc
            raise DesignSpecPersistenceError(
                "canonical",
                namespace.canonical.canonical_path,
                exc,
            ) from exc
    finally:
        if canonical is not None:
            os.close(canonical.descriptor)
        if archive is not None and archive.descriptor is not None:
            os.close(archive.descriptor)
        if snapshot is not None:
            os.close(snapshot.descriptor)


def _validate_posix_retained_namespace(
    namespace: _PosixCanonicalNamespace,
) -> None:
    opened_parent = os.fstat(namespace.directory)
    if (
        not _is_safe_directory(opened_parent)
        or (opened_parent.st_dev, opened_parent.st_ino)
        != (namespace.parent_metadata.st_dev, namespace.parent_metadata.st_ino)
    ):
        raise OSError(
            getattr(errno, "ESTALE", errno.EIO),
            "DesignSpec retained canonical parent changed identity",
            os.fspath(namespace.requested_canonical_path.parent),
        )
    _validate_canonical_parent_binding(
        namespace.requested_canonical_path,
        namespace.canonical_path,
        namespace.parent_metadata,
    )
    opened_lock = os.fstat(namespace.lock_descriptor)
    current_lock = os.stat(
        namespace.lock_name,
        dir_fd=namespace.directory,
        follow_symlinks=False,
    )
    if (
        not _is_private_regular_file(opened_lock)
        or not _is_private_regular_file(current_lock)
        or (opened_lock.st_dev, opened_lock.st_ino)
        != (current_lock.st_dev, current_lock.st_ino)
    ):
        raise OSError(
            errno.EPERM,
            "unsafe DesignSpec canonical lock file",
            os.fspath(namespace.canonical_path.with_name(namespace.lock_name)),
        )
    _validate_posix_canonical_entry(namespace.directory, namespace.canonical_path)


def _validate_posix_design_spec_namespace(
    namespace: _PosixDesignSpecNamespace,
) -> None:
    _validate_posix_retained_namespace(namespace.canonical)
    opened_archive_parent = os.fstat(namespace.archive_directory)
    try:
        current_archive_parent = os.stat(
            "specs",
            dir_fd=namespace.canonical.directory,
            follow_symlinks=False,
        )
    except FileNotFoundError as exc:
        raise OSError(
            getattr(errno, "ESTALE", errno.EIO),
            "DesignSpec archive directory disappeared from the requested namespace",
            os.fspath(namespace.archive_path.parent),
        ) from exc
    if (
        not _is_safe_directory(opened_archive_parent)
        or not _is_safe_directory(current_archive_parent)
        or (opened_archive_parent.st_dev, opened_archive_parent.st_ino)
        != (current_archive_parent.st_dev, current_archive_parent.st_ino)
    ):
        raise OSError(
            getattr(errno, "ESTALE", errno.EIO),
            "DesignSpec archive directory changed in the requested namespace",
            os.fspath(namespace.archive_path.parent),
        )


def _capture_canonical_snapshot(
    namespace: _PosixCanonicalNamespace,
) -> _CanonicalSnapshot | None:
    descriptor = _open_regular_file_at(
        namespace.directory,
        namespace.canonical_name,
        allow_missing=True,
    )
    if descriptor is None:
        return None
    try:
        metadata, data = _read_stable_descriptor(
            descriptor,
            allowed_links=frozenset({1}),
        )
        current = os.stat(
            namespace.canonical_name,
            dir_fd=namespace.directory,
            follow_symlinks=False,
        )
        if not _same_posix_file(metadata, current):
            raise OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "DesignSpec canonical changed while capturing rollback state",
                os.fspath(namespace.canonical_path),
            )
        return _CanonicalSnapshot(
            descriptor=descriptor,
            metadata=metadata,
            data=data,
        )
    except BaseException:
        os.close(descriptor)
        raise


def _publish_archive_no_replace_posix(
    namespace: _PosixDesignSpecNamespace,
    data: bytes,
    *,
    before_publish: Callable[[Path], None],
) -> _ArchivePublication:
    existing = _open_regular_file_at(
        namespace.archive_directory,
        namespace.archive_name,
        allow_missing=True,
    )
    if existing is not None:
        try:
            existing_metadata, existing_data = _read_stable_descriptor(
                existing,
                allowed_links=frozenset({1}),
            )
            current = os.stat(
                namespace.archive_name,
                dir_fd=namespace.archive_directory,
                follow_symlinks=False,
            )
            if existing_data != data or not _same_posix_file(existing_metadata, current):
                raise FileExistsError(
                    errno.EEXIST,
                    "immutable DesignSpec revision archive conflicts with existing bytes",
                    os.fspath(namespace.archive_path),
                )
            return _publication_from_stat(
                existing_metadata,
                descriptor=existing,
                owned=False,
            )
        except BaseException:
            os.close(existing)
            raise

    descriptor, temporary_name = _create_private_temp_at(
        namespace.archive_directory,
        prefix=f".{namespace.archive_name}.",
        suffix=".tmp",
    )
    keep_descriptor = False
    publication: _ArchivePublication | None = None
    try:
        _write_descriptor(descriptor, data)
        metadata = os.fstat(descriptor)
        before_publish(namespace.archive_path)
        _validate_posix_design_spec_namespace(namespace)
        _require_path_matches_descriptor(
            namespace.archive_directory,
            temporary_name,
            descriptor,
            allowed_links=frozenset({1}),
        )
        try:
            _rename_no_replace_at(
                src_dir_fd=namespace.archive_directory,
                src_name=temporary_name,
                dst_dir_fd=namespace.archive_directory,
                dst_name=namespace.archive_name,
            )
        except OSError as exc:
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
            existing = _open_regular_file_at(
                namespace.archive_directory,
                namespace.archive_name,
                allow_missing=False,
            )
            assert existing is not None
            try:
                existing_metadata, existing_data = _read_stable_descriptor(
                    existing,
                    allowed_links=frozenset({1}),
                )
                current = os.stat(
                    namespace.archive_name,
                    dir_fd=namespace.archive_directory,
                    follow_symlinks=False,
                )
                if (
                    existing_data != data
                    or not _same_posix_file(existing_metadata, current)
                ):
                    raise FileExistsError(
                        errno.EEXIST,
                        "immutable DesignSpec revision archive conflicts with existing bytes",
                        os.fspath(namespace.archive_path),
                    ) from exc
                return _publication_from_stat(
                    existing_metadata,
                    descriptor=existing,
                    owned=False,
                )
            except BaseException:
                os.close(existing)
                raise

        publication = _publication_from_stat(
            metadata,
            descriptor=descriptor,
        )
        try:
            _verify_posix_entry(
                directory=namespace.archive_directory,
                name=namespace.archive_name,
                descriptor=descriptor,
                publication=publication,
                data=data,
            )
            os.fsync(namespace.archive_directory)
        except BaseException as exc:
            try:
                if not _quarantine_archive_publication(namespace, publication, data):
                    raise OSError(
                        getattr(errno, "ESTALE", errno.EIO),
                        "DesignSpec archive publication rollback lost ownership",
                        os.fspath(namespace.archive_path),
                    )
            except Exception as rollback_exc:
                raise _ArchivePublishRollbackError(
                    getattr(rollback_exc, "path", namespace.archive_path),
                    rollback_exc,
                ) from exc
            raise
        keep_descriptor = True
        return publication
    finally:
        if not keep_descriptor:
            if publication is None:
                try:
                    _quarantine_owned_temp_at(
                        directory=namespace.archive_directory,
                        name=temporary_name,
                        descriptor=descriptor,
                    )
                except OSError:
                    pass
            os.close(descriptor)


def _replace_canonical_posix(
    namespace: _PosixCanonicalNamespace,
    data: bytes,
    *,
    expected: _CanonicalSnapshot | None,
    phase_hook: Callable[[str], None] | None = None,
) -> _CanonicalPublication:
    descriptor, temporary_name = _create_private_temp_at(
        namespace.directory,
        prefix=f".{namespace.canonical_name}.",
        suffix=".tmp",
    )
    installed = False
    try:
        _write_descriptor(descriptor, data)
        metadata = os.fstat(descriptor)
        _require_path_matches_descriptor(
            namespace.directory,
            temporary_name,
            descriptor,
            allowed_links=frozenset({1}),
        )
        _require_expected_canonical(namespace, expected)
        try:
            os.replace(
                temporary_name,
                namespace.canonical_name,
                src_dir_fd=namespace.directory,
                dst_dir_fd=namespace.directory,
            )
        except Exception as exc:
            if not _entry_matches_descriptor_and_data(
                directory=namespace.directory,
                name=namespace.canonical_name,
                descriptor=descriptor,
                data=data,
            ):
                if _expected_canonical_is_current(namespace, expected):
                    raise
                try:
                    _restore_canonical_after_ambiguous_replace(
                        namespace,
                        snapshot=expected,
                    )
                except Exception as rollback_exc:
                    raise _CanonicalReplaceRollbackError(
                        namespace.canonical_path,
                        rollback_exc,
                    ) from exc
                raise
        publication = _CanonicalPublication(
            descriptor=descriptor,
            metadata=metadata,
            temporary_name=temporary_name,
        )
        try:
            if phase_hook is not None:
                phase_hook("after_canonical_replace")
            _verify_posix_entry(
                directory=namespace.directory,
                name=namespace.canonical_name,
                descriptor=descriptor,
                publication=_publication_from_stat(
                    metadata,
                    descriptor=descriptor,
                ),
                data=data,
            )
            os.fsync(namespace.directory)
        except Exception as exc:
            if _expected_canonical_is_current(namespace, expected):
                raise
            try:
                _restore_canonical_after_ambiguous_replace(
                    namespace,
                    snapshot=expected,
                )
            except Exception as rollback_exc:
                raise _CanonicalReplaceRollbackError(
                    namespace.canonical_path,
                    rollback_exc,
                ) from exc
            raise
        installed = True
        return publication
    finally:
        if not installed:
            try:
                _quarantine_owned_temp_at(
                    directory=namespace.directory,
                    name=temporary_name,
                    descriptor=descriptor,
                )
            finally:
                os.close(descriptor)


def _expected_canonical_is_current(
    namespace: _PosixCanonicalNamespace,
    expected: _CanonicalSnapshot | None,
) -> bool:
    try:
        current = os.stat(
            namespace.canonical_name,
            dir_fd=namespace.directory,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return expected is None
    return expected is not None and _same_posix_file(expected.metadata, current)


def _restore_canonical_after_ambiguous_replace(
    namespace: _PosixCanonicalNamespace,
    *,
    snapshot: _CanonicalSnapshot | None,
) -> None:
    rollback_errors: list[Exception] = []
    try:
        _rename_to_unique_quarantine_at(
            directory=namespace.directory,
            name=namespace.canonical_name,
        )
    except FileNotFoundError:
        pass
    except Exception as exc:
        rollback_errors.append(exc)
    else:
        try:
            os.fsync(namespace.directory)
        except Exception as exc:
            rollback_errors.append(exc)
    if snapshot is not None:
        try:
            _publish_bytes_no_replace_at(
                directory=namespace.directory,
                name=namespace.canonical_name,
                data=snapshot.data,
            )
        except Exception as exc:
            rollback_errors.append(exc)
    try:
        os.fsync(namespace.directory)
    except Exception as exc:
        rollback_errors.append(exc)
    if rollback_errors:
        raise _RollbackStepErrors(
            rollback_errors,
            default_path=namespace.canonical_path,
        )


def _require_expected_canonical(
    namespace: _PosixCanonicalNamespace,
    expected: _CanonicalSnapshot | None,
) -> None:
    try:
        current = os.stat(
            namespace.canonical_name,
            dir_fd=namespace.directory,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if expected is None:
            return
        raise OSError(
            getattr(errno, "ESTALE", errno.EIO),
            "DesignSpec canonical disappeared before replacement",
            os.fspath(namespace.canonical_path),
        )
    if expected is None or not _same_posix_file(expected.metadata, current):
        raise OSError(
            getattr(errno, "ESTALE", errno.EIO),
            "DesignSpec canonical changed before replacement",
            os.fspath(namespace.canonical_path),
        )


def _rollback_canonical_posix(
    namespace: _PosixCanonicalNamespace,
    *,
    snapshot: _CanonicalSnapshot | None,
    publication: _CanonicalPublication,
    data: bytes,
) -> None:
    rollback_errors: list[Exception] = []
    current = _publication_from_stat(
        publication.metadata,
        descriptor=publication.descriptor,
    )
    try:
        quarantined = _quarantine_entry_at(
            directory=namespace.directory,
            name=namespace.canonical_name,
            descriptor=publication.descriptor,
            publication=current,
            data=data,
            display_path=namespace.canonical_path,
        )
        if not quarantined:
            raise OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "DesignSpec canonical rollback lost ownership",
                os.fspath(namespace.canonical_path),
            )
    except Exception as exc:
        rollback_errors.append(exc)
    if snapshot is not None:
        try:
            _publish_bytes_no_replace_at(
                directory=namespace.directory,
                name=namespace.canonical_name,
                data=snapshot.data,
            )
        except Exception as exc:
            rollback_errors.append(exc)
    try:
        os.fsync(namespace.directory)
    except Exception as exc:
        rollback_errors.append(exc)
    if rollback_errors:
        raise _RollbackStepErrors(
            rollback_errors,
            default_path=namespace.canonical_path,
        )


def _quarantine_archive_publication(
    namespace: _PosixDesignSpecNamespace,
    publication: _ArchivePublication,
    data: bytes,
) -> bool:
    assert publication.descriptor is not None
    return _quarantine_entry_at(
        directory=namespace.archive_directory,
        name=namespace.archive_name,
        descriptor=publication.descriptor,
        publication=publication,
        data=data,
        display_path=namespace.archive_path,
    )


def _quarantine_entry_at(
    *,
    directory: int,
    name: str,
    descriptor: int,
    publication: _ArchivePublication,
    data: bytes,
    allowed_links: frozenset[int] = frozenset({1}),
    display_path: Path | None = None,
) -> bool:
    try:
        _verify_posix_entry(
            directory=directory,
            name=name,
            descriptor=descriptor,
            publication=publication,
            data=data,
            allowed_links=allowed_links,
        )
    except FileNotFoundError:
        return True
    except OSError:
        return False
    quarantine_name = _rename_to_unique_quarantine_at(
        directory=directory,
        name=name,
    )
    os.fsync(directory)
    try:
        _verify_posix_entry(
            directory=directory,
            name=quarantine_name,
            descriptor=descriptor,
            publication=publication,
            data=data,
            allowed_links=allowed_links,
        )
    except Exception as exc:
        raise _classify_post_quarantine_verification_error(
            directory=directory,
            quarantine_name=quarantine_name,
            descriptor=descriptor,
            publication=publication,
            display_path=display_path,
            verification_error=exc,
        ) from exc
    return True


def _classify_post_quarantine_verification_error(
    *,
    directory: int,
    quarantine_name: str,
    descriptor: int,
    publication: _ArchivePublication,
    display_path: Path | None,
    verification_error: Exception,
) -> OSError:
    quarantine_path = (
        display_path.parent / quarantine_name
        if display_path is not None
        else Path(quarantine_name)
    )
    identity = _classify_quarantined_entry_identity_at(
        directory=directory,
        name=quarantine_name,
        descriptor=descriptor,
        publication=publication,
    )
    if identity.state == "owned":
        return _QuarantinedEntryVerificationError(
            quarantine_path,
            "retained the owned binding in quarantine; integrity verification "
            "failed, but matching device and inode do not establish foreign identity",
            verification_error,
        )
    if identity.state == "ambiguous":
        assert identity.error is not None
        return _QuarantinedEntryVerificationError(
            quarantine_path,
            "did not attempt reverse recovery because the quarantine binding was "
            "unavailable or its identity was uncertain",
            verification_error,
            classification_error=identity.error,
        )
    assert identity.state == "mismatch"
    return _QuarantinedArchiveMismatch(
        quarantine_path,
        "positive device/inode mismatch observed at the quarantine path; "
        "no reverse recovery was attempted; original verification error: "
        f"{type(verification_error).__name__}: {verification_error}",
    )


def _classify_quarantined_entry_identity_at(
    *,
    directory: int,
    name: str,
    descriptor: int,
    publication: _ArchivePublication,
) -> _QuarantinedEntryIdentity:
    try:
        retained = os.fstat(descriptor)
    except Exception as exc:
        return _QuarantinedEntryIdentity("ambiguous", exc)
    if (retained.st_dev, retained.st_ino) != (publication.device, publication.inode):
        return _QuarantinedEntryIdentity(
            "ambiguous",
            OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "retained DesignSpec publication identity no longer matches its "
                "original device and inode",
                name,
            ),
        )
    try:
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except Exception as exc:
        return _QuarantinedEntryIdentity("ambiguous", exc)
    if (current.st_dev, current.st_ino) != (retained.st_dev, retained.st_ino):
        return _QuarantinedEntryIdentity("mismatch")
    return _QuarantinedEntryIdentity("owned")


def _publish_bytes_no_replace_at(
    *,
    directory: int,
    name: str,
    data: bytes,
) -> None:
    descriptor, temporary_name = _create_private_temp_at(
        directory,
        prefix=f".{name}.",
        suffix=".rollback-tmp",
    )
    installed = False
    try:
        _write_descriptor(descriptor, data)
        _require_path_matches_descriptor(
            directory,
            temporary_name,
            descriptor,
            allowed_links=frozenset({1}),
        )
        _rename_no_replace_at(
            src_dir_fd=directory,
            src_name=temporary_name,
            dst_dir_fd=directory,
            dst_name=name,
        )
        installed = True
        metadata = os.fstat(descriptor)
        _verify_posix_entry(
            directory=directory,
            name=name,
            descriptor=descriptor,
            publication=_publication_from_stat(metadata, descriptor=descriptor),
            data=data,
        )
        os.fsync(directory)
    finally:
        if not installed:
            try:
                _quarantine_owned_temp_at(
                    directory=directory,
                    name=temporary_name,
                    descriptor=descriptor,
                )
            except OSError:
                pass
        os.close(descriptor)


def _require_posix_no_replace_rename() -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        return
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        return
    raise OSError(
        getattr(errno, "ENOTSUP", errno.EPERM),
        "atomic no-replace DesignSpec publication is unsupported on this platform",
    )


def _rename_no_replace_at(
    *,
    src_dir_fd: int,
    src_name: str,
    dst_dir_fd: int,
    dst_name: str,
) -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    source = os.fsencode(src_name)
    destination = os.fsencode(dst_name)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        operation = libc.renameatx_np
        operation.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        operation.restype = ctypes.c_int
        result = operation(
            src_dir_fd,
            source,
            dst_dir_fd,
            destination,
            0x00000004,
        )
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        operation = libc.renameat2
        operation.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        operation.restype = ctypes.c_int
        result = operation(
            src_dir_fd,
            source,
            dst_dir_fd,
            destination,
            0x00000001,
        )
    else:
        raise OSError(
            getattr(errno, "ENOTSUP", errno.EPERM),
            "atomic no-replace DesignSpec publication is unsupported on this platform",
        )
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), dst_name)


def _quarantine_owned_temp_at(
    *,
    directory: int,
    name: str,
    descriptor: int,
) -> bool:
    try:
        before = _require_path_matches_descriptor(
            directory,
            name,
            descriptor,
            allowed_links=frozenset({1}),
        )
    except (FileNotFoundError, OSError):
        return False
    quarantine_name = _rename_to_unique_quarantine_at(
        directory=directory,
        name=name,
    )
    os.fsync(directory)
    try:
        after = _require_path_matches_descriptor(
            directory,
            quarantine_name,
            descriptor,
            allowed_links=frozenset({1}),
        )
    except OSError as exc:
        raise _QuarantinedArchiveMismatch(
            Path(quarantine_name),
            "a private temporary source changed at quarantine rename; "
            "preserved the mismatched entry in quarantine",
        ) from exc
    if not _same_posix_file(before, after):
        raise _QuarantinedArchiveMismatch(
            Path(quarantine_name),
            "a private temporary source changed at quarantine rename; "
            "preserved the mismatched entry in quarantine",
        )
    return True


def _create_private_temp_at(
    directory: int,
    *,
    prefix: str,
    suffix: str,
) -> tuple[int, str]:
    flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_RDWR
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _ in range(32):
        name = f"{prefix}{secrets.token_hex(16)}{suffix}"
        try:
            return os.open(name, flags, 0o600, dir_fd=directory), name
        except FileExistsError:
            continue
    raise FileExistsError(
        errno.EEXIST,
        "could not allocate a private DesignSpec temporary file",
        prefix,
    )


def _write_descriptor(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise OSError(errno.EIO, "short DesignSpec temporary write")
        written += count
    os.fsync(descriptor)


def _open_regular_file_at(
    directory: int,
    name: str,
    *,
    allow_missing: bool,
) -> int | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    metadata = os.fstat(descriptor)
    if not _is_private_regular_file(metadata):
        os.close(descriptor)
        raise OSError(
            errno.EPERM,
            "unsafe DesignSpec file entry",
            name,
        )
    return descriptor


def _read_stable_descriptor(
    descriptor: int,
    *,
    allowed_links: frozenset[int],
) -> tuple[os.stat_result, bytes]:
    before = os.fstat(descriptor)
    if not _is_private_regular_file(before, allowed_links=allowed_links):
        raise OSError(errno.EPERM, "unsafe DesignSpec retained file")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 1 << 20):
        chunks.append(chunk)
    after = os.fstat(descriptor)
    if not _same_posix_file(before, after):
        raise OSError(
            getattr(errno, "ESTALE", errno.EIO),
            "DesignSpec retained file changed while reading",
        )
    return after, b"".join(chunks)


def _require_path_matches_descriptor(
    directory: int,
    name: str,
    descriptor: int,
    *,
    allowed_links: frozenset[int],
) -> os.stat_result:
    opened = os.fstat(descriptor)
    current = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if (
        not _is_private_regular_file(opened, allowed_links=allowed_links)
        or not _is_private_regular_file(current, allowed_links=allowed_links)
        or not _same_posix_file(opened, current)
    ):
        raise OSError(
            getattr(errno, "ESTALE", errno.EIO),
            "DesignSpec pathname no longer names the retained file",
            name,
        )
    return current


def _verify_posix_entry(
    *,
    directory: int,
    name: str,
    descriptor: int | None,
    publication: _ArchivePublication,
    data: bytes,
    allowed_links: frozenset[int] = frozenset({1}),
) -> None:
    if descriptor is None:
        raise OSError(errno.EBADF, "missing retained DesignSpec descriptor", name)
    metadata, observed = _read_stable_descriptor(
        descriptor,
        allowed_links=allowed_links,
    )
    current = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if (
        observed != data
        or not _is_private_regular_file(current, allowed_links=allowed_links)
        or not _matches_publication(metadata, publication)
        or not _matches_publication(current, publication)
        or not _same_posix_file(metadata, current)
    ):
        raise OSError(
            getattr(errno, "ESTALE", errno.EIO),
            "DesignSpec retained publication identity changed",
            name,
        )


def _entry_matches_descriptor_and_data(
    *,
    directory: int,
    name: str,
    descriptor: int,
    data: bytes,
) -> bool:
    try:
        metadata = os.fstat(descriptor)
        _verify_posix_entry(
            directory=directory,
            name=name,
            descriptor=descriptor,
            publication=_publication_from_stat(
                metadata,
                descriptor=descriptor,
            ),
            data=data,
        )
        return True
    except OSError:
        return False


def _publication_from_stat(
    metadata: os.stat_result,
    *,
    descriptor: int,
    owned: bool = True,
) -> _ArchivePublication:
    return _ArchivePublication(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        descriptor=descriptor,
        owned=owned,
    )


def _same_posix_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_nlink == after.st_nlink
        and stat.S_IFMT(before.st_mode) == stat.S_IFMT(after.st_mode)
    )


@contextmanager
def _canonical_transaction_lock(
    canonical_path: Path,
) -> Iterator[Path | _PosixCanonicalNamespace]:
    """Serialize every revision transaction for one canonical DesignSpec."""

    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    identity = _canonical_lock_identity(canonical_path)
    key = os.path.normcase(os.fspath(identity))
    with _CANONICAL_LOCKS_GUARD:
        local_lock = _CANONICAL_LOCKS.setdefault(key, threading.RLock())
    with local_lock:
        if _LOCK_OS_NAME == "nt":
            with _windows_canonical_transaction_lock(
                canonical_path,
                identity,
            ) as locked_path:
                yield locked_path
            return
        with _posix_canonical_transaction_lock(
            canonical_path,
            identity,
        ) as locked_path:
            yield locked_path


def _canonical_lock_identity(canonical_path: Path) -> Path:
    """Use a stable lexical parent; retained descriptors prove the live binding."""

    parent = Path(os.path.abspath(os.fspath(canonical_path.parent)))
    return parent / canonical_path.name


@contextmanager
def _posix_canonical_transaction_lock(
    canonical_path: Path,
    identity: Path,
) -> Iterator[_PosixCanonicalNamespace]:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    directory = os.open(identity.parent, directory_flags)
    descriptor: int | None = None
    try:
        parent_metadata = os.fstat(directory)
        _validate_canonical_parent_binding(
            canonical_path,
            identity,
            parent_metadata,
        )
        lock_name = f".{identity.name}.lock"
        descriptor = _open_posix_lock_sidecar(directory, lock_name)
        with os.fdopen(descriptor, "a+b") as handle:
            descriptor = None
            _lock_sidecar(handle)
            try:
                _validate_posix_locked_paths(
                    canonical_path=canonical_path,
                    identity=identity,
                    directory=directory,
                    parent_metadata=parent_metadata,
                    lock_name=lock_name,
                    lock_descriptor=handle.fileno(),
                )
                yield _PosixCanonicalNamespace(
                    canonical_path=identity,
                    requested_canonical_path=Path(
                        os.path.abspath(os.fspath(canonical_path))
                    ),
                    directory=directory,
                    parent_metadata=parent_metadata,
                    canonical_name=identity.name,
                    lock_name=lock_name,
                    lock_descriptor=handle.fileno(),
                )
            finally:
                _unlock_sidecar(handle)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def _open_posix_lock_sidecar(directory: int, lock_name: str) -> int:
    flags = (
        os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(
            lock_name,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory,
        )
    except FileExistsError:
        return os.open(lock_name, flags, dir_fd=directory)
    try:
        os.fsync(directory)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _windows_canonical_transaction_lock(
    canonical_path: Path,
    identity: Path,
) -> Iterator[Path]:
    parent_metadata = os.lstat(identity.parent)
    _validate_canonical_parent_binding(
        canonical_path,
        identity,
        parent_metadata,
    )
    lock_path = identity.with_name(f".{identity.name}.lock")
    native = _windows_archive_io_factory()
    handle = native.open_lock(lock_path)
    locked = False
    try:
        opened = native.metadata(handle)
        if not _is_private_windows_file(opened):
            raise OSError(
                errno.EPERM,
                "unsafe DesignSpec canonical lock file",
                os.fspath(lock_path),
            )
        native.lock(handle)
        locked = True
        _validate_windows_locked_paths(
            canonical_path=canonical_path,
            identity=identity,
            parent_metadata=parent_metadata,
            lock_path=lock_path,
            lock_handle=handle,
            native=native,
        )
        yield identity
        _validate_windows_locked_paths(
            canonical_path=canonical_path,
            identity=identity,
            parent_metadata=parent_metadata,
            lock_path=lock_path,
            lock_handle=handle,
            native=native,
        )
    finally:
        try:
            if locked:
                native.unlock(handle)
        finally:
            native.close(handle)


def _validate_canonical_parent_binding(
    canonical_path: Path,
    identity: Path,
    expected: os.stat_result,
) -> None:
    lexical_parent = os.lstat(canonical_path.parent)
    current = os.stat(identity.parent, follow_symlinks=True)
    if (
        not _is_safe_directory(lexical_parent)
        or not _is_safe_directory(current)
        or (lexical_parent.st_dev, lexical_parent.st_ino)
        != (expected.st_dev, expected.st_ino)
        or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
    ):
        raise OSError(
            errno.EPERM,
            "unsafe DesignSpec canonical parent",
            os.fspath(canonical_path.parent),
        )


def _validate_posix_locked_paths(
    *,
    canonical_path: Path,
    identity: Path,
    directory: int,
    parent_metadata: os.stat_result,
    lock_name: str,
    lock_descriptor: int,
) -> None:
    _validate_canonical_parent_binding(
        canonical_path,
        identity,
        parent_metadata,
    )
    opened_lock = os.fstat(lock_descriptor)
    current_lock = os.stat(
        lock_name,
        dir_fd=directory,
        follow_symlinks=False,
    )
    if (
        not _is_private_regular_file(opened_lock)
        or not _is_private_regular_file(current_lock)
        or (opened_lock.st_dev, opened_lock.st_ino)
        != (current_lock.st_dev, current_lock.st_ino)
    ):
        raise OSError(
            errno.EPERM,
            "unsafe DesignSpec canonical lock file",
            os.fspath(identity.with_name(lock_name)),
        )
    _validate_posix_canonical_entry(directory, identity)


def _validate_windows_locked_paths(
    *,
    canonical_path: Path,
    identity: Path,
    parent_metadata: os.stat_result,
    lock_path: Path,
    lock_handle: int,
    native: _WindowsArchiveIO,
) -> None:
    _validate_canonical_parent_binding(
        canonical_path,
        identity,
        parent_metadata,
    )
    opened_lock = native.metadata(lock_handle)
    current_lock = native.stat_path(lock_path)
    if (
        not _is_private_windows_file(opened_lock)
        or not _is_private_windows_file(current_lock)
        or not _same_windows_file(opened_lock, current_lock)
    ):
        raise OSError(
            errno.EPERM,
            "unsafe DesignSpec canonical lock file",
            os.fspath(lock_path),
        )
    try:
        canonical_metadata = native.stat_path(identity)
    except FileNotFoundError:
        return
    if not _is_private_windows_file(canonical_metadata):
        raise OSError(
            errno.EPERM,
            "unsafe DesignSpec canonical path",
            os.fspath(identity),
        )


def _validate_posix_canonical_entry(directory: int, identity: Path) -> None:
    try:
        metadata = os.stat(
            identity.name,
            dir_fd=directory,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    if not _is_private_regular_file(metadata):
        raise OSError(
            errno.EPERM,
            "unsafe DesignSpec canonical path",
            os.fspath(identity),
        )


def _is_safe_directory(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and not (
            getattr(metadata, "st_file_attributes", 0)
            & _WINDOWS_REPARSE_POINT
        )
    )


def _lock_sidecar(handle: BinaryIO) -> None:
    if os.name == "posix":
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    import msvcrt

    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)


def _unlock_sidecar(handle: BinaryIO) -> None:
    if os.name == "posix":
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    import msvcrt

    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _publish_archive_no_replace(
    path: Path,
    data: bytes,
    *,
    before_publish: Callable[[Path], None],
) -> _ArchivePublication | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=os.fspath(path.parent),
    )
    temporary_path = Path(temporary_name)
    publication: _ArchivePublication | None = None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            metadata = os.fstat(stream.fileno())

        before_publish(path)
        try:
            os.link(temporary_path, path)
        except OSError as exc:
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
            existing = _read_stable_regular_file(path)
            if existing != data:
                raise FileExistsError(
                    errno.EEXIST,
                    "immutable DesignSpec revision archive conflicts with existing bytes",
                    os.fspath(path),
                ) from exc
            return None
        publication = _ArchivePublication(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            modified_ns=metadata.st_mtime_ns,
        )
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        except Exception as cleanup_exc:
            try:
                released = _release_owned_archive_if_unchanged(
                    path,
                    data,
                    publication,
                    allowed_links=frozenset({1, 2}),
                )
                if not released:
                    raise OSError(
                        getattr(errno, "ESTALE", errno.EIO),
                        "DesignSpec archive publish rollback lost ownership",
                        os.fspath(path),
                    )
            except Exception as rollback_exc:
                rollback_path = getattr(rollback_exc, "path", path)
                raise _ArchivePublishRollbackError(
                    rollback_path,
                    rollback_exc,
                ) from cleanup_exc
            raise
        return publication
    finally:
        if publication is None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def _canonical_payload_is_installed(path: Path, data: bytes) -> bool:
    try:
        return _read_stable_regular_file(path) == data
    except (FileNotFoundError, OSError):
        return False


def _release_owned_archive_if_unchanged(
    path: Path,
    data: bytes,
    publication: _ArchivePublication,
    *,
    allowed_links: frozenset[int] = frozenset({1}),
) -> bool:
    if _RUNTIME_OS_NAME == "nt":
        return _remove_owned_windows_archive_if_unchanged(
            path,
            data,
            publication,
            allowed_links=allowed_links,
        )

    required_dir_fd_operations = {os.open, os.stat, os.rename}
    if not required_dir_fd_operations.issubset(os.supports_dir_fd):
        raise OSError(
            getattr(errno, "ENOTSUP", errno.EPERM),
            "secure DesignSpec archive rollback is unsupported on this platform",
            os.fspath(path),
        )

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    directory = os.open(path.parent, directory_flags)
    descriptor: int | None = None
    try:
        directory_metadata = os.fstat(directory)
        if not stat.S_ISDIR(directory_metadata.st_mode):
            raise OSError(
                errno.ENOTDIR,
                "DesignSpec archive parent is not a directory",
                os.fspath(path.parent),
            )
        try:
            descriptor = os.open(path.name, file_flags, dir_fd=directory)
        except FileNotFoundError:
            return True

        before = os.fstat(descriptor)
        if not _is_private_regular_file(
            before,
            allowed_links=allowed_links,
        ) or not _matches_publication(
            before,
            publication,
        ):
            return False
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1 << 20):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if (
            not _is_private_regular_file(after, allowed_links=allowed_links)
            or not _is_private_regular_file(current, allowed_links=allowed_links)
            or not _matches_publication(after, publication)
            or not _matches_publication(current, publication)
            or b"".join(chunks) != data
        ):
            return False

        final = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if not _is_private_regular_file(
            final,
            allowed_links=allowed_links,
        ) or not _matches_publication(
            final,
            publication,
        ):
            return False

        quarantine_name = _rename_to_unique_quarantine_at(
            directory=directory,
            name=path.name,
        )
        os.fsync(directory)

        try:
            _verify_posix_entry(
                directory=directory,
                name=quarantine_name,
                descriptor=descriptor,
                publication=publication,
                data=data,
                allowed_links=allowed_links,
            )
        except Exception as exc:
            raise _classify_post_quarantine_verification_error(
                directory=directory,
                quarantine_name=quarantine_name,
                descriptor=descriptor,
                publication=publication,
                display_path=path,
                verification_error=exc,
            ) from exc
        return True
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def _unique_quarantine_name(directory: int, archive_name: str) -> str:
    for _ in range(16):
        candidate = (
            f".{archive_name}.{secrets.token_hex(16)}.rollback-orphan"
        )
        try:
            os.stat(candidate, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return candidate
    raise FileExistsError(
        errno.EEXIST,
        "could not allocate a unique DesignSpec rollback quarantine name",
        archive_name,
    )


def _rename_to_unique_quarantine_at(
    *,
    directory: int,
    name: str,
) -> str:
    for _ in range(16):
        quarantine_name = _unique_quarantine_name(directory, name)
        try:
            _rename_no_replace_at(
                src_dir_fd=directory,
                src_name=name,
                dst_dir_fd=directory,
                dst_name=quarantine_name,
            )
        except OSError as exc:
            if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                continue
            raise
        return quarantine_name
    raise FileExistsError(
        errno.EEXIST,
        "could not reserve a unique DesignSpec rollback quarantine name",
        name,
    )


def _remove_owned_windows_archive_if_unchanged(
    path: Path,
    data: bytes,
    publication: _ArchivePublication,
    *,
    allowed_links: frozenset[int] = frozenset({1}),
) -> bool:
    native = _windows_archive_io_factory()
    try:
        handle = native.open_file(path, delete_access=True)
    except FileNotFoundError:
        return True

    try:
        before = native.metadata(handle)
        if not _is_private_windows_file(
            before,
            allowed_links=allowed_links,
        ) or not _matches_windows_publication(
            before,
            publication,
        ):
            return False
        observed = native.read_bytes(handle)
        after = native.metadata(handle)
        current = native.stat_path(path)
        if (
            not _is_private_windows_file(after, allowed_links=allowed_links)
            or not _is_private_windows_file(current, allowed_links=allowed_links)
            or not _same_windows_file(before, after)
            or not _same_windows_file(after, current)
            or not _matches_windows_publication(after, publication)
            or observed != data
        ):
            return False
        final = native.stat_path(path)
        if (
            not _is_private_windows_file(final, allowed_links=allowed_links)
            or not _same_windows_file(after, final)
            or not _matches_windows_publication(final, publication)
        ):
            return False
        native.mark_delete(handle)
    except FileNotFoundError:
        return True
    finally:
        native.close(handle)

    native.flush_parent(path.parent)
    return True


def _is_private_windows_file(
    metadata: _WindowsArchiveMetadata,
    *,
    allowed_links: frozenset[int] = frozenset({1}),
) -> bool:
    return (
        not (metadata.attributes & _WINDOWS_REPARSE_POINT)
        and not (metadata.attributes & _WINDOWS_DIRECTORY)
        and metadata.links in allowed_links
    )


def _same_windows_file(
    before: _WindowsArchiveMetadata,
    after: _WindowsArchiveMetadata,
) -> bool:
    return (
        before.device == after.device
        and before.inode == after.inode
        and before.size == after.size
        and before.modified_ns == after.modified_ns
        and before.links == after.links
        and before.attributes == after.attributes
    )


def _matches_windows_publication(
    metadata: _WindowsArchiveMetadata,
    publication: _ArchivePublication,
) -> bool:
    return (
        metadata.device == publication.device
        and metadata.inode == publication.inode
        and metadata.size == publication.size
        and metadata.modified_ns == publication.modified_ns
    )


def _matches_publication(
    metadata: os.stat_result,
    publication: _ArchivePublication,
) -> bool:
    return (
        metadata.st_dev == publication.device
        and metadata.st_ino == publication.inode
        and metadata.st_size == publication.size
        and metadata.st_mtime_ns == publication.modified_ns
    )


def _is_private_regular_file(
    metadata: os.stat_result,
    *,
    allowed_links: frozenset[int] = frozenset({1}),
) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and not (
            getattr(metadata, "st_file_attributes", 0)
            & _WINDOWS_REPARSE_POINT
        )
        and metadata.st_nlink in allowed_links
    )


def _read_stable_regular_file(path: Path) -> bytes:
    if _RUNTIME_OS_NAME == "nt":
        return _read_stable_windows_file(path)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not _is_private_regular_file(before):
            raise OSError(
                errno.EPERM,
                "existing DesignSpec revision archive is not a regular file",
                os.fspath(path),
            )
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1 << 20):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = os.lstat(path)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        path_identity = (current.st_dev, current.st_ino)
        if (
            identity_before != identity_after
            or path_identity != (after.st_dev, after.st_ino)
            or not _is_private_regular_file(after)
            or not _is_private_regular_file(current)
        ):
            raise OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "DesignSpec revision archive changed while verifying idempotency",
                os.fspath(path),
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_stable_windows_file(path: Path) -> bytes:
    native = _windows_archive_io_factory()
    handle = native.open_file(path)
    try:
        before = native.metadata(handle)
        if not _is_private_windows_file(before):
            raise OSError(
                errno.EPERM,
                "DesignSpec revision archive is not a private regular file",
                os.fspath(path),
            )
        data = native.read_bytes(handle)
        after = native.metadata(handle)
        current = native.stat_path(path)
        if (
            not _is_private_windows_file(after)
            or not _is_private_windows_file(current)
            or not _same_windows_file(before, after)
            or not _same_windows_file(after, current)
        ):
            raise OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "DesignSpec revision archive changed while verifying idempotency",
                os.fspath(path),
            )
        return data
    finally:
        native.close(handle)
