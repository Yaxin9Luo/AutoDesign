"""Durable, transport-neutral Paper All-in-One parent job records.

The parent record is the cancellation barrier for its four child reservations.
Child starts use a short durable claim/commit/resolve protocol; callers must
never keep a synchronous parent lock held across an async operation.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from types import MappingProxyType
from typing import Awaitable, BinaryIO, Callable, Iterator, Literal, Mapping, TypeAlias, cast
from uuid import uuid4

from .run_control import RunControlError, validate_run_id


PaperBundleState = Literal[
    "reserved",
    "running",
    "cancelling",
    "cancelled",
    "completed",
    "partial",
    "failed",
]

_SCHEMA_VERSION = 2
_LEGACY_SCHEMA_VERSION = 1
_ARTIFACT_TYPES = ("poster", "deck", "landing", "video")
_ARTIFACT_SET = frozenset(_ARTIFACT_TYPES)
_PARENT_STATES = frozenset(
    {"reserved", "running", "cancelling", "cancelled", "completed", "partial", "failed"}
)
_PARENT_TERMINAL_STATES = frozenset({"cancelled", "completed", "partial", "failed"})
_CHILD_STATES = frozenset(
    {
        "reserved",
        "uploading",
        "queued",
        "running",
        "completing",
        "completed",
        "cancelling",
        "cancelled",
        "failed",
    }
)
_CHILD_TERMINAL_STATES = frozenset({"completed", "cancelled", "failed"})
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_CLAIM_FILENAME_PATTERN = re.compile(
    r"(?P<owner_digest>[0-9a-f]{64})-(?P<idempotency_digest>[0-9a-f]{64})\.json\Z"
)
_PENDING_RECORD_TEMP_PATTERN = re.compile(
    r"\.paper_bundle_job\.json\.[0-9a-f]{32}\.tmp\Z"
)
_SAFE_SLOT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_NOFOLLOW_FLAGS = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_POSIX_DIR_FD_AVAILABLE = (
    os.name == "posix"
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
)


class PaperBundleError(RuntimeError):
    """Base class for durable parent-job errors."""


class InvalidPaperBundle(PaperBundleError, ValueError):
    """A record, descriptor, or path fails the parent-job contract."""


class PaperBundleConflict(PaperBundleError):
    """An ID or idempotency key conflicts with different creation metadata."""


class PaperBundleNotFound(PaperBundleError, KeyError):
    """The requested owner-visible parent job does not exist."""


class PaperBundleBarrierClosed(PaperBundleError):
    """A child attempted work after its parent cancellation barrier closed."""


class StalePaperBundleRevision(PaperBundleError):
    """A compare-and-swap mutation used an old parent revision."""


@dataclass(frozen=True)
class _WindowsStat:
    st_mode: int
    st_dev: int
    st_ino: int
    st_nlink: int
    st_file_attributes: int


@dataclass(frozen=True)
class _WindowsDirectoryHandle:
    raw_handle: int
    path: Path
    identity: tuple[int, int]
    access_mask: int


class _WindowsNativeIO:
    """Small Win32 adapter that pins directories and never follows reparse points."""

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _DELETE = 0x00010000
    _FILE_READ_ATTRIBUTES = 0x00000080
    _FILE_LIST_DIRECTORY = 0x00000001
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _CREATE_NEW = 1
    _OPEN_EXISTING = 3
    _OPEN_ALWAYS = 4
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_RENAME_INFO_CLASS = 3
    _FILE_DISPOSITION_INFO_CLASS = 4
    _ERROR_FILE_NOT_FOUND = 2
    _ERROR_PATH_NOT_FOUND = 3
    _ERROR_FILE_EXISTS = 80
    _ERROR_ALREADY_EXISTS = 183
    _ERROR_INVALID_FUNCTION = 1
    _ERROR_INVALID_HANDLE = 6

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class _ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        class _FileRenameInfo(ctypes.Structure):
            _fields_ = [
                ("ReplaceIfExists", ctypes.c_ubyte),
                ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.DWORD),
                ("FileName", wintypes.WCHAR * 1),
            ]

        class _FileDispositionInfo(ctypes.Structure):
            _fields_ = [("DeleteFile", ctypes.c_ubyte)]

        self._by_handle_file_information = _ByHandleFileInformation
        self._file_rename_info = _FileRenameInfo
        self._file_disposition_info = _FileDispositionInfo

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
        self.kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        self.kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        self.kernel32.GetVolumeInformationByHandleW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        self.kernel32.GetVolumeInformationByHandleW.restype = wintypes.BOOL
        self.kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, wintypes.LPVOID]
        self.kernel32.CreateDirectoryW.restype = wintypes.BOOL
        self.kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        self.kernel32.ReadFile.restype = wintypes.BOOL
        self.kernel32.WriteFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        self.kernel32.WriteFile.restype = wintypes.BOOL
        self.kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        self.kernel32.FlushFileBuffers.restype = wintypes.BOOL
        self.kernel32.SetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self.kernel32.SetFileInformationByHandle.restype = wintypes.BOOL

        self.invalid_handle = ctypes.c_void_p(-1).value

    def _last_error(self, operation: str) -> OSError:
        code = self.ctypes.get_last_error()
        if code in {self._ERROR_FILE_NOT_FOUND, self._ERROR_PATH_NOT_FOUND}:
            return FileNotFoundError(code, operation)
        if code in {self._ERROR_FILE_EXISTS, self._ERROR_ALREADY_EXISTS}:
            return FileExistsError(code, operation)
        return self.ctypes.WinError(code)

    def _create_file(
        self,
        path: Path,
        *,
        access: int,
        share: int,
        creation: int,
        flags: int,
    ) -> int:
        handle = self.kernel32.CreateFileW(
            str(path),
            access,
            share,
            None,
            creation,
            flags,
            None,
        )
        handle_value = getattr(handle, "value", handle)
        raw_handle = (
            int(handle_value)
            if handle_value is not None
            else self.invalid_handle
        )
        if raw_handle == self.invalid_handle:
            raise self._last_error(f"CreateFileW({path})")
        return raw_handle

    def close(self, raw_handle: int) -> None:
        if not self.kernel32.CloseHandle(raw_handle):
            raise self._last_error("CloseHandle")

    def stat_handle(self, raw_handle: int) -> _WindowsStat:
        info = self._by_handle_file_information()
        if not self.kernel32.GetFileInformationByHandle(raw_handle, self.ctypes.byref(info)):
            raise self._last_error("GetFileInformationByHandle")
        is_directory = bool(info.dwFileAttributes & self._FILE_ATTRIBUTE_DIRECTORY)
        mode = stat.S_IFDIR if is_directory else stat.S_IFREG
        return _WindowsStat(
            st_mode=mode,
            st_dev=int(info.dwVolumeSerialNumber),
            st_ino=(int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow),
            st_nlink=int(info.nNumberOfLinks),
            st_file_attributes=int(info.dwFileAttributes),
        )

    def _validate_handle(
        self,
        raw_handle: int,
        *,
        directory: bool,
        label: str,
    ) -> _WindowsStat:
        metadata = self.stat_handle(raw_handle)
        if metadata.st_file_attributes & self._FILE_ATTRIBUTE_REPARSE_POINT:
            raise InvalidPaperBundle(f"reparse points are not allowed: {label}")
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected(metadata.st_mode):
            raise InvalidPaperBundle(f"unsafe {'directory' if directory else 'file'}: {label}")
        if not directory and metadata.st_nlink != 1:
            raise InvalidPaperBundle(f"unsafe file: {label}")
        return metadata

    def _filesystem_name(self, raw_handle: int) -> str:
        filesystem_name = self.ctypes.create_unicode_buffer(64)
        if not self.kernel32.GetVolumeInformationByHandleW(
            raw_handle,
            None,
            0,
            None,
            None,
            None,
            filesystem_name,
            len(filesystem_name),
        ):
            raise self._last_error("GetVolumeInformationByHandleW")
        return filesystem_name.value.upper()

    def assert_supported_parent(self, path: Path) -> None:
        candidate = path.absolute()
        while not candidate.exists():
            parent = candidate.parent
            if parent == candidate:
                raise InvalidPaperBundle("cannot locate the Paper Bundle volume")
            candidate = parent
        handle = self.open_directory(candidate, require_ntfs=False)
        try:
            filesystem = self._filesystem_name(handle.raw_handle)
        finally:
            self.close(handle.raw_handle)
        if filesystem != "NTFS":
            raise InvalidPaperBundle(
                f"Paper Bundle durable cancellation requires NTFS, found {filesystem or 'unknown'}"
            )

    def open_directory(
        self,
        path: Path,
        *,
        require_ntfs: bool = True,
    ) -> _WindowsDirectoryHandle:
        access_mask = (
            self._GENERIC_WRITE
            | self._FILE_LIST_DIRECTORY
            | self._FILE_READ_ATTRIBUTES
        )
        raw_handle = self._create_file(
            path,
            access=access_mask,
            share=self._FILE_SHARE_READ | self._FILE_SHARE_WRITE,
            creation=self._OPEN_EXISTING,
            flags=(
                self._FILE_FLAG_BACKUP_SEMANTICS
                | self._FILE_FLAG_OPEN_REPARSE_POINT
            ),
        )
        try:
            metadata = self._validate_handle(
                raw_handle,
                directory=True,
                label=str(path),
            )
            if require_ntfs:
                filesystem = self._filesystem_name(raw_handle)
                if filesystem != "NTFS":
                    raise InvalidPaperBundle(
                        "Paper Bundle durable cancellation requires NTFS"
                    )
            return _WindowsDirectoryHandle(
                raw_handle=raw_handle,
                path=path.absolute(),
                identity=(metadata.st_dev, metadata.st_ino),
                access_mask=access_mask,
            )
        except BaseException:
            self.close(raw_handle)
            raise

    def open_directory_at(
        self,
        parent: _WindowsDirectoryHandle,
        name: str,
        *,
        create: bool,
        exclusive: bool,
    ) -> _WindowsDirectoryHandle:
        path = parent.path / name
        if create:
            created = bool(self.kernel32.CreateDirectoryW(str(path), None))
            if not created:
                error = self._last_error(f"CreateDirectoryW({path})")
                if not isinstance(error, FileExistsError) or exclusive:
                    raise error
        return self.open_directory(path, require_ntfs=False)

    def stat_path(self, path: Path) -> _WindowsStat:
        raw_handle = self._create_file(
            path,
            access=self._FILE_READ_ATTRIBUTES,
            share=self._FILE_SHARE_READ | self._FILE_SHARE_WRITE,
            creation=self._OPEN_EXISTING,
            flags=(
                self._FILE_FLAG_BACKUP_SEMANTICS
                | self._FILE_FLAG_OPEN_REPARSE_POINT
            ),
        )
        try:
            return self.stat_handle(raw_handle)
        finally:
            self.close(raw_handle)

    def stat_at(self, directory: _WindowsDirectoryHandle, name: str) -> _WindowsStat:
        return self.stat_path(directory.path / name)

    def listdir(self, directory: _WindowsDirectoryHandle) -> list[str]:
        return os.listdir(directory.path)

    def read_bytes(self, directory: _WindowsDirectoryHandle, name: str) -> bytes:
        path = directory.path / name
        raw_handle = self._create_file(
            path,
            access=self._GENERIC_READ | self._FILE_READ_ATTRIBUTES,
            share=self._FILE_SHARE_READ | self._FILE_SHARE_WRITE,
            creation=self._OPEN_EXISTING,
            flags=self._FILE_FLAG_OPEN_REPARSE_POINT,
        )
        try:
            self._validate_handle(raw_handle, directory=False, label=name)
            chunks: list[bytes] = []
            while True:
                buffer = self.ctypes.create_string_buffer(1024 * 1024)
                read = self.wintypes.DWORD()
                if not self.kernel32.ReadFile(
                    raw_handle,
                    buffer,
                    len(buffer),
                    self.ctypes.byref(read),
                    None,
                ):
                    raise self._last_error(f"ReadFile({path})")
                if read.value == 0:
                    break
                chunks.append(buffer.raw[: read.value])
            return b"".join(chunks)
        finally:
            self.close(raw_handle)

    def _write_all(self, raw_handle: int, data: bytes, *, label: str) -> None:
        offset = 0
        while offset < len(data):
            chunk = data[offset : offset + 1024 * 1024]
            buffer = self.ctypes.create_string_buffer(chunk)
            written = self.wintypes.DWORD()
            if not self.kernel32.WriteFile(
                raw_handle,
                buffer,
                len(chunk),
                self.ctypes.byref(written),
                None,
            ):
                raise self._last_error(f"WriteFile({label})")
            if written.value <= 0:
                raise OSError(f"short durable write: {label}")
            offset += written.value

    def _rename_handle(
        self,
        raw_handle: int,
        destination: _WindowsDirectoryHandle,
        name: str,
        *,
        replace_existing: bool,
    ) -> None:
        encoded_name = name.encode("utf-16-le")
        offset = self._file_rename_info.FileName.offset
        buffer = self.ctypes.create_string_buffer(offset + len(encoded_name))
        info = self.ctypes.cast(
            buffer,
            self.ctypes.POINTER(self._file_rename_info),
        ).contents
        info.ReplaceIfExists = 1 if replace_existing else 0
        info.RootDirectory = destination.raw_handle
        info.FileNameLength = len(encoded_name)
        self.ctypes.memmove(
            self.ctypes.addressof(buffer) + offset,
            encoded_name,
            len(encoded_name),
        )
        if not self.kernel32.SetFileInformationByHandle(
            raw_handle,
            self._FILE_RENAME_INFO_CLASS,
            buffer,
            len(buffer),
        ):
            raise self._last_error(f"SetFileInformationByHandle(rename {name})")

    def _mark_delete(self, raw_handle: int) -> None:
        info = self._file_disposition_info(DeleteFile=1)
        if not self.kernel32.SetFileInformationByHandle(
            raw_handle,
            self._FILE_DISPOSITION_INFO_CLASS,
            self.ctypes.byref(info),
            self.ctypes.sizeof(info),
        ):
            raise self._last_error("SetFileInformationByHandle(delete)")

    def durable_write(self, directory: _WindowsDirectoryHandle, name: str, data: bytes) -> None:
        temporary_name = f".{name}.{uuid4().hex}.tmp"
        raw_handle = self._create_file(
            directory.path / temporary_name,
            access=(
                self._GENERIC_READ
                | self._GENERIC_WRITE
                | self._DELETE
                | self._FILE_READ_ATTRIBUTES
            ),
            share=(
                self._FILE_SHARE_READ
                | self._FILE_SHARE_WRITE
                | self._FILE_SHARE_DELETE
            ),
            creation=self._CREATE_NEW,
            flags=self._FILE_ATTRIBUTE_NORMAL | self._FILE_FLAG_OPEN_REPARSE_POINT,
        )
        renamed = False
        try:
            self._validate_handle(raw_handle, directory=False, label=temporary_name)
            self._write_all(raw_handle, data, label=temporary_name)
            if not self.kernel32.FlushFileBuffers(raw_handle):
                raise self._last_error(f"FlushFileBuffers({temporary_name})")
            self._rename_handle(
                raw_handle,
                directory,
                name,
                replace_existing=True,
            )
            renamed = True
            if not self.kernel32.FlushFileBuffers(raw_handle):
                raise self._last_error(f"FlushFileBuffers({name})")
        except BaseException:
            if not renamed:
                try:
                    self._mark_delete(raw_handle)
                except OSError:
                    pass
            raise
        finally:
            self.close(raw_handle)
        metadata = self.stat_at(directory, name)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise InvalidPaperBundle(f"unsafe file after durable write: {name}")

    def open_lock(
        self,
        directory: _WindowsDirectoryHandle,
        name: str,
    ) -> BinaryIO:
        import msvcrt

        raw_handle = self._create_file(
            directory.path / name,
            access=(
                self._GENERIC_READ
                | self._GENERIC_WRITE
                | self._FILE_READ_ATTRIBUTES
            ),
            share=self._FILE_SHARE_READ | self._FILE_SHARE_WRITE,
            creation=self._OPEN_ALWAYS,
            flags=self._FILE_ATTRIBUTE_NORMAL | self._FILE_FLAG_OPEN_REPARSE_POINT,
        )
        try:
            self._validate_handle(raw_handle, directory=False, label=name)
            descriptor = msvcrt.open_osfhandle(raw_handle, os.O_RDWR)
        except BaseException:
            self.close(raw_handle)
            raise
        try:
            return os.fdopen(descriptor, "a+b")
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise

    def stat_open_file(self, handle: BinaryIO) -> _WindowsStat:
        import msvcrt

        return self.stat_handle(msvcrt.get_osfhandle(handle.fileno()))

    def rename_at(
        self,
        source_directory: _WindowsDirectoryHandle,
        source_name: str,
        destination_directory: _WindowsDirectoryHandle,
        destination_name: str,
    ) -> None:
        raw_handle = self._create_file(
            source_directory.path / source_name,
            access=self._DELETE | self._FILE_READ_ATTRIBUTES,
            share=(
                self._FILE_SHARE_READ
                | self._FILE_SHARE_WRITE
                | self._FILE_SHARE_DELETE
            ),
            creation=self._OPEN_EXISTING,
            flags=(
                self._FILE_FLAG_BACKUP_SEMANTICS
                | self._FILE_FLAG_OPEN_REPARSE_POINT
            ),
        )
        try:
            metadata = self.stat_handle(raw_handle)
            if metadata.st_file_attributes & self._FILE_ATTRIBUTE_REPARSE_POINT:
                raise InvalidPaperBundle(f"unsafe rename source: {source_name}")
            self._rename_handle(
                raw_handle,
                destination_directory,
                destination_name,
                replace_existing=True,
            )
        finally:
            self.close(raw_handle)

    def unlink_at(self, directory: _WindowsDirectoryHandle, name: str) -> None:
        raw_handle = self._create_file(
            directory.path / name,
            access=self._DELETE | self._FILE_READ_ATTRIBUTES,
            share=(
                self._FILE_SHARE_READ
                | self._FILE_SHARE_WRITE
                | self._FILE_SHARE_DELETE
            ),
            creation=self._OPEN_EXISTING,
            flags=(
                self._FILE_FLAG_BACKUP_SEMANTICS
                | self._FILE_FLAG_OPEN_REPARSE_POINT
            ),
        )
        try:
            self._mark_delete(raw_handle)
        finally:
            self.close(raw_handle)

    def flush_directory(self, directory: _WindowsDirectoryHandle) -> None:
        if not directory.access_mask & self._GENERIC_WRITE:
            raise InvalidPaperBundle(
                "Windows directory handle lacks durable write access"
            )
        if self.kernel32.FlushFileBuffers(directory.raw_handle):
            return
        if self.ctypes.get_last_error() not in {
            self._ERROR_INVALID_FUNCTION,
            self._ERROR_INVALID_HANDLE,
        }:
            raise self._last_error("FlushFileBuffers(directory)")


def _load_windows_native_io() -> _WindowsNativeIO | None:
    if os.name != "nt":
        return None
    try:
        return _WindowsNativeIO()
    except (AttributeError, OSError):
        return None


_WINDOWS_IO = _load_windows_native_io()
_SECURE_DIR_FD_AVAILABLE = _POSIX_DIR_FD_AVAILABLE or _WINDOWS_IO is not None
DirectoryHandle: TypeAlias = int | _WindowsDirectoryHandle | Path


@dataclass(frozen=True)
class PaperBundleInputSlot:
    name: str
    expected_sha256: str
    expected_size: int


@dataclass(frozen=True)
class PaperBundleChildDescriptor:
    run_id: str
    artifact_type: str
    conversation_id: str
    input_slots: tuple[PaperBundleInputSlot, ...]
    upload_token: str
    request_digest: str
    expires_at: float
    state: str = "reserved"
    terminal: bool = False
    process_free: bool = True
    diagnostic: str | None = None


@dataclass(frozen=True)
class ChildStateSnapshot:
    state: str
    terminal: bool
    process_free: bool
    diagnostic: str | None = None


def _is_quiescent_publication_source(
    child: PaperBundleChildDescriptor,
) -> bool:
    return (
        child.state in {"completed", "failed"}
        and child.terminal
        and child.process_free
    )


def _can_reserve_child_publication(
    child: PaperBundleChildDescriptor,
) -> bool:
    return _is_quiescent_publication_source(child) or (
        child.state in {"running", "completing"}
        and not child.terminal
        and not child.process_free
    )


@dataclass(frozen=True)
class PaperBundleCancellationBarrier:
    job_id: str
    owner_id: str
    child_run_ids: tuple[str, ...]
    pending_creation: bool = False


@dataclass(frozen=True)
class ChildStartIntent:
    intent_id: str
    run_id: str
    state: Literal["claimed", "committed", "registered", "aborted", "revoked"]
    claimed_at: float
    updated_at: float
    expires_at: float


@dataclass(frozen=True)
class PaperBundlePublication:
    source_run_id: str
    publication_run_id: str
    artifact_id: str
    source_attempt: int
    source_candidate_id: str
    source_candidate_sha256: str
    generation: int
    published_at: float


@dataclass(frozen=True)
class PaperBundleCreationResult:
    record: "PaperBundleJobRecord"
    reused: bool

    def to_payload(self) -> dict[str, object]:
        """Serialize a public create response without internal diagnostics."""
        payload = self.record.to_payload()
        children = cast(dict[str, dict[str, object]], payload["children"])
        for artifact_type, child in self.record.children.items():
            children[artifact_type]["upload_token"] = child.upload_token
        payload["reused"] = self.reused
        return payload


@dataclass(frozen=True)
class PaperBundleJobRecord:
    job_id: str
    owner_id: str
    conversation_id: str
    source_name: str
    prompt_version: str
    state: PaperBundleState
    children: Mapping[str, PaperBundleChildDescriptor]
    request_digest: str
    idempotency_key_digest: str
    revision: int
    created_at: float
    updated_at: float
    terminal: bool = False
    terminal_at: float | None = None
    cancel_requested: bool = False
    cancel_requested_at: float | None = None
    completed_children: tuple[str, ...] = ()
    diagnostics: Mapping[str, str] = MappingProxyType({})
    start_intents: Mapping[str, ChildStartIntent] = MappingProxyType({})
    publications: Mapping[str, PaperBundlePublication] = MappingProxyType({})
    publication_generations: Mapping[str, int] = MappingProxyType(
        {artifact_type: 0 for artifact_type in _ARTIFACT_TYPES}
    )

    def to_payload(self) -> dict[str, object]:
        """Return a redacted JSON-safe GET/list response."""
        payload = PaperBundleJobStore._record_to_payload(self)
        children = cast(dict[str, dict[str, object]], payload["children"])
        for child in children.values():
            child.pop("upload_token", None)
            child.pop("diagnostic", None)
        payload.pop("diagnostics", None)
        payload.pop("idempotency_key_digest", None)
        payload.pop("start_intents", None)
        payload.pop("publication_generations", None)
        return payload


@dataclass(frozen=True)
class PaperBundlePublicationCommitResult:
    status: Literal["applied", "idempotent", "superseded"]
    record: PaperBundleJobRecord


ChildStatusProvider = Callable[[str], ChildStateSnapshot]
ChildReservationFactory = Callable[
    [str, str, str],
    Awaitable[PaperBundleChildDescriptor],
]
ChildCleanup = Callable[[str], Awaitable[None]]


class PaperBundleJobStore:
    """Own durable Paper Bundle records and their cross-process parent locks."""

    def __init__(self, jobs_dir: str | Path) -> None:
        self.jobs_dir = Path(jobs_dir)
        self.claim_lease_s = 30.0
        self.start_intent_ttl_s = 120.0
        if not _SECURE_DIR_FD_AVAILABLE:
            return
        if _WINDOWS_IO is not None:
            _WINDOWS_IO.assert_supported_parent(self.jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        root_fd = _open_directory_path(self.jobs_dir)
        try:
            self._root_identity = _directory_identity(root_fd)
            self._internal_identities: dict[str, tuple[int, int]] = {}
            for directory_name in (
                ".owner-locks",
                ".creation-claims",
                ".pending",
                ".quarantine",
            ):
                internal_fd = _open_directory_at(
                    root_fd,
                    directory_name,
                    create=True,
                )
                self._internal_identities[directory_name] = _directory_identity(
                    internal_fd
                )
                _close_directory(internal_fd)
        finally:
            _close_directory(root_fd)

    async def create_with_factory(
        self,
        *,
        owner_id: str,
        conversation_id: str,
        source_name: str,
        prompt_version: str,
        idempotency_key: str,
        request_digest: str,
        child_reservation_factory: ChildReservationFactory,
        cleanup_child: ChildCleanup,
        job_id: str | None = None,
    ) -> PaperBundleCreationResult:
        """Claim one idempotent creation before reserving any child run.

        The durable claim preassigns all five IDs.  Only its current claimant
        may invoke the factory; concurrent retries wait for or reuse the same
        result.  A stale claimant is cleaned by assigned run ID before a retry
        receives fresh IDs, including runs whose process died before returning
        a descriptor.
        """
        owner_id = self._validate_text(owner_id, "owner_id", maximum=512)
        conversation_id = self._validate_text(
            conversation_id, "conversation_id", maximum=512
        )
        source_name = self._validate_source_name(source_name)
        prompt_version = self._validate_text(prompt_version, "prompt_version", maximum=256)
        idempotency_key = self._validate_text(
            idempotency_key, "idempotency_key", maximum=256
        )
        request_digest = self._validate_digest(request_digest, "request_digest")
        if job_id is not None:
            job_id = self._validate_identifier(job_id, "job_id")
        if not callable(child_reservation_factory) or not callable(cleanup_child):
            raise InvalidPaperBundle("creation factory and cleanup callback are required")
        idempotency_digest = self._idempotency_digest(owner_id, idempotency_key)

        while True:
            decision = await asyncio.to_thread(
                self._claim_creation,
                owner_id,
                idempotency_digest,
                request_digest,
                job_id,
            )
            if decision["action"] == "committed":
                record = await asyncio.to_thread(
                    self.read_owned,
                    cast(str, decision["job_id"]),
                    owner_id,
                )
                return PaperBundleCreationResult(record=record, reused=True)
            if decision["action"] == "wait":
                await asyncio.sleep(cast(float, decision["wait_s"]))
                continue

            claimant_nonce = cast(str, decision["claimant_nonce"])
            assigned_runs = cast(dict[str, str], decision["assigned_runs"])
            cleanup_runs = cast(tuple[str, ...], decision["cleanup_runs"])
            if cleanup_runs:
                cleanup_heartbeat_stop = asyncio.Event()
                cleanup_heartbeat = asyncio.create_task(
                    self._creation_heartbeat(
                        owner_id,
                        idempotency_digest,
                        claimant_nonce,
                        cleanup_heartbeat_stop,
                    )
                )
                try:
                    await self._cleanup_assigned_runs(cleanup_runs, cleanup_child)
                except BaseException:
                    cleanup_heartbeat_stop.set()
                    await cleanup_heartbeat
                    await asyncio.to_thread(
                        self._release_cleanup_claim,
                        owner_id,
                        idempotency_digest,
                        claimant_nonce,
                    )
                    raise
                else:
                    cleanup_heartbeat_stop.set()
                    await cleanup_heartbeat
                decision = await asyncio.to_thread(
                    self._reset_creation_after_cleanup,
                    owner_id,
                    idempotency_digest,
                    claimant_nonce,
                )
                claimant_nonce = cast(str, decision["claimant_nonce"])
                assigned_runs = cast(dict[str, str], decision["assigned_runs"])

            heartbeat_stop = asyncio.Event()
            heartbeat = asyncio.create_task(
                self._creation_heartbeat(
                    owner_id,
                    idempotency_digest,
                    claimant_nonce,
                    heartbeat_stop,
                )
            )
            parent_committed = False
            try:
                descriptors: dict[str, PaperBundleChildDescriptor] = {}
                job_id = cast(str, decision["job_id"])
                for artifact_type in _ARTIFACT_TYPES:
                    descriptor = await child_reservation_factory(
                        artifact_type,
                        job_id,
                        assigned_runs[artifact_type],
                    )
                    if (
                        not isinstance(descriptor, PaperBundleChildDescriptor)
                        or descriptor.run_id != assigned_runs[artifact_type]
                        or descriptor.artifact_type != artifact_type
                    ):
                        raise InvalidPaperBundle(
                            "child reservation factory returned the wrong assigned identity"
                        )
                    descriptors[artifact_type] = descriptor
                    await asyncio.to_thread(
                        self._record_claim_child,
                        owner_id,
                        idempotency_digest,
                        claimant_nonce,
                        descriptor,
                    )
                self._validate_children(descriptors)
                commit_task = asyncio.create_task(
                    asyncio.to_thread(
                        self._commit_creation_claim,
                        owner_id,
                        idempotency_digest,
                        claimant_nonce,
                        conversation_id,
                        source_name,
                        prompt_version,
                        request_digest,
                        descriptors,
                    )
                )
                try:
                    record = await asyncio.shield(commit_task)
                except asyncio.CancelledError:
                    record = await commit_task
                    parent_committed = True
                    raise
                parent_committed = True
                return PaperBundleCreationResult(record=record, reused=False)
            except BaseException:
                if parent_committed:
                    raise
                published_record = await asyncio.to_thread(
                    self._read_record_if_present,
                    cast(str, decision["job_id"]),
                )
                if (
                    published_record is not None
                    and published_record.owner_id == owner_id
                    and published_record.idempotency_key_digest == idempotency_digest
                    and published_record.request_digest == request_digest
                ):
                    raise
                cleanup_task = asyncio.create_task(
                    self._cleanup_assigned_runs(tuple(assigned_runs.values()), cleanup_child)
                )
                cleanup_complete = False
                try:
                    await asyncio.shield(cleanup_task)
                    cleanup_complete = True
                except asyncio.CancelledError:
                    await cleanup_task
                    cleanup_complete = True
                    raise
                finally:
                    await asyncio.to_thread(
                        self._mark_creation_failed,
                        owner_id,
                        idempotency_digest,
                        claimant_nonce,
                        cleanup_complete,
                    )
                raise
            finally:
                heartbeat_stop.set()
                await heartbeat
                await asyncio.to_thread(
                    self._mark_creation_factory_quiesced,
                    owner_id,
                    idempotency_digest,
                    assigned_runs,
                )

    async def cancel_pending_creation(
        self,
        job_id: str,
        owner_id: str,
        *,
        cleanup_child: ChildCleanup,
    ) -> Literal["cancelled", "pending", "published", "not_found"]:
        """Tombstone an unpublished parent and clean every preassigned child.

        The caller should also cancel its in-memory factory task.  The durable
        tombstone prevents a late factory result or restarted claimant from
        publishing the parent even if process-local cancellation loses a race.
        """
        job_id = self._validate_identifier(job_id, "job_id")
        owner_id = self._validate_text(owner_id, "owner_id", maximum=512)
        if not callable(cleanup_child):
            raise InvalidPaperBundle("cleanup callback is required")
        while True:
            decision = await asyncio.to_thread(
                self._claim_pending_creation_cancel,
                job_id,
                owner_id,
            )
            action = cast(str, decision["action"])
            if action in {"published", "not_found"}:
                return cast(Literal["published", "not_found"], action)
            if action == "done":
                return "cancelled"
            if action == "pending":
                return "pending"
            if action == "wait":
                await asyncio.sleep(cast(float, decision["wait_s"]))
                continue
            claimant_nonce = cast(str, decision["claimant_nonce"])
            idempotency_digest = cast(str, decision["idempotency_digest"])
            assigned_runs = cast(tuple[str, ...], decision["assigned_runs"])
            heartbeat_stop = asyncio.Event()
            heartbeat = asyncio.create_task(
                self._creation_heartbeat(
                    owner_id,
                    idempotency_digest,
                    claimant_nonce,
                    heartbeat_stop,
                )
            )
            try:
                await self._cleanup_assigned_runs(assigned_runs, cleanup_child)
            except BaseException:
                heartbeat_stop.set()
                await heartbeat
                await asyncio.to_thread(
                    self._release_cleanup_claim,
                    owner_id,
                    idempotency_digest,
                    claimant_nonce,
                )
                raise
            heartbeat_stop.set()
            await heartbeat
            quiesced = await asyncio.to_thread(
                self._mark_cancel_cleanup_complete,
                owner_id,
                idempotency_digest,
                claimant_nonce,
            )
            return "cancelled" if quiesced else "pending"

    def confirm_pending_creation_quiesced(
        self,
        job_id: str,
        owner_id: str,
    ) -> bool:
        """Record a supervisor's proof that an unpublished factory is stopped."""
        job_id = self._validate_identifier(job_id, "job_id")
        owner_id = self._validate_text(owner_id, "owner_id", maximum=512)
        with self._owner_lock(owner_id):
            match = self._find_creation_claim_by_job_unlocked(owner_id, job_id)
            if match is None:
                return False
            idempotency_digest, claim = match
            if claim["state"] != "cancelled":
                return False
            if not claim["factory_quiesced"]:
                claim["factory_quiesced"] = True
                claim["updated_at"] = self._claim_timestamp(claim)
                self._write_claim_unlocked(owner_id, idempotency_digest, claim)
            return True

    async def _creation_heartbeat(
        self,
        owner_id: str,
        idempotency_digest: str,
        claimant_nonce: str,
        stop: asyncio.Event,
    ) -> None:
        interval = max(0.01, self.claim_lease_s / 3.0)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                renewed = await asyncio.to_thread(
                    self._renew_creation_claim,
                    owner_id,
                    idempotency_digest,
                    claimant_nonce,
                )
                if not renewed:
                    return

    @staticmethod
    async def _cleanup_assigned_runs(
        run_ids: tuple[str, ...],
        cleanup_child: ChildCleanup,
    ) -> None:
        results = await asyncio.gather(
            *(cleanup_child(run_id) for run_id in run_ids),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise PaperBundleError(
                f"failed to clean {len(failures)} child reservation(s)"
            ) from failures[0]

    def _claim_creation(
        self,
        owner_id: str,
        idempotency_digest: str,
        request_digest: str,
        requested_job_id: str | None,
    ) -> dict[str, object]:
        wall_now = time.time()
        with self._owner_lock(owner_id):
            claim = self._read_claim_unlocked(owner_id, idempotency_digest)
            if claim is None:
                with self._root_lock() as root_fd:
                    self._quarantine_safe_orphans_unlocked(root_fd)
                    if requested_job_id is not None:
                        published = self._read_record_if_present(requested_job_id)
                        if published is not None:
                            if (
                                published.owner_id == owner_id
                                and published.idempotency_key_digest
                                == idempotency_digest
                                and published.request_digest == request_digest
                            ):
                                return {
                                    "action": "committed",
                                    "job_id": requested_job_id,
                                }
                            raise PaperBundleConflict(
                                "requested parent job ID is already reserved"
                            )
                        reserved = self._find_creation_claim_by_job_global_unlocked(
                            requested_job_id
                        )
                        if reserved is not None:
                            raise PaperBundleConflict(
                                "requested parent job ID is already reserved"
                            )
                    claim = self._new_creation_claim(
                        owner_id,
                        idempotency_digest,
                        request_digest,
                        now=wall_now,
                        requested_job_id=requested_job_id,
                    )
                    self._write_claim_unlocked(owner_id, idempotency_digest, claim)
                return self._claim_decision(claim, action="acquired")
            with self._root_lock() as root_fd:
                self._quarantine_safe_orphans_unlocked(root_fd)
            if claim.get("request_digest") != request_digest:
                raise PaperBundleConflict(
                    "idempotency key was reused with different bundle metadata"
                )
            now = self._claim_timestamp(claim, wall_now)
            job_id = cast(str, claim["job_id"])
            record = self._read_record_if_present(job_id)
            if record is not None:
                if (
                    record.owner_id != owner_id
                    or record.idempotency_key_digest != idempotency_digest
                    or record.request_digest != request_digest
                ):
                    raise PaperBundleConflict("creation claim conflicts with its parent record")
                if claim.get("state") != "committed":
                    claim["state"] = "committed"
                    claim["updated_at"] = now
                    claim["lease_expires_at"] = 0.0
                    self._write_claim_unlocked(owner_id, idempotency_digest, claim)
                return {"action": "committed", "job_id": job_id}

            state = claim.get("state")
            lease_expires_at = float(claim.get("lease_expires_at", 0.0))
            if state == "cancelled":
                raise PaperBundleBarrierClosed(
                    f"paper bundle creation {job_id!r} was cancelled"
                )
            if state in {"creating", "cleanup_pending"} and self._lease_is_active(
                lease_expires_at,
                wall_now,
            ):
                return {
                    "action": "wait",
                    "wait_s": min(0.05, max(0.005, lease_expires_at - wall_now)),
                }
            if state == "failed":
                replacement = self._new_creation_claim(
                    owner_id,
                    idempotency_digest,
                    request_digest,
                    now=now,
                    lease_now=wall_now,
                    created_at=float(claim["created_at"]),
                    requested_job_id=cast(str, claim["job_id"]),
                    generation=int(claim.get("generation", 0)) + 1,
                )
                self._write_claim_unlocked(owner_id, idempotency_digest, replacement)
                return self._claim_decision(replacement, action="acquired")
            with self._root_lock() as root_fd:
                self._quarantine_pending_unlocked(
                    root_fd,
                    cast(str, claim["claimant_nonce"]),
                )
            claimant_nonce = uuid4().hex
            claim["state"] = "cleanup_pending"
            claim["claimant_nonce"] = claimant_nonce
            claim["lease_expires_at"] = wall_now + self.claim_lease_s
            claim["updated_at"] = now
            self._write_claim_unlocked(owner_id, idempotency_digest, claim)
            decision = self._claim_decision(claim, action="acquired")
            decision["cleanup_runs"] = tuple(
                cast(dict[str, str], claim["assigned_runs"]).values()
            )
            return decision

    def _claim_pending_creation_cancel(
        self,
        job_id: str,
        owner_id: str,
    ) -> dict[str, object]:
        record = self._read_record_if_present(job_id)
        if record is not None:
            return {
                "action": "published" if record.owner_id == owner_id else "not_found"
            }
        wall_now = time.time()
        with self._owner_lock(owner_id):
            match = self._find_creation_claim_by_job_unlocked(owner_id, job_id)
            if match is None:
                return {"action": "not_found"}
            idempotency_digest, claim = match
            now = self._claim_timestamp(claim, wall_now)
            record = self._read_record_if_present(job_id)
            if record is not None:
                return {
                    "action": "published"
                    if record.owner_id == owner_id
                    else "not_found"
                }
            if claim["state"] == "cancelled":
                if claim["tombstone_cleanup_complete"]:
                    return {
                        "action": "done" if claim["factory_quiesced"] else "pending"
                    }
                lease_expires_at = float(claim["lease_expires_at"])
                if self._lease_is_active(lease_expires_at, wall_now):
                    return {
                        "action": "wait",
                        "wait_s": min(
                            0.05,
                            max(0.005, lease_expires_at - wall_now),
                        ),
                    }
            else:
                with self._root_lock() as root_fd:
                    self._quarantine_pending_unlocked(
                        root_fd,
                        cast(str, claim["claimant_nonce"]),
                    )
            claimant_nonce = uuid4().hex
            claim["state"] = "cancelled"
            claim["claimant_nonce"] = claimant_nonce
            claim["lease_expires_at"] = wall_now + self.claim_lease_s
            claim["updated_at"] = now
            claim["tombstone_cleanup_complete"] = False
            self._write_claim_unlocked(owner_id, idempotency_digest, claim)
            return {
                "action": "acquired",
                "idempotency_digest": idempotency_digest,
                "claimant_nonce": claimant_nonce,
                "assigned_runs": tuple(
                    cast(dict[str, str], claim["assigned_runs"]).values()
                ),
            }

    def _find_creation_claim_by_job_unlocked(
        self,
        owner_id: str,
        job_id: str,
    ) -> tuple[str, dict[str, object]] | None:
        owner_digest = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
        prefix = f"{owner_digest}-"
        matches: list[tuple[str, dict[str, object]]] = []
        with self._internal_directory(".creation-claims") as claims_fd:
            for filename in _listdir(claims_fd):
                if not filename.startswith(prefix) or not filename.endswith(".json"):
                    continue
                idempotency_digest = filename[len(prefix) : -len(".json")]
                self._validate_digest(
                    idempotency_digest,
                    "claim idempotency digest",
                )
                payload = _read_json_at(claims_fd, filename)
                claim = self._validate_creation_claim(
                    payload,
                    owner_id=owner_id,
                    idempotency_digest=idempotency_digest,
                )
                if claim["job_id"] == job_id:
                    matches.append((idempotency_digest, claim))
        if len(matches) > 1:
            raise PaperBundleConflict("multiple creation claims use the same parent job ID")
        return matches[0] if matches else None

    def _find_creation_claim_by_job_global_unlocked(
        self,
        job_id: str,
    ) -> tuple[str, str, dict[str, object]] | None:
        matches: list[tuple[str, str, dict[str, object]]] = []
        with self._internal_directory(".creation-claims") as claims_fd:
            for filename in _listdir(claims_fd):
                match = _CLAIM_FILENAME_PATTERN.fullmatch(filename)
                if match is None:
                    continue
                payload = _read_json_at(claims_fd, filename)
                if not isinstance(payload, dict):
                    raise InvalidPaperBundle("malformed durable creation claim")
                owner_id = self._validate_text(
                    payload.get("owner_id"),
                    "claim owner_id",
                    maximum=512,
                )
                owner_digest = match.group("owner_digest")
                if hashlib.sha256(owner_id.encode("utf-8")).hexdigest() != owner_digest:
                    raise InvalidPaperBundle("creation claim filename owner is inconsistent")
                idempotency_digest = match.group("idempotency_digest")
                claim = self._validate_creation_claim(
                    payload,
                    owner_id=owner_id,
                    idempotency_digest=idempotency_digest,
                )
                if claim["job_id"] == job_id:
                    matches.append((owner_id, idempotency_digest, claim))
        if len(matches) > 1:
            raise PaperBundleConflict("multiple creation claims use the same parent job ID")
        return matches[0] if matches else None

    def _reset_creation_after_cleanup(
        self,
        owner_id: str,
        idempotency_digest: str,
        claimant_nonce: str,
    ) -> dict[str, object]:
        wall_now = time.time()
        with self._owner_lock(owner_id):
            claim = self._require_claimant_unlocked(
                owner_id,
                idempotency_digest,
                claimant_nonce,
                required_state="cleanup_pending",
            )
            now = self._claim_timestamp(claim, wall_now)
            replacement = self._new_creation_claim(
                owner_id,
                idempotency_digest,
                cast(str, claim["request_digest"]),
                now=now,
                lease_now=wall_now,
                created_at=float(claim["created_at"]),
                requested_job_id=cast(str, claim["job_id"]),
                generation=int(claim.get("generation", 0)) + 1,
            )
            self._write_claim_unlocked(owner_id, idempotency_digest, replacement)
            return self._claim_decision(replacement, action="acquired")

    def _release_cleanup_claim(
        self,
        owner_id: str,
        idempotency_digest: str,
        claimant_nonce: str,
    ) -> None:
        with self._owner_lock(owner_id):
            claim = self._read_claim_unlocked(owner_id, idempotency_digest)
            if (
                claim is None
                or claim.get("state") not in {"cleanup_pending", "cancelled"}
                or claim.get("claimant_nonce") != claimant_nonce
            ):
                return
            claim["lease_expires_at"] = 0.0
            claim["updated_at"] = self._claim_timestamp(claim)
            self._write_claim_unlocked(owner_id, idempotency_digest, claim)

    def _mark_cancel_cleanup_complete(
        self,
        owner_id: str,
        idempotency_digest: str,
        claimant_nonce: str,
    ) -> bool:
        with self._owner_lock(owner_id):
            claim = self._require_claimant_unlocked(
                owner_id,
                idempotency_digest,
                claimant_nonce,
                required_state="cancelled",
            )
            claim["tombstone_cleanup_complete"] = True
            claim["lease_expires_at"] = 0.0
            claim["updated_at"] = self._claim_timestamp(claim)
            self._write_claim_unlocked(owner_id, idempotency_digest, claim)
            return cast(bool, claim["factory_quiesced"])

    def _mark_creation_factory_quiesced(
        self,
        owner_id: str,
        idempotency_digest: str,
        assigned_runs: Mapping[str, str],
    ) -> None:
        with self._owner_lock(owner_id):
            claim = self._read_claim_unlocked(owner_id, idempotency_digest)
            if claim is None or cast(
                dict[str, str], claim["assigned_runs"]
            ) != dict(assigned_runs):
                return
            if claim["factory_quiesced"]:
                return
            claim["factory_quiesced"] = True
            claim["updated_at"] = self._claim_timestamp(claim)
            self._write_claim_unlocked(owner_id, idempotency_digest, claim)

    def _record_claim_child(
        self,
        owner_id: str,
        idempotency_digest: str,
        claimant_nonce: str,
        descriptor: PaperBundleChildDescriptor,
    ) -> None:
        with self._owner_lock(owner_id):
            claim = self._require_claimant_unlocked(
                owner_id,
                idempotency_digest,
                claimant_nonce,
                required_state="creating",
            )
            artifact_type = descriptor.artifact_type
            assigned_runs = cast(dict[str, str], claim["assigned_runs"])
            if assigned_runs.get(artifact_type) != descriptor.run_id:
                raise PaperBundleConflict("child descriptor does not match its durable claim")
            children = cast(dict[str, object], claim["children"])
            children[artifact_type] = self._child_to_payload(descriptor)
            wall_now = time.time()
            claim["updated_at"] = self._claim_timestamp(claim, wall_now)
            claim["lease_expires_at"] = wall_now + self.claim_lease_s
            self._write_claim_unlocked(owner_id, idempotency_digest, claim)

    def _commit_creation_claim(
        self,
        owner_id: str,
        idempotency_digest: str,
        claimant_nonce: str,
        conversation_id: str,
        source_name: str,
        prompt_version: str,
        request_digest: str,
        descriptors: Mapping[str, PaperBundleChildDescriptor],
    ) -> PaperBundleJobRecord:
        validated_children = self._validate_children(descriptors)
        with self._owner_lock(owner_id):
            claim = self._require_claimant_unlocked(
                owner_id,
                idempotency_digest,
                claimant_nonce,
                required_state="creating",
            )
            if claim["request_digest"] != request_digest:
                raise PaperBundleConflict("creation request digest changed while claimed")
            job_id = cast(str, claim["job_id"])
            now = self._claim_timestamp(claim)
            proposed = PaperBundleJobRecord(
                job_id=job_id,
                owner_id=owner_id,
                conversation_id=conversation_id,
                source_name=source_name,
                prompt_version=prompt_version,
                state="reserved",
                children=MappingProxyType(dict(validated_children)),
                request_digest=request_digest,
                idempotency_key_digest=idempotency_digest,
                revision=0,
                created_at=now,
                updated_at=now,
            )
            with self._root_lock() as root_fd:
                try:
                    existing_metadata = _stat_at(root_fd, job_id)
                except FileNotFoundError:
                    existing_metadata = None
                if existing_metadata is None:
                    try:
                        with self._internal_directory(".pending") as pending_fd:
                            pending_name = claimant_nonce
                            staging_fd = _open_directory_at(
                                pending_fd,
                                pending_name,
                                create=True,
                                exclusive=True,
                            )
                            try:
                                self._write_record_unlocked(proposed, staging_fd)
                            finally:
                                _close_directory(staging_fd)
                            _replace_at(
                                pending_fd,
                                pending_name,
                                root_fd,
                                job_id,
                            )
                            _fsync_directory(pending_fd)
                            _fsync_directory(root_fd)
                    except BaseException:
                        self._quarantine_pending_unlocked(root_fd, claimant_nonce)
                        raise
                elif not stat.S_ISDIR(existing_metadata.st_mode):
                    raise InvalidPaperBundle("paper bundle identity is not a directory")
            with self._parent_lock(job_id) as job_fd:
                record = self._read_record_unlocked(job_id, job_fd)
            if (
                record.owner_id != owner_id
                or record.idempotency_key_digest != idempotency_digest
                or record.request_digest != request_digest
            ):
                raise PaperBundleConflict("committed parent conflicts with creation claim")
            claim["state"] = "committed"
            claim["updated_at"] = self._claim_timestamp(claim)
            claim["lease_expires_at"] = 0.0
            self._write_claim_unlocked(owner_id, idempotency_digest, claim)
            return record

    def _mark_creation_failed(
        self,
        owner_id: str,
        idempotency_digest: str,
        claimant_nonce: str,
        cleanup_complete: bool,
    ) -> None:
        with self._owner_lock(owner_id):
            claim = self._read_claim_unlocked(owner_id, idempotency_digest)
            if claim is None or claim.get("claimant_nonce") != claimant_nonce:
                return
            if claim.get("state") == "committed":
                return
            if claim.get("state") == "cancelled":
                if cleanup_complete and not claim["tombstone_cleanup_complete"]:
                    claim["tombstone_cleanup_complete"] = True
                    claim["lease_expires_at"] = 0.0
                    claim["updated_at"] = self._claim_timestamp(claim)
                    self._write_claim_unlocked(owner_id, idempotency_digest, claim)
                return
            claim["state"] = "failed" if cleanup_complete else "cleanup_pending"
            claim["updated_at"] = self._claim_timestamp(claim)
            claim["lease_expires_at"] = 0.0
            self._write_claim_unlocked(owner_id, idempotency_digest, claim)

    def _renew_creation_claim(
        self,
        owner_id: str,
        idempotency_digest: str,
        claimant_nonce: str,
    ) -> bool:
        with self._owner_lock(owner_id):
            claim = self._read_claim_unlocked(owner_id, idempotency_digest)
            if (
                claim is None
                or claim.get("state")
                not in {"creating", "cleanup_pending", "cancelled"}
                or claim.get("claimant_nonce") != claimant_nonce
            ):
                return False
            wall_now = time.time()
            claim["lease_expires_at"] = wall_now + self.claim_lease_s
            claim["updated_at"] = self._claim_timestamp(claim, wall_now)
            self._write_claim_unlocked(owner_id, idempotency_digest, claim)
            return True

    def _new_creation_claim(
        self,
        owner_id: str,
        idempotency_digest: str,
        request_digest: str,
        *,
        now: float,
        lease_now: float | None = None,
        created_at: float | None = None,
        requested_job_id: str | None = None,
        generation: int = 0,
    ) -> dict[str, object]:
        job_id = requested_job_id or f"bundle-{uuid4().hex}"
        suffix = "" if generation == 0 else f"-g{generation}-{uuid4().hex[:8]}"
        return {
            "schema_version": 1,
            "owner_id": owner_id,
            "idempotency_key_digest": idempotency_digest,
            "request_digest": request_digest,
            "job_id": job_id,
            "assigned_runs": {
                artifact_type: f"{job_id}-{artifact_type}{suffix}"
                for artifact_type in _ARTIFACT_TYPES
            },
            "children": {},
            "state": "creating",
            "claimant_nonce": uuid4().hex,
            "lease_expires_at": (
                now if lease_now is None else lease_now
            )
            + self.claim_lease_s,
            "created_at": now if created_at is None else created_at,
            "updated_at": now,
            "generation": generation,
            "tombstone_cleanup_complete": False,
            "factory_quiesced": False,
        }

    @staticmethod
    def _claim_decision(claim: Mapping[str, object], *, action: str) -> dict[str, object]:
        return {
            "action": action,
            "job_id": claim["job_id"],
            "claimant_nonce": claim["claimant_nonce"],
            "assigned_runs": dict(cast(dict[str, str], claim["assigned_runs"])),
            "cleanup_runs": (),
        }

    def _require_claimant_unlocked(
        self,
        owner_id: str,
        idempotency_digest: str,
        claimant_nonce: str,
        *,
        required_state: str,
    ) -> dict[str, object]:
        claim = self._read_claim_unlocked(owner_id, idempotency_digest)
        if (
            claim is not None
            and claim.get("state") == "cancelled"
            and required_state != "cancelled"
        ):
            raise PaperBundleBarrierClosed(
                f"paper bundle creation {claim['job_id']!r} was cancelled"
            )
        if (
            claim is None
            or claim.get("state") != required_state
            or claim.get("claimant_nonce") != claimant_nonce
        ):
            raise PaperBundleConflict("creation claim is no longer owned by this caller")
        return claim

    def _claim_filename(self, owner_id: str, idempotency_digest: str) -> str:
        owner_digest = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
        return f"{owner_digest}-{idempotency_digest}.json"

    def _read_claim_unlocked(
        self,
        owner_id: str,
        idempotency_digest: str,
    ) -> dict[str, object] | None:
        with self._internal_directory(".creation-claims") as claims_fd:
            payload = _read_json_at(
                claims_fd,
                self._claim_filename(owner_id, idempotency_digest),
                missing_ok=True,
            )
        if payload is None:
            return None
        return self._validate_creation_claim(
            payload,
            owner_id=owner_id,
            idempotency_digest=idempotency_digest,
        )

    def _write_claim_unlocked(
        self,
        owner_id: str,
        idempotency_digest: str,
        claim: Mapping[str, object],
    ) -> None:
        validated = self._validate_creation_claim(
            dict(claim),
            owner_id=owner_id,
            idempotency_digest=idempotency_digest,
        )
        with self._internal_directory(".creation-claims") as claims_fd:
            _durable_write_json_at(
                claims_fd,
                self._claim_filename(owner_id, idempotency_digest),
                validated,
            )

    @classmethod
    def _validate_creation_claim(
        cls,
        payload: object,
        *,
        owner_id: str,
        idempotency_digest: str,
    ) -> dict[str, object]:
        expected_keys = {
            "schema_version",
            "owner_id",
            "idempotency_key_digest",
            "request_digest",
            "job_id",
            "assigned_runs",
            "children",
            "state",
            "claimant_nonce",
            "lease_expires_at",
            "created_at",
            "updated_at",
            "generation",
            "tombstone_cleanup_complete",
            "factory_quiesced",
        }
        if not isinstance(payload, dict) or set(payload) != expected_keys:
            raise InvalidPaperBundle("malformed durable creation claim")
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != 1
            or payload["owner_id"] != owner_id
            or payload["idempotency_key_digest"] != idempotency_digest
            or payload["state"]
            not in {"creating", "cleanup_pending", "failed", "committed", "cancelled"}
        ):
            raise InvalidPaperBundle("malformed durable creation claim")
        cls._validate_digest(idempotency_digest, "idempotency_key_digest")
        cls._validate_digest(payload["request_digest"], "claim request digest")
        cls._validate_identifier(payload["job_id"], "claimed job ID")
        cls._validate_identifier(payload["claimant_nonce"], "claimant nonce")
        assigned = payload["assigned_runs"]
        if not isinstance(assigned, dict) or set(assigned) != _ARTIFACT_SET:
            raise InvalidPaperBundle("creation claim has incomplete child identities")
        assigned_run_ids: set[str] = set()
        for artifact_type in _ARTIFACT_TYPES:
            run_id = cls._validate_identifier(
                assigned[artifact_type],
                f"{artifact_type} assigned run ID",
            )
            if run_id in assigned_run_ids:
                raise InvalidPaperBundle("creation claim child identities are duplicated")
            assigned_run_ids.add(run_id)
        children = payload["children"]
        if not isinstance(children, dict) or not set(children).issubset(_ARTIFACT_SET):
            raise InvalidPaperBundle("creation claim children are malformed")
        for artifact_type, child_payload in children.items():
            child = cls._child_from_payload(child_payload)
            if (
                child.artifact_type != artifact_type
                or child.run_id != assigned[artifact_type]
                or child.state != "reserved"
                or child.terminal
                or not child.process_free
            ):
                raise InvalidPaperBundle("creation claim child identity is inconsistent")
        created_at = cls._finite_number(payload["created_at"], "claim created_at")
        updated_at = cls._finite_number(payload["updated_at"], "claim updated_at")
        lease_expires_at = cls._finite_number(
            payload["lease_expires_at"],
            "claim lease_expires_at",
        )
        if created_at > updated_at or lease_expires_at < 0:
            raise InvalidPaperBundle("creation claim timestamps are out of order")
        generation = payload["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise InvalidPaperBundle("creation claim generation is invalid")
        if not isinstance(payload["tombstone_cleanup_complete"], bool):
            raise InvalidPaperBundle("creation claim cleanup marker is invalid")
        if not isinstance(payload["factory_quiesced"], bool):
            raise InvalidPaperBundle("creation claim factory marker is invalid")
        if payload["state"] != "cancelled" and payload["tombstone_cleanup_complete"]:
            raise InvalidPaperBundle("non-cancelled creation claim has a cleanup tombstone")
        if payload["state"] == "cancelled" and payload["tombstone_cleanup_complete"]:
            if lease_expires_at != 0.0:
                raise InvalidPaperBundle("clean cancellation tombstone retains a lease")
        if payload["state"] == "committed" and lease_expires_at != 0.0:
            raise InvalidPaperBundle("committed creation claim retains a lease")
        if payload["state"] == "committed" and set(children) != _ARTIFACT_SET:
            raise InvalidPaperBundle("committed creation claim has incomplete children")
        return dict(payload)

    def _read_record_if_present(self, job_id: str) -> PaperBundleJobRecord | None:
        try:
            with self._parent_lock(job_id) as job_fd:
                return self._read_record_unlocked(job_id, job_fd)
        except (PaperBundleNotFound, FileNotFoundError):
            return None

    def _quarantine_safe_orphans_unlocked(
        self,
        root_fd: DirectoryHandle,
    ) -> None:
        with self._internal_directory(".quarantine") as quarantine_fd:
            names = tuple(_listdir(root_fd))
            for name in names:
                if name.startswith("."):
                    continue
                try:
                    validate_run_id(name)
                except RunControlError:
                    continue
                try:
                    candidate_fd = _open_directory_at(root_fd, name)
                except (FileNotFoundError, InvalidPaperBundle):
                    continue
                try:
                    entries = set(_listdir(candidate_fd))
                    if "paper_bundle_job.json" in entries:
                        continue
                    if not entries.issubset({".paper_bundle_job.lock"}):
                        continue
                    if ".paper_bundle_job.lock" in entries:
                        _validate_regular_file_at(
                            candidate_fd,
                            ".paper_bundle_job.lock",
                        )
                    opened = _directory_identity(candidate_fd)
                    current = _stat_at(root_fd, name)
                    if opened != (current.st_dev, current.st_ino):
                        raise InvalidPaperBundle("orphan directory changed during quarantine")
                    destination = f"{name}-{uuid4().hex}"
                    _replace_at(
                        root_fd,
                        name,
                        quarantine_fd,
                        destination,
                    )
                    _fsync_directory(root_fd)
                    _fsync_directory(quarantine_fd)
                finally:
                    _close_directory(candidate_fd)

    def _quarantine_pending_unlocked(
        self,
        root_fd: DirectoryHandle,
        pending_name: str,
    ) -> None:
        self._validate_identifier(pending_name, "pending creation identity")
        with self._internal_directory(".pending") as pending_fd:
            try:
                staging_fd = _open_directory_at(pending_fd, pending_name)
            except FileNotFoundError:
                return
            try:
                entries = set(_listdir(staging_fd))
                temporary_entries = entries - {"paper_bundle_job.json"}
                if any(
                    _PENDING_RECORD_TEMP_PATTERN.fullmatch(entry) is None
                    for entry in temporary_entries
                ):
                    raise InvalidPaperBundle(
                        "pending creation directory is not safe to quarantine"
                    )
                if "paper_bundle_job.json" in entries:
                    _validate_regular_file_at(staging_fd, "paper_bundle_job.json")
                for temporary_entry in temporary_entries:
                    _validate_regular_file_at(staging_fd, temporary_entry)
                opened = _directory_identity(staging_fd)
                current = _stat_at(pending_fd, pending_name)
                if opened != (current.st_dev, current.st_ino):
                    raise InvalidPaperBundle("pending creation directory changed")
            finally:
                _close_directory(staging_fd)
            with self._internal_directory(".quarantine") as quarantine_fd:
                destination = f"pending-{pending_name}-{uuid4().hex}"
                _replace_at(
                    pending_fd,
                    pending_name,
                    quarantine_fd,
                    destination,
                )
                _fsync_directory(pending_fd)
                _fsync_directory(quarantine_fd)
                _fsync_directory(root_fd)

    @contextmanager
    def _portable_root_anchor_lock(self) -> Iterator[None]:
        if not _SECURE_DIR_FD_AVAILABLE:
            raise InvalidPaperBundle(
                "Paper Bundle writes require stable native directory handles"
            )
        yield

    @contextmanager
    def _root_directory(self) -> Iterator[DirectoryHandle]:
        descriptor = _open_directory_path(self.jobs_dir)
        try:
            if _directory_identity(descriptor) != self._root_identity:
                raise InvalidPaperBundle("paper bundle root was replaced")
            yield descriptor
            current = _stat_path(self.jobs_dir)
            if (
                not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) != self._root_identity
            ):
                raise InvalidPaperBundle("paper bundle root changed during operation")
        finally:
            _close_directory(descriptor)

    @contextmanager
    def _internal_directory(self, name: str) -> Iterator[DirectoryHandle]:
        with self._root_directory() as root_fd:
            descriptor = _open_directory_at(root_fd, name)
            try:
                expected_identity = self._internal_identities.get(name)
                if (
                    expected_identity is None
                    or _directory_identity(descriptor) != expected_identity
                ):
                    raise InvalidPaperBundle(
                        f"paper bundle internal directory was replaced: {name}"
                    )
                yield descriptor
                current = _stat_at(root_fd, name)
                if (
                    not stat.S_ISDIR(current.st_mode)
                    or (current.st_dev, current.st_ino) != expected_identity
                ):
                    raise InvalidPaperBundle(
                        f"paper bundle internal directory changed: {name}"
                    )
            finally:
                _close_directory(descriptor)

    @contextmanager
    def _root_lock(self) -> Iterator[DirectoryHandle]:
        with self._portable_root_anchor_lock():
            with self._root_directory() as root_fd:
                with _locked_file_at(
                    root_fd,
                    ".paper_bundle_root.lock",
                    self._root_identity,
                ):
                    yield root_fd

    def read_owned(
        self,
        job_id: str,
        owner_id: str,
        *,
        child_status_provider: ChildStatusProvider | None = None,
    ) -> PaperBundleJobRecord:
        """Read one owner-visible job and optionally reconcile child state first."""
        if child_status_provider is not None:
            return self.reconcile(job_id, owner_id, child_status_provider)
        job_id = self._validate_identifier(job_id, "job_id")
        owner_id = self._validate_text(owner_id, "owner_id", maximum=512)
        with self._parent_lock(job_id) as job_fd:
            return self._read_owned_unlocked(job_id, owner_id, job_fd)

    def list_owned(
        self,
        owner_id: str,
        *,
        child_status_provider: ChildStatusProvider | None = None,
    ) -> tuple[PaperBundleJobRecord, ...]:
        """Return durable owner history for browser reconstruction."""
        owner_id = self._validate_text(owner_id, "owner_id", maximum=512)
        records: list[PaperBundleJobRecord] = []
        with self._root_directory() as root_fd:
            candidate_names = tuple(_listdir(root_fd))
        for candidate_name in candidate_names:
            if candidate_name.startswith("."):
                continue
            try:
                validate_run_id(candidate_name)
                with self._parent_lock(candidate_name) as job_fd:
                    record = self._read_record_unlocked(candidate_name, job_fd)
            except (PaperBundleError, RunControlError, OSError):
                continue
            if record.owner_id == owner_id:
                if child_status_provider is not None and not record.terminal:
                    record = self.reconcile(
                        record.job_id,
                        owner_id,
                        child_status_provider,
                    )
                records.append(record)
        records.sort(key=lambda item: (item.created_at, item.job_id), reverse=True)
        return tuple(records)

    def reconcile_all(
        self,
        child_status_provider: ChildStatusProvider,
    ) -> tuple[PaperBundleJobRecord, ...]:
        """Recover every valid published parent for backend startup/reaping."""
        if not callable(child_status_provider):
            raise InvalidPaperBundle("child_status_provider must be callable")
        records: list[PaperBundleJobRecord] = []
        with self._root_directory() as root_fd:
            candidate_names = tuple(_listdir(root_fd))
        for candidate_name in candidate_names:
            if candidate_name.startswith("."):
                continue
            try:
                validate_run_id(candidate_name)
                with self._parent_lock(candidate_name) as job_fd:
                    record = self._read_record_unlocked(candidate_name, job_fd)
            except (PaperBundleError, RunControlError, OSError):
                continue
            if not record.terminal:
                record = self.reconcile(
                    record.job_id,
                    record.owner_id,
                    child_status_provider,
                )
            records.append(record)
        records.sort(key=lambda item: item.job_id)
        return tuple(records)

    def recover_cancellation_barriers_after_restart(
        self,
    ) -> tuple[PaperBundleCancellationBarrier, ...]:
        """Return child barriers that must win before run recovery.

        A restarted process proves that unpublished factories from the previous
        process are quiescent. Their cleanup lease is released here so startup
        can finish child cancellation after run controls have been recovered.
        """
        barriers: list[PaperBundleCancellationBarrier] = []
        with self._root_directory() as root_fd:
            candidate_names = tuple(_listdir(root_fd))
        for candidate_name in candidate_names:
            if candidate_name.startswith("."):
                continue
            try:
                validate_run_id(candidate_name)
                with self._parent_lock(candidate_name) as job_fd:
                    record = self._read_record_unlocked(candidate_name, job_fd)
            except (PaperBundleError, RunControlError, OSError):
                continue
            if record.cancel_requested and not record.terminal:
                barriers.append(PaperBundleCancellationBarrier(
                    job_id=record.job_id,
                    owner_id=record.owner_id,
                    child_run_ids=tuple(
                        record.children[artifact_type].run_id
                        for artifact_type in _ARTIFACT_TYPES
                    ),
                ))

        with self._internal_directory(".creation-claims") as claims_fd:
            claim_names = tuple(_listdir(claims_fd))
        for filename in claim_names:
            match = _CLAIM_FILENAME_PATTERN.fullmatch(filename)
            if match is None:
                continue
            with self._internal_directory(".creation-claims") as claims_fd:
                payload = _read_json_at(claims_fd, filename)
            if not isinstance(payload, dict):
                continue
            try:
                owner_id = self._validate_text(
                    payload.get("owner_id"),
                    "claim owner_id",
                    maximum=512,
                )
                if hashlib.sha256(owner_id.encode("utf-8")).hexdigest() != match.group(
                    "owner_digest"
                ):
                    continue
                idempotency_digest = match.group("idempotency_digest")
                with self._owner_lock(owner_id):
                    claim = self._read_claim_unlocked(owner_id, idempotency_digest)
                    if claim is None or claim["state"] != "cancelled":
                        continue
                    if (
                        claim["tombstone_cleanup_complete"]
                        and claim["factory_quiesced"]
                    ):
                        continue
                    if not claim["factory_quiesced"] or claim["lease_expires_at"] != 0.0:
                        claim["factory_quiesced"] = True
                        claim["lease_expires_at"] = 0.0
                        claim["updated_at"] = self._claim_timestamp(claim)
                        self._write_claim_unlocked(owner_id, idempotency_digest, claim)
            except (PaperBundleError, RunControlError, OSError):
                continue
            assigned_runs = cast(dict[str, str], claim["assigned_runs"])
            barriers.append(PaperBundleCancellationBarrier(
                job_id=cast(str, claim["job_id"]),
                owner_id=owner_id,
                child_run_ids=tuple(
                    assigned_runs[artifact_type] for artifact_type in _ARTIFACT_TYPES
                ),
                pending_creation=True,
            ))
        barriers.sort(key=lambda item: (item.job_id, item.pending_creation))
        return tuple(barriers)

    def request_cancel(
        self,
        job_id: str,
        owner_id: str,
        *,
        expected_revision: int | None = None,
    ) -> PaperBundleJobRecord:
        """Close the parent barrier before child cancellation is dispatched."""
        job_id = self._validate_identifier(job_id, "job_id")
        owner_id = self._validate_text(owner_id, "owner_id", maximum=512)
        with self._parent_lock(job_id) as job_fd:
            current = self._read_owned_unlocked(job_id, owner_id, job_fd)
            if current.state in _PARENT_TERMINAL_STATES or current.state == "cancelling":
                return current
            self._assert_revision(current, expected_revision)
            now = self._record_timestamp(current)
            intents = {
                run_id: (
                    replace(intent, state="revoked", updated_at=now)
                    if intent.state == "claimed"
                    else intent
                )
                for run_id, intent in current.start_intents.items()
            }
            updated = replace(
                current,
                state="cancelling",
                revision=current.revision + 1,
                updated_at=now,
                cancel_requested=True,
                cancel_requested_at=now,
                start_intents=MappingProxyType(intents),
            )
            self._write_record_unlocked(updated, job_fd)
            return updated

    def reserve_child_publication(
        self,
        job_id: str,
        owner_id: str,
        artifact_type: str,
        source_run_id: str,
    ) -> int:
        """Allocate one monotonic publication generation under the parent lock."""
        job_id = self._validate_identifier(job_id, "job_id")
        owner_id = self._validate_text(owner_id, "owner_id", maximum=512)
        artifact_type = self._validate_artifact_type(artifact_type)
        source_run_id = self._validate_identifier(source_run_id, "source_run_id")
        with self._parent_lock(job_id) as job_fd:
            current = self._read_owned_unlocked(job_id, owner_id, job_fd)
            child = current.children[artifact_type]
            if child.run_id != source_run_id:
                raise PaperBundleConflict(
                    "publication source does not match the bundle child"
                )
            if (
                current.cancel_requested
                or current.state in {"cancelling", "cancelled"}
            ):
                raise PaperBundleBarrierClosed(
                    "paper bundle no longer accepts child publication"
                )
            if not _can_reserve_child_publication(child):
                raise PaperBundleConflict(
                    "publication source child cannot accept a publication intent"
                )
            generation = current.publication_generations[artifact_type] + 1
            generations = dict(current.publication_generations)
            generations[artifact_type] = generation
            now = self._record_timestamp(current)
            updated = replace(
                current,
                publication_generations=MappingProxyType(generations),
                revision=current.revision + 1,
                updated_at=now,
            )
            self._write_record_unlocked(updated, job_fd)
            return generation

    def commit_child_publication(
        self,
        job_id: str,
        owner_id: str,
        artifact_type: str,
        source_run_id: str,
        *,
        publication_run_id: str,
        artifact_id: str,
        source_attempt: int,
        source_candidate_id: str,
        source_candidate_sha256: str,
        generation: int,
    ) -> PaperBundlePublicationCommitResult:
        """Commit a reserved derived publication with generation ordering."""
        job_id = self._validate_identifier(job_id, "job_id")
        owner_id = self._validate_text(owner_id, "owner_id", maximum=512)
        artifact_type = self._validate_artifact_type(artifact_type)
        source_run_id = self._validate_identifier(source_run_id, "source_run_id")
        publication_run_id = self._validate_identifier(
            publication_run_id, "publication_run_id"
        )
        artifact_id = self._validate_identifier(artifact_id, "artifact_id")
        source_candidate_id = self._validate_identifier(
            source_candidate_id, "source_candidate_id"
        )
        source_candidate_sha256 = self._validate_digest(
            source_candidate_sha256, "source_candidate_sha256"
        )
        if (
            isinstance(source_attempt, bool)
            or not isinstance(source_attempt, int)
            or source_attempt <= 0
        ):
            raise InvalidPaperBundle("source_attempt must be a positive integer")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise InvalidPaperBundle("publication generation must be a positive integer")
        with self._parent_lock(job_id) as job_fd:
            current = self._read_owned_unlocked(job_id, owner_id, job_fd)
            child = current.children[artifact_type]
            if child.run_id != source_run_id:
                raise PaperBundleConflict(
                    "publication source does not match the bundle child"
                )
            if publication_run_id == source_run_id:
                raise PaperBundleConflict(
                    "publication run must differ from its source run"
                )
            for other_artifact_type, other in current.publications.items():
                if other_artifact_type == artifact_type:
                    continue
                if other.publication_run_id == publication_run_id:
                    raise PaperBundleConflict("publication run ID is already in use")
                if other.artifact_id == artifact_id:
                    raise PaperBundleConflict("publication artifact ID is already in use")
            allocated_generation = current.publication_generations[artifact_type]
            if generation > allocated_generation:
                raise PaperBundleConflict("publication generation was not reserved")
            if generation < allocated_generation:
                return PaperBundlePublicationCommitResult(
                    status="superseded",
                    record=current,
                )
            committed = current.publications.get(artifact_type)
            if committed is not None and generation == committed.generation:
                if self._publication_matches(
                    committed,
                    source_run_id=source_run_id,
                    publication_run_id=publication_run_id,
                    artifact_id=artifact_id,
                    source_attempt=source_attempt,
                    source_candidate_id=source_candidate_id,
                    source_candidate_sha256=source_candidate_sha256,
                    generation=generation,
                ):
                    return PaperBundlePublicationCommitResult(
                        status="idempotent",
                        record=current,
                    )
                raise PaperBundleConflict(
                    "publication generation conflicts with committed lineage"
                )
            if committed is not None and generation < committed.generation:
                return PaperBundlePublicationCommitResult(
                    status="superseded",
                    record=current,
                )
            if current.cancel_requested or current.state in {"cancelling", "cancelled"}:
                raise PaperBundleBarrierClosed(
                    "paper bundle cancellation won the publication race"
                )
            if not _is_quiescent_publication_source(child):
                raise PaperBundleConflict(
                    "publication source child is not quiescent and publishable"
                )
            now = self._record_timestamp(current)
            publication = PaperBundlePublication(
                source_run_id=source_run_id,
                publication_run_id=publication_run_id,
                artifact_id=artifact_id,
                source_attempt=source_attempt,
                source_candidate_id=source_candidate_id,
                source_candidate_sha256=source_candidate_sha256,
                generation=generation,
                published_at=now,
            )
            publications = dict(current.publications)
            publications[artifact_type] = publication
            state, terminal, completed_children = self._derive_parent_lifecycle(
                children=current.children,
                publications=publications,
                cancel_requested=current.cancel_requested,
                start_intents=current.start_intents,
            )
            updated = replace(
                current,
                state=state,
                terminal=terminal,
                terminal_at=(
                    current.terminal_at
                    if terminal and current.terminal_at is not None
                    else now if terminal else None
                ),
                completed_children=completed_children,
                publications=MappingProxyType(publications),
                revision=current.revision + 1,
                updated_at=now,
            )
            self._write_record_unlocked(updated, job_fd)
            return PaperBundlePublicationCommitResult(status="applied", record=updated)

    def reconcile(
        self,
        job_id: str,
        owner_id: str,
        child_status_provider: ChildStatusProvider,
    ) -> PaperBundleJobRecord:
        """Converge the parent from four authoritative child snapshots."""
        job_id = self._validate_identifier(job_id, "job_id")
        owner_id = self._validate_text(owner_id, "owner_id", maximum=512)
        if not callable(child_status_provider):
            raise InvalidPaperBundle("child_status_provider must be callable")
        for _ in range(16):
            with self._parent_lock(job_id) as job_fd:
                current = self._read_owned_unlocked(job_id, owner_id, job_fd)
                if current.terminal:
                    return current
                current = self._expire_start_intents_unlocked(current, job_fd=job_fd)
                expected_revision = current.revision
                child_ids = {
                    artifact_type: current.children[artifact_type].run_id
                    for artifact_type in _ARTIFACT_TYPES
                }
            snapshots: dict[str, ChildStateSnapshot] = {}
            for artifact_type in _ARTIFACT_TYPES:
                snapshot = self._validate_snapshot(
                    child_status_provider(child_ids[artifact_type])
                )
                snapshots[artifact_type] = snapshot
            with self._parent_lock(job_id) as job_fd:
                latest = self._read_owned_unlocked(job_id, owner_id, job_fd)
                if latest.terminal:
                    return latest
                if latest.revision != expected_revision:
                    continue
                return self._apply_child_snapshots_unlocked(latest, snapshots, job_fd)
        raise StalePaperBundleRevision("parent changed repeatedly during reconciliation")

    def _apply_child_snapshots_unlocked(
        self,
        current: PaperBundleJobRecord,
        snapshots: Mapping[str, ChildStateSnapshot],
        job_fd: DirectoryHandle,
    ) -> PaperBundleJobRecord:
        updated_children: dict[str, PaperBundleChildDescriptor] = {}
        diagnostics: dict[str, str] = {}
        for artifact_type in _ARTIFACT_TYPES:
            child = current.children[artifact_type]
            snapshot = snapshots[artifact_type]
            updated_children[artifact_type] = replace(
                child,
                state=snapshot.state,
                terminal=snapshot.terminal,
                process_free=snapshot.process_free,
                diagnostic=snapshot.diagnostic,
            )
            if snapshot.diagnostic:
                diagnostics[artifact_type] = snapshot.diagnostic
        now = self._record_timestamp(current)
        resolved_intents = dict(current.start_intents)
        child_artifact_by_run_id = {
            child.run_id: artifact_type
            for artifact_type, child in current.children.items()
        }
        for run_id, intent in tuple(resolved_intents.items()):
            artifact_type = child_artifact_by_run_id.get(run_id)
            snapshot = snapshots.get(artifact_type) if artifact_type is not None else None
            if (
                intent.state == "committed"
                and intent.expires_at <= now
                and snapshot is not None
                and snapshot.terminal
                and snapshot.process_free
            ):
                resolved_intents[run_id] = replace(
                    intent,
                    state="aborted",
                    updated_at=now,
                )
        target, terminal, completed_children = self._derive_parent_lifecycle(
            children=updated_children,
            publications=current.publications,
            cancel_requested=current.cancel_requested,
            start_intents=resolved_intents,
        )
        if (
            current.state == target
            and dict(current.children) == updated_children
            and current.completed_children == completed_children
            and dict(current.diagnostics) == diagnostics
            and dict(current.start_intents) == resolved_intents
            and current.terminal == terminal
        ):
            return current
        updated = replace(
            current,
            state=target,
            children=MappingProxyType(updated_children),
            revision=current.revision + 1,
            updated_at=now,
            terminal=terminal,
            terminal_at=now if terminal else None,
            completed_children=completed_children,
            diagnostics=MappingProxyType(diagnostics),
            start_intents=MappingProxyType(resolved_intents),
        )
        self._write_record_unlocked(updated, job_fd)
        return updated

    def claim_child_start(
        self,
        job_id: str,
        run_id: str,
        owner_id: str,
        *,
        intent_id: str | None = None,
        expires_at: float | None = None,
    ) -> ChildStartIntent:
        """Persist a short start intent without holding a lock across awaits."""
        job_id = self._validate_identifier(job_id, "job_id")
        run_id = self._validate_identifier(run_id, "run_id")
        owner_id = self._validate_text(owner_id, "owner_id", maximum=512)
        intent_id = uuid4().hex if intent_id is None else self._validate_identifier(
            intent_id, "intent_id"
        )
        wall_now = time.time()
        with self._parent_lock(job_id) as job_fd:
            current = self._read_owned_unlocked(job_id, owner_id, job_fd)
            now = self._record_timestamp(current, wall_now)
            deadline = (
                now + self.start_intent_ttl_s
                if expires_at is None
                else float(expires_at)
            )
            if not math.isfinite(deadline) or deadline <= now:
                raise InvalidPaperBundle("start intent expiry must be in the future")
            current = self._expire_start_intents_unlocked(
                current,
                now=now,
                job_fd=job_fd,
            )
            if not any(child.run_id == run_id for child in current.children.values()):
                raise PaperBundleNotFound(job_id)
            existing = current.start_intents.get(run_id)
            if existing is not None and existing.intent_id == intent_id:
                return existing
            if existing is not None and existing.state in {
                "claimed",
                "committed",
                "registered",
            }:
                raise PaperBundleConflict("child already has an in-flight start intent")
            if current.state not in {"reserved", "running"} or current.cancel_requested:
                raise PaperBundleBarrierClosed(
                    f"paper bundle {job_id!r} no longer accepts child starts"
                )
            intent = ChildStartIntent(
                intent_id=intent_id,
                run_id=run_id,
                state="claimed",
                claimed_at=now,
                updated_at=now,
                expires_at=deadline,
            )
            intents = dict(current.start_intents)
            intents[run_id] = intent
            updated = replace(
                current,
                start_intents=MappingProxyType(intents),
                revision=current.revision + 1,
                updated_at=now,
            )
            self._write_record_unlocked(updated, job_fd)
            return intent

    def commit_child_start(
        self,
        job_id: str,
        run_id: str,
        owner_id: str,
        intent_id: str,
    ) -> ChildStartIntent:
        """Linearize a claimed start against parent cancellation."""
        job_id = self._validate_identifier(job_id, "job_id")
        run_id = self._validate_identifier(run_id, "run_id")
        owner_id = self._validate_text(owner_id, "owner_id", maximum=512)
        intent_id = self._validate_identifier(intent_id, "intent_id")
        with self._parent_lock(job_id) as job_fd:
            current = self._read_owned_unlocked(job_id, owner_id, job_fd)
            current = self._expire_start_intents_unlocked(
                current,
                now=self._record_timestamp(current),
                job_fd=job_fd,
            )
            intent = current.start_intents.get(run_id)
            if intent is None or intent.intent_id != intent_id:
                raise PaperBundleConflict("start intent is missing or was superseded")
            if intent.state == "committed":
                return intent
            if intent.state != "claimed":
                raise PaperBundleBarrierClosed("start intent can no longer commit")
            now = self._record_timestamp(current)
            intents = dict(current.start_intents)
            if current.cancel_requested or current.state not in {"reserved", "running"}:
                intents[run_id] = replace(intent, state="revoked", updated_at=now)
                updated = replace(
                    current,
                    start_intents=MappingProxyType(intents),
                    revision=current.revision + 1,
                    updated_at=now,
                )
                self._write_record_unlocked(updated, job_fd)
                raise PaperBundleBarrierClosed("parent cancellation won the start race")
            committed = replace(intent, state="committed", updated_at=now)
            intents[run_id] = committed
            updated = replace(
                current,
                start_intents=MappingProxyType(intents),
                revision=current.revision + 1,
                updated_at=now,
            )
            self._write_record_unlocked(updated, job_fd)
            return committed

    def resolve_child_start(
        self,
        job_id: str,
        run_id: str,
        owner_id: str,
        intent_id: str,
        outcome: Literal["registered", "aborted"],
    ) -> PaperBundleJobRecord:
        """Resolve a claimed start after the child registration await completes."""
        job_id = self._validate_identifier(job_id, "job_id")
        run_id = self._validate_identifier(run_id, "run_id")
        owner_id = self._validate_text(owner_id, "owner_id", maximum=512)
        intent_id = self._validate_identifier(intent_id, "intent_id")
        if outcome not in {"registered", "aborted"}:
            raise InvalidPaperBundle("start intent outcome must be registered or aborted")
        with self._parent_lock(job_id) as job_fd:
            current = self._read_owned_unlocked(job_id, owner_id, job_fd)
            intent = current.start_intents.get(run_id)
            if intent is None or intent.intent_id != intent_id:
                raise PaperBundleConflict("start intent is missing or was superseded")
            if intent.state == outcome:
                return current
            if outcome == "registered" and (
                current.cancel_requested
                or current.terminal
                or intent.state in {"aborted", "revoked"}
            ):
                raise PaperBundleBarrierClosed(
                    "parent cancellation closed the child registration barrier"
                )
            if outcome == "aborted" and intent.state == "revoked":
                return current
            if outcome == "registered" and intent.state != "committed":
                raise PaperBundleConflict("uncommitted start cannot register")
            if outcome == "aborted" and intent.state not in {"claimed", "committed"}:
                raise PaperBundleConflict("start intent is already resolved")
            now = self._record_timestamp(current)
            intents = dict(current.start_intents)
            intents[run_id] = replace(
                intent,
                state=outcome,
                updated_at=now,
            )
            updated = replace(
                current,
                start_intents=MappingProxyType(intents),
                revision=current.revision + 1,
                updated_at=now,
            )
            self._write_record_unlocked(updated, job_fd)
            return updated

    def pending_child_start_intents(
        self,
        job_id: str,
        owner_id: str,
    ) -> tuple[ChildStartIntent, ...]:
        job_id = self._validate_identifier(job_id, "job_id")
        owner_id = self._validate_text(owner_id, "owner_id", maximum=512)
        with self._parent_lock(job_id) as job_fd:
            current = self._read_owned_unlocked(job_id, owner_id, job_fd)
            current = self._expire_start_intents_unlocked(current, job_fd=job_fd)
            return tuple(
                intent
                for intent in current.start_intents.values()
                if intent.state == "committed"
            )

    def _expire_start_intents_unlocked(
        self,
        current: PaperBundleJobRecord,
        *,
        now: float | None = None,
        job_fd: DirectoryHandle,
    ) -> PaperBundleJobRecord:
        timestamp = self._record_timestamp(current, now)
        intents = dict(current.start_intents)
        changed = False
        for run_id, intent in tuple(intents.items()):
            if intent.state == "claimed" and intent.expires_at <= timestamp:
                intents[run_id] = replace(
                    intent,
                    state="revoked",
                    updated_at=timestamp,
                )
                changed = True
        if not changed:
            return current
        updated = replace(
            current,
            start_intents=MappingProxyType(intents),
            revision=current.revision + 1,
            updated_at=timestamp,
        )
        self._write_record_unlocked(updated, job_fd)
        return updated

    def assert_child_may_upload_or_start(
        self,
        job_id: str,
        run_id: str,
        owner_id: str,
    ) -> PaperBundleJobRecord:
        """Check that a child may upload; starts use the intent protocol."""
        job_id = self._validate_identifier(job_id, "job_id")
        run_id = self._validate_identifier(run_id, "run_id")
        owner_id = self._validate_text(owner_id, "owner_id", maximum=512)
        with self._parent_lock(job_id) as job_fd:
            record = self._read_owned_unlocked(job_id, owner_id, job_fd)
            if not any(child.run_id == run_id for child in record.children.values()):
                raise PaperBundleNotFound(job_id)
            if record.state not in {"reserved", "running"} or record.cancel_requested:
                raise PaperBundleBarrierClosed(
                    f"paper bundle {job_id!r} no longer accepts child work"
                )
            return record

    def _read_owned_unlocked(
        self,
        job_id: str,
        owner_id: str,
        job_fd: DirectoryHandle,
    ) -> PaperBundleJobRecord:
        try:
            record = self._read_record_unlocked(job_id, job_fd)
        except FileNotFoundError as exc:
            raise PaperBundleNotFound(job_id) from exc
        if record.owner_id != owner_id:
            raise PaperBundleNotFound(job_id)
        return record

    def _read_record_unlocked(
        self,
        job_id: str,
        job_fd: DirectoryHandle,
    ) -> PaperBundleJobRecord:
        payload = _read_json_at(job_fd, "paper_bundle_job.json")
        return self._record_from_payload(payload, expected_job_id=job_id)

    def _write_record_unlocked(
        self,
        record: PaperBundleJobRecord,
        job_fd: DirectoryHandle,
    ) -> None:
        _durable_write_json_at(
            job_fd,
            "paper_bundle_job.json",
            self._record_to_payload(record),
        )

    @contextmanager
    def _owner_lock(self, owner_id: str) -> Iterator[None]:
        digest = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
        with self._portable_root_anchor_lock():
            with self._internal_directory(".owner-locks") as locks_fd:
                with _locked_file_at(
                    locks_fd,
                    f"{digest}.lock",
                    (*self._root_identity, ".owner-locks"),
                ):
                    yield

    @contextmanager
    def _parent_lock(self, job_id: str) -> Iterator[DirectoryHandle]:
        job_id = self._validate_identifier(job_id, "job_id")
        with self._portable_root_anchor_lock():
            with self._root_directory() as root_fd:
                try:
                    job_fd = _open_directory_at(root_fd, job_id)
                except FileNotFoundError as exc:
                    raise PaperBundleNotFound(job_id) from exc
                try:
                    if _directory_identity(root_fd) != self._root_identity:
                        raise InvalidPaperBundle("paper bundle root was replaced")
                    identity = _directory_identity(job_fd)
                    with _locked_file_at(
                        job_fd,
                        ".paper_bundle_job.lock",
                        identity,
                    ):
                        yield job_fd
                        current = _stat_at(root_fd, job_id)
                        if (
                            not stat.S_ISDIR(current.st_mode)
                            or (current.st_dev, current.st_ino)
                            != identity
                        ):
                            raise InvalidPaperBundle(
                                "paper bundle directory changed during operation"
                            )
                finally:
                    _close_directory(job_fd)

    @staticmethod
    def _assert_revision(
        record: PaperBundleJobRecord,
        expected_revision: int | None,
    ) -> None:
        if expected_revision is None:
            return
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise StalePaperBundleRevision("expected_revision must be an integer")
        if expected_revision != record.revision:
            raise StalePaperBundleRevision(
                f"expected revision {expected_revision}, found {record.revision}"
            )

    @staticmethod
    def _record_timestamp(
        record: PaperBundleJobRecord,
        candidate: float | None = None,
    ) -> float:
        values = [
            time.time() if candidate is None else candidate,
            record.created_at,
            record.updated_at,
        ]
        if record.terminal_at is not None:
            values.append(record.terminal_at)
        if record.cancel_requested_at is not None:
            values.append(record.cancel_requested_at)
        for intent in record.start_intents.values():
            values.extend((intent.claimed_at, intent.updated_at))
        for publication in record.publications.values():
            values.append(publication.published_at)
        return max(values)

    @staticmethod
    def _claim_timestamp(
        claim: Mapping[str, object],
        candidate: float | None = None,
    ) -> float:
        return max(
            time.time() if candidate is None else candidate,
            float(claim["created_at"]),
            float(claim["updated_at"]),
        )

    def _lease_is_active(self, lease_expires_at: float, wall_now: float) -> bool:
        maximum_remaining = max(1.0, self.claim_lease_s * 2.0)
        return wall_now < lease_expires_at <= wall_now + maximum_remaining

    @classmethod
    def _validate_children(
        cls,
        children: Mapping[str, PaperBundleChildDescriptor],
    ) -> dict[str, PaperBundleChildDescriptor]:
        if not isinstance(children, Mapping) or set(children) != _ARTIFACT_SET:
            raise InvalidPaperBundle("a Paper Bundle requires poster, deck, landing, and video")
        validated: dict[str, PaperBundleChildDescriptor] = {}
        run_ids: set[str] = set()
        for artifact_type in _ARTIFACT_TYPES:
            child = children[artifact_type]
            if not isinstance(child, PaperBundleChildDescriptor):
                raise InvalidPaperBundle(f"invalid {artifact_type} child descriptor")
            if child.artifact_type != artifact_type:
                raise InvalidPaperBundle("child artifact type does not match its map key")
            cls._validate_identifier(child.run_id, "child run_id")
            if child.run_id in run_ids:
                raise InvalidPaperBundle("child run IDs must be unique")
            run_ids.add(child.run_id)
            cls._validate_text(child.conversation_id, "child conversation_id", maximum=512)
            cls._validate_text(child.upload_token, "upload_token", maximum=1024)
            cls._validate_digest(child.request_digest, "child request_digest")
            if not isinstance(child.expires_at, (int, float)) or isinstance(
                child.expires_at, bool
            ) or not math.isfinite(float(child.expires_at)):
                raise InvalidPaperBundle("child expires_at must be finite")
            if float(child.expires_at) <= time.time():
                raise InvalidPaperBundle("child reservation is already expired")
            if (
                child.state != "reserved"
                or child.terminal is not False
                or child.process_free is not True
                or child.diagnostic is not None
            ):
                raise InvalidPaperBundle("new child descriptors must be reserved and process-free")
            if not child.input_slots:
                raise InvalidPaperBundle("each child requires at least one input slot")
            slot_names: set[str] = set()
            for slot in child.input_slots:
                if not isinstance(slot, PaperBundleInputSlot):
                    raise InvalidPaperBundle("invalid child input slot")
                if not _SAFE_SLOT_PATTERN.fullmatch(slot.name) or slot.name in {".", ".."}:
                    raise InvalidPaperBundle(f"unsafe input slot: {slot.name!r}")
                if slot.name in slot_names:
                    raise InvalidPaperBundle("duplicate child input slot")
                slot_names.add(slot.name)
                cls._validate_digest(slot.expected_sha256, "input slot digest")
                if (
                    isinstance(slot.expected_size, bool)
                    or not isinstance(slot.expected_size, int)
                    or slot.expected_size < 0
                ):
                    raise InvalidPaperBundle("input slot size must be a nonnegative integer")
            validated[artifact_type] = child
        return validated

    @staticmethod
    def _validate_snapshot(snapshot: ChildStateSnapshot) -> ChildStateSnapshot:
        if not isinstance(snapshot, ChildStateSnapshot):
            raise InvalidPaperBundle("child status provider returned an invalid snapshot")
        if snapshot.state not in _CHILD_STATES:
            raise InvalidPaperBundle(f"invalid child state: {snapshot.state!r}")
        if not isinstance(snapshot.terminal, bool) or not isinstance(snapshot.process_free, bool):
            raise InvalidPaperBundle("child terminal/process_free flags must be booleans")
        if snapshot.terminal != (snapshot.state in _CHILD_TERMINAL_STATES):
            raise InvalidPaperBundle("child terminal flag disagrees with its state")
        if snapshot.diagnostic is not None:
            PaperBundleJobStore._validate_text(
                snapshot.diagnostic, "child diagnostic", maximum=4096
            )
        return snapshot

    @staticmethod
    def _validate_artifact_type(value: object) -> str:
        if not isinstance(value, str) or value not in _ARTIFACT_SET:
            raise InvalidPaperBundle(f"invalid artifact_type: {value!r}")
        return value

    @staticmethod
    def _publication_matches(
        publication: PaperBundlePublication,
        *,
        source_run_id: str,
        publication_run_id: str,
        artifact_id: str,
        source_attempt: int,
        source_candidate_id: str,
        source_candidate_sha256: str,
        generation: int,
    ) -> bool:
        return (
            publication.source_run_id == source_run_id
            and publication.publication_run_id == publication_run_id
            and publication.artifact_id == artifact_id
            and publication.source_attempt == source_attempt
            and publication.source_candidate_id == source_candidate_id
            and publication.source_candidate_sha256 == source_candidate_sha256
            and publication.generation == generation
        )

    @staticmethod
    def _effective_completed_children(
        children: Mapping[str, PaperBundleChildDescriptor],
        publications: Mapping[str, PaperBundlePublication],
    ) -> tuple[str, ...]:
        return tuple(
            artifact_type
            for artifact_type in _ARTIFACT_TYPES
            if (
                children[artifact_type].state == "completed"
                and children[artifact_type].terminal
                and children[artifact_type].process_free
            )
            or artifact_type in publications
        )

    @classmethod
    def _derive_parent_lifecycle(
        cls,
        *,
        children: Mapping[str, PaperBundleChildDescriptor],
        publications: Mapping[str, PaperBundlePublication],
        cancel_requested: bool,
        start_intents: Mapping[str, ChildStartIntent],
    ) -> tuple[PaperBundleState, bool, tuple[str, ...]]:
        completed_children = cls._effective_completed_children(
            children,
            publications,
        )
        all_quiescent = all(
            child.terminal and child.process_free for child in children.values()
        )
        pending_start = any(
            intent.state in {"claimed", "committed"}
            for intent in start_intents.values()
        )
        if cancel_requested:
            state: PaperBundleState = (
                "cancelled" if all_quiescent and not pending_start else "cancelling"
            )
        elif all_quiescent and not pending_start:
            child_states = {child.state for child in children.values()}
            if len(completed_children) == len(_ARTIFACT_TYPES):
                state = "completed"
            elif completed_children:
                state = "partial"
            elif child_states == {"cancelled"}:
                state = "cancelled"
            else:
                state = "failed"
        elif all(child.state == "reserved" for child in children.values()):
            state = "reserved"
        else:
            state = "running"
        return state, state in _PARENT_TERMINAL_STATES, completed_children

    @classmethod
    def _record_from_payload(
        cls,
        payload: object,
        *,
        expected_job_id: str,
    ) -> PaperBundleJobRecord:
        if not isinstance(payload, dict):
            raise InvalidPaperBundle("paper bundle record must be a JSON object")
        legacy_keys = {
            "schema_version",
            "job_id",
            "owner_id",
            "conversation_id",
            "source_name",
            "prompt_version",
            "state",
            "children",
            "request_digest",
            "idempotency_key_digest",
            "revision",
            "created_at",
            "updated_at",
            "terminal",
            "terminal_at",
            "cancel_requested",
            "cancel_requested_at",
            "completed_children",
            "diagnostics",
            "start_intents",
        }
        schema_version = payload.get("schema_version")
        if type(schema_version) is not int:
            raise InvalidPaperBundle("unsupported or malformed paper bundle schema")
        if schema_version == _LEGACY_SCHEMA_VERSION:
            if set(payload) != legacy_keys:
                raise InvalidPaperBundle("unsupported or malformed paper bundle schema")
            publications_payload: object = {}
            publication_generations_payload: object = {
                artifact_type: 0 for artifact_type in _ARTIFACT_TYPES
            }
        elif schema_version == _SCHEMA_VERSION:
            if set(payload) != legacy_keys | {
                "publications",
                "publication_generations",
            }:
                raise InvalidPaperBundle("unsupported or malformed paper bundle schema")
            publications_payload = payload["publications"]
            publication_generations_payload = payload["publication_generations"]
        else:
            raise InvalidPaperBundle("unsupported or malformed paper bundle schema")
        if payload.get("job_id") != expected_job_id:
            raise InvalidPaperBundle("paper bundle identity does not match its path")
        try:
            children_payload = payload["children"]
            if not isinstance(children_payload, dict):
                raise InvalidPaperBundle("paper bundle children must be an object")
            children = {
                artifact_type: cls._child_from_payload(children_payload[artifact_type])
                for artifact_type in _ARTIFACT_TYPES
            }
            if set(children_payload) != _ARTIFACT_SET:
                raise InvalidPaperBundle("paper bundle children are incomplete")
            state = payload["state"]
            if state not in _PARENT_STATES:
                raise InvalidPaperBundle(f"invalid parent state: {state!r}")
            terminal = payload["terminal"]
            if not isinstance(terminal, bool) or terminal != (state in _PARENT_TERMINAL_STATES):
                raise InvalidPaperBundle("parent terminal flag disagrees with state")
            revision = payload["revision"]
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                raise InvalidPaperBundle("invalid parent revision")
            created_at = cls._finite_number(payload["created_at"], "created_at")
            updated_at = cls._finite_number(payload["updated_at"], "updated_at")
            terminal_at = cls._optional_finite_number(payload["terminal_at"], "terminal_at")
            cancel_requested_at = cls._optional_finite_number(
                payload["cancel_requested_at"], "cancel_requested_at"
            )
            cancel_requested = payload["cancel_requested"]
            if not isinstance(cancel_requested, bool):
                raise InvalidPaperBundle("cancel_requested must be boolean")
            completed_payload = payload["completed_children"]
            if not isinstance(completed_payload, list) or any(
                item not in _ARTIFACT_SET for item in completed_payload
            ) or len(set(completed_payload)) != len(completed_payload):
                raise InvalidPaperBundle("invalid completed_children")
            completed_children = tuple(completed_payload)
            diagnostics_payload = payload["diagnostics"]
            if not isinstance(diagnostics_payload, dict) or any(
                key not in _ARTIFACT_SET or not isinstance(value, str)
                for key, value in diagnostics_payload.items()
            ):
                raise InvalidPaperBundle("invalid parent diagnostics")
            intents_payload = payload["start_intents"]
            if not isinstance(intents_payload, dict):
                raise InvalidPaperBundle("invalid child start intents")
            start_intents = {
                run_id: cls._intent_from_payload(intent_payload, expected_run_id=run_id)
                for run_id, intent_payload in intents_payload.items()
            }
            if not isinstance(publications_payload, dict) or any(
                key not in _ARTIFACT_SET for key in publications_payload
            ):
                raise InvalidPaperBundle("invalid child publications")
            publications = {
                artifact_type: cls._publication_from_payload(publication_payload)
                for artifact_type, publication_payload in publications_payload.items()
            }
            if (
                not isinstance(publication_generations_payload, dict)
                or set(publication_generations_payload) != _ARTIFACT_SET
            ):
                raise InvalidPaperBundle("invalid publication generations")
            publication_generations: dict[str, int] = {}
            for artifact_type, generation in publication_generations_payload.items():
                if (
                    isinstance(generation, bool)
                    or not isinstance(generation, int)
                    or generation < 0
                ):
                    raise InvalidPaperBundle("invalid publication generation")
                publication_generations[artifact_type] = generation
            record = PaperBundleJobRecord(
                job_id=cls._validate_identifier(payload["job_id"], "job_id"),
                owner_id=cls._validate_text(payload["owner_id"], "owner_id", maximum=512),
                conversation_id=cls._validate_text(
                    payload["conversation_id"], "conversation_id", maximum=512
                ),
                source_name=cls._validate_source_name(payload["source_name"]),
                prompt_version=cls._validate_text(
                    payload["prompt_version"], "prompt_version", maximum=256
                ),
                state=state,
                children=MappingProxyType(children),
                request_digest=cls._validate_digest(
                    payload["request_digest"], "request_digest"
                ),
                idempotency_key_digest=cls._validate_digest(
                    payload["idempotency_key_digest"], "idempotency_key_digest"
                ),
                revision=revision,
                created_at=created_at,
                updated_at=updated_at,
                terminal=terminal,
                terminal_at=terminal_at,
                cancel_requested=cancel_requested,
                cancel_requested_at=cancel_requested_at,
                completed_children=completed_children,
                diagnostics=MappingProxyType(dict(diagnostics_payload)),
                start_intents=MappingProxyType(start_intents),
                publications=MappingProxyType(publications),
                publication_generations=MappingProxyType(publication_generations),
            )
        except KeyError as exc:
            raise InvalidPaperBundle("paper bundle record is incomplete") from exc
        if terminal != (terminal_at is not None):
            raise InvalidPaperBundle("parent terminal timestamp disagrees with state")
        if cancel_requested != (cancel_requested_at is not None):
            raise InvalidPaperBundle("parent cancellation timestamp disagrees with flag")
        if created_at > updated_at:
            raise InvalidPaperBundle("parent timestamps are out of order")
        if terminal_at is not None and terminal_at < created_at:
            raise InvalidPaperBundle("parent terminal timestamp is out of order")
        if cancel_requested_at is not None and cancel_requested_at < created_at:
            raise InvalidPaperBundle("parent cancellation timestamp is out of order")
        if state == "cancelling" and not cancel_requested:
            raise InvalidPaperBundle("cancelling parent lacks a cancellation request")
        cls._validate_children_for_record(record.children)
        publication_run_ids: set[str] = set()
        publication_artifact_ids: set[str] = set()
        for artifact_type, publication in record.publications.items():
            source_child = record.children[artifact_type]
            if publication.source_run_id != source_child.run_id:
                raise InvalidPaperBundle(
                    "publication source disagrees with bundle child"
                )
            if not _is_quiescent_publication_source(source_child):
                raise InvalidPaperBundle(
                    "publication source is not a quiescent publishable child"
                )
            if publication.publication_run_id == publication.source_run_id:
                raise InvalidPaperBundle(
                    "publication run must differ from its source run"
                )
            if publication.publication_run_id in publication_run_ids:
                raise InvalidPaperBundle("publication run IDs must be unique")
            if publication.artifact_id in publication_artifact_ids:
                raise InvalidPaperBundle("publication artifact IDs must be unique")
            publication_run_ids.add(publication.publication_run_id)
            publication_artifact_ids.add(publication.artifact_id)
            if publication.generation > record.publication_generations[artifact_type]:
                raise InvalidPaperBundle(
                    "publication generation exceeds its allocation"
                )
            if not created_at <= publication.published_at <= updated_at:
                raise InvalidPaperBundle("publication timestamp is out of order")
        expected_completed = cls._effective_completed_children(
            record.children,
            record.publications,
        )
        if completed_children != expected_completed:
            raise InvalidPaperBundle("completed_children disagrees with child state")
        expected_diagnostics = {
            artifact_type: child.diagnostic
            for artifact_type, child in record.children.items()
            if child.diagnostic is not None
        }
        if dict(record.diagnostics) != expected_diagnostics:
            raise InvalidPaperBundle("parent diagnostics disagree with child state")
        if terminal and not all(
            child.terminal and child.process_free for child in record.children.values()
        ):
            raise InvalidPaperBundle("terminal parent has a non-quiescent child")
        child_run_ids = {child.run_id for child in record.children.values()}
        if any(run_id not in child_run_ids for run_id in record.start_intents):
            raise InvalidPaperBundle("start intent does not belong to a child")
        if terminal and any(
            intent.state in {"claimed", "committed"}
            for intent in record.start_intents.values()
        ):
            raise InvalidPaperBundle("terminal parent retains an in-flight start intent")
        child_states = {child.state for child in record.children.values()}
        if state == "completed" and len(expected_completed) != len(_ARTIFACT_TYPES):
            raise InvalidPaperBundle("completed parent has a non-completed child")
        if state == "partial" and not (0 < len(expected_completed) < len(_ARTIFACT_TYPES)):
            raise InvalidPaperBundle("partial parent has invalid completed children")
        if state == "failed" and (expected_completed or child_states == {"cancelled"}):
            raise InvalidPaperBundle("failed parent has an invalid terminal derivation")
        if state == "cancelled" and not cancel_requested and child_states != {"cancelled"}:
            raise InvalidPaperBundle("cancelled parent lacks a valid terminal derivation")
        return record

    @classmethod
    def _validate_children_for_record(
        cls,
        children: Mapping[str, PaperBundleChildDescriptor],
    ) -> None:
        if set(children) != _ARTIFACT_SET:
            raise InvalidPaperBundle("paper bundle children are incomplete")
        run_ids: set[str] = set()
        for artifact_type, child in children.items():
            if child.artifact_type != artifact_type or child.run_id in run_ids:
                raise InvalidPaperBundle("paper bundle child identity is invalid")
            run_ids.add(child.run_id)
            if not child.input_slots:
                raise InvalidPaperBundle("paper bundle child input slots are empty")
            slot_names = [slot.name for slot in child.input_slots]
            if len(slot_names) != len(set(slot_names)):
                raise InvalidPaperBundle("paper bundle child input slots are duplicated")

    @classmethod
    def _child_from_payload(cls, payload: object) -> PaperBundleChildDescriptor:
        if not isinstance(payload, dict):
            raise InvalidPaperBundle("child descriptor must be an object")
        expected_keys = {
            "run_id",
            "artifact_type",
            "conversation_id",
            "input_slots",
            "upload_token",
            "request_digest",
            "expires_at",
            "state",
            "terminal",
            "process_free",
            "diagnostic",
        }
        if set(payload) != expected_keys:
            raise InvalidPaperBundle("malformed child descriptor")
        slots_payload = payload["input_slots"]
        if not isinstance(slots_payload, list):
            raise InvalidPaperBundle("child input slots must be a list")
        slots = tuple(cls._slot_from_payload(item) for item in slots_payload)
        state = payload["state"]
        terminal = payload["terminal"]
        process_free = payload["process_free"]
        if state not in _CHILD_STATES or not isinstance(terminal, bool) or not isinstance(
            process_free, bool
        ):
            raise InvalidPaperBundle("invalid child lifecycle fields")
        if terminal != (state in _CHILD_TERMINAL_STATES):
            raise InvalidPaperBundle("child terminal flag disagrees with state")
        diagnostic = payload["diagnostic"]
        if diagnostic is not None:
            diagnostic = cls._validate_text(diagnostic, "child diagnostic", maximum=4096)
        return PaperBundleChildDescriptor(
            run_id=cls._validate_identifier(payload["run_id"], "child run_id"),
            artifact_type=cls._validate_text(
                payload["artifact_type"], "artifact_type", maximum=32
            ),
            conversation_id=cls._validate_text(
                payload["conversation_id"], "child conversation_id", maximum=512
            ),
            input_slots=slots,
            upload_token=cls._validate_text(
                payload["upload_token"], "upload_token", maximum=1024
            ),
            request_digest=cls._validate_digest(
                payload["request_digest"], "child request_digest"
            ),
            expires_at=cls._finite_number(payload["expires_at"], "child expires_at"),
            state=state,
            terminal=terminal,
            process_free=process_free,
            diagnostic=diagnostic,
        )

    @classmethod
    def _slot_from_payload(cls, payload: object) -> PaperBundleInputSlot:
        if not isinstance(payload, dict) or set(payload) != {
            "name",
            "expected_sha256",
            "expected_size",
        }:
            raise InvalidPaperBundle("malformed input slot")
        name = payload["name"]
        size = payload["expected_size"]
        if not isinstance(name, str) or not _SAFE_SLOT_PATTERN.fullmatch(name):
            raise InvalidPaperBundle("unsafe input slot name")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise InvalidPaperBundle("invalid input slot size")
        return PaperBundleInputSlot(
            name=name,
            expected_sha256=cls._validate_digest(
                payload["expected_sha256"], "input slot digest"
            ),
            expected_size=size,
        )

    @classmethod
    def _publication_from_payload(cls, payload: object) -> PaperBundlePublication:
        if not isinstance(payload, dict) or set(payload) != {
            "source_run_id",
            "publication_run_id",
            "artifact_id",
            "source_attempt",
            "source_candidate_id",
            "source_candidate_sha256",
            "generation",
            "published_at",
        }:
            raise InvalidPaperBundle("malformed child publication")
        source_attempt = payload["source_attempt"]
        generation = payload["generation"]
        if (
            isinstance(source_attempt, bool)
            or not isinstance(source_attempt, int)
            or source_attempt <= 0
        ):
            raise InvalidPaperBundle("invalid publication source_attempt")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation <= 0
        ):
            raise InvalidPaperBundle("invalid publication generation")
        return PaperBundlePublication(
            source_run_id=cls._validate_identifier(
                payload["source_run_id"], "publication source_run_id"
            ),
            publication_run_id=cls._validate_identifier(
                payload["publication_run_id"], "publication_run_id"
            ),
            artifact_id=cls._validate_identifier(
                payload["artifact_id"], "publication artifact_id"
            ),
            source_attempt=source_attempt,
            source_candidate_id=cls._validate_identifier(
                payload["source_candidate_id"], "publication source_candidate_id"
            ),
            source_candidate_sha256=cls._validate_digest(
                payload["source_candidate_sha256"],
                "publication source_candidate_sha256",
            ),
            generation=generation,
            published_at=cls._finite_number(
                payload["published_at"], "publication published_at"
            ),
        )

    @staticmethod
    def _record_to_payload(record: PaperBundleJobRecord) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "job_id": record.job_id,
            "owner_id": record.owner_id,
            "conversation_id": record.conversation_id,
            "source_name": record.source_name,
            "prompt_version": record.prompt_version,
            "state": record.state,
            "children": {
                artifact_type: {
                    "run_id": child.run_id,
                    "artifact_type": child.artifact_type,
                    "conversation_id": child.conversation_id,
                    "input_slots": [
                        {
                            "name": slot.name,
                            "expected_sha256": slot.expected_sha256,
                            "expected_size": slot.expected_size,
                        }
                        for slot in child.input_slots
                    ],
                    "upload_token": child.upload_token,
                    "request_digest": child.request_digest,
                    "expires_at": child.expires_at,
                    "state": child.state,
                    "terminal": child.terminal,
                    "process_free": child.process_free,
                    "diagnostic": child.diagnostic,
                }
                for artifact_type, child in record.children.items()
            },
            "request_digest": record.request_digest,
            "idempotency_key_digest": record.idempotency_key_digest,
            "revision": record.revision,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "terminal": record.terminal,
            "terminal_at": record.terminal_at,
            "cancel_requested": record.cancel_requested,
            "cancel_requested_at": record.cancel_requested_at,
            "completed_children": list(record.completed_children),
            "diagnostics": dict(record.diagnostics),
            "start_intents": {
                run_id: {
                    "intent_id": intent.intent_id,
                    "run_id": intent.run_id,
                    "state": intent.state,
                    "claimed_at": intent.claimed_at,
                    "updated_at": intent.updated_at,
                    "expires_at": intent.expires_at,
                }
                for run_id, intent in record.start_intents.items()
            },
            "publications": {
                artifact_type: {
                    "source_run_id": publication.source_run_id,
                    "publication_run_id": publication.publication_run_id,
                    "artifact_id": publication.artifact_id,
                    "source_attempt": publication.source_attempt,
                    "source_candidate_id": publication.source_candidate_id,
                    "source_candidate_sha256": publication.source_candidate_sha256,
                    "generation": publication.generation,
                    "published_at": publication.published_at,
                }
                for artifact_type, publication in record.publications.items()
            },
            "publication_generations": dict(record.publication_generations),
        }

    @staticmethod
    def _child_to_payload(child: PaperBundleChildDescriptor) -> dict[str, object]:
        return {
            "run_id": child.run_id,
            "artifact_type": child.artifact_type,
            "conversation_id": child.conversation_id,
            "input_slots": [
                {
                    "name": slot.name,
                    "expected_sha256": slot.expected_sha256,
                    "expected_size": slot.expected_size,
                }
                for slot in child.input_slots
            ],
            "upload_token": child.upload_token,
            "request_digest": child.request_digest,
            "expires_at": child.expires_at,
            "state": child.state,
            "terminal": child.terminal,
            "process_free": child.process_free,
            "diagnostic": child.diagnostic,
        }

    @classmethod
    def _intent_from_payload(
        cls,
        payload: object,
        *,
        expected_run_id: str,
    ) -> ChildStartIntent:
        if not isinstance(payload, dict) or set(payload) != {
            "intent_id",
            "run_id",
            "state",
            "claimed_at",
            "updated_at",
            "expires_at",
        }:
            raise InvalidPaperBundle("malformed child start intent")
        run_id = cls._validate_identifier(payload["run_id"], "start intent run_id")
        if run_id != expected_run_id:
            raise InvalidPaperBundle("start intent identity disagrees with map key")
        state = payload["state"]
        if state not in {"claimed", "committed", "registered", "aborted", "revoked"}:
            raise InvalidPaperBundle("invalid child start intent state")
        claimed_at = cls._finite_number(payload["claimed_at"], "intent claimed_at")
        updated_at = cls._finite_number(payload["updated_at"], "intent updated_at")
        expires_at = cls._finite_number(payload["expires_at"], "intent expires_at")
        if not claimed_at <= updated_at or expires_at < claimed_at:
            raise InvalidPaperBundle("child start intent timestamps are out of order")
        return ChildStartIntent(
            intent_id=cls._validate_identifier(payload["intent_id"], "intent_id"),
            run_id=run_id,
            state=state,
            claimed_at=claimed_at,
            updated_at=updated_at,
            expires_at=expires_at,
        )

    @staticmethod
    def _validate_identifier(value: object, field_name: str) -> str:
        if not isinstance(value, str):
            raise InvalidPaperBundle(f"{field_name} must be a string")
        try:
            return validate_run_id(value)
        except RunControlError as exc:
            raise InvalidPaperBundle(str(exc)) from exc

    @staticmethod
    def _validate_text(value: object, field_name: str, *, maximum: int) -> str:
        if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
            raise InvalidPaperBundle(f"invalid {field_name}")
        return value

    @classmethod
    def _validate_source_name(cls, value: object) -> str:
        source_name = cls._validate_text(value, "source_name", maximum=1024)
        if source_name in {".", ".."} or "/" in source_name or "\\" in source_name:
            raise InvalidPaperBundle("source_name must be a display filename")
        return source_name

    @staticmethod
    def _validate_digest(value: object, field_name: str) -> str:
        if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
            raise InvalidPaperBundle(f"{field_name} must be a lowercase SHA-256 digest")
        return value

    @staticmethod
    def _finite_number(value: object, field_name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidPaperBundle(f"{field_name} must be finite")
        result = float(value)
        if not math.isfinite(result):
            raise InvalidPaperBundle(f"{field_name} must be finite")
        return result

    @classmethod
    def _optional_finite_number(cls, value: object, field_name: str) -> float | None:
        if value is None:
            return None
        return cls._finite_number(value, field_name)

    @staticmethod
    def _idempotency_digest(owner_id: str, idempotency_key: str) -> str:
        return hashlib.sha256(
            owner_id.encode("utf-8") + b"\0" + idempotency_key.encode("utf-8")
        ).hexdigest()


def _portable_lstat(path: Path) -> os.stat_result:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise InvalidPaperBundle(f"cannot safely inspect path: {path}") from exc
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    is_junction = getattr(path, "is_junction", lambda: False)()
    if stat.S_ISLNK(metadata.st_mode) or is_junction or (
        file_attributes & reparse_attribute
    ):
        raise InvalidPaperBundle(f"reparse points are not allowed: {path}")
    return metadata


def _stat_path(path: Path) -> os.stat_result:
    if _WINDOWS_IO is not None:
        return cast(os.stat_result, _WINDOWS_IO.stat_path(path))
    return _portable_lstat(path)


def _directory_identity(directory: DirectoryHandle) -> tuple[int, int]:
    if isinstance(directory, _WindowsDirectoryHandle):
        return directory.identity
    metadata = os.fstat(directory) if isinstance(directory, int) else _portable_lstat(directory)
    if not stat.S_ISDIR(metadata.st_mode):
        raise InvalidPaperBundle(f"unsafe directory: {directory}")
    return metadata.st_dev, metadata.st_ino


def _close_directory(directory: DirectoryHandle) -> None:
    if isinstance(directory, _WindowsDirectoryHandle):
        if _WINDOWS_IO is None:
            raise InvalidPaperBundle("Windows directory backend is unavailable")
        _WINDOWS_IO.close(directory.raw_handle)
    elif isinstance(directory, int):
        os.close(directory)


def _stat_at(directory: DirectoryHandle, name: str) -> os.stat_result:
    if isinstance(directory, _WindowsDirectoryHandle):
        if _WINDOWS_IO is None:
            raise InvalidPaperBundle("Windows directory backend is unavailable")
        return cast(os.stat_result, _WINDOWS_IO.stat_at(directory, name))
    if isinstance(directory, int):
        return os.stat(name, dir_fd=directory, follow_symlinks=False)
    return _portable_lstat(directory / name)


def _listdir(directory: DirectoryHandle) -> list[str]:
    if isinstance(directory, _WindowsDirectoryHandle):
        if _WINDOWS_IO is None:
            raise InvalidPaperBundle("Windows directory backend is unavailable")
        return _WINDOWS_IO.listdir(directory)
    return os.listdir(directory)


def _replace_at(
    source_directory: DirectoryHandle,
    source_name: str,
    destination_directory: DirectoryHandle,
    destination_name: str,
) -> None:
    if isinstance(source_directory, _WindowsDirectoryHandle) and isinstance(
        destination_directory,
        _WindowsDirectoryHandle,
    ):
        if _WINDOWS_IO is None:
            raise InvalidPaperBundle("Windows directory backend is unavailable")
        _WINDOWS_IO.rename_at(
            source_directory,
            source_name,
            destination_directory,
            destination_name,
        )
        return
    if isinstance(source_directory, int) and isinstance(destination_directory, int):
        os.replace(
            source_name,
            destination_name,
            src_dir_fd=source_directory,
            dst_dir_fd=destination_directory,
        )
        return
    if isinstance(source_directory, Path) and isinstance(destination_directory, Path):
        os.replace(source_directory / source_name, destination_directory / destination_name)
        return
    raise InvalidPaperBundle("filesystem backend changed during operation")


def _unlink_at(directory: DirectoryHandle, name: str) -> None:
    if isinstance(directory, _WindowsDirectoryHandle):
        if _WINDOWS_IO is None:
            raise InvalidPaperBundle("Windows directory backend is unavailable")
        _WINDOWS_IO.unlink_at(directory, name)
    elif isinstance(directory, int):
        os.unlink(name, dir_fd=directory)
    else:
        os.unlink(directory / name)


def _open_directory_path(path: Path) -> DirectoryHandle:
    if _WINDOWS_IO is not None:
        return _WINDOWS_IO.open_directory(path)
    if not _SECURE_DIR_FD_AVAILABLE:
        raise InvalidPaperBundle(
            "Paper Bundle writes require stable native directory handles"
        )
    try:
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise InvalidPaperBundle(f"cannot safely open directory: {path}") from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise InvalidPaperBundle(f"unsafe directory: {path}")
    return descriptor


def _open_directory_at(
    parent_fd: DirectoryHandle,
    name: str,
    *,
    create: bool = False,
    exclusive: bool = False,
) -> DirectoryHandle:
    if not name or "/" in name or name in {".", ".."}:
        raise InvalidPaperBundle("unsafe directory name")
    if isinstance(parent_fd, _WindowsDirectoryHandle):
        if _WINDOWS_IO is None:
            raise InvalidPaperBundle("Windows directory backend is unavailable")
        return _WINDOWS_IO.open_directory_at(
            parent_fd,
            name,
            create=create,
            exclusive=exclusive,
        )
    if isinstance(parent_fd, Path):
        path = parent_fd / name
        if create:
            try:
                path.mkdir(mode=0o700)
                _fsync_directory(parent_fd)
            except FileExistsError:
                if exclusive:
                    raise
        _directory_identity(path)
        return path
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            _fsync_directory(parent_fd)
        except FileExistsError:
            if exclusive:
                raise
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise InvalidPaperBundle(f"cannot safely open directory: {name}") from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise InvalidPaperBundle(f"unsafe directory: {name}")
    return descriptor


def _validate_regular_file_descriptor(descriptor: int, *, label: str) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise InvalidPaperBundle(f"unsafe {label}")


def _validate_regular_file_at(directory_fd: DirectoryHandle, name: str) -> None:
    if isinstance(directory_fd, _WindowsDirectoryHandle):
        metadata = _stat_at(directory_fd, name)
        reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or getattr(metadata, "st_file_attributes", 0) & reparse_attribute
        ):
            raise InvalidPaperBundle(f"unsafe {name}")
        return
    if isinstance(directory_fd, Path):
        path = directory_fd / name
        metadata = _portable_lstat(path)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise InvalidPaperBundle(f"unsafe {name}")
        return
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | _FILE_NOFOLLOW_FLAGS,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise InvalidPaperBundle(f"cannot safely open file: {name}") from exc
    try:
        _validate_regular_file_descriptor(descriptor, label=name)
    finally:
        os.close(descriptor)


