from __future__ import annotations

import errno
from copy import deepcopy
import json
import multiprocessing
import os
from pathlib import Path
import stat
import tempfile
import threading
from contextlib import contextmanager, ExitStack
from types import SimpleNamespace
import unittest
from unittest import mock

import autodesign.design_spec_persistence as persistence
from autodesign.schema import DesignSpec
from autodesign.tools import ToolContext
from autodesign.tools.propose_design_spec import propose_design_spec
from autodesign.util.design_spec_fingerprint import design_spec_sha256


class _FakeWindowsHandle:
    def __init__(self, path: Path) -> None:
        self.path = path
        metadata = os.lstat(path)
        self.descriptor = (
            None if stat_is_reparse(metadata) else os.open(path, os.O_RDONLY)
        )
        self.initial_metadata = metadata
        self.closed = False


def stat_is_reparse(metadata: os.stat_result) -> bool:
    return bool(
        stat.S_ISLNK(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0) & 0x400
    )


def _windows_metadata(metadata: os.stat_result) -> SimpleNamespace:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    if stat.S_ISLNK(metadata.st_mode):
        attributes |= 0x400
    if stat.S_ISDIR(metadata.st_mode):
        attributes |= 0x10
    return SimpleNamespace(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        links=metadata.st_nlink,
        attributes=attributes,
    )


