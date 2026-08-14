from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import autodesign.run_supervisor as run_supervisor_module
import autodesign.run_worker as run_worker_module
import autodesign.candidate_publish as candidate_publish_module
from autodesign.config import Settings
from autodesign.attempt_candidates import capture_attempt_candidate
from autodesign.attempt_selection import load_selection_journal
from autodesign.process_supervision import (
    ProcessLedger,
    process_identity,
    process_is_alive,
)
from autodesign.paper_bundle_jobs import (
    PaperBundleBarrierClosed,
    PaperBundleChildDescriptor,
    PaperBundleConflict,
    PaperBundleInputSlot,
)
from autodesign.run_control import RunControlStore
from autodesign.run_supervisor import (
    RunSupervisor,
    TerminalReconciliation,
    WorkerOutcome,
    WorkerExitDiagnostic,
)
from autodesign.run_worker_protocol import (
    VideoExportRetryWorkerRequest,
    encode_request,
)
from autodesign.web_run_services import WebRunServices
import scripts.web_server as web_server
from tests.test_video_web_delivery import _passed_delivery


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "derived_worker_process.py"


class WebDerivedRunSupervisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmp.name) / "out"
        self.runs_dir = self.out_dir / "runs"
        self.runs_dir.mkdir(parents=True)
        self.settings = Settings(
            anthropic_api_key="",
            anthropic_base_url=None,
            gemini_api_key="",
            designer_model="designer",
            critic_model="critic",
            repo_root=REPO_ROOT,
            out_dir=self.out_dir,
        )
        self._patches = (
            patch.object(web_server, "RUNS_DIR", self.runs_dir),
            patch.object(web_server, "_BOOT_OUT_DIR", self.out_dir),
            patch.object(web_server, "SETTINGS", self.settings),
            patch.object(web_server, "_RUN_ACCESS_CONTROL", False),
        )
        for current in self._patches:
            current.start()
        web_server._reset_web_run_runtime_for_tests()
        web_server._reset_paper_bundle_store_for_tests()
        store = RunControlStore(self.runs_dir)
        supervisor = RunSupervisor(
            self.runs_dir,
            control_store=store,
            worker_command=(sys.executable, str(FIXTURE)),
            grace_s=0.05,
            cancellation_quiescer=web_server._quiesce_web_completion_monitor,
        )
        web_server._WEB_RUN_RUNTIME = web_server._WebRunRuntime(
            runs_dir=self.runs_dir.resolve(),
            control_store=store,
            supervisor=supervisor,
            services=WebRunServices(
                self.runs_dir,
                control_store=store,
                supervisor=supervisor,
                upload_close_timeout_s=0.1,
            ),
        )
        self._client_context = TestClient(web_server.app)
        self.client = self._client_context.__enter__()

    def tearDown(self) -> None:
        for run_id in tuple(web_server._RUNS):
            control = self.runs_dir / run_id / "run_control.json"
            if control.is_file():
                try:
                    self.client.post(f"/api/runs/{run_id}/cancel")
                except BaseException:
                    pass
        self._client_context.__exit__(None, None, None)
        web_server._reset_web_run_runtime_for_tests()
        web_server._reset_paper_bundle_store_for_tests()
        for current in reversed(self._patches):
            current.stop()
        self._tmp.cleanup()

    def _complete_source(self, run_id: str, artifact_type: str) -> Path:
        store = RunControlStore(self.runs_dir)
        record = store.reserve(run_id, artifact_type)
        record = store.transition(run_id, record, "queued")
        record = store.transition(run_id, record, "running")
        record = store.transition(run_id, record, "completing")
        store.transition(run_id, record, "completed", publishable=True)
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _replace_with_cold_web_runtime(self) -> None:
        store = RunControlStore(self.runs_dir)
        supervisor = RunSupervisor(
            self.runs_dir,
            control_store=store,
            worker_command=(sys.executable, str(FIXTURE)),
            grace_s=0.05,
            cancellation_quiescer=web_server._quiesce_web_completion_monitor,
        )
        web_server._WEB_RUN_RUNTIME = web_server._WebRunRuntime(
            runs_dir=self.runs_dir.resolve(),
            control_store=store,
            supervisor=supervisor,
            services=WebRunServices(
                self.runs_dir,
                control_store=store,
                supervisor=supervisor,
                upload_close_timeout_s=0.1,
            ),
        )

    @staticmethod
    def _tree_bytes(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def _failed_source_with_candidates(
        self,
        run_id: str,
        *,
        attempts: int = 1,
    ):
        store = RunControlStore(self.runs_dir)
        record = store.reserve(run_id, "landing")
        record = store.transition(run_id, record, "queued")
        record = store.transition(run_id, record, "running")
        run_dir = self.runs_dir / run_id
        candidates = []
        for attempt in range(1, attempts + 1):
            attempt_dir = run_dir / "landing_author" / f"attempt_{attempt:02d}"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "index.html").write_text(
                f"<!doctype html><main>Attempt {attempt}</main>",
                encoding="utf-8",
            )
            (attempt_dir / "preview.png").write_bytes(
                f"preview-{attempt}".encode("utf-8")
            )
            (attempt_dir / "validation.json").write_text(
                '{"accepted":true}',
                encoding="utf-8",
            )
            (attempt_dir / "designer_author_done.json").write_text(
                '{"status":"done"}',
                encoding="utf-8",
            )
            candidates.append(capture_attempt_candidate(
                run_dir=run_dir,
                attempt_dir=attempt_dir,
                artifact_type="landing",
                attempt=attempt,
                max_attempts=max(1, attempts),
                source_path="index.html",
                dependency_paths=["designer_author_done.json"],
                preview_paths=["preview.png"],
                validation_summary_path="validation.json",
                safety_state="ready",
                hard_blockers=[],
                warnings=[],
            ))
        diagnostic_final = run_dir / "final"
        diagnostic_final.mkdir(parents=True)
        (diagnostic_final / "diagnostic.txt").write_text(
            "failed-source-diagnostic",
            encoding="utf-8",
        )
        store.transition(run_id, record, "failed", publishable=False)
        return run_dir, candidates

    def _failed_source_candidate_draft(
        self,
        source_run_id: str,
    ) -> tuple[Path, str, object]:
        source_dir, candidates = self._failed_source_with_candidates(source_run_id)
        candidate = candidates[0]
        draft_run_id = self._candidate_draft_for_source(source_run_id, candidate)
        return source_dir, draft_run_id, candidate

    def _candidate_draft_for_source(self, source_run_id: str, candidate) -> str:
        draft_run_id = f"{source_run_id}-draft"
        draft_dir = self.runs_dir / draft_run_id
        (draft_dir / "final").mkdir(parents=True)
        (draft_dir / "final" / "index.html").write_text(
            "<!doctype html><main>Edited failed-source draft</main>",
            encoding="utf-8",
        )
        lineage = {
            "schema_version": 1,
            "materialization_version": 2,
            "status": "draft",
            "artifact_type": "landing",
            "source_run_id": source_run_id,
            "source_attempt": candidate.attempt,
            "source_candidate_id": candidate.candidate_id,
            "source_candidate_sha256": candidate.source_sha256,
            "conversation_id": "failed-source-conversation",
        }
        (draft_dir / "candidate_draft_lineage.json").write_text(
            json.dumps(lineage, sort_keys=True),
            encoding="utf-8",
        )
        store = RunControlStore(self.runs_dir)
        record = store.reserve(
            draft_run_id,
            "landing",
            parent_job_id=source_run_id,
        )
        record = store.transition(draft_run_id, record, "queued")
        record = store.transition(draft_run_id, record, "running")
        record = store.transition(draft_run_id, record, "completing")
        store.transition(draft_run_id, record, "completed", publishable=True)
        (draft_dir / "derived_job.json").write_text(
            json.dumps({
                "version": 1,
                "job_kind": "attempt_fork",
                "run_id": draft_run_id,
                "parent_run_id": source_run_id,
                "artifact_type": "landing",
                "conversation_id": "failed-source-conversation",
                "baseline_artifact_json": "",
                "source_artifact_id": f"art_{source_run_id}",
                "artifact_name": "Attempt 1 draft",
                "source_relative_path": "candidate_draft_lineage.json",
            }, sort_keys=True),
            encoding="utf-8",
        )
        return draft_run_id

    def _legacy_failed_source_with_candidate(self, run_id: str):
        run_dir, candidates = self._failed_source_with_candidates(run_id)
        (run_dir / "run_control.json").unlink()
        (run_dir / "run_events.jsonl").write_text(
            "\n".join((
                json.dumps({"event": "run.start", "run_id": run_id}),
                json.dumps({
                    "event": "run.done",
                    "run_id": run_id,
                    "terminal_status": "fail",
                }),
            )) + "\n",
            encoding="utf-8",
        )
        return run_dir, candidates[0]

    def _failed_bundle_source(
        self,
        job_id: str,
        *,
        owner_id: str = "local",
        artifact_type: str = "landing",
        candidate_artifact_type: str | None = None,
        candidate_safety_state: str = "ready",
        source_state: str = "failed",
    ):
        self.assertIn(source_state, {"running", "completing", "failed", "completed"})
        controls = RunControlStore(self.runs_dir)
        bundle_store = web_server._paper_bundle_store()

        async def reserve_child(
            child_artifact_type: str,
            parent_job_id: str,
            child_run_id: str,
        ) -> PaperBundleChildDescriptor:
            controls.reserve(
                child_run_id,
                child_artifact_type,
                parent_job_id=parent_job_id,
            )
            return PaperBundleChildDescriptor(
                run_id=child_run_id,
                artifact_type=child_artifact_type,
                conversation_id=f"conversation-{child_artifact_type}",
                input_slots=(
                    PaperBundleInputSlot(
                        name="paper.pdf",
                        expected_sha256="a" * 64,
                        expected_size=123,
                    ),
                ),
                upload_token=f"token-{child_artifact_type}",
                request_digest=hashlib.sha256(
                    child_artifact_type.encode("utf-8")
                ).hexdigest(),
                expires_at=2_000_000_000.0,
            )

        async def cleanup_child(_child_run_id: str) -> None:
            return None

        creation = asyncio.run(bundle_store.create_with_factory(
            owner_id=owner_id,
            conversation_id=f"conversation-{job_id}",
            source_name="paper.pdf",
            prompt_version="paper-suite-v2",
            idempotency_key=f"create-{job_id}",
            request_digest=hashlib.sha256(job_id.encode("utf-8")).hexdigest(),
            child_reservation_factory=reserve_child,
            cleanup_child=cleanup_child,
            job_id=job_id,
        ))
        source_run_id = creation.record.children[artifact_type].run_id
        source_record = controls.read(source_run_id)
        source_record = controls.transition(source_run_id, source_record, "queued")
        source_record = controls.transition(source_run_id, source_record, "running")
        run_dir = self.runs_dir / source_run_id
        authored_type = candidate_artifact_type or artifact_type
        author_dir_name = {
            "poster": "poster_author",
            "deck": "slides_author",
            "landing": "landing_author",
            "video": "video_author",
        }[authored_type]
        source_name = {
            "poster": "poster.html",
            "deck": "deck.html",
            "landing": "index.html",
            "video": "index.html",
        }[authored_type]
        attempt_dir = run_dir / author_dir_name / "attempt_01"
        attempt_dir.mkdir(parents=True)
        (attempt_dir / source_name).write_text(
            "<!doctype html><main>Bundle attempt 1</main>",
            encoding="utf-8",
        )
        (attempt_dir / "preview.png").write_bytes(b"bundle-preview")
        (attempt_dir / "validation.json").write_text(
            '{"accepted":true}',
            encoding="utf-8",
        )
        (attempt_dir / "designer_author_done.json").write_text(
            '{"status":"done"}',
            encoding="utf-8",
        )
        candidate = capture_attempt_candidate(
            run_dir=run_dir,
            attempt_dir=attempt_dir,
            artifact_type=authored_type,
            attempt=1,
            max_attempts=1,
            source_path=source_name,
            dependency_paths=["designer_author_done.json"],
            preview_paths=["preview.png"],
            validation_summary_path="validation.json",
            safety_state=candidate_safety_state,
            hard_blockers=(
                [{
                    "issue_id": "original_attempt_blocked",
                    "message": "Original attempt requires Canvas repair",
                }]
                if candidate_safety_state == "blocked"
                else []
            ),
            warnings=[],
        )
        if source_state == "completed":
            source_record = controls.transition(
                source_run_id,
                source_record,
                "completing",
            )
            source_record = controls.transition(
                source_run_id,
                source_record,
                "completed",
                publishable=True,
            )
        elif source_state == "failed":
            source_record = controls.transition(
                source_run_id,
                source_record,
                "failed",
                publishable=False,
            )
        elif source_state == "completing":
            source_record = controls.transition(
                source_run_id,
                source_record,
                "completing",
            )
        self.assertEqual(source_record.parent_job_id, job_id)
        bundle_store.read_owned(
            job_id,
            owner_id,
            child_status_provider=web_server._paper_bundle_child_snapshot,
        )
        return bundle_store, run_dir, source_run_id, candidate

    def _reserve_direct_publication(
        self,
        source_run_id: str,
        candidate,
        *,
        idempotency_key: str,
    ):
        response = self.client.post(
            f"/api/runs/{source_run_id}/attempts/{candidate.attempt}/publish",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json={
                "conversation_id": f"conversation-{idempotency_key}",
                "expected_candidate_sha256": candidate.source_sha256,
                "idempotency_key": idempotency_key,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def _reserve_canvas_publication(self, draft_run_id: str):
        response = self.client.post(
            f"/api/artifacts/art_{draft_run_id}/publish-candidate-draft",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json={"conversation_id": f"canvas-publish-{draft_run_id}"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def _materialize_and_accept_direct_publication(
        self,
        published_run_id: str,
        source_run_id: str,
        candidate,
        *,
        clear_runtime_state: bool = False,
        source_draft_run_id: str | None = None,
    ) -> None:
        with patch.object(
            candidate_publish_module,
            "validate_candidate_draft",
            return_value=[],
        ):
            result = candidate_publish_module.run_candidate_publish_job(
                run_id=published_run_id,
                parent_run_id=source_draft_run_id or source_run_id,
                conversation_id=f"conversation-{published_run_id}",
                settings=self.settings,
                cancellation_token=web_server.CancellationToken.never(
                    published_run_id
                ),
                source_attempt=(None if source_draft_run_id else candidate.attempt),
                expected_candidate_sha256=(
                    None if source_draft_run_id else candidate.source_sha256
                ),
            )
        self.assertEqual(result["lineage"]["status"], "validated")
        controls = RunControlStore(self.runs_dir)
        record = controls.read(published_run_id)
        record = controls.transition(published_run_id, record, "running")
        controls.transition(published_run_id, record, "completing")
        if clear_runtime_state:
            web_server._RUNS.pop(published_run_id, None)
            web_server._reset_paper_bundle_store_for_tests()
        supervisor = web_server._web_run_runtime().supervisor
        supervisor._terminal_reconciler = web_server._reconcile_run_terminal_artifact

        async def accept() -> None:
            await supervisor.accept_completion(
                published_run_id,
                terminal_state="completed",
                publishable=True,
                result_digest="b" * 64,
            )

        self.client.portal.call(accept)

    def _prepare_video_retry_completion(
        self,
        run_id: str,
        parent_run_id: str,
        *,
        completing: bool,
    ) -> tuple[RunControlStore, dict[str, object], VideoExportRetryWorkerRequest]:
        store = RunControlStore(self.runs_dir)
        record = store.reserve(run_id, "video", parent_job_id=parent_run_id)
        record = store.transition(run_id, record, "queued")
        record = store.transition(run_id, record, "running")
        if completing:
            store.transition(run_id, record, "completing")
        run_dir = self.runs_dir / run_id
        manifest_path, mp4_path = _passed_delivery(run_dir)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        descriptor = {
            "version": 1,
            "job_kind": "video_export_retry",
            "run_id": run_id,
            "parent_run_id": parent_run_id,
            "artifact_type": "video",
            "conversation_id": "warning-conversation",
            "baseline_artifact_json": "{}",
            "source_artifact_id": f"art_{parent_run_id}",
            "artifact_name": "Video",
            "source_relative_path": "hyperframes-paper-video",
        }
        (run_dir / "derived_job.json").write_text(
            json.dumps(descriptor),
            encoding="utf-8",
        )
        result: dict[str, object] = {
            "run_id": run_id,
            "ok": True,
            "phase": "done",
            "project_dir": str(manifest_path.parent),
            "manifest_path": str(manifest_path),
            "mp4_path": str(mp4_path),
            "media_probe_path": str(manifest_path.parent / "media_probe.json"),
            "render_started_at": str(manifest["render_started_at"]),
        }
        request = VideoExportRetryWorkerRequest(
            job_kind="video_export_retry",
            run_id=run_id,
            parent_run_id=parent_run_id,
            source_project=str(
                self.runs_dir / parent_run_id / "hyperframes-paper-video"
            ),
            conversation_id="warning-conversation",
            baseline_artifact_json="{}",
            runs_dir=str(self.runs_dir),
        )
        return store, result, request

    @staticmethod
    def _worker_outcome_with_warnings(
        *,
        run_id: str,
        ok: bool,
        error: str | None,
        warnings: tuple[str, ...],
        result: dict[str, object] | None = None,
    ):
        outcome = web_server.WorkerOutcome(
            run_id=run_id,
            job_kind="video_export_retry",
            returncode=0 if ok else 1,
            ok=ok,
            result=result,
            error=error,
            relayed_events=0,
            failure_phase=None if ok else "final_pointer",
        )
        object.__setattr__(outcome, "pointer_cleanup_warnings", warnings)
        return outcome

    @staticmethod
    def _write_video_retry_worker_result(
        run_dir: Path,
        run_id: str,
        *,
        warnings: tuple[str, ...],
        result: dict[str, object] | None,
    ) -> None:
        envelope = (
            {
                "job_kind": "video_export_retry",
                "run_id": run_id,
                "ok": True,
                "result": {
                    **(result or {}),
                    "pointer_cleanup_warnings": list(warnings),
                },
            }
            if result is not None
            else {
                "job_kind": "video_export_retry",
                "run_id": run_id,
                "ok": False,
                "error": {
                    "type": "VideoPointerPublicationError",
                    "message": "derived retry failed",
                    "phase": "final_pointer",
                    "pointer_cleanup_warnings": list(warnings),
                },
            }
        )
        (run_dir / "worker_result.json").write_text(
            json.dumps(envelope),
            encoding="utf-8",
        )

    @staticmethod
    def _set_derived_descriptor_case(run_dir: Path, descriptor_case: str) -> None:
        descriptor_path = run_dir / "derived_job.json"
        if descriptor_case == "valid":
            return
        if descriptor_case == "missing":
            descriptor_path.unlink()
            return
        if descriptor_case == "malformed":
            descriptor_path.write_text("{broken", encoding="utf-8")
            return
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        if descriptor_case == "mismatched-kind":
            descriptor["job_kind"] = "pptx_export"
        elif descriptor_case == "mismatched-run":
            descriptor["run_id"] = "other-derived-run"
        else:
            raise AssertionError(f"unknown descriptor case: {descriptor_case}")
        descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")

    @staticmethod
    def _finalize_test_cancellation(
        store: RunControlStore,
        run_id: str,
    ) -> None:
        store.finalize_cancel(
            run_id,
            {
                "termination_verified": True,
                "reason": "test cancellation",
            },
        )

    def _reserve_editable_video(self, source_run_id: str) -> tuple[str, str]:
        self._complete_source(source_run_id, "video")
        artifact = {
            "artifact_id": f"art_{source_run_id}",
            "artifact_type": "video",
            "video_project": {
                "fps": 30,
                "scenes": [{"id": "scene-1", "duration_s": 1, "layers": []}],
            },
        }
        with patch.object(web_server, "_require_artifact_runtime"):
            response = self.client.post(
                "/api/video/render",
                headers={"X-Autodesign-Reserve-Only": "true"},
                json={"artifact": artifact, "conversation_id": "conversation"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload.get("start_token"))
        record = RunControlStore(self.runs_dir).read(payload["run_id"])
        self.assertEqual(record.state, "queued")
        self.assertIsNone(record.worker_pid)
        return str(payload["run_id"]), str(payload["start_token"])

    def test_editable_video_ack_has_registered_supervised_worker(self) -> None:
        source_run_id = "editable-video-source"
        self._complete_source(source_run_id, "video")
        artifact = {
            "artifact_id": f"art_{source_run_id}",
            "artifact_type": "video",
            "video_project": {
                "fps": 30,
                "scenes": [{"id": "scene-1", "duration_s": 1, "layers": []}],
            },
        }

        with patch.object(web_server, "_require_artifact_runtime"):
            response = self.client.post(
                "/api/video/render",
                json={"artifact": artifact, "conversation_id": "conversation"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        record = RunControlStore(self.runs_dir).read(run_id)
        self.assertEqual(record.state, "running")
        self.assertIsNotNone(record.worker_pid)
        assert record.worker_pid is not None
        identity = process_identity(record.worker_pid)
        self.assertTrue(process_is_alive(identity))
        self.assertTrue((self.runs_dir / run_id / "derived_job.json").is_file())

        cancelled = self.client.post(f"/api/runs/{run_id}/cancel")

        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertTrue(cancelled.json()["confirmed"])
        self.assertEqual(
            RunControlStore(self.runs_dir).read(run_id).state,
            "cancelled",
        )
        self.assertFalse(process_is_alive(identity))

    def test_attempt_fork_reserves_before_start_and_is_cancellable(self) -> None:
        source_run_id = "attempt-fork-source"
        source_dir = self._complete_source(source_run_id, "landing")
        attempt_dir = source_dir / "landing_author" / "attempt_01"
        attempt_dir.mkdir(parents=True)
        (attempt_dir / "index.html").write_text(
            "<!doctype html><main>Attempt</main>",
            encoding="utf-8",
        )
        (attempt_dir / "preview.png").write_bytes(b"preview")
        (attempt_dir / "validation.json").write_text(
            '{"accepted":true}',
            encoding="utf-8",
        )
        capture_attempt_candidate(
            run_dir=source_dir,
            attempt_dir=attempt_dir,
            artifact_type="landing",
            attempt=1,
            max_attempts=4,
            source_path="index.html",
            dependency_paths=[],
            preview_paths=["preview.png"],
            validation_summary_path="validation.json",
            safety_state="ready",
            hard_blockers=[],
            warnings=[],
        )

        reserved = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/fork",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json={"conversation_id": "conversation"},
        )

        self.assertEqual(reserved.status_code, 200, reserved.text)
        run_id = reserved.json()["run_id"]
        token = reserved.json()["start_token"]
        before = RunControlStore(self.runs_dir).read(run_id)
        self.assertEqual(before.state, "queued")
        self.assertIsNone(before.worker_pid)
        self.assertIsNone(web_server._RUNS[run_id].task)

        started = self.client.post(
            f"/api/runs/{run_id}/start",
            headers={"X-Autodesign-Upload-Token": token},
        )

        self.assertEqual(started.status_code, 200, started.text)
        running = RunControlStore(self.runs_dir).read(run_id)
        self.assertEqual(running.state, "running")
        self.assertIsNotNone(running.worker_pid)
        cancelled = self.client.post(f"/api/runs/{run_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertTrue(cancelled.json()["confirmed"])

    def test_failed_source_ready_attempt_reserves_direct_publish_without_mutation(
        self,
    ) -> None:
        source_run_id = "failed-direct-source"
        source_dir, candidates = self._failed_source_with_candidates(source_run_id)
        candidate = candidates[0]
        before = self._tree_bytes(source_dir)

        response = self.client.post(
            f"/api/runs/{source_run_id}/attempts/{candidate.attempt}/publish",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json={
                "conversation_id": "failed-direct-conversation",
                "expected_candidate_sha256": candidate.source_sha256,
                "idempotency_key": "failed-direct-publication-1",
            },
        )

        self.assertEqual(
            response.status_code,
            200,
            "a failed source with a Ready immutable attempt must reserve a derived publication",
        )
        payload = response.json()
        self.assertEqual(payload["progress_mode"], "attempt_publish")
        self.assertTrue(payload.get("start_token"))
        child = RunControlStore(self.runs_dir).read(payload["run_id"])
        self.assertEqual(child.state, "queued")
        self.assertEqual(child.parent_job_id, source_run_id)
        self.assertEqual(self._tree_bytes(source_dir), before)
        descriptor = json.loads(
            (
                self.runs_dir
                / payload["run_id"]
                / "candidate_publish_request.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(descriptor["version"], 1)
        self.assertNotIn("paper_bundle_job_id", descriptor)

    def test_bundle_attempt_publish_reserves_generation_and_writes_v2_binding(
        self,
    ) -> None:
        job_id = "bundle-direct-binding"
        bundle_store, source_dir, source_run_id, candidate = (
            self._failed_bundle_source(job_id)
        )
        before = self._tree_bytes(source_dir)

        response = self._reserve_direct_publication(
            source_run_id,
            candidate,
            idempotency_key="bundle-direct-binding-key",
        )

        published_run_id = response.json()["run_id"]
        descriptor = json.loads(
            (
                self.runs_dir
                / published_run_id
                / "candidate_publish_request.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(descriptor["version"], 2)
        self.assertEqual(descriptor["paper_bundle_job_id"], job_id)
        self.assertEqual(descriptor["paper_bundle_owner_id"], "local")
        self.assertEqual(descriptor["paper_bundle_artifact_type"], "landing")
        self.assertEqual(descriptor["publication_generation"], 1)
        parent = bundle_store.read_owned(job_id, "local")
        self.assertEqual(parent.publication_generations["landing"], 1)
        self.assertNotIn("landing", parent.publications)
        self.assertEqual(self._tree_bytes(source_dir), before)

    def test_running_bundle_attempt_can_reserve_publication_without_stopping_source(
        self,
    ) -> None:
        job_id = "bundle-running-binding"
        bundle_store, source_dir, source_run_id, candidate = (
            self._failed_bundle_source(job_id, source_state="running")
        )
        before = self._tree_bytes(source_dir)

        response = self._reserve_direct_publication(
            source_run_id,
            candidate,
            idempotency_key="bundle-running-binding-key",
        )

        published_run_id = response.json()["run_id"]
        descriptor = json.loads(
            (
                self.runs_dir
                / published_run_id
                / "candidate_publish_request.json"
            ).read_text(encoding="utf-8")
        )
        source_control = RunControlStore(self.runs_dir).read(source_run_id)
        parent = bundle_store.read_owned(job_id, "local")
        self.assertEqual(descriptor["version"], 2)
        self.assertEqual(descriptor["publication_generation"], 1)
        self.assertEqual(source_control.state, "running")
        self.assertFalse(
            (source_dir / "attempt_candidates" / "selection.json").exists()
        )
        self.assertNotIn("landing", parent.publications)
        self.assertEqual(self._tree_bytes(source_dir), before)

    def test_running_bundle_validation_failure_does_not_stop_source(self) -> None:
        job_id = "bundle-running-validation-failure"
        _bundle_store, source_dir, source_run_id, candidate = (
            self._failed_bundle_source(job_id, source_state="running")
        )
        response = self._reserve_direct_publication(
            source_run_id,
            candidate,
            idempotency_key="bundle-running-validation-failure-key",
        )
        published_run_id = response.json()["run_id"]
        controls = RunControlStore(self.runs_dir)
        child = controls.read(published_run_id)
        child = controls.transition(published_run_id, child, "running")
        controls.transition(published_run_id, child, "completing")
        descriptor = web_server._read_derived_job_descriptor(published_run_id)
        self.assertIsNotNone(descriptor)
        assert descriptor is not None
        state = web_server._RUNS[published_run_id]
        outcome = WorkerOutcome(
            run_id=published_run_id,
            job_kind="candidate_publish",
            returncode=1,
            ok=False,
            result=None,
            error="candidate validation failed",
            relayed_events=0,
            failure_phase="validation",
        )

        async def monitor() -> None:
            await web_server._monitor_supervised_derived_job(
                run_id=published_run_id,
                state=state,
                job_kind="candidate_publish",
                parent_run_id=source_run_id,
                descriptor=descriptor,
                recovered_outcome=outcome,
            )

        self.client.portal.call(monitor)

        self.assertEqual(controls.read(published_run_id).state, "failed")
        self.assertEqual(controls.read(source_run_id).state, "running")
        self.assertFalse(
            (source_dir / "attempt_candidates" / "selection.json").exists()
        )

    def test_running_bundle_publication_quiesces_source_after_validation_then_commits(
        self,
    ) -> None:
        job_id = "bundle-running-publication"
        bundle_store, _source_dir, source_run_id, candidate = (
            self._failed_bundle_source(job_id, source_state="running")
        )
        response = self._reserve_direct_publication(
            source_run_id,
            candidate,
            idempotency_key="bundle-running-publication-key",
        )
        published_run_id = response.json()["run_id"]
        with patch.object(
            candidate_publish_module,
            "validate_candidate_draft",
            return_value=[],
        ):
            result = candidate_publish_module.run_candidate_publish_job(
                run_id=published_run_id,
                parent_run_id=source_run_id,
                conversation_id="bundle-running-publication-conversation",
                settings=self.settings,
                cancellation_token=web_server.CancellationToken.never(
                    published_run_id
                ),
                source_attempt=candidate.attempt,
                expected_candidate_sha256=candidate.source_sha256,
            )
        controls = RunControlStore(self.runs_dir)
        child = controls.read(published_run_id)
        child = controls.transition(published_run_id, child, "running")
        controls.transition(published_run_id, child, "completing")
        descriptor = web_server._read_derived_job_descriptor(published_run_id)
        self.assertIsNotNone(descriptor)
        assert descriptor is not None
        published_state = web_server._RUNS[published_run_id]
        outcome = WorkerOutcome(
            run_id=published_run_id,
            job_kind="candidate_publish",
            returncode=0,
            ok=True,
            result=result,
            error=None,
            relayed_events=0,
        )
        web_server._web_run_runtime().supervisor._terminal_reconciler = (
            web_server._reconcile_run_terminal_artifact
        )

        async def monitor() -> None:
            source_state = web_server._RunState(
                artifact_type="landing",
                brief="source",
                conversation_id="bundle-running-source",
            )

            async def finish_source_after_selection() -> None:
                for _ in range(200):
                    if (
                        load_selection_journal(self.runs_dir / source_run_id)
                        is not None
                    ):
                        break
                    await asyncio.sleep(0.005)
                else:
                    self.fail("candidate publication did not select the source attempt")
                source = controls.read(source_run_id)
                if source.state == "running":
                    source = controls.transition(source_run_id, source, "completing")
                controls.transition(
                    source_run_id,
                    source,
                    "completed",
                    publishable=True,
                )
                await web_server._reconcile_paper_bundle_for_run(
                    source_run_id,
                    owner_id="local",
                )

            source_state.task = asyncio.create_task(finish_source_after_selection())
            web_server._RUNS[source_run_id] = source_state
            await web_server._monitor_supervised_derived_job(
                run_id=published_run_id,
                state=published_state,
                job_kind="candidate_publish",
                parent_run_id=source_run_id,
                descriptor=descriptor,
                recovered_outcome=outcome,
            )
            await source_state.task

        self.client.portal.call(monitor)

        self.assertEqual(controls.read(source_run_id).state, "completed")
        self.assertEqual(controls.read(published_run_id).state, "completed")
        parent = bundle_store.read_owned(job_id, "local")
        self.assertEqual(
            parent.publications["landing"].publication_run_id,
            published_run_id,
        )

    def test_superseded_running_bundle_publication_does_not_stop_source(self) -> None:
        job_id = "bundle-running-superseded"
        _bundle_store, source_dir, source_run_id, candidate = (
            self._failed_bundle_source(job_id, source_state="running")
        )
        first = self._reserve_direct_publication(
            source_run_id,
            candidate,
            idempotency_key="bundle-running-superseded-1",
        ).json()["run_id"]
        self._reserve_direct_publication(
            source_run_id,
            candidate,
            idempotency_key="bundle-running-superseded-2",
        )

        async def quiesce() -> None:
            async with web_server._run_tree_lock(source_run_id):
                await web_server._quiesce_bundle_candidate_publication_source(
                    first
                )

        with self.assertRaisesRegex(
            PaperBundleConflict,
            "superseded by a newer request",
        ):
            self.client.portal.call(quiesce)

        self.assertEqual(
            RunControlStore(self.runs_dir).read(source_run_id).state,
            "running",
        )
        self.assertFalse(
            (source_dir / "attempt_candidates" / "selection.json").exists()
        )

    def test_cancelled_bundle_publication_does_not_stop_running_source(self) -> None:
        job_id = "bundle-running-cancelled"
        bundle_store, source_dir, source_run_id, candidate = (
            self._failed_bundle_source(job_id, source_state="running")
        )
        published_run_id = self._reserve_direct_publication(
            source_run_id,
            candidate,
            idempotency_key="bundle-running-cancelled-key",
        ).json()["run_id"]
        bundle_store.request_cancel(job_id, "local")

        async def quiesce() -> None:
            async with web_server._run_tree_lock(source_run_id):
                await web_server._quiesce_bundle_candidate_publication_source(
                    published_run_id
                )

        with self.assertRaises(PaperBundleBarrierClosed):
            self.client.portal.call(quiesce)

        self.assertEqual(
            RunControlStore(self.runs_dir).read(source_run_id).state,
            "running",
        )
        self.assertFalse(
            (source_dir / "attempt_candidates" / "selection.json").exists()
        )

    def test_bundle_attempt_publish_fails_closed_for_unowned_or_invalid_parent(
        self,
    ) -> None:
        job_id = "bundle-direct-owner"
        _bundle_store, _source_dir, source_run_id, candidate = (
            self._failed_bundle_source(job_id, owner_id="owner-a")
        )
        payload = {
            "conversation_id": "bundle-owner-conversation",
            "expected_candidate_sha256": candidate.source_sha256,
            "idempotency_key": "bundle-owner-key",
        }
        with patch.object(web_server, "_run_owner_id", return_value="owner-b"):
            wrong_owner = self.client.post(
                f"/api/runs/{source_run_id}/attempts/1/publish",
                headers={"X-Autodesign-Reserve-Only": "true"},
                json=payload,
            )

        missing_source_id = "bundle-missing-parent-source"
        controls = RunControlStore(self.runs_dir)
        missing_record = controls.reserve(
            missing_source_id,
            "landing",
            parent_job_id="missing-parent",
        )
        missing_record = controls.transition(
            missing_source_id, missing_record, "queued"
        )
        missing_record = controls.transition(
            missing_source_id, missing_record, "running"
        )
        missing_dir = self.runs_dir / missing_source_id / "landing_author" / "attempt_01"
        missing_dir.mkdir(parents=True)
        (missing_dir / "index.html").write_text(
            "<!doctype html><main>missing parent</main>", encoding="utf-8"
        )
        (missing_dir / "validation.json").write_text(
            '{"accepted":true}', encoding="utf-8"
        )
        missing_candidate = capture_attempt_candidate(
            run_dir=self.runs_dir / missing_source_id,
            attempt_dir=missing_dir,
            artifact_type="landing",
            attempt=1,
            max_attempts=1,
            source_path="index.html",
            dependency_paths=[],
            preview_paths=[],
            validation_summary_path="validation.json",
            safety_state="ready",
            hard_blockers=[],
            warnings=[],
        )
        controls.transition(
            missing_source_id, missing_record, "failed", publishable=False
        )
        missing_parent = self.client.post(
            f"/api/runs/{missing_source_id}/attempts/1/publish",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json={
                "conversation_id": "missing-parent-conversation",
                "expected_candidate_sha256": missing_candidate.source_sha256,
                "idempotency_key": "missing-parent-key",
            },
        )

        mismatch_job_id = "bundle-direct-type-mismatch"
        _store, _run_dir, mismatch_source_id, mismatch_candidate = (
            self._failed_bundle_source(
                mismatch_job_id,
                artifact_type="landing",
                candidate_artifact_type="poster",
            )
        )
        wrong_artifact = self.client.post(
            f"/api/runs/{mismatch_source_id}/attempts/1/publish",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json={
                "conversation_id": "wrong-artifact-conversation",
                "expected_candidate_sha256": mismatch_candidate.source_sha256,
                "idempotency_key": "wrong-artifact-key",
            },
        )

        self.assertEqual(wrong_owner.status_code, 404, wrong_owner.text)
        self.assertEqual(missing_parent.status_code, 404, missing_parent.text)
        self.assertEqual(wrong_artifact.status_code, 409, wrong_artifact.text)

    def test_direct_publish_descriptor_v2_is_strict(self) -> None:
        run_id = "candidate-publish-strict-v2"
        run_dir = self.runs_dir / run_id
        run_dir.mkdir()
        valid = {
            "version": 2,
            "run_id": run_id,
            "source_run_id": "source-run",
            "source_attempt": 1,
            "source_candidate_id": "candidate-1",
            "source_candidate_sha256": "a" * 64,
            "idempotency_key_digest": "b" * 64,
            "request_digest": "c" * 64,
            "paper_bundle_job_id": "bundle-1",
            "paper_bundle_owner_id": "local",
            "paper_bundle_artifact_type": "landing",
            "publication_generation": 1,
        }
        descriptor_path = run_dir / "candidate_publish_request.json"
        descriptor_path.write_text(json.dumps(valid), encoding="utf-8")
        self.assertEqual(
            web_server._read_direct_candidate_publish_descriptor(run_id),
            valid,
        )
        corruptions = {
            "extra key": lambda value: value.update(extra=True),
            "unsafe bundle ID": lambda value: value.update(
                paper_bundle_job_id="../bundle"
            ),
            "blank owner": lambda value: value.update(paper_bundle_owner_id=""),
            "wrong artifact": lambda value: value.update(
                paper_bundle_artifact_type="audio"
            ),
            "boolean generation": lambda value: value.update(
                publication_generation=True
            ),
            "zero generation": lambda value: value.update(
                publication_generation=0
            ),
        }
        for label, corrupt in corruptions.items():
            with self.subTest(label=label):
                payload = json.loads(json.dumps(valid))
                corrupt(payload)
                descriptor_path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    web_server._read_direct_candidate_publish_descriptor(run_id)

        canvas = {
            **valid,
            "version": 3,
            "source_draft_run_id": "candidate-draft-1",
        }
        descriptor_path.write_text(json.dumps(canvas), encoding="utf-8")
        self.assertEqual(
            web_server._read_direct_candidate_publish_descriptor(run_id),
            canvas,
        )
        canvas["source_draft_run_id"] = "../candidate-draft"
        descriptor_path.write_text(json.dumps(canvas), encoding="utf-8")
        with self.assertRaises(ValueError):
            web_server._read_direct_candidate_publish_descriptor(run_id)

    def test_bundle_direct_publication_commits_overlay_without_source_writes(
        self,
    ) -> None:
        job_id = "bundle-direct-commit"
        bundle_store, source_dir, source_run_id, candidate = (
            self._failed_bundle_source(job_id)
        )
        before = self._tree_bytes(source_dir)
        reserved = self._reserve_direct_publication(
            source_run_id,
            candidate,
            idempotency_key="bundle-direct-commit-key",
        )
        published_run_id = reserved.json()["run_id"]

        self._materialize_and_accept_direct_publication(
            published_run_id,
            source_run_id,
            candidate,
        )

        parent = bundle_store.read_owned(job_id, "local")
        publication = parent.publications["landing"]
        self.assertEqual(publication.source_run_id, source_run_id)
        self.assertEqual(publication.publication_run_id, published_run_id)
        self.assertEqual(publication.artifact_id, f"art_{published_run_id}")
        self.assertEqual(publication.source_attempt, 1)
        self.assertEqual(publication.source_candidate_id, candidate.candidate_id)
        self.assertEqual(
            publication.source_candidate_sha256,
            candidate.source_sha256,
        )
        reconciliation = json.loads(
            (
                self.runs_dir
                / published_run_id
                / "candidate_publish_reconciliation.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(reconciliation["status"], "applied")
        self.assertEqual(reconciliation["paper_bundle_job_id"], job_id)
        self.assertEqual(reconciliation["publication_generation"], 1)
        self.assertEqual(self._tree_bytes(source_dir), before)

    def test_completed_bundle_child_supports_direct_then_canvas_republish(
        self,
    ) -> None:
        job_id = "bundle-completed-republish"
        bundle_store, source_dir, source_run_id, candidate = (
            self._failed_bundle_source(job_id, source_state="completed")
        )
        source_before = self._tree_bytes(source_dir)
        direct_run_id = self._reserve_direct_publication(
            source_run_id,
            candidate,
            idempotency_key="bundle-completed-direct",
        ).json()["run_id"]
        self._materialize_and_accept_direct_publication(
            direct_run_id,
            source_run_id,
            candidate,
        )

        draft_run_id = self._candidate_draft_for_source(source_run_id, candidate)
        draft_before = self._tree_bytes(self.runs_dir / draft_run_id)
        canvas_run_id = self._reserve_canvas_publication(draft_run_id).json()[
            "run_id"
        ]
        self._materialize_and_accept_direct_publication(
            canvas_run_id,
            source_run_id,
            candidate,
            source_draft_run_id=draft_run_id,
        )

        parent = bundle_store.read_owned(job_id, "local")
        self.assertEqual(parent.publication_generations["landing"], 2)
        self.assertEqual(
            parent.publications["landing"].publication_run_id,
            canvas_run_id,
        )
        self.assertEqual(
            RunControlStore(self.runs_dir).read(source_run_id).state,
            "completed",
        )
        self.assertEqual(self._tree_bytes(source_dir), source_before)
        self.assertEqual(
            self._tree_bytes(self.runs_dir / draft_run_id),
            draft_before,
        )

    def test_bundle_direct_publication_preflight_is_read_only_and_strict(
        self,
    ) -> None:
        job_id = "bundle-direct-preflight"
        bundle_store, _source_dir, source_run_id, candidate = (
            self._failed_bundle_source(job_id)
        )
        reserved = self._reserve_direct_publication(
            source_run_id,
            candidate,
            idempotency_key="bundle-direct-preflight-key",
        )
        published_run_id = reserved.json()["run_id"]
        with patch.object(
            candidate_publish_module,
            "validate_candidate_draft",
            return_value=[],
        ):
            candidate_publish_module.run_candidate_publish_job(
                run_id=published_run_id,
                parent_run_id=source_run_id,
                conversation_id="bundle-direct-preflight-conversation",
                settings=self.settings,
                cancellation_token=web_server.CancellationToken.never(
                    published_run_id
                ),
                source_attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
            )
        control = RunControlStore(self.runs_dir).read(published_run_id)
        request = TerminalReconciliation(
            run_id=published_run_id,
            decision="accept",
            phase="preflight",
            terminal_state="completed",
            record=control,
        )

        for label, invalid_record in (
            ("run", replace(control, run_id="other-publication")),
            ("artifact type", replace(control, artifact_type="poster")),
            ("parent", replace(control, parent_job_id="other-source")),
        ):
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    web_server._reconcile_run_terminal_artifact(replace(
                        request,
                        record=invalid_record,
                    ))
        with (
            patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
            patch.object(
                web_server,
                "_demo_run_access",
                return_value={"owner": "different-owner"},
            ),
            self.assertRaises(ValueError),
        ):
            web_server._reconcile_run_terminal_artifact(request)

        web_server._reconcile_run_terminal_artifact(request)

        parent = bundle_store.read_owned(job_id, "local")
        self.assertNotIn("landing", parent.publications)
        self.assertFalse(
            (
                self.runs_dir
                / published_run_id
                / "candidate_publish_reconciliation.json"
            ).exists()
        )
        lineage = json.loads(
            (
                self.runs_dir
                / published_run_id
                / "candidate_draft_lineage.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(lineage["status"], "validated")

        derived_path = self.runs_dir / published_run_id / "derived_job.json"
        derived = json.loads(derived_path.read_text(encoding="utf-8"))
        derived["artifact_type"] = "poster"
        derived_path.write_text(json.dumps(derived), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "lineage is inconsistent"):
            web_server._reconcile_run_terminal_artifact(request)

    def test_bundle_direct_publication_commit_replay_is_idempotent(self) -> None:
        job_id = "bundle-direct-idempotent"
        bundle_store, _source_dir, source_run_id, candidate = (
            self._failed_bundle_source(job_id)
        )
        reserved = self._reserve_direct_publication(
            source_run_id,
            candidate,
            idempotency_key="bundle-direct-idempotent-key",
        )
        published_run_id = reserved.json()["run_id"]
        self._materialize_and_accept_direct_publication(
            published_run_id,
            source_run_id,
            candidate,
        )
        bundle_store.request_cancel(job_id, "local")
        before = bundle_store.read_owned(job_id, "local")
        control = RunControlStore(self.runs_dir).read(published_run_id)

        web_server._reconcile_run_terminal_artifact(TerminalReconciliation(
            run_id=published_run_id,
            decision="accept",
            phase="commit",
            terminal_state="completed",
            record=control,
        ))

        after = bundle_store.read_owned(job_id, "local")
        self.assertEqual(after.revision, before.revision)
        self.assertEqual(after.publications, before.publications)
        reconciliation = json.loads(
            (
                self.runs_dir
                / published_run_id
                / "candidate_publish_reconciliation.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(reconciliation["status"], "idempotent")

    def test_bundle_direct_publication_generation_prevents_stale_overwrite(
        self,
    ) -> None:
        job_id = "bundle-direct-superseded"
        bundle_store, _source_dir, source_run_id, candidate = (
            self._failed_bundle_source(job_id)
        )
        first = self._reserve_direct_publication(
            source_run_id,
            candidate,
            idempotency_key="bundle-direct-generation-1",
        )
        second = self._reserve_direct_publication(
            source_run_id,
            candidate,
            idempotency_key="bundle-direct-generation-2",
        )
        first_run_id = first.json()["run_id"]
        second_run_id = second.json()["run_id"]

        self._materialize_and_accept_direct_publication(
            second_run_id,
            source_run_id,
            candidate,
        )
        self._materialize_and_accept_direct_publication(
            first_run_id,
            source_run_id,
            candidate,
        )

        parent = bundle_store.read_owned(job_id, "local")
        self.assertEqual(
            parent.publications["landing"].publication_run_id,
            second_run_id,
        )
        first_reconciliation = json.loads(
            (
                self.runs_dir
                / first_run_id
                / "candidate_publish_reconciliation.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(first_reconciliation["status"], "superseded")

    def test_bundle_cancel_between_reserve_and_commit_keeps_derived_artifact(
        self,
    ) -> None:
        job_id = "bundle-direct-cancel-race"
        bundle_store, _source_dir, source_run_id, candidate = (
            self._failed_bundle_source(job_id)
        )
        reserved = self._reserve_direct_publication(
            source_run_id,
            candidate,
            idempotency_key="bundle-direct-cancel-race-key",
        )
        published_run_id = reserved.json()["run_id"]
        bundle_store.request_cancel(job_id, "local")
        rejected = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/publish",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json={
                "conversation_id": "cancelled-new-key",
                "expected_candidate_sha256": candidate.source_sha256,
                "idempotency_key": "bundle-direct-cancelled-new-key",
            },
        )

        self._materialize_and_accept_direct_publication(
            published_run_id,
            source_run_id,
            candidate,
        )

        completed = RunControlStore(self.runs_dir).read(published_run_id)
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(completed.state, "completed")
        self.assertEqual(completed.terminal_reconciliation_status, "succeeded")
        parent = bundle_store.read_owned(job_id, "local")
        self.assertNotIn("landing", parent.publications)
        reconciliation = json.loads(
            (
                self.runs_dir
                / published_run_id
                / "candidate_publish_reconciliation.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            reconciliation["status"],
            "bundle_cancelled_not_attached",
        )
        self.assertEqual(reconciliation["paper_bundle_job_id"], job_id)

    def test_bundle_direct_publication_cold_reconciliation_uses_v2_binding(
        self,
    ) -> None:
        job_id = "bundle-direct-cold"
        _bundle_store, _source_dir, source_run_id, candidate = (
            self._failed_bundle_source(job_id)
        )
        reserved = self._reserve_direct_publication(
            source_run_id,
            candidate,
            idempotency_key="bundle-direct-cold-key",
        )
        published_run_id = reserved.json()["run_id"]

        self._materialize_and_accept_direct_publication(
            published_run_id,
            source_run_id,
            candidate,
            clear_runtime_state=True,
        )

        recovered_store = web_server._paper_bundle_store()
        parent = recovered_store.read_owned(job_id, "local")
        self.assertEqual(
            parent.publications["landing"].publication_run_id,
            published_run_id,
        )
        self.assertNotIn(published_run_id, web_server._RUNS)

    def test_failed_source_canvas_draft_publish_reserves_without_source_mutation(
        self,
    ) -> None:
        source_dir, draft_run_id, _candidate = self._failed_source_candidate_draft(
            "failed-canvas-source"
        )
        before = self._tree_bytes(source_dir)

        response = self.client.post(
            f"/api/artifacts/art_{draft_run_id}/publish-candidate-draft",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json={
                "conversation_id": "failed-canvas-conversation",
            },
        )

        self.assertEqual(
            response.status_code,
            200,
            "Canvas publication must read a failed original source as an immutable snapshot",
        )
        child = RunControlStore(self.runs_dir).read(response.json()["run_id"])
        self.assertEqual(child.state, "queued")
        self.assertEqual(child.parent_job_id, draft_run_id)
        self.assertEqual(self._tree_bytes(source_dir), before)
        descriptor = json.loads(
            (
                self.runs_dir
                / response.json()["run_id"]
                / "candidate_publish_request.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(descriptor["version"], 1)
        self.assertNotIn("paper_bundle_job_id", descriptor)

    def test_bundle_canvas_publication_v3_commits_blocked_repair_after_cold_restart(
        self,
    ) -> None:
        job_id = "bundle-canvas-cold-commit"
        bundle_store, source_dir, source_run_id, candidate = (
            self._failed_bundle_source(
                job_id,
                candidate_safety_state="blocked",
            )
        )
        draft_run_id = self._candidate_draft_for_source(source_run_id, candidate)
        draft_dir = self.runs_dir / draft_run_id
        source_before = self._tree_bytes(source_dir)
        draft_before = self._tree_bytes(draft_dir)

        reserved = self._reserve_canvas_publication(draft_run_id)
        published_run_id = reserved.json()["run_id"]
        descriptor = json.loads(
            (
                self.runs_dir
                / published_run_id
                / "candidate_publish_request.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(descriptor["version"], 3)
        self.assertEqual(descriptor["source_run_id"], source_run_id)
        self.assertEqual(descriptor["source_draft_run_id"], draft_run_id)
        self.assertEqual(descriptor["paper_bundle_job_id"], job_id)
        self.assertEqual(descriptor["publication_generation"], 1)

        self._materialize_and_accept_direct_publication(
            published_run_id,
            source_run_id,
            candidate,
            source_draft_run_id=draft_run_id,
            clear_runtime_state=True,
        )

        parent = bundle_store.read_owned(job_id, "local")
        publication = parent.publications["landing"]
        self.assertEqual(publication.source_run_id, source_run_id)
        self.assertEqual(publication.publication_run_id, published_run_id)
        self.assertEqual(publication.source_candidate_id, candidate.candidate_id)
        self.assertEqual(self._tree_bytes(source_dir), source_before)
        self.assertEqual(self._tree_bytes(draft_dir), draft_before)
        reconciliation = json.loads(
            (
                self.runs_dir
                / published_run_id
                / "candidate_publish_reconciliation.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(reconciliation["status"], "applied")
        self.assertNotIn(published_run_id, web_server._RUNS)

    def test_bundle_canvas_nonreserve_persists_v3_before_worker_start(
        self,
    ) -> None:
        job_id = "bundle-canvas-start-order"
        _store, _source_dir, source_run_id, candidate = (
            self._failed_bundle_source(job_id)
        )
        draft_run_id = self._candidate_draft_for_source(source_run_id, candidate)
        observed: dict[str, object] = {}

        async def assert_descriptor_before_start(**kwargs):
            published_run_id = str(kwargs["run_id"])
            request_descriptor = json.loads(
                (
                    self.runs_dir
                    / published_run_id
                    / "candidate_publish_request.json"
                ).read_text(encoding="utf-8")
            )
            observed["version"] = request_descriptor["version"]
            observed["draft"] = request_descriptor["source_draft_run_id"]

        with patch.object(
            web_server,
            "_start_reserved_derived_job",
            side_effect=assert_descriptor_before_start,
        ) as start:
            response = self.client.post(
                f"/api/artifacts/art_{draft_run_id}/publish-candidate-draft",
                json={"conversation_id": "bundle-canvas-start-order"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(observed, {"version": 3, "draft": draft_run_id})
        start.assert_awaited_once()

    def test_bundle_canvas_publication_cancel_race_keeps_validated_artifact(
        self,
    ) -> None:
        job_id = "bundle-canvas-cancel-race"
        bundle_store, _source_dir, source_run_id, candidate = (
            self._failed_bundle_source(job_id)
        )
        draft_run_id = self._candidate_draft_for_source(source_run_id, candidate)
        reserved = self._reserve_canvas_publication(draft_run_id)
        published_run_id = reserved.json()["run_id"]
        bundle_store.request_cancel(job_id, "local")

        self._materialize_and_accept_direct_publication(
            published_run_id,
            source_run_id,
            candidate,
            source_draft_run_id=draft_run_id,
        )

        completed = RunControlStore(self.runs_dir).read(published_run_id)
        self.assertEqual(completed.state, "completed")
        self.assertTrue(completed.publishable)
        parent = bundle_store.read_owned(job_id, "local")
        self.assertNotIn("landing", parent.publications)
        reconciliation = json.loads(
            (
                self.runs_dir
                / published_run_id
                / "candidate_publish_reconciliation.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            reconciliation["status"],
            "bundle_cancelled_not_attached",
        )

    def test_bundle_canvas_publication_preflight_requires_all_access_owners(
        self,
    ) -> None:
        job_id = "bundle-canvas-access"
        _bundle_store, _source_dir, source_run_id, candidate = (
            self._failed_bundle_source(job_id)
        )
        draft_run_id = self._candidate_draft_for_source(source_run_id, candidate)
        published_run_id = self._reserve_canvas_publication(draft_run_id).json()[
            "run_id"
        ]
        with patch.object(
            candidate_publish_module,
            "validate_candidate_draft",
            return_value=[],
        ):
            candidate_publish_module.run_candidate_publish_job(
                run_id=published_run_id,
                parent_run_id=draft_run_id,
                conversation_id="bundle-canvas-access-conversation",
                settings=self.settings,
                cancellation_token=web_server.CancellationToken.never(
                    published_run_id
                ),
            )
        control = RunControlStore(self.runs_dir).read(published_run_id)
        request = TerminalReconciliation(
            run_id=published_run_id,
            decision="accept",
            phase="preflight",
            terminal_state="completed",
            record=control,
        )

        for mismatched_run_id in (
            source_run_id,
            draft_run_id,
            published_run_id,
        ):
            with self.subTest(mismatched_run_id=mismatched_run_id):
                def access(candidate_run_id: str):
                    return {
                        "owner": (
                            "different-owner"
                            if candidate_run_id == mismatched_run_id
                            else "local"
                        )
                    }

                with (
                    patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
                    patch.object(
                        web_server,
                        "_demo_run_access",
                        side_effect=access,
                    ),
                    self.assertRaises(ValueError),
                ):
                    web_server._reconcile_run_terminal_artifact(request)

        with (
            patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
            patch.object(
                web_server,
                "_demo_run_access",
                return_value={"owner": "local"},
            ),
        ):
            web_server._reconcile_run_terminal_artifact(request)

    def test_bundle_canvas_publication_generation_prevents_stale_overwrite(
        self,
    ) -> None:
        job_id = "bundle-canvas-superseded"
        bundle_store, _source_dir, source_run_id, candidate = (
            self._failed_bundle_source(job_id)
        )
        draft_run_id = self._candidate_draft_for_source(source_run_id, candidate)
        first = self._reserve_canvas_publication(draft_run_id).json()["run_id"]
        second = self._reserve_canvas_publication(draft_run_id).json()["run_id"]

        self._materialize_and_accept_direct_publication(
            second,
            source_run_id,
            candidate,
            source_draft_run_id=draft_run_id,
        )
        self._materialize_and_accept_direct_publication(
            first,
            source_run_id,
            candidate,
            source_draft_run_id=draft_run_id,
        )

        parent = bundle_store.read_owned(job_id, "local")
        self.assertEqual(
            parent.publications["landing"].publication_run_id,
            second,
        )
        first_reconciliation = json.loads(
            (
                self.runs_dir
                / first
                / "candidate_publish_reconciliation.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(first_reconciliation["status"], "superseded")

    def test_direct_attempt_publish_idempotency_reuses_and_conflicts(self) -> None:
        source_run_id = "failed-idempotent-source"
        _source_dir, candidates = self._failed_source_with_candidates(
            source_run_id,
            attempts=2,
        )
        first_candidate, second_candidate = candidates
        headers = {"X-Autodesign-Reserve-Only": "true"}
        first_payload = {
            "conversation_id": "idempotent-conversation",
            "expected_candidate_sha256": first_candidate.source_sha256,
            "idempotency_key": "stable-direct-publication",
        }

        first = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/publish",
            headers=headers,
            json=first_payload,
        )
        replay = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/publish",
            headers=headers,
            json=first_payload,
        )
        conflict = self.client.post(
            f"/api/runs/{source_run_id}/attempts/2/publish",
            headers=headers,
            json={
                **first_payload,
                "expected_candidate_sha256": second_candidate.source_sha256,
            },
        )
        blank_key = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/publish",
            headers=headers,
            json={**first_payload, "idempotency_key": "   "},
        )

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["run_id"], first.json()["run_id"])
        self.assertEqual(replay.json()["start_token"], first.json()["start_token"])
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(blank_key.status_code, 422, blank_key.text)

    def test_direct_attempt_publish_cold_reserve_replay_reissues_start_token(
        self,
    ) -> None:
        source_run_id = "failed-cold-reserve-replay-source"
        _source_dir, candidates = self._failed_source_with_candidates(source_run_id)
        candidate = candidates[0]
        payload = {
            "conversation_id": "cold-reserve-replay-conversation",
            "expected_candidate_sha256": candidate.source_sha256,
            "idempotency_key": "cold-reserve-replay-key",
        }
        first = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/publish",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json=payload,
        )
        self.assertEqual(first.status_code, 200, first.text)
        published_run_id = first.json()["run_id"]
        first_token = first.json()["start_token"]
        web_server._RUNS.pop(published_run_id, None)
        self._replace_with_cold_web_runtime()
        self.client.portal.call(web_server._recover_web_run_controls)
        self.assertEqual(
            RunControlStore(self.runs_dir).read(published_run_id).state,
            "queued",
        )
        self.assertIn(published_run_id, web_server._RUNS)

        replay = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/publish",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json=payload,
        )

        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["run_id"], published_run_id)
        recovered_token = replay.json()["start_token"]
        self.assertTrue(recovered_token)
        self.assertNotEqual(recovered_token, first_token)
        started = self.client.post(
            f"/api/runs/{published_run_id}/start",
            headers={"X-Autodesign-Upload-Token": recovered_token},
        )
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(
            RunControlStore(self.runs_dir).read(published_run_id).state,
            "running",
        )

    def test_direct_attempt_publish_cold_normal_replay_starts_queued_run(
        self,
    ) -> None:
        source_run_id = "failed-cold-autostart-replay-source"
        _source_dir, candidates = self._failed_source_with_candidates(source_run_id)
        candidate = candidates[0]
        payload = {
            "conversation_id": "cold-autostart-replay-conversation",
            "expected_candidate_sha256": candidate.source_sha256,
            "idempotency_key": "cold-autostart-replay-key",
        }
        first = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/publish",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json=payload,
        )
        self.assertEqual(first.status_code, 200, first.text)
        published_run_id = first.json()["run_id"]
        web_server._RUNS.pop(published_run_id, None)
        self._replace_with_cold_web_runtime()
        self.client.portal.call(web_server._recover_web_run_controls)
        self.assertEqual(
            RunControlStore(self.runs_dir).read(published_run_id).state,
            "queued",
        )

        replay = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/publish",
            json=payload,
        )

        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["run_id"], published_run_id)
        self.assertIsNone(replay.json()["start_token"])
        self.assertEqual(
            RunControlStore(self.runs_dir).read(published_run_id).state,
            "running",
        )

    def test_direct_attempt_publish_replay_after_start_returns_ack_without_token(
        self,
    ) -> None:
        source_run_id = "failed-started-replay-source"
        _source_dir, candidates = self._failed_source_with_candidates(source_run_id)
        candidate = candidates[0]
        headers = {"X-Autodesign-Reserve-Only": "true"}
        payload = {
            "conversation_id": "started-replay-conversation",
            "expected_candidate_sha256": candidate.source_sha256,
            "idempotency_key": "started-direct-publication",
        }
        reserved = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/publish",
            headers=headers,
            json=payload,
        )
        self.assertEqual(reserved.status_code, 200, reserved.text)
        run_id = reserved.json()["run_id"]
        token = reserved.json()["start_token"]
        started = self.client.post(
            f"/api/runs/{run_id}/start",
            headers={"X-Autodesign-Upload-Token": token},
        )
        self.assertEqual(started.status_code, 200, started.text)
        control_before = RunControlStore(self.runs_dir).read(run_id)
        self.assertEqual(control_before.state, "running")

        with patch.object(
            web_server,
            "_start_reserved_derived_job",
            new_callable=AsyncMock,
        ) as second_start:
            replay = self.client.post(
                f"/api/runs/{source_run_id}/attempts/1/publish",
                headers=headers,
                json=payload,
            )

        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["run_id"], run_id)
        self.assertIsNone(replay.json()["start_token"])
        second_start.assert_not_awaited()
        control_after = RunControlStore(self.runs_dir).read(run_id)
        self.assertEqual(control_after.worker_pid, control_before.worker_pid)

    def test_direct_attempt_publish_started_replay_survives_source_cancellation(
        self,
    ) -> None:
        source_run_id = "cancelling-started-replay-source"
        source_dir = self.runs_dir / source_run_id
        store = RunControlStore(self.runs_dir)
        source_control = store.reserve(source_run_id, "landing")
        source_control = store.transition(source_run_id, source_control, "queued")
        source_control = store.transition(source_run_id, source_control, "running")
        attempt_dir = source_dir / "landing_author" / "attempt_01"
        attempt_dir.mkdir(parents=True)
        (attempt_dir / "index.html").write_text(
            "<!doctype html><main>Running source attempt</main>",
            encoding="utf-8",
        )
        (attempt_dir / "preview.png").write_bytes(b"running-source-preview")
        (attempt_dir / "validation.json").write_text(
            '{"accepted":true}',
            encoding="utf-8",
        )
        (attempt_dir / "designer_author_done.json").write_text(
            '{"status":"done"}',
            encoding="utf-8",
        )
        candidate = capture_attempt_candidate(
            run_dir=source_dir,
            attempt_dir=attempt_dir,
            artifact_type="landing",
            attempt=1,
            max_attempts=1,
            source_path="index.html",
            dependency_paths=["designer_author_done.json"],
            preview_paths=["preview.png"],
            validation_summary_path="validation.json",
            safety_state="ready",
            hard_blockers=[],
            warnings=[],
        )
        headers = {"X-Autodesign-Reserve-Only": "true"}
        payload = {
            "conversation_id": "cancelling-started-replay-conversation",
            "expected_candidate_sha256": candidate.source_sha256,
            "idempotency_key": "cancelling-started-direct-publication",
        }
        reserved = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/publish",
            headers=headers,
            json=payload,
        )
        self.assertEqual(reserved.status_code, 200, reserved.text)
        run_id = reserved.json()["run_id"]
        started = self.client.post(
            f"/api/runs/{run_id}/start",
            headers={
                "X-Autodesign-Upload-Token": reserved.json()["start_token"],
            },
        )
        self.assertEqual(started.status_code, 200, started.text)
        store.request_cancel(source_run_id)

        with patch.object(
            web_server,
            "_start_reserved_derived_job",
            new_callable=AsyncMock,
        ) as second_start:
            replay = self.client.post(
                f"/api/runs/{source_run_id}/attempts/1/publish",
                headers=headers,
                json=payload,
            )
            conflict = self.client.post(
                f"/api/runs/{source_run_id}/attempts/1/publish",
                headers=headers,
                json={**payload, "expected_candidate_sha256": "0" * 64},
            )

        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["run_id"], run_id)
        self.assertIsNone(replay.json()["start_token"])
        self.assertEqual(conflict.status_code, 409, conflict.text)
        second_start.assert_not_awaited()

    def test_direct_attempt_publish_started_replay_survives_source_unavailable(
        self,
    ) -> None:
        for mutation in ("changed", "missing"):
            with self.subTest(mutation=mutation):
                source_run_id = f"{mutation}-started-replay-source"
                source_dir, candidates = self._failed_source_with_candidates(
                    source_run_id
                )
                candidate = candidates[0]
                headers = {"X-Autodesign-Reserve-Only": "true"}
                payload = {
                    "conversation_id": f"{mutation}-started-replay-conversation",
                    "expected_candidate_sha256": candidate.source_sha256,
                    "idempotency_key": f"{mutation}-started-direct-publication",
                }
                reserved = self.client.post(
                    f"/api/runs/{source_run_id}/attempts/1/publish",
                    headers=headers,
                    json=payload,
                )
                self.assertEqual(reserved.status_code, 200, reserved.text)
                run_id = reserved.json()["run_id"]
                started = self.client.post(
                    f"/api/runs/{run_id}/start",
                    headers={
                        "X-Autodesign-Upload-Token": reserved.json()["start_token"],
                    },
                )
                self.assertEqual(started.status_code, 200, started.text)
                source_path = source_dir / candidate.source_relative_path
                if mutation == "changed":
                    source_path.write_text(
                        "changed after publication started",
                        encoding="utf-8",
                    )
                else:
                    source_path.unlink()

                with patch.object(
                    web_server,
                    "_start_reserved_derived_job",
                    new_callable=AsyncMock,
                ) as second_start:
                    replay = self.client.post(
                        f"/api/runs/{source_run_id}/attempts/1/publish",
                        headers=headers,
                        json=payload,
                    )

                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertEqual(replay.json()["run_id"], run_id)
                self.assertIsNone(replay.json()["start_token"])
                second_start.assert_not_awaited()

    def test_direct_attempt_publish_terminal_cold_replay_returns_ack_without_token(
        self,
    ) -> None:
        source_run_id = "failed-terminal-replay-source"
        _source_dir, candidates = self._failed_source_with_candidates(source_run_id)
        candidate = candidates[0]
        headers = {"X-Autodesign-Reserve-Only": "true"}
        payload = {
            "conversation_id": "terminal-replay-conversation",
            "expected_candidate_sha256": candidate.source_sha256,
            "idempotency_key": "terminal-direct-publication",
        }
        reserved = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/publish",
            headers=headers,
            json=payload,
        )
        self.assertEqual(reserved.status_code, 200, reserved.text)
        run_id = reserved.json()["run_id"]
        store = RunControlStore(self.runs_dir)
        control = store.read(run_id)
        control = store.transition(run_id, control, "running")
        control = store.transition(run_id, control, "completing")

        with patch.object(
            web_server,
            "_start_reserved_derived_job",
            new_callable=AsyncMock,
        ) as completing_start:
            completing_replay = self.client.post(
                f"/api/runs/{source_run_id}/attempts/1/publish",
                headers=headers,
                json=payload,
            )

        self.assertEqual(completing_replay.status_code, 200, completing_replay.text)
        self.assertEqual(completing_replay.json()["run_id"], run_id)
        self.assertIsNone(completing_replay.json()["start_token"])
        completing_start.assert_not_awaited()
        control = store.transition(run_id, control, "completed", publishable=True)
        web_server._RUNS.pop(run_id)

        with patch.object(
            web_server,
            "_start_reserved_derived_job",
            new_callable=AsyncMock,
        ) as second_start:
            replay = self.client.post(
                f"/api/runs/{source_run_id}/attempts/1/publish",
                headers=headers,
                json=payload,
            )

        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["run_id"], run_id)
        self.assertIsNone(replay.json()["start_token"])
        second_start.assert_not_awaited()
        self.assertNotIn(run_id, web_server._RUNS)
        self.assertEqual(store.read(run_id), control)

    def test_direct_attempt_publish_identity_survives_cold_history_reconstruction(
        self,
    ) -> None:
        source_run_id = "failed-cold-source"
        _source_dir, candidates = self._failed_source_with_candidates(source_run_id)
        candidate = candidates[0]
        response = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/publish",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json={
                "conversation_id": "cold-direct-conversation",
                "expected_candidate_sha256": candidate.source_sha256,
                "idempotency_key": "cold-direct-publication",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        live_state = web_server._RUNS.pop(run_id)
        try:
            recovered = web_server._conversation_from_disk_run(run_id)
        finally:
            web_server._RUNS[run_id] = live_state

        self.assertIsNotNone(recovered)
        assert recovered is not None
        message = recovered["messages"][0]
        self.assertEqual(message["task_type"], "candidate_publish")
        self.assertEqual(
            message["task_payload"]["source_run_id"],
            source_run_id,
        )
        self.assertEqual(
            message["task_payload"]["source_candidate_id"],
            candidate.candidate_id,
        )

    def test_direct_attempt_publish_rejects_cancelling_and_cancelled_source(
        self,
    ) -> None:
        source_run_id = "cancelled-direct-source"
        source_dir = self.runs_dir / source_run_id
        store = RunControlStore(self.runs_dir)
        record = store.reserve(source_run_id, "landing")
        record = store.transition(source_run_id, record, "queued")
        record = store.transition(source_run_id, record, "running")
        attempt_dir = source_dir / "landing_author" / "attempt_01"
        attempt_dir.mkdir(parents=True)
        (attempt_dir / "index.html").write_text(
            "<!doctype html><main>Attempt</main>",
            encoding="utf-8",
        )
        (attempt_dir / "validation.json").write_text(
            '{"accepted":true}',
            encoding="utf-8",
        )
        candidate = capture_attempt_candidate(
            run_dir=source_dir,
            attempt_dir=attempt_dir,
            artifact_type="landing",
            attempt=1,
            max_attempts=1,
            source_path="index.html",
            dependency_paths=[],
            preview_paths=[],
            validation_summary_path="validation.json",
            safety_state="ready",
            hard_blockers=[],
            warnings=[],
        )
        store.request_cancel(source_run_id)
        payload = {
            "conversation_id": "cancelled-direct-conversation",
            "expected_candidate_sha256": candidate.source_sha256,
            "idempotency_key": "cancelled-direct-publication",
        }

        cancelling = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/publish",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json=payload,
        )
        self._finalize_test_cancellation(store, source_run_id)
        cancelled = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/publish",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json={**payload, "idempotency_key": "cancelled-direct-publication-2"},
        )

        self.assertEqual(cancelling.status_code, 410, cancelling.text)
        self.assertEqual(cancelled.status_code, 410, cancelled.text)

    def test_failed_source_direct_publish_completes_without_source_writes(self) -> None:
        source_run_id = "failed-completion-source"
        source_dir, candidates = self._failed_source_with_candidates(source_run_id)
        candidate = candidates[0]
        before = self._tree_bytes(source_dir)
        response = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/publish",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json={
                "conversation_id": "failed-completion-conversation",
                "expected_candidate_sha256": candidate.source_sha256,
                "idempotency_key": "failed-completion-publication",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        parameters = inspect.signature(
            candidate_publish_module.run_candidate_publish_job
        ).parameters
        self.assertIn("source_attempt", parameters)
        self.assertIn("expected_candidate_sha256", parameters)
        with patch.object(
            candidate_publish_module,
            "validate_candidate_draft",
            return_value=[],
        ):
            result = candidate_publish_module.run_candidate_publish_job(
                run_id=run_id,
                parent_run_id=source_run_id,
                conversation_id="failed-completion-conversation",
                settings=self.settings,
                cancellation_token=web_server.CancellationToken.never(run_id),
                source_attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
            )
        self.assertEqual(result["lineage"]["status"], "validated")
        store = RunControlStore(self.runs_dir)
        record = store.read(run_id)
        record = store.transition(run_id, record, "running")
        store.transition(run_id, record, "completing")
        supervisor = web_server._web_run_runtime().supervisor
        supervisor._terminal_reconciler = web_server._reconcile_run_terminal_artifact

        async def accept() -> None:
            await supervisor.accept_completion(
                run_id,
                terminal_state="completed",
                publishable=True,
                result_digest="d" * 64,
            )

        self.client.portal.call(accept)

        completed = store.read(run_id)
        self.assertEqual(completed.state, "completed")
        self.assertTrue(completed.publishable)
        lineage = json.loads(
            (self.runs_dir / run_id / "candidate_draft_lineage.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(lineage["status"], "published")
        reconciliation = json.loads(
            (self.runs_dir / run_id / "candidate_publish_reconciliation.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(reconciliation["source_run_id"], source_run_id)
        self.assertEqual(self._tree_bytes(source_dir), before)

    def test_candidate_publish_monitor_terminalizes_failed_source_outcomes(self) -> None:
        source_run_id = "failed-monitor-source"
        source_dir, candidates = self._failed_source_with_candidates(source_run_id)
        candidate = candidates[0]
        before = self._tree_bytes(source_dir)
        response = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/publish",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json={
                "conversation_id": "failed-monitor-conversation",
                "expected_candidate_sha256": candidate.source_sha256,
                "idempotency_key": "failed-monitor-publication",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        request_before = (
            self.runs_dir / run_id / "candidate_publish_request.json"
        ).read_bytes()
        descriptor = web_server._read_derived_job_descriptor(run_id)
        self.assertIsNotNone(descriptor)
        assert descriptor is not None
        store = RunControlStore(self.runs_dir)
        record = store.read(run_id)
        record = store.transition(run_id, record, "running")
        store.transition(run_id, record, "completing")
        state = web_server._RUNS[run_id]
        diagnostic = (
            "narration_timing_unfit scene=scene_11 measured_s=30.677 "
            "available_s=29.750 max_speed=1.35 final_speed=1.35"
        )
        outcome = WorkerOutcome(
            run_id=run_id,
            job_kind="candidate_publish",
            returncode=1,
            ok=False,
            result=None,
            error=diagnostic,
            relayed_events=0,
            failure_phase="narration_timing",
        )

        async def monitor() -> None:
            await web_server._monitor_supervised_derived_job(
                run_id=run_id,
                state=state,
                job_kind="candidate_publish",
                parent_run_id=source_run_id,
                descriptor=descriptor,
                recovered_outcome=outcome,
            )

        self.client.portal.call(monitor)

        terminal = store.read(run_id)
        message = state.result_message
        failure = message.failure if message is not None else None
        self.assertEqual(terminal.state, "failed")
        self.assertFalse(terminal.publishable)
        self.assertEqual(state.error, diagnostic)
        self.assertEqual(message.text if message is not None else None, diagnostic)
        self.assertEqual(failure.phase if failure is not None else None, "narration_timing")
        self.assertEqual(
            failure.agent_last_note if failure is not None else None,
            diagnostic,
        )
        self.assertNotIn("Kokoro synthesis failed", message.text)
        self.assertEqual(
            (self.runs_dir / run_id / "candidate_publish_request.json").read_bytes(),
            request_before,
        )
        self.assertEqual(self._tree_bytes(source_dir), before)

    def test_candidate_publish_monitor_completes_failed_source_from_durable_identity(
        self,
    ) -> None:
        source_run_id = "failed-monitor-success-source"
        source_dir, candidates = self._failed_source_with_candidates(source_run_id)
        candidate = candidates[0]
        before = self._tree_bytes(source_dir)
        response = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/publish",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json={
                "conversation_id": "failed-monitor-success-conversation",
                "expected_candidate_sha256": candidate.source_sha256,
                "idempotency_key": "failed-monitor-success-publication",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        with patch.object(
            candidate_publish_module,
            "validate_candidate_draft",
            return_value=[],
        ):
            result = candidate_publish_module.run_candidate_publish_job(
                run_id=run_id,
                parent_run_id=source_run_id,
                conversation_id="failed-monitor-success-conversation",
                settings=self.settings,
                cancellation_token=web_server.CancellationToken.never(run_id),
                source_attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
            )
        descriptor = web_server._read_derived_job_descriptor(run_id)
        self.assertIsNotNone(descriptor)
        assert descriptor is not None
        store = RunControlStore(self.runs_dir)
        record = store.read(run_id)
        record = store.transition(run_id, record, "running")
        store.transition(run_id, record, "completing")
        state = web_server._RUNS[run_id]
        supervisor = web_server._web_run_runtime().supervisor
        supervisor._terminal_reconciler = web_server._reconcile_run_terminal_artifact
        outcome = WorkerOutcome(
            run_id=run_id,
            job_kind="candidate_publish",
            returncode=0,
            ok=True,
            result=result,
            error=None,
            relayed_events=0,
        )

        async def monitor() -> None:
            await web_server._monitor_supervised_derived_job(
                run_id=run_id,
                state=state,
                job_kind="candidate_publish",
                parent_run_id=source_run_id,
                descriptor=descriptor,
                recovered_outcome=outcome,
            )

        self.client.portal.call(monitor)

        terminal = store.read(run_id)
        lineage = json.loads(
            (self.runs_dir / run_id / "candidate_draft_lineage.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(terminal.state, "completed")
        self.assertTrue(terminal.publishable)
        self.assertIsNotNone(state.result_artifact)
        self.assertEqual(state.result_message.status, "done")
        self.assertEqual(lineage["status"], "published")
        self.assertEqual(self._tree_bytes(source_dir), before)

    def test_candidate_publish_precondition_error_terminalizes_failed(
        self,
    ) -> None:
        source_run_id = "failed-monitor-invalid-request-source"
        source_dir, candidates = self._failed_source_with_candidates(source_run_id)
        candidate = candidates[0]
        before = self._tree_bytes(source_dir)
        response = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/publish",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json={
                "conversation_id": "failed-monitor-invalid-request-conversation",
                "expected_candidate_sha256": candidate.source_sha256,
                "idempotency_key": "failed-monitor-invalid-request-publication",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        with patch.object(
            candidate_publish_module,
            "validate_candidate_draft",
            return_value=[],
        ):
            result = candidate_publish_module.run_candidate_publish_job(
                run_id=run_id,
                parent_run_id=source_run_id,
                conversation_id="failed-monitor-invalid-request-conversation",
                settings=self.settings,
                cancellation_token=web_server.CancellationToken.never(run_id),
                source_attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
            )
        request_path = self.runs_dir / run_id / "candidate_publish_request.json"
        request_path.write_text("{", encoding="utf-8")
        descriptor = web_server._read_derived_job_descriptor(run_id)
        self.assertIsNotNone(descriptor)
        assert descriptor is not None
        store = RunControlStore(self.runs_dir)
        record = store.read(run_id)
        record = store.transition(run_id, record, "running")
        store.transition(run_id, record, "completing")
        state = web_server._RUNS[run_id]
        outcome = WorkerOutcome(
            run_id=run_id,
            job_kind="candidate_publish",
            returncode=0,
            ok=True,
            result=result,
            error=None,
            relayed_events=0,
        )

        async def monitor() -> None:
            await web_server._monitor_supervised_derived_job(
                run_id=run_id,
                state=state,
                job_kind="candidate_publish",
                parent_run_id=source_run_id,
                descriptor=descriptor,
                recovered_outcome=outcome,
            )

        self.client.portal.call(monitor)

        terminal = store.read(run_id)
        self.assertEqual(terminal.state, "failed")
        self.assertFalse(terminal.publishable)
        self.assertIsNone(state.result_artifact)
        self.assertIsNotNone(state.result_message)
        assert state.result_message is not None
        self.assertEqual(state.result_message.status, "error")
        self.assertIn("request is unreadable", state.result_message.text)
        self.assertEqual(self._tree_bytes(source_dir), before)

    def test_candidate_publish_failure_cold_restart_reaches_status_and_history(
        self,
    ) -> None:
        source_run_id = "failed-monitor-cold-source"
        _source_dir, candidates = self._failed_source_with_candidates(source_run_id)
        candidate = candidates[0]
        conversation_id = "failed-monitor-cold-conversation"
        response = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/publish",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json={
                "conversation_id": conversation_id,
                "expected_candidate_sha256": candidate.source_sha256,
                "idempotency_key": "failed-monitor-cold-publication",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        store = RunControlStore(self.runs_dir)
        record = store.read(run_id)
        record = store.transition(run_id, record, "running")
        store.transition(run_id, record, "completing")
        diagnostic = (
            "narration_timing_unfit scene=scene_11 measured_s=30.677 "
            "available_s=29.750 max_speed=1.35 final_speed=1.35"
        )
        (self.runs_dir / run_id / "worker_result.json").write_text(
            json.dumps({
                "job_kind": "candidate_publish",
                "run_id": run_id,
                "ok": False,
                "error": {
                    "type": "narration_timing_unfit",
                    "message": diagnostic,
                },
            }),
            encoding="utf-8",
        )
        web_server._RUNS.pop(run_id, None)
        self._replace_with_cold_web_runtime()

        async def recover_and_join():
            await web_server._recover_web_run_controls()
            state = web_server._RUNS.get(run_id)
            if state is not None and state.task is not None:
                await state.task
            return state

        state = self.client.portal.call(recover_and_join)
        cold_descriptor = web_server._read_derived_job_descriptor(run_id)
        self.assertIsNotNone(cold_descriptor)
        assert cold_descriptor is not None
        cold_worker_result = web_server._decoded_worker_result_file(
            run_id,
            str(cold_descriptor["job_kind"]),
        )
        self.assertIsNotNone(cold_worker_result)
        disk_diagnostics = web_server._failure_diagnostics_from_disk(
            self.runs_dir / run_id
        )
        status = self.client.get(f"/api/runs/{run_id}/status")
        detail = self.client.get(
            f"/api/history/conversations/server_run_{run_id}"
        )

        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["run_state"], "failed")
        self.assertFalse(status.json()["publishable"])
        self.assertEqual(disk_diagnostics["error_message"], diagnostic)
        self.assertEqual(detail.status_code, 200, detail.text)
        messages = detail.json()["conversation"]["messages"]
        terminal = next(
            message for message in messages if message.get("run_id") == run_id
        )
        self.assertEqual(terminal["status"], "error")
        self.assertEqual(
            terminal["text"],
            "Run ended without a publishable artifact.",
        )
        self.assertEqual(terminal["failure"]["error_message"], diagnostic)
        serialized = json.dumps(terminal, ensure_ascii=False)
        self.assertNotIn(str(self.runs_dir), serialized)
        self.assertNotIn("Kokoro synthesis failed", serialized)
        self.assertEqual(store.read(run_id).state, "failed")
        self.assertIsNotNone(state)

    def test_legacy_failed_source_direct_publish_is_immutable(self) -> None:
        source_run_id = "legacy-failed-completion-source"
        source_dir, candidate = self._legacy_failed_source_with_candidate(
            source_run_id
        )
        before = self._tree_bytes(source_dir)
        response = self.client.post(
            f"/api/runs/{source_run_id}/attempts/1/publish",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json={
                "conversation_id": "legacy-failed-completion-conversation",
                "expected_candidate_sha256": candidate.source_sha256,
                "idempotency_key": "legacy-failed-completion-publication",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        with patch.object(
            candidate_publish_module,
            "validate_candidate_draft",
            return_value=[],
        ):
            result = candidate_publish_module.run_candidate_publish_job(
                run_id=run_id,
                parent_run_id=source_run_id,
                conversation_id="legacy-failed-completion-conversation",
                settings=self.settings,
                cancellation_token=web_server.CancellationToken.never(run_id),
                source_attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
            )
        self.assertEqual(result["lineage"]["status"], "validated")
        store = RunControlStore(self.runs_dir)
        record = store.read(run_id)
        record = store.transition(run_id, record, "running")
        store.transition(run_id, record, "completing")
        supervisor = web_server._web_run_runtime().supervisor
        supervisor._terminal_reconciler = web_server._reconcile_run_terminal_artifact

        async def accept() -> None:
            await supervisor.accept_completion(
                run_id,
                terminal_state="completed",
                publishable=True,
                result_digest="f" * 64,
            )

        self.client.portal.call(accept)

        completed = store.read(run_id)
        self.assertEqual(completed.state, "completed")
        self.assertTrue(completed.publishable)
        self.assertTrue((self.runs_dir / run_id / "final" / "index.html").is_file())
        lineage = json.loads(
            (self.runs_dir / run_id / "candidate_draft_lineage.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(lineage["status"], "published")
        self.assertFalse(
            (source_dir / "attempt_candidates" / "selection.json").exists()
        )
        self.assertEqual(self._tree_bytes(source_dir), before)

    def test_failed_source_canvas_publish_completes_without_source_writes(self) -> None:
        source_run_id = "failed-canvas-completion-source"
        source_dir, draft_run_id, _candidate = self._failed_source_candidate_draft(
            source_run_id
        )
        before = self._tree_bytes(source_dir)
        response = self.client.post(
            f"/api/artifacts/art_{draft_run_id}/publish-candidate-draft",
            headers={"X-Autodesign-Reserve-Only": "true"},
            json={
                "conversation_id": "failed-canvas-completion-conversation",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        with patch.object(
            candidate_publish_module,
            "validate_candidate_draft",
            return_value=[],
        ):
            result = candidate_publish_module.run_candidate_publish_job(
                run_id=run_id,
                parent_run_id=draft_run_id,
                conversation_id="failed-canvas-completion-conversation",
                settings=self.settings,
                cancellation_token=web_server.CancellationToken.never(run_id),
            )
        self.assertEqual(result["lineage"]["status"], "validated")
        store = RunControlStore(self.runs_dir)
        record = store.read(run_id)
        record = store.transition(run_id, record, "running")
        store.transition(run_id, record, "completing")
        supervisor = web_server._web_run_runtime().supervisor
        supervisor._terminal_reconciler = web_server._reconcile_run_terminal_artifact

        async def accept() -> None:
            await supervisor.accept_completion(
                run_id,
                terminal_state="completed",
                publishable=True,
                result_digest="e" * 64,
            )

        self.client.portal.call(accept)

        completed = store.read(run_id)
        self.assertEqual(completed.state, "completed")
        self.assertTrue(completed.publishable)
        lineage = json.loads(
            (self.runs_dir / run_id / "candidate_draft_lineage.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(lineage["status"], "published")
        self.assertEqual(self._tree_bytes(source_dir), before)

    def test_wrong_derived_start_token_does_not_poison_reservation(self) -> None:
        run_id, start_token = self._reserve_editable_video("wrong-token-source")
        store = RunControlStore(self.runs_dir)

        rejected = self.client.post(
            f"/api/runs/{run_id}/start",
            headers={"X-Autodesign-Upload-Token": "definitely-wrong"},
        )
        state_after_rejection = store.read(run_id).state
        accepted = self.client.post(
            f"/api/runs/{run_id}/start",
            headers={"X-Autodesign-Upload-Token": start_token},
        )
        state_after_acceptance = store.read(run_id).state
        if state_after_acceptance == "running":
            self.client.post(f"/api/runs/{run_id}/cancel")

        self.assertEqual(rejected.status_code, 404, rejected.text)
        self.assertEqual(state_after_rejection, "queued")
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(state_after_acceptance, "running")

    def test_repeated_derived_start_requires_token_and_reuses_monitor(self) -> None:
        run_id, start_token = self._reserve_editable_video("repeated-start-source")
        original_monitor = web_server._monitor_supervised_derived_job
        first_monitor_started = asyncio.Event()
        monitor_calls = 0

        async def counted_monitor(**kwargs):
            nonlocal monitor_calls
            monitor_calls += 1
            first_monitor_started.set()
            await original_monitor(**kwargs)

        async def wait_for_scheduled_monitor() -> None:
            await asyncio.wait_for(first_monitor_started.wait(), timeout=2.0)
            await asyncio.sleep(0)

        headers = {"X-Autodesign-Upload-Token": start_token}
        with patch.object(
            web_server,
            "_monitor_supervised_derived_job",
            side_effect=counted_monitor,
        ):
            started = self.client.post(
                f"/api/runs/{run_id}/start",
                headers=headers,
            )
            unauthorized_repeat = self.client.post(
                f"/api/runs/{run_id}/start",
                headers={"X-Autodesign-Upload-Token": "definitely-wrong"},
            )
            repeated = self.client.post(
                f"/api/runs/{run_id}/start",
                headers=headers,
            )
            self.client.portal.call(wait_for_scheduled_monitor)
            roots = [
                record
                for record in ProcessLedger(self.runs_dir / run_id).read().processes
                if record.role == "root-worker"
            ]
            cancelled = self.client.post(f"/api/runs/{run_id}/cancel")

        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(
            unauthorized_repeat.status_code,
            404,
            unauthorized_repeat.text,
        )
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(len(roots), 1)
        self.assertEqual(monitor_calls, 1)
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertTrue(cancelled.json()["confirmed"])

    def test_concurrent_derived_start_shares_one_worker_and_monitor(self) -> None:
        run_id, start_token = self._reserve_editable_video("concurrent-start-source")
        runtime = web_server._web_run_runtime()
        original_start = runtime.services.start
        original_monitor = web_server._monitor_supervised_derived_job
        first_pair_barrier = asyncio.Barrier(2)
        first_monitor_started = asyncio.Event()
        service_start_calls = 0
        monitor_calls = 0

        async def synchronized_start(*args, **kwargs):
            nonlocal service_start_calls
            service_start_calls += 1
            if service_start_calls <= 2:
                await first_pair_barrier.wait()
            return await original_start(*args, **kwargs)

        async def counted_monitor(**kwargs):
            nonlocal monitor_calls
            monitor_calls += 1
            first_monitor_started.set()
            await original_monitor(**kwargs)

        async def wait_for_scheduled_monitors() -> None:
            await asyncio.wait_for(first_monitor_started.wait(), timeout=2.0)
            await asyncio.sleep(0)

        headers = {"X-Autodesign-Upload-Token": start_token}
        with (
            patch.object(runtime.services, "start", side_effect=synchronized_start),
            patch.object(
                web_server,
                "_monitor_supervised_derived_job",
                side_effect=counted_monitor,
            ),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(
                    pool.map(
                        lambda _index: self.client.post(
                            f"/api/runs/{run_id}/start",
                            headers=headers,
                        ),
                        range(2),
                    )
                )
            self.client.portal.call(wait_for_scheduled_monitors)

            roots = [
                record
                for record in ProcessLedger(self.runs_dir / run_id).read().processes
                if record.role == "root-worker"
            ]
            cancelled = self.client.post(f"/api/runs/{run_id}/cancel")

        self.assertEqual(
            [response.status_code for response in responses],
            [200, 200],
            [response.text for response in responses],
        )
        self.assertEqual(len(roots), 1)
        self.assertEqual(monitor_calls, 1)
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertTrue(cancelled.json()["confirmed"])

    def test_cancel_racing_derived_start_finishes_cancelled_and_quiescent(self) -> None:
        run_id, start_token = self._reserve_editable_video("cancel-start-source")
        runtime = web_server._web_run_runtime()
        supervisor = runtime.supervisor
        headers = {"X-Autodesign-Upload-Token": start_token}

        with patch.object(supervisor, "_root_registration_delay_s", 0.2):
            with ThreadPoolExecutor(max_workers=1) as pool:
                start_request = pool.submit(
                    self.client.post,
                    f"/api/runs/{run_id}/start",
                    headers=headers,
                )
                ledger_path = self.runs_dir / run_id / "process_ledger.json"
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if (
                        ledger_path.is_file()
                        and '"status": "spawning"' in ledger_path.read_text(encoding="utf-8")
                    ):
                        break
                    time.sleep(0.01)
                else:
                    self.fail("derived worker never entered the spawning window")
                cancelled = self.client.post(f"/api/runs/{run_id}/cancel")
                started = start_request.result(timeout=5.0)

        record = RunControlStore(self.runs_dir).read(run_id)
        roots = [
            item
            for item in ProcessLedger(self.runs_dir / run_id).read().processes
            if item.role == "root-worker"
        ]
        state = web_server._RUNS[run_id]

        self.assertIn(started.status_code, {200, 409}, started.text)
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertTrue(cancelled.json()["confirmed"])
        self.assertEqual(record.state, "cancelled")
        self.assertTrue(supervisor.is_durably_quiescent(run_id))
        self.assertTrue(all(not process_is_alive(item.identity) for item in roots))
        self.assertTrue(state.task is None or state.task.done())

    def test_editable_video_requires_publishable_source_before_reserve(self) -> None:
        source_run_id = "running-video-source"
        store = RunControlStore(self.runs_dir)
        record = store.reserve(source_run_id, "video")
        record = store.transition(source_run_id, record, "queued")
        store.transition(source_run_id, record, "running")
        before = set(path.name for path in self.runs_dir.iterdir())
        artifact = {
            "artifact_id": f"art_{source_run_id}",
            "artifact_type": "video",
            "video_project": {
                "scenes": [{"id": "scene-1", "duration_s": 1, "layers": []}],
            },
        }

        with patch.object(web_server, "_require_artifact_runtime"):
            response = self.client.post(
                "/api/video/render",
                json={"artifact": artifact},
            )

        self.assertIn(response.status_code, {409, 410, 423}, response.text)
        self.assertEqual(
            set(path.name for path in self.runs_dir.iterdir()),
            before,
        )

    def test_editable_video_requires_real_source_run_id(self) -> None:
        before = set(path.name for path in self.runs_dir.iterdir())
        artifact = {
            "artifact_type": "video",
            "video_project": {
                "scenes": [{"id": "scene-1", "duration_s": 1, "layers": []}],
            },
        }

        with patch.object(web_server, "_require_artifact_runtime"):
            response = self.client.post(
                "/api/video/render",
                json={"artifact": artifact},
            )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(
            set(path.name for path in self.runs_dir.iterdir()),
            before,
        )

    def test_editable_video_endpoint_rejects_internal_cross_run_symlink_and_hardlink_assets(self) -> None:
        source_run_id = "video-asset-source"
        victim_run_id = "video-asset-victim"
        source_dir = self._complete_source(source_run_id, "video")
        victim_secret = self.runs_dir / victim_run_id / "final" / "secret.png"
        victim_secret.parent.mkdir(parents=True)
        victim_secret.write_bytes(b"victim-video-image")
        source_final = source_dir / "final"
        source_final.mkdir(parents=True, exist_ok=True)
        symlink_alias = source_final / "symlink.png"
        symlink_alias.symlink_to(victim_secret)
        hardlink_alias = source_final / "hardlink.png"
        hardlink_alias.hardlink_to(victim_secret)
        cases = {
            "internal": f"/api/files/runs/{source_run_id}/RUN_CONTROL.JSON",
            "cross_run": f"/api/files/runs/{victim_run_id}/final/secret.png",
            "symlink": f"/api/files/runs/{source_run_id}/final/symlink.png",
            "hardlink": f"/api/files/runs/{source_run_id}/final/hardlink.png",
            "encoded_separator": (
                f"/api/files/runs/{source_run_id}%2F..%2F{victim_run_id}"
                "/final/secret.png"
            ),
            "encoded_internal": (
                f"/api/files/runs/{source_run_id}/%52UN_CONTROL.JSON"
            ),
        }

        for label, image_url in cases.items():
            artifact = {
                "artifact_id": f"art_{source_run_id}",
                "artifact_type": "video",
                "layers": [{"kind": "image", "src": image_url}],
                "video_project": {
                    "fps": 30,
                    "scenes": [{
                        "id": "scene-1",
                        "duration_s": 1,
                    }],
                },
            }
            starter = AsyncMock()
            with (
                self.subTest(label=label),
                patch.object(web_server, "_require_artifact_runtime"),
                patch.object(web_server, "_start_supervised_derived_job", starter),
            ):
                response = self.client.post(
                    "/api/video/render",
                    json={"artifact": artifact},
                )

            self.assertEqual(response.status_code, 400, response.text)
            starter.assert_not_awaited()

    def test_editable_video_endpoint_allows_same_run_regular_asset(self) -> None:
        source_run_id = "video-asset-valid-source"
        source_dir = self._complete_source(source_run_id, "video")
        source_image = source_dir / "final" / "figure.png"
        source_image.parent.mkdir(parents=True, exist_ok=True)
        source_image.write_bytes(b"same-run-video-image")
        artifact = {
            "artifact_id": f"art_{source_run_id}",
            "artifact_type": "video",
            "layers": [{
                "kind": "image",
                "src": f"/api/files/runs/{source_run_id}/final/figure.png",
            }],
            "video_project": {
                "fps": 30,
                "scenes": [{
                    "id": "scene-1",
                    "duration_s": 1,
                }],
            },
        }
        starter = AsyncMock()

        with (
            patch.object(web_server, "_require_artifact_runtime"),
            patch.object(web_server, "_start_supervised_derived_job", starter),
        ):
            response = self.client.post(
                "/api/video/render",
                json={"artifact": artifact},
            )

        self.assertEqual(response.status_code, 200, response.text)
        starter.assert_awaited_once()

    def test_poster_code_edit_cancel_confirms_after_supervised_worker_exit(self) -> None:
        source_run_id = "poster-edit-source"
        source_dir = self._complete_source(source_run_id, "poster")
        source_html = source_dir / "final" / "poster.html"
        source_html.parent.mkdir(parents=True)
        source_html.write_text("<html><body>source</body></html>", encoding="utf-8")
        artifact = {
            "artifact_id": f"art_{source_run_id}",
            "artifact_type": "poster",
            "native_format": "html",
            "native_file_url": (
                f"/api/files/runs/{source_run_id}/final/poster.html"
            ),
        }

        with (
            patch.object(
                web_server,
                "_settings_for_code_editor_request",
                return_value=self.settings,
            ),
            patch.object(
                web_server,
                "_code_editor_cmd_resolution",
                return_value={
                    "available": True,
                    "cmd": "codex",
                    "source": "test",
                },
            ),
            patch.object(
                web_server,
                "require_academic_color_system",
                return_value={"palette_id": "royal_blue"},
            ),
            patch.object(
                web_server,
                "_validated_web_palette_id",
                return_value="royal_blue",
            ),
        ):
            response = self.client.post(
                "/api/code-edit/poster",
                json={
                    "artifact": artifact,
                    "instruction": "tighten the title",
                    "source_run_id": source_run_id,
                    "palette_id": "royal_blue",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        record = RunControlStore(self.runs_dir).read(run_id)
        self.assertEqual(record.state, "running")
        assert record.worker_pid is not None
        identity = process_identity(record.worker_pid)

        cancelled = self.client.post(f"/api/runs/{run_id}/cancel")

        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertTrue(cancelled.json()["confirmed"])
        self.assertFalse(process_is_alive(identity))
        self.assertFalse((self.runs_dir / run_id / "final" / "poster.html").exists())

    def test_pptx_cancel_kills_tree_and_never_mutates_source_run(self) -> None:
        source_run_id = "pptx-source"
        source_dir = self._complete_source(source_run_id, "poster")
        source_html = source_dir / "final" / "poster.html"
        source_html.parent.mkdir(parents=True)
        source_html.write_text("<html><body>source</body></html>", encoding="utf-8")
        artifact = {
            "artifact_id": f"art_{source_run_id}",
            "artifact_type": "poster",
            "native_format": "html",
            "native_file_url": (
                f"/api/files/runs/{source_run_id}/final/poster.html"
            ),
            "name": "Poster",
        }
        before = {
            path.relative_to(source_dir).as_posix(): path.read_bytes()
            for path in source_dir.rglob("*")
            if path.is_file()
        }

        with (
            patch.object(
                web_server,
                "_settings_for_code_editor_request",
                return_value=self.settings,
            ),
            patch.object(
                web_server,
                "_code_editor_cmd_resolution",
                return_value={
                    "available": True,
                    "cmd": "codex",
                    "source": "test",
                },
            ),
        ):
            response = self.client.post(
                "/api/artifacts/export/pptx-run",
                json={"artifact": artifact},
            )

        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        record = RunControlStore(self.runs_dir).read(run_id)
        self.assertEqual(record.state, "running")
        assert record.worker_pid is not None
        identity = process_identity(record.worker_pid)

        cancelled = self.client.post(f"/api/runs/{run_id}/cancel")

        after = {
            path.relative_to(source_dir).as_posix(): path.read_bytes()
            for path in source_dir.rglob("*")
            if path.is_file()
        }
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertTrue(cancelled.json()["confirmed"])
        self.assertFalse(process_is_alive(identity))
        self.assertEqual(after, before)
        self.assertFalse((self.runs_dir / run_id / "exports").exists())
        child_dir = self.runs_dir / run_id
        frozen = {
            path.relative_to(child_dir).as_posix(): path.read_bytes()
            for path in child_dir.rglob("*")
            if path.is_file()
        }
        time.sleep(0.15)
        self.assertEqual(
            {
                path.relative_to(child_dir).as_posix(): path.read_bytes()
                for path in child_dir.rglob("*")
                if path.is_file()
            },
            frozen,
        )

    def test_video_retry_cancel_stops_later_stage_and_final_delivery(self) -> None:
        source_run_id = "video-retry-source"
        source_dir = self.runs_dir / source_run_id
        project = source_dir / "hyperframes-source"
        project.mkdir(parents=True)
        (project / "index.html").write_text("<html></html>", encoding="utf-8")
        (project / "video_delivery_contract.json").write_text(
            "{}",
            encoding="utf-8",
        )
        (source_dir / "design_spec.json").write_text("{}", encoding="utf-8")
        store = RunControlStore(self.runs_dir)
        source = store.reserve(source_run_id, "video")
        source = store.transition(source_run_id, source, "queued")
        source = store.transition(source_run_id, source, "running")
        store.transition(source_run_id, source, "failed", publishable=False)

        with patch.object(web_server, "_require_artifact_runtime"):
            response = self.client.post(
                f"/api/runs/{source_run_id}/retry-video-export",
            )

        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        record = RunControlStore(self.runs_dir).read(run_id)
        self.assertEqual(record.state, "running")
        assert record.worker_pid is not None
        identity = process_identity(record.worker_pid)

        cancelled = self.client.post(f"/api/runs/{run_id}/cancel")

        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertTrue(cancelled.json()["confirmed"])
        self.assertFalse(process_is_alive(identity))
        self.assertFalse(
            (self.runs_dir / run_id / "video-retry" / "stage-two-started").exists()
        )
        self.assertFalse((self.runs_dir / run_id / "final").exists())
        child_dir = self.runs_dir / run_id
        frozen = {
            path.relative_to(child_dir).as_posix(): path.read_bytes()
            for path in child_dir.rglob("*")
            if path.is_file()
        }
        time.sleep(0.15)
        self.assertEqual(
            {
                path.relative_to(child_dir).as_posix(): path.read_bytes()
                for path in child_dir.rglob("*")
                if path.is_file()
            },
            frozen,
        )

    def test_legacy_video_retry_success_cold_recovers_as_publishable_artifact(
        self,
    ) -> None:
        run_id = "legacy-warning-recovery"
        parent_run_id = "legacy-warning-source"
        store, legacy_result, _request = self._prepare_video_retry_completion(
            run_id,
            parent_run_id,
            completing=True,
        )
        run_dir = self.runs_dir / run_id
        (run_dir / "worker_result.json").write_text(
            json.dumps(
                {
                    "job_kind": "video_export_retry",
                    "run_id": run_id,
                    "ok": True,
                    "result": legacy_result,
                }
            ),
            encoding="utf-8",
        )
        web_server._RUNS.pop(run_id, None)

        async def recover_and_join():
            await web_server._recover_web_run_controls()
            state = web_server._RUNS.get(run_id)
            if state is not None and state.task is not None:
                await state.task
            return state

        with patch.object(web_server, "_append_event") as append_event:
            state = self.client.portal.call(recover_and_join)

        generated = [
            call
            for call in append_event.call_args_list
            if len(call.args) >= 3 and call.args[2] == "artifact.generated"
        ]
        failed = [
            call
            for call in append_event.call_args_list
            if len(call.args) >= 3
            and call.args[2] == "artifact.generation_failed"
        ]
        record = store.read(run_id)
        message = getattr(state, "result_message", None)

        self.assertEqual(
            {
                "state_recovered": state is not None,
                "control_state": record.state,
                "publishable": record.publishable,
                "artifact": getattr(state, "result_artifact", None) is not None,
                "message_status": getattr(message, "status", None),
                "message_failure": getattr(message, "failure", None),
                "generated_events": len(generated),
                "warnings": (
                    generated[0].kwargs.get("data", {}).get(
                        "pointer_cleanup_warnings"
                    )
                    if generated
                    else None
                ),
                "failed_events": len(failed),
            },
            {
                "state_recovered": True,
                "control_state": "completed",
                "publishable": True,
                "artifact": True,
                "message_status": "done",
                "message_failure": None,
                "generated_events": 1,
                "warnings": [],
                "failed_events": 0,
            },
        )

    def test_legacy_video_retry_failure_cold_recovery_finishes_terminally(
        self,
    ) -> None:
        run_id = "legacy-video-retry-failure-recovery"
        parent_run_id = "legacy-video-retry-failure-source"
        diagnostic = "legacy video retry failed before warning diagnostics"
        store, _result, _request = self._prepare_video_retry_completion(
            run_id,
            parent_run_id,
            completing=True,
        )
        run_dir = self.runs_dir / run_id
        (run_dir / "worker_result.json").write_text(
            json.dumps(
                {
                    "job_kind": "video_export_retry",
                    "run_id": run_id,
                    "ok": False,
                    "error": {
                        "type": "RuntimeError",
                        "message": diagnostic,
                        "traceback": "legacy traceback",
                    },
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(store.read(run_id).state, "completing")
        web_server._RUNS.pop(run_id, None)

        async def recover_and_join():
            await web_server._recover_web_run_controls()
            state = web_server._RUNS.get(run_id)
            task = state.task if state is not None else None
            if task is not None:
                await task
            quiesced = await web_server._quiesce_web_completion_monitor(run_id)
            return state, task is not None and task.done(), quiesced

        with patch.object(web_server, "_append_event") as append_event:
            state, task_done, quiesced = self.client.portal.call(recover_and_join)

        failed = [
            call
            for call in append_event.call_args_list
            if len(call.args) >= 3
            and call.args[2] == "artifact.generation_failed"
        ]
        generated = [
            call
            for call in append_event.call_args_list
            if len(call.args) >= 3 and call.args[2] == "artifact.generated"
        ]
        message = getattr(state, "result_message", None)
        failure = getattr(message, "failure", None)
        failure_dump = failure.model_dump() if failure is not None else {}
        event_data = failed[0].kwargs.get("data", {}) if failed else {}
        event_failure = (
            event_data.get("failure", {})
            if isinstance(event_data, dict)
            else {}
        )
        record = store.read(run_id)

        self.assertEqual(
            {
                "state_recovered": state is not None,
                "control_state": record.state,
                "publishable": record.publishable,
                "task_done": task_done,
                "quiesced": quiesced,
                "active_or_completing": (
                    record.state == "completing" or not task_done
                ),
                "state_error": getattr(state, "error", None),
                "message_text": getattr(message, "text", None),
                "message_failure_note": failure_dump.get("agent_last_note"),
                "message_warnings": tuple(
                    failure_dump.get("pointer_cleanup_warnings") or ()
                ),
                "artifact": getattr(state, "result_artifact", None),
                "failed_events": len(failed),
                "generated_events": len(generated),
                "event_error": (
                    event_data.get("error")
                    if isinstance(event_data, dict)
                    else None
                ),
                "event_failure_note": (
                    event_failure.get("agent_last_note")
                    if isinstance(event_failure, dict)
                    else None
                ),
                "event_warnings": tuple(
                    event_failure.get("pointer_cleanup_warnings") or ()
                ) if isinstance(event_failure, dict) else (),
            },
            {
                "state_recovered": True,
                "control_state": "failed",
                "publishable": False,
                "task_done": True,
                "quiesced": True,
                "active_or_completing": False,
                "state_error": diagnostic,
                "message_text": diagnostic,
                "message_failure_note": diagnostic,
                "message_warnings": (),
                "artifact": None,
                "failed_events": 1,
                "generated_events": 0,
                "event_error": diagnostic,
                "event_failure_note": diagnostic,
                "event_warnings": (),
            },
        )

    def test_video_retry_success_warnings_reach_live_and_cold_web_diagnostics(
        self,
    ) -> None:
        warnings = [
            "invalidation cleanup warning",
            "publication cleanup warning",
        ]

        for transport in ("live", "cold"):
            with self.subTest(transport=transport):
                run_id = f"warning-success-{transport}"
                parent_run_id = f"warning-source-{transport}"
                store, result, request = self._prepare_video_retry_completion(
                    run_id,
                    parent_run_id,
                    completing=transport == "cold",
                )
                result["pointer_cleanup_warnings"] = list(warnings)
                run_dir = self.runs_dir / run_id
                (run_dir / "worker_result.json").write_text(
                    json.dumps(
                        {
                            "job_kind": "video_export_retry",
                            "run_id": run_id,
                            "ok": True,
                            "result": result,
                        }
                    ),
                    encoding="utf-8",
                )
                web_server._RUNS.pop(run_id, None)
                live_outcome_warnings: object = None

                async def run_live():
                    class _FinishedProcess:
                        async def wait(self) -> int:
                            return 0

                    async def done(value):
                        return value

                    outcome = await web_server._web_run_runtime().supervisor._monitor(
                        request,
                        _FinishedProcess(),
                        stdout_task=asyncio.create_task(done(None)),
                        stderr_task=asyncio.create_task(done(None)),
                        relay_task=asyncio.create_task(done(0)),
                    )
                    state = web_server._RunState(
                        artifact_type="video",
                        conversation_id="warning-conversation",
                        baseline_artifact_json="{}",
                    )
                    await web_server._monitor_supervised_derived_job(
                        run_id=run_id,
                        state=state,
                        job_kind="video_export_retry",
                        parent_run_id=parent_run_id,
                        descriptor={"job_kind": "video_export_retry"},
                        recovered_outcome=outcome,
                    )
                    return state, (
                        outcome.result.get("pointer_cleanup_warnings")
                        if isinstance(outcome.result, dict)
                        else None
                    )

                async def run_cold():
                    await web_server._recover_web_run_controls()
                    state = web_server._RUNS.get(run_id)
                    if state is not None and state.task is not None:
                        await state.task
                    return state

                with patch.object(web_server, "_append_event") as append_event:
                    if transport == "live":
                        state, live_outcome_warnings = self.client.portal.call(
                            run_live
                        )
                    else:
                        state = self.client.portal.call(run_cold)

                generated = [
                    call
                    for call in append_event.call_args_list
                    if len(call.args) >= 3
                    and call.args[2] == "artifact.generated"
                ]
                failed = [
                    call
                    for call in append_event.call_args_list
                    if len(call.args) >= 3
                    and call.args[2] == "artifact.generation_failed"
                ]
                record = store.read(run_id)
                message = getattr(state, "result_message", None)
                self.assertEqual(
                    {
                        "state_recovered": state is not None,
                        "control_state": record.state,
                        "publishable": record.publishable,
                        "state_error": getattr(state, "error", None),
                        "artifact": getattr(state, "result_artifact", None)
                        is not None,
                        "message_status": getattr(message, "status", None),
                        "message_failure": getattr(message, "failure", None),
                        "live_outcome_warnings": live_outcome_warnings,
                        "generated_events": len(generated),
                        "event_warnings": (
                            generated[0].kwargs.get("data", {}).get(
                                "pointer_cleanup_warnings"
                            )
                            if generated
                            else None
                        ),
                        "failed_events": len(failed),
                    },
                    {
                        "state_recovered": True,
                        "control_state": "completed",
                        "publishable": True,
                        "state_error": None,
                        "artifact": True,
                        "message_status": "done",
                        "message_failure": None,
                        "live_outcome_warnings": (
                            warnings if transport == "live" else None
                        ),
                        "generated_events": 1,
                        "event_warnings": warnings,
                        "failed_events": 0,
                    },
                )

    def test_video_retry_long_failure_warning_survives_cold_web_recovery(
        self,
    ) -> None:
        run_id = "warning-failure-cold"
        parent_run_id = "warning-failure-source"
        store, _result, _request = self._prepare_video_retry_completion(
            run_id,
            parent_run_id,
            completing=True,
        )
        warning = "cleanup warning retained after the long base error"
        parent_dir = self.runs_dir / parent_run_id
        source_project = parent_dir / "hyperframes-source"
        source_project.mkdir(parents=True)
        (source_project / "index.html").write_text(
            "<html></html>",
            encoding="utf-8",
        )
        (parent_dir / "design_spec.json").write_text("{}\n", encoding="utf-8")
        request = VideoExportRetryWorkerRequest(
            job_kind="video_export_retry",
            run_id=run_id,
            parent_run_id=parent_run_id,
            source_project=str(source_project),
            conversation_id="warning-conversation",
            baseline_artifact_json="{}",
            runs_dir=str(self.runs_dir),
        )
        stdin = SimpleNamespace(buffer=BytesIO(encode_request(request)))
        with (
            patch(
                "autodesign.tools.export_video.retry_video_export_project",
                return_value={
                    "ok": False,
                    "phase": "final_pointer",
                    "error": "x" * 2400,
                    "pointer_cleanup_warnings": [warning],
                },
            ),
            patch.object(sys, "argv", ["run_worker", "--run-id", run_id]),
            patch.object(sys, "stdin", stdin),
            patch.object(run_worker_module.signal, "signal"),
            patch.object(
                run_worker_module.os,
                "getsid",
                return_value=os.getpid(),
            ),
        ):
            exit_code = run_worker_module.worker_main()
        web_server._RUNS.pop(run_id, None)

        async def recover_and_join():
            await web_server._recover_web_run_controls()
            state = web_server._RUNS.get(run_id)
            if state is not None and state.task is not None:
                await state.task
            return state

        state = self.client.portal.call(recover_and_join)
        record = store.read(run_id)
        message = getattr(state, "result_message", None)
        failure = getattr(message, "failure", None)

        self.assertEqual(
            {
                "state_recovered": state is not None,
                "worker_exit_code": exit_code,
                "control_state": record.state,
                "publishable": record.publishable,
                "message_status": getattr(message, "status", None),
                "failure_phase": getattr(failure, "phase", None),
                "warning_in_state_error": warning
                in str(getattr(state, "error", "")),
                "warning_in_message": warning in str(getattr(message, "text", "")),
                "warning_in_failure": warning
                in str(getattr(failure, "agent_last_note", "")),
            },
            {
                "state_recovered": True,
                "worker_exit_code": 1,
                "control_state": "failed",
                "publishable": False,
                "message_status": "error",
                "failure_phase": "final_pointer",
                "warning_in_state_error": True,
                "warning_in_message": True,
                "warning_in_failure": True,
            },
        )

    def test_pptx_completing_run_recovers_from_durable_descriptor(self) -> None:
        source_run_id = "pptx-recovery-source"
        self._complete_source(source_run_id, "poster")
        run_id = "pptx-recovery-child"
        store = RunControlStore(self.runs_dir)
        record = store.reserve(run_id, "poster", parent_job_id=source_run_id)
        record = store.transition(run_id, record, "queued")
        record = store.transition(run_id, record, "running")
        store.transition(run_id, record, "completing")
        run_dir = self.runs_dir / run_id
        output = run_dir / "exports" / "Recovered.pptx"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"pptx-recovery")
        (run_dir / "derived_job.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "job_kind": "pptx_export",
                    "run_id": run_id,
                    "parent_run_id": source_run_id,
                    "artifact_type": "poster",
                    "conversation_id": "conversation",
                    "baseline_artifact_json": "",
                    "source_artifact_id": f"art_{source_run_id}",
                    "artifact_name": "Recovered",
                    "source_relative_path": "final/poster.html",
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "worker_result.json").write_text(
            json.dumps(
                {
                    "job_kind": "pptx_export",
                    "run_id": run_id,
                    "ok": True,
                    "result": {
                        "run_id": run_id,
                        "pptx_path": str(output),
                    },
                }
            ),
            encoding="utf-8",
        )
        web_server._RUNS.pop(run_id, None)

        async def recover_and_join() -> None:
            await web_server._recover_web_run_controls()
            state = web_server._RUNS[run_id]
            assert state.task is not None
            await state.task

        self.client.portal.call(recover_and_join)

        recovered = store.read(run_id)
        self.assertEqual(recovered.state, "completed")
        self.assertTrue(recovered.publishable)
        message = web_server._RUNS[run_id].result_message
        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn(f"/api/files/runs/{run_id}/exports/", message.download_url or "")

    def test_corrupt_derived_descriptor_fails_instead_of_staying_completing(self) -> None:
        source_run_id = "corrupt-derived-source"
        self._complete_source(source_run_id, "poster")
        run_id = "corrupt-derived-child"
        store = RunControlStore(self.runs_dir)
        record = store.reserve(run_id, "poster", parent_job_id=source_run_id)
        record = store.transition(run_id, record, "queued")
        record = store.transition(run_id, record, "running")
        store.transition(run_id, record, "completing")
        run_dir = self.runs_dir / run_id
        (run_dir / "derived_job.json").write_text("{broken", encoding="utf-8")
        (run_dir / "worker_result.json").write_text(
            json.dumps(
                {
                    "job_kind": "pptx_export",
                    "run_id": run_id,
                    "ok": False,
                    "error": {"type": "RuntimeError", "message": "failed"},
                }
            ),
            encoding="utf-8",
        )

        self.client.portal.call(web_server._recover_web_run_controls)

        recovered = store.read(run_id)
        self.assertEqual(recovered.state, "failed")
        self.assertFalse(recovered.publishable)

    def test_parent_cancel_fails_closed_on_corrupt_derived_descriptor(self) -> None:
        source_run_id = "corrupt-descendant-source"
        run_id, start_token = self._reserve_editable_video(source_run_id)
        started = self.client.post(
            f"/api/runs/{run_id}/start",
            headers={"X-Autodesign-Upload-Token": start_token},
        )
        self.assertEqual(started.status_code, 200, started.text)
        store = RunControlStore(self.runs_dir)
        running = store.read(run_id)
        self.assertEqual(running.state, "running")
        assert running.worker_pid is not None
        identity = process_identity(running.worker_pid)
        (self.runs_dir / run_id / "derived_job.json").write_text(
            "{broken",
            encoding="utf-8",
        )

        cancelled = self.client.post(f"/api/runs/{source_run_id}/cancel")

        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertTrue(cancelled.json()["confirmed"])
        self.assertEqual(store.read(run_id).state, "cancelled")
        self.assertFalse(process_is_alive(identity))

    def test_startup_recovery_does_not_terminalize_queued_run_before_supervisor(self) -> None:
        run_id = "queued-startup-recovery"
        store = RunControlStore(self.runs_dir)
        record = store.reserve(run_id, "video")
        store.transition(run_id, record, "queued")
        supervisor = web_server._web_run_runtime().supervisor
        original_recover = supervisor.recover
        observed_states: list[str] = []

        async def observing_recover(recovered_run_id: str):
            observed_states.append(store.read(recovered_run_id).state)
            return await original_recover(recovered_run_id)

        with patch.object(supervisor, "recover", side_effect=observing_recover):
            self.client.portal.call(web_server._recover_web_run_controls)

        self.assertEqual(observed_states, ["queued"])
        recovered = store.read(run_id)
        self.assertEqual(recovered.state, "failed")
        self.assertTrue(recovered.writes_frozen)

    def test_cancel_winning_derived_completion_clears_prepared_result(self) -> None:
        run_id = "derived-cancel-race"
        store = RunControlStore(self.runs_dir)
        record = store.reserve(run_id, "poster", parent_job_id="source")
        record = store.transition(run_id, record, "queued")
        record = store.transition(run_id, record, "running")
        store.transition(run_id, record, "completing")
        artifact = web_server.Artifact(
            artifact_id=f"art_{run_id}",
            name="Poster",
            artifact_type="poster",
            canvas=web_server.Canvas(w=1600, h=900),
            native_file_url=f"/api/files/runs/{run_id}/final/poster.html",
            native_format="html",
        )
        message = web_server.Message(
            id=f"msg_{run_id}",
            role="assistant",
            text="done",
            ts=0,
            run_id=run_id,
            artifact_id=artifact.artifact_id,
            status="done",
        )
        state = web_server._RunState(
            artifact_type="poster",
            brief="revise",
            conversation_id="conversation",
        )
        outcome = web_server.WorkerOutcome(
            run_id=run_id,
            job_kind="poster_code_edit",
            returncode=0,
            ok=True,
            result={"run_id": run_id},
            error=None,
            relayed_events=0,
        )

        class _CancellationWinsSupervisor:
            async def accept_completion(inner_self, _run_id: str, **_kwargs: object):
                return SimpleNamespace(state="cancelled")

        runtime = SimpleNamespace(
            control_store=store,
            supervisor=_CancellationWinsSupervisor(),
        )
        with (
            patch.object(web_server, "_web_run_runtime", return_value=runtime),
            patch.object(
                web_server,
                "_prepare_derived_completion",
                return_value=(True, artifact, message, {"artifact_type": "poster"}),
            ),
            patch.object(web_server, "_append_event") as append_event,
        ):
            async def monitor() -> None:
                await web_server._monitor_supervised_derived_job(
                    run_id=run_id,
                    state=state,
                    job_kind="poster_code_edit",
                    parent_run_id="source",
                    descriptor={"job_kind": "poster_code_edit"},
                    recovered_outcome=outcome,
                )

            self.client.portal.call(monitor)

        self.assertIsNone(state.result_artifact)
        self.assertIsNone(state.result_message)
        append_event.assert_not_called()

    def test_derived_worker_exit_uses_structured_failure_fields(self) -> None:
        run_id = "derived-worker-exit"
        store = RunControlStore(self.runs_dir)
        record = store.reserve(run_id, "deck", parent_job_id="source")
        record = store.transition(run_id, record, "queued")
        record = store.transition(run_id, record, "running")
        store.transition(run_id, record, "completing")
        state = web_server._RunState(
            artifact_type="deck",
            brief="export",
            conversation_id="conversation",
        )
        diagnostic = WorkerExitDiagnostic(
            version=1,
            returncode=17,
            error_code="worker_result_missing",
            error_message=(
                "The worker exited before writing its result. "
                "Review diagnostics before retrying."
            ),
            error_detail=(
                "Worker exit status: 17.\n"
                "Result protocol: worker_result.json is missing."
            ),
            protocol_error="worker_result.json is missing",
            last_event="deck.export",
            last_worker_seq=3,
            last_phase="render",
            last_reason="process_exit",
        )
        outcome = web_server.WorkerOutcome(
            run_id=run_id,
            job_kind="pptx_export",
            returncode=17,
            ok=False,
            result=None,
            error=diagnostic.error_message,
            relayed_events=3,
            exit_diagnostic=diagnostic,
        )

        class _FailedSupervisor:
            async def accept_completion(inner_self, _run_id: str, **_kwargs: object):
                return SimpleNamespace(state="failed")

        runtime = SimpleNamespace(
            control_store=store,
            supervisor=_FailedSupervisor(),
        )
        with (
            patch.object(web_server, "_web_run_runtime", return_value=runtime),
            patch.object(web_server, "_append_event"),
        ):
            async def monitor() -> None:
                await web_server._monitor_supervised_derived_job(
                    run_id=run_id,
                    state=state,
                    job_kind="pptx_export",
                    parent_run_id="source",
                    descriptor={"job_kind": "pptx_export"},
                    recovered_outcome=outcome,
                )

            self.client.portal.call(monitor)

        self.assertIsNotNone(state.result_message)
        assert state.result_message is not None
        failure = state.result_message.failure
        self.assertIsNotNone(failure)
        assert failure is not None
        self.assertEqual(failure.phase, "render")
        self.assertEqual(failure.error_code, "worker_result_missing")
        self.assertEqual(failure.error_message, diagnostic.error_message)
        self.assertEqual(failure.error_detail, diagnostic.error_detail)

    def test_structured_failure_retries_terminal_accept_without_losing_cause(
        self,
    ) -> None:
        run_id = "derived-failure-accept-retry"
        store = RunControlStore(self.runs_dir)
        record = store.reserve(run_id, "deck", parent_job_id="source")
        record = store.transition(run_id, record, "queued")
        record = store.transition(run_id, record, "running")
        store.transition(run_id, record, "completing")
        state = web_server._RunState(
            artifact_type="deck",
            brief="export",
            conversation_id="conversation",
        )
        primary_error = "specific structured worker failure"
        outcome = WorkerOutcome(
            run_id=run_id,
            job_kind="pptx_export",
            returncode=1,
            ok=False,
            result=None,
            error=primary_error,
            relayed_events=0,
            failure_phase="render",
        )

        class _RetryingSupervisor:
            def __init__(inner_self) -> None:
                inner_self.calls = 0

            async def accept_completion(inner_self, _run_id: str, **_kwargs: object):
                inner_self.calls += 1
                if inner_self.calls == 1:
                    raise OSError("transient control persistence failure")
                current = store.read(run_id)
                return store.transition(
                    run_id,
                    current,
                    "failed",
                    publishable=False,
                    terminal_event="run.error",
                )

        supervisor = _RetryingSupervisor()
        runtime = SimpleNamespace(control_store=store, supervisor=supervisor)
        with (
            patch.object(web_server, "_web_run_runtime", return_value=runtime),
            patch.object(web_server, "_append_event"),
        ):
            async def monitor() -> None:
                await web_server._monitor_supervised_derived_job(
                    run_id=run_id,
                    state=state,
                    job_kind="pptx_export",
                    parent_run_id="source",
                    descriptor={"job_kind": "pptx_export"},
                    recovered_outcome=outcome,
                )

            self.client.portal.call(monitor)

        self.assertEqual(supervisor.calls, 2)
        self.assertEqual(store.read(run_id).state, "failed")
        self.assertEqual(state.error, primary_error)
        self.assertIsNotNone(state.result_message)
        assert state.result_message is not None
        self.assertEqual(state.result_message.text, primary_error)
        self.assertEqual(
            state.result_message.failure.agent_last_note,
            primary_error,
        )

    def test_worker_outcome_keeps_immutable_warnings_live_cold_and_by_default(
        self,
    ) -> None:
        warnings = (
            "first cleanup warning",
            "second cleanup warning",
        )
        for transport in ("live", "cold"):
            for ok in (True, False):
                with self.subTest(transport=transport, ok=ok):
                    run_id = f"outcome-warnings-{transport}-{'ok' if ok else 'error'}"
                    parent_run_id = f"outcome-parent-{transport}-{'ok' if ok else 'error'}"
                    _store, result, request = self._prepare_video_retry_completion(
                        run_id,
                        parent_run_id,
                        completing=transport == "cold",
                    )
                    self._write_video_retry_worker_result(
                        self.runs_dir / run_id,
                        run_id,
                        warnings=warnings,
                        result=result if ok else None,
                    )

                    if transport == "cold":
                        outcome = web_server._recovered_worker_outcome(
                            run_id,
                            "video_export_retry",
                        )
                    else:
                        class _FinishedProcess:
                            async def wait(self) -> int:
                                return 0 if ok else 1

                        async def completed(value):
                            return value

                        async def monitor():
                            return await web_server._web_run_runtime().supervisor._monitor(
                                request,
                                _FinishedProcess(),
                                stdout_task=asyncio.create_task(completed(None)),
                                stderr_task=asyncio.create_task(completed(None)),
                                relay_task=asyncio.create_task(completed(0)),
                            )

                        outcome = self.client.portal.call(monitor)

                    outcome_warnings = getattr(
                        outcome,
                        "pointer_cleanup_warnings",
                        None,
                    )
                    self.assertEqual(
                        {
                            "outcome_exists": outcome is not None,
                            "ok": getattr(outcome, "ok", None),
                            "warnings_type": type(outcome_warnings),
                            "warnings": outcome_warnings,
                        },
                        {
                            "outcome_exists": True,
                            "ok": ok,
                            "warnings_type": tuple,
                            "warnings": warnings,
                        },
                    )

        ordinary = web_server.WorkerOutcome(
            run_id="ordinary-worker-outcome",
            job_kind="pptx_export",
            returncode=0,
            ok=True,
            result={"run_id": "ordinary-worker-outcome"},
            error=None,
            relayed_events=0,
        )
        ordinary_warnings = getattr(
            ordinary,
            "pointer_cleanup_warnings",
            None,
        )
        self.assertEqual(
            (type(ordinary_warnings), ordinary_warnings),
            (tuple, ()),
        )

    def test_web_exports_the_canonical_supervisor_worker_outcome(self) -> None:
        self.assertIs(
            web_server.WorkerOutcome,
            run_supervisor_module.WorkerOutcome,
        )

    def test_web_runtime_does_not_replace_the_supervisor_monitor(self) -> None:
        runs_dir = self.out_dir / "monitor-identity-runs"
        runs_dir.mkdir(parents=True)
        store = RunControlStore(runs_dir)
        supervisor = RunSupervisor(runs_dir, control_store=store)
        monitor_overridden_before = "_monitor" in supervisor.__dict__

        web_server._WebRunRuntime(
            runs_dir=runs_dir.resolve(),
            control_store=store,
            supervisor=supervisor,
            services=WebRunServices(
                runs_dir,
                control_store=store,
                supervisor=supervisor,
            ),
        )

        self.assertEqual(
            {
                "monitor_overridden_before": monitor_overridden_before,
                "monitor_overridden_after": "_monitor" in supervisor.__dict__,
            },
            {
                "monitor_overridden_before": False,
                "monitor_overridden_after": False,
            },
        )

    def test_live_worker_outcome_uses_one_configured_result_snapshot(self) -> None:
        configured_runs_dir = self.out_dir / "configured-live-runs"
        global_runs_dir = self.out_dir / "module-global-decoy-runs"
        configured_runs_dir.mkdir(parents=True)
        global_runs_dir.mkdir(parents=True)
        warning_sets = {
            "A": ("A cleanup warning one", "A cleanup warning two"),
            "B": ("B replacement warning",),
            "C": ("C module-global decoy warning",),
        }

        def envelope(run_id: str, *, ok: bool, variant: str) -> dict[str, object]:
            warnings = warning_sets[variant]
            if ok:
                return {
                    "job_kind": "video_export_retry",
                    "run_id": run_id,
                    "ok": True,
                    "result": {
                        "run_id": run_id,
                        "ok": True,
                        "phase": "done",
                        "project_dir": f"/snapshot-{variant.lower()}/project",
                        "manifest_path": f"/snapshot-{variant.lower()}/manifest.json",
                        "mp4_path": f"/snapshot-{variant.lower()}/video.mp4",
                        "media_probe_path": f"/snapshot-{variant.lower()}/probe.json",
                        "render_started_at": "2026-08-05T12:00:00+00:00",
                        "pointer_cleanup_warnings": list(warnings),
                    },
                }
            return {
                "job_kind": "video_export_retry",
                "run_id": run_id,
                "ok": False,
                "error": {
                    "type": "VideoExportRetryError",
                    "message": f"failure {variant}",
                    "phase": f"phase-{variant.lower()}",
                    "pointer_cleanup_warnings": list(warnings),
                },
            }

        for ok in (True, False):
            with self.subTest(ok=ok):
                run_id = f"single-live-snapshot-{'success' if ok else 'failure'}"
                parent_run_id = f"single-live-snapshot-parent-{'success' if ok else 'failure'}"
                configured_run_dir = configured_runs_dir / run_id
                global_run_dir = global_runs_dir / run_id
                configured_run_dir.mkdir(parents=True)
                global_run_dir.mkdir(parents=True)
                envelope_a = envelope(run_id, ok=ok, variant="A")
                envelope_b = envelope(run_id, ok=ok, variant="B")
                envelope_c = envelope(run_id, ok=ok, variant="C")
                result_path = configured_run_dir / "worker_result.json"
                result_path.write_text(json.dumps(envelope_a), encoding="utf-8")
                (global_run_dir / "worker_result.json").write_text(
                    json.dumps(envelope_c),
                    encoding="utf-8",
                )

                store = RunControlStore(configured_runs_dir)
                record = store.reserve(
                    run_id,
                    "video",
                    parent_job_id=parent_run_id,
                )
                record = store.transition(run_id, record, "queued")
                store.transition(run_id, record, "running")
                supervisor = RunSupervisor(
                    configured_runs_dir,
                    control_store=store,
                )
                request = VideoExportRetryWorkerRequest(
                    job_kind="video_export_retry",
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                    source_project=str(configured_runs_dir / parent_run_id / "project"),
                    conversation_id="single-live-snapshot-conversation",
                    baseline_artifact_json="{}",
                    runs_dir=str(configured_runs_dir),
                )

                with patch.object(web_server, "RUNS_DIR", global_runs_dir):
                    web_server._WebRunRuntime(
                        runs_dir=configured_runs_dir.resolve(),
                        control_store=store,
                        supervisor=supervisor,
                        services=WebRunServices(
                            configured_runs_dir,
                            control_store=store,
                            supervisor=supervisor,
                        ),
                    )
                    real_parse = run_supervisor_module.parse_worker_result_json
                    first_parse_finished = False

                    def parse_then_replace(raw: str):
                        nonlocal first_parse_finished
                        parsed = real_parse(raw)
                        if not first_parse_finished:
                            first_parse_finished = True
                            result_path.write_text(
                                json.dumps(envelope_b),
                                encoding="utf-8",
                            )
                        return parsed

                    class _FinishedProcess:
                        async def wait(self) -> int:
                            return 0 if ok else 1

                    async def completed(value):
                        return value

                    async def monitor():
                        return await supervisor._monitor(
                            request,
                            _FinishedProcess(),
                            stdout_task=asyncio.create_task(completed(None)),
                            stderr_task=asyncio.create_task(completed(None)),
                            relay_task=asyncio.create_task(completed(0)),
                        )

                    with patch.object(
                        run_supervisor_module,
                        "parse_worker_result_json",
                        side_effect=parse_then_replace,
                    ):
                        outcome = self.client.portal.call(monitor)

                outcome_result = outcome.result if isinstance(outcome.result, dict) else {}
                outcome_warnings = getattr(
                    outcome,
                    "pointer_cleanup_warnings",
                    None,
                )
                configured_after = json.loads(result_path.read_text(encoding="utf-8"))
                global_after = json.loads(
                    (global_run_dir / "worker_result.json").read_text(encoding="utf-8")
                )
                configured_value = (
                    configured_after["result"]
                    if configured_after["ok"]
                    else configured_after["error"]
                )
                global_value = (
                    global_after["result"]
                    if global_after["ok"]
                    else global_after["error"]
                )
                self.assertEqual(
                    {
                        "ok": outcome.ok,
                        "result_project_dir": outcome_result.get("project_dir"),
                        "result_warnings": (
                            tuple(outcome_result.get("pointer_cleanup_warnings", ()))
                            if outcome_result
                            else None
                        ),
                        "error": outcome.error,
                        "failure_phase": outcome.failure_phase,
                        "warnings_type": type(outcome_warnings),
                        "warnings": outcome_warnings,
                        "configured_disk_warnings": tuple(
                            configured_value["pointer_cleanup_warnings"]
                        ),
                        "global_disk_warnings": tuple(
                            global_value["pointer_cleanup_warnings"]
                        ),
                    },
                    {
                        "ok": ok,
                        "result_project_dir": "/snapshot-a/project" if ok else None,
                        "result_warnings": warning_sets["A"] if ok else None,
                        "error": (
                            None
                            if ok
                            else (
                                "failure A\nPointer cleanup warnings: "
                                "A cleanup warning one | A cleanup warning two"
                            )
                        ),
                        "failure_phase": None if ok else "phase-a",
                        "warnings_type": tuple,
                        "warnings": warning_sets["A"],
                        "configured_disk_warnings": warning_sets["B"],
                        "global_disk_warnings": warning_sets["C"],
                    },
                )

    def test_failed_derived_event_follows_accepted_cas_without_reformatting(
        self,
    ) -> None:
        run_id = "derived-warning-failed-cas"
        parent_run_id = "derived-warning-failed-parent"
        store, _result, _request = self._prepare_video_retry_completion(
            run_id,
            parent_run_id,
            completing=True,
        )
        warnings = (
            "invalidation warning",
            "publication warning",
        )
        suffix = "\nPointer cleanup warnings: " + " | ".join(warnings)
        error = "x" * (2000 - len(suffix)) + suffix
        outcome = self._worker_outcome_with_warnings(
            run_id=run_id,
            ok=False,
            error=error,
            warnings=warnings,
        )
        state = web_server._RunState(
            artifact_type="video",
            conversation_id="derived-warning-conversation",
        )
        order: list[str] = []
        events: list[tuple[tuple[object, ...], dict[str, object]]] = []

        class _AcceptingSupervisor:
            async def accept_completion(
                inner_self,
                accepted_run_id: str,
                *,
                terminal_state: str,
                publishable: bool,
                **_kwargs: object,
            ):
                order.append("cas")
                current = store.read(accepted_run_id)
                return store.transition(
                    accepted_run_id,
                    current,
                    terminal_state,
                    publishable=publishable,
                )

        runtime = SimpleNamespace(
            control_store=store,
            supervisor=_AcceptingSupervisor(),
        )

        def record_event(*args, **kwargs) -> None:
            order.append("event")
            events.append((args, kwargs))

        with (
            patch.object(web_server, "_web_run_runtime", return_value=runtime),
            patch.object(web_server, "_append_event", side_effect=record_event),
        ):
            async def monitor() -> None:
                await web_server._monitor_supervised_derived_job(
                    run_id=run_id,
                    state=state,
                    job_kind="video_export_retry",
                    parent_run_id=parent_run_id,
                    descriptor={"job_kind": "video_export_retry"},
                    recovered_outcome=outcome,
                )

            self.client.portal.call(monitor)

        message = state.result_message
        failure = getattr(message, "failure", None)
        failure_dump = failure.model_dump() if failure is not None else {}
        event_name = events[0][0][2] if events and len(events[0][0]) >= 3 else None
        event_data = events[0][1].get("data", {}) if events else {}
        event_failure = (
            event_data.get("failure", {})
            if isinstance(event_data, dict)
            else {}
        )
        record = store.read(run_id)
        with self.subTest(race="failed-cas-accepted"):
            self.assertEqual(
                {
                    "control_state": record.state,
                    "publishable": record.publishable,
                    "state_error": state.error,
                    "message_text": getattr(message, "text", None),
                    "message_length": len(getattr(message, "text", "")),
                    "value_error_wrapper": "ValueError:" in str(state.error),
                    "warning_occurrences": tuple(
                        str(getattr(message, "text", "")).count(warning)
                        for warning in warnings
                    ),
                    "failure_note": failure_dump.get("agent_last_note"),
                    "failure_warnings": tuple(
                        failure_dump.get("pointer_cleanup_warnings") or ()
                    ),
                    "artifact": state.result_artifact,
                    "event_count": len(events),
                    "event_name": event_name,
                    "event_status": (
                        event_data.get("status")
                        if isinstance(event_data, dict)
                        else None
                    ),
                    "event_warnings": tuple(
                        event_failure.get("pointer_cleanup_warnings") or ()
                    ),
                    "order": tuple(order),
                },
                {
                    "control_state": "failed",
                    "publishable": False,
                    "state_error": error,
                    "message_text": error,
                    "message_length": 2000,
                    "value_error_wrapper": False,
                    "warning_occurrences": (1, 1),
                    "failure_note": error,
                    "failure_warnings": warnings,
                    "artifact": None,
                    "event_count": 1,
                    "event_name": "artifact.generation_failed",
                    "event_status": "error",
                    "event_warnings": warnings,
                    "order": ("cas", "event"),
                },
            )

        with self.subTest(race="cancellation-wins-failed-cas"):
            race_run_id = "derived-warning-cancelled-cas"
            race_parent_id = "derived-warning-cancelled-parent"
            race_store, _result, _request = self._prepare_video_retry_completion(
                race_run_id,
                race_parent_id,
                completing=True,
            )
            race_state = web_server._RunState(
                artifact_type="video",
                conversation_id="derived-warning-conversation",
            )
            race_outcome = self._worker_outcome_with_warnings(
                run_id=race_run_id,
                ok=False,
                error="cancelled failure outcome",
                warnings=warnings,
            )
            race_events: list[str] = []
            race_cas: list[str] = []

            class _CancellationWinsSupervisor:
                async def accept_completion(
                    inner_self,
                    accepted_run_id: str,
                    **_kwargs: object,
                ):
                    race_cas.append(accepted_run_id)
                    return race_store.request_cancel(accepted_run_id)

            race_runtime = SimpleNamespace(
                control_store=race_store,
                supervisor=_CancellationWinsSupervisor(),
            )
            with (
                patch.object(
                    web_server,
                    "_web_run_runtime",
                    return_value=race_runtime,
                ),
                patch.object(
                    web_server,
                    "_append_event",
                    side_effect=lambda *_args, **_kwargs: race_events.append("event"),
                ),
            ):
                async def race_monitor() -> None:
                    await web_server._monitor_supervised_derived_job(
                        run_id=race_run_id,
                        state=race_state,
                        job_kind="video_export_retry",
                        parent_run_id=race_parent_id,
                        descriptor={"job_kind": "video_export_retry"},
                        recovered_outcome=race_outcome,
                    )

                self.client.portal.call(race_monitor)

            before_finalize = race_store.read(race_run_id)
            self._finalize_test_cancellation(race_store, race_run_id)
            cancelled = race_store.read(race_run_id)
            self.assertEqual(
                {
                    "cas_calls": tuple(race_cas),
                    "state_before_finalize": before_finalize.state,
                    "events": tuple(race_events),
                    "artifact": race_state.result_artifact,
                    "final_state": cancelled.state,
                    "publishable": cancelled.publishable,
                },
                {
                    "cas_calls": (race_run_id,),
                    "state_before_finalize": "cancelling",
                    "events": (),
                    "artifact": None,
                    "final_state": "cancelled",
                    "publishable": False,
                },
            )

    def test_cancelling_outcome_creates_or_enriches_provisional_warning_failure(
        self,
    ) -> None:
        warnings = ("warning captured before cancellation",)
        for control_state in ("cancelling", "cancelled"):
            for initial_message in ("missing", "existing"):
                run_id = f"provisional-{control_state}-{initial_message}"
                parent_run_id = f"provisional-parent-{initial_message}"
                store, _result, _request = self._prepare_video_retry_completion(
                    run_id,
                    parent_run_id,
                    completing=True,
                )
                store.request_cancel(run_id)
                if control_state == "cancelled":
                    self._finalize_test_cancellation(store, run_id)
                control_before_monitor = store.read(run_id)
                state = web_server._RunState(
                    artifact_type="video",
                    conversation_id="provisional-warning-conversation",
                )
                if initial_message == "existing":
                    state.result_message = web_server.Message(
                        id=f"msg_{run_id}",
                        role="assistant",
                        text="Run cancelled.",
                        ts=0,
                        run_id=run_id,
                        status="error",
                        failure=web_server.Failure(status="cancelled"),
                    )
                web_server._RUNS[run_id] = state
                outcome = self._worker_outcome_with_warnings(
                    run_id=run_id,
                    ok=False,
                    error="worker observed cancellation",
                    warnings=warnings,
                )
                cas_calls: list[str] = []
                events: list[str] = []

                class _NoCompletionSupervisor:
                    async def accept_completion(
                        inner_self,
                        accepted_run_id: str,
                        **_kwargs: object,
                    ):
                        cas_calls.append(accepted_run_id)
                        return store.read(accepted_run_id)

                runtime = SimpleNamespace(
                    control_store=store,
                    supervisor=_NoCompletionSupervisor(),
                )
                with (
                    patch.object(
                        web_server,
                        "_web_run_runtime",
                        return_value=runtime,
                    ),
                    patch.object(
                        web_server,
                        "_append_event",
                        side_effect=lambda *_args, **_kwargs: events.append("event"),
                    ),
                ):
                    async def monitor_and_quiesce():
                        await web_server._monitor_supervised_derived_job(
                            run_id=run_id,
                            state=state,
                            job_kind="video_export_retry",
                            parent_run_id=parent_run_id,
                            descriptor={"job_kind": "video_export_retry"},
                            recovered_outcome=outcome,
                        )
                        return await web_server._quiesce_web_completion_monitor(
                            run_id
                        )

                    quiesced = self.client.portal.call(monitor_and_quiesce)

                control_after_monitor = store.read(run_id)
                message = state.result_message
                failure = getattr(message, "failure", None)
                failure_dump = failure.model_dump() if failure is not None else {}
                if control_state == "cancelling":
                    self._finalize_test_cancellation(store, run_id)
                cancelled = store.read(run_id)
                actual = {
                    "message_created": message is not None,
                    "message_status": getattr(message, "status", None),
                    "failure_status": failure_dump.get("status"),
                    "failure_warnings": tuple(
                        failure_dump.get("pointer_cleanup_warnings") or ()
                    ),
                    "cas_calls": tuple(cas_calls),
                    "events": tuple(events),
                    "control_before_monitor": control_before_monitor.state,
                    "publishable_before_monitor": (
                        control_before_monitor.publishable
                    ),
                    "frozen_before_monitor": control_before_monitor.writes_frozen,
                    "control_after_monitor": control_after_monitor.state,
                    "publishable_after_monitor": (
                        control_after_monitor.publishable
                    ),
                    "frozen_after_monitor": control_after_monitor.writes_frozen,
                    "quiesced": quiesced,
                    "final_control": cancelled.state,
                    "publishable": cancelled.publishable,
                    "writes_frozen": cancelled.writes_frozen,
                }
                expected = {
                    "message_created": True,
                    "message_status": "error",
                    "failure_status": "cancelled",
                    "failure_warnings": warnings,
                    "cas_calls": (),
                    "events": (),
                    "control_before_monitor": control_state,
                    "publishable_before_monitor": False,
                    "frozen_before_monitor": control_state == "cancelled",
                    "control_after_monitor": control_state,
                    "publishable_after_monitor": False,
                    "frozen_after_monitor": control_state == "cancelled",
                    "quiesced": True,
                    "final_control": "cancelled",
                    "publishable": False,
                    "writes_frozen": True,
                }
                with self.subTest(
                    control_state=control_state,
                    initial_message=initial_message,
                ):
                    self.assertEqual(actual, expected)

    def test_cancelled_artifact_fallback_strictly_decodes_warning_payloads(
        self,
    ) -> None:
        trusted_warning = "strict fallback warning"
        cases = (
            ("live", "valid", "valid", "cancelling", (trusted_warning,)),
            ("cold", "valid", "valid", "cancelled", (trusted_warning,)),
            ("cold", "invalid", "valid", "cancelled", ()),
            ("cold", "duplicate", "valid", "cancelled", ()),
            ("cold", "mismatched", "valid", "cancelled", ()),
            ("live", "valid", "missing", "cancelled", ()),
            ("live", "valid", "malformed", "cancelled", ()),
            ("live", "valid", "mismatched-kind", "cancelled", ()),
            ("live", "valid", "mismatched-run", "cancelled", ()),
            ("cold", "valid", "missing", "cancelled", ()),
            ("cold", "valid", "malformed", "cancelled", ()),
            ("cold", "valid", "mismatched-kind", "cancelled", ()),
            ("cold", "valid", "mismatched-run", "cancelled", ()),
        )
        for (
            transport,
            payload_case,
            descriptor_case,
            control_state,
            expected_warnings,
        ) in cases:
            with self.subTest(
                transport=transport,
                payload_case=payload_case,
                descriptor_case=descriptor_case,
                control_state=control_state,
            ):
                run_id = (
                    f"artifact-fallback-{transport}-{payload_case}-"
                    f"{descriptor_case}"
                )
                parent_run_id = f"artifact-parent-{transport}-{payload_case}"
                store, _result, _request = self._prepare_video_retry_completion(
                    run_id,
                    parent_run_id,
                    completing=True,
                )
                run_dir = self.runs_dir / run_id
                valid_envelope = {
                    "job_kind": "video_export_retry",
                    "run_id": run_id,
                    "ok": False,
                    "error": {
                        "type": "RunCancelled",
                        "message": "worker cancelled",
                        "phase": "final_pointer",
                        "pointer_cleanup_warnings": [trusted_warning],
                    },
                }
                if payload_case == "valid":
                    raw = json.dumps(valid_envelope)
                elif payload_case == "invalid":
                    invalid = json.loads(json.dumps(valid_envelope))
                    invalid["error"]["pointer_cleanup_warnings"].append(7)
                    raw = json.dumps(invalid)
                elif payload_case == "duplicate":
                    raw = json.dumps(valid_envelope)
                    needle = (
                        '"pointer_cleanup_warnings": '
                        f'["{trusted_warning}"]'
                    )
                    raw = raw.replace(
                        needle,
                        needle + ', "pointer_cleanup_warnings": ["untrusted"]',
                    )
                elif payload_case == "mismatched":
                    mismatched = json.loads(json.dumps(valid_envelope))
                    mismatched["job_kind"] = "pptx_export"
                    raw = json.dumps(mismatched)
                else:
                    raise AssertionError(f"unknown payload case: {payload_case}")
                (run_dir / "worker_result.json").write_text(
                    raw,
                    encoding="utf-8",
                )
                self._set_derived_descriptor_case(run_dir, descriptor_case)
                store.request_cancel(run_id)
                if control_state == "cancelled":
                    self._finalize_test_cancellation(store, run_id)
                if transport == "cold":
                    web_server._RUNS.pop(run_id, None)
                else:
                    state = web_server._RunState(
                        artifact_type="video",
                        conversation_id="artifact-warning-conversation",
                    )
                    state.result_message = web_server.Message(
                        id=f"msg_{run_id}",
                        role="assistant",
                        text="Run cancelled.",
                        ts=0,
                        run_id=run_id,
                        status="error",
                        failure=web_server.Failure(status="cancelled"),
                    )
                    web_server._RUNS[run_id] = state

                response = self.client.get(f"/api/runs/{run_id}/artifact")
                record_after_request = store.read(run_id)
                body = response.json()
                message_data = body.get("message", {}) if isinstance(body, dict) else {}
                failure_data = (
                    message_data.get("failure", {})
                    if isinstance(message_data, dict)
                    else {}
                )
                artifact = body.get("artifact") if isinstance(body, dict) else None
                if control_state == "cancelling":
                    self._finalize_test_cancellation(store, run_id)
                final_record = store.read(run_id)
                self.assertEqual(
                    {
                        "status_code": response.status_code,
                        "failure_status": (
                            failure_data.get("status")
                            if isinstance(failure_data, dict)
                            else None
                        ),
                        "warnings": tuple(
                            failure_data.get("pointer_cleanup_warnings") or ()
                        ) if isinstance(failure_data, dict) else (),
                        "artifact": artifact,
                        "state_after_request": record_after_request.state,
                        "publishable_after_request": (
                            record_after_request.publishable
                        ),
                        "final_state": final_record.state,
                        "final_publishable": final_record.publishable,
                    },
                    {
                        "status_code": 200,
                        "failure_status": "cancelled",
                        "warnings": expected_warnings,
                        "artifact": None,
                        "state_after_request": control_state,
                        "publishable_after_request": False,
                        "final_state": "cancelled",
                        "final_publishable": False,
                    },
                )

    def test_failure_and_cancelled_history_keep_only_validated_warnings(
        self,
    ) -> None:
        warnings = ("history cleanup warning",)
        for warning_case, warning_payload, expected_warnings in (
            ("valid", ["history cleanup warning"], warnings),
            ("malformed-container", "not-a-warning-list", ()),
            ("malformed-element", ["history cleanup warning", 7], ()),
        ):
            with self.subTest(
                source="artifact.generation_failed",
                warning_case=warning_case,
            ):
                conversation = web_server._conversation_from_design_events(
                    "history-warning-conversation",
                    [{
                        "event": "artifact.generation_failed",
                        "conversation_id": "history-warning-conversation",
                        "run_id": "history-warning-event-run",
                        "_ts_ms": 1,
                        "data": {
                            "status": "error",
                            "failure": {
                                "status": "error",
                                "phase": "final_pointer",
                                "pointer_cleanup_warnings": warning_payload,
                            },
                        },
                    }],
                    set(),
                )
                message = (
                    conversation.get("messages", [{}])[0]
                    if isinstance(conversation, dict)
                    else {}
                )
                failure = (
                    message.get("failure", {})
                    if isinstance(message, dict)
                    else {}
                )
                self.assertEqual(
                    tuple(failure.get("pointer_cleanup_warnings") or ()),
                    expected_warnings,
                )

        for payload_case, descriptor_case, expected_warnings in (
            ("valid", "valid", warnings),
            ("mismatched", "valid", ()),
            ("valid", "missing", ()),
            ("valid", "malformed", ()),
            ("valid", "mismatched-kind", ()),
            ("valid", "mismatched-run", ()),
        ):
            with self.subTest(
                source="run.cancelled",
                payload_case=payload_case,
                descriptor_case=descriptor_case,
            ):
                run_id = f"history-{payload_case}-{descriptor_case}"
                parent_run_id = (
                    f"history-parent-{payload_case}-{descriptor_case}"
                )
                store, _result, _request = self._prepare_video_retry_completion(
                    run_id,
                    parent_run_id,
                    completing=True,
                )
                envelope = {
                    "job_kind": "video_export_retry",
                    "run_id": run_id,
                    "ok": False,
                    "error": {
                        "type": "RunCancelled",
                        "message": "worker cancelled",
                        "phase": "final_pointer",
                        "pointer_cleanup_warnings": list(warnings),
                    },
                }
                if payload_case == "mismatched":
                    envelope["run_id"] = "history-cancelled-other-run"
                (self.runs_dir / run_id / "worker_result.json").write_text(
                    json.dumps(envelope),
                    encoding="utf-8",
                )
                self._set_derived_descriptor_case(
                    self.runs_dir / run_id,
                    descriptor_case,
                )
                store.request_cancel(run_id)
                self._finalize_test_cancellation(store, run_id)

                conversation = web_server._conversation_from_disk_run(run_id)
                message = (
                    conversation.get("messages", [{}])[0]
                    if isinstance(conversation, dict)
                    else {}
                )
                failure = (
                    message.get("failure", {})
                    if isinstance(message, dict)
                    else {}
                )
                record = store.read(run_id)
                self.assertEqual(
                    {
                        "failure_status": failure.get("status"),
                        "warnings": tuple(
                            failure.get("pointer_cleanup_warnings") or ()
                        ),
                        "control_state": record.state,
                        "publishable": record.publishable,
                    },
                    {
                        "failure_status": "cancelled",
                        "warnings": expected_warnings,
                        "control_state": "cancelled",
                        "publishable": False,
                    },
                )


if __name__ == "__main__":
    unittest.main()
