from __future__ import annotations

import asyncio
import json
import hashlib
import multiprocessing
import os
from pathlib import Path
import queue
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from uuid import uuid4

from autodesign import paper_bundle_jobs as bundle_jobs
from autodesign.paper_bundle_jobs import (
    ChildStateSnapshot,
    InvalidPaperBundle,
    PaperBundleBarrierClosed,
    PaperBundleChildDescriptor,
    PaperBundleConflict,
    PaperBundleInputSlot,
    PaperBundleJobStore,
    PaperBundleNotFound,
    StalePaperBundleRevision,
)


ARTIFACT_TYPES = ("poster", "deck", "landing", "video")


class _FakeWindowsNativeIO:
    def __init__(self) -> None:
        self.open_lock_calls = 0
        self.flush_directory_calls = 0

    @staticmethod
    def _stat(path: Path):
        metadata = os.lstat(path)
        if path.is_symlink():
            raise InvalidPaperBundle(f"reparse points are not allowed: {path}")
        return bundle_jobs._WindowsStat(
            st_mode=metadata.st_mode,
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino,
            st_nlink=metadata.st_nlink,
            st_file_attributes=0,
        )

    def assert_supported_parent(self, path: Path) -> None:
        return None

    def open_directory(
        self,
        path: Path,
        *,
        require_ntfs: bool = True,
    ):
        metadata = self._stat(path)
        if not stat.S_ISDIR(metadata.st_mode):
            raise InvalidPaperBundle(f"unsafe directory: {path}")
        return bundle_jobs._WindowsDirectoryHandle(
            raw_handle=hash((metadata.st_dev, metadata.st_ino)),
            path=path.absolute(),
            identity=(metadata.st_dev, metadata.st_ino),
            access_mask=bundle_jobs._WindowsNativeIO._GENERIC_WRITE,
        )

    def open_directory_at(
        self,
        parent,
        name: str,
        *,
        create: bool,
        exclusive: bool,
    ):
        path = parent.path / name
        if create:
            try:
                path.mkdir()
            except FileExistsError:
                if exclusive:
                    raise
        return self.open_directory(path, require_ntfs=False)

    def close(self, raw_handle: int) -> None:
        return None

    def stat_path(self, path: Path):
        return self._stat(path)

    def stat_at(self, directory, name: str):
        return self._stat(directory.path / name)

    @staticmethod
    def listdir(directory) -> list[str]:
        return os.listdir(directory.path)

    @staticmethod
    def read_bytes(directory, name: str) -> bytes:
        path = directory.path / name
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode) or metadata.st_nlink != 1:
            raise InvalidPaperBundle(f"unsafe file: {name}")
        return path.read_bytes()

    @staticmethod
    def durable_write(directory, name: str, data: bytes) -> None:
        destination = directory.path / name
        if destination.exists() or destination.is_symlink():
            metadata = os.lstat(destination)
            if stat.S_ISLNK(metadata.st_mode) or metadata.st_nlink != 1:
                raise InvalidPaperBundle(f"unsafe file: {name}")
        temporary = directory.path / f".{name}.{uuid4().hex}.tmp"
        temporary.write_bytes(data)
        os.replace(temporary, destination)

    def open_lock(self, directory, name: str):
        self.open_lock_calls += 1
        path = directory.path / name
        if path.exists() or path.is_symlink():
            metadata = os.lstat(path)
            if stat.S_ISLNK(metadata.st_mode) or metadata.st_nlink != 1:
                raise InvalidPaperBundle(f"unsafe lock: {name}")
        return path.open("a+b")

    @staticmethod
    def stat_open_file(handle):
        metadata = os.fstat(handle.fileno())
        return bundle_jobs._WindowsStat(
            st_mode=metadata.st_mode,
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino,
            st_nlink=metadata.st_nlink,
            st_file_attributes=0,
        )

    @staticmethod
    def rename_at(source_directory, source_name, destination_directory, destination_name):
        os.replace(
            source_directory.path / source_name,
            destination_directory.path / destination_name,
        )

    @staticmethod
    def unlink_at(directory, name: str) -> None:
        (directory.path / name).unlink()

    def flush_directory(self, directory) -> None:
        self.flush_directory_calls += 1
        if not directory.access_mask & bundle_jobs._WindowsNativeIO._GENERIC_WRITE:
            raise PermissionError(5, "FlushFileBuffers requires GENERIC_WRITE")


def _children(prefix: str = "run") -> dict[str, PaperBundleChildDescriptor]:
    return {
        artifact_type: PaperBundleChildDescriptor(
            run_id=f"{prefix}-{artifact_type}",
            artifact_type=artifact_type,
            conversation_id=f"conversation-{artifact_type}",
            input_slots=(
                PaperBundleInputSlot(
                    name="paper.pdf",
                    expected_sha256="a" * 64,
                    expected_size=123,
                ),
            ),
            upload_token=f"token-{artifact_type}",
            request_digest=hashlib.sha256(artifact_type.encode("utf-8")).hexdigest(),
            expires_at=2_000_000_000.0,
        )
        for artifact_type in ARTIFACT_TYPES
    }


def _create(store: PaperBundleJobStore, *, job_id: str = "bundle-1"):
    return asyncio.run(
        _create_with_factory(
            store,
            idempotency_key=f"submit-{job_id}",
            request_digest="f" * 64,
            job_id=job_id,
        )
    ).record


def _reconcile_bundle(
    store: PaperBundleJobStore,
    states: dict[str, str],
    *,
    job_id: str = "bundle-1",
) -> None:
    snapshots = {
        f"{job_id}-{artifact_type}": ChildStateSnapshot(
            state=states[artifact_type],
            terminal=states[artifact_type] in {"completed", "cancelled", "failed"},
            process_free=states[artifact_type]
            in {"reserved", "uploading", "queued", "completed", "cancelled", "failed"},
            diagnostic=(
                f"{artifact_type}:{states[artifact_type]}"
                if states[artifact_type] in {"cancelled", "failed"}
                else None
            ),
        )
        for artifact_type in ARTIFACT_TYPES
    }
    store.reconcile(job_id, "owner-1", lambda run_id: snapshots[run_id])


def _commit_publication(
    store: PaperBundleJobStore,
    artifact_type: str,
    generation: int,
    *,
    suffix: str = "a",
    job_id: str = "bundle-1",
):
    return store.commit_child_publication(
        job_id,
        "owner-1",
        artifact_type,
        f"{job_id}-{artifact_type}",
        publication_run_id=f"published-{artifact_type}-{suffix}",
        artifact_id=f"art-{artifact_type}-{suffix}",
        source_attempt=1,
        source_candidate_id=f"candidate-{artifact_type}-{suffix}",
        source_candidate_sha256=(suffix[0].lower() if suffix[0].lower() in "abcdef" else "a")
        * 64,
        generation=generation,
    )


def _process_request_cancel(root: str, ready, result) -> None:
    store = PaperBundleJobStore(root)
    ready.set()
    try:
        record = store.request_cancel("bundle-1", "owner-1")
    except BaseException as exc:  # pragma: no cover - asserted in the parent.
        result.put(("error", type(exc).__name__, str(exc)))
    else:
        result.put(("ok", record.state, record.revision))


def _process_crash_during_first_reservation(root: str, ready) -> None:
    store = PaperBundleJobStore(root)
    store.claim_lease_s = 0.05

    async def reserve(artifact_type: str, job_id: str, run_id: str):
        ready.set()
        time.sleep(0.02)
        os._exit(23)

    async def cleanup(run_id: str) -> None:
        raise AssertionError("the crashed claimant must not clean in-process")

    asyncio.run(
        store.create_with_factory(
            owner_id="owner-1",
            conversation_id="conversation-parent",
            source_name="paper.pdf",
            prompt_version="paper-suite-v2",
            idempotency_key="crash-claim",
            request_digest="9" * 64,
            child_reservation_factory=reserve,
            cleanup_child=cleanup,
        )
    )


def _process_create_bundle(root: str, start, calls, result) -> None:
    store = PaperBundleJobStore(root)

    async def reserve(artifact_type: str, job_id: str, run_id: str):
        calls.put((artifact_type, run_id))
        await asyncio.sleep(0.01)
        return PaperBundleChildDescriptor(
            run_id=run_id,
            artifact_type=artifact_type,
            conversation_id=f"conversation-{artifact_type}",
            input_slots=(PaperBundleInputSlot("paper.pdf", "a" * 64, 123),),
            upload_token=f"token-{artifact_type}",
            request_digest=hashlib.sha256(artifact_type.encode()).hexdigest(),
            expires_at=2_000_000_000.0,
        )

    async def cleanup(run_id: str) -> None:
        return None

    start.wait()
    try:
        creation = asyncio.run(
            store.create_with_factory(
                owner_id="owner-1",
                conversation_id="conversation-parent",
                source_name="paper.pdf",
                prompt_version="paper-suite-v2",
                idempotency_key="cross-process-create",
                request_digest="3" * 64,
                child_reservation_factory=reserve,
                cleanup_child=cleanup,
            )
        )
    except BaseException as exc:  # pragma: no cover - asserted in parent.
        result.put(("error", type(exc).__name__, str(exc)))
    else:
        result.put(("ok", creation.record.job_id, creation.reused))


def _process_swap_bundle_root(
    root: str,
    displaced: str,
    start,
    done,
    result,
) -> None:
    start.wait()
    try:
        Path(root).rename(displaced)
        shutil.copytree(displaced, root)
    except BaseException as exc:  # pragma: no cover - asserted in the parent.
        result.put(("error", type(exc).__name__, str(exc)))
    else:
        result.put(("ok",))
    finally:
        done.set()


async def _create_with_factory(
    store: PaperBundleJobStore,
    *,
    idempotency_key: str = "factory-submit",
    request_digest: str = "d" * 64,
    calls: list[tuple[str, str]] | None = None,
    cleanup_calls: list[str] | None = None,
    job_id: str | None = None,
    owner_id: str = "owner-1",
    conversation_id: str = "conversation-parent",
):
    async def reserve(artifact_type: str, job_id: str, run_id: str):
        if calls is not None:
            calls.append((artifact_type, run_id))
        await asyncio.sleep(0)
        return PaperBundleChildDescriptor(
            run_id=run_id,
            artifact_type=artifact_type,
            conversation_id=f"conversation-{artifact_type}",
            input_slots=(PaperBundleInputSlot("paper.pdf", "a" * 64, 123),),
            upload_token=f"token-{artifact_type}",
            request_digest=hashlib.sha256(artifact_type.encode()).hexdigest(),
            expires_at=2_000_000_000.0,
        )

    async def cleanup(run_id: str) -> None:
        if cleanup_calls is not None:
            cleanup_calls.append(run_id)

    return await store.create_with_factory(
        owner_id=owner_id,
        conversation_id=conversation_id,
        source_name="paper.pdf",
        prompt_version="paper-suite-v2",
        idempotency_key=idempotency_key,
        request_digest=request_digest,
        child_reservation_factory=reserve,
        cleanup_child=cleanup,
        job_id=job_id,
    )


class PaperBundleJobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "paper-bundles"
        self.store = PaperBundleJobStore(self.root)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_create_publishes_four_complete_children_atomically(self) -> None:
        record = _create(self.store)
        self.assertEqual(set(record.children), set(ARTIFACT_TYPES))
        self.assertEqual(record.state, "reserved")
        for artifact_type, child in record.children.items():
            self.assertEqual(child.artifact_type, artifact_type)
            self.assertTrue(child.upload_token)
            self.assertEqual(len(child.input_slots), 1)
            self.assertEqual(len(child.request_digest), 64)
        json.dumps(record.to_payload())
        redacted = record.to_payload()
        self.assertTrue(
            all("upload_token" not in child for child in redacted["children"].values())
        )
        self.assertNotIn("start_intents", redacted)

    def test_async_creation_claim_invokes_factory_once_for_concurrent_retries(self) -> None:
        calls: list[tuple[str, str]] = []

        async def exercise():
            return await asyncio.gather(
                _create_with_factory(self.store, calls=calls),
                _create_with_factory(self.store, calls=calls),
            )

        first, second = asyncio.run(exercise())
        self.assertEqual(first.record.job_id, second.record.job_id)
        self.assertEqual(len(calls), 4)
        self.assertEqual({artifact for artifact, _ in calls}, set(ARTIFACT_TYPES))
        self.assertEqual(
            len({run_id for _, run_id in calls}),
            4,
        )
        initial = next(result for result in (first, second) if not result.reused)
        replay = next(result for result in (first, second) if result.reused)
        creation_payload = initial.to_payload()
        self.assertTrue(
            all("upload_token" in child for child in creation_payload["children"].values())
        )
        self.assertNotIn("idempotency_key_digest", creation_payload)
        self.assertNotIn("diagnostics", creation_payload)
        replay_payload = replay.to_payload()
        self.assertTrue(
            all(
                "upload_token" in child
                and "diagnostic" not in child
                for child in replay_payload["children"].values()
            )
        )
        self.assertEqual(
            {
                artifact_type: child["upload_token"]
                for artifact_type, child in replay_payload["children"].items()
            },
            {
                artifact_type: child["upload_token"]
                for artifact_type, child in creation_payload["children"].items()
            },
        )
        self.assertNotIn("idempotency_key_digest", replay_payload)
        self.assertNotIn("diagnostics", replay_payload)
        self.assertNotIn("start_intents", replay_payload)
        history_payload = first.record.to_payload()
        self.assertTrue(
            all("upload_token" not in child for child in history_payload["children"].values())
        )
        self.assertNotIn("idempotency_key_digest", history_payload)

    @unittest.skipUnless(
        "fork" in multiprocessing.get_all_start_methods(),
        "fork multiprocessing unavailable",
    )
    def test_creation_claim_invokes_only_four_factories_across_processes(self) -> None:
        context = multiprocessing.get_context("fork")
        start = context.Barrier(3)
        calls = context.Queue()
        results = context.Queue()
        processes = [
            context.Process(
                target=_process_create_bundle,
                args=(str(self.root), start, calls, results),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        start.wait()
        for process in processes:
            process.join(3.0)
            self.assertEqual(process.exitcode, 0)
        outcomes = [results.get(timeout=1.0) for _ in range(2)]
        self.assertTrue(all(outcome[0] == "ok" for outcome in outcomes), outcomes)
        self.assertEqual(len({outcome[1] for outcome in outcomes}), 1)
        factory_calls = [calls.get(timeout=1.0) for _ in range(4)]
        self.assertEqual(len(factory_calls), 4)
        self.assertEqual({artifact for artifact, _ in factory_calls}, set(ARTIFACT_TYPES))
        with self.assertRaises(queue.Empty):
            calls.get(timeout=0.1)

    def test_parent_directory_is_invisible_until_all_reservations_are_ready(self) -> None:
        first_reserved = asyncio.Event()
        release_factory = asyncio.Event()
        calls = 0

        async def reserve(artifact_type: str, job_id: str, run_id: str):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_reserved.set()
                await release_factory.wait()
            return PaperBundleChildDescriptor(
                run_id=run_id,
                artifact_type=artifact_type,
                conversation_id=f"conversation-{artifact_type}",
                input_slots=(PaperBundleInputSlot("paper.pdf", "a" * 64, 123),),
                upload_token=f"token-{artifact_type}",
                request_digest=hashlib.sha256(artifact_type.encode()).hexdigest(),
                expires_at=2_000_000_000.0,
            )

        async def exercise():
            creation = asyncio.create_task(
                self.store.create_with_factory(
                    owner_id="owner-1",
                    conversation_id="conversation-parent",
                    source_name="paper.pdf",
                    prompt_version="paper-suite-v2",
                    idempotency_key="atomic-publish",
                    request_digest="8" * 64,
                    child_reservation_factory=reserve,
                    cleanup_child=lambda run_id: asyncio.sleep(0),
                )
            )
            await asyncio.wait_for(first_reserved.wait(), 1.0)
            self.assertEqual(self.store.list_owned("owner-1"), ())
            self.assertFalse(any(not path.name.startswith(".") for path in self.root.iterdir()))
            release_factory.set()
            return await creation

        result = asyncio.run(exercise())
        self.assertEqual(result.record.state, "reserved")
        self.assertEqual(calls, 4)

    def test_pending_creation_cancel_tombstone_blocks_late_factory_and_retry(self) -> None:
        factory_entered = asyncio.Event()
        release_factory = asyncio.Event()
        factory_calls: list[str] = []
        cleanup_calls: list[str] = []

        async def reserve(artifact_type: str, job_id: str, run_id: str):
            factory_calls.append(run_id)
            factory_entered.set()
            await release_factory.wait()
            return PaperBundleChildDescriptor(
                run_id=run_id,
                artifact_type=artifact_type,
                conversation_id=f"conversation-{artifact_type}",
                input_slots=(PaperBundleInputSlot("paper.pdf", "a" * 64, 123),),
                upload_token=f"token-{artifact_type}",
                request_digest=hashlib.sha256(artifact_type.encode()).hexdigest(),
                expires_at=2_000_000_000.0,
            )

        async def cleanup(run_id: str) -> None:
            cleanup_calls.append(run_id)

        async def exercise():
            creation = asyncio.create_task(
                self.store.create_with_factory(
                    owner_id="owner-1",
                    conversation_id="conversation-parent",
                    source_name="paper.pdf",
                    prompt_version="paper-suite-v2",
                    idempotency_key="cancel-before-publish",
                    request_digest="0" * 64,
                    child_reservation_factory=reserve,
                    cleanup_child=cleanup,
                    job_id="bundle-cancel-before-publish",
                )
            )
            await asyncio.wait_for(factory_entered.wait(), 1.0)
            first_cancel = await self.store.cancel_pending_creation(
                "bundle-cancel-before-publish",
                "owner-1",
                cleanup_child=cleanup,
            )
            self.assertEqual(first_cancel, "pending")
            self.assertFalse(creation.done())
            release_factory.set()
            with self.assertRaises(PaperBundleBarrierClosed):
                await creation
            second_cancel = await self.store.cancel_pending_creation(
                "bundle-cancel-before-publish",
                "owner-1",
                cleanup_child=cleanup,
            )
            confirmed_cleanup_count = len(cleanup_calls)
            await asyncio.sleep(0.03)
            self.assertEqual(len(cleanup_calls), confirmed_cleanup_count)
            with self.assertRaises(PaperBundleBarrierClosed):
                await _create_with_factory(
                    self.store,
                    idempotency_key="cancel-before-publish",
                    request_digest="0" * 64,
                    job_id="bundle-cancel-before-publish",
                )
            return first_cancel, second_cancel

        first_cancel, second_cancel = asyncio.run(exercise())
        self.assertEqual(first_cancel, "pending")
        self.assertEqual(second_cancel, "cancelled")
        self.assertEqual(len(factory_calls), 1)
        self.assertEqual(self.store.list_owned("owner-1"), ())
        self.assertFalse((self.root / "bundle-cancel-before-publish").exists())
        self.assertGreaterEqual(len(cleanup_calls), 4)

    def test_pending_creation_cancel_after_restart_is_idempotent(self) -> None:
        owner_id = "owner-1"
        idempotency_digest = self.store._idempotency_digest(owner_id, "restart-cancel")
        self.store.claim_lease_s = 0.01
        decision = self.store._claim_creation(
            owner_id,
            idempotency_digest,
            "1" * 64,
            "bundle-restart-cancel",
        )
        self.assertEqual(decision["action"], "acquired")
        restarted = PaperBundleJobStore(self.root)
        cleanup_calls: list[str] = []

        async def cleanup(run_id: str) -> None:
            cleanup_calls.append(run_id)

        pending = asyncio.run(
            restarted.cancel_pending_creation(
                "bundle-restart-cancel",
                owner_id,
                cleanup_child=cleanup,
            )
        )
        self.assertEqual(pending, "pending")
        self.assertTrue(
            restarted.confirm_pending_creation_quiesced(
                "bundle-restart-cancel",
                owner_id,
            )
        )
        first = asyncio.run(
            restarted.cancel_pending_creation(
                "bundle-restart-cancel",
                owner_id,
                cleanup_child=cleanup,
            )
        )
        second = asyncio.run(
            restarted.cancel_pending_creation(
                "bundle-restart-cancel",
                owner_id,
                cleanup_child=cleanup,
            )
        )
        self.assertEqual((first, second), ("cancelled", "cancelled"))
        self.assertEqual(len(cleanup_calls), 4)
        with self.assertRaises(PaperBundleBarrierClosed):
            asyncio.run(
                _create_with_factory(
                    restarted,
                    idempotency_key="restart-cancel",
                    request_digest="1" * 64,
                    job_id="bundle-restart-cancel",
                )
            )

    def test_pending_creation_cancel_does_not_touch_published_or_other_owner(self) -> None:
        _create(self.store)
        cleanup_calls: list[str] = []

        async def cleanup(run_id: str) -> None:
            cleanup_calls.append(run_id)

        published = asyncio.run(
            self.store.cancel_pending_creation(
                "bundle-1",
                "owner-1",
                cleanup_child=cleanup,
            )
        )
        hidden = asyncio.run(
            self.store.cancel_pending_creation(
                "bundle-1",
                "owner-2",
                cleanup_child=cleanup,
            )
        )
        self.assertEqual(published, "published")
        self.assertEqual(hidden, "not_found")
        self.assertEqual(cleanup_calls, [])
        self.assertFalse(self.store.read_owned("bundle-1", "owner-1").cancel_requested)

    def test_cancel_tombstone_survives_cleanup_failure_and_retries_cleanup_only(self) -> None:
        owner_id = "owner-1"
        idempotency_digest = self.store._idempotency_digest(owner_id, "cancel-cleanup")
        self.store._claim_creation(
            owner_id,
            idempotency_digest,
            "2" * 64,
            "bundle-cancel-cleanup",
        )

        async def failing_cleanup(run_id: str) -> None:
            raise RuntimeError("cleanup unavailable")

        cleanup_calls: list[str] = []

        async def cleanup(run_id: str) -> None:
            cleanup_calls.append(run_id)

        with self.assertRaisesRegex(Exception, "failed to clean"):
            asyncio.run(
                self.store.cancel_pending_creation(
                    "bundle-cancel-cleanup",
                    owner_id,
                    cleanup_child=failing_cleanup,
                )
            )
        with self.assertRaises(PaperBundleBarrierClosed):
            asyncio.run(
                _create_with_factory(
                    self.store,
                    idempotency_key="cancel-cleanup",
                    request_digest="2" * 64,
                    job_id="bundle-cancel-cleanup",
                )
            )
        self.assertTrue(
            self.store.confirm_pending_creation_quiesced(
                "bundle-cancel-cleanup",
                owner_id,
            )
        )
        result = asyncio.run(
            self.store.cancel_pending_creation(
                "bundle-cancel-cleanup",
                owner_id,
                cleanup_child=cleanup,
            )
        )
        self.assertEqual(result, "cancelled")
        self.assertEqual(len(cleanup_calls), 4)

    def test_async_creation_factory_failure_cleans_every_assigned_run_and_retries(self) -> None:
        cleanup_calls: list[str] = []
        attempts = 0

        async def failing_factory(artifact_type: str, job_id: str, run_id: str):
            nonlocal attempts
            attempts += 1
            if artifact_type == "landing":
                raise RuntimeError("reservation failed")
            return PaperBundleChildDescriptor(
                run_id=run_id,
                artifact_type=artifact_type,
                conversation_id=f"conversation-{artifact_type}",
                input_slots=(PaperBundleInputSlot("paper.pdf", "a" * 64, 123),),
                upload_token=f"token-{artifact_type}",
                request_digest=hashlib.sha256(artifact_type.encode()).hexdigest(),
                expires_at=2_000_000_000.0,
            )

        async def cleanup(run_id: str) -> None:
            cleanup_calls.append(run_id)

        async def exercise():
            with self.assertRaises(RuntimeError):
                await self.store.create_with_factory(
                    owner_id="owner-1",
                    conversation_id="conversation-parent",
                    source_name="paper.pdf",
                    prompt_version="paper-suite-v2",
                    idempotency_key="factory-failure",
                    request_digest="c" * 64,
                    child_reservation_factory=failing_factory,
                    cleanup_child=cleanup,
                )
            return await _create_with_factory(
                self.store,
                idempotency_key="factory-failure",
                request_digest="c" * 64,
            )

        result = asyncio.run(exercise())
        self.assertEqual(attempts, 3)
        self.assertEqual(len(cleanup_calls), 4)
        self.assertEqual(result.record.state, "reserved")

    def test_poisoned_initial_child_descriptor_never_publishes(self) -> None:
        cleanup_calls: list[str] = []

        async def reserve(artifact_type: str, job_id: str, run_id: str):
            return PaperBundleChildDescriptor(
                run_id=run_id,
                artifact_type=artifact_type,
                conversation_id=f"conversation-{artifact_type}",
                input_slots=(PaperBundleInputSlot("paper.pdf", "a" * 64, 123),),
                upload_token=f"token-{artifact_type}",
                request_digest=hashlib.sha256(artifact_type.encode()).hexdigest(),
                expires_at=2_000_000_000.0,
                terminal=False,
                process_free=True,
                diagnostic="initial descriptors cannot carry diagnostics",
            )

        async def cleanup(run_id: str) -> None:
            cleanup_calls.append(run_id)

        with self.assertRaises(InvalidPaperBundle):
            asyncio.run(
                self.store.create_with_factory(
                    owner_id="owner-1",
                    conversation_id="conversation-parent",
                    source_name="paper.pdf",
                    prompt_version="paper-suite-v2",
                    idempotency_key="poisoned-descriptor",
                    request_digest="8" * 64,
                    child_reservation_factory=reserve,
                    cleanup_child=cleanup,
                    job_id="bundle-poisoned-descriptor",
                )
            )
        self.assertFalse((self.root / "bundle-poisoned-descriptor").exists())
        self.assertEqual(len(cleanup_calls), 4)

    def test_cleanup_pending_blocks_new_factory_until_cleanup_succeeds(self) -> None:
        factory_calls = 0

        async def failing_factory(artifact_type: str, job_id: str, run_id: str):
            nonlocal factory_calls
            factory_calls += 1
            raise RuntimeError("reservation failed")

        async def failing_cleanup(run_id: str) -> None:
            raise RuntimeError("cleanup unavailable")

        async def exercise():
            with self.assertRaisesRegex(Exception, "failed to clean"):
                await self.store.create_with_factory(
                    owner_id="owner-1",
                    conversation_id="conversation-parent",
                    source_name="paper.pdf",
                    prompt_version="paper-suite-v2",
                    idempotency_key="cleanup-pending",
                    request_digest="7" * 64,
                    child_reservation_factory=failing_factory,
                    cleanup_child=failing_cleanup,
                )
            first_count = factory_calls
            with self.assertRaisesRegex(Exception, "failed to clean"):
                await self.store.create_with_factory(
                    owner_id="owner-1",
                    conversation_id="conversation-parent",
                    source_name="paper.pdf",
                    prompt_version="paper-suite-v2",
                    idempotency_key="cleanup-pending",
                    request_digest="7" * 64,
                    child_reservation_factory=failing_factory,
                    cleanup_child=failing_cleanup,
                )
            self.assertEqual(factory_calls, first_count)
            return await _create_with_factory(
                self.store,
                idempotency_key="cleanup-pending",
                request_digest="7" * 64,
            )

        result = asyncio.run(exercise())
        self.assertEqual(result.record.state, "reserved")

    def test_active_cleanup_claim_prevents_concurrent_duplicate_cleanup(self) -> None:
        self.store.claim_lease_s = 0.03
        async def failing_factory(artifact_type: str, job_id: str, run_id: str):
            raise RuntimeError("reservation failed")

        async def failing_cleanup(run_id: str) -> None:
            raise RuntimeError("cleanup unavailable")

        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()
        cleanup_calls: list[str] = []
        factory_calls: list[tuple[str, str]] = []

        async def slow_cleanup(run_id: str) -> None:
            cleanup_calls.append(run_id)
            cleanup_started.set()
            await release_cleanup.wait()

        async def reserve(artifact_type: str, job_id: str, run_id: str):
            factory_calls.append((artifact_type, run_id))
            return PaperBundleChildDescriptor(
                run_id=run_id,
                artifact_type=artifact_type,
                conversation_id=f"conversation-{artifact_type}",
                input_slots=(PaperBundleInputSlot("paper.pdf", "a" * 64, 123),),
                upload_token=f"token-{artifact_type}",
                request_digest=hashlib.sha256(artifact_type.encode()).hexdigest(),
                expires_at=2_000_000_000.0,
            )

        async def exercise():
            with self.assertRaises(Exception):
                await self.store.create_with_factory(
                    owner_id="owner-1",
                    conversation_id="conversation-parent",
                    source_name="paper.pdf",
                    prompt_version="paper-suite-v2",
                    idempotency_key="cleanup-race",
                    request_digest="3" * 64,
                    child_reservation_factory=failing_factory,
                    cleanup_child=failing_cleanup,
                )
            common = dict(
                owner_id="owner-1",
                conversation_id="conversation-parent",
                source_name="paper.pdf",
                prompt_version="paper-suite-v2",
                idempotency_key="cleanup-race",
                request_digest="3" * 64,
                child_reservation_factory=reserve,
                cleanup_child=slow_cleanup,
            )
            first = asyncio.create_task(self.store.create_with_factory(**common))
            await asyncio.wait_for(cleanup_started.wait(), 1.0)
            second = asyncio.create_task(self.store.create_with_factory(**common))
            await asyncio.sleep(0.08)
            self.assertEqual(len(cleanup_calls), 4)
            release_cleanup.set()
            return await asyncio.gather(first, second)

        first, second = asyncio.run(exercise())
        self.assertEqual(first.record.job_id, second.record.job_id)
        self.assertEqual(len(cleanup_calls), 4)
        self.assertEqual(len(factory_calls), 4)

    @unittest.skipUnless(
        "fork" in multiprocessing.get_all_start_methods(),
        "fork multiprocessing unavailable",
    )
    def test_stale_crashed_creation_cleans_all_preassigned_children_before_retry(self) -> None:
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        process = context.Process(
            target=_process_crash_during_first_reservation,
            args=(str(self.root), ready),
        )
        process.start()
        self.assertTrue(ready.wait(2.0))
        process.join(2.0)
        self.assertEqual(process.exitcode, 23)
        claim_path = next((self.root / ".creation-claims").glob("*.json"))
        old_claim = json.loads(claim_path.read_text(encoding="utf-8"))
        old_run_ids = set(old_claim["assigned_runs"].values())
        self.assertEqual(len(old_run_ids), 4)

        self.store.claim_lease_s = 0.05
        time.sleep(0.08)
        cleanup_calls: list[str] = []
        result = asyncio.run(
            _create_with_factory(
                self.store,
                idempotency_key="crash-claim",
                request_digest="9" * 64,
                cleanup_calls=cleanup_calls,
            )
        )
        self.assertEqual(set(cleanup_calls), old_run_ids)
        self.assertTrue(
            old_run_ids.isdisjoint(child.run_id for child in result.record.children.values())
        )

    def test_stale_publish_temp_residue_is_quarantined_before_retry(self) -> None:
        self.store.claim_lease_s = 0.01
        owner_id = "owner-1"
        idempotency_digest = self.store._idempotency_digest(
            owner_id,
            "stale-publish-temp",
        )
        decision = self.store._claim_creation(
            owner_id,
            idempotency_digest,
            "7" * 64,
            "bundle-stale-publish-temp",
        )
        claimant_nonce = decision["claimant_nonce"]
        staging = self.root / ".pending" / claimant_nonce
        staging.mkdir()
        temporary_record = staging / f".paper_bundle_job.json.{'a' * 32}.tmp"
        temporary_record.write_bytes(b'{"schema_version":')
        time.sleep(0.03)
        cleanup_calls: list[str] = []

        recovered = asyncio.run(
            _create_with_factory(
                self.store,
                idempotency_key="stale-publish-temp",
                request_digest="7" * 64,
                cleanup_calls=cleanup_calls,
                job_id="bundle-stale-publish-temp",
            )
        )

        self.assertEqual(recovered.record.state, "reserved")
        self.assertEqual(len(cleanup_calls), 4)
        self.assertFalse(staging.exists())
        quarantined = tuple((self.root / ".quarantine").iterdir())
        self.assertTrue(
            any(path.name.startswith(f"pending-{claimant_nonce}-") for path in quarantined)
        )

    def test_parent_publish_survives_crash_before_claim_commit_without_cleanup(self) -> None:
        original_write_claim = self.store._write_claim_unlocked
        cleanup_calls: list[str] = []

        def fail_committed_claim(owner_id, idempotency_digest, claim):
            if claim["state"] == "committed":
                raise OSError("simulated crash after parent publish")
            return original_write_claim(owner_id, idempotency_digest, claim)

        async def exercise():
            with mock.patch.object(
                self.store,
                "_write_claim_unlocked",
                side_effect=fail_committed_claim,
            ):
                with self.assertRaisesRegex(OSError, "after parent publish"):
                    await _create_with_factory(
                        self.store,
                        idempotency_key="publish-crash",
                        request_digest="6" * 64,
                        cleanup_calls=cleanup_calls,
                    )
            return await _create_with_factory(
                self.store,
                idempotency_key="publish-crash",
                request_digest="6" * 64,
            )

        result = asyncio.run(exercise())
        self.assertEqual(cleanup_calls, [])
        self.assertTrue(result.reused)

    def test_failed_pending_publish_is_quarantined_before_retry(self) -> None:
        original_replace = os.replace
        cleanup_calls: list[str] = []

        def fail_parent_publish(source, destination, *args, **kwargs):
            if destination == "bundle-publish-fail":
                raise OSError("simulated publish failure")
            return original_replace(source, destination, *args, **kwargs)

        with mock.patch(
            "autodesign.paper_bundle_jobs.os.replace",
            side_effect=fail_parent_publish,
        ):
            with self.assertRaisesRegex(OSError, "publish failure"):
                asyncio.run(
                    _create_with_factory(
                        self.store,
                        idempotency_key="pending-publish-fail",
                        request_digest="2" * 64,
                        cleanup_calls=cleanup_calls,
                        job_id="bundle-publish-fail",
                    )
                )
        self.assertEqual(len(cleanup_calls), 4)
        self.assertEqual(list((self.root / ".pending").iterdir()), [])
        self.assertTrue(
            any(
                path.name.startswith("pending-")
                for path in (self.root / ".quarantine").iterdir()
            )
        )
        retried = asyncio.run(
            _create_with_factory(
                self.store,
                idempotency_key="pending-publish-fail",
                request_digest="2" * 64,
                job_id="bundle-publish-fail",
            )
        )
        self.assertEqual(retried.record.state, "reserved")

    def test_short_start_intent_allows_async_cancel_to_linearize_without_deadlock(self) -> None:
        _create(self.store)

        async def exercise():
            intent = await asyncio.to_thread(
                self.store.claim_child_start,
                "bundle-1",
                "bundle-1-video",
                "owner-1",
            )
            await asyncio.to_thread(
                self.store.commit_child_start,
                "bundle-1",
                "bundle-1-video",
                "owner-1",
                intent.intent_id,
            )
            cancelling = await asyncio.wait_for(
                asyncio.to_thread(
                    self.store.request_cancel,
                    "bundle-1",
                    "owner-1",
                ),
                timeout=1.0,
            )
            pending_snapshots = {
                child.run_id: ChildStateSnapshot("reserved", False, True)
                for child in _children("bundle-1").values()
            }
            pending = await asyncio.to_thread(
                self.store.reconcile,
                "bundle-1",
                "owner-1",
                lambda run_id: pending_snapshots[run_id],
            )
            self.assertEqual(pending.state, "cancelling")
            with self.assertRaises(PaperBundleBarrierClosed):
                await asyncio.to_thread(
                    self.store.resolve_child_start,
                    "bundle-1",
                    "bundle-1-video",
                    "owner-1",
                    intent.intent_id,
                    "registered",
                )
            terminal_snapshots = {
                child.run_id: ChildStateSnapshot("cancelled", True, True)
                for child in _children("bundle-1").values()
            }
            still_cancelling = await asyncio.to_thread(
                self.store.reconcile,
                "bundle-1",
                "owner-1",
                lambda run_id: terminal_snapshots[run_id],
            )
            self.assertEqual(still_cancelling.state, "cancelling")
            await asyncio.to_thread(
                self.store.resolve_child_start,
                "bundle-1",
                "bundle-1-video",
                "owner-1",
                intent.intent_id,
                "aborted",
            )
            terminal = await asyncio.to_thread(
                self.store.reconcile,
                "bundle-1",
                "owner-1",
                lambda run_id: terminal_snapshots[run_id],
            )
            return cancelling, terminal

        cancelling, terminal = asyncio.run(exercise())
        self.assertEqual(cancelling.state, "cancelling")
        self.assertEqual(terminal.state, "cancelled")
        with self.assertRaises(PaperBundleBarrierClosed):
            self.store.claim_child_start(
                "bundle-1", "bundle-1-deck", "owner-1"
            )

    def test_cancel_between_start_claim_and_commit_revokes_start(self) -> None:
        _create(self.store)
        intent = self.store.claim_child_start(
            "bundle-1", "bundle-1-video", "owner-1"
        )
        self.store.request_cancel("bundle-1", "owner-1")
        with self.assertRaises(PaperBundleBarrierClosed):
            self.store.commit_child_start(
                "bundle-1",
                "bundle-1-video",
                "owner-1",
                intent.intent_id,
            )
        self.assertEqual(
            self.store.read_owned("bundle-1", "owner-1").start_intents[
                "bundle-1-video"
            ].state,
            "revoked",
        )

    def test_committed_start_requires_explicit_abort_before_cancel_terminalizes(self) -> None:
        _create(self.store)
        intent = self.store.claim_child_start(
            "bundle-1",
            "bundle-1-video",
            "owner-1",
        )
        self.store.commit_child_start(
            "bundle-1",
            "bundle-1-video",
            "owner-1",
            intent.intent_id,
        )
        self.store.request_cancel("bundle-1", "owner-1")
        snapshots = {
            child.run_id: ChildStateSnapshot("cancelled", True, True)
            for child in _children("bundle-1").values()
        }
        pending = self.store.reconcile(
            "bundle-1",
            "owner-1",
            lambda run_id: snapshots[run_id],
        )
        self.assertEqual(pending.state, "cancelling")
        self.assertEqual(
            pending.start_intents["bundle-1-video"].state,
            "committed",
        )
        with self.assertRaises(PaperBundleBarrierClosed):
            self.store.resolve_child_start(
                "bundle-1",
                "bundle-1-video",
                "owner-1",
                intent.intent_id,
                "registered",
            )
        self.store.resolve_child_start(
            "bundle-1",
            "bundle-1-video",
            "owner-1",
            intent.intent_id,
            "aborted",
        )
        terminal = self.store.reconcile(
            "bundle-1",
            "owner-1",
            lambda run_id: snapshots[run_id],
        )
        self.assertEqual(terminal.state, "cancelled")
        self.assertEqual(
            terminal.start_intents["bundle-1-video"].state,
            "aborted",
        )

    def test_expired_committed_start_recovers_when_child_never_started(self) -> None:
        _create(self.store)
        self.store.start_intent_ttl_s = 0.01
        intent = self.store.claim_child_start(
            "bundle-1",
            "bundle-1-video",
            "owner-1",
        )
        self.store.commit_child_start(
            "bundle-1",
            "bundle-1-video",
            "owner-1",
            intent.intent_id,
        )
        self.store.request_cancel("bundle-1", "owner-1")
        time.sleep(0.02)
        snapshots = {
            child.run_id: ChildStateSnapshot("cancelled", True, True)
            for child in _children("bundle-1").values()
        }

        recovered = self.store.reconcile(
            "bundle-1",
            "owner-1",
            lambda run_id: snapshots[run_id],
        )

        self.assertEqual(recovered.state, "cancelled")
        self.assertEqual(
            recovered.start_intents["bundle-1-video"].state,
            "aborted",
        )

    def test_expired_committed_start_waits_for_live_child_then_recovers_once(self) -> None:
        _create(self.store)
        self.store.start_intent_ttl_s = 0.01
        intent = self.store.claim_child_start(
            "bundle-1",
            "bundle-1-video",
            "owner-1",
        )
        self.store.commit_child_start(
            "bundle-1",
            "bundle-1-video",
            "owner-1",
            intent.intent_id,
        )
        self.store.request_cancel("bundle-1", "owner-1")
        time.sleep(0.02)
        snapshots = {
            child.run_id: ChildStateSnapshot("cancelled", True, True)
            for child in _children("bundle-1").values()
        }
        snapshots["bundle-1-video"] = ChildStateSnapshot(
            "running",
            False,
            False,
        )

        still_running = self.store.reconcile(
            "bundle-1",
            "owner-1",
            lambda run_id: snapshots[run_id],
        )

        self.assertEqual(still_running.state, "cancelling")
        self.assertEqual(
            still_running.start_intents["bundle-1-video"].state,
            "committed",
        )

        snapshots["bundle-1-video"] = ChildStateSnapshot(
            "cancelled",
            True,
            True,
        )
        recovered = self.store.reconcile(
            "bundle-1",
            "owner-1",
            lambda run_id: snapshots[run_id],
        )
        replay = self.store.reconcile(
            "bundle-1",
            "owner-1",
            lambda run_id: snapshots[run_id],
        )

        self.assertEqual(recovered.state, "cancelled")
        self.assertEqual(
            recovered.start_intents["bundle-1-video"].state,
            "aborted",
        )
        self.assertEqual(replay.revision, recovered.revision)

    def test_registered_start_cannot_be_replaced_by_a_second_intent(self) -> None:
        _create(self.store)
        intent = self.store.claim_child_start(
            "bundle-1",
            "bundle-1-video",
            "owner-1",
        )
        self.store.commit_child_start(
            "bundle-1",
            "bundle-1-video",
            "owner-1",
            intent.intent_id,
        )
        self.store.resolve_child_start(
            "bundle-1",
            "bundle-1-video",
            "owner-1",
            intent.intent_id,
            "registered",
        )
        with self.assertRaises(PaperBundleConflict):
            self.store.claim_child_start(
                "bundle-1",
                "bundle-1-video",
                "owner-1",
            )

    def test_expired_uncommitted_start_cannot_cross_the_barrier(self) -> None:
        _create(self.store)
        intent = self.store.claim_child_start(
            "bundle-1",
            "bundle-1-video",
            "owner-1",
            expires_at=time.time() + 0.01,
        )
        time.sleep(0.02)
        with self.assertRaises(PaperBundleBarrierClosed):
            self.store.commit_child_start(
                "bundle-1",
                "bundle-1-video",
                "owner-1",
                intent.intent_id,
            )
        self.assertEqual(
            self.store.read_owned("bundle-1", "owner-1").start_intents[
                "bundle-1-video"
            ].state,
            "revoked",
        )

    def test_lock_only_orphan_directory_does_not_poison_new_claim(self) -> None:
        orphan = self.root / "orphan-bundle"
        orphan.mkdir(parents=True)
        (orphan / ".paper_bundle_job.lock").touch()
        result = asyncio.run(_create_with_factory(self.store))
        self.assertEqual(result.record.state, "reserved")
        self.assertFalse(orphan.exists())
        quarantine = self.root / ".quarantine"
        self.assertTrue(any(path.name.startswith("orphan-bundle-") for path in quarantine.iterdir()))

    def test_create_is_owner_scoped_idempotent_and_conflict_safe(self) -> None:
        first = _create(self.store)
        second = asyncio.run(
            _create_with_factory(
                self.store,
                idempotency_key="submit-bundle-1",
                request_digest="f" * 64,
                job_id="ignored-new-id",
            )
        ).record
        self.assertEqual(second.job_id, first.job_id)
        self.assertEqual(second.revision, first.revision)

        with self.assertRaises(PaperBundleConflict):
            asyncio.run(
                _create_with_factory(
                    self.store,
                    idempotency_key="submit-bundle-1",
                    request_digest="e" * 64,
                    job_id="bundle-2",
                )
            )

        other_owner = asyncio.run(
            _create_with_factory(
                self.store,
                owner_id="owner-2",
                conversation_id="conversation-parent-2",
                idempotency_key="submit-bundle-2",
                request_digest="e" * 64,
                job_id="bundle-2",
            )
        ).record
        self.assertEqual(other_owner.job_id, "bundle-2")

    def test_same_owner_cannot_reuse_job_id_with_a_different_idempotency_key(self) -> None:
        original = _create(self.store)
        record_path = self.root / "bundle-1" / "paper_bundle_job.json"
        original_bytes = record_path.read_bytes()
        factory_calls: list[tuple[str, str]] = []
        cleanup_calls: list[str] = []
        with self.assertRaises(PaperBundleConflict):
            asyncio.run(
                _create_with_factory(
                    self.store,
                    idempotency_key="different-key-same-job",
                    request_digest="4" * 64,
                    job_id="bundle-1",
                    calls=factory_calls,
                    cleanup_calls=cleanup_calls,
                )
            )
        self.assertEqual(factory_calls, [])
        self.assertEqual(cleanup_calls, [])
        self.assertEqual(record_path.read_bytes(), original_bytes)
        recovered = self.store.read_owned("bundle-1", "owner-1")
        self.assertEqual(
            [child.run_id for child in recovered.children.values()],
            [child.run_id for child in original.children.values()],
        )

    def test_different_owners_cannot_reserve_the_same_unpublished_job_id(self) -> None:
        first_factory_entered = asyncio.Event()
        release_first_factory = asyncio.Event()
        first_factory_calls: list[str] = []
        second_factory_calls: list[str] = []
        second_cleanup_calls: list[str] = []

        async def first_reserve(artifact_type: str, job_id: str, run_id: str):
            first_factory_calls.append(run_id)
            first_factory_entered.set()
            await release_first_factory.wait()
            return PaperBundleChildDescriptor(
                run_id=run_id,
                artifact_type=artifact_type,
                conversation_id=f"conversation-{artifact_type}",
                input_slots=(PaperBundleInputSlot("paper.pdf", "a" * 64, 123),),
                upload_token=f"token-{artifact_type}",
                request_digest=hashlib.sha256(artifact_type.encode()).hexdigest(),
                expires_at=2_000_000_000.0,
            )

        async def first_cleanup(run_id: str) -> None:
            raise AssertionError("winning reservation must not be cleaned")

        async def second_reserve(artifact_type: str, job_id: str, run_id: str):
            second_factory_calls.append(run_id)
            raise AssertionError("conflicting owner must not invoke its factory")

        async def second_cleanup(run_id: str) -> None:
            second_cleanup_calls.append(run_id)

        async def exercise():
            first = asyncio.create_task(
                self.store.create_with_factory(
                    owner_id="owner-1",
                    conversation_id="conversation-owner-1",
                    source_name="paper.pdf",
                    prompt_version="paper-suite-v2",
                    idempotency_key="owner-1-key",
                    request_digest="1" * 64,
                    child_reservation_factory=first_reserve,
                    cleanup_child=first_cleanup,
                    job_id="shared-unpublished-job",
                )
            )
            await asyncio.wait_for(first_factory_entered.wait(), 1.0)
            with self.assertRaises(PaperBundleConflict):
                await self.store.create_with_factory(
                    owner_id="owner-2",
                    conversation_id="conversation-owner-2",
                    source_name="paper.pdf",
                    prompt_version="paper-suite-v2",
                    idempotency_key="owner-2-key",
                    request_digest="2" * 64,
                    child_reservation_factory=second_reserve,
                    cleanup_child=second_cleanup,
                    job_id="shared-unpublished-job",
                )
            release_first_factory.set()
            return await first

        winner = asyncio.run(exercise())
        self.assertEqual(winner.record.owner_id, "owner-1")
        self.assertEqual(len(first_factory_calls), 4)
        self.assertEqual(second_factory_calls, [])
        self.assertEqual(second_cleanup_calls, [])
        self.assertEqual(self.store.list_owned("owner-2"), ())

    def test_wrong_owner_observes_not_found_and_owned_list_recovers(self) -> None:
        _create(self.store)
        for operation in (
            lambda: self.store.read_owned("bundle-1", "owner-2"),
            lambda: self.store.request_cancel("bundle-1", "owner-2"),
            lambda: self.store.assert_child_may_upload_or_start(
                "bundle-1", "bundle-1-poster", "owner-2"
            ),
        ):
            with self.assertRaises(PaperBundleNotFound):
                operation()

        self.assertEqual(self.store.list_owned("owner-2"), ())
        recovered = PaperBundleJobStore(self.root).list_owned("owner-1")
        self.assertEqual([record.job_id for record in recovered], ["bundle-1"])
        self.assertEqual(recovered[0].conversation_id, "conversation-parent")
        self.assertEqual(
            recovered[0].children["video"].conversation_id,
            "conversation-video",
        )

    def test_parent_cancel_and_child_commit_have_one_linearized_winner(self) -> None:
        _create(self.store)
        intent = self.store.claim_child_start(
            "bundle-1", "bundle-1-video", "owner-1"
        )
        release = threading.Barrier(3)
        outcomes: list[str] = []

        def child_commit() -> None:
            release.wait()
            try:
                self.store.commit_child_start(
                    "bundle-1",
                    "bundle-1-video",
                    "owner-1",
                    intent.intent_id,
                )
            except PaperBundleBarrierClosed:
                outcomes.append("cancel")
            else:
                outcomes.append("start")

        def cancel() -> None:
            release.wait()
            self.store.request_cancel("bundle-1", "owner-1")

        child_thread = threading.Thread(target=child_commit)
        cancel_thread = threading.Thread(target=cancel)
        child_thread.start()
        cancel_thread.start()
        release.wait()
        child_thread.join(2.0)
        cancel_thread.join(2.0)
        self.assertEqual(len(outcomes), 1)
        state = self.store.read_owned("bundle-1", "owner-1")
        self.assertTrue(state.cancel_requested)
        intent_state = state.start_intents["bundle-1-video"].state
        self.assertEqual(intent_state, "committed" if outcomes == ["start"] else "revoked")

    @unittest.skipUnless(
        "fork" in multiprocessing.get_all_start_methods(),
        "fork multiprocessing unavailable",
    )
    def test_parent_cancel_revokes_precommit_start_across_processes(self) -> None:
        _create(self.store)
        intent = self.store.claim_child_start(
            "bundle-1", "bundle-1-poster", "owner-1"
        )
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        result = context.Queue()
        process = context.Process(
            target=_process_request_cancel,
            args=(str(self.root), ready, result),
        )
        process.start()
        self.assertTrue(ready.wait(2.0))
        process.join(3.0)
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(result.get(timeout=1.0)[:2], ("ok", "cancelling"))
        with self.assertRaises(PaperBundleBarrierClosed):
            self.store.commit_child_start(
                "bundle-1",
                "bundle-1-poster",
                "owner-1",
                intent.intent_id,
            )

    def test_cancel_is_idempotent_and_stale_revision_fails(self) -> None:
        initial = _create(self.store)
        cancelling = self.store.request_cancel(
            "bundle-1", "owner-1", expected_revision=initial.revision
        )
        self.assertEqual(cancelling.state, "cancelling")
        self.assertIsNotNone(cancelling.cancel_requested_at)
        same = self.store.request_cancel("bundle-1", "owner-1")
        self.assertEqual(same.revision, cancelling.revision)
        exact_retry = self.store.request_cancel(
            "bundle-1",
            "owner-1",
            expected_revision=initial.revision,
        )
        self.assertEqual(exact_retry.revision, cancelling.revision)
        fresh = PaperBundleJobStore(self.root / "fresh-stale")
        _create(fresh)
        with self.assertRaises(StalePaperBundleRevision):
            fresh.request_cancel("bundle-1", "owner-1", expected_revision=999)

    def test_cancelled_committed_start_rejects_late_registration(self) -> None:
        _create(self.store)
        intent = self.store.claim_child_start(
            "bundle-1",
            "bundle-1-video",
            "owner-1",
        )
        self.store.commit_child_start(
            "bundle-1",
            "bundle-1-video",
            "owner-1",
            intent.intent_id,
        )
        self.store.request_cancel("bundle-1", "owner-1")
        snapshots = {
            child.run_id: ChildStateSnapshot("cancelled", True, True)
            for child in _children("bundle-1").values()
        }
        pending = self.store.reconcile(
            "bundle-1",
            "owner-1",
            lambda run_id: snapshots[run_id],
        )
        self.assertEqual(pending.state, "cancelling")
        self.assertEqual(
            pending.start_intents["bundle-1-video"].state,
            "committed",
        )
        with self.assertRaises(PaperBundleBarrierClosed):
            self.store.resolve_child_start(
                "bundle-1",
                "bundle-1-video",
                "owner-1",
                intent.intent_id,
                "registered",
            )
        self.store.resolve_child_start(
            "bundle-1",
            "bundle-1-video",
            "owner-1",
            intent.intent_id,
            "aborted",
        )
        terminal = self.store.reconcile(
            "bundle-1",
            "owner-1",
            lambda run_id: snapshots[run_id],
        )
        self.assertEqual(terminal.state, "cancelled")

    def test_reconcile_derives_all_terminal_states(self) -> None:
        cases = (
            ({name: "completed" for name in ARTIFACT_TYPES}, "completed"),
            (
                {"poster": "completed", "deck": "failed", "landing": "cancelled", "video": "failed"},
                "partial",
            ),
            ({name: "cancelled" for name in ARTIFACT_TYPES}, "cancelled"),
            (
                {"poster": "failed", "deck": "failed", "landing": "cancelled", "video": "failed"},
                "failed",
            ),
        )
        for index, (states, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                store = PaperBundleJobStore(self.root / f"case-{index}")
                _create(store)
                snapshots = {
                    f"bundle-1-{artifact_type}": ChildStateSnapshot(
                        state=state,
                        terminal=True,
                        process_free=True,
                        diagnostic=f"{artifact_type}:{state}",
                    )
                    for artifact_type, state in states.items()
                }
                record = store.reconcile(
                    "bundle-1", "owner-1", lambda run_id: snapshots[run_id]
                )
                self.assertEqual(record.state, expected)
                self.assertEqual(record.terminal, True)
                self.assertIsNotNone(record.terminal_at)

    def test_v1_record_migrates_to_empty_publication_state_and_next_write_is_v2(self) -> None:
        _create(self.store)
        record_path = self.root / "bundle-1" / "paper_bundle_job.json"
        v1_payload = json.loads(record_path.read_text(encoding="utf-8"))
        v1_payload["schema_version"] = 1
        v1_payload.pop("publications", None)
        v1_payload.pop("publication_generations", None)
        record_path.write_text(json.dumps(v1_payload), encoding="utf-8")

        restarted = PaperBundleJobStore(self.root)
        migrated = restarted.read_owned("bundle-1", "owner-1")
        self.assertEqual(dict(migrated.publications), {})
        self.assertEqual(
            dict(migrated.publication_generations),
            {artifact_type: 0 for artifact_type in ARTIFACT_TYPES},
        )

        restarted.request_cancel("bundle-1", "owner-1")
        rewritten = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(rewritten["schema_version"], 2)
        self.assertEqual(rewritten["publications"], {})
        self.assertEqual(
            rewritten["publication_generations"],
            {artifact_type: 0 for artifact_type in ARTIFACT_TYPES},
        )

    def test_v2_publication_schema_is_strict_and_fails_closed(self) -> None:
        _create(self.store)
        _reconcile_bundle(
            self.store,
            {"poster": "failed", "deck": "running", "landing": "failed", "video": "running"},
        )
        for artifact_type in ("poster", "landing"):
            generation = self.store.reserve_child_publication(
                "bundle-1",
                "owner-1",
                artifact_type,
                f"bundle-1-{artifact_type}",
            )
            _commit_publication(self.store, artifact_type, generation)
        record_path = self.root / "bundle-1" / "paper_bundle_job.json"
        valid = json.loads(record_path.read_text(encoding="utf-8"))
        corruptions = {
            "missing publications": lambda payload: payload.pop("publications"),
            "extra record key": lambda payload: payload.update(extra=True),
            "boolean generation": lambda payload: payload["publication_generations"].update(
                poster=True
            ),
            "missing generation key": lambda payload: payload[
                "publication_generations"
            ].pop("video"),
            "bad publication sha": lambda payload: payload["publications"]["poster"].update(
                source_candidate_sha256="BAD"
            ),
            "extra publication key": lambda payload: payload["publications"]["poster"].update(
                extra=True
            ),
            "missing publication field": lambda payload: payload["publications"]["poster"].pop(
                "artifact_id"
            ),
            "invalid publication id": lambda payload: payload["publications"]["poster"].update(
                publication_run_id="../bad"
            ),
            "publication generation beyond allocation": lambda payload: payload[
                "publications"
            ]["poster"].update(generation=2),
            "unknown publication artifact": lambda payload: payload["publications"].update(
                audio=payload["publications"]["poster"]
            ),
            "publication source child not quiescent": lambda payload: payload[
                "children"
            ]["poster"].update(
                state="running",
                terminal=False,
                process_free=False,
            ),
            "publication source child not process free": lambda payload: payload[
                "children"
            ]["poster"].update(process_free=False),
            "publication run equals source run": lambda payload: payload["publications"][
                "poster"
            ].update(publication_run_id="bundle-1-poster"),
            "duplicate publication run": lambda payload: payload["publications"][
                "landing"
            ].update(
                publication_run_id=payload["publications"]["poster"][
                    "publication_run_id"
                ]
            ),
            "duplicate artifact id": lambda payload: payload["publications"][
                "landing"
            ].update(artifact_id=payload["publications"]["poster"]["artifact_id"]),
            "boolean source attempt": lambda payload: payload["publications"]["poster"].update(
                source_attempt=True
            ),
            "nonfinite published timestamp": lambda payload: payload["publications"][
                "poster"
            ].update(published_at=float("inf")),
        }
        for label, corrupt in corruptions.items():
            with self.subTest(label=label):
                payload = json.loads(json.dumps(valid))
                corrupt(payload)
                record_path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(InvalidPaperBundle):
                    PaperBundleJobStore(self.root).read_owned("bundle-1", "owner-1")
        record_path.write_text(json.dumps(valid), encoding="utf-8")

    def test_completed_child_supports_first_and_repeated_publication(self) -> None:
        _create(self.store)
        _reconcile_bundle(
            self.store,
            {artifact_type: "completed" for artifact_type in ARTIFACT_TYPES},
        )
        before = self.store.read_owned("bundle-1", "owner-1")
        self.assertEqual(before.state, "completed")

        first_generation = self.store.reserve_child_publication(
            "bundle-1", "owner-1", "poster", "bundle-1-poster"
        )
        first = _commit_publication(
            self.store,
            "poster",
            first_generation,
            suffix="a",
        )
        second_generation = self.store.reserve_child_publication(
            "bundle-1", "owner-1", "poster", "bundle-1-poster"
        )
        second = _commit_publication(
            self.store,
            "poster",
            second_generation,
            suffix="b",
        )

        self.assertEqual((first_generation, second_generation), (1, 2))
        self.assertEqual((first.status, second.status), ("applied", "applied"))
        self.assertEqual(second.record.state, "completed")
        self.assertTrue(second.record.terminal)
        self.assertEqual(second.record.children["poster"].state, "completed")
        self.assertEqual(
            second.record.publications["poster"].publication_run_id,
            "published-poster-b",
        )
        restarted = PaperBundleJobStore(self.root).read_owned(
            "bundle-1",
            "owner-1",
        )
        self.assertEqual(restarted.publications, second.record.publications)

    def test_publication_reserves_running_and_completing_source_children(
        self,
    ) -> None:
        for index, source_state in enumerate(("running", "completing")):
            with self.subTest(source_state=source_state):
                store = PaperBundleJobStore(self.root / f"source-{index}")
                _create(store)
                _reconcile_bundle(
                    store,
                    {
                        "poster": source_state,
                        "deck": "running",
                        "landing": "running",
                        "video": "running",
                    },
                )
                generation = store.reserve_child_publication(
                    "bundle-1",
                    "owner-1",
                    "poster",
                    "bundle-1-poster",
                )
                self.assertEqual(generation, 1)
                with self.assertRaises(PaperBundleConflict):
                    _commit_publication(store, "poster", generation)

    def test_publication_rejects_cancelling_and_cancelled_source_children(
        self,
    ) -> None:
        for index, source_state in enumerate(("cancelling", "cancelled")):
            with self.subTest(source_state=source_state):
                store = PaperBundleJobStore(self.root / f"closed-source-{index}")
                _create(store)
                _reconcile_bundle(
                    store,
                    {
                        "poster": source_state,
                        "deck": "running",
                        "landing": "running",
                        "video": "running",
                    },
                )
                with self.assertRaises(PaperBundleConflict):
                    store.reserve_child_publication(
                        "bundle-1",
                        "owner-1",
                        "poster",
                        "bundle-1-poster",
                    )

    def test_completed_child_publication_commit_loses_to_cancel_race(self) -> None:
        _create(self.store)
        _reconcile_bundle(
            self.store,
            {
                "poster": "completed",
                "deck": "running",
                "landing": "running",
                "video": "running",
            },
        )
        generation = self.store.reserve_child_publication(
            "bundle-1", "owner-1", "poster", "bundle-1-poster"
        )
        self.store.request_cancel("bundle-1", "owner-1")

        with self.assertRaises(PaperBundleBarrierClosed):
            _commit_publication(self.store, "poster", generation)

    def test_running_parent_can_publish_failed_child_without_rewriting_child_facts(self) -> None:
        _create(self.store)
        _reconcile_bundle(
            self.store,
            {"poster": "failed", "deck": "running", "landing": "running", "video": "running"},
        )

        generation = self.store.reserve_child_publication(
            "bundle-1", "owner-1", "poster", "bundle-1-poster"
        )
        committed = _commit_publication(self.store, "poster", generation)

        self.assertEqual(committed.status, "applied")
        self.assertEqual(committed.record.state, "running")
        self.assertEqual(committed.record.completed_children, ("poster",))
        self.assertEqual(committed.record.children["poster"].state, "failed")
        self.assertEqual(committed.record.diagnostics["poster"], "poster:failed")
        self.assertEqual(committed.record.publications["poster"].generation, generation)
        public = committed.record.to_payload()
        self.assertIn("publications", public)
        self.assertNotIn("publication_generations", public)

        _reconcile_bundle(
            self.store,
            {"poster": "failed", "deck": "completed", "landing": "failed", "video": "failed"},
        )
        reconciled = self.store.read_owned("bundle-1", "owner-1")
        self.assertEqual(reconciled.state, "partial")
        self.assertEqual(reconciled.completed_children, ("poster", "deck"))
        self.assertEqual(reconciled.publications, committed.record.publications)
        self.assertEqual(reconciled.publication_generations["poster"], generation)

    def test_publications_promote_terminal_failed_parent_to_partial_then_completed(self) -> None:
        _create(self.store)
        _reconcile_bundle(
            self.store,
            {artifact_type: "failed" for artifact_type in ARTIFACT_TYPES},
        )
        self.assertEqual(self.store.read_owned("bundle-1", "owner-1").state, "failed")

        for index, artifact_type in enumerate(ARTIFACT_TYPES):
            generation = self.store.reserve_child_publication(
                "bundle-1",
                "owner-1",
                artifact_type,
                f"bundle-1-{artifact_type}",
            )
            result = _commit_publication(
                self.store,
                artifact_type,
                generation,
                suffix=chr(ord("a") + index),
            )
            expected_state = "completed" if index == len(ARTIFACT_TYPES) - 1 else "partial"
            self.assertEqual(result.record.state, expected_state)
            self.assertTrue(result.record.terminal)
        self.assertEqual(result.record.completed_children, ARTIFACT_TYPES)

        replacement_generation = self.store.reserve_child_publication(
            "bundle-1", "owner-1", "poster", "bundle-1-poster"
        )
        replacement = _commit_publication(
            self.store,
            "poster",
            replacement_generation,
            suffix="b",
        )
        self.assertEqual(replacement.status, "applied")
        self.assertEqual(replacement.record.state, "completed")
        self.assertEqual(
            replacement.record.publications["poster"].generation,
            replacement_generation,
        )

    def test_publication_commit_is_idempotent_and_persists_across_restart(self) -> None:
        _create(self.store)
        _reconcile_bundle(
            self.store,
            {"poster": "failed", "deck": "running", "landing": "running", "video": "running"},
        )
        generation = self.store.reserve_child_publication(
            "bundle-1", "owner-1", "poster", "bundle-1-poster"
        )
        applied = _commit_publication(self.store, "poster", generation)
        revision = applied.record.revision
        published_at = applied.record.publications["poster"].published_at

        restarted = PaperBundleJobStore(self.root)
        replay = _commit_publication(restarted, "poster", generation)
        recovered = restarted.read_owned("bundle-1", "owner-1")

        self.assertEqual(replay.status, "idempotent")
        self.assertEqual(replay.record.revision, revision)
        self.assertEqual(replay.record.publications["poster"].published_at, published_at)
        self.assertEqual(recovered.publications, replay.record.publications)
        self.assertEqual(recovered.publication_generations["poster"], generation)

    def test_publication_generation_orders_sequential_and_out_of_order_commits(self) -> None:
        _create(self.store)
        _reconcile_bundle(
            self.store,
            {"poster": "failed", "deck": "running", "landing": "running", "video": "running"},
        )
        generation_a = self.store.reserve_child_publication(
            "bundle-1", "owner-1", "poster", "bundle-1-poster"
        )
        generation_b = self.store.reserve_child_publication(
            "bundle-1", "owner-1", "poster", "bundle-1-poster"
        )
        self.assertEqual((generation_a, generation_b), (1, 2))

        stale_a = _commit_publication(
            self.store, "poster", generation_a, suffix="a"
        )
        applied_b = _commit_publication(
            self.store, "poster", generation_b, suffix="b"
        )
        self.assertEqual(stale_a.status, "superseded")
        self.assertEqual(applied_b.status, "applied")
        self.assertNotIn("poster", stale_a.record.publications)
        self.assertEqual(
            applied_b.record.publications["poster"].publication_run_id,
            "published-poster-b",
        )

        generation_c = self.store.reserve_child_publication(
            "bundle-1", "owner-1", "poster", "bundle-1-poster"
        )
        superseded_b = _commit_publication(
            self.store, "poster", generation_b, suffix="b"
        )
        self.assertEqual(superseded_b.status, "superseded")
        applied_c = _commit_publication(
            self.store, "poster", generation_c, suffix="c"
        )
        self.assertEqual(applied_c.status, "applied")
        self.assertEqual(applied_c.record.publications["poster"].generation, 3)

    def test_uncommitted_generation_does_not_block_next_reserve_and_commit(self) -> None:
        _create(self.store)
        _reconcile_bundle(
            self.store,
            {"poster": "failed", "deck": "running", "landing": "running", "video": "running"},
        )
        abandoned = self.store.reserve_child_publication(
            "bundle-1", "owner-1", "poster", "bundle-1-poster"
        )
        next_generation = self.store.reserve_child_publication(
            "bundle-1", "owner-1", "poster", "bundle-1-poster"
        )

        committed = _commit_publication(
            self.store,
            "poster",
            next_generation,
            suffix="after-abandoned",
        )

        self.assertEqual((abandoned, next_generation), (1, 2))
        self.assertEqual(committed.status, "applied")
        self.assertEqual(
            committed.record.publications["poster"].generation,
            next_generation,
        )

    def test_publication_contract_rejects_mismatches_and_unallocated_generations(self) -> None:
        _create(self.store)
        _reconcile_bundle(
            self.store,
            {"poster": "failed", "deck": "running", "landing": "running", "video": "running"},
        )
        with self.assertRaises(PaperBundleNotFound):
            self.store.reserve_child_publication(
                "bundle-1", "wrong-owner", "poster", "bundle-1-poster"
            )
        with self.assertRaises(PaperBundleConflict):
            self.store.reserve_child_publication(
                "bundle-1", "owner-1", "poster", "bundle-1-deck"
            )
        with self.assertRaises(PaperBundleConflict):
            self.store.reserve_child_publication(
                "bundle-1", "owner-1", "deck", "bundle-1-poster"
            )

        generation = self.store.reserve_child_publication(
            "bundle-1", "owner-1", "poster", "bundle-1-poster"
        )
        for invalid_generation in (0, True, generation + 1):
            with self.subTest(generation=invalid_generation):
                with self.assertRaises((InvalidPaperBundle, PaperBundleConflict)):
                    _commit_publication(self.store, "poster", invalid_generation)
        with self.assertRaises(PaperBundleConflict):
            self.store.commit_child_publication(
                "bundle-1",
                "owner-1",
                "poster",
                "bundle-1-deck",
                publication_run_id="published-poster-a",
                artifact_id="art-poster-a",
                source_attempt=1,
                source_candidate_id="candidate-poster-a",
                source_candidate_sha256="a" * 64,
                generation=generation,
            )

    def test_publication_reservation_and_commit_obey_cancellation_barrier(self) -> None:
        before = PaperBundleJobStore(self.root / "cancel-before")
        _create(before)
        _reconcile_bundle(
            before,
            {"poster": "failed", "deck": "running", "landing": "running", "video": "running"},
        )
        before.request_cancel("bundle-1", "owner-1")
        with self.assertRaises(PaperBundleBarrierClosed):
            before.reserve_child_publication(
                "bundle-1", "owner-1", "poster", "bundle-1-poster"
            )

        between = PaperBundleJobStore(self.root / "cancel-between")
        _create(between)
        _reconcile_bundle(
            between,
            {"poster": "failed", "deck": "running", "landing": "running", "video": "running"},
        )
        generation = between.reserve_child_publication(
            "bundle-1", "owner-1", "poster", "bundle-1-poster"
        )
        between.request_cancel("bundle-1", "owner-1")
        with self.assertRaises(PaperBundleBarrierClosed):
            _commit_publication(between, "poster", generation)

        replay_store = PaperBundleJobStore(self.root / "cancel-after")
        _create(replay_store)
        _reconcile_bundle(
            replay_store,
            {"poster": "failed", "deck": "running", "landing": "running", "video": "running"},
        )
        generation = replay_store.reserve_child_publication(
            "bundle-1", "owner-1", "poster", "bundle-1-poster"
        )
        applied = _commit_publication(replay_store, "poster", generation)
        replay_store.request_cancel("bundle-1", "owner-1")
        replay = _commit_publication(replay_store, "poster", generation)
        self.assertEqual(replay.status, "idempotent")
        self.assertEqual(replay.record.revision, applied.record.revision + 1)

    def test_claimed_start_blocks_every_terminal_derivation_until_resolved(self) -> None:
        cases = (
            ("completed", "completed"),
            ("failed", "failed"),
            ("cancelled", "cancelled"),
        )
        for index, (child_state, expected_terminal) in enumerate(cases):
            with self.subTest(child_state=child_state):
                store = PaperBundleJobStore(self.root / f"claimed-terminal-{index}")
                _create(store)
                intent = store.claim_child_start(
                    "bundle-1",
                    "bundle-1-video",
                    "owner-1",
                )
                snapshots = {
                    child.run_id: ChildStateSnapshot(child_state, True, True)
                    for child in _children("bundle-1").values()
                }

                pending = store.reconcile(
                    "bundle-1",
                    "owner-1",
                    lambda run_id: snapshots[run_id],
                )

                self.assertEqual(pending.state, "running")
                self.assertFalse(pending.terminal)
                reloaded = PaperBundleJobStore(store.jobs_dir).read_owned(
                    "bundle-1",
                    "owner-1",
                )
                self.assertEqual(
                    reloaded.start_intents["bundle-1-video"].state,
                    "claimed",
                )
                store.resolve_child_start(
                    "bundle-1",
                    "bundle-1-video",
                    "owner-1",
                    intent.intent_id,
                    "aborted",
                )
                terminal = store.reconcile(
                    "bundle-1",
                    "owner-1",
                    lambda run_id: snapshots[run_id],
                )
                self.assertEqual(terminal.state, expected_terminal)
                self.assertTrue(terminal.terminal)
                self.assertEqual(
                    PaperBundleJobStore(store.jobs_dir)
                    .read_owned("bundle-1", "owner-1")
                    .state,
                    expected_terminal,
                )

    def test_cancelled_parent_waits_for_process_free_children_and_retains_completed(self) -> None:
        _create(self.store)
        self.store.request_cancel("bundle-1", "owner-1")
        states = {
            "poster": ChildStateSnapshot("completed", True, True, "done"),
            "deck": ChildStateSnapshot("cancelled", True, True, "stopped"),
            "landing": ChildStateSnapshot("cancelling", False, False, "worker alive"),
            "video": ChildStateSnapshot("failed", True, True, "failed before cancel"),
        }
        pending = self.store.reconcile(
            "bundle-1",
            "owner-1",
            lambda run_id: states[run_id.rsplit("-", 1)[-1]],
        )
        self.assertEqual(pending.state, "cancelling")
        self.assertFalse(pending.terminal)

        states["landing"] = ChildStateSnapshot("cancelled", True, True, "stopped")
        cancelled = self.store.reconcile(
            "bundle-1",
            "owner-1",
            lambda run_id: states[run_id.rsplit("-", 1)[-1]],
        )
        self.assertEqual(cancelled.state, "cancelled")
        self.assertEqual(cancelled.completed_children, ("poster",))
        self.assertEqual(cancelled.children["poster"].state, "completed")
        public_payload = cancelled.to_payload()
        self.assertNotIn("diagnostics", public_payload)
        self.assertTrue(
            all(
                "diagnostic" not in child
                and "upload_token" not in child
                for child in public_payload["children"].values()
            )
        )

    def test_one_child_cancel_does_not_cancel_siblings(self) -> None:
        _create(self.store)
        snapshots = {
            "poster": ChildStateSnapshot("cancelled", True, True),
            "deck": ChildStateSnapshot("running", False, False),
            "landing": ChildStateSnapshot("reserved", False, True),
            "video": ChildStateSnapshot("queued", False, True),
        }
        record = self.store.reconcile(
            "bundle-1",
            "owner-1",
            lambda run_id: snapshots[run_id.rsplit("-", 1)[-1]],
        )
        self.assertEqual(record.state, "running")
        self.assertFalse(record.cancel_requested)
        self.store.assert_child_may_upload_or_start(
            "bundle-1", "bundle-1-deck", "owner-1"
        )

    def test_state_and_revision_recover_from_disk(self) -> None:
        _create(self.store)
        cancelling = self.store.request_cancel("bundle-1", "owner-1")
        restarted = PaperBundleJobStore(self.root)
        recovered = restarted.read_owned("bundle-1", "owner-1")
        self.assertEqual(recovered.state, "cancelling")
        self.assertEqual(recovered.revision, cancelling.revision)
        with self.assertRaises(PaperBundleBarrierClosed):
            restarted.assert_child_may_upload_or_start(
                "bundle-1", "bundle-1-video", "owner-1"
            )

    def test_owned_history_reconciles_from_child_status_after_restart(self) -> None:
        _create(self.store)
        restarted = PaperBundleJobStore(self.root)
        snapshots = {
            child.run_id: ChildStateSnapshot("completed", True, True, "done")
            for child in _children("bundle-1").values()
        }
        recovered = restarted.list_owned(
            "owner-1", child_status_provider=lambda run_id: snapshots[run_id]
        )
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].state, "completed")
        self.assertEqual(recovered[0].completed_children, ARTIFACT_TYPES)
        same = restarted.read_owned(
            "bundle-1", "owner-1", child_status_provider=lambda run_id: snapshots[run_id]
        )
        self.assertEqual(same.revision, recovered[0].revision)

    def test_reconcile_all_recovers_nonterminal_jobs_for_multiple_owners(self) -> None:
        asyncio.run(
            _create_with_factory(
                self.store,
                owner_id="owner-1",
                idempotency_key="owner-1-recovery",
                request_digest="1" * 64,
                job_id="bundle-owner-1",
            )
        )
        asyncio.run(
            _create_with_factory(
                self.store,
                owner_id="owner-2",
                idempotency_key="owner-2-recovery",
                request_digest="2" * 64,
                job_id="bundle-owner-2",
            )
        )
        snapshots = {
            child.run_id: ChildStateSnapshot("completed", True, True)
            for owner_id in ("owner-1", "owner-2")
            for record in self.store.list_owned(owner_id)
            for child in record.children.values()
        }

        recovered = PaperBundleJobStore(self.root).reconcile_all(
            lambda run_id: snapshots[run_id]
        )

        self.assertEqual(
            [(record.job_id, record.owner_id, record.state) for record in recovered],
            [
                ("bundle-owner-1", "owner-1", "completed"),
                ("bundle-owner-2", "owner-2", "completed"),
            ],
        )
        self.assertTrue(
            all(
                "upload_token" not in child
                for record in recovered
                for child in record.to_payload()["children"].values()
            )
        )

    def test_reconcile_all_ignores_unrelated_corrupt_record(self) -> None:
        _create(self.store)
        corrupt = self.root / "bundle-corrupt"
        corrupt.mkdir()
        (corrupt / "paper_bundle_job.json").write_text(
            "{not-json",
            encoding="utf-8",
        )
        snapshots = {
            child.run_id: ChildStateSnapshot("completed", True, True)
            for child in _children("bundle-1").values()
        }

        recovered = self.store.reconcile_all(lambda run_id: snapshots[run_id])

        self.assertEqual([record.job_id for record in recovered], ["bundle-1"])
        self.assertEqual(recovered[0].state, "completed")
        self.assertTrue(corrupt.exists())

    def test_reconcile_all_converges_cancelling_parent_after_restart(self) -> None:
        _create(self.store)
        self.store.request_cancel("bundle-1", "owner-1")
        snapshots = {
            child.run_id: ChildStateSnapshot("cancelled", True, True)
            for child in _children("bundle-1").values()
        }

        recovered = PaperBundleJobStore(self.root).reconcile_all(
            lambda run_id: snapshots[run_id]
        )

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].state, "cancelled")
        self.assertTrue(recovered[0].terminal)
        self.assertTrue(recovered[0].cancel_requested)

    def test_corrupt_unknown_schema_and_symlink_records_fail_closed(self) -> None:
        _create(self.store)
        record_path = self.root / "bundle-1" / "paper_bundle_job.json"
        record_path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(InvalidPaperBundle):
            self.store.read_owned("bundle-1", "owner-1")

        record_path.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")
        with self.assertRaises(InvalidPaperBundle):
            self.store.read_owned("bundle-1", "owner-1")

        target = self.root.parent / "outside"
        target.mkdir()
        link = self.root / "bundle-link"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(InvalidPaperBundle):
            self.store.read_owned("bundle-link", "owner-1")

        clean = PaperBundleJobStore(self.root.parent / "record-link-root")
        _create(clean)
        clean_record = clean.jobs_dir / "bundle-1" / "paper_bundle_job.json"
        clean_record.unlink()
        clean_record.symlink_to(self.root.parent / "outside-record.json")
        with self.assertRaises(InvalidPaperBundle):
            clean.read_owned("bundle-1", "owner-1")

    def test_boolean_schema_versions_fail_closed_for_records_and_claims(self) -> None:
        _create(self.store)
        record_path = self.root / "bundle-1" / "paper_bundle_job.json"
        record_payload = json.loads(record_path.read_text(encoding="utf-8"))
        record_payload["schema_version"] = True
        record_path.write_text(json.dumps(record_payload), encoding="utf-8")
        with self.assertRaises(InvalidPaperBundle):
            self.store.read_owned("bundle-1", "owner-1")

        claim_store = PaperBundleJobStore(self.root.parent / "boolean-claim-schema")
        owner_id = "owner-1"
        idempotency_key = "boolean-schema"
        idempotency_digest = claim_store._idempotency_digest(
            owner_id,
            idempotency_key,
        )
        claim_store._claim_creation(
            owner_id,
            idempotency_digest,
            "7" * 64,
            "bundle-boolean-schema",
        )
        claim_path = next(
            (claim_store.jobs_dir / ".creation-claims").glob("*.json")
        )
        claim_payload = json.loads(claim_path.read_text(encoding="utf-8"))
        claim_payload["schema_version"] = True
        claim_path.write_text(json.dumps(claim_payload), encoding="utf-8")
        with self.assertRaises(InvalidPaperBundle):
            claim_store._claim_creation(
                owner_id,
                idempotency_digest,
                "7" * 64,
                "bundle-boolean-schema",
            )

    def test_hardlinked_record_and_claim_files_fail_closed(self) -> None:
        _create(self.store)
        record_path = self.root / "bundle-1" / "paper_bundle_job.json"
        record_bytes = record_path.read_bytes()
        outside_record = self.root.parent / "outside-record.json"
        outside_record.write_bytes(record_bytes)
        record_path.unlink()
        os.link(outside_record, record_path)
        with self.assertRaises(InvalidPaperBundle):
            self.store.read_owned("bundle-1", "owner-1")

        separate = PaperBundleJobStore(self.root.parent / "claim-link-root")
        asyncio.run(
            _create_with_factory(
                separate,
                idempotency_key="claim-link",
                request_digest="5" * 64,
            )
        )
        claim_path = next((separate.jobs_dir / ".creation-claims").glob("*.json"))
        outside_claim = self.root.parent / "outside-claim.json"
        outside_claim.write_bytes(claim_path.read_bytes())
        claim_path.unlink()
        os.link(outside_claim, claim_path)
        with self.assertRaises(InvalidPaperBundle):
            asyncio.run(
                _create_with_factory(
                    separate,
                    idempotency_key="claim-link",
                    request_digest="5" * 64,
                )
            )

    def test_replaced_root_is_rejected_before_any_read_or_write(self) -> None:
        _create(self.store)
        displaced = self.root.parent / "displaced-paper-bundles"
        self.root.rename(displaced)
        self.root.mkdir()
        with self.assertRaisesRegex(InvalidPaperBundle, "root was replaced"):
            self.store.read_owned("bundle-1", "owner-1")
        with self.assertRaisesRegex(InvalidPaperBundle, "root was replaced"):
            asyncio.run(
                _create_with_factory(
                    self.store,
                    idempotency_key="after-root-swap",
                    request_digest="4" * 64,
                )
            )
        self.assertEqual(list(self.root.iterdir()), [])

    def test_filesystem_without_stable_directory_handles_fails_before_writes(self) -> None:
        portable_root = self.root.parent / "portable-paper-bundles"
        with mock.patch(
            "autodesign.paper_bundle_jobs._SECURE_DIR_FD_AVAILABLE",
            False,
        ):
            store = PaperBundleJobStore(portable_root)
            self.assertFalse(portable_root.exists())
            with self.assertRaisesRegex(
                InvalidPaperBundle,
                "stable native directory handles",
            ):
                _create(store, job_id="portable-bundle")
        self.assertFalse(portable_root.exists())

    def test_windows_native_backend_supports_bundle_lifecycle(self) -> None:
        windows_root = self.root.parent / "windows-paper-bundles"
        native = _FakeWindowsNativeIO()
        with mock.patch.object(bundle_jobs, "_WINDOWS_IO", native):
            with mock.patch.object(bundle_jobs, "_SECURE_DIR_FD_AVAILABLE", True):
                store = PaperBundleJobStore(windows_root)
                created = _create(store, job_id="windows-bundle")
                replay = asyncio.run(
                    _create_with_factory(
                        store,
                        idempotency_key="submit-windows-bundle",
                        request_digest="f" * 64,
                        job_id="windows-bundle",
                    )
                )
                self.assertTrue(replay.reused)
                self.assertEqual(replay.record.job_id, created.job_id)
                cancelling = store.request_cancel(
                    "windows-bundle",
                    "owner-1",
                )
                self.assertEqual(cancelling.state, "cancelling")
                snapshots = {
                    child.run_id: ChildStateSnapshot("cancelled", True, True)
                    for child in created.children.values()
                }
                terminal = store.reconcile(
                    "windows-bundle",
                    "owner-1",
                    lambda run_id: snapshots[run_id],
                )
                self.assertEqual(terminal.state, "cancelled")
                restarted = PaperBundleJobStore(windows_root)
                self.assertEqual(
                    restarted.read_owned("windows-bundle", "owner-1").state,
                    "cancelled",
                )
        self.assertGreater(native.open_lock_calls, 0)
        self.assertGreater(native.flush_directory_calls, 0)

    def test_windows_create_file_accepts_pointer_valued_handle(self) -> None:
        class PointerValuedHandle:
            value = 123

        native = bundle_jobs._WindowsNativeIO.__new__(
            bundle_jobs._WindowsNativeIO,
        )
        native.kernel32 = mock.Mock()
        native.kernel32.CreateFileW.return_value = PointerValuedHandle()
        native.invalid_handle = -1

        raw_handle = native._create_file(
            Path("C:/paper-bundles"),
            access=1,
            share=2,
            creation=3,
            flags=4,
        )

        self.assertEqual(raw_handle, 123)

    def test_windows_directory_handles_request_flush_access(self) -> None:
        native = bundle_jobs._WindowsNativeIO.__new__(
            bundle_jobs._WindowsNativeIO,
        )
        observed_access: list[int] = []

        def create_file(
            path,
            *,
            access: int,
            share: int,
            creation: int,
            flags: int,
        ) -> int:
            observed_access.append(access)
            if not access & native._GENERIC_WRITE:
                raise PermissionError(5, "FlushFileBuffers requires GENERIC_WRITE")
            return 123

        native._create_file = create_file
        native._validate_handle = mock.Mock(
            return_value=bundle_jobs._WindowsStat(
                st_mode=stat.S_IFDIR,
                st_dev=1,
                st_ino=2,
                st_nlink=1,
                st_file_attributes=0,
            ),
        )
        native._filesystem_name = mock.Mock(return_value="NTFS")
        native.close = mock.Mock()

        directory = native.open_directory(Path("C:/paper-bundles"))

        self.assertEqual(directory.raw_handle, 123)
        self.assertEqual(len(observed_access), 1)
        self.assertTrue(observed_access[0] & native._GENERIC_WRITE)
        native.close.assert_not_called()

    def test_windows_directory_flush_tolerates_unsupported_handle(self) -> None:
        native = bundle_jobs._WindowsNativeIO.__new__(
            bundle_jobs._WindowsNativeIO,
        )
        native.kernel32 = mock.Mock()
        native.kernel32.FlushFileBuffers.return_value = False
        native.ctypes = mock.Mock()
        native.ctypes.get_last_error.return_value = 6
        directory = bundle_jobs._WindowsDirectoryHandle(
            raw_handle=123,
            path=Path("C:/paper-bundles"),
            identity=(1, 2),
            access_mask=native._GENERIC_WRITE,
        )

        native.flush_directory(directory)

        native.kernel32.FlushFileBuffers.assert_called_once_with(123)

    def test_windows_open_lock_closes_descriptor_when_fdopen_fails(self) -> None:
        native = bundle_jobs._WindowsNativeIO.__new__(
            bundle_jobs._WindowsNativeIO,
        )
        native._create_file = mock.Mock(return_value=123)
        native._validate_handle = mock.Mock()
        native.close = mock.Mock()
        directory = bundle_jobs._WindowsDirectoryHandle(
            raw_handle=456,
            path=self.root,
            identity=(1, 2),
            access_mask=native._GENERIC_WRITE,
        )
        fake_msvcrt = mock.Mock()
        descriptor = os.dup(sys.stdout.fileno())
        fake_msvcrt.open_osfhandle.return_value = descriptor
        real_close = os.close

        try:
            with mock.patch.dict(sys.modules, {"msvcrt": fake_msvcrt}):
                with mock.patch.object(bundle_jobs.os, "fdopen", side_effect=OSError("boom")):
                    with mock.patch.object(
                        bundle_jobs.os,
                        "close",
                        side_effect=real_close,
                    ) as close_descriptor:
                        with self.assertRaisesRegex(OSError, "boom"):
                            native.open_lock(directory, "bundle.lock")
            close_descriptor.assert_called_once_with(descriptor)
            with self.assertRaises(OSError):
                os.fstat(descriptor)
            native.close.assert_not_called()
        finally:
            try:
                real_close(descriptor)
            except OSError:
                pass

    def test_windows_lock_adapter_uses_msvcrt_byte_locking(self) -> None:
        fake_msvcrt = mock.Mock()
        fake_msvcrt.LK_LOCK = 1
        fake_msvcrt.LK_UNLCK = 2
        with tempfile.TemporaryFile(mode="w+b") as handle:
            descriptor = handle.fileno()
            with mock.patch.dict(sys.modules, {"msvcrt": fake_msvcrt}):
                with mock.patch.object(bundle_jobs.os, "name", "nt"):
                    bundle_jobs._lock_file(handle)
                    bundle_jobs._unlock_file(handle)
        self.assertEqual(
            fake_msvcrt.locking.call_args_list,
            [
                mock.call(descriptor, fake_msvcrt.LK_LOCK, 1),
                mock.call(descriptor, fake_msvcrt.LK_UNLCK, 1),
            ],
        )

    def test_path_lock_backend_rejects_before_windows_open_race(self) -> None:
        lock_root = self.root.parent / "windows-lock-race"
        lock_root.mkdir()
        outside = self.root.parent / "windows-lock-race-outside"

        def racing_open(*args, **kwargs):
            outside.write_text("created by unsafe open", encoding="utf-8")
            raise RuntimeError("simulated reparse-point race")

        with mock.patch.object(bundle_jobs.os, "name", "nt"):
            with mock.patch.object(bundle_jobs.os, "open", side_effect=racing_open) as opened:
                with self.assertRaisesRegex(
                    InvalidPaperBundle,
                    "stable native directory handles",
                ):
                    with bundle_jobs._locked_file_at(
                        lock_root,
                        "bundle.lock",
                        (1, 2),
                    ):
                        self.fail("an unsafe path lock must never be yielded")
        opened.assert_not_called()
        self.assertFalse(outside.exists())

    @unittest.skipUnless(
        bundle_jobs._SECURE_DIR_FD_AVAILABLE
        and "fork" in multiprocessing.get_all_start_methods(),
        "stable POSIX directory handles and fork are required",
    )
    def test_root_swap_after_handle_open_cannot_mutate_replacement_tree(self) -> None:
        _create(self.store)
        displaced = self.root.parent / "root-swap-trusted"
        context = multiprocessing.get_context("fork")
        start = context.Event()
        done = context.Event()
        result = context.Queue()
        process = context.Process(
            target=_process_swap_bundle_root,
            args=(str(self.root), str(displaced), start, done, result),
        )
        process.start()
        original_write = bundle_jobs._durable_write_json_at
        interleaved = False
        replacement_before: bytes | None = None

        def write_after_root_swap(directory, name, payload):
            nonlocal interleaved, replacement_before
            if not interleaved and name == "paper_bundle_job.json":
                interleaved = True
                start.set()
                self.assertTrue(done.wait(3.0))
                self.assertEqual(result.get(timeout=1.0), ("ok",))
                replacement_before = (
                    self.root / "bundle-1" / "paper_bundle_job.json"
                ).read_bytes()
            return original_write(directory, name, payload)

        try:
            with mock.patch.object(
                bundle_jobs,
                "_durable_write_json_at",
                side_effect=write_after_root_swap,
            ):
                with self.assertRaisesRegex(InvalidPaperBundle, "root changed"):
                    self.store.request_cancel("bundle-1", "owner-1")
        finally:
            process.join(3.0)
            if process.is_alive():
                process.terminate()
                process.join(1.0)

        self.assertEqual(process.exitcode, 0)
        self.assertTrue(interleaved)
        self.assertIsNotNone(replacement_before)
        self.assertEqual(
            (self.root / "bundle-1" / "paper_bundle_job.json").read_bytes(),
            replacement_before,
        )
        trusted_payload = json.loads(
            (displaced / "bundle-1" / "paper_bundle_job.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(trusted_payload["cancel_requested"])

    def test_wall_clock_rollback_keeps_parent_claim_and_intent_readable(self) -> None:
        record = _create(self.store)
        rollback = record.created_at - 60.0
        with mock.patch("autodesign.paper_bundle_jobs.time.time", return_value=rollback):
            cancelling = self.store.request_cancel("bundle-1", "owner-1")
        self.assertGreaterEqual(cancelling.updated_at, record.updated_at)
        self.assertGreaterEqual(cancelling.cancel_requested_at, record.created_at)
        recovered = PaperBundleJobStore(self.root).read_owned("bundle-1", "owner-1")
        self.assertEqual(recovered.state, "cancelling")

        start_store = PaperBundleJobStore(self.root.parent / "rollback-start")
        start_record = _create(start_store)
        with mock.patch(
            "autodesign.paper_bundle_jobs.time.time",
            return_value=start_record.created_at - 60.0,
        ):
            intent = start_store.claim_child_start(
                "bundle-1",
                "bundle-1-video",
                "owner-1",
            )
        self.assertGreaterEqual(intent.claimed_at, start_record.created_at)
        recovered_start = PaperBundleJobStore(start_store.jobs_dir).read_owned(
            "bundle-1",
            "owner-1",
        )
        self.assertEqual(
            recovered_start.start_intents["bundle-1-video"].state,
            "claimed",
        )

        claim_store = PaperBundleJobStore(self.root.parent / "rollback-claim")
        owner_id = "owner-1"
        idempotency_digest = claim_store._idempotency_digest(owner_id, "rollback")
        decision = claim_store._claim_creation(
            owner_id,
            idempotency_digest,
            "9" * 64,
            "rollback-bundle",
        )
        claim = claim_store._read_claim_unlocked(owner_id, idempotency_digest)
        with mock.patch(
            "autodesign.paper_bundle_jobs.time.time",
            return_value=float(claim["created_at"]) - 60.0,
        ):
            self.assertTrue(
                claim_store._renew_creation_claim(
                    owner_id,
                    idempotency_digest,
                    decision["claimant_nonce"],
                )
            )
        recovered_claim = PaperBundleJobStore(claim_store.jobs_dir)._read_claim_unlocked(
            owner_id,
            idempotency_digest,
        )
        self.assertGreaterEqual(recovered_claim["updated_at"], claim["updated_at"])

    def test_replaced_internal_directory_is_rejected(self) -> None:
        claims = self.root / ".creation-claims"
        displaced = self.root / ".creation-claims-displaced"
        claims.rename(displaced)
        claims.mkdir()
        with self.assertRaisesRegex(InvalidPaperBundle, "internal directory was replaced"):
            asyncio.run(
                _create_with_factory(
                    self.store,
                    idempotency_key="internal-swap",
                    request_digest="1" * 64,
                )
            )
        self.assertEqual(list(claims.iterdir()), [])

    def test_open_job_handle_does_not_follow_a_late_directory_swap(self) -> None:
        original = _create(self.store)
        original_directory = self.root / "bundle-1"
        displaced = self.root / "bundle-1-displaced"
        with self.assertRaisesRegex(InvalidPaperBundle, "changed during operation"):
            with self.store._parent_lock("bundle-1") as job_fd:
                original_directory.rename(displaced)
                original_directory.mkdir()
                (original_directory / "paper_bundle_job.json").write_text(
                    "{not-json",
                    encoding="utf-8",
                )
                held_record = self.store._read_record_unlocked("bundle-1", job_fd)
        self.assertEqual(held_record.request_digest, original.request_digest)
        with self.assertRaises(InvalidPaperBundle):
            self.store.read_owned("bundle-1", "owner-1")

    def test_creation_fsyncs_files_and_directories_before_publication(self) -> None:
        with mock.patch(
            "autodesign.paper_bundle_jobs.os.fsync",
            wraps=os.fsync,
        ) as fsync:
            _create(self.store)
        self.assertGreaterEqual(fsync.call_count, 8)

    def test_corrupt_unrelated_record_does_not_poison_new_idempotent_creation(self) -> None:
        _create(self.store)
        (self.root / "bundle-1" / "paper_bundle_job.json").write_text(
            "{broken", encoding="utf-8"
        )
        created = asyncio.run(
            _create_with_factory(
                self.store,
                idempotency_key="unrelated-submit",
                request_digest="b" * 64,
                job_id="bundle-2",
            )
        )
        self.assertEqual(created.record.job_id, "bundle-2")

    def test_invalid_paths_and_duplicate_child_run_ids_are_rejected(self) -> None:
        with self.assertRaises(InvalidPaperBundle):
            asyncio.run(
                _create_with_factory(
                    self.store,
                    idempotency_key="invalid-path",
                    request_digest="f" * 64,
                    job_id="../escape",
                )
            )
        for unsafe_source_name in (
            "/Users/alice/private/paper.pdf",
            "../private/paper.pdf",
            r"C:\\Users\\alice\\private\\paper.pdf",
        ):
            with self.subTest(source_name=unsafe_source_name):
                async def reserve(artifact_type: str, job_id: str, run_id: str):
                    raise AssertionError("unsafe source must fail before reservation")

                async def cleanup(run_id: str) -> None:
                    raise AssertionError("unsafe source creates no reservation")

                with self.assertRaises(InvalidPaperBundle):
                    asyncio.run(
                        self.store.create_with_factory(
                            owner_id="owner-1",
                            conversation_id="conversation-parent",
                            source_name=unsafe_source_name,
                            prompt_version="paper-suite-v2",
                            idempotency_key=f"unsafe-source-{len(unsafe_source_name)}",
                            request_digest="a" * 64,
                            child_reservation_factory=reserve,
                            cleanup_child=cleanup,
                        )
                    )

    def test_concurrent_cancel_retries_are_idempotent(self) -> None:
        initial = _create(self.store)
        outcomes: list[tuple[str, int]] = []
        lock = threading.Lock()

        def cancel() -> None:
            try:
                record = self.store.request_cancel(
                    "bundle-1", "owner-1", expected_revision=initial.revision
                )
            except StalePaperBundleRevision:
                outcome = ("stale", -1)
            else:
                outcome = (record.state, record.revision)
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=cancel) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2.0)
        self.assertEqual(
            outcomes,
            [("cancelling", initial.revision + 1)] * 2,
        )


if __name__ == "__main__":
    unittest.main()
