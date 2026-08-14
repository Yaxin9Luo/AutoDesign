"""Canonical, run-scoped access to local run files."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
from typing import BinaryIO
from urllib.parse import unquote


INTERNAL_RUN_FILE_NAMES = frozenset({
    "cancel_snapshot.json",
    "derived_job.json",
    "process_ledger.json",
    "run_control.json",
    "run_events.jsonl",
    "worker_events.jsonl",
    "worker_result.json",
    "worker_stderr.log",
    "worker_stdout.log",
})

_INTERNAL_RUN_FILE_NAMES_CASEFOLDED = frozenset(
    name.casefold() for name in INTERNAL_RUN_FILE_NAMES
)
_WINDOWS_RESERVED_BASENAMES = frozenset({
    "aux",
    "clock$",
    "con",
    "conin$",
    "conout$",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
    "com¹",
    "com²",
    "com³",
    "lpt¹",
    "lpt²",
    "lpt³",
})
_WINDOWS_SHORT_NAME = re.compile(
    r"^[^. ~]{1,6}~[0-9]{1,6}(?:\.[^. ]{0,3})?$",
    re.IGNORECASE,
)
_ENCODED_SEPARATOR = re.compile(r"%(?:2f|5c)", re.IGNORECASE)


class RunFileAccessError(ValueError):
    """A requested run file cannot be authorized and opened safely."""


@dataclass(frozen=True)
class OpenedRunFile:
    path: Path
    handle: BinaryIO
    stat_result: os.stat_result

    def close(self) -> None:
        self.handle.close()

    def __enter__(self) -> "OpenedRunFile":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def canonical_run_file_parts(
    relative_path: str,
    *,
    expected_run_id: str | None = None,
) -> tuple[str, ...]:
    decoded_path = str(relative_path)
    for _ in range(4):
        if _ENCODED_SEPARATOR.search(decoded_path):
            raise RunFileAccessError("run file path contains an encoded separator")
        decoded = unquote(decoded_path)
        if decoded == decoded_path:
            break
        decoded_path = decoded
    else:
        raise RunFileAccessError("run file path is over-encoded")
    parts = tuple(part for part in decoded_path.split("/") if part)
    if len(parts) < 2:
        raise RunFileAccessError("run file path is incomplete")
    if expected_run_id is not None and parts[0] != expected_run_id:
        raise RunFileAccessError("run file is outside the authorized source run")
    for component in parts:
        folded = component.casefold()
        basename = folded.split(".", 1)[0].rstrip(" .")
        if (
            component in {".", ".."}
            or "\\" in component
            or ":" in component
            or component.endswith((".", " "))
            or any(ord(character) < 32 for character in component)
            or basename in _WINDOWS_RESERVED_BASENAMES
            or _WINDOWS_SHORT_NAME.fullmatch(component) is not None
        ):
            raise RunFileAccessError("run file path contains a non-canonical component")
    for component in parts[1:]:
        folded = component.casefold()
        if (
            folded in _INTERNAL_RUN_FILE_NAMES_CASEFOLDED
            or component.startswith(".")
            or folded.endswith(".partial")
            or folded.endswith(".tmp")
        ):
            raise RunFileAccessError("run file is internal")
    return parts


def open_run_file(
    runs_dir: Path,
    relative_path: str,
    *,
    expected_run_id: str | None = None,
) -> OpenedRunFile:
    parts = canonical_run_file_parts(
        relative_path,
        expected_run_id=expected_run_id,
    )
    root = Path(runs_dir).resolve()
    secure_dir_fd = (
        os.name == "posix"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
    )
    if secure_dir_fd:
        return _open_posix(root, parts)
    return _open_portable(root, parts)


def _validate_opened_file(descriptor: int) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RunFileAccessError("run file is not a private regular file")
    return metadata


def _open_posix(root: Path, parts: tuple[str, ...]) -> OpenedRunFile:
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    directories: list[int] = []
    file_descriptor: int | None = None
    try:
        directory = os.open(root, directory_flags)
        directories.append(directory)
        for component in parts[:-1]:
            directory = os.open(component, directory_flags, dir_fd=directory)
            directories.append(directory)
        file_descriptor = os.open(parts[-1], file_flags, dir_fd=directory)
        metadata = _validate_opened_file(file_descriptor)
        handle = os.fdopen(file_descriptor, "rb")
        file_descriptor = None
        return OpenedRunFile(root.joinpath(*parts), handle, metadata)
    except RunFileAccessError:
        raise
    except OSError as exc:
        raise RunFileAccessError("run file is unavailable") from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        for directory in reversed(directories):
            os.close(directory)


def _portable_component_stat(path: Path) -> os.stat_result:
    metadata = os.lstat(path)
    is_junction = getattr(path, "is_junction", lambda: False)()
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or is_junction
        or getattr(metadata, "st_file_attributes", 0) & reparse_attribute
    ):
        raise RunFileAccessError("run file path contains a link or reparse point")
    return metadata


def _open_portable(root: Path, parts: tuple[str, ...]) -> OpenedRunFile:
    snapshots: list[tuple[Path, os.stat_result]] = []
    handle: BinaryIO | None = None
    current = root
    try:
        root_metadata = _portable_component_stat(root)
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise RunFileAccessError("runs root is unavailable")
        snapshots.append((root, root_metadata))
        for index, component in enumerate(parts):
            current = current / component
            metadata = _portable_component_stat(current)
            if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
                raise RunFileAccessError("run file path contains a non-directory")
            snapshots.append((current, metadata))
        handle = current.open("rb")
        opened = _validate_opened_file(handle.fileno())
        expected = snapshots[-1][1]
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise RunFileAccessError("run file changed while it was opened")
        for path, before in snapshots:
            after = _portable_component_stat(path)
            if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
                raise RunFileAccessError("run file path changed while it was opened")
        return OpenedRunFile(root.joinpath(*parts), handle, opened)
    except RunFileAccessError:
        if handle is not None:
            handle.close()
        raise
    except OSError as exc:
        if handle is not None:
            handle.close()
        raise RunFileAccessError("run file is unavailable") from exc
