"""Crash-safe final-directory promotion for externally authored artifacts."""

from __future__ import annotations

from collections.abc import Callable
import errno
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from ..attempt_candidates import (
    assert_promotion_run_unchanged,
    is_active_promotion_filesystem_root,
)
from ..run_control import durable_replace_json


_ARTIFACT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_JOURNAL_VERSION = 1
_TRANSACTION_OWNER = "autodesign.atomic_artifact_promotion.v1"
_UNCOMMITTED_PHASES = frozenset(
    {"prepared", "backup_created", "final_installed"}
)
_ROLLBACK_PHASES = frozenset({"rollback_started", "rolled_back"})
_VALID_PHASES = _UNCOMMITTED_PHASES | _ROLLBACK_PHASES | {"committed"}


def publish_artifact_directory(
    staging_dir: Path,
    final_dir: Path,
    *,
    artifact_name: str,
    post_publish: Callable[[], None],
) -> None:
    """Replace ``final_dir`` while retaining rollback state for run reconciliation."""

    assert_promotion_run_unchanged()
    paths = _promotion_paths(final_dir, artifact_name)
    _validate_promotion_root(final_dir, paths=paths)
    if not _same_filesystem_path(staging_dir.parent, final_dir.parent):
        raise ValueError("artifact staging and final directories must share a parent")
    if (
        Path(staging_dir.name).name != staging_dir.name
        or not staging_dir.name.startswith(paths.staging_prefix)
        or not staging_dir.is_dir()
        or staging_dir.is_symlink()
    ):
        raise ValueError("invalid artifact staging directory")
    _validate_promotion_entries(
        final_dir=final_dir,
        staging_dir=staging_dir,
        backup_dir=None,
        journal_path=paths.journal_path,
    )

    recover_artifact_promotion(final_dir, artifact_name=artifact_name)
    assert_promotion_run_unchanged()
    if paths.journal_path.exists() or paths.journal_path.is_symlink():
        raise RuntimeError(
            f"{artifact_name} promotion is awaiting global run reconciliation"
        )
    _validate_promotion_entries(
        final_dir=final_dir,
        staging_dir=staging_dir,
        backup_dir=None,
        journal_path=paths.journal_path,
    )
    backup_dir: Path | None = None
    if _path_exists(final_dir):
        assert_promotion_run_unchanged()
        created_backup_dir = Path(
            tempfile.mkdtemp(prefix=paths.backup_prefix, dir=final_dir.parent)
        )
        try:
            assert_promotion_run_unchanged()
            created_backup_dir.rmdir()
        except BaseException:
            shutil.rmtree(created_backup_dir, ignore_errors=True)
            raise
        backup_dir = created_backup_dir
    journal: dict[str, Any] = {
        "version": _JOURNAL_VERSION,
        "transaction_owner": _TRANSACTION_OWNER,
        "phase": "prepared",
        "final_name": final_dir.name,
        "backup_name": backup_dir.name if backup_dir is not None else "",
        "staging_name": staging_dir.name,
    }
    _validate_promotion_entries(
        final_dir=final_dir,
        staging_dir=staging_dir,
        backup_dir=backup_dir,
        journal_path=paths.journal_path,
    )
    _validate_promotion_root(final_dir, paths=paths)
    try:
        assert_promotion_run_unchanged()
        durable_replace_json(paths.journal_path, journal)
        assert_promotion_run_unchanged()
        if backup_dir is not None:
            _validate_promotion_root(final_dir, paths=paths)
            _validate_promotion_entries(
                final_dir=final_dir,
                staging_dir=staging_dir,
                backup_dir=backup_dir,
                journal_path=paths.journal_path,
            )
            assert_promotion_run_unchanged()
            _replace_path(final_dir, backup_dir)
            assert_promotion_run_unchanged()
            journal["phase"] = "backup_created"
            durable_replace_json(paths.journal_path, journal)
            assert_promotion_run_unchanged()
        _validate_promotion_root(final_dir, paths=paths)
        _validate_promotion_entries(
            final_dir=final_dir,
            staging_dir=staging_dir,
            backup_dir=backup_dir,
            journal_path=paths.journal_path,
        )
        assert_promotion_run_unchanged()
        _replace_path(staging_dir, final_dir)
        assert_promotion_run_unchanged()
        journal["phase"] = "final_installed"
        durable_replace_json(paths.journal_path, journal)
        assert_promotion_run_unchanged()
        post_publish()
        assert_promotion_run_unchanged()
        _validate_promotion_root(final_dir, paths=paths)
        journal["phase"] = "committed"
        durable_replace_json(paths.journal_path, journal)
        assert_promotion_run_unchanged()
    except BaseException:
        _rollback_promotion(
            final_dir=final_dir,
            staging_dir=staging_dir,
            backup_dir=backup_dir,
            journal_path=paths.journal_path,
            journal=journal,
        )
        raise

    # ``committed`` is the adapter-local linearization point.  The run
    # supervisor still owns the global completed-vs-cancelled decision, so the
    # journal and previous final remain available until it reconciles them.