class _FakeWindowsArchiveIO:
    def __init__(self) -> None:
        self.handles: list[_FakeWindowsHandle] = []
        self.flushed_parents: list[Path] = []
        self.on_read = None
        self.on_stat_path = None

    def open_file(
        self,
        path: Path,
        *,
        delete_access: bool = False,
    ) -> _FakeWindowsHandle:
        del delete_access
        handle = _FakeWindowsHandle(path)
        self.handles.append(handle)
        return handle

    def metadata(self, handle: _FakeWindowsHandle) -> SimpleNamespace:
        metadata = (
            handle.initial_metadata
            if handle.descriptor is None
            else os.fstat(handle.descriptor)
        )
        return _windows_metadata(metadata)

    def read_bytes(self, handle: _FakeWindowsHandle) -> bytes:
        if self.on_read is not None:
            callback, self.on_read = self.on_read, None
            callback()
        if handle.descriptor is None:
            raise OSError("cannot read a reparse point")
        os.lseek(handle.descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while chunk := os.read(handle.descriptor, 1 << 20):
            chunks.append(chunk)
        return b"".join(chunks)

    def stat_path(self, path: Path) -> SimpleNamespace:
        if self.on_stat_path is not None:
            callback, self.on_stat_path = self.on_stat_path, None
            callback()
        return _windows_metadata(os.lstat(path))

    def open_lock(self, path: Path) -> _FakeWindowsHandle:
        path.touch(exist_ok=True)
        return self.open_file(path)

    def lock(self, _handle: _FakeWindowsHandle) -> None:
        return

    def unlock(self, _handle: _FakeWindowsHandle) -> None:
        return

    def mark_delete(self, handle: _FakeWindowsHandle) -> None:
        current = os.lstat(handle.path)
        opened = self.metadata(handle)
        if (current.st_dev, current.st_ino) == (opened.device, opened.inode):
            os.unlink(handle.path)

    def close(self, handle: _FakeWindowsHandle) -> None:
        if handle.closed:
            raise AssertionError("Windows archive handle closed twice")
        handle.closed = True
        if handle.descriptor is not None:
            os.close(handle.descriptor)

    def flush_parent(self, parent: Path) -> None:
        self.flushed_parents.append(parent)


def _encoded(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _persist(root: Path, payload: dict[str, object]) -> None:
    persistence.persist_design_spec_payload(
        canonical_path=root / "design_spec.json",
        archive_path=root / "specs" / "design_spec_02.json",
        payload=payload,
        before_archive_publish=lambda _path: None,
    )


def _rollback_orphans(archive: Path) -> list[Path]:
    return sorted(archive.parent.glob(f".{archive.name}.*.rollback-orphan"))


def _video_spec(title: str) -> dict[str, object]:
    return {
        "brief": "Exercise DesignSpec persistence atomicity.",
        "artifact_type": "video",
        "canvas": {"w_px": 1920, "h_px": 1080},
        "html_artifact": {
            "target": "video",
            "theme": {},
            "frames": [
                {
                    "frame_id": "scene_01",
                    "kind": "scene",
                    "role": "intro",
                    "blocks": [
                        {
                            "block_id": "headline",
                            "kind": "text",
                            "text": title,
                        }
                    ],
                }
            ],
        },
    }


def _fresh_video_context(root: Path) -> ToolContext:
    root.mkdir(parents=True, exist_ok=True)
    layers = root / "layers"
    layers.mkdir(exist_ok=True)
    ctx = ToolContext(
        settings=SimpleNamespace(poster_harness_mode="cheap"),
        run_dir=root,
        layers_dir=layers,
        run_id="design-spec-namespace-atomicity",
    )
    ctx.state.update(
        {
            "artifact_type": "video",
            "rendered_layers": {"retained": {"owner": "prior"}},
            "composition": {"identity": "prior-composition"},
            "sentinel": {"nested": ["caller-state"]},
        }
    )
    return ctx


def _prior_video_context(root: Path) -> tuple[ToolContext, bytes]:
    ctx = _fresh_video_context(root)
    spec = DesignSpec.model_validate(_video_spec("Prior canonical"))
    spec_hash = design_spec_sha256(spec)
    ctx.state.update(
        {
            "design_spec": spec,
            "design_spec_sha256": spec_hash,
            "spec_revision_count": 1,
        }
    )
    prior_bytes = _encoded(
        {
            "artifact_type": "video",
            "is_revision": False,
            "revision": 1,
            "design_spec_sha256": spec_hash,
            "design_spec": spec.model_dump(mode="json"),
        }
    )
    (root / "design_spec.json").write_bytes(prior_bytes)
    return ctx, prior_bytes


def _fd_count() -> int:
    return len(os.listdir("/dev/fd"))


@contextmanager
def _inject_post_quarantine_identity_fault(archive: Path, fault: str):
    real_verify = persistence._verify_posix_entry
    real_fstat = persistence.os.fstat
    real_stat = persistence.os.stat
    real_read = persistence.os.read
    real_supports_dir_fd = persistence.os.supports_dir_fd
    active = False
    retained_descriptor: int | None = None

    def is_quarantine_name(value: object) -> bool:
        name = os.fsdecode(os.fspath(value))
        return (
            name.startswith(f".{archive.name}.")
            and name.endswith(".rollback-orphan")
        )

    def fail_verification_then_classify(
        *,
        directory: int,
        name: str,
        descriptor: int | None,
        publication: persistence._ArchivePublication,
        data: bytes,
        allowed_links: frozenset[int] = frozenset({1}),
    ) -> None:
        nonlocal active, retained_descriptor
        if is_quarantine_name(name):
            active = True
            retained_descriptor = descriptor
            if fault != "read_eio":
                raise OSError(
                    errno.EIO,
                    "injected post-quarantine integrity verification failure",
                )
        real_verify(
            directory=directory,
            name=name,
            descriptor=descriptor,
            publication=publication,
            data=data,
            allowed_links=allowed_links,
        )

    def inject_fstat(descriptor: int) -> os.stat_result:
        metadata = real_fstat(descriptor)
        if not active or descriptor != retained_descriptor:
            return metadata
        if fault == "fstat_eio":
            raise OSError(errno.EIO, "injected retained publication fstat EIO")
        if fault == "publication_identity_mismatch":
            return SimpleNamespace(
                st_mode=metadata.st_mode,
                st_ino=metadata.st_ino + 1,
                st_dev=metadata.st_dev,
                st_nlink=metadata.st_nlink,
                st_size=metadata.st_size,
                st_mtime_ns=metadata.st_mtime_ns,
            )
        return metadata

    def inject_stat(
        path: object,
        *args: object,
        **kwargs: object,
    ) -> os.stat_result:
        if active and is_quarantine_name(path):
            if fault == "stat_eio":
                raise OSError(errno.EIO, "injected quarantine binding stat EIO")
            if fault == "stat_missing":
                raise FileNotFoundError(
                    errno.ENOENT,
                    "injected missing quarantine binding",
                    os.fspath(path),
                )
        return real_stat(path, *args, **kwargs)

    def inject_read(descriptor: int, count: int) -> bytes:
        if (
            active
            and descriptor == retained_descriptor
            and fault == "read_eio"
        ):
            raise OSError(errno.EIO, "injected retained publication read EIO")
        return real_read(descriptor, count)

    stat_mock = mock.Mock(side_effect=inject_stat)
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(
                persistence,
                "_verify_posix_entry",
                side_effect=fail_verification_then_classify,
            )
        )
        stack.enter_context(
            mock.patch.object(persistence.os, "fstat", side_effect=inject_fstat)
        )
        stack.enter_context(mock.patch.object(persistence.os, "stat", stat_mock))
        stack.enter_context(
            mock.patch.object(persistence.os, "read", side_effect=inject_read)
        )
        stack.enter_context(
            mock.patch.object(
                persistence.os,
                "supports_dir_fd",
                frozenset(set(real_supports_dir_fd) | {stat_mock}),
            )
        )
        yield


@contextmanager
def _rebind_quarantine_source_after_classification(
    archive: Path,
    *,
    owner_out: Path,
    foreign_out: Path,
    foreign_bytes: bytes,
    race_phase: str,
    official_bytes: bytes | None = None,
):
    real_classifier = persistence._classify_quarantined_entry_identity_at
    real_no_replace = persistence._rename_no_replace_at
    real_rename = persistence.os.rename
    initial_swapped = False
    post_classification_swapped = False

    def move_owner_into_quarantine(
        *,
        directory: int,
        quarantine_name: str,
    ) -> None:
        nonlocal post_classification_swapped
        real_rename(
            quarantine_name,
            foreign_out.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        real_rename(
            owner_out.name,
            quarantine_name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        post_classification_swapped = True

    def classify_then_rebind(**kwargs: object):
        result = real_classifier(**kwargs)
        if race_phase == "after_identity_classification" and result.state == "mismatch":
            move_owner_into_quarantine(
                directory=int(kwargs["directory"]),
                quarantine_name=str(kwargs["name"]),
            )
            if official_bytes is not None:
                archive.write_bytes(official_bytes)
        return result

    def swap_source_at_rename(
        *,
        src_dir_fd: int,
        src_name: str,
        dst_dir_fd: int,
        dst_name: str,
    ) -> None:
        nonlocal initial_swapped
        if (
            not initial_swapped
            and src_name == archive.name
            and dst_name.startswith(f".{archive.name}.")
            and dst_name.endswith(".rollback-orphan")
        ):
            real_rename(
                archive.name,
                owner_out.name,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=src_dir_fd,
            )
            foreign = archive.parent / "foreign-source-winner.json"
            foreign.write_bytes(foreign_bytes)
            real_rename(
                foreign.name,
                archive.name,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=src_dir_fd,
            )
            initial_swapped = True
        elif (
            race_phase == "during_reverse_rename"
            and initial_swapped
            and not post_classification_swapped
            and src_name.startswith(f".{archive.name}.")
            and src_name.endswith(".rollback-orphan")
            and dst_name == archive.name
        ):
            move_owner_into_quarantine(
                directory=src_dir_fd,
                quarantine_name=src_name,
            )
        real_no_replace(
            src_dir_fd=src_dir_fd,
            src_name=src_name,
            dst_dir_fd=dst_dir_fd,
            dst_name=dst_name,
        )

    with (
        mock.patch.object(
            persistence,
            "_classify_quarantined_entry_identity_at",
            side_effect=classify_then_rebind,
        ),
        mock.patch.object(
            persistence,
            "_rename_no_replace_at",
            side_effect=swap_source_at_rename,
        ),
    ):
        yield


@contextmanager
def _canonical_replace_interceptor(callback):
    real_replace = persistence.os.replace

    def intercept(
        source: object,
        destination: object,
        *args: object,
        **kwargs: object,
    ) -> None:
        if Path(os.fspath(destination)).name == "design_spec.json":
            callback(real_replace, source, destination, args, kwargs)
            return
        real_replace(source, destination, *args, **kwargs)

    with mock.patch.object(
        persistence.os,
        "replace",
        side_effect=intercept,
    ):
        yield


@contextmanager
def _fail_canonical_replace(before_failure=None):
    def fail(_real_replace, _source, _destination, _args, _kwargs) -> None:
        if before_failure is not None:
            before_failure()
        raise OSError("injected canonical write failure")

    with _canonical_replace_interceptor(fail):
        yield


def _cross_process_persist(
    root_text: str,
    archive_name: str,
    started: object,
    entered_publish: object,
    result: object,
) -> None:
    root = Path(root_text)
    started.set()
    try:
        persistence.persist_design_spec_payload(
            canonical_path=root / "design_spec.json",
            archive_path=root / "specs" / archive_name,
            payload={"revision": 3, "design_spec": {"title": "cross-process"}},
            before_archive_publish=lambda _path: entered_publish.set(),
        )
    except BaseException as exc:
        result.put((type(exc).__name__, str(exc)))
    else:
        result.put(None)


class DesignSpecPersistenceTests(unittest.TestCase):
    def test_temp_path_replacement_before_link_never_commits_foreign_archive(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX retained-temp publication probe")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            payload = {"revision": 2, "design_spec": {"title": "owned"}}
            archive = root / "specs" / "design_spec_02.json"
            owner_out = root / "specs" / "owner-temp-renamed-out.json"
            foreign_bytes = b'{"foreign":"temp-path-winner"}'
            foreign_temp: Path | None = None

            def replace_temp_path(_path: Path) -> None:
                nonlocal foreign_temp
                temporary_paths = list(archive.parent.glob(f".{archive.name}.*.tmp"))
                self.assertEqual(len(temporary_paths), 1)
                foreign_temp = temporary_paths[0]
                foreign_temp.rename(owner_out)
                foreign_temp.write_bytes(foreign_bytes)

            with self.assertRaises(persistence.DesignSpecPersistenceError) as raised:
                persistence.persist_design_spec_payload(
                    canonical_path=root / "design_spec.json",
                    archive_path=archive,
                    payload=payload,
                    before_archive_publish=replace_temp_path,
                )

            self.assertEqual(raised.exception.phase, "archive")
            self.assertFalse(archive.exists())
            self.assertFalse((root / "design_spec.json").exists())
            self.assertEqual(owner_out.read_bytes(), _encoded(payload))
            self.assertIsNotNone(foreign_temp)
            self.assertEqual(foreign_temp.read_bytes(), foreign_bytes)

    def test_archive_replacement_during_canonical_write_rolls_back_canonical(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX retained archive identity probe")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            payload = {"revision": 2, "design_spec": {"title": "owned"}}
            archive = root / "specs" / "design_spec_02.json"
            canonical = root / "design_spec.json"
            owner_out = root / "specs" / "owner-archive-renamed-out.json"
            foreign_bytes = b'{"foreign":"archive-path-winner"}'
            real_replace = persistence.os.replace
            replaced = False

            def replace_archive_during_canonical_write(
                source: object,
                destination: object,
                *args: object,
                **kwargs: object,
            ) -> None:
                nonlocal replaced
                if not replaced and Path(os.fspath(destination)).name == canonical.name:
                    replaced = True
                    archive.rename(owner_out)
                    archive.write_bytes(foreign_bytes)
                real_replace(source, destination, *args, **kwargs)

            with mock.patch.object(
                persistence.os,
                "replace",
                side_effect=replace_archive_during_canonical_write,
            ):
                with self.assertRaises(
                    persistence.DesignSpecPersistenceError,
                ) as raised:
                    _persist(root, payload)

            self.assertIn(raised.exception.phase, {"canonical", "canonical_rollback"})
            self.assertTrue(replaced)
            self.assertFalse(canonical.exists())
            self.assertEqual(archive.read_bytes(), foreign_bytes)
            self.assertEqual(owner_out.read_bytes(), _encoded(payload))

    def test_canonical_temp_swap_during_replace_restores_prior_without_deleting_foreign(
        self,
    ) -> None:
        if os.name != "posix":
            self.skipTest("POSIX canonical temp replacement probe")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            canonical = root / "design_spec.json"
            archive = root / "specs" / "design_spec_02.json"
            prior_bytes = b'{"prior":true}'
            foreign_bytes = b'{"foreign":"canonical-temp-winner"}'
            payload = {"revision": 2, "design_spec": {"title": "owned"}}
            canonical.write_bytes(prior_bytes)
            owner_temp_name = "owner-canonical-temp-renamed-out.json"
            real_replace = persistence.os.replace
            swapped = False

            def swap_canonical_temp(
                source: object,
                destination: object,
                *args: object,
                **kwargs: object,
            ) -> None:
                nonlocal swapped
                if not swapped and Path(os.fspath(destination)).name == canonical.name:
                    swapped = True
                    directory = int(kwargs["src_dir_fd"])
                    os.rename(
                        source,
                        owner_temp_name,
                        src_dir_fd=directory,
                        dst_dir_fd=directory,
                    )
                    foreign = os.open(
                        os.fspath(source),
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o600,
                        dir_fd=directory,
                    )
                    try:
                        os.write(foreign, foreign_bytes)
                        os.fsync(foreign)
                    finally:
                        os.close(foreign)
                real_replace(source, destination, *args, **kwargs)

            with mock.patch.object(
                persistence.os,
                "replace",
                side_effect=swap_canonical_temp,
            ):
                with self.assertRaises(
                    persistence.DesignSpecPersistenceError,
                ):
                    _persist(root, payload)

            self.assertTrue(swapped)
            self.assertEqual(canonical.read_bytes(), prior_bytes)
            self.assertFalse(archive.exists())
            self.assertEqual((root / owner_temp_name).read_bytes(), _encoded(payload))
            quarantines = list(root.glob(f".{canonical.name}.*.rollback-orphan"))
            self.assertIn(foreign_bytes, [path.read_bytes() for path in quarantines])

    def test_parent_rebind_rolls_back_before_caller_state_install(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX retained directory probe")
        with tempfile.TemporaryDirectory() as raw_tmp:
            container = Path(raw_tmp)
            root = container / "active"
            moved = container / "moved"
            ctx = _fresh_video_context(root)
            prior_state = deepcopy(ctx.state)
            canonical = root / "design_spec.json"
            archive = root / "specs" / "design_spec_01.json"
            real_replace = persistence.os.replace
            rebound = False

            def rebind_before_canonical_replace(
                source: object,
                destination: object,
                *args: object,
                **kwargs: object,
            ) -> None:
                nonlocal rebound
                destination_name = os.fspath(destination)
                if not rebound and Path(destination_name).name == canonical.name:
                    rebound = True
                    root.rename(moved)
                    root.mkdir()
                real_replace(source, destination, *args, **kwargs)

            with mock.patch.object(
                persistence.os,
                "replace",
                side_effect=rebind_before_canonical_replace,
            ):
                result = propose_design_spec(
                    {"design_spec": _video_spec("Rebound proposal")},
                    ctx=ctx,
                )

            self.assertTrue(rebound)
            self.assertEqual(result.status, "error")
            self.assertEqual(ctx.state, prior_state)
            self.assertFalse(canonical.exists())
            self.assertFalse(archive.exists())
            self.assertFalse((moved / canonical.name).exists())
            self.assertFalse((moved / "specs" / archive.name).exists())

    def test_final_archive_fsync_root_rebind_rolls_back_before_state_install(
        self,
    ) -> None:
        if os.name != "posix":
            self.skipTest("POSIX retained directory fsync probe")
        with tempfile.TemporaryDirectory() as raw_tmp:
            container = Path(raw_tmp)
            root = container / "active"
            moved = container / "moved"
            ctx = _fresh_video_context(root)
            prior_state = deepcopy(ctx.state)
            canonical = root / "design_spec.json"
            archive = root / "specs" / "design_spec_01.json"
            real_fsync = persistence.os.fsync
            archive_fsyncs = 0
            rebound = False

            def rebind_root_during_final_archive_fsync(descriptor: int) -> None:
                nonlocal archive_fsyncs, rebound
                opened = os.fstat(descriptor)
                if stat.S_ISDIR(opened.st_mode) and archive.parent.exists():
                    archive_parent = os.stat(archive.parent)
                    if (opened.st_dev, opened.st_ino) == (
                        archive_parent.st_dev,
                        archive_parent.st_ino,
                    ):
                        archive_fsyncs += 1
                        if archive_fsyncs == 2:
                            rebound = True
                            root.rename(moved)
                            root.mkdir()
                real_fsync(descriptor)

            with mock.patch.object(
                persistence.os,
                "fsync",
                side_effect=rebind_root_during_final_archive_fsync,
            ):
                result = propose_design_spec(
                    {"design_spec": _video_spec("Late root rebind")},
                    ctx=ctx,
                )

            self.assertTrue(rebound)
            self.assertEqual(result.status, "error")
            self.assertEqual(ctx.state, prior_state)
            self.assertFalse(canonical.exists())
            self.assertFalse(archive.exists())
            self.assertFalse((moved / canonical.name).exists())
            self.assertFalse((moved / "specs" / archive.name).exists())

    def test_final_canonical_fsync_specs_rebind_rolls_back_before_state_install(
        self,
    ) -> None:
        if os.name != "posix":
            self.skipTest("POSIX retained directory fsync probe")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp) / "run"
            ctx = _fresh_video_context(root)
            prior_state = deepcopy(ctx.state)
            canonical = root / "design_spec.json"
            archive = root / "specs" / "design_spec_01.json"
            moved_specs = root / "moved-specs"
            root_metadata = os.stat(root)
            real_fsync = persistence.os.fsync
            root_fsyncs = 0
            rebound = False

            def rebind_specs_during_final_canonical_fsync(descriptor: int) -> None:
                nonlocal root_fsyncs, rebound
                opened = os.fstat(descriptor)
                if (
                    stat.S_ISDIR(opened.st_mode)
                    and (opened.st_dev, opened.st_ino)
                    == (root_metadata.st_dev, root_metadata.st_ino)
                ):
                    root_fsyncs += 1
                    if root_fsyncs == 3:
                        rebound = True
                        archive.parent.rename(moved_specs)
                        archive.parent.mkdir()
                real_fsync(descriptor)

            with mock.patch.object(
                persistence.os,
                "fsync",
                side_effect=rebind_specs_during_final_canonical_fsync,
            ):
                result = propose_design_spec(
                    {"design_spec": _video_spec("Late specs rebind")},
                    ctx=ctx,
                )

            self.assertTrue(rebound)
            self.assertEqual(result.status, "error")
            self.assertEqual(ctx.state, prior_state)
            self.assertFalse(canonical.exists())
            self.assertFalse(archive.exists())
            self.assertFalse((moved_specs / archive.name).exists())

    def test_ancestor_rebind_preserves_requested_path_state_consistency(self) -> None:
        if os.name != "posix" or not hasattr(os, "symlink"):
            self.skipTest("POSIX ancestor rebind probe requires symlinks")
        with tempfile.TemporaryDirectory() as raw_tmp:
            container = Path(raw_tmp)
            old_root = container / "old"
            new_root = container / "new"
            (old_root / "run").mkdir(parents=True)
            (new_root / "run").mkdir(parents=True)
            route = container / "route"
            route.symlink_to(old_root.name, target_is_directory=True)
            run_dir = route / "run"
            ctx = _fresh_video_context(run_dir)
            prior_state = deepcopy(ctx.state)
            canonical = run_dir / "design_spec.json"
            archive = run_dir / "specs" / "design_spec_01.json"
            real_replace = persistence.os.replace
            rebound = False

            def rebind_before_canonical_replace(
                source: object,
                destination: object,
                *args: object,
                **kwargs: object,
            ) -> None:
                nonlocal rebound
                destination_name = os.fspath(destination)
                if not rebound and Path(destination_name).name == canonical.name:
                    rebound = True
                    route.unlink()
                    route.symlink_to(new_root.name, target_is_directory=True)
                real_replace(source, destination, *args, **kwargs)

            with mock.patch.object(
                persistence.os,
                "replace",
                side_effect=rebind_before_canonical_replace,
            ):
                result = propose_design_spec(
                    {"design_spec": _video_spec("Ancestor rebound proposal")},
                    ctx=ctx,
                )

            self.assertTrue(rebound)
            self.assertEqual(result.status, "error")
            self.assertEqual(ctx.state, prior_state)
            self.assertFalse((old_root / "run" / canonical.name).exists())
            self.assertFalse((old_root / "run" / "specs" / archive.name).exists())
            self.assertFalse((new_root / "run" / canonical.name).exists())
            self.assertFalse((new_root / "run" / "specs" / archive.name).exists())

    def test_lock_replacement_during_canonical_commit_rolls_back_the_pair(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX retained lock identity probe")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            canonical = root / "design_spec.json"
            archive = root / "specs" / "design_spec_02.json"
            lock_path = root / ".design_spec.json.lock"
            owner_lock = root / "owner-lock-renamed-out"
            foreign_lock_bytes = b"foreign-lock"
            payload = {"revision": 2, "design_spec": {"title": "owned"}}
            replaced = False

            def replace_lock_then_commit(
                real_replace,
                source: object,
                destination: object,
                args: tuple[object, ...],
                kwargs: dict[str, object],
            ) -> None:
                nonlocal replaced
                replaced = True
                lock_path.rename(owner_lock)
                replacement = root / "replacement-lock"
                replacement.write_bytes(foreign_lock_bytes)
                real_replace(replacement, lock_path)
                real_replace(source, destination, *args, **kwargs)

            with _canonical_replace_interceptor(replace_lock_then_commit):
                with self.assertRaises(
                    persistence.DesignSpecPersistenceError,
                ) as raised:
                    _persist(root, payload)

            self.assertTrue(replaced)
            self.assertIn(raised.exception.phase, {"canonical", "canonical_rollback"})
            self.assertFalse(canonical.exists())
            self.assertFalse(archive.exists())
            self.assertEqual(lock_path.read_bytes(), foreign_lock_bytes)
            self.assertTrue(owner_lock.is_file())

    def test_recovery_persistence_failure_restores_nested_layout_selection_state(
        self,
    ) -> None:
        from PIL import Image, ImageDraw

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            layers = root / "layers"
            layers.mkdir()
            source_visual = layers / "source.png"
            image = Image.new("RGB", (640, 360), "white")
            ImageDraw.Draw(image).rectangle((80, 60, 560, 300), fill="#224466")
            image.save(source_visual)
            ctx = ToolContext(
                settings=SimpleNamespace(poster_harness_mode="cheap"),
                run_dir=root,
                layers_dir=layers,
                run_id="nested-recovery-state-rollback",
            )
            ctx.state.update(
                {
                    "artifact_type": "poster",
                    "poster_content_brief": {
                        "kind": "paper_poster_content_brief",
                        "title": "Retained state",
                        "authors": ["A. Researcher"],
                        "sections": [
                            {
                                "section_id": "method",
                                "heading": "Method",
                                "bullets": [
                                    {
                                        "text": "The method connects source evidence to an editable layout."
                                    }
                                ],
                            }
                        ],
                    },
                    "paper_visual_storyboard": {
                        "kind": "paper_visual_storyboard",
                        "selected_assets": [{"asset_id": "ingest_fig_01"}],
                        "layout_selected_assets": [{"asset_id": "prior-storyboard"}],
                        "layout_selected_asset_count": 1,
                    },
                    "poster_plan_contract": {
                        "kind": "paper_poster_plan_contract",
                        "title": "Retained state",
                        "selected_visuals": [{"asset_id": "ingest_fig_01"}],
                        "layout_selected_assets": [{"asset_id": "prior-contract"}],
                        "layout_selected_asset_count": 1,
                    },
                    "rendered_layers": {
                        "ingest_fig_01": {
                            "src_path": os.fspath(source_visual),
                            "image_size": "640x360",
                            "caption": "Source-grounded system overview.",
                            "visual_score": 90,
                        }
                    },
                }
            )
            before = deepcopy(ctx.state)
            (root / "specs").write_bytes(b"not a directory")

            with mock.patch.dict(
                os.environ,
                {"POSTER_ENABLE_DETERMINISTIC_SPEC_RECOVERY": "1"},
            ):
                result = propose_design_spec({}, ctx=ctx)

            self.assertEqual(result.status, "error")
            self.assertEqual(result.payload.get("phase"), "namespace_scan")
            self.assertEqual(ctx.state, before)

    def test_archive_directory_fsync_failure_quarantines_owned_publication(
        self,
    ) -> None:
        if os.name != "posix":
            self.skipTest("POSIX directory fsync probe")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            payload = {"revision": 2, "design_spec": {"title": "archive-fsync"}}
            archive = root / "specs" / "design_spec_02.json"
            canonical = root / "design_spec.json"
            real_fsync = persistence.os.fsync
            failed = False

            def fail_archive_directory_once(descriptor: int) -> None:
                nonlocal failed
                opened = os.fstat(descriptor)
                if not failed and stat.S_ISDIR(opened.st_mode) and archive.parent.exists():
                    archive_parent = os.stat(archive.parent)
                    if (opened.st_dev, opened.st_ino) == (
                        archive_parent.st_dev,
                        archive_parent.st_ino,
                    ):
                        failed = True
                        raise OSError("injected archive directory fsync failure")
                real_fsync(descriptor)

            with mock.patch.object(
                persistence.os,
                "fsync",
                side_effect=fail_archive_directory_once,
            ):
                with self.assertRaises(
                    persistence.DesignSpecPersistenceError,
                ) as raised:
                    _persist(root, payload)

            self.assertTrue(failed)
            self.assertEqual(raised.exception.phase, "archive")
            self.assertFalse(archive.exists())
            self.assertFalse(canonical.exists())
            quarantines = _rollback_orphans(archive)
            self.assertEqual(len(quarantines), 1)
            self.assertEqual(quarantines[0].read_bytes(), _encoded(payload))

    def test_canonical_directory_fsync_failure_restores_prior_and_closes_fd(
        self,
    ) -> None:
        if os.name != "posix" or not Path("/dev/fd").is_dir():
            self.skipTest("POSIX fd-count and directory fsync probe")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            canonical = root / "design_spec.json"
            archive = root / "specs" / "design_spec_02.json"
            prior_bytes = b'{"prior":true}'
            payload = {"revision": 2, "design_spec": {"title": "canonical-fsync"}}
            canonical.write_bytes(prior_bytes)
            new_bytes = _encoded(payload)
            root_metadata = os.stat(root)
            real_fsync = persistence.os.fsync
            failed = False
            before_fds = _fd_count()

            def fail_installed_canonical_directory_once(descriptor: int) -> None:
                nonlocal failed
                opened = os.fstat(descriptor)
                if (
                    not failed
                    and stat.S_ISDIR(opened.st_mode)
                    and (opened.st_dev, opened.st_ino)
                    == (root_metadata.st_dev, root_metadata.st_ino)
                    and canonical.is_file()
                    and canonical.read_bytes() == new_bytes
                ):
                    failed = True
                    raise OSError("injected canonical directory fsync failure")
                real_fsync(descriptor)

            with mock.patch.object(
                persistence.os,
                "fsync",
                side_effect=fail_installed_canonical_directory_once,
            ):
                with self.assertRaises(
                    persistence.DesignSpecPersistenceError,
                ) as raised:
                    _persist(root, payload)

            after_fds = _fd_count()
            self.assertTrue(failed)
            self.assertIn(raised.exception.phase, {"canonical", "canonical_rollback"})
            self.assertEqual(canonical.read_bytes(), prior_bytes)
            self.assertFalse(archive.exists())
            self.assertEqual(after_fds, before_fds)

    def test_canonical_rollback_preserves_later_official_and_prior_snapshot(
        self,
    ) -> None:
        if os.name != "posix" or not Path("/dev/fd").is_dir():
            self.skipTest("POSIX canonical rollback and fd-count probe")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            canonical = root / "design_spec.json"
            archive = root / "specs" / "design_spec_02.json"
            prior_bytes = b'{"prior":true}'
            later_bytes = b'{"foreign":"later-canonical"}'
            payload = {"revision": 2, "design_spec": {"title": "canonical-race"}}
            new_bytes = _encoded(payload)
            canonical.write_bytes(prior_bytes)
            root_metadata = os.stat(root)
            real_fsync = persistence.os.fsync
            real_no_replace = persistence._rename_no_replace_at
            primary_failed = False
            occupied_identity: tuple[int, int] | None = None

            def fail_installed_canonical_fsync_once(descriptor: int) -> None:
                nonlocal primary_failed
                opened = os.fstat(descriptor)
                if (
                    not primary_failed
                    and stat.S_ISDIR(opened.st_mode)
                    and (opened.st_dev, opened.st_ino)
                    == (root_metadata.st_dev, root_metadata.st_ino)
                    and canonical.is_file()
                    and canonical.read_bytes() == new_bytes
                ):
                    primary_failed = True
                    raise OSError("injected canonical durability failure")
                real_fsync(descriptor)

            def occupy_after_canonical_quarantine(
                *,
                src_dir_fd: int,
                src_name: str,
                dst_dir_fd: int,
                dst_name: str,
            ) -> None:
                nonlocal occupied_identity
                real_no_replace(
                    src_dir_fd=src_dir_fd,
                    src_name=src_name,
                    dst_dir_fd=dst_dir_fd,
                    dst_name=dst_name,
                )
                if (
                    occupied_identity is None
                    and src_name == canonical.name
                    and dst_name.startswith(f".{canonical.name}.")
                    and dst_name.endswith(".rollback-orphan")
                ):
                    canonical.write_bytes(later_bytes)
                    metadata = canonical.stat()
                    occupied_identity = (metadata.st_dev, metadata.st_ino)

            before_fds = _fd_count()
            with (
                mock.patch.object(
                    persistence.os,
                    "fsync",
                    side_effect=fail_installed_canonical_fsync_once,
                ),
                mock.patch.object(
                    persistence,
                    "_rename_no_replace_at",
                    side_effect=occupy_after_canonical_quarantine,
                ),
            ):
                with self.assertRaises(
                    persistence.DesignSpecPersistenceError,
                ) as raised:
                    _persist(root, payload)
            after_fds = _fd_count()

            self.assertTrue(primary_failed)
            self.assertEqual(raised.exception.phase, "canonical_rollback")
            self.assertEqual(canonical.read_bytes(), later_bytes)
            self.assertIsNotNone(occupied_identity)
            current = canonical.stat()
            self.assertEqual(
                (current.st_dev, current.st_ino),
                occupied_identity,
            )
            canonical_orphan_bytes = [
                path.read_bytes()
                for path in root.iterdir()
                if path.name.endswith(".rollback-orphan")
            ]
            self.assertIn(new_bytes, canonical_orphan_bytes)
            self.assertIn(prior_bytes, canonical_orphan_bytes)
            self.assertFalse(archive.exists())
            self.assertIn(
                new_bytes,
                [path.read_bytes() for path in _rollback_orphans(archive)],
            )
            diagnostic = str(raised.exception)
            self.assertIn("injected canonical durability failure", diagnostic)
            self.assertIn("rollback errors", diagnostic)
            self.assertEqual(after_fds, before_fds)

    def test_rollback_fsync_failure_still_restores_prior_and_reports_both_errors(
        self,
    ) -> None:
        if os.name != "posix" or not Path("/dev/fd").is_dir():
            self.skipTest("POSIX fd-count and directory fsync probe")
        for primary_mode in ("canonical_durability", "namespace_rebind"):
            with (
                self.subTest(primary_mode=primary_mode),
                tempfile.TemporaryDirectory() as raw_tmp,
            ):
                container = Path(raw_tmp)
                root = container / "active"
                moved = container / "moved"
                ctx, prior_bytes = _prior_video_context(root)
                prior_state = deepcopy(ctx.state)
                canonical = root / "design_spec.json"
                archive = root / "specs" / "design_spec_02.json"
                root_metadata = os.stat(root)
                real_fsync = persistence.os.fsync
                primary_failed = False
                rollback_fsync_failed = False
                rebound = False

                def fail_primary_and_rollback_fsync(descriptor: int) -> None:
                    nonlocal primary_failed, rollback_fsync_failed
                    opened = os.fstat(descriptor)
                    retained_root = moved if rebound else root
                    retained_canonical = retained_root / canonical.name
                    rollback_orphans = list(
                        retained_root.glob(f".{canonical.name}.*.rollback-orphan")
                    )
                    is_retained_root = (
                        stat.S_ISDIR(opened.st_mode)
                        and (opened.st_dev, opened.st_ino)
                        == (root_metadata.st_dev, root_metadata.st_ino)
                    )
                    if (
                        primary_mode == "canonical_durability"
                        and is_retained_root
                        and not primary_failed
                        and retained_canonical.is_file()
                        and retained_canonical.read_bytes() != prior_bytes
                    ):
                        primary_failed = True
                        raise OSError("injected canonical primary fsync failure")
                    if (
                        is_retained_root
                        and not rollback_fsync_failed
                        and (primary_failed or rebound)
                        and not retained_canonical.exists()
                        and rollback_orphans
                    ):
                        rollback_fsync_failed = True
                        raise OSError("injected rollback durability fsync failure")
                    real_fsync(descriptor)

                def replace_then_rebind(
                    real_replace,
                    source: object,
                    destination: object,
                    args: tuple[object, ...],
                    kwargs: dict[str, object],
                ) -> None:
                    nonlocal rebound
                    real_replace(source, destination, *args, **kwargs)
                    root.rename(moved)
                    root.mkdir()
                    rebound = True

                before_fds = _fd_count()
                with ExitStack() as stack:
                    stack.enter_context(
                        mock.patch.object(
                            persistence.os,
                            "fsync",
                            side_effect=fail_primary_and_rollback_fsync,
                        )
                    )
                    if primary_mode == "namespace_rebind":
                        stack.enter_context(
                            _canonical_replace_interceptor(replace_then_rebind)
                        )
                    result = propose_design_spec(
                        {"design_spec": _video_spec("Replacement canonical")},
                        ctx=ctx,
                    )
                after_fds = _fd_count()

                retained_root = moved if rebound else root
                self.assertEqual(result.status, "error")
                self.assertEqual(ctx.state, prior_state)
                self.assertTrue(rollback_fsync_failed)
                self.assertEqual(
                    (retained_root / canonical.name).read_bytes(),
                    prior_bytes,
                )
                self.assertFalse((retained_root / "specs" / archive.name).exists())
                self.assertEqual(after_fds, before_fds)
                self.assertIn(
                    "injected rollback durability fsync failure",
                    result.error_message or "",
                )
                if primary_mode == "canonical_durability":
                    self.assertTrue(primary_failed)
                    self.assertIn(
                        "injected canonical primary fsync failure",
                        result.error_message or "",
                    )
                else:
                    self.assertTrue(rebound)
                    self.assertFalse(canonical.exists())
                    self.assertFalse(archive.exists())
                    self.assertIn(
                        "unsafe DesignSpec canonical parent",
                        result.error_message or "",
                    )

    def test_fsync_failure_allows_retry_with_different_same_revision_payload(
        self,
    ) -> None:
        if os.name != "posix":
            self.skipTest("POSIX directory fsync probe")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp) / "run"
            ctx = _fresh_video_context(root)
            prior_state = deepcopy(ctx.state)
            archive = root / "specs" / "design_spec_01.json"
            real_fsync = persistence.os.fsync
            failed = False

            def fail_archive_directory_once(descriptor: int) -> None:
                nonlocal failed
                opened = os.fstat(descriptor)
                if not failed and stat.S_ISDIR(opened.st_mode) and archive.parent.exists():
                    archive_parent = os.stat(archive.parent)
                    if (opened.st_dev, opened.st_ino) == (
                        archive_parent.st_dev,
                        archive_parent.st_ino,
                    ):
                        failed = True
                        raise OSError("injected first revision fsync failure")
                real_fsync(descriptor)

            with mock.patch.object(
                persistence.os,
                "fsync",
                side_effect=fail_archive_directory_once,
            ):
                first = propose_design_spec(
                    {"design_spec": _video_spec("First payload")},
                    ctx=ctx,
                )

            self.assertTrue(failed)
            self.assertEqual(first.status, "error")
            self.assertEqual(ctx.state, prior_state)
            self.assertFalse(archive.exists())
            self.assertFalse((root / "design_spec.json").exists())

            second = propose_design_spec(
                {"design_spec": _video_spec("Different retry payload")},
                ctx=ctx,
            )

            current_spec = ctx.state.get("design_spec")
            self.assertEqual(second.status, "ok")
            self.assertIsInstance(current_spec, DesignSpec)
            self.assertEqual(ctx.state.get("spec_revision_count"), 1)
            self.assertEqual(
                ctx.state.get("design_spec_sha256"),
                design_spec_sha256(current_spec),
            )
            persisted = json.loads(archive.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["design_spec"]["html_artifact"]["frames"][0]["blocks"][0]["text"],
                "Different retry payload",
            )

    def test_posix_archive_publish_never_unlinks_a_temp_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            payload = {"revision": 2, "design_spec": {"title": "retry"}}
            archive = root / "specs" / "design_spec_02.json"
            canonical = root / "design_spec.json"
            real_unlink = persistence.os.unlink
            injected = False

            def fail_first_temp_unlink(path: object, *args: object, **kwargs: object) -> None:
                nonlocal injected
                if not injected and str(path).endswith(".tmp"):
                    injected = True
                    raise OSError("injected archive temp unlink failure")
                real_unlink(path, *args, **kwargs)

            with mock.patch.object(
                persistence.os,
                "unlink",
                side_effect=fail_first_temp_unlink,
            ):
                _persist(root, payload)

            self.assertFalse(injected)
            self.assertEqual(archive.read_bytes(), _encoded(payload))
            self.assertEqual(archive.stat().st_nlink, 1)
            self.assertEqual(canonical.read_bytes(), _encoded(payload))

    def test_windows_archive_temp_unlink_failure_releases_official_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            payload = {"revision": 2, "design_spec": {"title": "windows-retry"}}
            archive = root / "specs" / "design_spec_02.json"
            native = _FakeWindowsArchiveIO()
            real_unlink = persistence.os.unlink
            injected = False

            def fail_first_temp_unlink(path: object, *args: object, **kwargs: object) -> None:
                nonlocal injected
                if not injected and str(path).endswith(".tmp"):
                    injected = True
                    raise OSError("injected Windows archive temp unlink failure")
                real_unlink(path, *args, **kwargs)

            with (
                mock.patch.object(persistence, "_RUNTIME_OS_NAME", "nt"),
                mock.patch.object(
                    persistence,
                    "_windows_archive_io_factory",
                    return_value=native,
                ),
                mock.patch.object(
                    persistence.os,
                    "unlink",
                    side_effect=fail_first_temp_unlink,
                ),
            ):
                with self.assertRaises(persistence.DesignSpecPersistenceError) as raised:
                    _persist(root, payload)
                self.assertEqual(raised.exception.phase, "archive")
                self.assertFalse(archive.exists())
                _persist(root, payload)

            self.assertTrue(injected)
            self.assertEqual(archive.read_bytes(), _encoded(payload))
            self.assertEqual(archive.stat().st_nlink, 1)
            self.assertTrue(native.flushed_parents)
            self.assertTrue(all(handle.closed for handle in native.handles))

    def test_archive_temp_foreign_swap_is_preserved_without_path_cleanup(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX archive rollback uses directory-relative rename")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            payload = {"revision": 2, "design_spec": {"title": "owned"}}
            archive = root / "specs" / "design_spec_02.json"
            owner_out = root / "specs" / "owner-temp-renamed-out.json"
            foreign_bytes = b'{"foreign":"publish-winner"}'
            foreign_temp: Path | None = None

            def swap_temp(_path: Path) -> None:
                nonlocal foreign_temp
                temporary_paths = list(archive.parent.glob(f".{archive.name}.*.tmp"))
                self.assertEqual(len(temporary_paths), 1)
                foreign_temp = temporary_paths[0]
                foreign_temp.rename(owner_out)
                foreign_temp.write_bytes(foreign_bytes)

            with self.assertRaises(persistence.DesignSpecPersistenceError) as raised:
                persistence.persist_design_spec_payload(
                    canonical_path=root / "design_spec.json",
                    archive_path=archive,
                    payload=payload,
                    before_archive_publish=swap_temp,
                )

            self.assertEqual(raised.exception.phase, "archive")
            self.assertFalse(archive.exists())
            self.assertEqual(owner_out.read_bytes(), _encoded(payload))
            self.assertIsNotNone(foreign_temp)
            self.assertEqual(foreign_temp.read_bytes(), foreign_bytes)

    def test_canonical_symlink_cannot_change_lock_identity_during_replace(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink support is required")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            canonical = root / "design_spec.json"
            target = root / "canonical-target.json"
            target.write_bytes(b'{"old":true}')
            canonical.symlink_to(target.name)
            owner_replaced = threading.Event()
            release_owner = threading.Event()
            adopter_entered_publish = threading.Event()
            owner_done = threading.Event()
            errors: dict[str, BaseException] = {}
            real_atomic_write_json = persistence.atomic_write_json

            def coordinated_write(path: Path, data: object) -> None:
                if threading.current_thread().name == "symlink-lock-owner":
                    real_atomic_write_json(path, data)
                    owner_replaced.set()
                    self.assertTrue(release_owner.wait(5))
                    return
                real_atomic_write_json(path, data)

            def owner_run() -> None:
                try:
                    persistence.persist_design_spec_payload(
                        canonical_path=canonical,
                        archive_path=root / "specs" / "design_spec_02.json",
                        payload={"revision": 2},
                        before_archive_publish=lambda _path: None,
                    )
                except BaseException as exc:
                    errors["owner"] = exc
                finally:
                    owner_done.set()

            def adopter_run() -> None:
                try:
                    persistence.persist_design_spec_payload(
                        canonical_path=canonical,
                        archive_path=root / "specs" / "design_spec_03.json",
                        payload={"revision": 3},
                        before_archive_publish=lambda _path: adopter_entered_publish.set(),
                    )
                except BaseException as exc:
                    errors["adopter"] = exc

            with mock.patch.object(
                persistence,
                "atomic_write_json",
                side_effect=coordinated_write,
            ):
                owner = threading.Thread(target=owner_run, name="symlink-lock-owner")
                owner.start()
                replaced_before_rejection = owner_replaced.wait(0.5)
                adopter: threading.Thread | None = None
                overlapped = False
                if replaced_before_rejection:
                    adopter = threading.Thread(
                        target=adopter_run,
                        name="symlink-lock-adopter",
                    )
                    adopter.start()
                    overlapped = adopter_entered_publish.wait(0.5)
                    release_owner.set()
                    adopter.join(5)
                owner.join(5)

            self.assertFalse(owner.is_alive())
            if adopter is not None:
                self.assertFalse(adopter.is_alive())
            self.assertFalse(
                replaced_before_rejection,
                "unsafe canonical symlink was replaced while using a drifting lock identity",
            )
            self.assertFalse(overlapped)
            self.assertIsInstance(
                errors.get("owner"),
                persistence.DesignSpecPersistenceError,
            )
            self.assertEqual(errors["owner"].phase, "canonical_lock")
            self.assertTrue(canonical.is_symlink())
            self.assertEqual(target.read_bytes(), b'{"old":true}')

    def test_canonical_lock_identity_never_resolves_the_final_component(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            canonical = root / "design_spec.json"
            target = root / "target.json"
            target.write_text("old", encoding="utf-8")
            canonical.symlink_to(target.name)
            before = persistence._canonical_lock_identity(canonical)
            canonical.unlink()
            canonical.write_text("new", encoding="utf-8")
            after = persistence._canonical_lock_identity(canonical)

            self.assertEqual(before, after)
            self.assertEqual(before.name, canonical.name)

    def test_posix_sidecar_rejects_symlink_hardlink_and_path_replacement(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX sidecar probe requires flock and dir_fd")
        for mutation in ("symlink", "hardlink", "replace_identity"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as raw_tmp,
                ExitStack() as stack,
            ):
                root = Path(raw_tmp)
                canonical = root / "design_spec.json"
                archive = root / "specs" / "design_spec_02.json"
                lock_path = root / ".design_spec.json.lock"

                if mutation == "symlink":
                    target = root / "lock-target"
                    target.write_bytes(b"target")
                    lock_path.symlink_to(target.name)
                else:
                    lock_path.write_bytes(b"lock")
                    if mutation == "hardlink":
                        os.link(lock_path, root / "other-lock-link")
                    else:
                        real_lock_sidecar = persistence._lock_sidecar

                        def replace_after_lock(handle: object) -> None:
                            real_lock_sidecar(handle)
                            replacement = root / "replacement-lock"
                            replacement.write_bytes(b"foreign-lock")
                            os.replace(replacement, lock_path)

                        stack.enter_context(
                            mock.patch.object(
                                persistence,
                                "_lock_sidecar",
                                side_effect=replace_after_lock,
                            )
                        )

                with self.assertRaises(
                    persistence.DesignSpecPersistenceError,
                ) as raised:
                    persistence.persist_design_spec_payload(
                        canonical_path=canonical,
                        archive_path=archive,
                        payload={"revision": 2},
                        before_archive_publish=lambda _path: None,
                    )

                self.assertEqual(raised.exception.phase, "canonical_lock")
                self.assertFalse(archive.exists())
                if mutation == "symlink":
                    self.assertTrue(lock_path.is_symlink())
                elif mutation == "hardlink":
                    self.assertEqual(lock_path.stat().st_nlink, 2)
                else:
                    self.assertEqual(lock_path.read_bytes(), b"foreign-lock")

    def test_canonical_parent_replacement_after_lock_fails_closed(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX parent replacement probe requires flock")
        with tempfile.TemporaryDirectory() as raw_tmp:
            container = Path(raw_tmp)
            root = container / "active"
            moved = container / "moved"
            root.mkdir()
            canonical = root / "design_spec.json"
            archive = root / "specs" / "design_spec_02.json"
            real_lock_sidecar = persistence._lock_sidecar

            def replace_parent_after_lock(handle: object) -> None:
                real_lock_sidecar(handle)
                root.rename(moved)
                root.mkdir()

            with mock.patch.object(
                persistence,
                "_lock_sidecar",
                side_effect=replace_parent_after_lock,
            ):
                with self.assertRaises(
                    persistence.DesignSpecPersistenceError,
                ) as raised:
                    persistence.persist_design_spec_payload(
                        canonical_path=canonical,
                        archive_path=archive,
                        payload={"revision": 2},
                        before_archive_publish=lambda _path: None,
                    )

            self.assertEqual(raised.exception.phase, "canonical_lock")
            self.assertFalse(archive.exists())
            self.assertFalse((moved / "specs" / archive.name).exists())
            self.assertFalse(canonical.exists())
            self.assertFalse((moved / canonical.name).exists())

    def test_archive_directory_rebind_never_returns_success(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX retained archive directory probe")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            canonical = root / "design_spec.json"
            archive = root / "specs" / "design_spec_02.json"
            moved_specs = root / "moved-specs"
            payload = {"revision": 2, "design_spec": {"title": "bound"}}
            rebound = False

            def rebind_archive_directory(_path: Path) -> None:
                nonlocal rebound
                rebound = True
                archive.parent.rename(moved_specs)
                archive.parent.mkdir()

            with self.assertRaises(persistence.DesignSpecPersistenceError):
                persistence.persist_design_spec_payload(
                    canonical_path=canonical,
                    archive_path=archive,
                    payload=payload,
                    before_archive_publish=rebind_archive_directory,
                )

            self.assertTrue(rebound)
            self.assertFalse(canonical.exists())
            self.assertFalse(archive.exists())
            self.assertFalse((moved_specs / archive.name).exists())

    def test_archive_binding_rejects_escape_and_reparse_parent(self) -> None:
        mutations = ["escape"]
        if hasattr(os, "symlink"):
            mutations.append("reparse_parent")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(raw_tmp) / "run"
                root.mkdir()
                canonical = root / "design_spec.json"
                if mutation == "escape":
                    archive = root.parent / "escaped" / "design_spec_02.json"
                else:
                    target = root.parent / "archive-target"
                    target.mkdir()
                    (root / "specs").symlink_to(
                        target,
                        target_is_directory=True,
                    )
                    archive = root / "specs" / "design_spec_02.json"

                with self.assertRaises(
                    persistence.DesignSpecPersistenceError,
                ) as raised:
                    persistence.persist_design_spec_payload(
                        canonical_path=canonical,
                        archive_path=archive,
                        payload={"revision": 2},
                        before_archive_publish=lambda _path: None,
                    )

                self.assertEqual(raised.exception.phase, "canonical_lock")
                self.assertFalse(canonical.exists())
                self.assertFalse(archive.exists())

    def test_sidecar_lock_is_cross_process_and_shared_across_revisions(self) -> None:
        if os.name != "posix":
            self.skipTest("cross-process lock probe uses the POSIX test runtime")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            canonical = root / "design_spec.json"
            context = multiprocessing.get_context("spawn")
            started = context.Event()
            entered_publish = context.Event()
            result = context.Queue()
            process = context.Process(
                target=_cross_process_persist,
                args=(
                    os.fspath(root),
                    "design_spec_03.json",
                    started,
                    entered_publish,
                    result,
                ),
            )

            with persistence._canonical_transaction_lock(canonical):
                process.start()
                self.assertTrue(started.wait(5))
                entered_while_locked = entered_publish.wait(0.5)

            self.assertTrue(entered_publish.wait(5))
            process.join(5)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
            self.assertFalse(
                entered_while_locked,
                "another process entered a different revision under the same canonical lock",
            )
            self.assertIsNone(result.get(timeout=1))
            self.assertTrue((root / "specs" / "design_spec_03.json").is_file())

    def test_same_payload_adopter_waits_for_owner_rollback_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            payload = {"revision": 2, "design_spec": {"title": "shared"}}
            archive = root / "specs" / "design_spec_02.json"
            canonical = root / "design_spec.json"
            owner_at_canonical = threading.Event()
            release_owner = threading.Event()
            adopter_at_canonical = threading.Event()
            release_adopter = threading.Event()
            errors: dict[str, BaseException] = {}

            def coordinated_write(
                real_replace,
                source: object,
                destination: object,
                args: tuple[object, ...],
                kwargs: dict[str, object],
            ) -> None:
                if threading.current_thread().name == "design-spec-owner":
                    owner_at_canonical.set()
                    self.assertTrue(release_owner.wait(5))
                    raise OSError("injected owner canonical failure")
                adopter_at_canonical.set()
                self.assertTrue(release_adopter.wait(5))
                real_replace(source, destination, *args, **kwargs)

            def persist_in_thread(role: str) -> None:
                try:
                    _persist(root, payload)
                except BaseException as exc:
                    errors[role] = exc

            with _canonical_replace_interceptor(coordinated_write):
                owner = threading.Thread(
                    target=persist_in_thread,
                    args=("owner",),
                    name="design-spec-owner",
                )
                owner.start()
                self.assertTrue(owner_at_canonical.wait(5))

                adopter = threading.Thread(
                    target=persist_in_thread,
                    args=("adopter",),
                    name="design-spec-adopter",
                )
                adopter.start()
                overlapped_owner = adopter_at_canonical.wait(0.5)
                release_owner.set()
                owner.join(5)
                release_adopter.set()
                adopter.join(5)

            self.assertFalse(owner.is_alive())
            self.assertFalse(adopter.is_alive())
            self.assertFalse(
                overlapped_owner,
                "same-payload adopter entered before the owner transaction released",
            )
            self.assertIsInstance(
                errors.get("owner"),
                persistence.DesignSpecPersistenceError,
            )
            self.assertNotIn("adopter", errors)
            self.assertEqual(archive.read_bytes(), _encoded(payload))
            self.assertEqual(canonical.read_bytes(), _encoded(payload))

    def test_posix_rollback_quarantines_owned_archive_instead_of_unlinking(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX rollback uses directory-relative rename")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            payload = {"revision": 2, "design_spec": {"title": "owned"}}
            archive = root / "specs" / "design_spec_02.json"

            with _fail_canonical_replace():
                with self.assertRaises(persistence.DesignSpecPersistenceError) as raised:
                    _persist(root, payload)

            self.assertEqual(raised.exception.phase, "canonical")
            self.assertFalse(archive.exists())
            quarantines = _rollback_orphans(archive)
            self.assertEqual(len(quarantines), 1)
            self.assertEqual(quarantines[0].read_bytes(), _encoded(payload))

    def test_quarantine_destination_race_never_overwrites_foreign_entry(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX quarantine uses atomic no-replace rename")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            payload = {"revision": 2, "design_spec": {"title": "owned"}}
            archive = root / "specs" / "design_spec_02.json"
            foreign_bytes = b'{"foreign":"quarantine-destination-winner"}'
            real_unique_name = persistence._unique_quarantine_name
            raced_destination: Path | None = None

            def create_foreign_after_name_selection(
                directory: int,
                archive_name: str,
            ) -> str:
                nonlocal raced_destination
                candidate = real_unique_name(directory, archive_name)
                if archive_name == archive.name and raced_destination is None:
                    descriptor = os.open(
                        candidate,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o600,
                        dir_fd=directory,
                    )
                    try:
                        os.write(descriptor, foreign_bytes)
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    raced_destination = archive.parent / candidate
                return candidate

            with _fail_canonical_replace(), mock.patch.object(
                persistence,
                "_unique_quarantine_name",
                side_effect=create_foreign_after_name_selection,
            ):
                with self.assertRaises(persistence.DesignSpecPersistenceError):
                    _persist(root, payload)

            self.assertIsNotNone(raced_destination)
            assert raced_destination is not None
            self.assertEqual(raced_destination.read_bytes(), foreign_bytes)
            self.assertFalse(archive.exists())
            owned_quarantines = [
                path
                for path in _rollback_orphans(archive)
                if path != raced_destination
            ]
            self.assertEqual(len(owned_quarantines), 1)
            self.assertEqual(owned_quarantines[0].read_bytes(), _encoded(payload))

    def test_posix_positive_quarantine_mismatch_never_reverses_foreign_binding(
        self,
    ) -> None:
        if os.name != "posix":
            self.skipTest("POSIX rollback uses directory-relative rename")
        for official_reoccupied in (False, True):
            with (
                self.subTest(official_reoccupied=official_reoccupied),
                tempfile.TemporaryDirectory() as raw_tmp,
            ):
                root = Path(raw_tmp)
                payload = {"revision": 2, "design_spec": {"title": "owned"}}
                archive = root / "specs" / "design_spec_02.json"
                owner_out = root / "specs" / "owner-renamed-out.json"
                foreign = root / "specs" / "foreign-winner.json"
                foreign_bytes = b'{"foreign":"winner"}'
                occupied_bytes = b'{"foreign":"new-official"}'
                real_rename = persistence.os.rename
                real_no_replace = persistence._rename_no_replace_at
                swapped = False
                before_fds = _fd_count() if Path("/dev/fd").is_dir() else None

                def swap_before_quarantine_rename(
                    *,
                    src_dir_fd: int,
                    src_name: str,
                    dst_dir_fd: int,
                    dst_name: str,
                ) -> None:
                    nonlocal swapped
                    if not swapped and src_name == archive.name:
                        swapped = True
                        real_rename(archive, owner_out)
                        foreign.write_bytes(foreign_bytes)
                        real_rename(foreign, archive)
                        real_no_replace(
                            src_dir_fd=src_dir_fd,
                            src_name=src_name,
                            dst_dir_fd=dst_dir_fd,
                            dst_name=dst_name,
                        )
                        if official_reoccupied:
                            archive.write_bytes(occupied_bytes)
                        return
                    real_no_replace(
                        src_dir_fd=src_dir_fd,
                        src_name=src_name,
                        dst_dir_fd=dst_dir_fd,
                        dst_name=dst_name,
                    )

                with _fail_canonical_replace(), mock.patch.object(
                    persistence,
                    "_rename_no_replace_at",
                    side_effect=swap_before_quarantine_rename,
                ):
                    with self.assertRaises(
                        persistence.DesignSpecPersistenceError,
                    ) as raised:
                        _persist(root, payload)

                diagnostic = str(raised.exception)
                self.assertTrue(swapped)
                self.assertEqual(raised.exception.phase, "canonical_rollback")
                self.assertEqual(owner_out.read_bytes(), _encoded(payload))
                quarantines = _rollback_orphans(archive)
                self.assertEqual(len(quarantines), 1)
                self.assertEqual(quarantines[0].read_bytes(), foreign_bytes)
                if official_reoccupied:
                    self.assertEqual(archive.read_bytes(), occupied_bytes)
                    occupied_identity = archive.stat()
                else:
                    self.assertFalse(archive.exists())
                    occupied_identity = None
                self.assertIn("positive device/inode mismatch", diagnostic)
                self.assertIn("moved a foreign entry", diagnostic)
                self.assertIn("no reverse recovery was attempted", diagnostic)
                self.assertNotIn("restored", diagnostic)
                self.assertNotIn("preserved", diagnostic)
                self.assertNotIn("kept", diagnostic)

                occupied_out = archive.parent / "occupied-official-out.json"
                if official_reoccupied:
                    archive.rename(occupied_out)
                    self.assertEqual(occupied_out.read_bytes(), occupied_bytes)
                    moved_identity = occupied_out.stat()
                    assert occupied_identity is not None
                    self.assertEqual(
                        (moved_identity.st_dev, moved_identity.st_ino),
                        (occupied_identity.st_dev, occupied_identity.st_ino),
                    )
                retry_payload = {
                    "revision": 2,
                    "design_spec": {"title": "different retry"},
                }
                _persist(root, retry_payload)
                self.assertEqual(archive.read_bytes(), _encoded(retry_payload))
                self.assertEqual(
                    (root / "design_spec.json").read_bytes(),
                    _encoded(retry_payload),
                )
                self.assertEqual(quarantines[0].read_bytes(), foreign_bytes)
                if before_fds is not None:
                    self.assertEqual(_fd_count(), before_fds)

    def test_posix_primary_post_quarantine_source_swaps_never_trigger_recovery(
        self,
    ) -> None:
        if os.name != "posix" or not Path("/dev/fd").is_dir():
            self.skipTest("POSIX quarantine source identity and fd-count probe")
        for race_phase in (
            "after_identity_classification",
            "during_reverse_rename",
        ):
            with (
                self.subTest(race_phase=race_phase),
                tempfile.TemporaryDirectory() as raw_tmp,
            ):
                root = Path(raw_tmp)
                ctx, prior_bytes = _prior_video_context(root)
                prior_state = deepcopy(ctx.state)
                archive = root / "specs" / "design_spec_02.json"
                canonical = root / "design_spec.json"
                owner_out = archive.parent / "retained-owner-out.json"
                foreign_out = archive.parent / "classified-foreign-out.json"
                foreign_bytes = b'{"foreign":"classified-source"}'
                occupied_bytes = b'{"foreign":"later-official"}'
                proposed_spec = DesignSpec.model_validate(
                    _video_spec(f"Primary quarantine source swap {race_phase}")
                )
                proposed_bytes = _encoded(
                    {
                        "artifact_type": "video",
                        "is_revision": True,
                        "revision": 2,
                        "parent_revision": 1,
                        "parent_design_spec_sha256": ctx.state["design_spec_sha256"],
                        "design_spec_sha256": design_spec_sha256(proposed_spec),
                        "design_spec": proposed_spec.model_dump(mode="json"),
                    }
                )

                before_fds = _fd_count()
                with (
                    _fail_canonical_replace(),
                    _rebind_quarantine_source_after_classification(
                        archive,
                        owner_out=owner_out,
                        foreign_out=foreign_out,
                        foreign_bytes=foreign_bytes,
                        race_phase=race_phase,
                        official_bytes=(
                            occupied_bytes
                            if race_phase == "after_identity_classification"
                            else None
                        ),
                    ),
                ):
                    result = propose_design_spec(
                        {
                            "design_spec": _video_spec(
                                f"Primary quarantine source swap {race_phase}"
                            )
                        },
                        ctx=ctx,
                    )
                after_fds = _fd_count()

                diagnostic = result.error_message or ""
                self.assertEqual(result.status, "error")
                self.assertEqual(ctx.state, prior_state)
                self.assertEqual(canonical.read_bytes(), prior_bytes)
                self.assertEqual(after_fds, before_fds)
                quarantines = _rollback_orphans(archive)
                self.assertEqual(len(quarantines), 1)
                if race_phase == "after_identity_classification":
                    official_identity = archive.stat()
                    self.assertEqual(archive.read_bytes(), occupied_bytes)
                    self.assertFalse(owner_out.exists())
                    self.assertEqual(foreign_out.read_bytes(), foreign_bytes)
                    self.assertEqual(quarantines[0].read_bytes(), proposed_bytes)
                else:
                    official_identity = None
                    self.assertFalse(archive.exists())
                    self.assertTrue(owner_out.is_file())
                    self.assertEqual(owner_out.read_bytes(), proposed_bytes)
                    self.assertFalse(foreign_out.exists())
                    self.assertEqual(quarantines[0].read_bytes(), foreign_bytes)
                self.assertIn("positive device/inode mismatch", diagnostic)
                self.assertIn("moved a foreign entry", diagnostic)
                self.assertIn("no reverse recovery was attempted", diagnostic)
                self.assertNotIn("restored", diagnostic)
                self.assertNotIn("preserved", diagnostic)
                self.assertNotIn("kept", diagnostic)

                occupied_out = archive.parent / "later-official-out.json"
                if archive.exists():
                    archive.rename(occupied_out)
                    moved_identity = occupied_out.stat()
                    assert official_identity is not None
                    self.assertEqual(
                        (moved_identity.st_dev, moved_identity.st_ino),
                        (official_identity.st_dev, official_identity.st_ino),
                    )
                    self.assertEqual(occupied_out.read_bytes(), occupied_bytes)

                retry = propose_design_spec(
                    {
                        "design_spec": _video_spec(
                            f"Retry after primary quarantine swap {race_phase}"
                        )
                    },
                    ctx=ctx,
                )
                self.assertEqual(retry.status, "ok")
                self.assertEqual(ctx.state.get("spec_revision_count"), 2)
                self.assertEqual(
                    quarantines[0].read_bytes(),
                    proposed_bytes
                    if race_phase == "after_identity_classification"
                    else foreign_bytes,
                )
                self.assertEqual(_fd_count(), before_fds)

    def test_posix_primary_post_quarantine_identity_is_classified_from_retained_evidence(
        self,
    ) -> None:
        if os.name != "posix" or not Path("/dev/fd").is_dir():
            self.skipTest("POSIX retained quarantine and fd-count probe")
        for identity_state in ("owned", "mismatch", "ambiguous"):
            with (
                self.subTest(identity_state=identity_state),
                tempfile.TemporaryDirectory() as raw_tmp,
            ):
                root = Path(raw_tmp)
                ctx, prior_bytes = _prior_video_context(root)
                prior_state = deepcopy(ctx.state)
                archive = root / "specs" / "design_spec_02.json"
                canonical = root / "design_spec.json"
                owner_out = archive.parent / "owner-renamed-out.json"
                foreign = archive.parent / "foreign-winner.json"
                foreign_bytes = b'{"foreign":"source-boundary-winner"}'
                proposed_spec = DesignSpec.model_validate(
                    _video_spec("Post-quarantine identity")
                )
                proposed_bytes = _encoded(
                    {
                        "artifact_type": "video",
                        "is_revision": True,
                        "revision": 2,
                        "parent_revision": 1,
                        "parent_design_spec_sha256": ctx.state["design_spec_sha256"],
                        "design_spec_sha256": design_spec_sha256(proposed_spec),
                        "design_spec": proposed_spec.model_dump(mode="json"),
                    }
                )
                real_stat = persistence.os.stat
                real_rename = persistence.os.rename
                real_no_replace = persistence._rename_no_replace_at
                quarantine_stat_calls = 0
                swapped = False

                def inject_quarantine_stat_error(
                    path: object,
                    *args: object,
                    **kwargs: object,
                ) -> os.stat_result:
                    nonlocal quarantine_stat_calls
                    result = real_stat(path, *args, **kwargs)
                    name = os.fsdecode(os.fspath(path))
                    if (
                        kwargs.get("dir_fd") is not None
                        and name.startswith(f".{archive.name}.")
                        and name.endswith(".rollback-orphan")
                    ):
                        quarantine_stat_calls += 1
                        if quarantine_stat_calls == 1:
                            raise OSError(
                                errno.EIO,
                                "injected post-quarantine verification EIO",
                            )
                        if identity_state == "ambiguous" and quarantine_stat_calls == 2:
                            raise OSError(
                                errno.EIO,
                                "injected quarantine classification EIO",
                            )
                    return result

                def swap_source_before_quarantine_rename(
                    *,
                    src_dir_fd: int,
                    src_name: str,
                    dst_dir_fd: int,
                    dst_name: str,
                ) -> None:
                    nonlocal swapped
                    if not swapped and src_name == archive.name:
                        swapped = True
                        real_rename(archive, owner_out)
                        foreign.write_bytes(foreign_bytes)
                        real_rename(foreign, archive)
                    real_no_replace(
                        src_dir_fd=src_dir_fd,
                        src_name=src_name,
                        dst_dir_fd=dst_dir_fd,
                        dst_name=dst_name,
                    )

                before_fds = _fd_count()
                with ExitStack() as stack:
                    stack.enter_context(_fail_canonical_replace())
                    if identity_state == "mismatch":
                        stack.enter_context(
                            mock.patch.object(
                                persistence,
                                "_rename_no_replace_at",
                                side_effect=swap_source_before_quarantine_rename,
                            )
                        )
                    else:
                        stack.enter_context(
                            mock.patch.object(
                                persistence.os,
                                "stat",
                                side_effect=inject_quarantine_stat_error,
                            )
                        )
                    result = propose_design_spec(
                        {"design_spec": _video_spec("Post-quarantine identity")},
                        ctx=ctx,
                    )
                after_fds = _fd_count()

                diagnostic = result.error_message or ""
                self.assertEqual(result.status, "error")
                self.assertEqual(ctx.state, prior_state)
                self.assertEqual(canonical.read_bytes(), prior_bytes)
                self.assertEqual(after_fds, before_fds)
                self.assertFalse(archive.exists())
                quarantines = _rollback_orphans(archive)
                self.assertEqual(len(quarantines), 1)
                if identity_state == "mismatch":
                    self.assertTrue(swapped)
                    self.assertEqual(owner_out.read_bytes(), proposed_bytes)
                    self.assertEqual(quarantines[0].read_bytes(), foreign_bytes)
                    self.assertIn("positive device/inode mismatch", diagnostic)
                    self.assertIn("moved a foreign entry", diagnostic)
                    self.assertIn("no reverse recovery was attempted", diagnostic)
                else:
                    self.assertIn(
                        "injected post-quarantine verification EIO",
                        diagnostic,
                    )
                    self.assertEqual(quarantines[0].read_bytes(), proposed_bytes)
                    if identity_state == "owned":
                        self.assertIn("owned binding", diagnostic)
                        self.assertIn("integrity verification failed", diagnostic)
                    else:
                        self.assertIn("did not attempt reverse recovery", diagnostic)
                        self.assertIn("identity was uncertain", diagnostic)
                        self.assertNotIn("kept the entry in quarantine", diagnostic)
                        self.assertIn(
                            "injected quarantine classification EIO",
                            diagnostic,
                        )
                self.assertNotIn("restored", diagnostic)
                self.assertNotIn("preserved", diagnostic)
                self.assertNotIn("kept", diagnostic)

                retry = propose_design_spec(
                    {"design_spec": _video_spec(f"Retry after {identity_state}")},
                    ctx=ctx,
                )
                self.assertEqual(retry.status, "ok")
                self.assertEqual(ctx.state.get("spec_revision_count"), 2)
                self.assertEqual(
                    quarantines[0].read_bytes(),
                    foreign_bytes if identity_state == "mismatch" else proposed_bytes,
                )
                self.assertEqual(_fd_count(), before_fds)

    def test_posix_primary_same_inode_corruption_stays_quarantined_for_retry(
        self,
    ) -> None:
        if os.name != "posix" or not Path("/dev/fd").is_dir():
            self.skipTest("POSIX retained quarantine and fd-count probe")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            ctx, prior_bytes = _prior_video_context(root)
            prior_state = deepcopy(ctx.state)
            archive = root / "specs" / "design_spec_02.json"
            canonical = root / "design_spec.json"
            proposed_spec = DesignSpec.model_validate(
                _video_spec("Same-inode primary corruption")
            )
            proposed_bytes = _encoded(
                {
                    "artifact_type": "video",
                    "is_revision": True,
                    "revision": 2,
                    "parent_revision": 1,
                    "parent_design_spec_sha256": ctx.state["design_spec_sha256"],
                    "design_spec_sha256": design_spec_sha256(proposed_spec),
                    "design_spec": proposed_spec.model_dump(mode="json"),
                }
            )
            corrupted_bytes = b"!" * len(proposed_bytes)
            corrupted_identity: tuple[int, int] | None = None
            real_verify = persistence._verify_posix_entry

            def corrupt_quarantine_then_verify(
                *,
                directory: int,
                name: str,
                descriptor: int | None,
                publication: persistence._ArchivePublication,
                data: bytes,
                allowed_links: frozenset[int] = frozenset({1}),
            ) -> None:
                nonlocal corrupted_identity
                if (
                    corrupted_identity is None
                    and name.startswith(f".{archive.name}.")
                    and name.endswith(".rollback-orphan")
                ):
                    before = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    quarantine = archive.parent / name
                    quarantine.write_bytes(corrupted_bytes)
                    after = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    self.assertEqual(
                        (after.st_dev, after.st_ino),
                        (before.st_dev, before.st_ino),
                    )
                    corrupted_identity = (after.st_dev, after.st_ino)
                real_verify(
                    directory=directory,
                    name=name,
                    descriptor=descriptor,
                    publication=publication,
                    data=data,
                    allowed_links=allowed_links,
                )

            before_fds = _fd_count()
            with _fail_canonical_replace(), mock.patch.object(
                persistence,
                "_verify_posix_entry",
                side_effect=corrupt_quarantine_then_verify,
            ):
                result = propose_design_spec(
                    {"design_spec": _video_spec("Same-inode primary corruption")},
                    ctx=ctx,
                )
            after_fds = _fd_count()

            diagnostic = result.error_message or ""
            self.assertEqual(result.status, "error")
            self.assertEqual(ctx.state, prior_state)
            self.assertEqual(canonical.read_bytes(), prior_bytes)
            self.assertEqual(after_fds, before_fds)
            self.assertFalse(archive.exists())
            quarantines = _rollback_orphans(archive)
            self.assertEqual(len(quarantines), 1)
            self.assertEqual(quarantines[0].read_bytes(), corrupted_bytes)
            quarantined = quarantines[0].stat()
            self.assertEqual(
                (quarantined.st_dev, quarantined.st_ino),
                corrupted_identity,
            )
            self.assertIn("owned binding", diagnostic)
            self.assertIn("integrity verification failed", diagnostic)
            self.assertNotIn("foreign entry was restored", diagnostic)

            retry = propose_design_spec(
                {"design_spec": _video_spec("Retry after primary corruption")},
                ctx=ctx,
            )
            self.assertEqual(retry.status, "ok")
            self.assertEqual(ctx.state.get("spec_revision_count"), 2)
            self.assertEqual(_fd_count(), before_fds)

    def test_posix_primary_identity_probe_failures_never_reverse_quarantine(
        self,
    ) -> None:
        if os.name != "posix" or not Path("/dev/fd").is_dir():
            self.skipTest("POSIX retained quarantine and fd-count probe")
        faults = (
            "fstat_eio",
            "publication_identity_mismatch",
            "stat_eio",
            "stat_missing",
            "read_eio",
        )
        for fault in faults:
            with (
                self.subTest(fault=fault),
                tempfile.TemporaryDirectory() as raw_tmp,
            ):
                root = Path(raw_tmp)
                ctx, prior_bytes = _prior_video_context(root)
                prior_state = deepcopy(ctx.state)
                archive = root / "specs" / "design_spec_02.json"
                canonical = root / "design_spec.json"
                proposed_spec = DesignSpec.model_validate(
                    _video_spec(f"Primary identity fault {fault}")
                )
                proposed_bytes = _encoded(
                    {
                        "artifact_type": "video",
                        "is_revision": True,
                        "revision": 2,
                        "parent_revision": 1,
                        "parent_design_spec_sha256": ctx.state["design_spec_sha256"],
                        "design_spec_sha256": design_spec_sha256(proposed_spec),
                        "design_spec": proposed_spec.model_dump(mode="json"),
                    }
                )

                before_fds = _fd_count()
                with (
                    _fail_canonical_replace(),
                    _inject_post_quarantine_identity_fault(archive, fault),
                ):
                    result = propose_design_spec(
                        {
                            "design_spec": _video_spec(
                                f"Primary identity fault {fault}"
                            )
                        },
                        ctx=ctx,
                    )
                after_fds = _fd_count()

                diagnostic = result.error_message or ""
                self.assertEqual(result.status, "error")
                self.assertEqual(ctx.state, prior_state)
                self.assertEqual(canonical.read_bytes(), prior_bytes)
                self.assertEqual(after_fds, before_fds)
                self.assertFalse(archive.exists())
                quarantines = _rollback_orphans(archive)
                self.assertEqual(len(quarantines), 1)
                self.assertEqual(quarantines[0].read_bytes(), proposed_bytes)
                self.assertNotIn("foreign entry was restored", diagnostic)
                if fault == "read_eio":
                    self.assertIn("owned binding", diagnostic)
                    self.assertIn("integrity verification failed", diagnostic)
                    self.assertIn("injected retained publication read EIO", diagnostic)
                else:
                    self.assertIn(
                        "quarantine binding was unavailable or its identity was uncertain",
                        diagnostic,
                    )
                    self.assertIn("did not attempt reverse recovery", diagnostic)
                    self.assertNotIn("kept the entry in quarantine", diagnostic)

                retry = propose_design_spec(
                    {"design_spec": _video_spec(f"Retry after primary {fault}")},
                    ctx=ctx,
                )
                self.assertEqual(retry.status, "ok")
                self.assertEqual(ctx.state.get("spec_revision_count"), 2)
                self.assertEqual(_fd_count(), before_fds)

    def test_posix_legacy_post_quarantine_identity_is_classified_from_retained_evidence(
        self,
    ) -> None:
        if os.name != "posix" or not Path("/dev/fd").is_dir():
            self.skipTest("POSIX retained quarantine and fd-count probe")
        for identity_state in ("owned", "mismatch", "ambiguous"):
            with (
                self.subTest(identity_state=identity_state),
                tempfile.TemporaryDirectory() as raw_tmp,
            ):
                root = Path(raw_tmp)
                root.mkdir(exist_ok=True)
                canonical = root / "design_spec.json"
                canonical.write_bytes(b'{"prior":true}')
                prior_bytes = canonical.read_bytes()
                archive = root / "specs" / "design_spec_02.json"
                archive.parent.mkdir()
                payload = {"revision": 2, "design_spec": {"title": "owned"}}
                owned_bytes = _encoded(payload)
                archive.write_bytes(owned_bytes)
                metadata = os.stat(archive)
                publication = persistence._ArchivePublication(
                    device=metadata.st_dev,
                    inode=metadata.st_ino,
                    size=metadata.st_size,
                    modified_ns=metadata.st_mtime_ns,
                )
                owner_out = archive.parent / "owner-renamed-out.json"
                foreign = archive.parent / "foreign-winner.json"
                foreign_bytes = b'{"foreign":"legacy-source-boundary-winner"}'
                real_stat = persistence.os.stat
                real_supports_dir_fd = persistence.os.supports_dir_fd
                real_rename = persistence.os.rename
                real_no_replace = persistence._rename_no_replace_at
                quarantine_stat_calls = 0
                swapped = False

                def inject_quarantine_stat_error(
                    path: object,
                    *args: object,
                    **kwargs: object,
                ) -> os.stat_result:
                    nonlocal quarantine_stat_calls
                    result = real_stat(path, *args, **kwargs)
                    name = os.fsdecode(os.fspath(path))
                    if (
                        kwargs.get("dir_fd") is not None
                        and name.startswith(f".{archive.name}.")
                        and name.endswith(".rollback-orphan")
                    ):
                        quarantine_stat_calls += 1
                        if quarantine_stat_calls == 1:
                            raise OSError(
                                errno.EIO,
                                "injected post-quarantine verification EIO",
                            )
                        if identity_state == "ambiguous" and quarantine_stat_calls == 2:
                            raise OSError(
                                errno.EIO,
                                "injected quarantine classification EIO",
                            )
                    return result

                def swap_source_before_quarantine_rename(
                    *,
                    src_dir_fd: int,
                    src_name: str,
                    dst_dir_fd: int,
                    dst_name: str,
                ) -> None:
                    nonlocal swapped
                    if not swapped and src_name == archive.name:
                        swapped = True
                        real_rename(archive, owner_out)
                        foreign.write_bytes(foreign_bytes)
                        real_rename(foreign, archive)
                    real_no_replace(
                        src_dir_fd=src_dir_fd,
                        src_name=src_name,
                        dst_dir_fd=dst_dir_fd,
                        dst_name=dst_name,
                    )

                before_fds = _fd_count()
                with ExitStack() as stack:
                    if identity_state == "mismatch":
                        stack.enter_context(
                            mock.patch.object(
                                persistence,
                                "_rename_no_replace_at",
                                side_effect=swap_source_before_quarantine_rename,
                            )
                        )
                    else:
                        stat_mock = mock.Mock(
                            side_effect=inject_quarantine_stat_error,
                        )
                        stack.enter_context(
                            mock.patch.object(
                                persistence.os,
                                "stat",
                                stat_mock,
                            )
                        )
                        stack.enter_context(
                            mock.patch.object(
                                persistence.os,
                                "supports_dir_fd",
                                frozenset(set(real_supports_dir_fd) | {stat_mock}),
                            )
                        )
                    with self.assertRaises(OSError) as raised:
                        persistence._release_owned_archive_if_unchanged(
                            archive,
                            owned_bytes,
                            publication,
                        )
                after_fds = _fd_count()

                diagnostic = str(raised.exception)
                self.assertEqual(canonical.read_bytes(), prior_bytes)
                self.assertEqual(after_fds, before_fds)
                self.assertFalse(archive.exists())
                quarantines = _rollback_orphans(archive)
                self.assertEqual(len(quarantines), 1)
                if identity_state == "mismatch":
                    self.assertTrue(swapped)
                    self.assertEqual(owner_out.read_bytes(), owned_bytes)
                    self.assertEqual(quarantines[0].read_bytes(), foreign_bytes)
                    self.assertIn("positive device/inode mismatch", diagnostic)
                    self.assertIn("moved a foreign entry", diagnostic)
                    self.assertIn("no reverse recovery was attempted", diagnostic)
                else:
                    self.assertIn(
                        "injected post-quarantine verification EIO",
                        diagnostic,
                    )
                    self.assertEqual(quarantines[0].read_bytes(), owned_bytes)
                    if identity_state == "owned":
                        self.assertIn("owned binding", diagnostic)
                        self.assertIn("integrity verification failed", diagnostic)
                    else:
                        self.assertIn("did not attempt reverse recovery", diagnostic)
                        self.assertIn("identity was uncertain", diagnostic)
                        self.assertNotIn("kept the entry in quarantine", diagnostic)
                        self.assertIn(
                            "injected quarantine classification EIO",
                            diagnostic,
                        )
                self.assertNotIn("restored", diagnostic)
                self.assertNotIn("preserved", diagnostic)
                self.assertNotIn("kept", diagnostic)

                retry_payload = {
                    "revision": 2,
                    "design_spec": {"title": f"retry-{identity_state}"},
                }
                _persist(root, retry_payload)
                self.assertEqual(archive.read_bytes(), _encoded(retry_payload))
                self.assertEqual(canonical.read_bytes(), _encoded(retry_payload))
                self.assertEqual(
                    quarantines[0].read_bytes(),
                    foreign_bytes if identity_state == "mismatch" else owned_bytes,
                )
                self.assertEqual(_fd_count(), before_fds)

    def test_posix_legacy_same_inode_corruption_stays_quarantined_for_retry(
        self,
    ) -> None:
        if os.name != "posix" or not Path("/dev/fd").is_dir():
            self.skipTest("POSIX retained quarantine and fd-count probe")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            canonical = root / "design_spec.json"
            canonical.write_bytes(b'{"prior":true}')
            prior_bytes = canonical.read_bytes()
            archive = root / "specs" / "design_spec_02.json"
            archive.parent.mkdir()
            payload = {"revision": 2, "design_spec": {"title": "owned"}}
            owned_bytes = _encoded(payload)
            archive.write_bytes(owned_bytes)
            metadata = os.stat(archive)
            publication = persistence._ArchivePublication(
                device=metadata.st_dev,
                inode=metadata.st_ino,
                size=metadata.st_size,
                modified_ns=metadata.st_mtime_ns,
            )
            corrupted_bytes = b"!" * len(owned_bytes)
            corrupted_identity: tuple[int, int] | None = None
            real_verify = persistence._verify_posix_entry

            def corrupt_quarantine_then_verify(
                *,
                directory: int,
                name: str,
                descriptor: int | None,
                publication: persistence._ArchivePublication,
                data: bytes,
                allowed_links: frozenset[int] = frozenset({1}),
            ) -> None:
                nonlocal corrupted_identity
                if (
                    corrupted_identity is None
                    and name.startswith(f".{archive.name}.")
                    and name.endswith(".rollback-orphan")
                ):
                    before = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    quarantine = archive.parent / name
                    quarantine.write_bytes(corrupted_bytes)
                    after = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    self.assertEqual(
                        (after.st_dev, after.st_ino),
                        (before.st_dev, before.st_ino),
                    )
                    corrupted_identity = (after.st_dev, after.st_ino)
                real_verify(
                    directory=directory,
                    name=name,
                    descriptor=descriptor,
                    publication=publication,
                    data=data,
                    allowed_links=allowed_links,
                )

            before_fds = _fd_count()
            with mock.patch.object(
                persistence,
                "_verify_posix_entry",
                side_effect=corrupt_quarantine_then_verify,
            ):
                with self.assertRaises(OSError) as raised:
                    persistence._release_owned_archive_if_unchanged(
                        archive,
                        owned_bytes,
                        publication,
                    )
            after_fds = _fd_count()

            diagnostic = str(raised.exception)
            self.assertEqual(canonical.read_bytes(), prior_bytes)
            self.assertEqual(after_fds, before_fds)
            self.assertFalse(archive.exists())
            quarantines = _rollback_orphans(archive)
            self.assertEqual(len(quarantines), 1)
            self.assertEqual(quarantines[0].read_bytes(), corrupted_bytes)
            quarantined = quarantines[0].stat()
            self.assertEqual(
                (quarantined.st_dev, quarantined.st_ino),
                corrupted_identity,
            )
            self.assertIn("owned binding", diagnostic)
            self.assertIn("integrity verification failed", diagnostic)
            self.assertNotIn("foreign entry was restored", diagnostic)

            retry_payload = {
                "revision": 2,
                "design_spec": {"title": "retry-after-legacy-corruption"},
            }
            _persist(root, retry_payload)
            self.assertEqual(archive.read_bytes(), _encoded(retry_payload))
            self.assertEqual(canonical.read_bytes(), _encoded(retry_payload))
            self.assertEqual(_fd_count(), before_fds)

    def test_posix_legacy_identity_probe_failures_never_reverse_quarantine(
        self,
    ) -> None:
        if os.name != "posix" or not Path("/dev/fd").is_dir():
            self.skipTest("POSIX retained quarantine and fd-count probe")
        faults = (
            "fstat_eio",
            "publication_identity_mismatch",
            "stat_eio",
            "stat_missing",
            "read_eio",
        )
        for fault in faults:
            with (
                self.subTest(fault=fault),
                tempfile.TemporaryDirectory() as raw_tmp,
            ):
                root = Path(raw_tmp)
                canonical = root / "design_spec.json"
                canonical.write_bytes(b'{"prior":true}')
                prior_bytes = canonical.read_bytes()
                archive = root / "specs" / "design_spec_02.json"
                archive.parent.mkdir()
                payload = {"revision": 2, "design_spec": {"title": "owned"}}
                owned_bytes = _encoded(payload)
                archive.write_bytes(owned_bytes)
                metadata = os.stat(archive)
                publication = persistence._ArchivePublication(
                    device=metadata.st_dev,
                    inode=metadata.st_ino,
                    size=metadata.st_size,
                    modified_ns=metadata.st_mtime_ns,
                )

                before_fds = _fd_count()
                with _inject_post_quarantine_identity_fault(archive, fault):
                    with self.assertRaises(OSError) as raised:
                        persistence._release_owned_archive_if_unchanged(
                            archive,
                            owned_bytes,
                            publication,
                        )
                after_fds = _fd_count()

                diagnostic = str(raised.exception)
                self.assertEqual(canonical.read_bytes(), prior_bytes)
                self.assertEqual(after_fds, before_fds)
                self.assertFalse(archive.exists())
                quarantines = _rollback_orphans(archive)
                self.assertEqual(len(quarantines), 1)
                self.assertEqual(quarantines[0].read_bytes(), owned_bytes)
                self.assertNotIn("foreign entry was restored", diagnostic)
                if fault == "read_eio":
                    self.assertIn("owned binding", diagnostic)
                    self.assertIn("integrity verification failed", diagnostic)
                    self.assertIn("injected retained publication read EIO", diagnostic)
                else:
                    self.assertIn(
                        "quarantine binding was unavailable or its identity was uncertain",
                        diagnostic,
                    )
                    self.assertIn("did not attempt reverse recovery", diagnostic)
                    self.assertNotIn("kept the entry in quarantine", diagnostic)

                retry_payload = {
                    "revision": 2,
                    "design_spec": {"title": f"retry-after-legacy-{fault}"},
                }
                _persist(root, retry_payload)
                self.assertEqual(archive.read_bytes(), _encoded(retry_payload))
                self.assertEqual(canonical.read_bytes(), _encoded(retry_payload))
                self.assertEqual(_fd_count(), before_fds)

    def test_posix_legacy_post_quarantine_source_swaps_never_trigger_recovery(
        self,
    ) -> None:
        if os.name != "posix" or not Path("/dev/fd").is_dir():
            self.skipTest("POSIX quarantine source identity and fd-count probe")
        for race_phase in (
            "after_identity_classification",
            "during_reverse_rename",
        ):
            with (
                self.subTest(race_phase=race_phase),
                tempfile.TemporaryDirectory() as raw_tmp,
            ):
                root = Path(raw_tmp)
                canonical = root / "design_spec.json"
                canonical.write_bytes(b'{"prior":true}')
                prior_bytes = canonical.read_bytes()
                archive = root / "specs" / "design_spec_02.json"
                archive.parent.mkdir()
                payload = {"revision": 2, "design_spec": {"title": "owned"}}
                owned_bytes = _encoded(payload)
                archive.write_bytes(owned_bytes)
                metadata = os.stat(archive)
                publication = persistence._ArchivePublication(
                    device=metadata.st_dev,
                    inode=metadata.st_ino,
                    size=metadata.st_size,
                    modified_ns=metadata.st_mtime_ns,
                )
                owner_out = archive.parent / "retained-owner-out.json"
                foreign_out = archive.parent / "classified-foreign-out.json"
                foreign_bytes = b'{"foreign":"classified-source"}'
                occupied_bytes = b'{"foreign":"later-official"}'

                before_fds = _fd_count()
                with _rebind_quarantine_source_after_classification(
                    archive,
                    owner_out=owner_out,
                    foreign_out=foreign_out,
                    foreign_bytes=foreign_bytes,
                    race_phase=race_phase,
                    official_bytes=(
                        occupied_bytes
                        if race_phase == "after_identity_classification"
                        else None
                    ),
                ):
                    with self.assertRaises(OSError) as raised:
                        persistence._release_owned_archive_if_unchanged(
                            archive,
                            owned_bytes,
                            publication,
                        )
                after_fds = _fd_count()

                diagnostic = str(raised.exception)
                self.assertEqual(canonical.read_bytes(), prior_bytes)
                self.assertEqual(after_fds, before_fds)
                quarantines = _rollback_orphans(archive)
                self.assertEqual(len(quarantines), 1)
                if race_phase == "after_identity_classification":
                    official_identity = archive.stat()
                    self.assertEqual(archive.read_bytes(), occupied_bytes)
                    self.assertFalse(owner_out.exists())
                    self.assertEqual(foreign_out.read_bytes(), foreign_bytes)
                    self.assertEqual(quarantines[0].read_bytes(), owned_bytes)
                else:
                    official_identity = None
                    self.assertFalse(archive.exists())
                    self.assertTrue(owner_out.is_file())
                    self.assertEqual(owner_out.read_bytes(), owned_bytes)
                    self.assertFalse(foreign_out.exists())
                    self.assertEqual(quarantines[0].read_bytes(), foreign_bytes)
                self.assertIn("positive device/inode mismatch", diagnostic)
                self.assertIn("moved a foreign entry", diagnostic)
                self.assertIn("no reverse recovery was attempted", diagnostic)
                self.assertNotIn("restored", diagnostic)
                self.assertNotIn("preserved", diagnostic)
                self.assertNotIn("kept", diagnostic)

                occupied_out = archive.parent / "later-official-out.json"
                if archive.exists():
                    archive.rename(occupied_out)
                    moved_identity = occupied_out.stat()
                    assert official_identity is not None
                    self.assertEqual(
                        (moved_identity.st_dev, moved_identity.st_ino),
                        (official_identity.st_dev, official_identity.st_ino),
                    )
                    self.assertEqual(occupied_out.read_bytes(), occupied_bytes)

                retry_payload = {
                    "revision": 2,
                    "design_spec": {
                        "title": f"retry-after-quarantine-swap-{race_phase}"
                    },
                }
                _persist(root, retry_payload)
                self.assertEqual(archive.read_bytes(), _encoded(retry_payload))
                self.assertEqual(canonical.read_bytes(), _encoded(retry_payload))
                self.assertEqual(
                    quarantines[0].read_bytes(),
                    owned_bytes
                    if race_phase == "after_identity_classification"
                    else foreign_bytes,
                )
                self.assertEqual(_fd_count(), before_fds)

    def test_windows_delete_handle_blocks_before_mark_writers_and_renames(self) -> None:
        native = object.__new__(persistence._WindowsArchiveIO)
        descriptor = os.open(os.devnull, os.O_RDONLY)
        native.msvcrt = SimpleNamespace(open_osfhandle=lambda _handle, _flags: descriptor)
        native.kernel32 = SimpleNamespace(CloseHandle=lambda _handle: True)
        native._open = mock.Mock(return_value=12345)

        opened = native.open_file(Path("archive.json"), delete_access=True)
        try:
            self.assertEqual(opened, descriptor)
            native._open.assert_called_once_with(
                Path("archive.json"),
                access=(
                    native._GENERIC_READ
                    | native._FILE_READ_ATTRIBUTES
                    | native._DELETE
                ),
                flags=native._FILE_FLAG_OPEN_REPARSE_POINT,
                share_access=native._FILE_SHARE_READ,
            )
        finally:
            os.close(descriptor)

    def test_windows_sidecar_open_is_native_nofollow_and_path_bound(self) -> None:
        native = object.__new__(persistence._WindowsArchiveIO)
        descriptor = os.open(os.devnull, os.O_RDONLY)
        native.msvcrt = SimpleNamespace(open_osfhandle=lambda _handle, _flags: descriptor)
        native.kernel32 = SimpleNamespace(CloseHandle=lambda _handle: True)
        native._open = mock.Mock(return_value=24680)

        opened = native.open_lock(Path(".design_spec.json.lock"))
        try:
            self.assertEqual(opened, descriptor)
            native._open.assert_called_once_with(
                Path(".design_spec.json.lock"),
                access=(
                    native._GENERIC_READ
                    | native._GENERIC_WRITE
                    | native._FILE_READ_ATTRIBUTES
                ),
                flags=native._FILE_FLAG_OPEN_REPARSE_POINT,
                share_access=(native._FILE_SHARE_READ | native._FILE_SHARE_WRITE),
                creation_disposition=native._OPEN_ALWAYS,
            )
        finally:
            os.close(descriptor)

    def test_windows_sidecar_rejects_reparse_hardlink_and_path_replacement(self) -> None:
        for mutation in ("reparse", "hardlink", "replace_identity"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(raw_tmp)
                canonical = root / "design_spec.json"
                lock_path = root / ".design_spec.json.lock"
                native = _FakeWindowsArchiveIO()
                replacement_bytes = b"foreign-lock"
                if mutation == "reparse":
                    target = root / "lock-target"
                    target.write_bytes(b"target")
                    lock_path.symlink_to(target.name)
                else:
                    lock_path.write_bytes(b"lock")
                    if mutation == "hardlink":
                        os.link(lock_path, root / "other-lock-link")
                    else:
                        def replace_lock_path() -> None:
                            replacement = root / "replacement-lock"
                            replacement.write_bytes(replacement_bytes)
                            os.replace(replacement, lock_path)

                        native.on_stat_path = replace_lock_path

                with (
                    mock.patch.object(
                        persistence,
                        "_LOCK_OS_NAME",
                        "nt",
                        create=True,
                    ),
                    mock.patch.object(
                        persistence,
                        "_windows_archive_io_factory",
                        return_value=native,
                    ),
                ):
                    with self.assertRaises(
                        persistence.DesignSpecPersistenceError,
                    ) as raised:
                        persistence.persist_design_spec_payload(
                            canonical_path=canonical,
                            archive_path=root / "specs" / "design_spec_02.json",
                            payload={"revision": 2},
                            before_archive_publish=lambda _path: None,
                        )

                self.assertEqual(raised.exception.phase, "canonical_lock")
                self.assertTrue(native.handles)
                self.assertTrue(all(handle.closed for handle in native.handles))
                self.assertFalse((root / "specs" / "design_spec_02.json").exists())
                if mutation == "reparse":
                    self.assertTrue(lock_path.is_symlink())
                elif mutation == "hardlink":
                    self.assertEqual(lock_path.stat().st_nlink, 2)
                else:
                    self.assertEqual(lock_path.read_bytes(), replacement_bytes)

    def test_windows_canonical_failure_releases_revision_for_next_payload(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            first = {"revision": 2, "design_spec": {"title": "first"}}
            second = {"revision": 2, "design_spec": {"title": "second"}}
            native = _FakeWindowsArchiveIO()
            real_atomic_write_json = persistence.atomic_write_json
            call_count = 0

            def fail_first_canonical(path: Path, payload: object) -> None:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise OSError("injected canonical write failure")
                real_atomic_write_json(path, payload)

            with (
                mock.patch.object(
                    persistence,
                    "_RUNTIME_OS_NAME",
                    "nt",
                    create=True,
                ),
                mock.patch.object(
                    persistence,
                    "_windows_archive_io_factory",
                    return_value=native,
                    create=True,
                ),
                mock.patch.object(persistence.os, "supports_dir_fd", frozenset()),
                mock.patch.object(
                    persistence,
                    "atomic_write_json",
                    side_effect=fail_first_canonical,
                ),
            ):
                with self.assertRaises(persistence.DesignSpecPersistenceError):
                    _persist(root, first)
                _persist(root, second)

            self.assertEqual(
                (root / "specs" / "design_spec_02.json").read_bytes(),
                _encoded(second),
            )
            self.assertEqual(
                (root / "design_spec.json").read_bytes(),
                _encoded(second),
            )
            self.assertTrue(native.flushed_parents)
            self.assertTrue(native.handles)
            self.assertTrue(all(handle.closed for handle in native.handles))

    def test_windows_post_replace_exception_accepts_crlf_canonical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            payload = {"revision": 2, "design_spec": {"title": "installed"}}
            native = _FakeWindowsArchiveIO()

            def write_crlf_then_fail(path: Path, _payload: object) -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(_encoded(payload).replace(b"\n", b"\r\n"))
                raise OSError("injected exception after canonical replace")

            with (
                mock.patch.object(
                    persistence,
                    "_RUNTIME_OS_NAME",
                    "nt",
                    create=True,
                ),
                mock.patch.object(
                    persistence,
                    "_windows_archive_io_factory",
                    return_value=native,
                    create=True,
                ),
                mock.patch.object(
                    persistence,
                    "atomic_write_json",
                    side_effect=write_crlf_then_fail,
                ),
            ):
                _persist(root, payload)

            self.assertEqual(
                (root / "specs" / "design_spec_02.json").read_bytes(),
                _encoded(payload),
            )
            self.assertEqual(
                (root / "design_spec.json").read_bytes(),
                _encoded(payload).replace(b"\n", b"\r\n"),
            )
            self.assertTrue(all(handle.closed for handle in native.handles))

    def test_windows_cleanup_rejects_reparse_hardlink_and_mutation(self) -> None:
        for mutation in ("reparse", "hardlink", "replace_identity", "change_bytes"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(raw_tmp)
                payload = {"revision": 2, "design_spec": {"title": "owned"}}
                archive = root / "specs" / "design_spec_02.json"
                native = _FakeWindowsArchiveIO()
                expected_bytes = _encoded(payload)

                def mutate_then_fail(_path: Path, _payload: object) -> None:
                    nonlocal expected_bytes
                    if mutation == "reparse":
                        winner = archive.parent / "winner.json"
                        winner.write_bytes(expected_bytes)
                        archive.unlink()
                        archive.symlink_to(winner.name)
                    elif mutation == "hardlink":
                        os.link(archive, archive.parent / "other-link.json")
                    elif mutation == "replace_identity":
                        replacement = archive.parent / "replacement.json"
                        replacement.write_bytes(expected_bytes)
                        os.replace(replacement, archive)
                    else:
                        expected_bytes = b'{"foreign":"winner"}'
                        archive.write_bytes(expected_bytes)
                    raise OSError("injected canonical write failure")

                with (
                    mock.patch.object(
                        persistence,
                        "_RUNTIME_OS_NAME",
                        "nt",
                        create=True,
                    ),
                    mock.patch.object(
                        persistence,
                        "_windows_archive_io_factory",
                        return_value=native,
                        create=True,
                    ),
                    mock.patch.object(persistence.os, "supports_dir_fd", frozenset()),
                    mock.patch.object(
                        persistence,
                        "atomic_write_json",
                        side_effect=mutate_then_fail,
                    ),
                ):
                    with self.assertRaises(
                        persistence.DesignSpecPersistenceError,
                    ) as raised:
                        _persist(root, payload)

                self.assertEqual(raised.exception.phase, "canonical_rollback")
                self.assertTrue(archive.exists())
                self.assertEqual(archive.read_bytes(), expected_bytes)
                self.assertTrue(all(handle.closed for handle in native.handles))
                self.assertFalse(native.flushed_parents)

    def test_windows_retained_handle_never_deletes_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            payload = {"revision": 2, "design_spec": {"title": "owned"}}
            archive = root / "specs" / "design_spec_02.json"
            native = _FakeWindowsArchiveIO()
            winner_bytes = b'{"foreign":"late-winner"}'

            def replace_after_retained_open() -> None:
                replacement = archive.parent / "late-winner.json"
                replacement.write_bytes(winner_bytes)
                os.replace(replacement, archive)

            native.on_read = replace_after_retained_open
            with (
                mock.patch.object(
                    persistence,
                    "_RUNTIME_OS_NAME",
                    "nt",
                    create=True,
                ),
                mock.patch.object(
                    persistence,
                    "_windows_archive_io_factory",
                    return_value=native,
                    create=True,
                ),
                mock.patch.object(persistence.os, "supports_dir_fd", frozenset()),
                mock.patch.object(
                    persistence,
                    "atomic_write_json",
                    side_effect=OSError("injected canonical write failure"),
                ),
            ):
                with self.assertRaises(
                    persistence.DesignSpecPersistenceError,
                ) as raised:
                    _persist(root, payload)

            self.assertEqual(raised.exception.phase, "canonical_rollback")
            self.assertEqual(archive.read_bytes(), winner_bytes)
            self.assertTrue(native.handles)
            self.assertTrue(all(handle.closed for handle in native.handles))
            self.assertFalse(native.flushed_parents)

    def test_unsupported_no_replace_backend_fails_before_payload_writes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            payload = {"revision": 2, "design_spec": {"title": "owned"}}
            with mock.patch.object(
                persistence,
                "_require_posix_no_replace_rename",
                side_effect=OSError(errno.ENOTSUP, "unsupported no-replace rename"),
            ):
                with self.assertRaises(
                    persistence.DesignSpecPersistenceError,
                ) as raised:
                    _persist(root, payload)

            self.assertEqual(raised.exception.phase, "canonical_lock")
            self.assertFalse((root / "specs").exists())
            self.assertFalse((root / "design_spec.json").exists())

    @unittest.skipUnless(os.name == "nt", "requires the real Windows API")
    def test_real_windows_sidecar_handle_blocks_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            lock_path = root / ".design_spec.json.lock"
            replacement = root / "replacement.lock"
            native = persistence._WindowsArchiveIO()
            handle = native.open_lock(lock_path)
            locked = False
            try:
                native.lock(handle)
                locked = True
                replacement.write_bytes(b"foreign-lock")
                with self.assertRaises(OSError):
                    os.replace(replacement, lock_path)
                self.assertTrue(
                    persistence._is_private_windows_file(native.metadata(handle))
                )
                self.assertTrue(
                    persistence._same_windows_file(
                        native.metadata(handle),
                        native.stat_path(lock_path),
                    )
                )
            finally:
                if locked:
                    native.unlock(handle)
                native.close(handle)

    @unittest.skipUnless(os.name == "nt", "requires the real Windows API")
    def test_real_windows_delete_handle_blocks_before_mark_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            archive = Path(raw_tmp) / "design_spec_02.json"
            archive.write_bytes(b"owned")
            renamed = archive.with_name("renamed.json")
            native = persistence._WindowsArchiveIO()
            handle = native.open_file(archive, delete_access=True)
            try:
                before = native.metadata(handle)
                self.assertEqual(native.read_bytes(handle), b"owned")
                self.assertEqual(native.metadata(handle), before)
                self.assertEqual(native.stat_path(archive), before)
                with self.assertRaises(OSError):
                    with archive.open("r+b"):
                        pass
                with self.assertRaises(OSError):
                    os.rename(archive, renamed)
                native.mark_delete(handle)
            finally:
                native.close(handle)

            self.assertFalse(archive.exists())
            self.assertFalse(renamed.exists())

    @unittest.skipUnless(os.name == "nt", "requires the real Windows API")
    def test_real_windows_revision_commit_round_trips_and_rejects_stale_cas(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            spec = DesignSpec.model_validate(_video_spec("windows-base"))
            spec_payload = spec.model_dump(mode="json")
            committed = persistence.commit_design_spec_revision(
                canonical_path=root / "design_spec.json",
                artifact_type=spec.artifact_type.value,
                design_spec=spec_payload,
                is_revision=False,
                expected_base_revision=None,
                expected_base_sha256=None,
            )

            restored = persistence.load_design_spec_canonical(
                root / "design_spec.json"
            )
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.revision, committed.revision)
            self.assertEqual(restored.design_spec, spec_payload)

            with self.assertRaises(persistence.DesignSpecPersistenceError) as raised:
                persistence.commit_design_spec_revision(
                    canonical_path=root / "design_spec.json",
                    artifact_type=spec.artifact_type.value,
                    design_spec=DesignSpec.model_validate(
                        _video_spec("windows-stale")
                    ).model_dump(mode="json"),
                    is_revision=True,
                    expected_base_revision=None,
                    expected_base_sha256=None,
                )
            self.assertEqual(raised.exception.phase, "cas")

    def test_canonical_failure_releases_owned_archive_for_reused_revision(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            first = {"revision": 2, "design_spec": {"title": "first"}}
            second = {"revision": 2, "design_spec": {"title": "second"}}
            call_count = 0

            def fail_first_canonical(
                real_replace,
                source: object,
                destination: object,
                args: tuple[object, ...],
                kwargs: dict[str, object],
            ) -> None:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise OSError("injected canonical write failure")
                real_replace(source, destination, *args, **kwargs)

            with _canonical_replace_interceptor(fail_first_canonical):
                with self.assertRaises(persistence.DesignSpecPersistenceError):
                    _persist(root, first)
                _persist(root, second)

            archive = root / "specs" / "design_spec_02.json"
            canonical = root / "design_spec.json"
            self.assertEqual(archive.read_bytes(), _encoded(second))
            self.assertEqual(canonical.read_bytes(), _encoded(second))

    def test_canonical_failure_never_deletes_preexisting_identical_archive(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            payload = {"revision": 2, "design_spec": {"title": "existing"}}
            archive = root / "specs" / "design_spec_02.json"
            archive.parent.mkdir(parents=True)
            archive.write_bytes(_encoded(payload))
            before = archive.stat()

            with _fail_canonical_replace():
                with self.assertRaises(persistence.DesignSpecPersistenceError):
                    _persist(root, payload)

            after = archive.stat()
            self.assertEqual(archive.read_bytes(), _encoded(payload))
            self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))

    def test_canonical_failure_preserves_unsafe_or_mutated_archive_entries(self) -> None:
        for mutation in ("symlink", "hardlink", "replace_identity", "change_bytes"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(raw_tmp)
                payload = {"revision": 2, "design_spec": {"title": "owned"}}
                archive = root / "specs" / "design_spec_02.json"
                replacement_identity: tuple[int, int] | None = None
                expected_bytes = _encoded(payload)

                def mutate_archive() -> None:
                    nonlocal replacement_identity, expected_bytes
                    if mutation == "symlink":
                        winner = archive.parent / "winner.json"
                        winner.write_bytes(expected_bytes)
                        archive.unlink()
                        archive.symlink_to(winner.name)
                    elif mutation == "hardlink":
                        os.link(archive, archive.parent / "other-link.json")
                    elif mutation == "replace_identity":
                        replacement = archive.parent / "replacement.json"
                        replacement.write_bytes(_encoded(payload))
                        os.replace(replacement, archive)
                        metadata = archive.stat()
                        replacement_identity = (metadata.st_dev, metadata.st_ino)
                    else:
                        expected_bytes = b'{"foreign":"winner"}'
                        archive.write_bytes(expected_bytes)
                with _fail_canonical_replace(before_failure=mutate_archive):
                    with self.assertRaises(persistence.DesignSpecPersistenceError):
                        _persist(root, payload)

                self.assertTrue(archive.is_file())
                self.assertEqual(archive.read_bytes(), expected_bytes)
                if mutation == "symlink":
                    self.assertTrue(archive.is_symlink())
                if mutation == "hardlink":
                    self.assertTrue((archive.parent / "other-link.json").is_file())
                if replacement_identity is not None:
                    metadata = archive.stat()
                    self.assertEqual(
                        (metadata.st_dev, metadata.st_ino),
                        replacement_identity,
                    )

    def test_post_replace_canonical_exception_is_recovered_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            payload = {"revision": 2, "design_spec": {"title": "installed"}}
            def write_then_fail(
                real_replace,
                source: object,
                destination: object,
                args: tuple[object, ...],
                kwargs: dict[str, object],
            ) -> None:
                real_replace(source, destination, *args, **kwargs)
                raise OSError("injected exception after canonical replace")

            with _canonical_replace_interceptor(write_then_fail):
                _persist(root, payload)

            self.assertEqual(
                (root / "specs" / "design_spec_02.json").read_bytes(),
                _encoded(payload),
            )
            self.assertEqual(
                (root / "design_spec.json").read_bytes(),
                _encoded(payload),
            )


if __name__ == "__main__":
    unittest.main()
