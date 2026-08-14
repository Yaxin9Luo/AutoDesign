from __future__ import annotations

import asyncio
from dataclasses import replace
import errno
import gc
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import weakref

from autodesign.config import Settings
from autodesign.process_supervision import ProcessLedger, process_is_alive
from autodesign.run_control import RunControlStore
from autodesign.run_supervisor import CancellationOutcome, RunSupervisor
from autodesign.run_worker_protocol import PipelineWorkerRequest
from autodesign.web_run_services import (
    InputSlot,
    InvalidInputSlot,
    InvalidReservation,
    ReservationConflict,
    RunNotReady,
    UploadAuthorizationError,
    UploadCancelled,
    UploadConflict,
    UploadIntegrityError,
    WebRunServices,
)


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cancellation_worker.py"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


async def _chunks(*items: bytes):
    for item in items:
        yield item


async def _wait_for(predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met before polling deadline")


class _FakeSupervisor:
    def __init__(self, store: RunControlStore) -> None:
        self.store = store
        self.start_calls = 0
        self.cancel_calls = 0
        self.start_entered = asyncio.Event()
        self.start_release = asyncio.Event()
        self.cancel_observer = None
        self.supervised = object()

    async def start(self, request: PipelineWorkerRequest):
        self.start_calls += 1
        self.start_entered.set()
        await self.start_release.wait()
        return self.supervised

    async def cancel(self, run_id: str, reason: str) -> CancellationOutcome:
        self.cancel_calls += 1
        if self.cancel_observer is not None:
            self.cancel_observer()
        record = self.store.finalize_cancel(
            run_id,
            {"termination_verified": True, "reason": reason},
        )
        return CancellationOutcome(run_id, record.state, (), (), False)


class _UncooperativeChunks:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_seen = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            await self.release.wait()
        raise StopAsyncIteration


class _CancelBeforeUploadingStore(RunControlStore):
    def __init__(self, runs_dir: Path) -> None:
        super().__init__(runs_dir)
        self.cancelled_transition = False

    def transition(self, run_id, expected, target, **updates):
        if target == "uploading" and not self.cancelled_transition:
            self.cancelled_transition = True
            self.request_cancel(run_id)
        return super().transition(run_id, expected, target, **updates)


class _BrokenAsyncIterable:
    def __aiter__(self):
        raise RuntimeError("broken stream")


class _Payload(dict):
    pass


class _PendingCancellationSupervisor(_FakeSupervisor):
    async def cancel(self, run_id: str, reason: str) -> CancellationOutcome:
        self.cancel_calls += 1
        return CancellationOutcome(
            run_id,
            "cancelling",
            (101, 202),
            (303, 404),
            False,
        )


class _BlockingCancelSupervisor(_FakeSupervisor):
    def __init__(self, store: RunControlStore) -> None:
        super().__init__(store)
        self.cancel_entered = asyncio.Event()
        self.cancel_release = asyncio.Event()

    async def cancel(self, run_id: str, reason: str) -> CancellationOutcome:
        self.cancel_calls += 1
        self.cancel_entered.set()
        await self.cancel_release.wait()
        record = self.store.finalize_cancel(
            run_id,
            {"termination_verified": True, "reason": reason},
        )
        return CancellationOutcome(run_id, record.state, (), (), False)


class _TerminalFailingStartSupervisor(_FakeSupervisor):
    async def start(self, request: PipelineWorkerRequest):
        self.start_calls += 1
        self.start_entered.set()
        await self.start_release.wait()
        current = self.store.read(request.run_id)
        self.store.transition(
            request.run_id,
            current,
            "failed",
            publishable=False,
            writes_frozen=True,
            terminal_event="run.error",
        )
        raise RuntimeError("start failed after durable terminal state")


class _TerminalFailingCancelSupervisor(_BlockingCancelSupervisor):
    async def cancel(self, run_id: str, reason: str) -> CancellationOutcome:
        self.cancel_calls += 1
        self.cancel_entered.set()
        await self.cancel_release.wait()
        record = self.store.finalize_cancel(
            run_id,
            {"termination_verified": True, "reason": reason},
        )
        if self.cancel_calls == 1:
            raise RuntimeError("cancel failed after durable terminal state")
        return CancellationOutcome(run_id, record.state, (), (), True)


class _PausedExpiryServices(WebRunServices):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.candidates_ready = asyncio.Event()
        self.release_candidates = asyncio.Event()

    async def expired_reservation_ids(self, *, now: float | None = None):
        candidates = await super().expired_reservation_ids(now=now)
        self.candidates_ready.set()
        await self.release_candidates.wait()
        return candidates


class WebRunServicesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runs_dir = self.root / "out" / "runs"
        self.store = RunControlStore(self.runs_dir)
        self.supervisor = _FakeSupervisor(self.store)
        self.settings = Settings(
            anthropic_api_key="reserve-secret-31847",
            anthropic_base_url=None,
            gemini_api_key="",
            designer_model="designer",
            critic_model="critic",
            repo_root=Path(__file__).resolve().parents[1],
            out_dir=self.root / "out",
        )
        self.services = WebRunServices(
            self.runs_dir,
            control_store=self.store,
            supervisor=self.supervisor,
            upload_close_timeout_s=0.01,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def _reserve(
        self,
        run_id: str,
        *,
        payload: object | None = None,
        slots: tuple[InputSlot, ...] = (),
    ):
        return await self.services.reserve(
            run_id=run_id,
            artifact_type="poster",
            idempotency_key=f"key-{run_id}",
            request_digest=_sha256(f"request-{run_id}".encode()),
            settings=self.settings,
            payload=payload if payload is not None else {"brief": "hello"},
            input_slots=slots,
        )

    @staticmethod
    def _request_factory(run_id, settings, payload, completed_slots):
        return PipelineWorkerRequest(
            job_kind="pipeline",
            run_id=run_id,
            brief=payload["brief"],
            attachments=tuple(str(completed_slots[name]) for name in sorted(completed_slots)),
            template=None,
            palette_id=None,
            resume_run=None,
            reference_poster=None,
            settings=settings,
        )

    async def test_reserve_is_idempotent_without_persisting_request_secrets(self) -> None:
        """Break caught: reserving after upload or replacing the first in-memory request."""
        payload = {"brief": "exact object", "secret": "payload-secret-27182"}
        digest = _sha256(b"same-request")
        first = await self.services.reserve(
            run_id="reserve-once",
            artifact_type="poster",
            idempotency_key="same-key",
            request_digest=digest,
            settings=self.settings,
            payload=payload,
            input_slots=(InputSlot("paper.pdf", _sha256(b"paper"), 5),),
        )
        second = await self.services.reserve(
            run_id="ignored-on-retry",
            artifact_type="deck",
            idempotency_key="same-key",
            request_digest=digest,
            settings=Settings(
                anthropic_api_key="replacement-secret",
                anthropic_base_url=None,
                gemini_api_key="",
                designer_model="other",
                critic_model="other",
                repo_root=self.root,
                out_dir=self.root / "other",
            ),
            payload={"brief": "replacement"},
            input_slots=(),
        )

        self.assertEqual(first.run_id, "reserve-once")
        self.assertEqual(second.run_id, first.run_id)
        self.assertEqual(second.upload_token, first.upload_token)
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        self.assertEqual(self.store.read(first.run_id).state, "reserved")
        self.assertFalse((self.runs_dir / first.run_id / "uploads").exists())
        persisted = b"".join(
            path.read_bytes() for path in self.runs_dir.rglob("*") if path.is_file()
        )
        self.assertNotIn(self.settings.anthropic_api_key.encode(), persisted)
        self.assertNotIn(payload["secret"].encode(), persisted)
        self.assertNotIn(first.upload_token.encode(), persisted)

        await self.services.upload(
            first.run_id,
            first.upload_token,
            "paper.pdf",
            _chunks(b"paper"),
        )
        captured: dict[str, object] = {}

        def capture_factory(run_id, settings, stored_payload, completed_slots):
            captured.update(settings=settings, payload=stored_payload)
            return self._request_factory(
                run_id,
                settings,
                stored_payload,
                completed_slots,
            )

        self.supervisor.start_release.set()
        await self.services.start(first.run_id, first.upload_token, capture_factory)
        self.assertIs(captured["settings"], self.settings)
        self.assertIs(captured["payload"], payload)

        with self.assertRaises(ReservationConflict):
            await self.services.reserve(
                run_id="conflicting-retry",
                artifact_type="poster",
                idempotency_key="same-key",
                request_digest=_sha256(b"different-request"),
                settings=self.settings,
                payload=payload,
                input_slots=(),
            )

    async def test_recover_queued_no_slot_reservation_reissues_only_exact_context(
        self,
    ) -> None:
        run_id = "recover-no-slot-derived"
        request_digest = _sha256(b"durable-derived-descriptor")
        original = await self.services.reserve(
            run_id=run_id,
            artifact_type="landing",
            idempotency_key=f"derived:{run_id}",
            request_digest=request_digest,
            settings=self.settings,
            payload={"brief": "durable request"},
            input_slots=(),
            parent_job_id="failed-bundle-child",
        )
        self.assertEqual(self.store.read(run_id).state, "queued")
        descriptor_path = self.runs_dir / run_id / "derived_job.json"
        descriptor_path.write_text(
            '{"job_kind":"candidate_publish","version":1}',
            encoding="utf-8",
        )
        descriptor_sha256 = _sha256(descriptor_path.read_bytes())

        recovered_supervisor = _FakeSupervisor(self.store)
        recovered = WebRunServices(
            self.runs_dir,
            control_store=self.store,
            supervisor=recovered_supervisor,
        )
        payload = {"brief": "recovered request"}
        reservation = await recovered.recover_queued_derived_reservation(
            run_id=run_id,
            artifact_type="landing",
            parent_job_id="failed-bundle-child",
            idempotency_key=f"derived:{run_id}",
            request_digest=request_digest,
            descriptor_sha256=descriptor_sha256,
            settings=self.settings,
            payload=payload,
        )

        self.assertEqual(reservation.run_id, run_id)
        self.assertEqual(reservation.state, "queued")
        self.assertNotEqual(reservation.upload_token, original.upload_token)
        recovered_supervisor.start_release.set()
        supervised = await recovered.start(
            run_id,
            reservation.upload_token,
            self._request_factory,
        )
        self.assertIs(supervised, recovered_supervisor.supervised)
        self.assertEqual(recovered_supervisor.start_calls, 1)

        for label, overrides in (
            ("artifact", {"artifact_type": "poster"}),
            ("parent", {"parent_job_id": "other-parent"}),
            ("descriptor", {"descriptor_sha256": _sha256(b"other-descriptor")}),
        ):
            with self.subTest(label=label):
                another = WebRunServices(
                    self.runs_dir,
                    control_store=self.store,
                    supervisor=_FakeSupervisor(self.store),
                )
                with self.assertRaises((InvalidReservation, ReservationConflict)):
                    recovery = {
                        "run_id": run_id,
                        "artifact_type": "landing",
                        "parent_job_id": "failed-bundle-child",
                        "idempotency_key": f"derived:{run_id}",
                        "request_digest": request_digest,
                        "descriptor_sha256": descriptor_sha256,
                        "settings": self.settings,
                        "payload": payload,
                        **overrides,
                    }
                    await another.recover_queued_derived_reservation(
                        **recovery,
                    )

    async def test_recover_queued_reservation_rejects_upload_and_terminal_runs(
        self,
    ) -> None:
        upload = await self._reserve(
            "recover-upload-reservation",
            slots=(InputSlot("paper.pdf", _sha256(b"paper"), 5),),
        )
        recovered = WebRunServices(
            self.runs_dir,
            control_store=self.store,
            supervisor=_FakeSupervisor(self.store),
        )
        upload_descriptor = self.runs_dir / upload.run_id / "derived_job.json"
        upload_descriptor.write_text("{}", encoding="utf-8")
        with self.assertRaises((InvalidReservation, ReservationConflict)):
            await recovered.recover_queued_derived_reservation(
                run_id=upload.run_id,
                artifact_type="poster",
                parent_job_id=None,
                idempotency_key=f"derived:{upload.run_id}",
                request_digest=_sha256(b"upload-reservation"),
                descriptor_sha256=_sha256(upload_descriptor.read_bytes()),
                settings=self.settings,
                payload={"brief": "must not recover"},
            )

        terminal_run_id = "recover-terminal-reservation"
        terminal = await self._reserve(terminal_run_id)
        record = self.store.read(terminal_run_id)
        record = self.store.transition(terminal_run_id, record, "running")
        self.store.transition(
            terminal_run_id,
            record,
            "failed",
            publishable=False,
        )
        terminal_descriptor = (
            self.runs_dir / terminal.run_id / "derived_job.json"
        )
        terminal_descriptor.write_text("{}", encoding="utf-8")
        with self.assertRaises((InvalidReservation, ReservationConflict)):
            await recovered.recover_queued_derived_reservation(
                run_id=terminal.run_id,
                artifact_type="poster",
                parent_job_id=None,
                idempotency_key=f"derived:{terminal.run_id}",
                request_digest=_sha256(b"terminal-reservation"),
                descriptor_sha256=_sha256(terminal_descriptor.read_bytes()),
                settings=self.settings,
                payload={"brief": "must not recover"},
            )

    async def test_no_slot_reservation_publishes_queued_control_atomically(
        self,
    ) -> None:
        with patch.object(
            self.store,
            "transition",
            side_effect=AssertionError("no-slot reserve must not require a second write"),
        ):
            reservation = await self.services.reserve(
                run_id="atomic-no-slot-reserve",
                artifact_type="poster",
                idempotency_key="derived:atomic-no-slot-reserve",
                request_digest=_sha256(b"atomic-no-slot-reserve"),
                settings=self.settings,
                payload={"brief": "publish one candidate"},
                input_slots=(),
                parent_job_id="source-run",
            )

        record = self.store.read(reservation.run_id)
        self.assertEqual(reservation.state, "queued")
        self.assertEqual(record.state, "queued")
        self.assertEqual(record.revision, 0)

    async def test_slot_validation_and_opaque_token_guard_upload_paths(self) -> None:
        """Break caught: accepting traversal names, malformed digests, or a wrong token."""
        invalid_slots = (
            InputSlot("../paper.pdf", "0" * 64, 1),
            InputSlot("paper.pdf", "ABC", 1),
            InputSlot("paper.pdf", "0" * 64, -1),
        )
        for index, slot in enumerate(invalid_slots):
            with self.subTest(slot=slot):
                with self.assertRaises(InvalidInputSlot):
                    await self.services.reserve(
                        run_id=f"invalid-slot-{index}",
                        artifact_type="poster",
                        idempotency_key=f"invalid-slot-key-{index}",
                        request_digest=_sha256(str(index).encode()),
                        settings=self.settings,
                        payload={},
                        input_slots=(slot,),
                    )

        reservation = await self._reserve(
            "token-guard",
            slots=(InputSlot("paper.pdf", _sha256(b"paper"), 5),),
        )
        with self.assertRaises(UploadAuthorizationError):
            await self.services.upload(
                reservation.run_id,
                "wrong-token",
                "paper.pdf",
                _chunks(b"paper"),
            )
        self.assertFalse((self.runs_dir / reservation.run_id / "uploads").exists())

    async def test_upload_rejects_existing_partial_symlink_without_mutating_target(self) -> None:
        """Break caught: opening a pre-planted partial symlink with truncation."""
        paper = b"paper"
        reservation = await self._reserve(
            "partial-symlink",
            slots=(InputSlot("paper.pdf", _sha256(paper), len(paper)),),
        )
        victim = self.root / "victim.txt"
        victim.write_bytes(b"do-not-mutate")
        uploads_dir = self.runs_dir / reservation.run_id / "uploads"
        uploads_dir.mkdir()
        (uploads_dir / "paper.pdf.partial").symlink_to(victim)

        with self.assertRaises(UploadIntegrityError):
            await self.services.upload(
                reservation.run_id,
                reservation.upload_token,
                "paper.pdf",
                _chunks(paper),
            )

        self.assertEqual(victim.read_bytes(), b"do-not-mutate")
        self.assertTrue((uploads_dir / "paper.pdf.partial").is_symlink())
        self.assertFalse((uploads_dir / "paper.pdf").exists())
        self.assertEqual(self.store.read(reservation.run_id).state, "reserved")

    async def test_upload_rejects_unsafe_directories_and_path_aliases(self) -> None:
        """Break caught: upload traversing linked directories or replacing planted entries."""
        paper = b"paper"
        for kind in (
            "run-symlink",
            "uploads-symlink",
            "partial-hardlink",
            "partial-directory",
            "final-regular",
            "final-symlink",
            "final-hardlink",
            "final-directory",
        ):
            with self.subTest(kind=kind):
                reservation = await self._reserve(
                    f"unsafe-{kind}",
                    slots=(InputSlot("paper.pdf", _sha256(paper), len(paper)),),
                )
                run_dir = self.runs_dir / reservation.run_id
                uploads_dir = run_dir / "uploads"
                victim = self.root / f"victim-{kind}"
                victim.write_bytes(b"do-not-mutate")
                if kind == "run-symlink":
                    backing = self.runs_dir / f"backing-{kind}"
                    run_dir.rename(backing)
                    run_dir.symlink_to(backing, target_is_directory=True)
                elif kind == "uploads-symlink":
                    target_dir = self.root / "outside-uploads"
                    target_dir.mkdir()
                    uploads_dir.symlink_to(target_dir, target_is_directory=True)
                else:
                    uploads_dir.mkdir()
                    name = "paper.pdf.partial" if kind.startswith("partial-") else "paper.pdf"
                    planted = uploads_dir / name
                    if kind.endswith("directory"):
                        planted.mkdir()
                    elif kind.endswith("symlink"):
                        planted.symlink_to(victim)
                    elif kind.endswith("hardlink"):
                        planted.hardlink_to(victim)
                    else:
                        planted.write_bytes(b"planted")

                with self.assertRaises(UploadIntegrityError):
                    await self.services.upload(
                        reservation.run_id,
                        reservation.upload_token,
                        "paper.pdf",
                        _chunks(paper),
                    )
                self.assertEqual(victim.read_bytes(), b"do-not-mutate")

    async def test_upload_revalidates_directory_tree_at_promotion(self) -> None:
        """Break caught: a mid-stream uploads-directory symlink surviving promotion."""
        paper = b"paper"
        reservation = await self._reserve(
            "promotion-directory-race",
            slots=(InputSlot("paper.pdf", _sha256(paper), len(paper)),),
        )
        chunk_consumed = asyncio.Event()
        release = asyncio.Event()

        async def source():
            yield paper
            chunk_consumed.set()
            await release.wait()

        upload = asyncio.create_task(
            self.services.upload(
                reservation.run_id,
                reservation.upload_token,
                "paper.pdf",
                source(),
            )
        )
        await chunk_consumed.wait()
        run_dir = self.runs_dir / reservation.run_id
        uploads_dir = run_dir / "uploads"
        backing = run_dir / "uploads-backing"
        uploads_dir.rename(backing)
        uploads_dir.symlink_to(backing, target_is_directory=True)
        release.set()

        with self.assertRaises(UploadIntegrityError):
            await upload
        self.assertTrue(uploads_dir.is_symlink())
        self.assertFalse((backing / "paper.pdf").exists())
        self.assertEqual((backing / "paper.pdf.partial").read_bytes(), paper)

    async def test_upload_parent_swap_during_registration_never_writes_victim(self) -> None:
        """Break caught: O_NOFOLLOW protecting the file but not a swapped parent."""
        paper = b"paper"
        reservation = await self._reserve(
            "registration-parent-swap",
            slots=(InputSlot("paper.pdf", _sha256(paper), len(paper)),),
        )
        run_dir = self.runs_dir / reservation.run_id
        uploads_dir = run_dir / "uploads"
        backing = run_dir / "uploads-backing"
        victim_dir = self.root / "registration-victim"
        victim_dir.mkdir()
        real_open = os.open
        swapped = False

        def racing_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if not swapped and str(path).endswith("paper.pdf.partial"):
                swapped = True
                uploads_dir.rename(backing)
                uploads_dir.symlink_to(victim_dir, target_is_directory=True)
            if dir_fd is None:
                return real_open(path, flags, mode)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with patch("autodesign.web_run_services.os.open", side_effect=racing_open):
            with self.assertRaises(UploadIntegrityError):
                await self.services.upload(
                    reservation.run_id,
                    reservation.upload_token,
                    "paper.pdf",
                    _chunks(paper),
                )

        self.assertTrue(swapped)
        self.assertEqual(list(victim_dir.iterdir()), [])

    async def test_upload_parent_swap_during_promotion_never_replaces_victim(self) -> None:
        """Break caught: absolute os.replace re-resolving a swapped uploads parent."""
        paper = b"paper"
        reservation = await self._reserve(
            "promotion-parent-swap",
            slots=(InputSlot("paper.pdf", _sha256(paper), len(paper)),),
        )
        run_dir = self.runs_dir / reservation.run_id
        uploads_dir = run_dir / "uploads"
        backing = run_dir / "uploads-backing"
        victim_dir = self.root / "promotion-victim"
        victim_dir.mkdir()
        victim_final = victim_dir / "paper.pdf"
        victim_final.write_bytes(b"do-not-replace")
        (victim_dir / "paper.pdf.partial").write_bytes(b"attacker")
        real_replace = os.replace
        swapped = False

        def racing_replace(source, destination, **kwargs):
            nonlocal swapped
            if not swapped and str(source).endswith("paper.pdf.partial"):
                swapped = True
                uploads_dir.rename(backing)
                uploads_dir.symlink_to(victim_dir, target_is_directory=True)
            return real_replace(source, destination, **kwargs)

        with patch("autodesign.web_run_services.os.replace", side_effect=racing_replace):
            with self.assertRaises(UploadIntegrityError):
                await self.services.upload(
                    reservation.run_id,
                    reservation.upload_token,
                    "paper.pdf",
                    _chunks(paper),
                )

        self.assertTrue(swapped)
        self.assertEqual(victim_final.read_bytes(), b"do-not-replace")

    async def test_stream_upload_verifies_integrity_queues_and_handles_duplicates(self) -> None:
        """Break caught: promoting partial bytes or queueing before every slot is valid."""
        paper = b"paper-bytes"
        notes = b"notes"
        reservation = await self._reserve(
            "streamed",
            slots=(
                InputSlot("paper.pdf", _sha256(paper), len(paper)),
                InputSlot("notes.txt", _sha256(notes), len(notes)),
            ),
        )

        first = await self.services.upload(
            reservation.run_id,
            reservation.upload_token,
            "paper.pdf",
            _chunks(paper[:3], paper[3:]),
        )
        self.assertEqual(first.state, "uploading")
        self.assertFalse(first.idempotent)
        self.assertEqual(first.path.read_bytes(), paper)
        self.assertFalse(first.path.with_name("paper.pdf.partial").exists())

        with self.assertRaises(UploadIntegrityError):
            await self.services.upload(
                reservation.run_id,
                reservation.upload_token,
                "notes.txt",
                _chunks(b"wrong"),
            )
        partial = self.runs_dir / reservation.run_id / "uploads" / "notes.txt.partial"
        self.assertEqual(partial.read_bytes(), b"wrong")
        self.assertEqual(self.store.read(reservation.run_id).state, "uploading")

        second = await self.services.upload(
            reservation.run_id,
            reservation.upload_token,
            "notes.txt",
            _chunks(notes),
        )
        self.assertEqual(second.state, "queued")
        self.assertEqual(self.store.read(reservation.run_id).state, "queued")

        duplicate = await self.services.upload(
            reservation.run_id,
            reservation.upload_token,
            "paper.pdf",
            _chunks(paper),
        )
        self.assertTrue(duplicate.idempotent)
        self.assertEqual(first.path.read_bytes(), paper)

        with self.assertRaises(UploadConflict):
            await self.services.upload(
                reservation.run_id,
                reservation.upload_token,
                "paper.pdf",
                _chunks(b"different"),
            )
        self.assertEqual(first.path.read_bytes(), paper)
        self.assertEqual(first.path.with_name("paper.pdf.partial").read_bytes(), b"different")

    async def test_completed_upload_is_fsynced_before_and_after_promotion(self) -> None:
        """Break caught: queued state becoming durable before uploaded bytes and rename."""
        first = b"first"
        second = b"second"
        reservation = await self._reserve(
            "durable-upload",
            slots=(
                InputSlot("first.bin", _sha256(first), len(first)),
                InputSlot("second.bin", _sha256(second), len(second)),
            ),
        )
        await self.services.upload(
            reservation.run_id,
            reservation.upload_token,
            "first.bin",
            _chunks(first),
        )
        final_path = self.runs_dir / reservation.run_id / "uploads" / "second.bin"
        real_fsync = os.fsync
        observations: list[str] = []

        def observe_fsync(descriptor: int) -> None:
            metadata = os.fstat(descriptor)
            if not final_path.exists():
                observations.append("file-before-promotion")
            elif stat.S_ISDIR(metadata.st_mode):
                observations.append("directory-after-promotion")
            else:
                observations.append("other-after-promotion")
            real_fsync(descriptor)

        with patch("autodesign.web_run_services.os.fsync", side_effect=observe_fsync):
            await self.services.upload(
                reservation.run_id,
                reservation.upload_token,
                "second.bin",
                _chunks(second),
            )

        self.assertGreaterEqual(len(observations), 2)
        self.assertEqual(
            observations[:2],
            ["file-before-promotion", "directory-after-promotion"],
        )

    async def test_new_uploads_directory_is_fsynced_before_file_creation(self) -> None:
        """Break caught: a crash losing the uploads directory after accepting bytes."""
        paper = b"paper"
        reservation = await self._reserve(
            "durable-uploads-directory",
            slots=(InputSlot("paper.pdf", _sha256(paper), len(paper)),),
        )
        run_dir = self.runs_dir / reservation.run_id
        uploads_dir = run_dir / "uploads"
        run_identity = run_dir.stat()
        real_fsync = os.fsync
        observations: list[str] = []

        def observe_fsync(descriptor: int) -> None:
            metadata = os.fstat(descriptor)
            if (
                stat.S_ISDIR(metadata.st_mode)
                and (metadata.st_dev, metadata.st_ino)
                == (run_identity.st_dev, run_identity.st_ino)
                and uploads_dir.is_dir()
            ):
                observations.append("run-directory-after-create")
            real_fsync(descriptor)

        with patch("autodesign.web_run_services.os.fsync", side_effect=observe_fsync):
            await self.services.upload(
                reservation.run_id,
                reservation.upload_token,
                "paper.pdf",
                _chunks(paper),
            )

        self.assertIn("run-directory-after-create", observations)

    async def test_upload_path_fallback_keeps_repeated_tree_checks(self) -> None:
        """Break caught: platforms without directory-relative APIs losing uploads."""
        paper = b"paper"
        reservation = await self._reserve(
            "path-fallback",
            slots=(InputSlot("paper.pdf", _sha256(paper), len(paper)),),
        )

        with (
            patch("autodesign.web_run_services._HAS_DIRECTORY_FD", False),
            patch.object(
                self.services,
                "_require_upload_path_tree",
                wraps=self.services._require_upload_path_tree,
            ) as tree_checks,
        ):
            uploaded = await self.services.upload(
                reservation.run_id,
                reservation.upload_token,
                "paper.pdf",
                _chunks(paper),
            )

        self.assertEqual(uploaded.path.read_bytes(), paper)
        self.assertEqual(self.store.read(reservation.run_id).state, "queued")
        self.assertGreaterEqual(tree_checks.call_count, 4)

    async def test_start_requires_queued_and_concurrent_calls_share_one_start(self) -> None:
        """Break caught: starting while uploads are missing or spawning twice concurrently."""
        payload = {"brief": "identity"}
        paper = b"paper"
        reservation = await self._reserve(
            "start-once",
            payload=payload,
            slots=(InputSlot("paper.pdf", _sha256(paper), len(paper)),),
        )
        with self.assertRaises(RunNotReady):
            await self.services.start(
                reservation.run_id,
                reservation.upload_token,
                self._request_factory,
            )

        await self.services.upload(
            reservation.run_id,
            reservation.upload_token,
            "paper.pdf",
            _chunks(paper),
        )
        starts = [
            asyncio.create_task(
                self.services.start(
                    reservation.run_id,
                    reservation.upload_token,
                    self._request_factory,
                )
            )
            for _ in range(3)
        ]
        await self.supervisor.start_entered.wait()
        self.assertEqual(self.supervisor.start_calls, 1)
        self.supervisor.start_release.set()
        results = await asyncio.gather(*starts)

        self.assertTrue(all(result is self.supervisor.supervised for result in results))
        self.assertEqual(self.supervisor.start_calls, 1)

    async def test_start_caller_cancellation_still_adopts_supervised_run_and_sanitizes(
        self,
    ) -> None:
        """Break caught: caller disconnect owning successful-start adoption and cleanup."""
        settings = Settings(
            anthropic_api_key="cancelled-start-secret",
            anthropic_base_url=None,
            gemini_api_key="",
            designer_model="designer",
            critic_model="critic",
            repo_root=Path(__file__).resolve().parents[1],
            out_dir=self.root / "out",
        )
        payload = _Payload(brief="cancelled-start")
        reservation = await self.services.reserve(
            run_id="cancelled-start-caller",
            artifact_type="poster",
            idempotency_key="cancelled-start-caller-key",
            request_digest=_sha256(b"cancelled-start-caller"),
            settings=settings,
            payload=payload,
            input_slots=(),
        )
        caller = asyncio.create_task(
            self.services.start(
                reservation.run_id,
                reservation.upload_token,
                self._request_factory,
            )
        )
        await self.supervisor.start_entered.wait()

        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.supervisor.start_release.set()
        context = await self.services._context(reservation.run_id)
        await _wait_for(lambda: context.supervised_run is self.supervisor.supervised)

        self.assertIsNone(context.start_task)
        self.assertIsNone(context.settings)
        self.assertIsNone(context.payload)
        self.assertEqual(context.reservation.upload_token, "")
        reused = await self.services.start(
            reservation.run_id,
            reservation.upload_token,
            self._request_factory,
        )
        self.assertIs(reused, self.supervisor.supervised)
        self.assertEqual(self.supervisor.start_calls, 1)

    async def test_start_disconnect_then_terminal_supervisor_error_cleans_lifecycle(
        self,
    ) -> None:
        """Break caught: terminal start failure retaining secrets and a stale task."""
        supervisor = _TerminalFailingStartSupervisor(self.store)
        services = WebRunServices(
            self.runs_dir,
            control_store=self.store,
            supervisor=supervisor,
        )
        reservation = await services.reserve(
            run_id="terminal-start-error",
            artifact_type="poster",
            idempotency_key="terminal-start-error-key",
            request_digest=_sha256(b"terminal-start-error"),
            settings=self.settings,
            payload=_Payload(brief="terminal-start-error"),
            input_slots=(),
        )
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        unhandled: list[dict] = []
        loop.set_exception_handler(lambda _loop, details: unhandled.append(details))
        try:
            caller = asyncio.create_task(
                services.start(
                    reservation.run_id,
                    reservation.upload_token,
                    self._request_factory,
                )
            )
            await supervisor.start_entered.wait()
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            supervisor.start_release.set()
            context = await services._context(reservation.run_id)
            await _wait_for(lambda: self.store.read(reservation.run_id).state == "failed")
            await _wait_for(lambda: context.start_task is None)
            del caller
            gc.collect()
            await asyncio.sleep(0)

            self.assertIsNone(context.settings)
            self.assertIsNone(context.payload)
            self.assertEqual(context.reservation.upload_token, "")
            pending = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and not task.done()
                and "_start_and_adopt" in task.get_coro().__qualname__
            ]
            self.assertEqual(pending, [])
            self.assertEqual(unhandled, [])
        finally:
            loop.set_exception_handler(previous_handler)

    async def test_start_rehashes_completed_slots_and_rejects_tampering(self) -> None:
        """Break caught: a queued slot changing after upload but before worker spawn."""
        paper = b"paper"
        reservation = await self._reserve(
            "start-rehash",
            payload={"brief": "tamper"},
            slots=(InputSlot("paper.pdf", _sha256(paper), len(paper)),),
        )
        uploaded = await self.services.upload(
            reservation.run_id,
            reservation.upload_token,
            "paper.pdf",
            _chunks(paper),
        )
        uploaded.path.write_bytes(b"other")
        self.supervisor.start_release.set()

        with self.assertRaises(UploadIntegrityError):
            await self.services.start(
                reservation.run_id,
                reservation.upload_token,
                self._request_factory,
            )
        self.assertEqual(self.supervisor.start_calls, 0)

    async def test_start_rejects_symlink_alias_and_non_regular_completed_slots(self) -> None:
        """Break caught: start accepting a slot path that is not one owned regular file."""
        self.supervisor.start_release.set()
        for kind in ("symlink", "hardlink", "directory"):
            with self.subTest(kind=kind):
                paper = b"paper"
                reservation = await self._reserve(
                    f"start-path-{kind}",
                    payload={"brief": kind},
                    slots=(InputSlot("paper.pdf", _sha256(paper), len(paper)),),
                )
                uploaded = await self.services.upload(
                    reservation.run_id,
                    reservation.upload_token,
                    "paper.pdf",
                    _chunks(paper),
                )
                uploaded.path.unlink()
                if kind == "directory":
                    uploaded.path.mkdir()
                else:
                    source = self.root / f"{kind}-paper.pdf"
                    source.write_bytes(paper)
                    if kind == "symlink":
                        uploaded.path.symlink_to(source)
                    else:
                        uploaded.path.hardlink_to(source)

                with self.assertRaises(UploadIntegrityError):
                    await self.services.start(
                        reservation.run_id,
                        reservation.upload_token,
                        self._request_factory,
                    )
        self.assertEqual(self.supervisor.start_calls, 0)

    async def test_start_fdopen_failure_closes_raw_descriptor(self) -> None:
        """Break caught: secure-read descriptor leaking when fdopen construction fails."""
        paper = b"paper"
        reservation = await self._reserve(
            "fdopen-leak",
            payload={"brief": "fdopen"},
            slots=(InputSlot("paper.pdf", _sha256(paper), len(paper)),),
        )
        uploaded = await self.services.upload(
            reservation.run_id,
            reservation.upload_token,
            "paper.pdf",
            _chunks(paper),
        )
        real_open = os.open
        descriptors: list[int] = []

        def capture_open(path, flags, mode=0o777, *, dir_fd=None):
            if dir_fd is None:
                descriptor = real_open(path, flags, mode)
            else:
                descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if str(path).endswith("paper.pdf") and not flags & os.O_WRONLY:
                descriptors.append(descriptor)
            return descriptor

        with (
            patch("autodesign.web_run_services.os.open", side_effect=capture_open),
            patch(
                "autodesign.web_run_services.os.fdopen",
                side_effect=RuntimeError("fdopen failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "fdopen failed"):
                await self.services.start(
                    reservation.run_id,
                    reservation.upload_token,
                    self._request_factory,
                )

        self.assertEqual(len(descriptors), 1)
        leaked: list[int] = []
        for descriptor in descriptors:
            try:
                os.fstat(descriptor)
            except OSError:
                continue
            leaked.append(descriptor)
            os.close(descriptor)
        self.assertEqual(leaked, [])
        self.assertEqual(uploaded.path.read_bytes(), paper)

    async def test_upload_closes_file_and_directory_descriptors(self) -> None:
        """Break caught: successful upload retaining its anchored directory handles."""
        paper = b"paper"
        reservation = await self._reserve(
            "upload-descriptor-cleanup",
            slots=(InputSlot("paper.pdf", _sha256(paper), len(paper)),),
        )
        real_open = os.open
        descriptors: list[int] = []

        def capture_open(path, flags, mode=0o777, *, dir_fd=None):
            if dir_fd is None:
                descriptor = real_open(path, flags, mode)
            else:
                descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            descriptors.append(descriptor)
            return descriptor

        with patch("autodesign.web_run_services.os.open", side_effect=capture_open):
            await self.services.upload(
                reservation.run_id,
                reservation.upload_token,
                "paper.pdf",
                _chunks(paper),
            )

        self.assertGreaterEqual(len(descriptors), 4)
        leaked: list[int] = []
        for descriptor in set(descriptors):
            try:
                os.fstat(descriptor)
            except OSError as exc:
                self.assertEqual(exc.errno, errno.EBADF)
            else:
                leaked.append(descriptor)
                os.close(descriptor)
        self.assertEqual(leaked, [])

    async def test_cancel_closes_upload_before_supervisor_and_leaves_partial(self) -> None:
        """Break caught: process cancellation racing ahead of open upload writers."""
        payload = b"first-second"
        reservation = await self._reserve(
            "cancel-upload",
            slots=(InputSlot("paper.pdf", _sha256(payload), len(payload)),),
        )
        source_closed = asyncio.Event()
        first_written = asyncio.Event()

        async def source():
            try:
                yield b"first-"
                first_written.set()
                await asyncio.Event().wait()
            finally:
                source_closed.set()

        upload = asyncio.create_task(
            self.services.upload(
                reservation.run_id,
                reservation.upload_token,
                "paper.pdf",
                source(),
            )
        )
        await first_written.wait()
        self.supervisor.cancel_observer = lambda: self.assertTrue(source_closed.is_set())

        result = await self.services.cancel(reservation.run_id, "user_requested")

        self.assertTrue(result.confirmed)
        self.assertEqual(result.state, "cancelled")
        self.assertTrue(result.supervisor_invoked)
        self.assertTrue(result.terminal_event_handled_by_supervisor)
        self.assertTrue(result.cancel_request_event_required)
        self.assertEqual(self.supervisor.cancel_calls, 1)
        with self.assertRaises(UploadCancelled):
            await upload
        partial = self.runs_dir / reservation.run_id / "uploads" / "paper.pdf.partial"
        self.assertEqual(partial.read_bytes(), b"first-")

        repeated = await self.services.cancel(reservation.run_id, "again")
        self.assertTrue(repeated.confirmed)
        self.assertFalse(repeated.cancel_request_event_required)
        self.assertEqual(self.supervisor.cancel_calls, 1)

    async def test_cross_process_cancel_during_writer_registration_closes_handle(self) -> None:
        """Break caught: a failed uploading CAS leaking its just-opened writer."""
        store = _CancelBeforeUploadingStore(self.runs_dir)
        supervisor = _FakeSupervisor(store)
        services = WebRunServices(
            self.runs_dir,
            control_store=store,
            supervisor=supervisor,
            upload_close_timeout_s=0.01,
        )
        reservation = await services.reserve(
            run_id="registration-cas-race",
            artifact_type="poster",
            idempotency_key="registration-cas-key",
            request_digest=_sha256(b"registration-cas-request"),
            settings=self.settings,
            payload={"brief": "race"},
            input_slots=(InputSlot("paper.pdf", _sha256(b"paper"), 5),),
        )

        with self.assertRaises(UploadCancelled):
            await services.upload(
                reservation.run_id,
                reservation.upload_token,
                "paper.pdf",
                _chunks(b"paper"),
            )
        partial = self.runs_dir / reservation.run_id / "uploads" / "paper.pdf.partial"
        self.assertTrue(partial.exists())
        self.assertEqual(store.read(reservation.run_id).state, "cancelling")

        outcome = await services.cancel(reservation.run_id, "finish")
        self.assertTrue(outcome.confirmed)
        self.assertEqual(supervisor.cancel_calls, 1)

    async def test_broken_stream_initialization_deregisters_writer(self) -> None:
        """Break caught: stream initialization failing before writer cleanup is armed."""
        reservation = await self._reserve(
            "broken-stream",
            slots=(InputSlot("paper.pdf", _sha256(b"paper"), 5),),
        )
        with self.assertRaisesRegex(RuntimeError, "broken stream"):
            await self.services.upload(
                reservation.run_id,
                reservation.upload_token,
                "paper.pdf",
                _BrokenAsyncIterable(),
            )

        outcome = await self.services.cancel(reservation.run_id, "cleanup")
        self.assertTrue(outcome.confirmed)
        self.assertEqual(self.supervisor.cancel_calls, 1)

    async def test_cancel_timeout_never_calls_supervisor_and_later_retry_finishes(self) -> None:
        """Break caught: finalizing cancellation while an upload iterator still owns work."""
        reservation = await self._reserve(
            "cancel-timeout",
            slots=(InputSlot("paper.pdf", _sha256(b"paper"), 5),),
        )
        source = _UncooperativeChunks()
        upload = asyncio.create_task(
            self.services.upload(
                reservation.run_id,
                reservation.upload_token,
                "paper.pdf",
                source,
            )
        )
        await source.entered.wait()

        timed_out = await self.services.cancel(reservation.run_id, "user_requested")

        self.assertFalse(timed_out.confirmed)
        self.assertEqual(timed_out.state, "cancelling")
        self.assertFalse(timed_out.supervisor_invoked)
        self.assertFalse(timed_out.terminal_event_handled_by_supervisor)
        self.assertTrue(timed_out.cancel_request_event_required)
        self.assertEqual(self.supervisor.cancel_calls, 0)
        self.assertEqual(self.store.read(reservation.run_id).state, "cancelling")
        self.assertTrue(source.cancel_seen.is_set())

        source.release.set()
        with self.assertRaises(UploadCancelled):
            await upload
        confirmed = await self.services.cancel(reservation.run_id, "retry")
        self.assertTrue(confirmed.confirmed)
        self.assertFalse(confirmed.cancel_request_event_required)
        self.assertEqual(self.supervisor.cancel_calls, 1)

    async def test_cancellation_pending_preserves_surviving_pids(self) -> None:
        """Break caught: transport result hiding the processes blocking confirmation."""
        supervisor = _PendingCancellationSupervisor(self.store)
        services = WebRunServices(
            self.runs_dir,
            control_store=self.store,
            supervisor=supervisor,
        )
        reservation = await services.reserve(
            run_id="surviving-pids",
            artifact_type="poster",
            idempotency_key="surviving-pids-key",
            request_digest=_sha256(b"surviving-pids-request"),
            settings=self.settings,
            payload={"brief": "pending"},
            input_slots=(),
        )

        outcome = await services.cancel(reservation.run_id, "cannot_kill")

        self.assertEqual(outcome.state, "cancelling")
        self.assertFalse(outcome.confirmed)
        self.assertEqual(outcome.terminated_pids, (101, 202))
        self.assertEqual(outcome.surviving_pids, (303, 404))

    async def test_cancellation_persisted_first_prevents_late_start(self) -> None:
        """Break caught: a queued request spawning after cancellation has linearized."""
        reservation = await self._reserve("cancel-before-start")
        first = await self.services.cancel(reservation.run_id, "before_start")
        self.assertTrue(first.confirmed)

        with self.assertRaises(RunNotReady):
            await self.services.start(
                reservation.run_id,
                reservation.upload_token,
                self._request_factory,
            )
        self.assertEqual(self.supervisor.start_calls, 0)

    async def test_cancel_after_service_restart_uses_durable_control_or_expiry_reconciler(
        self,
    ) -> None:
        """Break caught: restart losing the in-memory context needed to cancel."""
        reservation = await self._reserve(
            "restart-cancel",
            slots=(InputSlot("paper.pdf", _sha256(b"paper"), 5),),
        )
        fresh_services = WebRunServices(
            self.runs_dir,
            control_store=self.store,
            supervisor=self.supervisor,
            upload_close_timeout_s=0.01,
        )

        outcome = await fresh_services.cancel(reservation.run_id, "after_restart")

        self.assertTrue(outcome.confirmed)
        self.assertEqual(outcome.state, "cancelled")
        self.assertTrue(outcome.cancel_request_event_required)
        self.assertEqual(self.supervisor.cancel_calls, 1)
        self.assertEqual(self.store.read(reservation.run_id).state, "cancelled")

    async def test_expiry_reconciler_fails_abandoned_process_local_reservation_once(
        self,
    ) -> None:
        """Break caught: expired reserved/uploading contexts remaining nonterminal forever."""
        supervisor = RunSupervisor(self.runs_dir, control_store=self.store)
        services = WebRunServices(
            self.runs_dir,
            control_store=self.store,
            supervisor=supervisor,
            reservation_ttl_s=30.0,
        )
        reservation = await services.reserve(
            run_id="expired-reservation",
            artifact_type="poster",
            idempotency_key="expired-key",
            request_digest=_sha256(b"expired-request"),
            settings=self.settings,
            payload={"brief": "expire"},
            input_slots=(InputSlot("paper.pdf", _sha256(b"paper"), 5),),
            expires_at=100.0,
        )

        self.assertEqual(reservation.expires_at, 100.0)
        self.assertEqual(await services.expired_reservation_ids(now=99.0), ())
        self.assertEqual(
            await services.expired_reservation_ids(now=100.0),
            (reservation.run_id,),
        )
        self.assertEqual(
            await services.reconcile_expired_reservations(now=100.0),
            (reservation.run_id,),
        )
        record = self.store.read(reservation.run_id)
        self.assertEqual(record.state, "failed")
        self.assertTrue(record.writes_frozen)
        events_path = self.runs_dir / reservation.run_id / "run_events.jsonl"
        first_events = events_path.read_bytes()
        events = [json.loads(line) for line in first_events.decode().splitlines()]
        self.assertEqual([event["event"] for event in events], ["run.error"])

        self.assertEqual(await services.reconcile_expired_reservations(now=101.0), ())
        self.assertEqual(events_path.read_bytes(), first_events)
        retry = await services.reserve(
            run_id="ignored-expired-reservation",
            artifact_type="deck",
            idempotency_key="expired-key",
            request_digest=_sha256(b"expired-request"),
            settings=self.settings,
            payload={"brief": "replacement"},
            input_slots=(),
        )
        self.assertTrue(retry.reused)
        self.assertEqual(retry.run_id, reservation.run_id)
        self.assertEqual(retry.upload_token, "")

    async def test_expiry_reconciles_queued_no_input_and_uploaded_unstarted(self) -> None:
        """Break caught: queued reservations being permanently outside expiry."""
        supervisor = RunSupervisor(self.runs_dir, control_store=self.store)
        services = WebRunServices(
            self.runs_dir,
            control_store=self.store,
            supervisor=supervisor,
            upload_close_timeout_s=0.05,
        )
        no_input = await services.reserve(
            run_id="expired-queued-empty",
            artifact_type="poster",
            idempotency_key="expired-queued-empty-key",
            request_digest=_sha256(b"expired-queued-empty"),
            settings=self.settings,
            payload={"brief": "empty"},
            input_slots=(),
            expires_at=100.0,
        )
        paper = b"paper"
        uploaded = await services.reserve(
            run_id="expired-queued-uploaded",
            artifact_type="poster",
            idempotency_key="expired-queued-uploaded-key",
            request_digest=_sha256(b"expired-queued-uploaded"),
            settings=self.settings,
            payload={"brief": "uploaded"},
            input_slots=(InputSlot("paper.pdf", _sha256(paper), len(paper)),),
            expires_at=100.0,
        )
        await services.upload(
            uploaded.run_id,
            uploaded.upload_token,
            "paper.pdf",
            _chunks(paper),
        )

        self.assertEqual(
            await services.reconcile_expired_reservations(now=100.0),
            tuple(sorted((no_input.run_id, uploaded.run_id))),
        )
        for run_id in (no_input.run_id, uploaded.run_id):
            self.assertEqual(self.store.read(run_id).state, "failed")
            events = [
                json.loads(line)
                for line in (self.runs_dir / run_id / "run_events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual([event["event"] for event in events], ["run.error"])

    async def test_expiry_aborts_active_writer_before_terminal_transition(self) -> None:
        """Break caught: an expired active writer being skipped instead of quiesced."""
        supervisor = RunSupervisor(self.runs_dir, control_store=self.store)
        services = WebRunServices(
            self.runs_dir,
            control_store=self.store,
            supervisor=supervisor,
            upload_close_timeout_s=0.05,
        )
        paper = b"paper"
        reservation = await services.reserve(
            run_id="expired-active-writer",
            artifact_type="poster",
            idempotency_key="expired-active-writer-key",
            request_digest=_sha256(b"expired-active-writer"),
            settings=self.settings,
            payload={"brief": "active"},
            input_slots=(InputSlot("paper.pdf", _sha256(paper), len(paper)),),
            expires_at=100.0,
        )
        entered = asyncio.Event()
        release = asyncio.Event()

        async def source():
            yield paper
            entered.set()
            await release.wait()

        upload = asyncio.create_task(
            services.upload(
                reservation.run_id,
                reservation.upload_token,
                "paper.pdf",
                source(),
            )
        )
        await entered.wait()

        expired = await services.reconcile_expired_reservations(now=100.0)
        release.set()
        upload_cancelled = False
        try:
            await upload
        except UploadCancelled:
            upload_cancelled = True

        self.assertEqual(expired, (reservation.run_id,))
        self.assertTrue(upload_cancelled)
        self.assertEqual(self.store.read(reservation.run_id).state, "failed")

    async def test_expiry_writer_timeout_stays_nonterminal_and_retries(self) -> None:
        """Break caught: expiry either skipping or failing a writer that has not quiesced."""
        supervisor = RunSupervisor(self.runs_dir, control_store=self.store)
        services = WebRunServices(
            self.runs_dir,
            control_store=self.store,
            supervisor=supervisor,
            upload_close_timeout_s=0.01,
        )
        reservation = await services.reserve(
            run_id="expired-writer-timeout",
            artifact_type="poster",
            idempotency_key="expired-writer-timeout-key",
            request_digest=_sha256(b"expired-writer-timeout"),
            settings=self.settings,
            payload={"brief": "timeout"},
            input_slots=(InputSlot("paper.pdf", _sha256(b"paper"), 5),),
            expires_at=100.0,
        )
        source = _UncooperativeChunks()
        upload = asyncio.create_task(
            services.upload(
                reservation.run_id,
                reservation.upload_token,
                "paper.pdf",
                source,
            )
        )
        await source.entered.wait()

        first = await services.reconcile_expired_reservations(now=100.0)
        first_state = self.store.read(reservation.run_id).state
        cancel_seen = source.cancel_seen.is_set()
        source.release.set()
        upload_cancelled = False
        try:
            await upload
        except UploadCancelled:
            upload_cancelled = True
        except UploadIntegrityError:
            pass
        second = await services.reconcile_expired_reservations(now=101.0)

        self.assertEqual(first, ())
        self.assertEqual(first_state, "uploading")
        self.assertTrue(cancel_seen)
        self.assertTrue(upload_cancelled)
        self.assertEqual(second, (reservation.run_id,))
        self.assertEqual(self.store.read(reservation.run_id).state, "failed")

    async def test_expiry_rechecks_deadline_under_context_lock(self) -> None:
        """Break caught: a stale candidate expiring after its deadline was extended."""
        supervisor = RunSupervisor(self.runs_dir, control_store=self.store)
        services = _PausedExpiryServices(
            self.runs_dir,
            control_store=self.store,
            supervisor=supervisor,
        )
        reservation = await services.reserve(
            run_id="expiry-deadline-race",
            artifact_type="poster",
            idempotency_key="expiry-deadline-race-key",
            request_digest=_sha256(b"expiry-deadline-race"),
            settings=self.settings,
            payload={"brief": "extend"},
            input_slots=(InputSlot("paper.pdf", _sha256(b"paper"), 5),),
            expires_at=100.0,
        )
        reconcile = asyncio.create_task(
            services.reconcile_expired_reservations(now=100.0)
        )
        await services.candidates_ready.wait()
        context = await services._context(reservation.run_id)
        async with context.lock:
            context.reservation = replace(context.reservation, expires_at=200.0)
        services.release_candidates.set()

        self.assertEqual(await reconcile, ())
        self.assertEqual(self.store.read(reservation.run_id).state, "reserved")

    async def test_start_ownership_wins_expiry_candidate_race(self) -> None:
        """Break caught: expiry failing a queued run after start acquired ownership."""
        services = _PausedExpiryServices(
            self.runs_dir,
            control_store=self.store,
            supervisor=self.supervisor,
        )
        reservation = await services.reserve(
            run_id="expiry-start-race",
            artifact_type="poster",
            idempotency_key="expiry-start-race-key",
            request_digest=_sha256(b"expiry-start-race"),
            settings=self.settings,
            payload={"brief": "race"},
            input_slots=(),
            expires_at=100.0,
        )
        reconcile = asyncio.create_task(
            services.reconcile_expired_reservations(now=100.0)
        )
        await services.candidates_ready.wait()
        start = asyncio.create_task(
            services.start(
                reservation.run_id,
                reservation.upload_token,
                self._request_factory,
            )
        )
        await self.supervisor.start_entered.wait()
        services.release_candidates.set()

        self.assertEqual(await reconcile, ())
        self.assertEqual(self.store.read(reservation.run_id).state, "queued")
        self.supervisor.start_release.set()
        self.assertIs(await start, self.supervisor.supervised)

    async def test_successful_start_replaces_sensitive_context_with_tombstone(self) -> None:
        """Break caught: registered workers leaving Settings, payload, and raw token resident."""
        settings = Settings(
            anthropic_api_key="start-tombstone-secret",
            anthropic_base_url=None,
            gemini_api_key="",
            designer_model="designer",
            critic_model="critic",
            repo_root=Path(__file__).resolve().parents[1],
            out_dir=self.root / "out",
        )
        payload = _Payload(brief="tombstone-start")
        settings_ref = weakref.ref(settings)
        payload_ref = weakref.ref(payload)
        request_digest = _sha256(b"tombstone-start")
        reservation = await self.services.reserve(
            run_id="tombstone-start",
            artifact_type="poster",
            idempotency_key="tombstone-start-key",
            request_digest=request_digest,
            settings=settings,
            payload=payload,
            input_slots=(),
        )
        self.supervisor.start_release.set()
        await self.services.start(
            reservation.run_id,
            reservation.upload_token,
            self._request_factory,
        )
        del settings, payload
        gc.collect()

        retry = await self.services.reserve(
            run_id="ignored-tombstone-start",
            artifact_type="deck",
            idempotency_key="tombstone-start-key",
            request_digest=request_digest,
            settings=self.settings,
            payload={"brief": "replacement"},
            input_slots=(),
        )
        self.assertIsNone(settings_ref())
        self.assertIsNone(payload_ref())
        self.assertTrue(retry.reused)
        self.assertEqual(retry.run_id, reservation.run_id)
        self.assertEqual(retry.upload_token, "")

    async def test_confirmed_cancel_replaces_sensitive_context_with_tombstone(self) -> None:
        """Break caught: confirmed cancellation retaining request credentials and token."""
        settings = Settings(
            anthropic_api_key="cancel-tombstone-secret",
            anthropic_base_url=None,
            gemini_api_key="",
            designer_model="designer",
            critic_model="critic",
            repo_root=Path(__file__).resolve().parents[1],
            out_dir=self.root / "out",
        )
        payload = _Payload(brief="tombstone-cancel")
        settings_ref = weakref.ref(settings)
        payload_ref = weakref.ref(payload)
        request_digest = _sha256(b"tombstone-cancel")
        reservation = await self.services.reserve(
            run_id="tombstone-cancel",
            artifact_type="poster",
            idempotency_key="tombstone-cancel-key",
            request_digest=request_digest,
            settings=settings,
            payload=payload,
            input_slots=(),
        )
        outcome = await self.services.cancel(reservation.run_id, "tombstone")
        self.assertTrue(outcome.confirmed)
        del settings, payload
        gc.collect()

        retry = await self.services.reserve(
            run_id="ignored-tombstone-cancel",
            artifact_type="deck",
            idempotency_key="tombstone-cancel-key",
            request_digest=request_digest,
            settings=self.settings,
            payload={"brief": "replacement"},
            input_slots=(),
        )
        self.assertIsNone(settings_ref())
        self.assertIsNone(payload_ref())
        self.assertTrue(retry.reused)
        self.assertEqual(retry.run_id, reservation.run_id)
        self.assertEqual(retry.upload_token, "")

    async def test_cancel_caller_cancellation_still_sanitizes_after_confirmed_terminal(
        self,
    ) -> None:
        """Break caught: caller disconnect owning confirmed-cancel secret cleanup."""
        supervisor = _BlockingCancelSupervisor(self.store)
        services = WebRunServices(
            self.runs_dir,
            control_store=self.store,
            supervisor=supervisor,
        )
        reservation = await services.reserve(
            run_id="cancelled-cancel-caller",
            artifact_type="poster",
            idempotency_key="cancelled-cancel-caller-key",
            request_digest=_sha256(b"cancelled-cancel-caller"),
            settings=self.settings,
            payload=_Payload(brief="cancelled-cancel"),
            input_slots=(),
        )
        caller = asyncio.create_task(
            services.cancel(reservation.run_id, "caller_disconnected")
        )
        await supervisor.cancel_entered.wait()

        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        supervisor.cancel_release.set()
        context = await services._context(reservation.run_id)
        await _wait_for(lambda: self.store.read(reservation.run_id).state == "cancelled")

        self.assertIsNone(context.settings)
        self.assertIsNone(context.payload)
        self.assertEqual(context.reservation.upload_token, "")

    async def test_cancel_request_event_claim_retries_after_caller_cancellation(self) -> None:
        """Break caught: a disconnected response permanently consuming event delivery."""
        supervisor = _BlockingCancelSupervisor(self.store)
        services = WebRunServices(
            self.runs_dir,
            control_store=self.store,
            supervisor=supervisor,
        )
        reservation = await services.reserve(
            run_id="cancelled-event-claim",
            artifact_type="poster",
            idempotency_key="cancelled-event-claim-key",
            request_digest=_sha256(b"cancelled-event-claim"),
            settings=self.settings,
            payload={"brief": "event"},
            input_slots=(),
        )
        caller = asyncio.create_task(
            services.cancel(reservation.run_id, "caller_disconnected")
        )
        await supervisor.cancel_entered.wait()
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        supervisor.cancel_release.set()
        await _wait_for(lambda: self.store.read(reservation.run_id).state == "cancelled")

        retry = await services.cancel(reservation.run_id, "retry_delivery")
        repeated = await services.cancel(reservation.run_id, "already_delivered")

        self.assertTrue(retry.cancel_request_event_required)
        self.assertFalse(repeated.cancel_request_event_required)

    async def test_cancel_disconnect_then_terminal_supervisor_error_cleans_lifecycle(
        self,
    ) -> None:
        """Break caught: terminal cancel failure retaining secrets and a stale task."""
        supervisor = _TerminalFailingCancelSupervisor(self.store)
        services = WebRunServices(
            self.runs_dir,
            control_store=self.store,
            supervisor=supervisor,
        )
        reservation = await services.reserve(
            run_id="terminal-cancel-error",
            artifact_type="poster",
            idempotency_key="terminal-cancel-error-key",
            request_digest=_sha256(b"terminal-cancel-error"),
            settings=self.settings,
            payload=_Payload(brief="terminal-cancel-error"),
            input_slots=(),
        )
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        unhandled: list[dict] = []
        loop.set_exception_handler(lambda _loop, details: unhandled.append(details))
        try:
            caller = asyncio.create_task(
                services.cancel(reservation.run_id, "caller_disconnected")
            )
            await supervisor.cancel_entered.wait()
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            supervisor.cancel_release.set()
            context = await services._context(reservation.run_id)
            await _wait_for(
                lambda: self.store.read(reservation.run_id).state == "cancelled"
            )
            await _wait_for(lambda: context.cancel_task is None)
            del caller
            gc.collect()
            await asyncio.sleep(0)

            self.assertIsNone(context.settings)
            self.assertIsNone(context.payload)
            self.assertEqual(context.reservation.upload_token, "")
            pending = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and not task.done()
                and (
                    "_cancel_with_context" in task.get_coro().__qualname__
                    or "_finish_cancel_and_adopt" in task.get_coro().__qualname__
                )
            ]
            self.assertEqual(pending, [])
            self.assertEqual(unhandled, [])

            retry = await services.cancel(reservation.run_id, "retry_delivery")
            repeated = await services.cancel(reservation.run_id, "already_delivered")
            self.assertTrue(retry.cancel_request_event_required)
            self.assertFalse(repeated.cancel_request_event_required)
            self.assertEqual(supervisor.cancel_calls, 2)
        finally:
            loop.set_exception_handler(previous_handler)

    async def test_real_supervisor_start_cancel_registration_race(self) -> None:
        """Break caught: service locking allowing a worker to escape a queued cancel race."""
        real_store = RunControlStore(self.runs_dir)
        real_supervisor = RunSupervisor(
            self.runs_dir,
            control_store=real_store,
            worker_command=(sys.executable, str(FIXTURE)),
            grace_s=0.1,
            root_registration_delay_s=0.05,
        )
        services = WebRunServices(
            self.runs_dir,
            control_store=real_store,
            supervisor=real_supervisor,
            upload_close_timeout_s=0.1,
        )
        reservation = await services.reserve(
            run_id="real-start-cancel-race",
            artifact_type="poster",
            idempotency_key="real-race-key",
            request_digest=_sha256(b"real-race-request"),
            settings=self.settings,
            payload={"brief": "blocked"},
            input_slots=(),
        )
        start = asyncio.create_task(
            services.start(
                reservation.run_id,
                reservation.upload_token,
                self._request_factory,
            )
        )
        ledger_path = self.runs_dir / reservation.run_id / "process_ledger.json"
        await _wait_for(
            lambda: ledger_path.exists()
            and '"status": "spawning"' in ledger_path.read_text(encoding="utf-8")
        )

        cancel = asyncio.create_task(services.cancel(reservation.run_id, "race"))
        supervised = await start
        outcome = await cancel

        self.assertTrue(outcome.confirmed)
        self.assertEqual(outcome.state, "cancelled")
        self.assertTrue(outcome.terminal_event_handled_by_supervisor)
        identity = next(
            record.identity
            for record in ProcessLedger(self.runs_dir / reservation.run_id).read().processes
            if record.role == "root-worker"
        )
        self.assertFalse(process_is_alive(identity))
        self.assertIsNotNone(supervised.process.returncode)
        self.assertEqual(real_store.read(reservation.run_id).state, "cancelled")
        events = [
            json.loads(line)
            for line in (self.runs_dir / reservation.run_id / "run_events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        terminal = [event for event in events if event.get("event") == "run.cancelled"]
        self.assertEqual(len(terminal), 1)


if __name__ == "__main__":
    unittest.main()
