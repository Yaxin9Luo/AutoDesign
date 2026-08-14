"""Crash-safe append-only publication for final/video_delivery.json."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from types import MappingProxyType
from typing import Any, Callable, Literal
import uuid

_RUNTIME_OS = os
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_MOVEFILE_WRITE_THROUGH = 0x8


def _run_member_is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & 0x400
    )


def _validate_run_member_file(metadata: os.stat_result, *, label: str) -> None:
    if _run_member_is_reparse(metadata):
        raise ValueError(f"{label} is a link or reparse point")
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError(f"{label} is an unsafe hard-linked or non-regular file")


def _same_identity(before: os.stat_result, after: os.stat_result) -> bool:
    return before.st_dev == after.st_dev and before.st_ino == after.st_ino


def _run_member_file_unchanged(
    before: os.stat_result,
    after: os.stat_result,
) -> bool:
    return (
        _same_identity(before, after)
        and before.st_mode == after.st_mode
        and before.st_nlink == after.st_nlink
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
    )


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise OSError("video pointer write made no progress")
        offset += written


@dataclass
class RetainedParent:
    """Accessor-owned nested parent whose ownership transfers to one transaction."""

    native_parent: int | Path
    platform: Literal["posix", "windows"]
    validate_callback: Callable[[], None]
    close_callback: Callable[[], None]
    windows_user_sid: str | None = None
    transferred: bool = False
    closed: bool = False

    def validate(self) -> None:
        if self.closed:
            raise RuntimeError("retained video pointer parent is closed")
        self.validate_callback()

    def transfer(self) -> None:
        if self.transferred:
            raise RuntimeError("retained video pointer parent was transferred twice")
        self.validate()
        self.transferred = True

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_callback()


@dataclass(frozen=True)
class VideoPointerObservation:
    """Exact bytes and native identity captured from one retained leaf."""

    data: bytes
    platform: Literal["posix", "windows"]
    stable: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "stable",
            MappingProxyType(dict(self.stable)),
        )


_VIDEO_POINTER_TARGET = "video_delivery.json"
_VIDEO_POINTER_MAX_BYTES = 64 * 1024
_VIDEO_POINTER_PROTOCOL_VERSION = 1
_VIDEO_POINTER_PHASE_LIMIT = 64
_VIDEO_POINTER_PHASE_SIZE_LIMIT = 64 * 1024
_VIDEO_POINTER_CONFLICT_LIMIT = 16
_CONSTRUCTION_ERROR_DISPLAY_LIMIT = 1000
_CLEANUP_ERROR_DISPLAY_LIMIT = 256
_CLEANUP_AGGREGATE_DISPLAY_LIMIT = 2000
_VIDEO_POINTER_PHASE_RE = re.compile(
    r"^\.video_delivery\.json\.([0-9a-f]{32})\."
    r"phase-([0-9]{6})-([a-z][a-z0-9-]{0,47})\.json$"
)
_VIDEO_POINTER_PHASES = frozenset({
    "prepared",
    "quarantine-intent",
    "prior-quarantined",
    "no-prior-confirmed",
    "publish-intent",
    "published",
    "committed",
    "recovery-committed-confirmed",
    "aborting",
    "displace-intent",
    "displaced",
    "restore-intent",
    "conflict-intent",
    "aborted",
    "reconciliation-required",
})
_VIDEO_POINTER_TERMINAL_PHASES = frozenset({
    "aborted",
    "reconciliation-required",
    "recovery-committed-confirmed",
})
_VIDEO_POINTER_STABLE_KEYS = {
    "posix": frozenset({
        "dev",
        "ino",
        "mode",
        "nlink",
        "size",
        "mtime_ns",
        "sha256",
    }),
    "windows": frozenset({
        "volume_serial_number",
        "file_id",
        "file_attributes",
        "reparse",
        "nlink",
        "size",
        "last_write_time",
        "sha256",
    }),
}


class VideoPointerPublicationPreconditionError(ValueError):
    """A publication checkpoint changed before its transaction could commit."""


def _video_pointer_transaction_phase_hook(
    phase: str,
    **_details: Any,
) -> None:
    """Test seam invoked only after a phase record is durably published."""


def _publication_resource_close_hook(
    *,
    durable_committed: bool,
    transaction: "_VideoPointerTransaction",
) -> None:
    """Test seam for failures at the pre/post-commit close boundary."""


def _native_no_replace_rename_api_factory() -> tuple[Any, Callable[[], int]]:
    """Return the platform's atomic, same-parent, no-replace rename syscall."""

    if _RUNTIME_OS.name != "posix":
        raise RuntimeError("native POSIX no-replace rename is unavailable")
    import ctypes

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            rename = libc.renameatx_np
        elif sys.platform.startswith("linux"):
            rename = libc.renameat2
        else:
            raise RuntimeError(
                f"native no-replace rename is unsupported on {sys.platform}"
            )
    except (AttributeError, OSError) as exc:
        raise RuntimeError("native no-replace rename API is unavailable") from exc
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    return rename, ctypes.get_errno


def _atomic_no_replace_rename(
    parent: int | Path,
    source_name: str,
    destination_name: str,
) -> None:
    """Move one same-parent entry without replacement or a weaker fallback."""

    if not source_name or not destination_name:
        raise ValueError("no-replace rename requires non-empty role names")
    if Path(source_name).name != source_name or Path(destination_name).name != destination_name:
        raise ValueError("no-replace rename roles must stay in one parent")
    if _RUNTIME_OS.name == "posix":
        if not isinstance(parent, int):
            raise RuntimeError("POSIX no-replace rename requires a retained parent fd")
        rename, get_error = _native_no_replace_rename_api_factory()
        flag = 0x4 if sys.platform == "darwin" else 0x1
        result = rename(
            parent,
            os.fsencode(source_name),
            parent,
            os.fsencode(destination_name),
            flag,
        )
        if result == 0:
            return
        error_number = int(get_error())
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
            raise FileExistsError(
                error_number,
                "native no-replace destination exists",
                destination_name,
            )
        raise OSError(
            error_number,
            "native no-replace rename failed closed",
            source_name,
        )
    if _RUNTIME_OS.name == "nt":
        if not isinstance(parent, Path):
            raise RuntimeError("Windows no-replace rename requires a guarded parent")
        api = _windows_native_handle_api_factory()
        source = parent / source_name
        destination = parent / destination_name
        if not api.move_file_ex(source, destination, _MOVEFILE_WRITE_THROUGH):
            error_number = int(api.get_last_error())
            if error_number in {80, 183}:
                raise FileExistsError(
                    error_number,
                    "MoveFileExW no-replace destination exists",
                    str(destination),
                )
            raise OSError(
                error_number,
                "MoveFileExW no-replace rename failed closed",
                str(source),
            )
        return
    raise RuntimeError("native no-replace rename is unavailable")


