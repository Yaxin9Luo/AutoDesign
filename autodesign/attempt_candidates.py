"""Durable immutable snapshots for external-author attempts."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import sys
import tempfile
import threading
from types import MappingProxyType
from typing import Any, Callable, Literal
from urllib.parse import quote, unquote_to_bytes, urlsplit
import uuid

from pydantic import ValidationError

from .schema import (
    ArtifactType,
    AttemptCandidate,
    AttemptCandidateIndex,
    AttemptIssue,
    AttemptSafetyState,
    AttemptSelectionJournal,
)
from .util.io import atomic_write_json, sha256_file
from .video_pointer_transaction import (
    RetainedParent,
    VideoPointerPublication,
    observe_video_delivery_pointer,
    recover_video_delivery_pointer,
    stage_video_delivery_pointer,
)


_RUNTIME_OS = os


_INDEX_RELATIVE_PATH = Path("attempt_candidates/index.json")
_SELECTION_RELATIVE_PATH = Path("attempt_candidates/selection.json")
_CONTROL_DIRECTORY_NAME = "attempt_candidates"
_SELECTION_TRANSACTION_NAME = "selection_adapter_transaction.json"
_STORE_LOCKS: dict[str, threading.RLock] = {}
_STORE_LOCKS_GUARD = threading.Lock()
_SECURE_DIR_FD_AVAILABLE = os.name == "posix" and os.open in os.supports_dir_fd
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_BROWSER_PREVIEW_RESOURCE_SUFFIXES = frozenset({
    ".avif",
    ".bmp",
    ".css",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".mjs",
    ".otf",
    ".png",
    ".svg",
    ".ttf",
    ".webp",
    ".woff",
    ".woff2",
})
_PROMOTION_BROWSER_ORIGIN = "https://autodesign.invalid"
_BROWSER_BLOCKED_INTERNAL_PARTS = frozenset({_CONTROL_DIRECTORY_NAME})
_BROWSER_BLOCKED_INTERNAL_NAMES = frozenset({
    "run_control.json",
    "run_events.jsonl",
})
_BROWSER_BLOCKED_SUFFIXES = frozenset({".json", ".jsonl", ".log"})
_PROMOTION_BROWSER_MAX_DEPENDENCIES = 512
_PROMOTION_BROWSER_MAX_RESOURCES = 512
_PROMOTION_BROWSER_MAX_DEPENDENCY_BYTES = 64 * 1024**2
_PROMOTION_BROWSER_MAX_TOTAL_BYTES = 256 * 1024**2
_PROMOTION_BROWSER_MAX_DOCUMENT_BYTES = 32 * 1024**2
_PROMOTION_BROWSER_MAX_MANIFEST_BYTES = 4 * 1024**2
_MOVEFILE_REPLACE_EXISTING = 0x1
_MOVEFILE_COPY_ALLOWED = 0x2
_MOVEFILE_WRITE_THROUGH = 0x8
_VIDEO_POINTER_PAYLOAD_UNSET = object()


class _PromotionLeaseBinding:
    def __init__(
        self,
        *,
        requested_run_path: Path,
        canonical_run_path: Path,
        filesystem_run_path: Path,
        run_identity: tuple[int, int],
        run_reference: int | Path,
        control_reference: int | Path,
    ) -> None:
        self.requested_run_path = requested_run_path
        self.canonical_run_path = canonical_run_path
        self.filesystem_run_path = filesystem_run_path
        self.run_identity = run_identity
        self.run_reference = run_reference
        self.control_reference = control_reference
        self._lease_open = True

    def assert_requested_run_unchanged(self) -> None:
        metadata = _portable_lstat(self.requested_run_path)
        _validate_directory(metadata)
        current_identity = (metadata.st_dev, metadata.st_ino)
        if current_identity != self.run_identity:
            raise ValueError("attempt selection run directory changed during lease")

    def assert_active_and_unchanged(self) -> None:
        if not self._lease_open or _ACTIVE_PROMOTION_LEASE.get() is not self:
            raise ValueError("attempt promotion lease path is no longer active")
        self.assert_requested_run_unchanged()

    def assert_active(self) -> None:
        if not self._lease_open or _ACTIVE_PROMOTION_LEASE.get() is not self:
            raise ValueError("attempt promotion lease path is no longer active")

    def assert_open_and_unchanged(self) -> None:
        if not self._lease_open:
            raise ValueError("attempt promotion lease is no longer open")
        self.assert_requested_run_unchanged()

    def close(self) -> None:
        self._lease_open = False


class _StableCoordinationBinding:
    def __init__(
        self,
        *,
        requested_run_path: Path,
        canonical_run_path: Path,
        filesystem_run_path: Path,
        run_identity: tuple[int, int],
        run_reference: int | Path,
        path_replacement_guarded: bool = False,
    ) -> None:
        self.requested_run_path = requested_run_path
        self.canonical_run_path = canonical_run_path
        self.filesystem_run_path = filesystem_run_path
        self.run_identity = run_identity
        self.run_reference = run_reference
        self.path_replacement_guarded = path_replacement_guarded


class _PromotionLeasePath(type(Path())):
    def __new__(
        cls,
        *pathsegments: object,
        validator,
        display_root: Path | None = None,
        filesystem_root: Path | None = None,
    ):
        return super().__new__(cls, *pathsegments)

    def __init__(
        self,
        *pathsegments: object,
        validator,
        display_root: Path | None = None,
        filesystem_root: Path | None = None,
    ) -> None:
        self._lease_validator = validator
        self._lease_display_root = display_root
        self._lease_filesystem_root = filesystem_root
        try:
            super().__init__(*pathsegments)
        except TypeError:
            super().__init__()

    def _lease_validator_callback(self):
        validator = getattr(self, "_lease_validator", None)
        if validator is not None:
            return validator
        binding = _ACTIVE_PROMOTION_LEASE.get()
        if binding is None:
            raise ValueError("attempt promotion lease path is no longer active")
        return binding.assert_active_and_unchanged

    def with_segments(self, *pathsegments: object):
        validator = self._lease_validator_callback()
        validator()
        display_root, filesystem_root = self._lease_roots()
        stable_root = Path(os.path.abspath(os.fspath(filesystem_root)))
        display_root = Path(os.path.abspath(os.fspath(display_root)))
        normalized_segments: list[object] = []
        for segment in pathsegments:
            raw_segment = os.fspath(segment) if isinstance(
                segment,
                (str, os.PathLike),
            ) else segment
            if isinstance(raw_segment, str) and os.path.isabs(raw_segment):
                candidate = Path(os.path.abspath(raw_segment))
                try:
                    candidate.relative_to(stable_root)
                except ValueError:
                    try:
                        relative = candidate.relative_to(display_root)
                    except ValueError as exc:
                        raise ValueError(
                            "attempt promotion path escaped its stable run root"
                        ) from exc
                    candidate = stable_root / relative
                raw_segment = os.fspath(candidate)
            normalized_segments.append(raw_segment)
        return type(self)(
            *normalized_segments,
            validator=validator,
            display_root=display_root,
            filesystem_root=stable_root,
        )

    def glob(
        self,
        pattern: str,
        *,
        case_sensitive: bool | None = None,
        recurse_symlinks: bool = False,
    ):
        self._lease_validator_callback()()
        if recurse_symlinks:
            raise ValueError("attempt promotion traversal cannot follow symlinks")
        stable_root = Path(os.path.abspath(os.fspath(self)))
        if sys.version_info >= (3, 13):
            paths = stable_root.glob(
                pattern,
                case_sensitive=case_sensitive,
                recurse_symlinks=recurse_symlinks,
            )
        elif sys.version_info >= (3, 12):
            paths = stable_root.glob(pattern, case_sensitive=case_sensitive)
        else:
            paths = stable_root.glob(pattern)
        for path in paths:
            self._lease_validator_callback()()
            yield self.with_segments(os.fspath(path))

    def rglob(
        self,
        pattern: str,
        *,
        case_sensitive: bool | None = None,
        recurse_symlinks: bool = False,
    ):
        self._lease_validator_callback()()
        if recurse_symlinks:
            raise ValueError("attempt promotion traversal cannot follow symlinks")
        stable_root = Path(os.path.abspath(os.fspath(self)))
        if sys.version_info >= (3, 13):
            paths = stable_root.rglob(
                pattern,
                case_sensitive=case_sensitive,
                recurse_symlinks=recurse_symlinks,
            )
        elif sys.version_info >= (3, 12):
            paths = stable_root.rglob(pattern, case_sensitive=case_sensitive)
        else:
            paths = stable_root.rglob(pattern)
        for path in paths:
            self._lease_validator_callback()()
            yield self.with_segments(os.fspath(path))

    def iterdir(self):
        self._lease_validator_callback()()
        stable_root = Path(os.path.abspath(os.fspath(self)))
        for path in stable_root.iterdir():
            self._lease_validator_callback()()
            yield self.with_segments(os.fspath(path))

    def walk(
        self,
        top_down: bool = True,
        on_error=None,
        follow_symlinks: bool = False,
    ):
        self._lease_validator_callback()()
        if follow_symlinks:
            raise ValueError("attempt promotion traversal cannot follow symlinks")
        for root, dirnames, filenames in os.walk(
            os.path.abspath(os.fspath(self)),
            topdown=top_down,
            onerror=on_error,
            followlinks=follow_symlinks,
        ):
            self._lease_validator_callback()()
            yield self.with_segments(root), dirnames, filenames

    def _lease_roots(self) -> tuple[Path, Path]:
        display_root = getattr(self, "_lease_display_root", None)
        filesystem_root = getattr(self, "_lease_filesystem_root", None)
        if display_root is not None and filesystem_root is not None:
            return display_root, filesystem_root
        binding = _ACTIVE_PROMOTION_LEASE.get()
        if binding is None:
            raise ValueError("attempt promotion lease path is no longer active")
        return binding.canonical_run_path, binding.filesystem_run_path

    def __fspath__(self) -> str:
        self._lease_validator_callback()()
        return super().__str__()

    def __str__(self) -> str:
        self._lease_validator_callback()()
        raw_path = Path(super().__str__())
        if not raw_path.is_absolute():
            return super().__str__()
        display_root, filesystem_root = self._lease_roots()
        try:
            relative = raw_path.relative_to(filesystem_root)
        except ValueError as exc:
            raise ValueError("attempt promotion path escaped its stable run root") from exc
        return str(display_root / relative)

    def resolve(self, strict: bool = False) -> Path:
        """Return an anchored plain path after rejecting symlink traversal."""

        self._lease_validator_callback()()
        display_root, filesystem_root = self._lease_roots()
        stable_root = Path(os.path.abspath(os.fspath(filesystem_root)))
        display_root = Path(os.path.abspath(os.fspath(display_root)))
        candidate = Path(os.path.abspath(os.fspath(self)))
        try:
            relative = candidate.relative_to(stable_root)
        except ValueError:
            try:
                relative = candidate.relative_to(display_root)
            except ValueError as exc:
                raise ValueError(
                    "attempt promotion path escaped its stable run root"
                ) from exc
        if ".." in relative.parts:
            raise ValueError("attempt promotion path escaped its stable run root")
        current = stable_root
        for component in relative.parts:
            current = current / component
            try:
                _portable_lstat(current)
            except FileNotFoundError:
                if strict:
                    raise
                break
        return stable_root / relative


_ACTIVE_PROMOTION_LEASE: ContextVar[_PromotionLeaseBinding | None] = ContextVar(
    "autodesign_active_promotion_lease",
    default=None,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store_lock(run_dir: Path) -> threading.RLock:
    key = os.path.abspath(os.fspath(run_dir))
    with _STORE_LOCKS_GUARD:
        return _STORE_LOCKS.setdefault(key, threading.RLock())


def _store_lock_for_identity(run_identity: tuple[int, int]) -> threading.RLock:
    key = f"opened:{run_identity[0]}:{run_identity[1]}"
    with _STORE_LOCKS_GUARD:
        return _STORE_LOCKS.setdefault(key, threading.RLock())


def _portable_lstat(path: Path) -> os.stat_result:
    metadata = os.lstat(path)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    is_junction = getattr(path, "is_junction", lambda: False)()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or is_junction
        or bool(file_attributes & reparse_attribute)
    ):
        raise ValueError("attempt selection paths cannot be reparse points")
    return metadata


def promotion_run_identity(run_dir: Path) -> tuple[int, int]:
    """Capture the directory identity ordinary promotion intends to mutate."""

    metadata = _portable_lstat(Path(os.path.abspath(os.fspath(run_dir))))
    _validate_directory(metadata)
    return metadata.st_dev, metadata.st_ino


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _validate_directory(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("attempt selection path must be a directory")


def _validate_regular_file(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("attempt selection control file is unsafe")


def _active_promotion_lease_for(
    run_dir: Path,
) -> _PromotionLeaseBinding | None:
    binding = _ACTIVE_PROMOTION_LEASE.get()
    if binding is None:
        return None
    requested_path = Path(os.path.abspath(os.fspath(run_dir)))
    if requested_path in {
        binding.requested_run_path,
        binding.canonical_run_path,
        binding.filesystem_run_path,
    }:
        binding.assert_active_and_unchanged()
        return binding
    metadata = _portable_lstat(requested_path)
    _validate_directory(metadata)
    if (metadata.st_dev, metadata.st_ino) == binding.run_identity:
        raise ValueError(
            "attempt selection control access requires an exact active lease root"
        )
    return None


def active_promotion_run_path(run_dir: Path) -> Path:
    binding = _active_promotion_lease_for(run_dir)
    if binding is None:
        raise ValueError("attempt promotion lease is not active for this run")
    return _PromotionLeasePath(
        binding.filesystem_run_path,
        validator=binding.assert_active,
        display_root=binding.canonical_run_path,
        filesystem_root=binding.filesystem_run_path,
    )


def assert_promotion_run_unchanged() -> None:
    binding = _ACTIVE_PROMOTION_LEASE.get()
    if binding is None:
        return
    binding.assert_active_and_unchanged()


@dataclass(frozen=True)
class SecureRunMemberSnapshot:
    """Retained bytes or digest for one securely opened run member."""

    relative_path: Path
    sha256: str
    size: int
    data: bytes | None = None
    device: int | None = None
    inode: int | None = None
    mode: int | None = None
    nlink: int | None = None
    mtime_ns: int | None = None
    native_platform: Literal["posix", "windows"] | None = None
    native_stable: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.native_stable is not None:
            object.__setattr__(
                self,
                "native_stable",
                MappingProxyType(dict(self.native_stable)),
            )


@dataclass(frozen=True)
class VideoDeliveryInvalidation:
    """Typed tombstone for one securely retained prior delivery pointer."""

    invalidation_id: str
    reason: str
    prior_pointer_sha256: str
    prior_manifest_sha256: str | None
    prior_design_spec_sha256: str | None
    prior_design_spec_revision: int | None
    prior_render_started_at: str | None
    invalidated_at: str
    status: Literal["invalidated"] = "invalidated"

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "invalidation_id": self.invalidation_id,
            "reason": self.reason,
            "prior_pointer_sha256": self.prior_pointer_sha256,
            "prior_manifest_sha256": self.prior_manifest_sha256,
            "prior_design_spec_sha256": self.prior_design_spec_sha256,
            "prior_design_spec_revision": self.prior_design_spec_revision,
            "prior_render_started_at": self.prior_render_started_at,
            "invalidated_at": self.invalidated_at,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "VideoDeliveryInvalidation":
        if not isinstance(payload, dict) or payload.get("status") != "invalidated":
            raise ValueError("video delivery invalidation tombstone is malformed")
        invalidation_id = payload.get("invalidation_id")
        reason = payload.get("reason")
        pointer_sha256 = payload.get("prior_pointer_sha256")
        invalidated_at = payload.get("invalidated_at")
        if not isinstance(invalidation_id, str) or not invalidation_id:
            raise ValueError("video delivery invalidation ID is missing")
        if not isinstance(reason, str) or not reason:
            raise ValueError("video delivery invalidation reason is missing")
        if not _is_sha256(pointer_sha256):
            raise ValueError("video delivery prior pointer hash is invalid")
        if not isinstance(invalidated_at, str) or not invalidated_at:
            raise ValueError("video delivery invalidation timestamp is missing")
        manifest_sha256 = payload.get("prior_manifest_sha256")
        spec_sha256 = payload.get("prior_design_spec_sha256")
        spec_revision = payload.get("prior_design_spec_revision")
        render_started_at = payload.get("prior_render_started_at")
        if manifest_sha256 is not None and not _is_sha256(manifest_sha256):
            raise ValueError("video delivery prior manifest hash is invalid")
        if spec_sha256 is not None and not _is_sha256(spec_sha256):
            raise ValueError("video delivery prior DesignSpec hash is invalid")
        if spec_revision is not None and (
            type(spec_revision) is not int or spec_revision < 0
        ):
            raise ValueError("video delivery prior DesignSpec revision is invalid")
        if render_started_at is not None and not isinstance(render_started_at, str):
            raise ValueError("video delivery prior render identity is invalid")
        return cls(
            invalidation_id=invalidation_id,
            reason=reason,
            prior_pointer_sha256=pointer_sha256,
            prior_manifest_sha256=manifest_sha256,
            prior_design_spec_sha256=spec_sha256,
            prior_design_spec_revision=spec_revision,
            prior_render_started_at=render_started_at,
            invalidated_at=invalidated_at,
        )


@dataclass(frozen=True)
class VideoDeliveryPointerUpdate:
    status: Literal["published", "invalidated", "already_invalidated", "absent"]
    pointer_snapshot: SecureRunMemberSnapshot | None
    payload: dict[str, Any] | None
    cleanup_warnings: tuple[str, ...] = ()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _video_delivery_pointer_adapter_hook(
    event: str,
    **_details: Any,
) -> None:
    """Deterministic test seam after secure present/absent classification."""


def _run_member_is_reparse(metadata: os.stat_result) -> bool:
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_attribute
    )


def _validate_run_member_directory(
    metadata: os.stat_result,
    *,
    label: str,
) -> None:
    if _run_member_is_reparse(metadata):
        raise ValueError(
            f"{label} path contains a link or reparse point; "
            "no-follow run members are required"
        )
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} path component is not a directory")


def _validate_run_member_file(
    metadata: os.stat_result,
    *,
    label: str,
) -> None:
    if _run_member_is_reparse(metadata):
        raise ValueError(
            f"{label} path contains a link or reparse point; "
            "no-follow run members are required"
        )
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError(
            f"{label} is an unsafe hard-linked or non-regular run member"
        )


def _run_member_file_unchanged(
    before: os.stat_result,
    after: os.stat_result,
) -> bool:
    return (
        _same_identity(before, after)
        and before.st_size == after.st_size
        and getattr(before, "st_mtime_ns", None)
        == getattr(after, "st_mtime_ns", None)
        and getattr(before, "st_ctime_ns", None)
        == getattr(after, "st_ctime_ns", None)
        and before.st_nlink == after.st_nlink
    )


def _raise_run_member_open_error(label: str, exc: OSError) -> None:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        raise ValueError(
            f"{label} path contains a link or reparse point; "
            "no-follow run members are required"
        ) from exc
    raise exc


def _validate_secure_run_member_root_identity(
    accessor: "SecureRunMemberAccessor",
) -> None:
    if accessor._binding is not None:
        accessor._binding.assert_open_and_unchanged()
    if isinstance(accessor._root_reference, int):
        metadata = os.fstat(accessor._root_reference)
    else:
        metadata = _portable_lstat(accessor._root_reference)
    _validate_run_member_directory(metadata, label="run root")
    if (metadata.st_dev, metadata.st_ino) != accessor._root_identity:
        raise ValueError("secure run root changed during member access")
    if accessor._binding is None:
        for path in dict.fromkeys(
            (accessor._requested_root, accessor._canonical_root)
        ):
            current = _portable_lstat(path)
            _validate_run_member_directory(current, label="run root")
            if (current.st_dev, current.st_ino) != accessor._root_identity:
                raise ValueError("secure run root changed during member access")



class SecureRunMemberAccessor:
    """Read, hash, and publish members beneath one retained run root."""

    def __init__(
        self,
        *,
        root_reference: int | Path,
        root_identity: tuple[int, int],
        accepted_roots: Sequence[Path],
        requested_root: Path,
        canonical_root: Path,
        binding: _PromotionLeaseBinding | None,
    ) -> None:
        self._root_reference = root_reference
        self._root_identity = root_identity
        self._accepted_roots = tuple(dict.fromkeys(accepted_roots))
        self._requested_root = requested_root
        self._canonical_root = canonical_root
        self._binding = binding
        self._pending_publications: list[VideoPointerPublication] = []
        self.cleanup_warnings: list[str] = []
        self._durable_publication_committed = False

    def _rollback_publications(self) -> BaseException | None:
        rollback_errors: list[str] = []
        for publication in reversed(self._pending_publications):
            if publication.durable_committed:
                self._durable_publication_committed = True
                self.cleanup_warnings.append(
                    "durable video pointer publication was not rolled back"
                )
                try:
                    publication.close()
                except BaseException as exc:
                    self.cleanup_warnings.append(
                        f"post-commit publication close failed: {exc}"
                    )
                continue
            try:
                publication.abort()
            except BaseException as exc:
                rollback_errors.append(f"rollback failed: {exc}")
            try:
                publication.close()
            except BaseException as exc:
                rollback_errors.append(f"rollback close failed: {exc}")
        self._pending_publications.clear()
        if rollback_errors:
            return RuntimeError("; ".join(rollback_errors))
        return None

    def _commit_publications(self) -> None:
        for publication in self._pending_publications:
            try:
                publication.assert_precondition(checkpoint="precommit")
                publication.commit()
            except BaseException as exc:
                if not publication.durable_committed:
                    raise
                self._durable_publication_committed = True
                self.cleanup_warnings.append(
                    f"post-commit publication diagnostic failed: {exc}"
                )
            else:
                self._durable_publication_committed = True
            try:
                publication.close()
            except BaseException as exc:
                self.cleanup_warnings.append(
                    f"post-commit publication close failed: {exc}"
                )
            self.cleanup_warnings.extend(publication.cleanup_warnings)
        self._pending_publications.clear()

    def _record_postcommit_cleanup_warning(self, exc: BaseException) -> None:
        self.cleanup_warnings.append(str(exc))

    def _assert_root_unchanged(self) -> None:
        _validate_secure_run_member_root_identity(self)

    @staticmethod
    def _reject_unsafe_raw_path(raw_value: str, *, label: str) -> None:
        if not raw_value.strip() or "\x00" in raw_value:
            raise ValueError(f"{label} path is invalid")
        posix_parts = PurePosixPath(raw_value).parts
        windows_parts = PureWindowsPath(raw_value).parts
        if ".." in posix_parts or ".." in windows_parts:
            raise ValueError(f"{label} path contains traversal")

    def member_relative_path(
        self,
        value: str | os.PathLike[str],
        *,
        label: str,
        base: Path | None = None,
    ) -> Path:
        raw_value = _RUNTIME_OS.fspath(value)
        if not isinstance(raw_value, str):
            raise ValueError(f"{label} path is invalid")
        self._reject_unsafe_raw_path(raw_value, label=label)
        native_path = Path(raw_value)
        windows_path = PureWindowsPath(raw_value)
        has_windows_anchor = bool(
            windows_path.drive or windows_path.root or windows_path.anchor
        )
        if native_path.is_absolute():
            if base is not None:
                raise ValueError(f"{label} must be relative")
            candidate = Path(_RUNTIME_OS.path.abspath(raw_value))
            matches: list[tuple[int, Path]] = []
            for root in self._accepted_roots:
                try:
                    relative = candidate.relative_to(root)
                except ValueError:
                    continue
                matches.append((len(root.parts), relative))
            if not matches:
                raise ValueError(
                    f"{label} is outside the current run's exact secure roots"
                )
            relative = max(matches, key=lambda item: item[0])[1]
        else:
            if has_windows_anchor:
                raise ValueError(f"{label} contains a Windows path anchor")
            if PurePosixPath(raw_value).is_absolute():
                raise ValueError(f"{label} is not a valid native path")
            relative = native_path if base is None else base / native_path
        if relative == Path(".") or relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"{label} must stay inside the secure run root")
        return relative

    def _open_fd_directories(
        self,
        parts: Sequence[str],
        *,
        label: str,
        create: bool = False,
    ) -> tuple[list[tuple[int, str, int, os.stat_result]], int]:
        assert isinstance(self._root_reference, int)
        directory_flags = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC
        opened: list[tuple[int, str, int, os.stat_result]] = []
        current_fd = self._root_reference
        try:
            for component in parts:
                try:
                    entry = os.stat(
                        component,
                        dir_fd=current_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, 0o755, dir_fd=current_fd)
                    os.fsync(current_fd)
                    entry = os.stat(
                        component,
                        dir_fd=current_fd,
                        follow_symlinks=False,
                    )
                _validate_run_member_directory(entry, label=label)
                try:
                    next_fd = os.open(
                        component,
                        directory_flags,
                        dir_fd=current_fd,
                    )
                except OSError as exc:
                    _raise_run_member_open_error(label, exc)
                opened_entry = os.fstat(next_fd)
                _validate_run_member_directory(opened_entry, label=label)
                if not _same_identity(entry, opened_entry):
                    os.close(next_fd)
                    raise ValueError(
                        f"{label} directory changed during no-follow open"
                    )
                opened.append((current_fd, component, next_fd, opened_entry))
                current_fd = next_fd
            return opened, current_fd
        except BaseException:
            for _, _, descriptor, _ in reversed(opened):
                os.close(descriptor)
            raise

    @staticmethod
    def _close_fd_directories(
        opened: Sequence[tuple[int, str, int, os.stat_result]],
    ) -> None:
        for _, _, descriptor, _ in reversed(opened):
            os.close(descriptor)

    @staticmethod
    def _verify_fd_directories(
        opened: Sequence[tuple[int, str, int, os.stat_result]],
        *,
        label: str,
    ) -> None:
        for parent_fd, component, descriptor, before in reversed(opened):
            opened_now = os.fstat(descriptor)
            current = os.stat(
                component,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            _validate_run_member_directory(opened_now, label=label)
            _validate_run_member_directory(current, label=label)
            if not _same_identity(before, opened_now) or not _same_identity(
                before,
                current,
            ):
                raise ValueError(f"{label} directory changed during access")

    def _read_from_fd(
        self,
        relative: Path,
        *,
        label: str,
        capture: bool,
        max_bytes: int | None = None,
    ) -> SecureRunMemberSnapshot:
        assert isinstance(self._root_reference, int)
        self._assert_root_unchanged()
        opened, parent_fd = self._open_fd_directories(
            relative.parts[:-1],
            label=label,
        )
        member_fd: int | None = None
        try:
            entry = os.stat(
                relative.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            _validate_run_member_file(entry, label=label)
            file_flags = (
                os.O_RDONLY
                | _NOFOLLOW
                | _CLOEXEC
                | getattr(os, "O_NONBLOCK", 0)
            )
            try:
                member_fd = os.open(
                    relative.name,
                    file_flags,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                _raise_run_member_open_error(label, exc)
            opened_file = os.fstat(member_fd)
            _validate_run_member_file(opened_file, label=label)
            if not _same_identity(entry, opened_file):
                raise ValueError(f"{label} changed during no-follow open")
            digest = hashlib.sha256()
            chunks: list[bytes] | None = [] if capture else None
            total = 0
            while True:
                read_size = 1 << 20
                if max_bytes is not None:
                    read_size = min(read_size, max_bytes - total + 1)
                chunk = os.read(member_fd, read_size)
                if not chunk:
                    break
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise ValueError(f"{label} exceeds its byte limit")
                digest.update(chunk)
                if chunks is not None:
                    chunks.append(chunk)
            after_file = os.fstat(member_fd)
            current = os.stat(
                relative.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            _validate_run_member_file(after_file, label=label)
            _validate_run_member_file(current, label=label)
            if (
                not _run_member_file_unchanged(opened_file, after_file)
                or not _run_member_file_unchanged(after_file, current)
                or total != after_file.st_size
            ):
                raise ValueError(f"{label} changed during retained read")
            self._verify_fd_directories(opened, label=label)
            self._assert_root_unchanged()
            return SecureRunMemberSnapshot(
                relative_path=relative,
                sha256=digest.hexdigest(),
                size=total,
                data=b"".join(chunks) if chunks is not None else None,
                device=int(after_file.st_dev),
                inode=int(after_file.st_ino),
                mode=int(after_file.st_mode),
                nlink=int(after_file.st_nlink),
                mtime_ns=int(after_file.st_mtime_ns),
            )
        finally:
            if member_fd is not None:
                os.close(member_fd)
            self._close_fd_directories(opened)

    def _open_path_directories(
        self,
        parts: Sequence[str],
        *,
        label: str,
        stack: ExitStack,
        create: bool = False,
    ) -> tuple[list[tuple[Path, os.stat_result]], Path]:
        assert isinstance(self._root_reference, Path)
        current = self._root_reference
        opened: list[tuple[Path, os.stat_result]] = []
        for component in parts:
            current = current / component
            try:
                entry = _portable_lstat(current)
            except FileNotFoundError:
                if not create:
                    raise
                current.mkdir(mode=0o755)
                entry = _portable_lstat(current)
            _validate_run_member_directory(entry, label=label)
            stack.enter_context(_windows_directory_replacement_guard(current))
            opened_entry = _portable_lstat(current)
            _validate_run_member_directory(opened_entry, label=label)
            if not _same_identity(entry, opened_entry):
                raise ValueError(f"{label} directory changed during guarded open")
            opened.append((current, opened_entry))
        return opened, current

    @staticmethod
    def _verify_path_directories(
        opened: Sequence[tuple[Path, os.stat_result]],
        *,
        label: str,
    ) -> None:
        for path, before in reversed(opened):
            current = _portable_lstat(path)
            _validate_run_member_directory(current, label=label)
            if not _same_identity(before, current):
                raise ValueError(f"{label} directory changed during access")

    def _read_from_path(
        self,
        relative: Path,
        *,
        label: str,
        capture: bool,
        max_bytes: int | None = None,
    ) -> SecureRunMemberSnapshot:
        assert isinstance(self._root_reference, Path)
        self._assert_root_unchanged()
        with ExitStack() as stack:
            opened, parent = self._open_path_directories(
                relative.parts[:-1],
                label=label,
                stack=stack,
            )
            member = parent / relative.name
            entry = _portable_lstat(member)
            _validate_run_member_file(entry, label=label)
            digest = hashlib.sha256()
            chunks: list[bytes] | None = [] if capture else None
            total = 0
            with member.open("rb") as handle:
                opened_file = os.fstat(handle.fileno())
                _validate_run_member_file(opened_file, label=label)
                if not _same_identity(entry, opened_file):
                    raise ValueError(f"{label} changed during guarded open")
                while True:
                    read_size = 1 << 20
                    if max_bytes is not None:
                        read_size = min(read_size, max_bytes - total + 1)
                    chunk = handle.read(read_size)
                    if not chunk:
                        break
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise ValueError(f"{label} exceeds its byte limit")
                    digest.update(chunk)
                    if chunks is not None:
                        chunks.append(chunk)
                after_file = os.fstat(handle.fileno())
            current = _portable_lstat(member)
            _validate_run_member_file(after_file, label=label)
            _validate_run_member_file(current, label=label)
            if (
                not _run_member_file_unchanged(opened_file, after_file)
                or not _run_member_file_unchanged(after_file, current)
                or total != after_file.st_size
            ):
                raise ValueError(f"{label} changed during retained read")
            self._verify_path_directories(opened, label=label)
            self._assert_root_unchanged()
            return SecureRunMemberSnapshot(
                relative_path=relative,
                sha256=digest.hexdigest(),
                size=total,
                data=b"".join(chunks) if chunks is not None else None,
                device=int(after_file.st_dev),
                inode=int(after_file.st_ino),
                mode=int(after_file.st_mode),
                nlink=int(after_file.st_nlink),
                mtime_ns=int(after_file.st_mtime_ns),
            )

    def read_bytes(
        self,
        value: str | os.PathLike[str],
        *,
        label: str,
        base: Path | None = None,
        max_bytes: int | None = None,
    ) -> SecureRunMemberSnapshot:
        if max_bytes is not None and (
            type(max_bytes) is not int or max_bytes < 0
        ):
            raise ValueError("secure run member max_bytes must be non-negative")
        relative = self.member_relative_path(value, label=label, base=base)
        if isinstance(self._root_reference, int):
            return self._read_from_fd(
                relative,
                label=label,
                capture=True,
                max_bytes=max_bytes,
            )
        return self._read_from_path(
            relative,
            label=label,
            capture=True,
            max_bytes=max_bytes,
        )

    def digest(
        self,
        value: str | os.PathLike[str],
        *,
        label: str,
        base: Path | None = None,
    ) -> SecureRunMemberSnapshot:
        relative = self.member_relative_path(value, label=label, base=base)
        if isinstance(self._root_reference, int):
            return self._read_from_fd(relative, label=label, capture=False)
        return self._read_from_path(relative, label=label, capture=False)

    def assert_snapshots_unchanged(
        self,
        snapshots: Sequence[SecureRunMemberSnapshot],
    ) -> None:
        """Reopen and fully rehash one deduplicated retained snapshot graph."""

        expected_by_path: dict[Path, SecureRunMemberSnapshot] = {}
        for snapshot in snapshots:
            if not isinstance(snapshot, SecureRunMemberSnapshot):
                raise ValueError("secure run snapshot graph contains an invalid entry")
            relative = self.member_relative_path(
                snapshot.relative_path,
                label="secure run snapshot",
            )
            if relative != snapshot.relative_path:
                raise ValueError(
                    f"{snapshot.relative_path} snapshot path is not normalized"
                )
            existing = expected_by_path.get(relative)
            if existing is not None:
                if not self._video_pointer_snapshots_match(existing, snapshot):
                    raise ValueError(
                        f"{relative.as_posix()} has inconsistent validated snapshots"
                    )
                continue
            expected_by_path[relative] = snapshot

        for relative, expected in expected_by_path.items():
            label = f"validated snapshot {relative.as_posix()}"
            try:
                current = (
                    self.read_bytes(relative, label=label)
                    if expected.data is not None
                    else self.digest(relative, label=label)
                )
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError(
                    f"{relative.as_posix()} snapshot could not be revalidated: {exc}"
                ) from exc
            if not self._video_pointer_snapshots_match(expected, current):
                raise ValueError(
                    f"{relative.as_posix()} snapshot changed before publication"
                )

    def validate_directory(
        self,
        value: str | os.PathLike[str],
        *,
        label: str,
        base: Path | None = None,
    ) -> Path:
        relative = self.member_relative_path(value, label=label, base=base)
        self._assert_root_unchanged()
        if isinstance(self._root_reference, int):
            opened, _ = self._open_fd_directories(
                relative.parts,
                label=label,
            )
            try:
                self._verify_fd_directories(opened, label=label)
                self._assert_root_unchanged()
            finally:
                self._close_fd_directories(opened)
            return relative
        with ExitStack() as stack:
            opened, _ = self._open_path_directories(
                relative.parts,
                label=label,
                stack=stack,
            )
            self._verify_path_directories(opened, label=label)
            self._assert_root_unchanged()
        return relative

    def _open_video_pointer_parent(
        self,
        relative: Path,
        *,
        label: str,
    ) -> RetainedParent:
        self._assert_root_unchanged()
        if isinstance(self._root_reference, int):
            opened, parent_fd = self._open_fd_directories(
                relative.parts[:-1],
                label=label,
                create=True,
            )

            def validate_parent() -> None:
                self._verify_fd_directories(opened, label=label)
                _validate_secure_run_member_root_identity(self)

            return RetainedParent(
                native_parent=parent_fd,
                platform="posix",
                validate_callback=validate_parent,
                close_callback=lambda: self._close_fd_directories(opened),
            )

        windows_user_sid = _windows_current_user_sid()
        if (
            not isinstance(windows_user_sid, str)
            or not windows_user_sid.strip()
        ):
            raise ValueError(
                "Windows pointer publication requires a current-user SID"
            )
        stack = ExitStack()
        stack.__enter__()
        try:
            opened, parent_path = self._open_path_directories(
                relative.parts[:-1],
                label=label,
                stack=stack,
                create=True,
            )
        except BaseException:
            stack.close()
            raise

        def validate_parent() -> None:
            self._verify_path_directories(opened, label=label)
            _validate_secure_run_member_root_identity(self)

        return RetainedParent(
            native_parent=parent_path,
            platform="windows",
            validate_callback=validate_parent,
            close_callback=stack.close,
            windows_user_sid=windows_user_sid,
        )

    @staticmethod
    def _video_pointer_snapshots_match(
        left: SecureRunMemberSnapshot | None,
        right: SecureRunMemberSnapshot | None,
    ) -> bool:
        if left is None or right is None:
            return left is right
        return (
            left.relative_path == right.relative_path
            and left.sha256 == right.sha256
            and left.size == right.size
            and left.device == right.device
            and left.inode == right.inode
            and left.mode == right.mode
            and left.nlink == right.nlink
            and left.mtime_ns == right.mtime_ns
            and left.data == right.data
            and left.native_platform == right.native_platform
            and left.native_stable == right.native_stable
        )

    def _read_video_pointer_from_parent(
        self,
        retained_parent: RetainedParent,
        relative: Path,
        *,
        label: str,
    ) -> SecureRunMemberSnapshot | None:
        observation = observe_video_delivery_pointer(retained_parent)
        if observation is None:
            return None
        stable = observation.stable
        is_posix = observation.platform == "posix"
        return SecureRunMemberSnapshot(
            relative_path=relative,
            sha256=str(stable["sha256"]),
            size=int(stable["size"]),
            data=observation.data,
            device=int(stable["dev"]) if is_posix else None,
            inode=int(stable["ino"]) if is_posix else None,
            mode=int(stable["mode"]) if is_posix else None,
            nlink=int(stable["nlink"]),
            mtime_ns=int(stable["mtime_ns"]) if is_posix else None,
            native_platform=observation.platform,
            native_stable=stable,
        )

    @staticmethod
    def _video_pointer_expected_prior(
        prior: SecureRunMemberSnapshot | None,
        *,
        platform: Literal["posix", "windows"],
    ) -> dict[str, Any] | None:
        if prior is None:
            return None
        if (
            prior.native_platform != platform
            or prior.native_stable is None
        ):
            raise ValueError("classified video delivery pointer identity is incomplete")
        return {
            "platform": prior.native_platform,
            "stable": dict(prior.native_stable),
        }

    def stage_video_delivery_pointer(
        self,
        value: str | os.PathLike[str],
        payload: Any = _VIDEO_POINTER_PAYLOAD_UNSET,
        *,
        label: str,
        payload_factory: Callable[
            [SecureRunMemberSnapshot | None], Any | None
        ] | None = None,
        invoke_adapter_hook: bool = False,
        precondition: Callable[[], None] | None = None,
    ) -> SecureRunMemberSnapshot | None:
        """Stage the single crash-safe final/video_delivery.json publication."""

        if self._pending_publications:
            raise RuntimeError("only one video pointer publication is allowed per accessor")
        if (payload is _VIDEO_POINTER_PAYLOAD_UNSET) == (payload_factory is None):
            raise ValueError("video pointer publication requires one payload source")
        relative = self.member_relative_path(value, label=label)
        if relative != Path("final/video_delivery.json"):
            raise ValueError("video pointer publication target must be final/video_delivery.json")
        retained_parent = self._open_video_pointer_parent(relative, label=label)
        publication: VideoPointerPublication | None = None
        try:
            recovery_warnings = recover_video_delivery_pointer(retained_parent)
            self.cleanup_warnings.extend(recovery_warnings)
            classified = self._read_video_pointer_from_parent(
                retained_parent,
                relative,
                label=label,
            )
            if invoke_adapter_hook:
                _video_delivery_pointer_adapter_hook(
                    "classified_present" if classified is not None else "classified_absent",
                    run_dir=self._canonical_root,
                    relative_path=relative,
                    snapshot=classified,
                )
                observed = self._read_video_pointer_from_parent(
                    retained_parent,
                    relative,
                    label=label,
                )
                if not self._video_pointer_snapshots_match(classified, observed):
                    raise ValueError(
                        "video delivery pointer changed after secure classification"
                    )
            selected_payload = (
                payload_factory(classified)
                if payload_factory is not None
                else payload
            )
            if selected_payload is None:
                retained_parent.close()
                return None
            encoded = json.dumps(
                selected_payload,
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            publication_kwargs: dict[str, Any] = {
                "expected_prior": self._video_pointer_expected_prior(
                    classified,
                    platform=retained_parent.platform,
                ),
                "recovery_warnings": recovery_warnings,
            }
            if precondition is not None:
                publication_kwargs["precondition"] = precondition
            publication = stage_video_delivery_pointer(
                retained_parent,
                encoded,
                **publication_kwargs,
            )
            self._pending_publications.append(publication)
        except BaseException:
            if publication is None:
                retained_parent.close()
            raise
        return SecureRunMemberSnapshot(
            relative_path=relative,
            sha256=hashlib.sha256(encoded).hexdigest(),
            size=len(encoded),
            data=None,
        )

def _exact_active_run_member_binding(
    run_dir: Path,
) -> _PromotionLeaseBinding | None:
    binding = _ACTIVE_PROMOTION_LEASE.get()
    if binding is None:
        return None
    binding.assert_active_and_unchanged()
    requested = Path(_RUNTIME_OS.path.abspath(_RUNTIME_OS.fspath(run_dir)))
    if requested not in {
        binding.requested_run_path,
        binding.canonical_run_path,
        binding.filesystem_run_path,
    }:
        raise ValueError(
            "secure run-member access requires an exact active lease root"
        )
    return binding


@contextmanager
def _secure_run_member_accessor_lifecycle(
    accessor: SecureRunMemberAccessor,
) -> Iterator[SecureRunMemberAccessor]:
    try:
        accessor._assert_root_unchanged()
        yield accessor
        accessor._assert_root_unchanged()
        accessor._commit_publications()
    except BaseException as original_error:
        if accessor._durable_publication_committed:
            accessor._record_postcommit_cleanup_warning(original_error)
            return
        rollback_error = accessor._rollback_publications()
        if rollback_error is not None:
            raise ValueError(
                f"{original_error}; secure publication rollback failed: "
                f"{rollback_error}"
            ) from original_error
        raise


def _complete_secure_run_member_cleanup(
    *,
    accessor: SecureRunMemberAccessor | None,
    original_error: BaseException | None,
    cleanup_error: BaseException | None,
    label: str,
) -> None:
    if cleanup_error is not None:
        if accessor is not None and accessor._durable_publication_committed:
            accessor._record_postcommit_cleanup_warning(
                OSError(f"post-commit {label} close failed: {cleanup_error}")
            )
        elif original_error is not None:
            raise ValueError(
                f"{original_error}; {label} close failed: {cleanup_error}"
            ) from original_error
        else:
            raise cleanup_error
    if original_error is not None:
        raise original_error


@contextmanager
def secure_run_member_access(
    run_dir: Path,
) -> Iterator[SecureRunMemberAccessor]:
    """Retain one exact run root for no-follow member reads and publication."""

    binding = _exact_active_run_member_binding(run_dir)
    if binding is not None:
        accepted_roots = (
            binding.requested_run_path,
            binding.canonical_run_path,
            binding.filesystem_run_path,
        )
        if isinstance(binding.run_reference, int):
            if (
                _RUNTIME_OS.name != "posix"
                or not _SECURE_DIR_FD_AVAILABLE
                or not _NOFOLLOW
                or not _DIRECTORY
            ):
                raise RuntimeError("secure run-member primitive is unavailable")
            run_reference = os.dup(binding.run_reference)
            accessor: SecureRunMemberAccessor | None = None
            original_error: BaseException | None = None
            try:
                metadata = os.fstat(run_reference)
                _validate_run_member_directory(metadata, label="run root")
                if (metadata.st_dev, metadata.st_ino) != binding.run_identity:
                    raise ValueError("secure active run root identity mismatch")
                accessor = SecureRunMemberAccessor(
                    root_reference=run_reference,
                    root_identity=binding.run_identity,
                    accepted_roots=accepted_roots,
                    requested_root=binding.requested_run_path,
                    canonical_root=binding.canonical_run_path,
                    binding=binding,
                )
                with _secure_run_member_accessor_lifecycle(accessor) as retained:
                    yield retained
            except BaseException as exc:
                original_error = exc
            cleanup_error: BaseException | None = None
            try:
                os.close(run_reference)
            except BaseException as exc:
                cleanup_error = exc
            _complete_secure_run_member_cleanup(
                accessor=accessor,
                original_error=original_error,
                cleanup_error=cleanup_error,
                label="retained run descriptor",
            )
            return
        if _RUNTIME_OS.name != "nt":
            raise RuntimeError("secure run-member primitive is unavailable")
        guard = _windows_directory_replacement_guard(binding.canonical_run_path)
        guard.__enter__()
        accessor = None
        original_error = None
        try:
            accessor = SecureRunMemberAccessor(
                root_reference=binding.canonical_run_path,
                root_identity=binding.run_identity,
                accepted_roots=accepted_roots,
                requested_root=binding.requested_run_path,
                canonical_root=binding.canonical_run_path,
                binding=binding,
            )
            with _secure_run_member_accessor_lifecycle(accessor) as retained:
                yield retained
        except BaseException as exc:
            original_error = exc
        cleanup_error = None
        try:
            guard.__exit__(None, None, None)
        except BaseException as exc:
            cleanup_error = exc
        _complete_secure_run_member_cleanup(
            accessor=accessor,
            original_error=original_error,
            cleanup_error=cleanup_error,
            label="Windows run guard",
        )
        return

    requested_root = Path(
        _RUNTIME_OS.path.abspath(_RUNTIME_OS.fspath(run_dir))
    )
    requested_metadata = _portable_lstat(requested_root)
    _validate_run_member_directory(requested_metadata, label="run root")
    canonical_root = Path(
        _RUNTIME_OS.path.realpath(requested_root, strict=True)
    )
    canonical_metadata = _portable_lstat(canonical_root)
    _validate_run_member_directory(canonical_metadata, label="run root")
    if not _same_identity(requested_metadata, canonical_metadata):
        raise ValueError("secure run root changed during canonicalization")
    run_identity = (canonical_metadata.st_dev, canonical_metadata.st_ino)
    accepted_roots = (requested_root, canonical_root)
    if _RUNTIME_OS.name == "posix":
        if not _SECURE_DIR_FD_AVAILABLE or not _NOFOLLOW or not _DIRECTORY:
            raise RuntimeError("secure run-member primitive is unavailable")
        run_reference = os.open(
            canonical_root,
            os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
        )
        accessor = None
        original_error = None
        try:
            opened = os.fstat(run_reference)
            _validate_run_member_directory(opened, label="run root")
            if not _same_identity(canonical_metadata, opened):
                raise ValueError("secure run root changed during open")
            accessor = SecureRunMemberAccessor(
                root_reference=run_reference,
                root_identity=run_identity,
                accepted_roots=accepted_roots,
                requested_root=requested_root,
                canonical_root=canonical_root,
                binding=None,
            )
            with _secure_run_member_accessor_lifecycle(accessor) as retained:
                yield retained
        except BaseException as exc:
            original_error = exc
        cleanup_error = None
        try:
            os.close(run_reference)
        except BaseException as exc:
            cleanup_error = exc
        _complete_secure_run_member_cleanup(
            accessor=accessor,
            original_error=original_error,
            cleanup_error=cleanup_error,
            label="retained run descriptor",
        )
        return
    if _RUNTIME_OS.name == "nt":
        guard = _windows_directory_replacement_guard(canonical_root)
        guard.__enter__()
        accessor = None
        original_error = None
        try:
            accessor = SecureRunMemberAccessor(
                root_reference=canonical_root,
                root_identity=run_identity,
                accepted_roots=accepted_roots,
                requested_root=requested_root,
                canonical_root=canonical_root,
                binding=None,
            )
            with _secure_run_member_accessor_lifecycle(accessor) as retained:
                yield retained
        except BaseException as exc:
            original_error = exc
        cleanup_error = None
        try:
            guard.__exit__(None, None, None)
        except BaseException as exc:
            cleanup_error = exc
        _complete_secure_run_member_cleanup(
            accessor=accessor,
            original_error=original_error,
            cleanup_error=cleanup_error,
            label="Windows run guard",
        )
        return
    raise RuntimeError("secure run-member primitive is unavailable")


def update_video_delivery_pointer(
    run_dir: Path,
    *,
    mode: Literal["publish", "invalidate_if_present"],
    payload: dict[str, Any] | None = None,
    reason: str | None = None,
    prior_delivery: dict[str, Any] | None = None,
    expected_snapshots: Sequence[SecureRunMemberSnapshot] | None = None,
) -> VideoDeliveryPointerUpdate:
    """Publish or invalidate the delivery pointer through one secure transaction."""

    if mode == "publish":
        if not isinstance(payload, dict) or reason is not None:
            raise ValueError("video pointer publish requires one exact payload")
        frozen_payload = dict(payload)
        frozen_snapshots = (
            tuple(expected_snapshots)
            if expected_snapshots is not None
            else None
        )
        with secure_run_member_access(run_dir) as accessor:
            pointer_snapshot = accessor.stage_video_delivery_pointer(
                Path("final/video_delivery.json"),
                frozen_payload,
                label="final Video delivery pointer",
                invoke_adapter_hook=True,
                precondition=(
                    lambda: accessor.assert_snapshots_unchanged(
                        frozen_snapshots
                    )
                    if frozen_snapshots is not None
                    else None
                ),
            )
        assert pointer_snapshot is not None
        return VideoDeliveryPointerUpdate(
            status="published",
            pointer_snapshot=pointer_snapshot,
            payload=frozen_payload,
            cleanup_warnings=tuple(accessor.cleanup_warnings),
        )

    if mode != "invalidate_if_present" or payload is not None:
        raise ValueError("unsupported video pointer adapter mode")
    invalidation_reason = str(reason or "video_delivery_replaced").strip()
    if not invalidation_reason:
        raise ValueError("video pointer invalidation reason is required")
    selected_status: Literal["invalidated", "already_invalidated", "absent"] = (
        "absent"
    )
    selected_payload: dict[str, Any] | None = None

    with secure_run_member_access(run_dir) as accessor:
        def invalidation_payload_factory(
            current: SecureRunMemberSnapshot | None,
        ) -> dict[str, Any] | None:
            nonlocal selected_status, selected_payload
            if current is None:
                selected_status = "absent"
                return None
            if current.data is None:
                raise ValueError("current video delivery pointer bytes were not retained")
            try:
                pointer_payload = json.loads(current.data)
            except (UnicodeDecodeError, json.JSONDecodeError):
                pointer_payload = {}
            if isinstance(pointer_payload, dict) and (
                pointer_payload.get("status") == "invalidated"
            ):
                tombstone = VideoDeliveryInvalidation.from_payload(pointer_payload)
                selected_status = "already_invalidated"
                selected_payload = tombstone.to_payload()
                return None

            prior_manifest_sha256: str | None = None
            prior_spec_sha256: str | None = None
            prior_spec_revision: int | None = None
            prior_render_started_at: str | None = None
            if isinstance(pointer_payload, dict):
                manifest_sha256 = pointer_payload.get("manifest_sha256")
                if _is_sha256(manifest_sha256):
                    prior_manifest_sha256 = manifest_sha256
                spec_sha256 = pointer_payload.get("design_spec_sha256")
                if _is_sha256(spec_sha256):
                    prior_spec_sha256 = spec_sha256
                spec_revision = pointer_payload.get("design_spec_revision")
                if type(spec_revision) is int and spec_revision >= 0:
                    prior_spec_revision = spec_revision
                manifest_value = pointer_payload.get("manifest_path")
                if isinstance(manifest_value, str) and manifest_value:
                    manifest_snapshot = accessor.read_bytes(
                        manifest_value,
                        label="prior Video delivery manifest",
                    )
                    if (
                        prior_manifest_sha256 is not None
                        and manifest_snapshot.sha256 != prior_manifest_sha256
                    ):
                        raise ValueError(
                            "prior Video delivery manifest changed before invalidation"
                        )
                    prior_manifest_sha256 = manifest_snapshot.sha256
                    if manifest_snapshot.data is not None:
                        try:
                            manifest_payload = json.loads(manifest_snapshot.data)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            manifest_payload = None
                        if isinstance(manifest_payload, dict):
                            render_identity = manifest_payload.get("render_started_at")
                            if isinstance(render_identity, str) and render_identity:
                                prior_render_started_at = render_identity

            if isinstance(prior_delivery, dict):
                manifest_sha256 = prior_delivery.get("delivery_manifest_sha256")
                if _is_sha256(manifest_sha256):
                    if (
                        prior_manifest_sha256 is not None
                        and prior_manifest_sha256 != manifest_sha256
                    ):
                        raise ValueError(
                            "in-memory Video delivery does not match the retained pointer"
                        )
                    prior_manifest_sha256 = manifest_sha256
                spec_sha256 = prior_delivery.get("design_spec_sha256")
                if _is_sha256(spec_sha256):
                    prior_spec_sha256 = spec_sha256
                spec_revision = prior_delivery.get("design_spec_revision")
                if type(spec_revision) is int and spec_revision >= 0:
                    prior_spec_revision = spec_revision
                render_identity = prior_delivery.get("render_started_at")
                if isinstance(render_identity, str) and render_identity:
                    prior_render_started_at = render_identity

            tombstone = VideoDeliveryInvalidation(
                invalidation_id=uuid.uuid4().hex,
                reason=invalidation_reason,
                prior_pointer_sha256=current.sha256,
                prior_manifest_sha256=prior_manifest_sha256,
                prior_design_spec_sha256=prior_spec_sha256,
                prior_design_spec_revision=prior_spec_revision,
                prior_render_started_at=prior_render_started_at,
                invalidated_at=datetime.now(timezone.utc).isoformat(),
            )
            selected_status = "invalidated"
            selected_payload = tombstone.to_payload()
            VideoDeliveryInvalidation.from_payload(selected_payload)
            return selected_payload

        pointer_snapshot = accessor.stage_video_delivery_pointer(
            Path("final/video_delivery.json"),
            label="final Video delivery pointer",
            payload_factory=invalidation_payload_factory,
            invoke_adapter_hook=True,
        )
    return VideoDeliveryPointerUpdate(
        status=selected_status,
        pointer_snapshot=pointer_snapshot,
        payload=selected_payload,
        cleanup_warnings=tuple(accessor.cleanup_warnings),
    )


def _browser_relative_path(value: str, *, label: str) -> PurePosixPath:
    if not value or "\\" in value or "\x00" in value:
        raise ValueError(f"{label} is invalid")
    parts = value.split("/")
    if value.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{label} must stay inside the promotion run")
    return PurePosixPath(*parts)


def _promotion_document_relative_path(
    binding: _PromotionLeaseBinding,
    path: Path,
) -> PurePosixPath:
    binding.assert_active_and_unchanged()
    stable_root = Path(os.path.abspath(os.fspath(binding.filesystem_run_path)))
    canonical_root = Path(os.path.abspath(os.fspath(binding.canonical_run_path)))
    candidate = Path(os.path.abspath(os.fspath(path)))
    try:
        relative = candidate.relative_to(stable_root)
    except ValueError:
        try:
            relative = candidate.relative_to(canonical_root)
        except ValueError as exc:
            raise ValueError(
                "attempt promotion browser document escaped its run root"
            ) from exc
    normalized = _browser_relative_path(
        PurePosixPath(*relative.parts).as_posix(),
        label="attempt promotion browser document",
    )
    if normalized.suffix.lower() not in {".html", ".htm"}:
        raise ValueError("attempt promotion browser document must be HTML")
    return normalized


def _candidate_browser_resources(
    accessor: SecureRunMemberAccessor,
    binding: _PromotionLeaseBinding,
    document_relative: PurePosixPath,
    document_snapshot: SecureRunMemberSnapshot,
) -> dict[str, tuple[bytes, str]] | None:
    parts = document_relative.parts
    candidate_index = next(
        (
            index
            for index in range(len(parts) - 1, 1, -1)
            if parts[index] == "candidate"
            and parts[index - 1].startswith("attempt_")
        ),
        None,
    )
    if candidate_index is None:
        return None
    manifest_relative = PurePosixPath(
        *parts[:candidate_index],
        "attempt_candidate.json",
    )
    try:
        manifest_snapshot = accessor.read_bytes(
            manifest_relative,
            label="attempt candidate browser manifest",
            max_bytes=_PROMOTION_BROWSER_MAX_MANIFEST_BYTES,
        )
    except FileNotFoundError:
        return None
    assert manifest_snapshot.data is not None
    try:
        payload = json.loads(manifest_snapshot.data.decode("utf-8"))
        candidate = AttemptCandidate.model_validate(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        raise ValueError("invalid attempt candidate browser resource allowlist") from exc
    if candidate.run_id != binding.canonical_run_path.name:
        raise ValueError("attempt candidate browser resource run mismatch")
    if candidate.source_relative_path != document_relative.as_posix():
        raise ValueError("attempt candidate browser resource source mismatch")
    if document_snapshot.sha256 != candidate.source_sha256:
        raise ValueError("attempt candidate browser document hash mismatch")
    browser_resources = candidate.browser_resource_relative_paths
    if browser_resources is None:
        return None
    if len(candidate.dependency_relative_paths) > _PROMOTION_BROWSER_MAX_DEPENDENCIES:
        raise ValueError("attempt candidate browser dependency count exceeds limit")
    if len(browser_resources) > _PROMOTION_BROWSER_MAX_RESOURCES:
        raise ValueError("attempt candidate browser resource count exceeds limit")

    snapshot_root = PurePosixPath(*parts[: candidate_index + 1])
    dependencies: list[tuple[PurePosixPath, str]] = []
    for value in candidate.dependency_relative_paths:
        dependency = _browser_relative_path(
            value,
            label="attempt candidate dependency",
        )
        try:
            snapshot_relative = dependency.relative_to(snapshot_root)
        except ValueError as exc:
            raise ValueError(
                "attempt candidate dependency escaped its snapshot"
            ) from exc
        dependencies.append((dependency, snapshot_relative.as_posix()))

    dependency_paths = {
        dependency.as_posix() for dependency, _ in dependencies
    }
    normalized_browser_resources: list[str] = []
    for value in browser_resources:
        relative = _browser_relative_path(
            value,
            label="attempt candidate browser resource",
        )
        relative_value = relative.as_posix()
        if relative_value not in dependency_paths:
            raise ValueError("attempt candidate browser resource escaped dependencies")
        if not is_browser_preview_resource_path(relative_value):
            raise ValueError("attempt candidate browser resource is not static")
        normalized_browser_resources.append(relative_value)

    total_bytes = document_snapshot.size
    dependency_snapshots: dict[str, SecureRunMemberSnapshot] = {}
    dependency_entries: list[tuple[str, str]] = []
    for dependency, snapshot_relative in dependencies:
        relative_value = dependency.as_posix()
        snapshot = dependency_snapshots.get(relative_value)
        if snapshot is None:
            remaining_bytes = _PROMOTION_BROWSER_MAX_TOTAL_BYTES - total_bytes
            snapshot = accessor.read_bytes(
                dependency,
                label="attempt candidate browser dependency",
                max_bytes=min(
                    _PROMOTION_BROWSER_MAX_DEPENDENCY_BYTES,
                    max(remaining_bytes, 0),
                ),
            )
            dependency_snapshots[relative_value] = snapshot
        if total_bytes + snapshot.size > _PROMOTION_BROWSER_MAX_TOTAL_BYTES:
            raise ValueError("attempt candidate browser snapshot exceeds byte limit")
        total_bytes += snapshot.size
        dependency_entries.append((snapshot_relative, snapshot.sha256))

    digest = hashlib.sha256()
    for relative, dependency_sha256 in sorted(dependency_entries):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(dependency_sha256.encode("ascii"))
        digest.update(b"\0")
    if digest.hexdigest() != candidate.dependency_fingerprint:
        raise ValueError("attempt candidate dependency fingerprint mismatch")

    retained: dict[str, tuple[bytes, str]] = {}
    for relative_value in normalized_browser_resources:
        snapshot = dependency_snapshots[relative_value]
        assert snapshot.data is not None
        retained[relative_value] = (
            snapshot.data,
            mimetypes.guess_type(PurePosixPath(relative_value).name)[0]
            or "application/octet-stream",
        )
    return retained


def _browser_resource_is_internal(relative: PurePosixPath) -> bool:
    lowered_parts = tuple(part.lower() for part in relative.parts)
    return (
        any(part in _BROWSER_BLOCKED_INTERNAL_PARTS for part in lowered_parts)
        or relative.name.lower() in _BROWSER_BLOCKED_INTERNAL_NAMES
        or relative.suffix.lower() in _BROWSER_BLOCKED_SUFFIXES
    )


class _PromotionBrowserDocumentSession:
    def __init__(
        self,
        *,
        url: str,
        binding: _PromotionLeaseBinding | None = None,
        accessor: SecureRunMemberAccessor | None = None,
        document_relative: PurePosixPath | None = None,
        document_bytes: bytes | None = None,
        browser_resources: Mapping[str, tuple[bytes, str]] | None = None,
    ) -> None:
        self.url = url
        self._binding = binding
        self._accessor = accessor
        self._document_relative = document_relative
        self._document_bytes = document_bytes
        self._browser_resources = (
            None
            if browser_resources is None
            else MappingProxyType(dict(browser_resources))
        )
        self._condition = threading.Condition()
        self._state = "NEW"
        self._inflight_callbacks = 0
        self._context: Any | None = None
        self._http_handler: Callable[[Any], None] | None = None
        self._websocket_handler: Callable[[Any], None] | None = None
        self._http_installed = False
        self._websocket_installed = False

    def install(self, page: Any) -> None:
        with self._condition:
            if self._state != "NEW":
                raise RuntimeError("attempt promotion browser session is already used")
            self._state = "ACTIVE"
        if self._binding is None or self._accessor is None:
            return
        context = page.context
        http_handler = self._route_request
        websocket_handler = self._close_websocket
        with self._condition:
            self._context = context
            self._http_handler = http_handler
            self._websocket_handler = websocket_handler
        try:
            context.set_offline(True)
            context.route("**/*", http_handler)
            with self._condition:
                self._http_installed = True
            route_web_socket = getattr(context, "route_web_socket", None)
            if callable(route_web_socket):
                route_web_socket("**/*", websocket_handler)
                with self._condition:
                    self._websocket_installed = True
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        with self._condition:
            if self._state == "CLOSED":
                return
            if self._state == "CLOSING":
                while self._state != "CLOSED":
                    self._condition.wait()
                return
            self._state = "CLOSING"
            context = self._context
            http_handler = self._http_handler
            websocket_handler = self._websocket_handler
            http_installed = self._http_installed
            websocket_installed = self._websocket_installed
            self._http_installed = False
            self._websocket_installed = False

        if context is not None and http_installed and http_handler is not None:
            try:
                context.unroute("**/*", http_handler)
            except Exception:
                pass
        if (
            context is not None
            and websocket_installed
            and websocket_handler is not None
        ):
            unroute_web_socket = getattr(context, "unroute_web_socket", None)
            if callable(unroute_web_socket):
                try:
                    unroute_web_socket("**/*", websocket_handler)
                except Exception:
                    pass

        with self._condition:
            while self._inflight_callbacks:
                self._condition.wait()
            self._state = "CLOSED"
            self._condition.notify_all()

    def _enter_callback(self) -> bool:
        with self._condition:
            if self._state != "ACTIVE":
                return False
            self._inflight_callbacks += 1
            return True

    def _leave_callback(self) -> None:
        with self._condition:
            self._inflight_callbacks -= 1
            self._condition.notify_all()

    def _reserve_fulfill(self) -> bool:
        with self._condition:
            return self._state == "ACTIVE"

    @staticmethod
    def _close_websocket(socket: Any) -> None:
        socket.close()

    def _relative_from_url(self, raw_url: str) -> PurePosixPath | None:
        try:
            parsed = urlsplit(raw_url)
            if (
                parsed.scheme != "https"
                or parsed.netloc != urlsplit(_PROMOTION_BROWSER_ORIGIN).netloc
            ):
                return None
            decoded = unquote_to_bytes(parsed.path).decode("utf-8")
            if not decoded.startswith("/"):
                return None
            return _browser_relative_path(
                decoded[1:],
                label="attempt promotion browser request",
            )
        except (UnicodeDecodeError, ValueError):
            return None

    @staticmethod
    def _abort(route: Any) -> None:
        route.abort("blockedbyclient")

    def _route_request(self, route: Any) -> None:
        if not self._enter_callback():
            self._abort(route)
            return
        accessor = self._accessor
        document_relative = self._document_relative
        document_bytes = self._document_bytes
        try:
            if (
                accessor is None
                or document_relative is None
                or document_bytes is None
            ):
                self._abort(route)
                return
            request = route.request
            relative = self._relative_from_url(request.url)
            if request.method != "GET" or relative is None:
                self._abort(route)
                return

            if relative == document_relative:
                response_body = document_bytes
                content_type = "text/html; charset=utf-8"
            else:
                if _browser_resource_is_internal(relative):
                    self._abort(route)
                    return
                relative_value = relative.as_posix()
                if self._browser_resources is None:
                    if not is_browser_preview_resource_path(relative_value):
                        self._abort(route)
                        return
                    try:
                        snapshot = accessor.read_bytes(
                            relative,
                            label="attempt promotion browser resource",
                            max_bytes=_PROMOTION_BROWSER_MAX_DEPENDENCY_BYTES,
                        )
                    except (OSError, ValueError):
                        self._abort(route)
                        return
                    assert snapshot.data is not None
                    response_body = snapshot.data
                    content_type = (
                        mimetypes.guess_type(relative.name)[0]
                        or "application/octet-stream"
                    )
                else:
                    retained = self._browser_resources.get(relative_value)
                    if retained is None:
                        self._abort(route)
                        return
                    response_body, content_type = retained

            try:
                accessor._assert_root_unchanged()
            except (OSError, ValueError):
                self._abort(route)
                return
            if not self._reserve_fulfill():
                self._abort(route)
                return
            route.fulfill(
                status=200,
                body=response_body,
                content_type=content_type,
                headers={"Cache-Control": "no-store"},
            )
        finally:
            self._leave_callback()


@contextmanager
def promotion_browser_document_session(
    path: Path,
) -> Iterator[_PromotionBrowserDocumentSession]:
    """Bind promotion rendering to retained run-contained document bytes."""

    binding = _ACTIVE_PROMOTION_LEASE.get()
    if binding is None:
        session = _PromotionBrowserDocumentSession(
            url=Path(path).resolve().as_uri()
        )
        try:
            yield session
        finally:
            session.close()
        return
    document_relative = _promotion_document_relative_path(binding, path)
    try:
        with secure_run_member_access(binding.filesystem_run_path) as accessor:
            document_snapshot = accessor.read_bytes(
                document_relative,
                label="attempt promotion browser document",
                max_bytes=min(
                    _PROMOTION_BROWSER_MAX_DOCUMENT_BYTES,
                    _PROMOTION_BROWSER_MAX_TOTAL_BYTES,
                ),
            )
            assert document_snapshot.data is not None
            browser_resources = _candidate_browser_resources(
                accessor,
                binding,
                document_relative,
                document_snapshot,
            )
            document_url = (
                f"{_PROMOTION_BROWSER_ORIGIN}/"
                f"{quote(document_relative.as_posix(), safe='/')}"
            )
            session = _PromotionBrowserDocumentSession(
                url=document_url,
                binding=binding,
                accessor=accessor,
                document_relative=document_relative,
                document_bytes=document_snapshot.data,
                browser_resources=browser_resources,
            )
            try:
                yield session
            finally:
                session.close()
    finally:
        binding.assert_active_and_unchanged()


def is_active_promotion_filesystem_root(path: Path) -> bool:
    binding = _ACTIVE_PROMOTION_LEASE.get()
    if binding is None:
        return False
    binding.assert_active()
    candidate = Path(os.path.abspath(os.fspath(path)))
    filesystem_root = Path(
        os.path.abspath(os.fspath(binding.filesystem_run_path))
    )
    if candidate != filesystem_root:
        return False
    metadata = os.stat(candidate)
    return (metadata.st_dev, metadata.st_ino) == binding.run_identity


def _opened_control_context_before_open() -> None:
    """Test seam immediately before a non-leased run root is opened."""


@dataclass(frozen=True)
class _OpenedControlContext:
    requested_run_path: Path
    logical_run_id: str
    run_reference: int | Path
    control_reference: int | Path
    run_metadata: os.stat_result
    control_metadata: os.stat_result
    binding: _PromotionLeaseBinding | None = None

    @property
    def run_identity(self) -> tuple[int, int]:
        return self.run_metadata.st_dev, self.run_metadata.st_ino

    def assert_unchanged(self) -> None:
        if self.binding is not None:
            self.binding.assert_active_and_unchanged()
        if isinstance(self.run_reference, int):
            _verify_control_identity(
                self.requested_run_path,
                self.run_reference,
                self.run_metadata,
                self.control_reference,
                self.control_metadata,
            )
            return
        requested_metadata = _portable_lstat(self.requested_run_path)
        _validate_directory(requested_metadata)
        if not _same_identity(self.run_metadata, requested_metadata):
            raise ValueError("attempt selection run directory changed during access")
        if not _same_identity(
            self.run_metadata,
            _portable_lstat(self.run_reference),
        ):
            raise ValueError("attempt selection run directory changed during access")
        if not _same_identity(
            self.control_metadata,
            _portable_lstat(self.control_reference),
        ):
            raise ValueError(
                "attempt selection control directory changed during access"
            )

    def read_json(self, name: str) -> dict[str, Any] | None:
        return _read_opened_control_json(self, name)

    def write_json(self, name: str, payload: dict[str, Any]) -> None:
        _write_opened_control_json(self, name, payload)

    def delete_file(self, name: str) -> None:
        _delete_opened_control_file(self, name)


@contextmanager
def _open_control_directory(
    run_dir: Path,
    *,
    create: bool,
    expected_run_identity: tuple[int, int] | None = None,
) -> Iterator[_OpenedControlContext | None]:
    binding = _active_promotion_lease_for(run_dir)
    if binding is not None:
        if (
            expected_run_identity is not None
            and binding.run_identity != expected_run_identity
        ):
            raise ValueError("attempt selection run directory changed before access")
        run_metadata = (
            os.fstat(binding.run_reference)
            if isinstance(binding.run_reference, int)
            else _portable_lstat(binding.run_reference)
        )
        control_metadata = (
            os.fstat(binding.control_reference)
            if isinstance(binding.control_reference, int)
            else _portable_lstat(binding.control_reference)
        )
        _validate_directory(run_metadata)
        _validate_directory(control_metadata)
        yield _OpenedControlContext(
            requested_run_path=binding.requested_run_path,
            logical_run_id=binding.canonical_run_path.name,
            run_reference=binding.run_reference,
            control_reference=binding.control_reference,
            run_metadata=run_metadata,
            control_metadata=control_metadata,
            binding=binding,
        )
        binding.assert_requested_run_unchanged()
        return
    run_path = Path(os.path.abspath(os.fspath(run_dir)))
    run_metadata = _portable_lstat(run_path)
    _validate_directory(run_metadata)
    if (
        expected_run_identity is not None
        and (run_metadata.st_dev, run_metadata.st_ino) != expected_run_identity
    ):
        raise ValueError("attempt selection run directory changed before access")

    if not _SECURE_DIR_FD_AVAILABLE:
        if _RUNTIME_OS.name != "nt":
            raise RuntimeError(
                "secure attempt selection control primitive is unavailable"
            )
        canonical_run_path = Path(os.path.realpath(run_path, strict=True))
        canonical_run_metadata = _portable_lstat(canonical_run_path)
        _validate_directory(canonical_run_metadata)
        if not _same_identity(run_metadata, canonical_run_metadata):
            raise ValueError("attempt selection run directory changed during resolution")
        _opened_control_context_before_open()
        with ExitStack() as stack:
            stack.enter_context(
                _windows_directory_replacement_guard(canonical_run_path)
            )
            opened_run_metadata = _portable_lstat(canonical_run_path)
            requested_run_metadata = _portable_lstat(run_path)
            _validate_directory(opened_run_metadata)
            _validate_directory(requested_run_metadata)
            if (
                not _same_identity(canonical_run_metadata, opened_run_metadata)
                or not _same_identity(opened_run_metadata, requested_run_metadata)
            ):
                raise ValueError(
                    "attempt selection run directory changed during access"
                )
            control_path = canonical_run_path / _CONTROL_DIRECTORY_NAME
            if create:
                try:
                    control_path.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            elif not control_path.exists():
                yield None
                return
            control_metadata = _portable_lstat(control_path)
            _validate_directory(control_metadata)
            stack.enter_context(
                _windows_directory_replacement_guard(control_path)
            )
            opened_control_metadata = _portable_lstat(control_path)
            _validate_directory(opened_control_metadata)
            if not _same_identity(control_metadata, opened_control_metadata):
                raise ValueError(
                    "attempt selection control directory changed during access"
                )
            context = _OpenedControlContext(
                requested_run_path=run_path,
                logical_run_id=run_path.name,
                run_reference=canonical_run_path,
                control_reference=control_path,
                run_metadata=opened_run_metadata,
                control_metadata=opened_control_metadata,
            )
            context.assert_unchanged()
            yield context
            context.assert_unchanged()
        return

    directory_flags = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC
    _opened_control_context_before_open()
    run_fd = os.open(run_path, directory_flags)
    control_fd: int | None = None
    try:
        opened_run_metadata = os.fstat(run_fd)
        _validate_directory(opened_run_metadata)
        if not _same_identity(run_metadata, opened_run_metadata):
            raise ValueError("attempt selection run directory changed during access")
        if (
            expected_run_identity is not None
            and (
                opened_run_metadata.st_dev,
                opened_run_metadata.st_ino,
            )
            != expected_run_identity
        ):
            raise ValueError("attempt selection run directory changed before access")
        if create:
            try:
                os.mkdir(_CONTROL_DIRECTORY_NAME, 0o700, dir_fd=run_fd)
                os.fsync(run_fd)
            except FileExistsError:
                pass
        else:
            try:
                os.stat(
                    _CONTROL_DIRECTORY_NAME,
                    dir_fd=run_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                yield None
                return
        control_metadata = os.stat(
            _CONTROL_DIRECTORY_NAME,
            dir_fd=run_fd,
            follow_symlinks=False,
        )
        _validate_directory(control_metadata)
        control_fd = os.open(
            _CONTROL_DIRECTORY_NAME,
            directory_flags,
            dir_fd=run_fd,
        )
        opened_control_metadata = os.fstat(control_fd)
        _validate_directory(opened_control_metadata)
        if not _same_identity(control_metadata, opened_control_metadata):
            raise ValueError("attempt selection control directory changed during access")
        yield _OpenedControlContext(
            requested_run_path=run_path,
            logical_run_id=run_path.name,
            run_reference=run_fd,
            control_reference=control_fd,
            run_metadata=opened_run_metadata,
            control_metadata=opened_control_metadata,
        )
        _verify_control_identity(
            run_path,
            run_fd,
            opened_run_metadata,
            control_fd,
            opened_control_metadata,
        )
    finally:
        if control_fd is not None:
            os.close(control_fd)
        os.close(run_fd)


@contextmanager
def _open_control_directory_from_stable_lease(
    lease: _StableCoordinationBinding,
    *,
    create: bool,
) -> Iterator[tuple[int | Path, int | Path] | None]:
    if not isinstance(lease.run_reference, int):
        if os.name != "nt" or not lease.path_replacement_guarded:
            raise RuntimeError(
                "path-based promotion leases require a held Windows directory handle"
            )
        run_path = lease.canonical_run_path
        if promotion_run_identity(run_path) != lease.run_identity:
            raise ValueError("attempt selection run directory changed before access")
        control_path = run_path / _CONTROL_DIRECTORY_NAME
        if create:
            try:
                control_path.mkdir(mode=0o700)
            except FileExistsError:
                pass
        elif not control_path.exists():
            yield None
            return
        control_metadata = _portable_lstat(control_path)
        _validate_directory(control_metadata)
        with _windows_directory_replacement_guard(control_path):
            opened_control_metadata = _portable_lstat(control_path)
            if not _same_identity(control_metadata, opened_control_metadata):
                raise ValueError(
                    "attempt selection control directory changed during access"
                )
            if promotion_run_identity(run_path) != lease.run_identity:
                raise ValueError("attempt selection run directory changed during access")
            yield run_path, control_path
            if promotion_run_identity(run_path) != lease.run_identity:
                raise ValueError("attempt selection run directory changed during access")
            if not _same_identity(
                opened_control_metadata,
                _portable_lstat(control_path),
            ):
                raise ValueError(
                    "attempt selection control directory changed during access"
                )
        return

    run_fd = lease.run_reference
    run_metadata = os.fstat(run_fd)
    _validate_directory(run_metadata)
    if (run_metadata.st_dev, run_metadata.st_ino) != lease.run_identity:
        raise ValueError("attempt selection locked run directory changed")

    control_fd: int | None = None
    directory_flags = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC
    try:
        if create:
            try:
                os.mkdir(_CONTROL_DIRECTORY_NAME, 0o700, dir_fd=run_fd)
                os.fsync(run_fd)
            except FileExistsError:
                pass
        else:
            try:
                os.stat(
                    _CONTROL_DIRECTORY_NAME,
                    dir_fd=run_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                yield None
                return
        control_metadata = os.stat(
            _CONTROL_DIRECTORY_NAME,
            dir_fd=run_fd,
            follow_symlinks=False,
        )
        _validate_directory(control_metadata)
        control_fd = os.open(
            _CONTROL_DIRECTORY_NAME,
            directory_flags,
            dir_fd=run_fd,
        )
        opened_control_metadata = os.fstat(control_fd)
        _validate_directory(opened_control_metadata)
        if not _same_identity(control_metadata, opened_control_metadata):
            raise ValueError("attempt selection control directory changed during access")
        yield run_fd, control_fd
        _verify_control_identity(
            lease.requested_run_path,
            run_fd,
            run_metadata,
            control_fd,
            opened_control_metadata,
        )
    finally:
        if control_fd is not None:
            os.close(control_fd)


def _verify_control_identity(
    run_path: Path,
    run_fd: int,
    run_metadata: os.stat_result,
    control_fd: int,
    control_metadata: os.stat_result,
) -> None:
    if not _same_identity(run_metadata, os.fstat(run_fd)):
        raise ValueError("attempt selection run directory changed during access")
    binding = _ACTIVE_PROMOTION_LEASE.get()
    requested_path = Path(os.path.abspath(os.fspath(run_path)))
    if binding is not None and requested_path == binding.filesystem_run_path:
        binding.assert_active_and_unchanged()
        if (run_metadata.st_dev, run_metadata.st_ino) != binding.run_identity:
            raise ValueError("attempt selection run directory changed during access")
    elif not _same_identity(run_metadata, _portable_lstat(run_path)):
        raise ValueError("attempt selection run directory changed during access")
    current_control = os.stat(
        _CONTROL_DIRECTORY_NAME,
        dir_fd=run_fd,
        follow_symlinks=False,
    )
    if not _same_identity(control_metadata, current_control):
        raise ValueError("attempt selection control directory changed during access")
    if not _same_identity(control_metadata, os.fstat(control_fd)):
        raise ValueError("attempt selection control directory changed during access")


def _control_path(directory: int | Path, name: str) -> Path:
    if isinstance(directory, Path):
        return directory / name
    raise TypeError("directory descriptor does not have a filesystem path")


def _decode_control_json(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid attempt selection control JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("attempt selection control JSON must contain an object")
    return payload


def _read_opened_control_json(
    context: _OpenedControlContext,
    name: str,
) -> dict[str, Any] | None:
    context.assert_unchanged()
    control = context.control_reference
    try:
        if isinstance(control, Path):
            path = _control_path(control, name)
            metadata = _portable_lstat(path)
            _validate_regular_file(metadata)
            raw = path.read_bytes()
        else:
            descriptor = os.open(
                name,
                os.O_RDONLY | _NOFOLLOW | _CLOEXEC,
                dir_fd=control,
            )
            try:
                _validate_regular_file(os.fstat(descriptor))
                with os.fdopen(descriptor, "rb", closefd=False) as handle:
                    raw = handle.read()
            finally:
                os.close(descriptor)
    except FileNotFoundError:
        return None
    context.assert_unchanged()
    return _decode_control_json(raw)


def _write_opened_control_json(
    context: _OpenedControlContext,
    name: str,
    payload: dict[str, Any],
) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        default=str,
    ).encode("utf-8")
    temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"
    context.assert_unchanged()
    control = context.control_reference
    if isinstance(control, Path):
        temporary_path = control / temporary_name
        descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            _validate_regular_file(os.fstat(descriptor))
            context.assert_unchanged()
            os.replace(temporary_path, control / name)
        finally:
            os.close(descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        context.assert_unchanged()
        return

    descriptor = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
        0o600,
        dir_fd=control,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        _validate_regular_file(os.fstat(descriptor))
        context.assert_unchanged()
        os.replace(
            temporary_name,
            name,
            src_dir_fd=control,
            dst_dir_fd=control,
        )
        os.fsync(control)
    finally:
        os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=control)
        except FileNotFoundError:
            pass
    context.assert_unchanged()


def _delete_opened_control_file(
    context: _OpenedControlContext,
    name: str,
) -> None:
    context.assert_unchanged()
    control = context.control_reference
    if isinstance(control, Path):
        path = control / name
        try:
            _validate_regular_file(_portable_lstat(path))
            path.unlink()
        except FileNotFoundError:
            return
        context.assert_unchanged()
        return
    try:
        metadata = os.stat(name, dir_fd=control, follow_symlinks=False)
    except FileNotFoundError:
        return
    _validate_regular_file(metadata)
    context.assert_unchanged()
    os.unlink(name, dir_fd=control)
    os.fsync(control)
    context.assert_unchanged()


@contextmanager
def _opened_control_context(
    run_dir: Path,
    *,
    create: bool,
) -> Iterator[_OpenedControlContext | None]:
    with _open_control_directory(run_dir, create=create) as context:
        yield context


def _read_control_json(run_dir: Path, name: str) -> dict[str, Any] | None:
    with _opened_control_context(run_dir, create=False) as context:
        if context is None:
            return None
        return context.read_json(name)


def _write_control_json(run_dir: Path, name: str, payload: dict[str, Any]) -> None:
    with _opened_control_context(run_dir, create=True) as context:
        assert context is not None
        context.write_json(name, payload)


def _delete_control_file(run_dir: Path, name: str) -> None:
    with _opened_control_context(run_dir, create=False) as context:
        if context is None:
            return
        context.delete_file(name)


def _windows_named_mutex_name(
    owner_sid: str,
    run_identity: tuple[int, int],
) -> str:
    identity = (
        f"sid:{owner_sid.casefold()}\0"
        f"run:{run_identity[0]}:{run_identity[1]}"
    )
    token = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"Local\\AutoDesignAttemptPromotion-{token}"


def _windows_directory_guard_api() -> tuple[Any, Any, Any]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
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
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    return create_file, close_handle, ctypes.get_last_error


@contextmanager
def _windows_directory_replacement_guard(path: Path) -> Iterator[int]:
    """Hold a directory handle that denies rename/delete for the lease lifetime."""

    file_list_directory = 0x0001
    file_read_attributes = 0x0080
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000
    import ctypes

    invalid_handle_value = ctypes.c_void_p(-1).value
    create_file, close_handle, get_last_error = _windows_directory_guard_api()
    handle = create_file(
        str(path),
        file_list_directory | file_read_attributes,
        file_share_read | file_share_write,
        None,
        open_existing,
        file_flag_backup_semantics | file_flag_open_reparse_point,
        None,
    )
    handle_value = getattr(handle, "value", handle)
    if handle_value in {None, 0, invalid_handle_value}:
        raise OSError(get_last_error(), "CreateFileW directory guard failed")
    try:
        yield int(handle_value)
    finally:
        if not close_handle(handle):
            raise OSError(get_last_error(), "CloseHandle directory guard failed")


def _posix_stable_run_path(
    run_descriptor: int,
    run_identity: tuple[int, int],
) -> Path:
    candidates: list[Path] = []
    if sys.platform == "darwin":
        candidates.append(
            Path("/.vol") / str(run_identity[0]) / str(run_identity[1])
        )
    candidates.extend(
        (
            Path(f"/proc/{os.getpid()}/fd/{run_descriptor}"),
            Path(f"/proc/self/fd/{run_descriptor}"),
            Path(f"/dev/fd/{run_descriptor}"),
        )
    )
    for candidate in candidates:
        try:
            metadata = os.stat(candidate)
            traversed_metadata = os.stat(candidate / ".")
        except OSError:
            continue
        if (
            (metadata.st_dev, metadata.st_ino) == run_identity
            and (traversed_metadata.st_dev, traversed_metadata.st_ino)
            == run_identity
        ):
            return candidate
    raise RuntimeError("no inode-stable path is available for promotion writes")


def _windows_mutex_api() -> tuple[Any, Any, Any, Any, Any]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    create_mutex.restype = wintypes.HANDLE
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait_for_single_object.restype = wintypes.DWORD
    release_mutex = kernel32.ReleaseMutex
    release_mutex.argtypes = [wintypes.HANDLE]
    release_mutex.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    return (
        create_mutex,
        wait_for_single_object,
        release_mutex,
        close_handle,
        ctypes.get_last_error,
    )


@contextmanager
def _windows_named_mutex_lease(name: str) -> Iterator[None]:
    wait_object_0 = 0x00000000
    wait_abandoned = 0x00000080
    wait_timeout = 0x00000102
    wait_failed = 0xFFFFFFFF
    infinite = 0xFFFFFFFF
    (
        create_mutex,
        wait_for_single_object,
        release_mutex,
        close_handle,
        get_last_error,
    ) = _windows_mutex_api()
    handle = create_mutex(None, False, name)
    if not handle:
        raise OSError(get_last_error(), "CreateMutexW failed")
    acquired = False
    try:
        wait_result = wait_for_single_object(handle, infinite)
        if wait_result in {wait_object_0, wait_abandoned}:
            acquired = True
        elif wait_result == wait_timeout:
            raise TimeoutError("Windows promotion mutex wait timed out")
        elif wait_result == wait_failed:
            raise OSError(get_last_error(), "WaitForSingleObject failed")
        else:
            raise OSError(
                f"unexpected WaitForSingleObject result: {wait_result:#x}"
            )
        yield
    finally:
        release_error: OSError | None = None
        if acquired and not release_mutex(handle):
            release_error = OSError(get_last_error(), "ReleaseMutex failed")
        close_error: OSError | None = None
        if not close_handle(handle):
            close_error = OSError(get_last_error(), "CloseHandle failed")
        if release_error is not None:
            raise release_error
        if close_error is not None:
            raise close_error


def _windows_sid_to_string(sid_pointer: int) -> str:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    convert_sid.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    sid_string = wintypes.LPWSTR()
    if not convert_sid(ctypes.c_void_p(sid_pointer), ctypes.byref(sid_string)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        value = sid_string.value
        if not value:
            raise OSError("Windows returned an empty user SID")
        return value
    finally:
        kernel32.LocalFree(ctypes.cast(sid_string, ctypes.c_void_p))


def _windows_current_user_sid() -> str:
    import ctypes
    from ctypes import wintypes

    token_query = 0x0008
    token_user_class = 1
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("user", _SidAndAttributes)]

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_uint,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token,
            token_user_class,
            None,
            0,
            ctypes.byref(required),
        )
        if required.value == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            token_user_class,
            buffer,
            required,
            ctypes.byref(required),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        return _windows_sid_to_string(token_user.user.sid)
    finally:
        kernel32.CloseHandle(token)


def _windows_create_private_directory(path: Path, user_sid: str) -> None:
    import ctypes
    from ctypes import wintypes

    security_descriptor_revision = 1
    error_already_exists = 183
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("security_descriptor", ctypes.c_void_p),
            ("inherit_handle", wintypes.BOOL),
        ]

    convert_descriptor = (
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    )
    convert_descriptor.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert_descriptor.restype = wintypes.BOOL
    kernel32.CreateDirectoryW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(_SecurityAttributes),
    ]
    kernel32.CreateDirectoryW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    descriptor = ctypes.c_void_p()
    descriptor_size = wintypes.DWORD()
    sddl = f"D:P(A;;FA;;;{user_sid})(A;;FA;;;SY)(A;;FA;;;BA)"
    if not convert_descriptor(
        sddl,
        security_descriptor_revision,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = _SecurityAttributes(
            ctypes.sizeof(_SecurityAttributes),
            descriptor,
            False,
        )
        if not kernel32.CreateDirectoryW(str(path), ctypes.byref(attributes)):
            error = ctypes.get_last_error()
            if error != error_already_exists:
                raise ctypes.WinError(error)
    finally:
        kernel32.LocalFree(descriptor)


def _windows_security_descriptor(path: Path) -> tuple[int, int, int]:
    import ctypes
    from ctypes import wintypes

    se_file_object = 1
    owner_security_information = 0x00000001
    dacl_security_information = 0x00000004
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    get_security = advapi32.GetNamedSecurityInfoW
    get_security.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_uint,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security.restype = wintypes.DWORD
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    error = get_security(
        str(path),
        se_file_object,
        owner_security_information | dacl_security_information,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if error:
        raise ctypes.WinError(error)
    return owner.value or 0, dacl.value or 0, descriptor.value or 0


def _windows_path_owner_sid(path: Path) -> str:
    import ctypes

    owner, _, descriptor = _windows_security_descriptor(path)
    try:
        if not owner:
            raise ValueError("attempt promotion coordination directory has no owner")
        return _windows_sid_to_string(owner)
    finally:
        if descriptor:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree.restype = ctypes.c_void_p
            kernel32.LocalFree(ctypes.c_void_p(descriptor))


def _windows_directory_is_private(path: Path, user_sid: str) -> bool:
    import ctypes
    from ctypes import wintypes

    access_allowed_ace_type = 0
    access_allowed_compound_ace_type = 4
    access_allowed_object_ace_type = 5
    access_allowed_callback_ace_type = 9
    access_allowed_callback_object_ace_type = 11
    acl_size_information_class = 2
    se_dacl_protected = 0x1000
    allowed_sids = {
        user_sid.casefold(),
        "S-1-5-18".casefold(),
        "S-1-5-32-544".casefold(),
    }
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_uint,
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetAce.restype = wintypes.BOOL

    class _AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("ace_count", wintypes.DWORD),
            ("bytes_in_use", wintypes.DWORD),
            ("bytes_free", wintypes.DWORD),
        ]

    class _AceHeader(ctypes.Structure):
        _fields_ = [
            ("ace_type", ctypes.c_ubyte),
            ("ace_flags", ctypes.c_ubyte),
            ("ace_size", wintypes.WORD),
        ]

    class _AccessAllowedAce(ctypes.Structure):
        _fields_ = [
            ("header", _AceHeader),
            ("mask", wintypes.DWORD),
            ("sid_start", wintypes.DWORD),
        ]

    owner, dacl, descriptor = _windows_security_descriptor(path)
    del owner
    try:
        if not dacl:
            return False
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(
            ctypes.c_void_p(descriptor),
            ctypes.byref(control),
            ctypes.byref(revision),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not control.value & se_dacl_protected:
            return False

        information = _AclSizeInformation()
        if not advapi32.GetAclInformation(
            ctypes.c_void_p(dacl),
            ctypes.byref(information),
            ctypes.sizeof(information),
            acl_size_information_class,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        saw_user = False
        unsupported_allow_types = {
            access_allowed_compound_ace_type,
            access_allowed_object_ace_type,
            access_allowed_callback_ace_type,
            access_allowed_callback_object_ace_type,
        }
        for index in range(information.ace_count):
            ace_pointer = ctypes.c_void_p()
            if not advapi32.GetAce(
                ctypes.c_void_p(dacl), index, ctypes.byref(ace_pointer)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            header = ctypes.cast(
                ace_pointer, ctypes.POINTER(_AceHeader)
            ).contents
            if header.ace_type in unsupported_allow_types:
                return False
            if header.ace_type != access_allowed_ace_type:
                continue
            sid_pointer = ace_pointer.value + _AccessAllowedAce.sid_start.offset
            sid = _windows_sid_to_string(sid_pointer).casefold()
            if sid not in allowed_sids:
                return False
            saw_user = saw_user or sid == user_sid.casefold()
        return saw_user
    finally:
        if descriptor:
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree.restype = ctypes.c_void_p
            kernel32.LocalFree(ctypes.c_void_p(descriptor))


def _coordination_owner_identity() -> tuple[str, str]:
    getuid = getattr(os, "getuid", None)
    if callable(getuid):
        return "uid", str(getuid())
    if os.name == "nt":
        return "sid", _windows_current_user_sid()
    raise RuntimeError("cannot establish a per-user promotion lease namespace")


def _coordination_lock_details(run_dir: Path) -> tuple[Path, tuple[int, int]]:
    owner_kind, owner_identity = _coordination_owner_identity()
    owner_token = hashlib.sha256(
        f"{owner_kind}:{owner_identity}".encode("utf-8")
    ).hexdigest()[:32]
    root = (
        Path(tempfile.gettempdir())
        / f"autodesign-promotion-leases-{owner_kind}-{owner_token}"
    )
    if owner_kind == "sid":
        _windows_create_private_directory(root, owner_identity)
    else:
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            pass
    metadata = _portable_lstat(root)
    _validate_directory(metadata)
    if owner_kind == "uid":
        if metadata.st_uid != int(owner_identity):
            raise ValueError("attempt promotion coordination directory has wrong owner")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError("attempt promotion coordination directory is not private")
    else:
        if _windows_path_owner_sid(root).casefold() != owner_identity.casefold():
            raise ValueError("attempt promotion coordination directory has wrong owner")
        if not _windows_directory_is_private(root, owner_identity):
            raise ValueError("attempt promotion coordination directory is not private")

    run_path = Path(os.path.abspath(os.fspath(run_dir)))
    run_metadata = _portable_lstat(run_path)
    _validate_directory(run_metadata)
    run_identity = (run_metadata.st_dev, run_metadata.st_ino)
    canonical_identity = f"{run_identity[0]}:{run_identity[1]}"
    key = hashlib.sha256(canonical_identity.encode("ascii")).hexdigest()
    lease_directory = root / f"{key}.lease"
    if owner_kind == "sid":
        _windows_create_private_directory(lease_directory, owner_identity)
    else:
        try:
            lease_directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
    lease_metadata = _portable_lstat(lease_directory)
    _validate_directory(lease_metadata)
    if owner_kind == "uid":
        if lease_metadata.st_uid != int(owner_identity):
            raise ValueError("attempt promotion lease directory has wrong owner")
        if stat.S_IMODE(lease_metadata.st_mode) & 0o077:
            raise ValueError("attempt promotion lease directory is not private")
    else:
        if (
            _windows_path_owner_sid(lease_directory).casefold()
            != owner_identity.casefold()
        ):
            raise ValueError("attempt promotion lease directory has wrong owner")
        if not _windows_directory_is_private(lease_directory, owner_identity):
            raise ValueError("attempt promotion lease directory is not private")
    return lease_directory / "coordination.lock", run_identity


def _coordination_lock_path(run_dir: Path) -> Path:
    lock_path, _ = _coordination_lock_details(run_dir)
    return lock_path


@contextmanager
def _stable_coordination_lease(
    run_dir: Path,
) -> Iterator[_StableCoordinationBinding]:
    if os.name == "nt":
        _, run_identity = _coordination_lock_details(run_dir)
        owner_kind, owner_identity = _coordination_owner_identity()
        if owner_kind != "sid":
            raise RuntimeError("Windows promotion lease requires a user SID")
        mutex_name = _windows_named_mutex_name(owner_identity, run_identity)
        requested_run_path = Path(
            _RUNTIME_OS.path.abspath(_RUNTIME_OS.fspath(run_dir))
        )
        canonical_run_path = Path(
            _RUNTIME_OS.path.realpath(requested_run_path, strict=True)
        )
        with _windows_named_mutex_lease(mutex_name):
            with _windows_directory_replacement_guard(canonical_run_path):
                if promotion_run_identity(requested_run_path) != run_identity:
                    raise ValueError(
                        "attempt promotion run alias changed during acquisition"
                    )
                if promotion_run_identity(canonical_run_path) != run_identity:
                    raise ValueError(
                        "attempt promotion run directory changed during acquisition"
                    )
                yield _StableCoordinationBinding(
                    requested_run_path=requested_run_path,
                    canonical_run_path=canonical_run_path,
                    filesystem_run_path=canonical_run_path,
                    run_identity=run_identity,
                    run_reference=canonical_run_path,
                    path_replacement_guarded=True,
                )
        return
    if os.name != "posix":
        raise RuntimeError("unsupported promotion lease platform")

    requested_run_path = Path(os.path.abspath(os.fspath(run_dir)))
    requested_metadata = _portable_lstat(requested_run_path)
    _validate_directory(requested_metadata)
    run_identity = (requested_metadata.st_dev, requested_metadata.st_ino)
    canonical_run_path = Path(
        os.path.realpath(requested_run_path, strict=True)
    )
    canonical_metadata = _portable_lstat(canonical_run_path)
    _validate_directory(canonical_metadata)
    if (canonical_metadata.st_dev, canonical_metadata.st_ino) != run_identity:
        raise ValueError("attempt promotion run directory changed during resolution")

    lease_descriptor = os.open(
        canonical_run_path,
        os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
    )
    try:
        opened_run_metadata = os.fstat(lease_descriptor)
        _validate_directory(opened_run_metadata)
        if not _same_identity(canonical_metadata, opened_run_metadata):
            raise ValueError("attempt promotion run directory changed during open")

        import fcntl

        fcntl.flock(lease_descriptor, fcntl.LOCK_EX)
        try:
            if not _same_identity(
                opened_run_metadata, _portable_lstat(canonical_run_path)
            ):
                raise ValueError(
                    "attempt promotion run directory changed during acquisition"
                )
            if not _same_identity(
                opened_run_metadata, _portable_lstat(requested_run_path)
            ):
                raise ValueError("attempt promotion run alias changed during acquisition")
            filesystem_run_path = _posix_stable_run_path(
                lease_descriptor,
                run_identity,
            )
            yield _StableCoordinationBinding(
                requested_run_path=requested_run_path,
                canonical_run_path=canonical_run_path,
                filesystem_run_path=filesystem_run_path,
                run_identity=run_identity,
                run_reference=lease_descriptor,
            )
        finally:
            fcntl.flock(lease_descriptor, fcntl.LOCK_UN)
    finally:
        os.close(lease_descriptor)


@contextmanager
def attempt_promotion_lease(
    run_dir: Path,
    *,
    expected_run_identity: tuple[int, int] | None = None,
) -> Iterator[Path]:
    """Serialize selection and final publication without following links."""

    intended_run_identity = expected_run_identity or promotion_run_identity(run_dir)
    with _store_lock(run_dir):
        with _stable_coordination_lease(run_dir) as stable_lease:
            if stable_lease.run_identity != intended_run_identity:
                raise ValueError(
                    "attempt selection run directory changed before lease entry"
                )
            with _open_control_directory_from_stable_lease(
                stable_lease,
                create=True,
            ) as directories:
                assert directories is not None
                run, _ = directories
                opened_run_metadata = (
                    _portable_lstat(run) if isinstance(run, Path) else os.fstat(run)
                )
                opened_run_identity = (
                    opened_run_metadata.st_dev,
                    opened_run_metadata.st_ino,
                )
                if opened_run_identity != intended_run_identity:
                    raise ValueError(
                        "attempt selection run directory changed before lease entry"
                    )
                requested_run_path = stable_lease.requested_run_path
                canonical_run_path = stable_lease.canonical_run_path
                canonical_metadata = _portable_lstat(canonical_run_path)
                _validate_directory(canonical_metadata)
                if (
                    canonical_metadata.st_dev,
                    canonical_metadata.st_ino,
                ) != intended_run_identity:
                    raise ValueError(
                        "attempt selection run directory changed before lease binding"
                    )
                binding = _PromotionLeaseBinding(
                    requested_run_path=requested_run_path,
                    canonical_run_path=canonical_run_path,
                    filesystem_run_path=stable_lease.filesystem_run_path,
                    run_identity=intended_run_identity,
                    run_reference=(run if isinstance(run, int) else canonical_run_path),
                    control_reference=(
                        directories[1]
                        if isinstance(directories[1], int)
                        else canonical_run_path / _CONTROL_DIRECTORY_NAME
                    ),
                )
                token = _ACTIVE_PROMOTION_LEASE.set(binding)
                try:
                    yield active_promotion_run_path(requested_run_path)
                finally:
                    binding.close()
                    _ACTIVE_PROMOTION_LEASE.reset(token)


def _safe_relative_path(value: str, *, label: str) -> Path:
    path = Path(value)
    if path.is_absolute() or not value.strip() or ".." in path.parts:
        raise ValueError(f"{label} must be an attempt-relative path")
    normalized = Path(os.path.normpath(value))
    if normalized == Path(".") or ".." in normalized.parts:
        raise ValueError(f"{label} must be an attempt-relative path")
    return normalized


def is_browser_preview_resource_path(value: str) -> bool:
    """Return whether a candidate dependency is safe to expose for preview."""

    return Path(value).suffix.lower() in _BROWSER_PREVIEW_RESOURCE_SUFFIXES


def _require_within(path: Path, root: Path, *, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} escapes its allowed root") from exc
    return resolved


def _copy_snapshot_member(
    *,
    attempt_dir: Path,
    snapshot_dir: Path,
    relative_path: str,
) -> Path:
    relative = _safe_relative_path(relative_path, label="candidate member")
    source = _require_within(
        attempt_dir / relative,
        attempt_dir,
        label="candidate member",
    )
    if not source.is_file():
        raise ValueError(f"candidate member is missing: {relative.as_posix()}")
    target = snapshot_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def _dependency_fingerprint(paths: Sequence[Path], *, root: Path) -> str:
    digest = hashlib.sha256()
    ordered = sorted(paths, key=lambda path: path.relative_to(root).as_posix())
    for path in ordered:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid candidate JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"candidate JSON must contain an object: {path}")
    return payload


def _load_manifest(path: Path, *, run_dir: Path) -> AttemptCandidate:
    try:
        candidate = AttemptCandidate.model_validate(_read_json(path))
    except ValidationError as exc:
        raise ValueError(f"invalid candidate manifest: {path}") from exc
    if candidate.run_id != run_dir.name:
        raise ValueError("candidate manifest run_id does not match its run directory")
    _validate_candidate_integrity(run_dir, candidate)
    return candidate


def _candidate_paths(
    run_dir: Path,
    candidate: AttemptCandidate,
) -> tuple[Path, list[Path], list[Path], Path]:
    source = _require_within(
        run_dir / candidate.source_relative_path,
        run_dir,
        label="candidate source",
    )
    dependencies = [
        _require_within(
            run_dir / relative,
            run_dir,
            label="candidate dependency",
        )
        for relative in candidate.dependency_relative_paths
    ]
    previews = [
        _require_within(
            run_dir / relative,
            run_dir,
            label="candidate preview",
        )
        for relative in candidate.preview_relative_paths
    ]
    validation = _require_within(
        run_dir / candidate.validation_summary_relative_path,
        run_dir,
        label="candidate validation summary",
    )
    return source, dependencies, previews, validation


def _validate_candidate_integrity(
    run_dir: Path,
    candidate: AttemptCandidate,
) -> None:
    source, dependencies, previews, validation = _candidate_paths(run_dir, candidate)
    browser_resources = candidate.browser_resource_relative_paths
    if browser_resources is not None and not set(browser_resources).issubset(
        candidate.dependency_relative_paths
    ):
        raise ValueError(
            "candidate integrity check failed: browser resource is outside the "
            "dependency closure"
        )
    if browser_resources is not None and any(
        not is_browser_preview_resource_path(path)
        for path in browser_resources
    ):
        raise ValueError(
            "candidate integrity check failed: browser resource is not a static "
            "preview asset"
        )
    required = [source, *dependencies, *previews, validation]
    if not all(path.is_file() for path in required):
        raise ValueError("candidate integrity check failed: snapshot member is missing")
    if sha256_file(source) != candidate.source_sha256:
        raise ValueError("candidate integrity check failed: source hash mismatch")
    snapshot_root = next(
        (parent for parent in (source, *source.parents) if parent.name == "candidate"),
        None,
    )
    if snapshot_root is None:
        raise ValueError("candidate integrity check failed: snapshot root is missing")
    if (
        _dependency_fingerprint(dependencies, root=snapshot_root)
        != candidate.dependency_fingerprint
    ):
        raise ValueError("candidate integrity check failed: dependency hash mismatch")


def _candidate_manifest_paths(run_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for manifest in run_dir.glob("*_author/attempt_*/attempt_candidate.json"):
        if manifest.is_file():
            paths.append(manifest)
    return sorted(paths)


def _index_from_candidates(
    run_dir: Path,
    entries: Sequence[tuple[Path, AttemptCandidate]],
) -> AttemptCandidateIndex:
    ordered = sorted(entries, key=lambda item: item[1].attempt)
    return AttemptCandidateIndex(
        run_id=run_dir.name,
        candidate_ids=[candidate.candidate_id for _, candidate in ordered],
        manifest_relative_paths=[
            path.relative_to(run_dir).as_posix() for path, _ in ordered
        ],
        updated_at=_now_iso(),
    )


def rebuild_candidate_index(run_dir: Path) -> AttemptCandidateIndex:
    run_dir = run_dir.resolve()
    entries: list[tuple[Path, AttemptCandidate]] = []
    for manifest_path in _candidate_manifest_paths(run_dir):
        try:
            candidate = _load_manifest(manifest_path, run_dir=run_dir)
        except ValueError:
            continue
        entries.append((manifest_path, candidate))
    index = _index_from_candidates(run_dir, entries)
    atomic_write_json(run_dir / _INDEX_RELATIVE_PATH, index.model_dump(mode="json"))
    return index


def load_candidate_index(run_dir: Path) -> AttemptCandidateIndex | None:
    run_dir = run_dir.resolve()
    path = run_dir / _INDEX_RELATIVE_PATH
    if not path.is_file():
        return None
    try:
        index = AttemptCandidateIndex.model_validate(_read_json(path))
    except ValidationError as exc:
        raise ValueError("invalid attempt candidate index") from exc
    if index.run_id != run_dir.name:
        raise ValueError("attempt candidate index run_id mismatch")
    return index


def _existing_candidate(attempt_dir: Path, run_dir: Path) -> AttemptCandidate | None:
    manifest_path = attempt_dir / "attempt_candidate.json"
    if not manifest_path.is_file():
        return None
    return _load_manifest(manifest_path, run_dir=run_dir)


def _current_input_fingerprint(
    *,
    attempt_dir: Path,
    source_path: str,
    dependency_paths: Sequence[str],
) -> tuple[str, str]:
    source_relative = _safe_relative_path(source_path, label="source_path")
    source = _require_within(
        attempt_dir / source_relative,
        attempt_dir,
        label="source_path",
    )
    if not source.is_file():
        raise ValueError(f"candidate source is missing: {source_relative.as_posix()}")
    dependencies = []
    for value in dependency_paths:
        relative = _safe_relative_path(value, label="dependency_path")
        dependency = _require_within(
            attempt_dir / relative,
            attempt_dir,
            label="dependency_path",
        )
        if not dependency.is_file():
            raise ValueError(f"candidate dependency is missing: {relative.as_posix()}")
        dependencies.append(dependency)
    return (
        sha256_file(source),
        _dependency_fingerprint(dependencies, root=attempt_dir),
    )


def capture_attempt_candidate(
    *,
    run_dir: Path,
    attempt_dir: Path,
    artifact_type: Literal["poster", "deck", "landing", "video"],
    attempt: int,
    max_attempts: int,
    source_path: str,
    dependency_paths: Sequence[str],
    preview_paths: Sequence[str],
    validation_summary_path: str,
    safety_state: AttemptSafetyState,
    hard_blockers: Sequence[AttemptIssue],
    warnings: Sequence[AttemptIssue],
    browser_resource_paths: Sequence[str] = (),
    previous_candidate_id: str | None = None,
    repair_source_attempt: int | None = None,
) -> AttemptCandidate:
    run_dir = run_dir.resolve()
    attempt_dir = _require_within(
        attempt_dir,
        run_dir,
        label="attempt directory",
    )
    if attempt < 1 or max_attempts < 1:
        raise ValueError("attempt and max_attempts must be positive")
    dependency_path_set = {
        _safe_relative_path(value, label="dependency_path").as_posix()
        for value in dependency_paths
    }
    browser_resource_path_set = {
        _safe_relative_path(value, label="browser resource path").as_posix()
        for value in browser_resource_paths
    }
    if not browser_resource_path_set.issubset(dependency_path_set):
        raise ValueError("browser resource path must be in the dependency closure")
    if any(
        not is_browser_preview_resource_path(value)
        for value in browser_resource_path_set
    ):
        raise ValueError("browser resource path must be a static preview asset")
    source_sha, input_dependency_fingerprint = _current_input_fingerprint(
        attempt_dir=attempt_dir,
        source_path=source_path,
        dependency_paths=dependency_paths,
    )
    with _store_lock(run_dir):
        existing = _existing_candidate(attempt_dir, run_dir)
        if existing is not None:
            if (
                existing.source_sha256 == source_sha
                and existing.dependency_fingerprint == input_dependency_fingerprint
            ):
                return existing
            raise ValueError("attempt candidate is immutable once captured")

        snapshot_dir = attempt_dir / "candidate"
        if snapshot_dir.exists():
            raise ValueError("candidate snapshot exists without a valid immutable manifest")
        snapshot_dir.mkdir(parents=True)
        try:
            copied_source = _copy_snapshot_member(
                attempt_dir=attempt_dir,
                snapshot_dir=snapshot_dir,
                relative_path=source_path,
            )
            copied_dependencies = [
                _copy_snapshot_member(
                    attempt_dir=attempt_dir,
                    snapshot_dir=snapshot_dir,
                    relative_path=value,
                )
                for value in dependency_paths
            ]
            copied_previews = [
                _copy_snapshot_member(
                    attempt_dir=attempt_dir,
                    snapshot_dir=snapshot_dir,
                    relative_path=value,
                )
                for value in preview_paths
            ]
            copied_validation = _copy_snapshot_member(
                attempt_dir=attempt_dir,
                snapshot_dir=snapshot_dir,
                relative_path=validation_summary_path,
            )
            dependency_fingerprint = _dependency_fingerprint(
                copied_dependencies,
                root=snapshot_dir,
            )
            if dependency_fingerprint != input_dependency_fingerprint:
                raise ValueError("candidate dependencies changed during snapshot capture")
            copied_source_sha = sha256_file(copied_source)
            if copied_source_sha != source_sha:
                raise ValueError("candidate source changed during snapshot capture")
            candidate_id = (
                f"{artifact_type}-attempt-{attempt:02d}-{copied_source_sha[:12]}"
            )
            candidate = AttemptCandidate(
                candidate_id=candidate_id,
                run_id=run_dir.name,
                artifact_type=ArtifactType(artifact_type),
                attempt=attempt,
                max_attempts=max_attempts,
                created_at=_now_iso(),
                source_relative_path=copied_source.relative_to(run_dir).as_posix(),
                preview_relative_paths=[
                    path.relative_to(run_dir).as_posix() for path in copied_previews
                ],
                dependency_relative_paths=[
                    path.relative_to(run_dir).as_posix()
                    for path in copied_dependencies
                ],
                browser_resource_relative_paths=[
                    path.relative_to(run_dir).as_posix()
                    for path in copied_dependencies
                    if path.relative_to(snapshot_dir).as_posix()
                    in browser_resource_path_set
                ],
                source_sha256=copied_source_sha,
                dependency_fingerprint=dependency_fingerprint,
                safety_state=safety_state,
                hard_blockers=list(hard_blockers),
                warnings=list(warnings),
                validation_summary_relative_path=(
                    copied_validation.relative_to(run_dir).as_posix()
                ),
                previous_candidate_id=previous_candidate_id,
                repair_source_attempt=repair_source_attempt,
            )
            manifest_path = attempt_dir / "attempt_candidate.json"
            atomic_write_json(manifest_path, candidate.model_dump(mode="json"))
            rebuild_candidate_index(run_dir)
            return candidate
        except Exception:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            raise


def load_attempt_candidate(run_dir: Path, attempt: int) -> AttemptCandidate:
    for candidate in load_attempt_candidates(run_dir):
        if candidate.attempt == attempt:
            return candidate
    raise ValueError(
        f"attempt candidate not found or failed integrity validation: {attempt}"
    )


def load_attempt_candidates(run_dir: Path) -> list[AttemptCandidate]:
    run_dir = run_dir.resolve()
    index = load_candidate_index(run_dir)
    if index is None:
        raise ValueError("attempt candidate index is missing")
    candidates: list[AttemptCandidate] = []
    for relative in index.manifest_relative_paths:
        manifest_path = _require_within(
            run_dir / relative,
            run_dir,
            label="candidate manifest",
        )
        try:
            candidates.append(_load_manifest(manifest_path, run_dir=run_dir))
        except ValueError:
            continue
    return candidates


def candidate_summary(candidate: AttemptCandidate) -> dict[str, Any]:
    return candidate.model_dump(mode="json")


def write_selection_journal(
    run_dir: Path,
    journal: AttemptSelectionJournal,
) -> None:
    with _opened_control_context(run_dir, create=True) as context:
        assert context is not None
        if journal.run_id != context.logical_run_id:
            raise ValueError("selection journal run_id mismatch")
        with _store_lock_for_identity(context.run_identity):
            context.write_json(
                _SELECTION_RELATIVE_PATH.name,
                journal.model_dump(mode="json"),
            )


def load_selection_journal(run_dir: Path) -> AttemptSelectionJournal | None:
    with _opened_control_context(run_dir, create=False) as context:
        if context is None:
            return None
        payload = context.read_json(_SELECTION_RELATIVE_PATH.name)
        if payload is None:
            return None
        try:
            journal = AttemptSelectionJournal.model_validate(payload)
        except ValidationError as exc:
            raise ValueError("invalid attempt selection journal") from exc
        if journal.run_id != context.logical_run_id:
            raise ValueError("selection journal run_id mismatch")
        return journal


def write_selection_adapter_transaction(
    run_dir: Path,
    payload: dict[str, Any],
) -> None:
    with _opened_control_context(run_dir, create=True) as context:
        assert context is not None
        with _store_lock_for_identity(context.run_identity):
            context.write_json(_SELECTION_TRANSACTION_NAME, payload)


def load_selection_adapter_transaction(run_dir: Path) -> dict[str, Any] | None:
    with _opened_control_context(run_dir, create=False) as context:
        if context is None:
            return None
        with _store_lock_for_identity(context.run_identity):
            return context.read_json(_SELECTION_TRANSACTION_NAME)


def clear_selection_adapter_transaction(run_dir: Path) -> None:
    with _opened_control_context(run_dir, create=False) as context:
        if context is None:
            return
        with _store_lock_for_identity(context.run_identity):
            context.delete_file(_SELECTION_TRANSACTION_NAME)