def _read_json_at(
    directory_fd: DirectoryHandle,
    name: str,
    *,
    missing_ok: bool = False,
) -> object | None:
    if isinstance(directory_fd, _WindowsDirectoryHandle):
        if _WINDOWS_IO is None:
            raise InvalidPaperBundle("Windows directory backend is unavailable")
        try:
            data = _WINDOWS_IO.read_bytes(directory_fd, name)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise
        try:
            return json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise InvalidPaperBundle(f"invalid JSON file: {name}") from exc
    path: Path | None = directory_fd / name if isinstance(directory_fd, Path) else None
    before: os.stat_result | None = None
    if path is not None:
        try:
            before = _portable_lstat(path)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise InvalidPaperBundle(f"unsafe {name}")
    try:
        descriptor = os.open(
            path if path is not None else name,
            os.O_RDONLY | _FILE_NOFOLLOW_FLAGS,
            **({} if path is not None else {"dir_fd": directory_fd}),
        )
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    except OSError as exc:
        raise InvalidPaperBundle(f"cannot safely read file: {name}") from exc
    try:
        _validate_regular_file_descriptor(descriptor, label=name)
        opened = os.fstat(descriptor)
        if before is not None and (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise InvalidPaperBundle(f"file changed while opening: {name}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        try:
            result = json.loads(b"".join(chunks).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise InvalidPaperBundle(f"invalid JSON file: {name}") from exc
        if path is not None:
            current = _portable_lstat(path)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise InvalidPaperBundle(f"file changed during read: {name}")
        return result
    finally:
        os.close(descriptor)


def _durable_write_json_at(
    directory_fd: DirectoryHandle,
    name: str,
    payload: Mapping[str, object],
) -> None:
    data = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if isinstance(directory_fd, _WindowsDirectoryHandle):
        if _WINDOWS_IO is None:
            raise InvalidPaperBundle("Windows directory backend is unavailable")
        try:
            _validate_regular_file_at(directory_fd, name)
        except FileNotFoundError:
            pass
        _WINDOWS_IO.durable_write(directory_fd, name, data)
        return
    try:
        _validate_regular_file_at(directory_fd, name)
    except FileNotFoundError:
        pass
    except InvalidPaperBundle as exc:
        # Missing files are reported as InvalidPaperBundle only for unsafe opens;
        # distinguish ENOENT without weakening symlink/hardlink rejection.
        try:
            _stat_at(directory_fd, name)
        except FileNotFoundError:
            pass
        else:
            raise exc
    temporary_name = f".{name}.{uuid4().hex}.tmp"
    descriptor = os.open(
        directory_fd / temporary_name if isinstance(directory_fd, Path) else temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_NOFOLLOW_FLAGS,
        0o600,
        **({} if isinstance(directory_fd, Path) else {"dir_fd": directory_fd}),
    )
    try:
        _validate_regular_file_descriptor(descriptor, label=temporary_name)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise OSError("short durable JSON write")
            offset += written
        os.fsync(descriptor)
    except BaseException:
        try:
            _unlink_at(directory_fd, temporary_name)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)
    try:
        _replace_at(directory_fd, temporary_name, directory_fd, name)
        _fsync_directory(directory_fd)
        _validate_regular_file_at(directory_fd, name)
    except BaseException:
        try:
            _unlink_at(directory_fd, temporary_name)
        except FileNotFoundError:
            pass
        raise


def _fsync_directory(directory_fd: DirectoryHandle) -> None:
    if isinstance(directory_fd, _WindowsDirectoryHandle):
        if _WINDOWS_IO is None:
            raise InvalidPaperBundle("Windows directory backend is unavailable")
        _WINDOWS_IO.flush_directory(directory_fd)
    elif isinstance(directory_fd, int):
        os.fsync(directory_fd)


@contextmanager
def _locked_file_at(
    directory_fd: DirectoryHandle,
    name: str,
    identity: object,
) -> Iterator[None]:
    """Take a process-local and OS lock through one held directory handle."""
    if isinstance(directory_fd, Path):
        raise InvalidPaperBundle(
            "Paper Bundle locks require stable native directory handles"
        )
    key = f"{identity!r}:{name}"
    with _LOCAL_LOCKS_GUARD:
        local_lock = _LOCAL_LOCKS.setdefault(key, threading.RLock())
    with local_lock:
        if isinstance(directory_fd, _WindowsDirectoryHandle):
            if _WINDOWS_IO is None:
                raise InvalidPaperBundle("Windows directory backend is unavailable")
            with _WINDOWS_IO.open_lock(directory_fd, name) as handle:
                _lock_file(handle)
                try:
                    yield
                    current = _stat_at(directory_fd, name)
                    opened = _WINDOWS_IO.stat_open_file(handle)
                    if (
                        not stat.S_ISREG(current.st_mode)
                        or current.st_nlink != 1
                        or (current.st_dev, current.st_ino)
                        != (opened.st_dev, opened.st_ino)
                    ):
                        raise InvalidPaperBundle(f"lock changed during operation: {name}")
                finally:
                    _unlock_file(handle)
            return
        portable_before: os.stat_result | None = None
        if isinstance(directory_fd, Path):
            try:
                portable_before = _portable_lstat(directory_fd / name)
            except FileNotFoundError:
                pass
            else:
                if (
                    not stat.S_ISREG(portable_before.st_mode)
                    or portable_before.st_nlink != 1
                ):
                    raise InvalidPaperBundle(f"unsafe {name}")
        descriptor: int | None = None
        last_error: OSError | None = None
        for _ in range(3):
            try:
                descriptor = os.open(
                    directory_fd / name if isinstance(directory_fd, Path) else name,
                    os.O_CREAT | os.O_RDWR | _FILE_NOFOLLOW_FLAGS,
                    0o600,
                    **({} if isinstance(directory_fd, Path) else {"dir_fd": directory_fd}),
                )
                break
            except FileNotFoundError as exc:
                # macOS openat can transiently report ENOENT when two
                # processes create the same no-follow lock file concurrently.
                last_error = exc
            except OSError as exc:
                raise InvalidPaperBundle(
                    f"cannot safely open lock {name!r}: {exc}"
                ) from exc
        if descriptor is None:
            raise InvalidPaperBundle(
                f"cannot safely open lock {name!r}: {last_error}"
            ) from last_error
        with os.fdopen(descriptor, "a+b") as handle:
            _validate_regular_file_descriptor(handle.fileno(), label=name)
            opened = os.fstat(handle.fileno())
            if isinstance(directory_fd, Path):
                current_before_lock = _stat_at(directory_fd, name)
                if (
                    portable_before is not None
                    and (portable_before.st_dev, portable_before.st_ino)
                    != (opened.st_dev, opened.st_ino)
                ) or (
                    current_before_lock.st_dev,
                    current_before_lock.st_ino,
                ) != (opened.st_dev, opened.st_ino):
                    raise InvalidPaperBundle(f"lock changed while opening: {name}")
            _lock_file(handle)
            try:
                yield
                current = _stat_at(directory_fd, name)
                if (
                    not stat.S_ISREG(current.st_mode)
                    or current.st_nlink != 1
                    or (current.st_dev, current.st_ino)
                    != (opened.st_dev, opened.st_ino)
                ):
                    raise InvalidPaperBundle(f"lock changed during operation: {name}")
            finally:
                _unlock_file(handle)


def _lock_file(handle: BinaryIO) -> None:
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


def _unlock_file(handle: BinaryIO) -> None:
    if os.name == "posix":
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    else:
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