def reconcile_artifact_promotion(
    final_dir: Path,
    *,
    artifact_name: str,
    accept: bool,
) -> None:
    """Accept or reject an adapter-local commit after the run terminal CAS."""

    paths = _promotion_paths(final_dir, artifact_name)
    _validate_promotion_root(final_dir, paths=paths)
    if not paths.journal_path.is_file() or paths.journal_path.is_symlink():
        if paths.journal_path.is_symlink():
            raise ValueError("invalid artifact promotion journal path")
        return
    journal = _read_journal(paths.journal_path)
    staging_dir, backup_dir, phase, trusted = _validated_journal(
        journal,
        final_dir=final_dir,
        paths=paths,
    )
    if not trusted:
        raise ValueError("cannot reconcile an untrusted artifact promotion journal")
    _validate_promotion_entries(
        final_dir=final_dir,
        staging_dir=staging_dir,
        backup_dir=backup_dir,
        journal_path=paths.journal_path,
    )
    if accept and phase == "committed":
        if not _path_exists(final_dir):
            raise ValueError("committed final is missing; recovery evidence retained")
        _remove_path(backup_dir)
        _remove_path(staging_dir)
        _fsync_directory(final_dir.parent)
        paths.journal_path.unlink(missing_ok=True)
        _fsync_directory(final_dir.parent)
        return
    if accept:
        raise ValueError("cannot accept an uncommitted artifact promotion")

    _complete_rollback(
        final_dir=final_dir,
        staging_dir=staging_dir,
        backup_dir=backup_dir,
        journal_path=paths.journal_path,
        journal=journal,
        phase=phase,
    )


def recover_artifact_promotion(
    final_dir: Path,
    *,
    artifact_name: str,
    trust_legacy_journal: bool = False,
) -> None:
    """Resolve an interrupted final-directory transaction without publishing a draft."""

    paths = _promotion_paths(final_dir, artifact_name)
    _validate_promotion_root(final_dir, paths=paths)
    if paths.journal_path.is_symlink():
        raise ValueError("invalid artifact promotion journal path")
    if not paths.journal_path.is_file():
        return
    journal = _read_journal(paths.journal_path)
    staging_dir, backup_dir, phase, trusted = _validated_journal(
        journal,
        final_dir=final_dir,
        paths=paths,
    )
    backup_exists = _path_exists(backup_dir)
    if not trusted and not trust_legacy_journal:
        _validate_promotion_entries(
            final_dir=final_dir,
            staging_dir=staging_dir,
            backup_dir=backup_dir,
            journal_path=paths.journal_path,
            allow_staging_symlink=True,
        )
        _remove_path(staging_dir)
        if backup_exists:
            raise ValueError(
                f"{artifact_name} validated backup retained for manual recovery: "
                f"{backup_dir.name}"
            )
        paths.journal_path.unlink(missing_ok=True)
        return
    _validate_promotion_entries(
        final_dir=final_dir,
        staging_dir=staging_dir,
        backup_dir=backup_dir,
        journal_path=paths.journal_path,
    )

    if phase == "committed" and trusted:
        _remove_path(staging_dir)
        return
    if phase == "committed":
        if not _path_exists(final_dir) and backup_exists and backup_dir is not None:
            _replace_path(backup_dir, final_dir)
            _fsync_restored_final(final_dir)
        elif backup_exists:
            _remove_path(backup_dir)
        _remove_path(staging_dir)
        _fsync_directory(final_dir.parent)
        paths.journal_path.unlink(missing_ok=True)
        _fsync_directory(final_dir.parent)
        return

    _complete_rollback(
        final_dir=final_dir,
        staging_dir=staging_dir,
        backup_dir=backup_dir,
        journal_path=paths.journal_path,
        journal=journal,
        phase=phase,
    )


class _PromotionPaths:
    def __init__(self, final_dir: Path, artifact_name: str) -> None:
        self.journal_path = (
            final_dir.parent / f".{artifact_name}-final-promotion.json"
        )
        self.staging_prefix = f".{artifact_name}-final-staging-"
        self.backup_prefix = f".{artifact_name}-final-backup-"


def _promotion_paths(final_dir: Path, artifact_name: str) -> _PromotionPaths:
    if not _ARTIFACT_NAME_RE.fullmatch(artifact_name):
        raise ValueError("invalid artifact promotion name")
    if Path(final_dir.name).name != final_dir.name or not final_dir.name:
        raise ValueError("invalid artifact final directory")
    return _PromotionPaths(final_dir, artifact_name)


def _read_journal(journal_path: Path) -> dict[str, Any]:
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("invalid artifact promotion journal") from exc
    if not isinstance(journal, dict):
        raise ValueError("invalid artifact promotion journal")
    return journal


def _validate_promotion_root(
    final_dir: Path,
    *,
    paths: _PromotionPaths,
) -> None:
    parent = final_dir.parent
    stable_lease_root = is_active_promotion_filesystem_root(parent)
    if (parent.is_symlink() and not stable_lease_root) or not parent.is_dir():
        raise ValueError("artifact promotion parent must be a real directory")
    if not _same_filesystem_path(paths.journal_path.parent, parent):
        raise ValueError("artifact promotion journal escaped its parent")