class _WindowsNativeHandleAPI:
    """Small injectable owner-neutral wrapper around retained Windows HANDLE I/O."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows retained HANDLE API is unavailable")
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._invalid_handle = ctypes.c_void_p(-1).value
        self._configure_io_signatures()

    def _configure_io_signatures(self) -> None:
        ctypes = self._ctypes
        wintypes = self._wintypes
        dword_pointer = ctypes.POINTER(wintypes.DWORD)
        large_integer_pointer = ctypes.POINTER(ctypes.c_longlong)

        self._kernel32.WriteFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            dword_pointer,
            ctypes.c_void_p,
        ]
        self._kernel32.WriteFile.restype = wintypes.BOOL
        self._kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            dword_pointer,
            ctypes.c_void_p,
        ]
        self._kernel32.ReadFile.restype = wintypes.BOOL
        self._kernel32.SetFilePointerEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_longlong,
            large_integer_pointer,
            wintypes.DWORD,
        ]
        self._kernel32.SetFilePointerEx.restype = wintypes.BOOL
        self._kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        self._kernel32.FlushFileBuffers.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.MoveFileExW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
        ]
        self._kernel32.MoveFileExW.restype = wintypes.BOOL

    def create_file(
        self,
        path: Path,
        desired_access: int,
        share_mode: int,
        security_attributes: Any,
        creation_disposition: int,
        flags_and_attributes: int,
    ) -> int:
        ctypes = self._ctypes
        wintypes = self._wintypes
        create_file = self._kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(path),
            desired_access,
            share_mode,
            security_attributes,
            creation_disposition,
            flags_and_attributes,
            None,
        )
        value = getattr(handle, "value", handle)
        if value in {None, 0, self._invalid_handle}:
            raise OSError(self.get_last_error(), f"CreateFileW failed: {path}")
        return int(value)

    def get_file_information_by_handle_ex(
        self,
        handle: int,
        info_class: str,
    ) -> dict[str, Any]:
        ctypes = self._ctypes
        wintypes = self._wintypes

        class _FileBasicInfo(ctypes.Structure):
            _fields_ = [
                ("creation_time", ctypes.c_longlong),
                ("last_access_time", ctypes.c_longlong),
                ("last_write_time", ctypes.c_longlong),
                ("change_time", ctypes.c_longlong),
                ("file_attributes", wintypes.DWORD),
            ]

        class _FileStandardInfo(ctypes.Structure):
            _fields_ = [
                ("allocation_size", ctypes.c_longlong),
                ("end_of_file", ctypes.c_longlong),
                ("number_of_links", wintypes.DWORD),
                ("delete_pending", wintypes.BOOLEAN),
                ("directory", wintypes.BOOLEAN),
            ]

        class _FileId128(ctypes.Structure):
            _fields_ = [("identifier", ctypes.c_ubyte * 16)]

        class _FileIdInfo(ctypes.Structure):
            _fields_ = [
                ("volume_serial_number", ctypes.c_ulonglong),
                ("file_id", _FileId128),
            ]

        mapping = {
            "FileBasicInfo": (0, _FileBasicInfo),
            "FileStandardInfo": (1, _FileStandardInfo),
            "FileIdInfo": (18, _FileIdInfo),
        }
        try:
            class_number, structure = mapping[info_class]
        except KeyError as exc:
            raise ValueError(f"unsupported Windows file info class: {info_class}") from exc
        value = structure()
        get_info = self._kernel32.GetFileInformationByHandleEx
        get_info.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        get_info.restype = wintypes.BOOL
        if not get_info(handle, class_number, ctypes.byref(value), ctypes.sizeof(value)):
            raise OSError(self.get_last_error(), f"{info_class} failed")
        if info_class == "FileIdInfo":
            return {
                "volume_serial_number": int(value.volume_serial_number),
                "file_id": bytes(value.file_id.identifier),
            }
        if info_class == "FileStandardInfo":
            return {
                "number_of_links": int(value.number_of_links),
                "end_of_file": int(value.end_of_file),
                "directory": bool(value.directory),
            }
        return {
            "file_attributes": int(value.file_attributes),
            "last_write_time": int(value.last_write_time),
            "creation_time": int(value.creation_time),
            "change_time": int(value.change_time),
            "last_access_time": int(value.last_access_time),
        }

    def write_file(self, handle: int, data: bytes) -> int:
        ctypes = self._ctypes
        wintypes = self._wintypes
        written = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(data)
        write_file = self._kernel32.WriteFile
        if not write_file(handle, buffer, len(data), ctypes.byref(written), None):
            raise OSError(self.get_last_error(), "WriteFile failed")
        return int(written.value)

    def read_file(self, handle: int, size: int) -> bytes:
        ctypes = self._ctypes
        wintypes = self._wintypes
        buffer = ctypes.create_string_buffer(size)
        read = wintypes.DWORD()
        read_file = self._kernel32.ReadFile
        if not read_file(handle, buffer, size, ctypes.byref(read), None):
            error_number = self.get_last_error()
            if error_number == 38:
                return b""
            raise OSError(error_number, "ReadFile failed")
        return buffer.raw[: read.value]

    def set_file_pointer_ex(self, handle: int, offset: int) -> None:
        new_position = self._ctypes.c_longlong()
        if not self._kernel32.SetFilePointerEx(
            handle,
            self._ctypes.c_longlong(offset),
            self._ctypes.byref(new_position),
            0,
        ):
            raise OSError(self.get_last_error(), "SetFilePointerEx failed")

    def flush_file_buffers(self, handle: int) -> None:
        if not self._kernel32.FlushFileBuffers(handle):
            raise OSError(self.get_last_error(), "FlushFileBuffers failed")

    def close_handle(self, handle: int) -> None:
        if not self._kernel32.CloseHandle(handle):
            raise OSError(self.get_last_error(), "CloseHandle failed")

    def get_last_error(self) -> int:
        return int(self._ctypes.get_last_error())

    def move_file_ex(self, source: Path, destination: Path, flags: int) -> int:
        return int(self._kernel32.MoveFileExW(str(source), str(destination), flags))


def _windows_native_handle_api_factory() -> _WindowsNativeHandleAPI:
    return _WindowsNativeHandleAPI()


def _windows_pointer_security_attributes_factory(
    user_sid: str,
) -> tuple[Any, Callable[[], None]]:
    """Build a protected DACL for current user, SYSTEM, and Administrators."""

    if os.name != "nt":
        raise RuntimeError("Windows pointer ACL construction is unavailable")
    import ctypes
    from ctypes import wintypes

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("security_descriptor", ctypes.c_void_p),
            ("inherit_handle", wintypes.BOOL),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert.restype = wintypes.BOOL
    descriptor = ctypes.c_void_p()
    descriptor_size = wintypes.DWORD()
    if not user_sid:
        raise ValueError("Windows pointer publication requires a current-user SID")
    sddl = f"D:P(A;;FA;;;{user_sid})(A;;FA;;;SY)(A;;FA;;;BA)"
    if not convert(
        sddl,
        1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    attributes = _SecurityAttributes(
        ctypes.sizeof(_SecurityAttributes),
        descriptor,
        False,
    )
    pointer = ctypes.byref(attributes)
    closed = False

    def cleanup() -> None:
        nonlocal closed
        if not closed:
            closed = True
            kernel32.LocalFree(descriptor)

    return pointer, cleanup


@dataclass
class _RetainedPublicationFile:
    name: str
    handle: Any
    snapshot: dict[str, Any]
    close_callback: Callable[[Any], None]
    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        self.close_callback(self.handle)
        self.closed = True


class _PublicationIdentityMismatch(RuntimeError):
    def __init__(self, destination_name: str) -> None:
        super().__init__(f"moved object identity mismatch at {destination_name}")
        self.destination_name = destination_name


class _ExpectedPriorMismatch(ValueError):
    def __init__(self, recovery_warnings: tuple[str, ...]) -> None:
        super().__init__(
            "video delivery pointer changed before transaction publication"
        )
        self.recovery_warnings = recovery_warnings


def _construction_cleanup_error(
    original_error: BaseException,
    cleanup_errors: tuple[tuple[str, BaseException], ...],
) -> RuntimeError:
    root_text = str(original_error)[:_CONSTRUCTION_ERROR_DISPLAY_LIMIT]
    cleanup_text = "; ".join(
        f"{role}: {str(error)[:_CLEANUP_ERROR_DISPLAY_LIMIT]}"
        for role, error in cleanup_errors
    )
    message = f"{root_text}; construction cleanup failed: {cleanup_text}"
    if len(message) > _CLEANUP_AGGREGATE_DISPLAY_LIMIT:
        message = message[: _CLEANUP_AGGREGATE_DISPLAY_LIMIT - 3] + "..."
    aggregate = RuntimeError(message)
    aggregate.cleanup_errors = cleanup_errors
    return aggregate


def _construction_error_details(
    error: BaseException,
) -> tuple[BaseException, tuple[tuple[str, BaseException], ...]]:
    cleanup_errors = getattr(error, "cleanup_errors", ())
    if not cleanup_errors or type(cleanup_errors) is not tuple or not all(
        type(item) is tuple
        and len(item) == 2
        and isinstance(item[0], str)
        and isinstance(item[1], BaseException)
        for item in cleanup_errors
    ):
        return error, ()
    root_error = error.__cause__
    if root_error is None:
        root_error = error
    return root_error, cleanup_errors


class _VideoPointerBackend:
    def __init__(self, parent: RetainedParent) -> None:
        self.retained_parent = parent
        self.parent = parent.native_parent
        if parent.platform == "posix" and not isinstance(self.parent, int):
            raise RuntimeError("POSIX retained parent requires a directory fd")
        if parent.platform == "windows" and not isinstance(self.parent, Path):
            raise RuntimeError("Windows retained parent requires a guarded path")
        self._windows_api: Any | None = None

    @property
    def is_posix(self) -> bool:
        return self.retained_parent.platform == "posix"

    def _windows_handle_api(self) -> Any:
        if self.is_posix:
            raise RuntimeError("Windows handle API is unavailable on POSIX")
        if self._windows_api is None:
            self._windows_api = _windows_native_handle_api_factory()
        return self._windows_api

    def list_names(self) -> list[str]:
        self.retained_parent.validate()
        return sorted(os.listdir(self.parent))

    @staticmethod
    def _stable_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        return dict(snapshot.get("stable") or {})

    @classmethod
    def snapshots_match(
        cls,
        expected: dict[str, Any],
        observed: dict[str, Any],
    ) -> bool:
        return (
            expected.get("platform") == observed.get("platform")
            and cls._stable_snapshot(expected) == cls._stable_snapshot(observed)
        )

    def _posix_snapshot(self, descriptor: int, *, max_bytes: int | None) -> dict[str, Any]:
        metadata = os.fstat(descriptor)
        _validate_run_member_file(metadata, label="video pointer transaction member")
        if max_bytes is not None and metadata.st_size > max_bytes:
            raise ValueError("video pointer transaction record exceeds 64 KiB")
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, min(1 << 20, (max_bytes or (1 << 20)) + 1))
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ValueError("video pointer transaction record exceeds 64 KiB")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if not _run_member_file_unchanged(metadata, after):
            raise ValueError("video pointer transaction member changed during read")
        return {
            "platform": "posix",
            "stable": {
                "dev": int(after.st_dev),
                "ino": int(after.st_ino),
                "mode": int(after.st_mode),
                "nlink": int(after.st_nlink),
                "size": int(after.st_size),
                "mtime_ns": int(after.st_mtime_ns),
                "sha256": digest.hexdigest(),
            },
            "audit": {"ctime_ns": int(after.st_ctime_ns)},
        }

    def _windows_snapshot(self, handle: Any, *, max_bytes: int | None) -> dict[str, Any]:
        windows_api = self._windows_handle_api()
        identity = windows_api.get_file_information_by_handle_ex(
            handle,
            "FileIdInfo",
        )
        standard = windows_api.get_file_information_by_handle_ex(
            handle,
            "FileStandardInfo",
        )
        basic = windows_api.get_file_information_by_handle_ex(
            handle,
            "FileBasicInfo",
        )
        file_id = bytes(identity["file_id"])
        if len(file_id) != 16:
            raise ValueError("video pointer transaction file ID is not 128-bit")
        reparse_attribute = 0x400
        if (
            standard.get("directory")
            or int(standard.get("number_of_links", 0)) != 1
            or int(basic.get("file_attributes", 0)) & reparse_attribute
        ):
            raise ValueError("video pointer transaction member is unsafe")
        size = int(standard.get("end_of_file", -1))
        if size < 0 or (max_bytes is not None and size > max_bytes):
            raise ValueError("video pointer transaction record exceeds 64 KiB")
        windows_api.set_file_pointer_ex(handle, 0)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = bytes(windows_api.read_file(handle, 1 << 20))
            if not chunk:
                break
            total += len(chunk)
            if total > size or (max_bytes is not None and total > max_bytes):
                raise ValueError("video pointer transaction member changed during read")
            digest.update(chunk)
        if total != size:
            raise ValueError("video pointer transaction member changed during read")
        return {
            "platform": "windows",
            "stable": {
                "volume_serial_number": int(identity["volume_serial_number"]),
                "file_id": file_id.hex(),
                "file_attributes": int(basic["file_attributes"]),
                "reparse": False,
                "nlink": int(standard["number_of_links"]),
                "size": size,
                "last_write_time": int(basic["last_write_time"]),
                "sha256": digest.hexdigest(),
            },
            "audit": {
                "creation_time": int(basic["creation_time"]),
                "change_time": int(basic["change_time"]),
                "last_access_time": int(basic["last_access_time"]),
            },
        }

    def snapshot(
        self,
        retained: _RetainedPublicationFile,
        *,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        if self.is_posix:
            return self._posix_snapshot(retained.handle, max_bytes=max_bytes)
        return self._windows_snapshot(retained.handle, max_bytes=max_bytes)

    def create(self, name: str, data: bytes) -> _RetainedPublicationFile:
        self.retained_parent.validate()
        if self.is_posix:
            descriptor = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                0o600,
                dir_fd=self.parent,
            )
            retained = _RetainedPublicationFile(name, descriptor, {}, os.close)
            try:
                os.fchmod(descriptor, 0o600)
                _write_all(descriptor, data)
                os.fsync(descriptor)
                retained.snapshot = self._posix_snapshot(descriptor, max_bytes=None)
                return retained
            except BaseException:
                retained.close()
                raise
        assert isinstance(self.parent, Path)
        windows_api = self._windows_handle_api()
        security_attributes, cleanup = _windows_pointer_security_attributes_factory(
            self.retained_parent.windows_user_sid or ""
        )
        handle: Any = None
        try:
            handle = windows_api.create_file(
                self.parent / name,
                0x80000000 | 0x40000000 | 0x0080,
                0x1 | 0x2 | 0x4,
                security_attributes,
                1,
                0x00200000,
            )
        finally:
            cleanup()
        retained = _RetainedPublicationFile(
            name,
            handle,
            {},
            windows_api.close_handle,
        )
        try:
            offset = 0
            while offset < len(data):
                written = int(windows_api.write_file(handle, data[offset:]))
                if written <= 0:
                    raise OSError("WriteFile made no progress")
                offset += written
            windows_api.flush_file_buffers(handle)
            retained.snapshot = self._windows_snapshot(handle, max_bytes=None)
            return retained
        except BaseException:
            retained.close()
            raise

    def open(
        self,
        name: str,
        *,
        max_bytes: int | None = None,
    ) -> _RetainedPublicationFile:
        self.retained_parent.validate()
        if self.is_posix:
            descriptor = os.open(
                name,
                os.O_RDONLY | _NOFOLLOW | _CLOEXEC | getattr(os, "O_NONBLOCK", 0),
                dir_fd=self.parent,
            )
            retained = _RetainedPublicationFile(name, descriptor, {}, os.close)
            try:
                retained.snapshot = self._posix_snapshot(descriptor, max_bytes=max_bytes)
                return retained
            except BaseException:
                retained.close()
                raise
        assert isinstance(self.parent, Path)
        windows_api = self._windows_handle_api()
        handle = windows_api.create_file(
            self.parent / name,
            0x80000000 | 0x0080,
            0x1 | 0x2 | 0x4,
            None,
            3,
            0x00200000,
        )
        retained = _RetainedPublicationFile(
            name,
            handle,
            {},
            windows_api.close_handle,
        )
        try:
            retained.snapshot = self._windows_snapshot(handle, max_bytes=max_bytes)
            return retained
        except BaseException:
            retained.close()
            raise

    def try_open(
        self,
        name: str,
        *,
        max_bytes: int | None = None,
    ) -> _RetainedPublicationFile | None:
        try:
            return self.open(name, max_bytes=max_bytes)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if getattr(exc, "winerror", None) in {2, 3} or exc.errno in {2, 3}:
                return None
            raise

    def read_bytes(
        self,
        retained: _RetainedPublicationFile,
        *,
        max_bytes: int,
    ) -> bytes:
        if self.is_posix:
            metadata = os.fstat(retained.handle)
            if metadata.st_size > max_bytes:
                raise ValueError("video pointer transaction record exceeds 64 KiB")
            os.lseek(retained.handle, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(retained.handle, min(1 << 16, max_bytes + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("video pointer transaction record exceeds 64 KiB")
                chunks.append(chunk)
            return b"".join(chunks)
        windows_api = self._windows_handle_api()
        windows_api.set_file_pointer_ex(retained.handle, 0)
        chunks = []
        total = 0
        while True:
            chunk = bytes(windows_api.read_file(retained.handle, 1 << 16))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("video pointer transaction record exceeds 64 KiB")
            chunks.append(chunk)
        return b"".join(chunks)

    def sync_parent(self) -> None:
        self.retained_parent.validate()
        if self.is_posix:
            os.fsync(self.parent)

    def move(
        self,
        source: _RetainedPublicationFile,
        destination_name: str,
    ) -> _RetainedPublicationFile:
        self.retained_parent.validate()
        current_source = self.open(source.name)
        try:
            if not self.snapshots_match(source.snapshot, current_source.snapshot):
                raise _PublicationIdentityMismatch(source.name)
        finally:
            current_source.close()
        move_error: BaseException | None = None
        try:
            _atomic_no_replace_rename(self.parent, source.name, destination_name)
        except BaseException as exc:
            move_error = exc
        destination = self.try_open(destination_name)
        source_after = self.try_open(source.name)
        try:
            moved_exactly = (
                destination is not None
                and source_after is None
                and self.snapshots_match(source.snapshot, destination.snapshot)
            )
            if move_error is not None and not moved_exactly:
                raise move_error
            if destination is None:
                raise RuntimeError("no-replace move did not produce a destination")
            if not moved_exactly:
                raise _PublicationIdentityMismatch(destination_name)
            self.sync_parent()
            self.retained_parent.validate()
            return destination
        finally:
            if source_after is not None:
                source_after.close()
            if destination is not None and (
                not self.snapshots_match(source.snapshot, destination.snapshot)
                or move_error is not None and not moved_exactly
            ):
                destination.close()

    def validate_name(
        self,
        name: str,
        expected: dict[str, Any],
    ) -> _RetainedPublicationFile | None:
        observed = self.try_open(name)
        if observed is None:
            return None
        if not self.snapshots_match(expected, observed.snapshot):
            observed.close()
            return None
        return observed


def observe_video_delivery_pointer(
    parent: RetainedParent,
) -> VideoPointerObservation | None:
    """Observe the pointer through one borrowed native retained leaf."""

    backend = _VideoPointerBackend(parent)
    retained = backend.try_open(
        _VIDEO_POINTER_TARGET,
        max_bytes=_VIDEO_POINTER_MAX_BYTES,
    )
    if retained is None:
        parent.validate()
        return None
    try:
        before_platform = retained.snapshot.get("platform")
        before_stable_value = retained.snapshot.get("stable")
        if (
            before_platform not in _VIDEO_POINTER_STABLE_KEYS
            or not isinstance(before_stable_value, Mapping)
        ):
            raise ValueError("video pointer native snapshot is incomplete")
        before_stable = dict(before_stable_value)

        data = backend.read_bytes(
            retained,
            max_bytes=_VIDEO_POINTER_MAX_BYTES,
        )
        after = backend.snapshot(
            retained,
            max_bytes=_VIDEO_POINTER_MAX_BYTES,
        )
        after_platform = after.get("platform")
        after_stable_value = after.get("stable")
        if (
            after_platform not in _VIDEO_POINTER_STABLE_KEYS
            or not isinstance(after_stable_value, Mapping)
        ):
            raise ValueError("video pointer native snapshot is incomplete")
        after_stable = dict(after_stable_value)
        required_keys = _VIDEO_POINTER_STABLE_KEYS[before_platform]
        if (
            before_platform != parent.platform
            or after_platform != before_platform
            or frozenset(before_stable) != required_keys
            or frozenset(after_stable) != required_keys
            or after_stable != before_stable
        ):
            raise ValueError("video pointer changed during native observation")
        size = before_stable.get("size")
        digest = before_stable.get("sha256")
        if (
            type(size) is not int
            or size < 0
            or len(data) != size
            or not isinstance(digest, str)
            or hashlib.sha256(data).hexdigest() != digest
        ):
            raise ValueError(
                "video pointer bytes do not match the native observation"
            )
        parent.validate()
        return VideoPointerObservation(
            data=data,
            platform=before_platform,
            stable=before_stable,
        )
    finally:
        retained.close()


class _VideoPointerTransaction:
    def __init__(
        self,
        *,
        backend: _VideoPointerBackend,
        txid: str,
        sequence: int = 0,
        phase: str = "",
        new_snapshot: dict[str, Any] | None = None,
        prior_snapshot: dict[str, Any] | None = None,
        had_prior: bool = False,
        precondition: Callable[[], None] | None = None,
    ) -> None:
        self.backend = backend
        self.txid = txid
        self.sequence = sequence
        self.phase = phase
        self.target_name = _VIDEO_POINTER_TARGET
        self.new_name = f".{self.target_name}.{txid}.new"
        self.prior_name = f".{self.target_name}.{txid}.prior"
        self.displaced_name = f".{self.target_name}.{txid}.displaced"
        self.new_snapshot = new_snapshot
        self.prior_snapshot = prior_snapshot
        self.had_prior = had_prior
        self.precondition = precondition
        self.new_file: _RetainedPublicationFile | None = None
        self.prior_file: _RetainedPublicationFile | None = None
        self._retained: list[_RetainedPublicationFile] = []
        self._conflicts = 0
        self.durable_committed = phase in {
            "committed",
            "recovery-committed-confirmed",
        }
        self.cleanup_warnings: list[str] = []
        self.closed = False
        self.owns_parent = False

    def assert_precondition(self, *, checkpoint: str) -> None:
        if self.precondition is None:
            return
        try:
            self.precondition()
        except Exception as exc:
            if checkpoint == "precommit":
                message = (
                    "video delivery snapshot precommit precondition failed "
                    f"before durable commit: {exc}"
                )
            else:
                message = (
                    "video delivery snapshot prepublication precondition failed: "
                    f"{exc}"
                )
            raise VideoPointerPublicationPreconditionError(message) from exc

    def _remember(self, retained: _RetainedPublicationFile | None) -> None:
        if retained is not None and retained not in self._retained:
            self._retained.append(retained)

    def _construction_cleanup_role(
        self,
        retained: _RetainedPublicationFile,
        stored_index: int,
    ) -> str:
        if retained is self.new_file:
            return "new_file"
        if retained is self.prior_file:
            return "prior_file"
        return f"retained_file_{stored_index}"

    def _phase_payload(self, phase: str, **details: Any) -> dict[str, Any]:
        return {
            "protocol_version": _VIDEO_POINTER_PROTOCOL_VERSION,
            "sequence": self.sequence + 1,
            "txid": self.txid,
            "phase": phase,
            "target_name": self.target_name,
            "new_name": self.new_name,
            "prior_name": self.prior_name,
            "displaced_name": self.displaced_name,
            "had_prior": self.had_prior,
            "new_snapshot": self.new_snapshot,
            "prior_snapshot": self.prior_snapshot,
            "details": details,
        }

    def append_phase(self, phase: str, **details: Any) -> None:
        if phase not in _VIDEO_POINTER_PHASES:
            raise ValueError(f"unknown video pointer transaction phase: {phase}")
        if self.sequence >= _VIDEO_POINTER_PHASE_LIMIT:
            raise RuntimeError("video pointer transaction phase limit exceeded")
        payload = self._phase_payload(phase, **details)
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _VIDEO_POINTER_PHASE_SIZE_LIMIT:
            raise RuntimeError("video pointer transaction phase record exceeds 64 KiB")
        sequence = self.sequence + 1
        final_name = (
            f".{self.target_name}.{self.txid}."
            f"phase-{sequence:06d}-{phase}.json"
        )
        writing_name = f"{final_name}.{uuid.uuid4().hex}.writing"
        writing = self.backend.create(writing_name, encoded)
        try:
            published = self.backend.move(writing, final_name)
            published.close()
        finally:
            writing.close()
        self.sequence = sequence
        self.phase = phase
        if phase in {"committed", "recovery-committed-confirmed"}:
            self.durable_committed = True
        _video_pointer_transaction_phase_hook(
            phase,
            txid=self.txid,
            sequence=sequence,
            details=details,
        )

    @classmethod
    def begin(
        cls,
        backend: _VideoPointerBackend,
        encoded: bytes,
        *,
        expected_prior: dict[str, Any] | None,
        precondition: Callable[[], None] | None = None,
    ) -> "_VideoPointerTransaction":
        recovery_warnings = tuple(_recover_video_pointer_transactions(backend))
        prior: _RetainedPublicationFile | None = None
        tx: _VideoPointerTransaction | None = None
        prior_owned_by_tx = False
        try:
            prior = backend.try_open(_VIDEO_POINTER_TARGET)
            if not _expected_prior_matches(
                backend,
                expected_prior=expected_prior,
                observed_prior=prior,
            ):
                raise _ExpectedPriorMismatch(recovery_warnings)

            tx = cls(
                backend=backend,
                txid=uuid.uuid4().hex,
                precondition=precondition,
            )
            tx.cleanup_warnings.extend(recovery_warnings)
            if prior is not None:
                tx._remember(prior)
                prior_owned_by_tx = True
                tx.prior_file = prior
                tx.prior_snapshot = prior.snapshot
                tx.had_prior = True
            new_file = backend.create(tx.new_name, encoded)
            tx._remember(new_file)
            tx.new_file = new_file
            tx.new_snapshot = new_file.snapshot
            tx.append_phase("prepared")
            backend.retained_parent.transfer()
            tx.owns_parent = True
            return tx
        except BaseException as original_error:
            cleanup_errors: list[tuple[str, BaseException]] = []
            attempted: set[int] = set()
            if tx is not None:
                for stored_index in reversed(range(len(tx._retained))):
                    retained = tx._retained[stored_index]
                    retained_identity = id(retained)
                    if retained_identity in attempted:
                        continue
                    attempted.add(retained_identity)
                    try:
                        retained.close()
                    except BaseException as close_error:
                        cleanup_errors.append((
                            tx._construction_cleanup_role(
                                retained,
                                stored_index,
                            ),
                            close_error,
                        ))
            if (
                prior is not None
                and not prior_owned_by_tx
                and id(prior) not in attempted
            ):
                try:
                    prior.close()
                except BaseException as close_error:
                    cleanup_errors.append(("prior_file", close_error))
            if cleanup_errors:
                aggregate = _construction_cleanup_error(
                    original_error,
                    tuple(cleanup_errors),
                )
                raise aggregate from original_error
            raise

    @classmethod
    def from_record(
        cls,
        backend: _VideoPointerBackend,
        payload: dict[str, Any],
        *,
        sequence: int,
        phase: str,
    ) -> "_VideoPointerTransaction":
        tx = cls(
            backend=backend,
            txid=str(payload["txid"]),
            sequence=sequence,
            phase=phase,
            new_snapshot=payload.get("new_snapshot"),
            prior_snapshot=payload.get("prior_snapshot"),
            had_prior=bool(payload.get("had_prior")),
        )
        if payload.get("target_name") != tx.target_name:
            raise ValueError("video pointer transaction target mismatch")
        expected_roles = {
            "new_name": tx.new_name,
            "prior_name": tx.prior_name,
            "displaced_name": tx.displaced_name,
        }
        for key, expected in expected_roles.items():
            if payload.get(key) != expected:
                raise ValueError("video pointer transaction role mismatch")
        if not isinstance(tx.new_snapshot, dict):
            raise ValueError("video pointer transaction new snapshot is missing")
        if tx.had_prior != isinstance(tx.prior_snapshot, dict):
            raise ValueError("video pointer transaction prior snapshot is invalid")
        return tx

    def forward(self) -> None:
        try:
            if self.had_prior:
                assert self.prior_file is not None
                self.append_phase("quarantine-intent")
                quarantined = self.backend.move(self.prior_file, self.prior_name)
                quarantined.close()
                self.append_phase("prior-quarantined")
            else:
                self.append_phase("no-prior-confirmed")
            self.append_phase("publish-intent")
            self.assert_precondition(checkpoint="prepublication")
            assert self.new_file is not None
            published = self.backend.move(self.new_file, self.target_name)
            published.close()
            self.append_phase("published")
        except VideoPointerPublicationPreconditionError:
            raise
        except _PublicationIdentityMismatch as exc:
            self._retain_conflict(exc.destination_name, reason=str(exc))
            raise
        except BaseException as exc:
            self.reconciliation_required(reason=f"forward publication failed: {exc}")
            raise

    def _target_state(self) -> tuple[str, _RetainedPublicationFile | None]:
        target = self.backend.try_open(self.target_name)
        if target is None:
            return "absent", None
        if self.new_snapshot and self.backend.snapshots_match(
            self.new_snapshot,
            target.snapshot,
        ):
            return "new", target
        if self.prior_snapshot and self.backend.snapshots_match(
            self.prior_snapshot,
            target.snapshot,
        ):
            return "prior", target
        return "foreign", target

    def _retain_conflict(self, name: str, *, reason: str) -> None:
        if self._conflicts >= _VIDEO_POINTER_CONFLICT_LIMIT:
            self.reconciliation_required(reason="video pointer conflict limit exceeded")
            return
        unexpected = self.backend.try_open(name)
        if unexpected is None:
            self.reconciliation_required(reason=reason)
            return
        self._conflicts += 1
        conflict_name = (
            f".{self.target_name}.{self.txid}."
            f"conflict-{self._conflicts:04d}-{uuid.uuid4().hex[:12]}"
        )
        self.append_phase(
            "conflict-intent",
            source_name=name,
            conflict_name=conflict_name,
            reason=reason,
        )
        try:
            moved = self.backend.move(unexpected, conflict_name)
            moved.close()
        finally:
            unexpected.close()
        self.reconciliation_required(reason=reason, conflict_name=conflict_name)

    def reconciliation_required(self, *, reason: str, **details: Any) -> None:
        if self.phase != "reconciliation-required":
            self.append_phase("reconciliation-required", reason=reason, **details)

    def abort(self) -> None:
        if self.phase in _VIDEO_POINTER_TERMINAL_PHASES:
            return
        errors: list[str] = []
        try:
            self.append_phase("aborting", from_phase=self.phase)
            target_state, target = self._target_state()
            if target_state == "foreign":
                assert target is not None
                target.close()
                self._retain_conflict(
                    self.target_name,
                    reason="foreign target encountered during abort",
                )
                return
            if target_state == "new":
                assert target is not None
                target.close()
                self.append_phase(
                    "displace-intent",
                    source_name=self.target_name,
                    displaced_name=self.displaced_name,
                )
                source = self.backend.open(self.target_name)
                try:
                    displaced = self.backend.move(source, self.displaced_name)
                    if not self.backend.snapshots_match(
                        self.new_snapshot or {},
                        displaced.snapshot,
                    ):
                        displaced.close()
                        self._retain_conflict(
                            self.displaced_name,
                            reason="foreign object displaced during abort",
                        )
                        return
                    displaced.close()
                except _PublicationIdentityMismatch as exc:
                    self._retain_conflict(exc.destination_name, reason=str(exc))
                    return
                finally:
                    source.close()
                self.append_phase("displaced")
                target_state = "absent"
            elif target is not None:
                target.close()

            if self.had_prior and target_state == "absent":
                prior = self.backend.validate_name(
                    self.prior_name,
                    self.prior_snapshot or {},
                )
                if prior is None:
                    self.reconciliation_required(
                        reason="retained prior pointer is missing or mutated"
                    )
                    return
                self.append_phase(
                    "restore-intent",
                    source_name=self.prior_name,
                    target_name=self.target_name,
                )
                try:
                    restored = self.backend.move(prior, self.target_name)
                    if not self.backend.snapshots_match(
                        self.prior_snapshot or {},
                        restored.snapshot,
                    ):
                        restored.close()
                        self._retain_conflict(
                            self.target_name,
                            reason="restored prior pointer changed identity",
                        )
                        return
                    restored.close()
                except _PublicationIdentityMismatch as exc:
                    self._retain_conflict(exc.destination_name, reason=str(exc))
                    return
                finally:
                    prior.close()
            elif self.had_prior and target_state != "prior":
                self.reconciliation_required(
                    reason="prior pointer pre-state could not be restored"
                )
                return
            self.append_phase("aborted")
        except BaseException as exc:
            errors.append(str(exc))
            try:
                self.reconciliation_required(reason=f"abort failed: {exc}")
            except BaseException as marker_exc:
                errors.append(f"reconciliation marker failed: {marker_exc}")
            raise RuntimeError("; ".join(errors)) from exc

    def commit(self) -> None:
        if self.phase != "published":
            raise RuntimeError("video pointer transaction is not publish-ready")
        target = self.backend.validate_name(
            self.target_name,
            self.new_snapshot or {},
        )
        if target is None:
            self.reconciliation_required(
                reason="published pointer changed before durable commit"
            )
            raise RuntimeError("published pointer changed before durable commit")
        target.close()
        self.append_phase("committed")

    def confirm_recovered_commit(self) -> None:
        target = self.backend.validate_name(
            self.target_name,
            self.new_snapshot or {},
        )
        if target is None:
            self.reconciliation_required(
                reason="committed pointer does not match retained new snapshot"
            )
            raise RuntimeError("committed pointer requires reconciliation")
        target.close()
        self.backend.sync_parent()
        self.append_phase("recovery-committed-confirmed")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        errors: list[str] = []
        for retained in reversed(self._retained):
            try:
                retained.close()
            except BaseException as exc:
                errors.append(str(exc))
        if self.owns_parent:
            try:
                self.backend.retained_parent.close()
            except BaseException as exc:
                errors.append(str(exc))
        try:
            _publication_resource_close_hook(
                durable_committed=self.durable_committed,
                transaction=self,
            )
        except BaseException as exc:
            errors.append(str(exc))
        if errors:
            raise OSError("; ".join(errors))


def _validate_phase_payload(
    payload: Any,
    *,
    txid: str,
    sequence: int,
    phase: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("video pointer transaction phase must contain an object")
    expected = {
        "protocol_version": _VIDEO_POINTER_PROTOCOL_VERSION,
        "txid": txid,
        "sequence": sequence,
        "phase": phase,
        "target_name": _VIDEO_POINTER_TARGET,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(f"video pointer transaction phase {key} mismatch")
    return payload


def _scan_video_pointer_transactions(
    backend: _VideoPointerBackend,
) -> list[tuple[str, int, str, dict[str, Any]]]:
    grouped: dict[str, list[tuple[int, str, dict[str, Any]]]] = {}
    for name in backend.list_names():
        match = _VIDEO_POINTER_PHASE_RE.fullmatch(name)
        if match is None:
            continue
        txid, sequence_text, phase = match.groups()
        if phase not in _VIDEO_POINTER_PHASES:
            raise ValueError("unknown video pointer transaction phase")
        retained = backend.open(name, max_bytes=_VIDEO_POINTER_PHASE_SIZE_LIMIT)
        try:
            if backend.is_posix:
                mode = int(retained.snapshot["stable"]["mode"])
                if stat.S_IMODE(mode) != 0o600:
                    raise ValueError("video pointer phase record must have mode 0600")
            raw = backend.read_bytes(
                retained,
                max_bytes=_VIDEO_POINTER_PHASE_SIZE_LIMIT,
            )
        finally:
            retained.close()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid video pointer transaction phase JSON") from exc
        sequence = int(sequence_text)
        grouped.setdefault(txid, []).append(
            (
                sequence,
                phase,
                _validate_phase_payload(
                    payload,
                    txid=txid,
                    sequence=sequence,
                    phase=phase,
                ),
            )
        )
    active: list[tuple[str, int, str, dict[str, Any]]] = []
    committed: list[tuple[str, int, str, dict[str, Any]]] = []
    for txid, records in grouped.items():
        records.sort(key=lambda item: item[0])
        if len(records) > _VIDEO_POINTER_PHASE_LIMIT:
            raise ValueError("video pointer transaction has too many phase records")
        expected_sequences = list(range(1, len(records) + 1))
        if [item[0] for item in records] != expected_sequences:
            raise ValueError("video pointer transaction phases are noncontiguous")
        sequence, phase, payload = records[-1]
        record = (txid, sequence, phase, payload)
        if phase == "committed":
            committed.append(record)
        elif phase not in _VIDEO_POINTER_TERMINAL_PHASES:
            active.append(record)
    if len(active) > 1:
        raise ValueError("multiple incomplete video pointer transactions")
    return committed + active


def _recover_video_pointer_transactions(backend: _VideoPointerBackend) -> list[str]:
    warnings: list[str] = []
    for txid, sequence, phase, payload in _scan_video_pointer_transactions(backend):
        tx = _VideoPointerTransaction.from_record(
            backend,
            payload,
            sequence=sequence,
            phase=phase,
        )
        recovery_error: BaseException | None = None
        try:
            if phase == "committed":
                tx.confirm_recovered_commit()
            else:
                tx.abort()
        except BaseException as exc:
            recovery_error = exc
        close_error: BaseException | None = None
        try:
            tx.close()
        except BaseException as exc:
            close_error = exc
        recovered_commit_confirmed = (
            tx.phase == "recovery-committed-confirmed"
        )
        if recovery_error is not None and not recovered_commit_confirmed:
            if close_error is not None:
                raise ValueError(
                    f"{recovery_error}; recovery close failed: {close_error}"
                ) from recovery_error
            raise recovery_error
        if recovery_error is not None:
            warnings.append(
                f"post-commit recovery diagnostic failed: {recovery_error}"
            )
        if close_error is not None:
            if not recovered_commit_confirmed:
                raise close_error
            warnings.append(f"post-commit recovery close failed: {close_error}")
    return warnings


def recover_video_delivery_pointer(parent: RetainedParent) -> tuple[str, ...]:
    """Recover interrupted transactions without taking ownership of the parent."""

    return tuple(_recover_video_pointer_transactions(_VideoPointerBackend(parent)))


def _expected_prior_matches(
    backend: _VideoPointerBackend,
    *,
    expected_prior: dict[str, Any] | None,
    observed_prior: _RetainedPublicationFile | None,
) -> bool:
    if expected_prior is None:
        return observed_prior is None
    if observed_prior is None:
        return False
    platform = "posix" if backend.is_posix else "windows"
    if (
        expected_prior.get("platform") != platform
        or observed_prior.snapshot.get("platform") != platform
    ):
        return False
    expected_stable = expected_prior.get("stable")
    observed_stable = observed_prior.snapshot.get("stable")
    if not isinstance(expected_stable, dict) or not isinstance(
        observed_stable,
        dict,
    ):
        return False
    required = _VIDEO_POINTER_STABLE_KEYS[platform]
    return (
        frozenset(expected_stable) == required
        and frozenset(observed_stable) == required
        and observed_stable == expected_stable
    )


def stage_video_delivery_pointer(
    parent: RetainedParent,
    encoded: bytes,
    *,
    expected_prior: dict[str, Any] | None,
    recovery_warnings: tuple[str, ...] = (),
    precondition: Callable[[], None] | None = None,
) -> _VideoPointerTransaction:
    """Recover prior state, publish one new pointer, and transfer parent ownership."""

    backend = _VideoPointerBackend(parent)
    transaction: _VideoPointerTransaction | None = None
    try:
        transaction = _VideoPointerTransaction.begin(
            backend,
            encoded,
            expected_prior=expected_prior,
            precondition=precondition,
        )
        transaction.forward()
        return transaction
    except BaseException as original_error:
        if transaction is None:
            root_error, cleanup_errors = _construction_error_details(
                original_error
            )
            unique_recovery_warnings = tuple(dict.fromkeys((
                *recovery_warnings,
                *getattr(original_error, "recovery_warnings", ()),
                *getattr(root_error, "recovery_warnings", ()),
            )))
            try:
                parent.close()
            except BaseException as close_error:
                aggregate = _construction_cleanup_error(
                    root_error,
                    cleanup_errors + (("retained_parent", close_error),),
                )
                aggregate.recovery_warnings = unique_recovery_warnings
                raise aggregate from root_error
            original_error.recovery_warnings = unique_recovery_warnings
            if (
                not cleanup_errors
                and original_error is root_error
                and isinstance(root_error, _ExpectedPriorMismatch)
                and unique_recovery_warnings
            ):
                warning_text = "; ".join(unique_recovery_warnings)
                display = f"{original_error}; recovery warnings: {warning_text}"
                if len(display) > _CLEANUP_AGGREGATE_DISPLAY_LIMIT:
                    display = (
                        display[: _CLEANUP_AGGREGATE_DISPLAY_LIMIT - 3]
                        + "..."
                    )
                original_error.args = (display,)
            raise

        errors = [str(original_error)]
        visible_recovery_warnings = list(recovery_warnings)
        visible_recovery_warnings.extend(
            getattr(original_error, "recovery_warnings", ())
        )
        visible_recovery_warnings.extend(transaction.cleanup_warnings)
        try:
            transaction.abort()
        except BaseException as rollback_error:
            errors.append(f"secure publication rollback failed: {rollback_error}")
        try:
            transaction.close()
        except BaseException as close_error:
            errors.append(f"secure publication rollback close failed: {close_error}")
        unique_recovery_warnings = tuple(dict.fromkeys(visible_recovery_warnings))
        if unique_recovery_warnings:
            errors.append(
                "recovery warnings: " + "; ".join(unique_recovery_warnings)
            )
        if len(errors) > 1:
            raise ValueError("; ".join(errors)) from original_error
        raise


VideoPointerPublication = _VideoPointerTransaction