def _validate_promotion_entries(
    *,
    final_dir: Path,
    staging_dir: Path,
    backup_dir: Path | None,
    journal_path: Path,
    allow_staging_symlink: bool = False,
) -> None:
    parent = final_dir.parent
    for path, label in (
        (final_dir, "final"),
        (staging_dir, "staging"),
        (backup_dir, "backup"),
        (journal_path, "journal"),
    ):
        if path is None:
            continue
        if not _same_filesystem_path(path.parent, parent):
            raise ValueError(f"artifact promotion {label} escaped its parent")
        if path.is_symlink() and not (
            allow_staging_symlink and label == "staging"
        ):
            raise ValueError(f"artifact promotion {label} must not be a symlink")


def _validated_journal(
    journal: Any,
    *,
    final_dir: Path,
    paths: _PromotionPaths,
) -> tuple[Path, Path | None, str, bool]:
    if not isinstance(journal, dict) or journal.get("version") != _JOURNAL_VERSION:
        raise ValueError("invalid promotion journal")
    if journal.get("final_name") != final_dir.name:
        raise ValueError("invalid promotion journal final name")
    staging_name = str(journal.get("staging_name") or "")
    backup_name = str(journal.get("backup_name") or "")
    if (
        Path(staging_name).name != staging_name
        or not staging_name.startswith(paths.staging_prefix)
    ):
        raise ValueError("invalid promotion journal staging name")
    if backup_name and (
        Path(backup_name).name != backup_name
        or not backup_name.startswith(paths.backup_prefix)
    ):
        raise ValueError("invalid promotion journal backup name")
    phase = str(journal.get("phase") or "")
    if phase not in _VALID_PHASES:
        raise ValueError("invalid promotion journal phase")
    return (
        final_dir.parent / staging_name,
        final_dir.parent / backup_name if backup_name else None,
        phase,
        journal.get("transaction_owner") == _TRANSACTION_OWNER,
    )


def _rollback_promotion(
    *,
    final_dir: Path,
    staging_dir: Path,
    backup_dir: Path | None,
    journal_path: Path,
    journal: dict[str, Any],
) -> None:
    _complete_rollback(
        final_dir=final_dir,
        staging_dir=staging_dir,
        backup_dir=backup_dir,
        journal_path=journal_path,
        journal=journal,
        phase=str(journal.get("phase") or ""),
    )


def _complete_rollback(
    *,
    final_dir: Path,
    staging_dir: Path,
    backup_dir: Path | None,
    journal_path: Path,
    journal: dict[str, Any],
    phase: str,
) -> None:
    """Durably restore the previous final and remove transaction residue."""

    replacement = backup_dir is not None
    if phase not in _ROLLBACK_PHASES:
        if (
            replacement
            and phase in {"backup_created", "final_installed", "committed"}
            and not _path_exists(backup_dir)
        ):
            raise ValueError(
                "required backup is missing; promotion journal retained"
            )
        journal["phase"] = "rollback_started"
        durable_replace_json(journal_path, journal)
        phase = "rollback_started"

    if phase == "rollback_started":
        if replacement:
            if _path_exists(backup_dir):
                _remove_path(final_dir)
                _fsync_directory(final_dir.parent)
                assert backup_dir is not None
                _replace_path(backup_dir, final_dir)
                _fsync_restored_final(final_dir)
            elif not _path_exists(final_dir):
                raise ValueError(
                    "required backup is missing before restoration; "
                    "promotion journal retained"
                )
            else:
                _fsync_restored_final(final_dir)
        else:
            _remove_path(final_dir)
            _fsync_directory(final_dir.parent)
        journal["phase"] = "rolled_back"
        durable_replace_json(journal_path, journal)
        phase = "rolled_back"

    if phase != "rolled_back":
        raise ValueError("invalid artifact promotion rollback phase")
    if replacement and not _path_exists(final_dir):
        raise ValueError(
            "restored final is missing; promotion journal retained"
        )
    if not replacement:
        _remove_path(final_dir)
    _remove_path(staging_dir)
    _remove_path(backup_dir)
    _fsync_directory(final_dir.parent)
    journal_path.unlink(missing_ok=True)
    _fsync_directory(final_dir.parent)


_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = frozenset(
    {
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
)


def _fsync_restored_final(final_dir: Path) -> None:
    _fsync_directory(final_dir)
    _fsync_directory(final_dir.parent)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            return
        raise
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            raise
    finally:
        os.close(descriptor)


def _path_exists(path: Path | None) -> bool:
    return path is not None and (path.exists() or path.is_symlink())


def _same_filesystem_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(os.fspath(left))) == os.path.normcase(
        os.path.abspath(os.fspath(right))
    )


def _remove_path(path: Path | None) -> None:
    if path is None:
        return
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _replace_path(source: Path, destination: Path) -> None:
    os.replace(source, destination)
