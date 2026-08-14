from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import asyncio
import base64
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from autodesign.attempt_candidates import capture_attempt_candidate
from autodesign.config import Settings
from autodesign.process_supervision import ProcessLedger
from autodesign.run_control import RunControlStore
from autodesign.run_supervisor import RunSupervisor, WorkerExitDiagnostic
from autodesign.web_run_services import WebRunServices
import scripts.web_server as web_server


class WebRunCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmp.name) / "out"
        self.runs_dir = self.out_dir / "runs"
        self.runs_dir.mkdir(parents=True)
        self.store = RunControlStore(self.runs_dir)
        self._runs_patch = patch.object(web_server, "RUNS_DIR", self.runs_dir)
        self._runs_patch.start()

    def tearDown(self) -> None:
        self._runs_patch.stop()
        self._tmp.cleanup()

    def _write_final(self, run_id: str, content: str = "partial") -> Path:
        path = self.runs_dir / run_id / "final" / "index.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _write_attempt_snapshot(self, run_id: str):
        attempt_dir = self.runs_dir / run_id / "landing_author" / "attempt_01"
        assets_dir = attempt_dir / "assets"
        assets_dir.mkdir(parents=True)
        (attempt_dir / "index.html").write_text(
            '<html><body><img src="assets/figure.png"></body></html>',
            encoding="utf-8",
        )
        (assets_dir / "figure.png").write_bytes(b"image")
        (attempt_dir / "preview.png").write_bytes(b"preview")
        (attempt_dir / "validation.json").write_text(
            '{"status":"blocked"}',
            encoding="utf-8",
        )
        return capture_attempt_candidate(
            run_dir=attempt_dir.parents[1],
            attempt_dir=attempt_dir,
            artifact_type="landing",
            attempt=1,
            max_attempts=4,
            source_path="index.html",
            dependency_paths=["assets/figure.png"],
            preview_paths=["preview.png"],
            validation_summary_path="validation.json",
            safety_state="blocked",
            hard_blockers=[],
            warnings=[],
            browser_resource_paths=["assets/figure.png"],
        )

    def _cancel(self, run_id: str) -> None:
        self.store.reserve(run_id, "landing")
        self.store.request_cancel(run_id)
        self.store.finalize_cancel(
            run_id,
            {"termination_verified": True, "reason": "test"},
        )

    def test_cancelled_run_static_file_path_is_not_accessible(self) -> None:
        run_id = "cancelled-static"
        self._write_final(run_id)
        self._cancel(run_id)

        with self.assertRaises(HTTPException) as raised:
            web_server._resolve_public_run_file(f"{run_id}/final/index.html")

        self.assertIn(raised.exception.status_code, {404, 409, 410, 423})

    def test_cancelled_run_artifact_endpoint_never_publishes_partial_files(self) -> None:
        run_id = "cancelled-artifact"
        self._write_final(run_id)
        self._cancel(run_id)

        with self.assertRaises(HTTPException):
            web_server._assert_controlled_run_publishable(run_id)

    def test_legacy_run_without_control_remains_readable(self) -> None:
        run_id = "legacy-readable"
        expected = self._write_final(run_id, "legacy")

        resolved = web_server._resolve_public_run_file(f"{run_id}/final/index.html")

        self.assertEqual(resolved, expected.resolve())

    def test_controlled_run_is_readable_only_after_publishable_completion(self) -> None:
        run_id = "controlled-complete"
        expected = self._write_final(run_id, "complete")
        record = self.store.reserve(run_id, "landing")
        record = self.store.transition(run_id, record, "queued")
        record = self.store.transition(run_id, record, "running")
        record = self.store.transition(run_id, record, "completing")
        self.store.transition(run_id, record, "completed", publishable=True)

        resolved = web_server._resolve_public_run_file(f"{run_id}/final/index.html")

        self.assertEqual(resolved, expected.resolve())

    def test_running_run_serves_only_immutable_attempt_snapshot_members(self) -> None:
        run_id = "running-attempt-snapshot"
        candidate = self._write_attempt_snapshot(run_id)
        self._write_final(run_id, "partial final")
        record = self.store.reserve(run_id, "landing")
        record = self.store.transition(run_id, record, "queued")
        self.store.transition(run_id, record, "running")

        allowed = [
            candidate.source_relative_path,
            *candidate.preview_relative_paths,
            *candidate.browser_resource_relative_paths,
        ]
        for relative in allowed:
            with self.subTest(relative=relative):
                resolved = web_server._resolve_public_run_file(
                    f"{run_id}/{relative}"
                )
                self.assertTrue(resolved.is_file())

        with self.assertRaises(HTTPException) as final_raised:
            web_server._resolve_public_run_file(f"{run_id}/final/index.html")
        self.assertEqual(final_raised.exception.status_code, 409)

        with self.assertRaises(HTTPException) as diagnostics_raised:
            web_server._resolve_public_run_file(
                f"{run_id}/{candidate.validation_summary_relative_path}"
            )
        self.assertEqual(diagnostics_raised.exception.status_code, 409)

        self.store.request_cancel(run_id)
        self.store.finalize_cancel(
            run_id,
            {"termination_verified": True, "reason": "test"},
        )
        with self.assertRaises(HTTPException) as cancelled_raised:
            web_server._resolve_public_run_file(
                f"{run_id}/{candidate.source_relative_path}"
            )
        self.assertEqual(cancelled_raised.exception.status_code, 410)

    def test_completed_run_internal_diagnostics_are_not_public_files(self) -> None:
        run_id = "controlled-internal-files"
        record = self.store.reserve(run_id, "landing")
        record = self.store.transition(run_id, record, "queued")
        record = self.store.transition(run_id, record, "running")
        record = self.store.transition(run_id, record, "completing")
        self.store.transition(run_id, record, "completed", publishable=True)
        run_dir = self.runs_dir / run_id
        internal_names = (
            "run_control.json",
            "process_ledger.json",
            "worker_stdout.log",
            "worker_stderr.log",
            "worker_result.json",
            "worker_events.jsonl",
            "run_events.jsonl",
            "cancel_snapshot.json",
            ".spawn.lock",
            ".poster-final-promotion.json",
            "upload.partial",
            "staged.tmp",
        )
        for name in internal_names:
            path = run_dir / name
            if not path.exists():
                path.write_text("private diagnostic", encoding="utf-8")

        for name in internal_names:
            with self.subTest(name=name):
                with self.assertRaises(HTTPException) as raised:
                    web_server._resolve_public_run_file(f"{run_id}/{name}")
                self.assertEqual(raised.exception.status_code, 404)

    def test_completed_run_derived_job_descriptor_is_not_public(self) -> None:
        run_id = "controlled-derived-descriptor"
        record = self.store.reserve(run_id, "landing")
        record = self.store.transition(run_id, record, "queued")
        record = self.store.transition(run_id, record, "running")
        record = self.store.transition(run_id, record, "completing")
        self.store.transition(run_id, record, "completed", publishable=True)
        descriptor = self.runs_dir / run_id / "derived_job.json"
        descriptor.write_text(
            json.dumps({"baseline_artifact_json": "/Users/alice/private.json"}),
            encoding="utf-8",
        )

        with self.assertRaises(HTTPException) as raised:
            web_server._resolve_public_run_file(f"{run_id}/derived_job.json")

        self.assertEqual(raised.exception.status_code, 404)

    def test_completed_run_rejects_same_run_symlink_to_internal_file(self) -> None:
        run_id = "controlled-internal-symlink"
        record = self.store.reserve(run_id, "landing")
        record = self.store.transition(run_id, record, "queued")
        record = self.store.transition(run_id, record, "running")
        record = self.store.transition(run_id, record, "completing")
        self.store.transition(run_id, record, "completed", publishable=True)
        run_dir = self.runs_dir / run_id
        private_log = run_dir / "worker_stderr.log"
        private_log.write_text("private diagnostic", encoding="utf-8")
        alias = run_dir / "final" / "preview.txt"
        alias.parent.mkdir(parents=True)
        alias.symlink_to(private_log)

        with self.assertRaises(HTTPException) as raised:
            web_server._resolve_public_run_file(f"{run_id}/final/preview.txt")

        self.assertEqual(raised.exception.status_code, 404)

    def test_completed_run_rejects_symlink_into_another_run(self) -> None:
        public_run_id = "controlled-cross-run-symlink"
        private_run_id = "other-private-run"
        record = self.store.reserve(public_run_id, "landing")
        record = self.store.transition(public_run_id, record, "queued")
        record = self.store.transition(public_run_id, record, "running")
        record = self.store.transition(public_run_id, record, "completing")
        self.store.transition(public_run_id, record, "completed", publishable=True)
        private_snapshot = self.runs_dir / private_run_id / "cancel_snapshot.json"
        private_snapshot.parent.mkdir(parents=True)
        private_snapshot.write_text("private snapshot", encoding="utf-8")
        alias = self.runs_dir / public_run_id / "final" / "preview.json"
        alias.parent.mkdir(parents=True)
        alias.symlink_to(private_snapshot)

        with self.assertRaises(HTTPException) as raised:
            web_server._resolve_public_run_file(
                f"{public_run_id}/final/preview.json"
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_completed_run_rejects_parent_traversal_outside_requested_run(self) -> None:
        public_run_id = "controlled-traversal"
        private_run_id = "traversal-target"
        record = self.store.reserve(public_run_id, "landing")
        record = self.store.transition(public_run_id, record, "queued")
        record = self.store.transition(public_run_id, record, "running")
        record = self.store.transition(public_run_id, record, "completing")
        self.store.transition(public_run_id, record, "completed", publishable=True)
        private_file = self.runs_dir / private_run_id / "final" / "index.html"
        private_file.parent.mkdir(parents=True)
        private_file.write_text("private artifact", encoding="utf-8")

        with self.assertRaises(HTTPException) as raised:
            web_server._resolve_public_run_file(
                f"{public_run_id}/../{private_run_id}/final/index.html"
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_run_file_route_is_not_bypassed_by_static_mount(self) -> None:
        mounts = [
            route
            for route in web_server.app.routes
            if route.__class__.__name__ == "Mount"
            and getattr(route, "path", "") == "/api/files/runs"
        ]
        self.assertEqual(mounts, [])

    def test_cancelled_controlled_source_is_never_usable(self) -> None:
        run_id = "cancelled-source"
        self._write_final(run_id)
        self._cancel(run_id)

        for mode in ("artifact", "mutation", "snapshot"):
            with self.subTest(mode=mode), self.assertRaises(HTTPException) as raised:
                web_server._assert_controlled_run_source_usable(run_id, mode=mode)
            self.assertEqual(raised.exception.status_code, 410)

    def test_controlled_source_modes_reject_nonpublishable_mutation(self) -> None:
        failed_id = "failed-source"
        failed = self.store.reserve(failed_id, "landing")
        failed = self.store.transition(failed_id, failed, "queued")
        failed = self.store.transition(failed_id, failed, "running")
        self.store.transition(failed_id, failed, "failed", publishable=False)
        with self.assertRaises(HTTPException):
            web_server._assert_controlled_run_source_usable(failed_id, mode="artifact")
        with self.assertRaises(HTTPException):
            web_server._assert_controlled_run_source_usable(failed_id, mode="mutation")
        web_server._assert_controlled_run_source_usable(failed_id, mode="snapshot")

        running_id = "running-source"
        running = self.store.reserve(running_id, "landing")
        running = self.store.transition(running_id, running, "queued")
        self.store.transition(running_id, running, "running")
        with self.assertRaises(HTTPException):
            web_server._assert_controlled_run_source_usable(running_id, mode="artifact")
        web_server._assert_controlled_run_source_usable(running_id, mode="mutation")
        web_server._assert_controlled_run_source_usable(running_id, mode="snapshot")

    def test_controlled_artifact_response_is_read_only_during_cancellation(self) -> None:
        cases = (
            ("deck", "deck.html", "slides_author_manifest.json"),
            ("landing", "index.html", "landing_author_manifest.json"),
        )
        for artifact_type, html_name, manifest_name in cases:
            with self.subTest(artifact_type=artifact_type):
                run_id = f"cancelling-{artifact_type}"
                run_dir = self.runs_dir / run_id
                final_dir = run_dir / "final"
                final_dir.mkdir(parents=True)
                html_path = final_dir / html_name
                manifest_path = final_dir / manifest_name
                html_path.write_text(
                    f"<html><body>{artifact_type}</body></html>",
                    encoding="utf-8",
                )
                manifest_path.write_text(
                    json.dumps({"html_sha256": "stale"}),
                    encoding="utf-8",
                )
                record = self.store.reserve(run_id, artifact_type)
                record = self.store.transition(run_id, record, "queued")
                self.store.transition(run_id, record, "running")
                self.store.request_cancel(run_id)
                before = {
                    path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in (html_path, manifest_path)
                }

                artifact = web_server._build_artifact_response(
                    run_dir,
                    run_id,
                    artifact_type,
                    baseline_artifact_json=None,
                )

                self.assertIsNotNone(artifact)
                for path, (expected_bytes, expected_mtime) in before.items():
                    self.assertEqual(path.read_bytes(), expected_bytes)
                    self.assertEqual(path.stat().st_mtime_ns, expected_mtime)


class WebRunProtocolApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmp.name) / "out"
        self.runs_dir = self.out_dir / "runs"
        self.runs_dir.mkdir(parents=True)
        self.settings = Settings(
            anthropic_api_key="endpoint-secret-71931",
            anthropic_base_url=None,
            gemini_api_key="",
            designer_model="designer",
            critic_model="critic",
            repo_root=Path(__file__).resolve().parents[1],
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
        self._client_context = TestClient(web_server.app)
        self.client = self._client_context.__enter__()

    def _use_fixture_supervisor(self) -> None:
        store = RunControlStore(self.runs_dir)
        supervisor = RunSupervisor(
            self.runs_dir,
            control_store=store,
            worker_command=(
                sys.executable,
                str(Path(__file__).resolve().parent / "fixtures" / "cancellation_worker.py"),
            ),
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

    def tearDown(self) -> None:
        self._client_context.__exit__(None, None, None)
        web_server._reset_web_run_runtime_for_tests()
        for current in reversed(self._patches):
            current.stop()
        self._tmp.cleanup()

    def _reserve(self, *, content: bytes = b"paper") -> dict[str, object]:
        digest = hashlib.sha256(content).hexdigest()
        response = self.client.post(
            "/api/runs/reserve",
            headers={"Idempotency-Key": "reserve-api-test"},
            json={
                "brief": json.dumps({"mode": "success"}),
                "artifact_type": "landing",
                "input_slots": [
                    {
                        "name": "attachment-0.pdf",
                        "role": "attachment",
                        "sha256": digest,
                        "size": len(content),
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _ready_landing_attempt(self, run_id: str):
        attempt_dir = self.runs_dir / run_id / "landing_author" / "attempt_01"
        attempt_dir.mkdir(parents=True)
        (attempt_dir / "index.html").write_text(
            "<!doctype html><html><body>ready</body></html>",
            encoding="utf-8",
        )
        (attempt_dir / "preview.png").write_bytes(b"preview")
        (attempt_dir / "validation.json").write_text(
            '{"accepted":true}',
            encoding="utf-8",
        )
        return capture_attempt_candidate(
            run_dir=self.runs_dir / run_id,
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

    def test_failed_run_attempt_selection_returns_structured_fallback_code(
        self,
    ) -> None:
        run_id = "failed-attempt-selection-race"
        candidate = self._ready_landing_attempt(run_id)
        store = RunControlStore(self.runs_dir)
        record = store.reserve(run_id, "landing", initial_state="queued")
        record = store.transition(run_id, record, "running")
        store.transition(run_id, record, "failed", publishable=False)

        response = self.client.post(
            f"/api/runs/{run_id}/attempts/1/select",
            json={
                "expected_candidate_sha256": candidate.source_sha256,
                "idempotency_key": "failed-race-selection",
            },
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            response.json()["detail"],
            {"code": "run_not_selectable"},
        )
        self.assertFalse(
            (self.runs_dir / run_id / "attempt_candidates" / "selection.json").exists()
        )

    def test_cancelled_run_attempt_selection_remains_fail_closed(self) -> None:
        run_id = "cancelled-attempt-selection-race"
        candidate = self._ready_landing_attempt(run_id)
        store = RunControlStore(self.runs_dir)
        record = store.reserve(run_id, "landing", initial_state="queued")
        record = store.transition(run_id, record, "running")
        store.request_cancel(run_id)
        store.finalize_cancel(
            run_id,
            {"termination_verified": True, "reason": "test"},
        )

        response = self.client.post(
            f"/api/runs/{run_id}/attempts/1/select",
            json={
                "expected_candidate_sha256": candidate.source_sha256,
                "idempotency_key": "cancelled-race-selection",
            },
        )

        self.assertEqual(response.status_code, 410, response.text)
        self.assertNotEqual(
            response.json()["detail"],
            {"code": "run_not_selectable"},
        )
        self.assertFalse(
            (self.runs_dir / run_id / "attempt_candidates" / "selection.json").exists()
        )

    def test_attempt_selection_rejects_invalid_run_id_before_source_access(
        self,
    ) -> None:
        run_id = "invalid:run-id"
        candidate = self._ready_landing_attempt(run_id)
        with patch.object(web_server, "request_attempt_selection") as selection:
            response = self.client.post(
                f"/api/runs/{run_id}/attempts/1/select",
                json={
                    "expected_candidate_sha256": candidate.source_sha256,
                    "idempotency_key": "unsafe-run-id",
                },
            )

        self.assertEqual(response.status_code, 404, response.text)
        selection.assert_not_called()

    def test_generate_reserves_run_before_upload(self) -> None:
        payload = self._reserve()
        run_id = str(payload["run_id"])
        record = RunControlStore(self.runs_dir).read(run_id)
        self.assertEqual(record.state, "reserved")
        self.assertTrue(payload["upload_token"])
        self.assertEqual(payload["input_slots"][0]["name"], "attachment-0.pdf")
        self.assertRegex(str(payload["request_digest"]), r"^[0-9a-f]{64}$")
        disk = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (self.runs_dir / run_id).rglob("*")
            if path.is_file()
        )
        self.assertNotIn("endpoint-secret-71931", disk)

    def test_shutdown_waits_for_reservation_before_durable_write(self) -> None:
        import asyncio
        import threading

        runtime = web_server._web_run_runtime()
        original_reserve = runtime.services.reserve
        entered = threading.Event()
        release = self.client.portal.call(asyncio.Event)

        async def delayed_reserve(*args: object, **kwargs: object):
            entered.set()
            await release.wait()
            return await original_reserve(*args, **kwargs)

        async def shutdown() -> None:
            await web_server._shutdown_supervised_runs(
                timeout_s=3.0,
                poll_s=0.01,
            )

        content = b"paper"
        digest = hashlib.sha256(content).hexdigest()
        response = None
        state_after_shutdown = None
        with patch.object(runtime.services, "reserve", side_effect=delayed_reserve):
            with ThreadPoolExecutor(max_workers=2) as pool:
                reservation = pool.submit(
                    self.client.post,
                    "/api/runs/reserve",
                    headers={"Idempotency-Key": "shutdown-reserve-race"},
                    json={
                        "brief": json.dumps({"mode": "success"}),
                        "artifact_type": "landing",
                        "input_slots": [
                            {
                                "name": "attachment-0.pdf",
                                "role": "attachment",
                                "sha256": digest,
                                "size": len(content),
                            }
                        ],
                    },
                )
                self.assertTrue(entered.wait(timeout=2.0))
                stopping = pool.submit(self.client.portal.call, shutdown)
                time.sleep(0.1)
                waited_for_reservation = not stopping.done()
                self.client.portal.call(release.set)
                response = reservation.result(timeout=5.0)
                stopping.result(timeout=5.0)

        self.assertIsNotNone(response)
        self.assertEqual(response.status_code, 200, response.text)
        run_id = str(response.json()["run_id"])
        state_after_shutdown = RunControlStore(self.runs_dir).read(run_id).state
        if state_after_shutdown not in {"completed", "failed", "cancelled"}:
            self.client.post(f"/api/runs/{run_id}/cancel")

        self.assertTrue(waited_for_reservation)
        self.assertEqual(state_after_shutdown, "cancelled")

    def test_completed_run_route_serves_final_but_denies_internal_diagnostics(self) -> None:
        run_id = "completed-file-policy"
        store = RunControlStore(self.runs_dir)
        record = store.reserve(run_id, "landing")
        record = store.transition(run_id, record, "queued")
        record = store.transition(run_id, record, "running")
        record = store.transition(run_id, record, "completing")
        store.transition(run_id, record, "completed", publishable=True)
        final_path = self.runs_dir / run_id / "final" / "index.html"
        final_path.parent.mkdir(parents=True)
        final_path.write_text("<html>artifact</html>", encoding="utf-8")
        internal_path = self.runs_dir / run_id / "worker_stderr.log"
        internal_path.write_text("/Users/alice/private/paper.pdf", encoding="utf-8")

        artifact = self.client.get(f"/api/files/runs/{run_id}/final/index.html")
        internal = self.client.get(f"/api/files/runs/{run_id}/worker_stderr.log")
        with (
            patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
            patch.object(
                web_server,
                "_demo_run_access",
                return_value={"token": "owner-token"},
            ),
        ):
            unauthorized = self.client.get(
                f"/api/files/runs/{run_id}/worker_stderr.log"
            )

        self.assertEqual(artifact.status_code, 200, artifact.text)
        self.assertEqual(internal.status_code, 404, internal.text)
        self.assertIn(unauthorized.status_code, {403, 404})

    def test_running_attempt_snapshot_route_serves_browser_resources_only(self) -> None:
        run_id = "running-browser-snapshot"
        attempt_dir = self.runs_dir / run_id / "landing_author" / "attempt_01"
        assets_dir = attempt_dir / "assets"
        assets_dir.mkdir(parents=True)
        (attempt_dir / "index.html").write_text(
            '<!doctype html><link rel="stylesheet" href="assets/style.css">'
            '<img src="assets/figure.png">',
            encoding="utf-8",
        )
        (assets_dir / "figure.png").write_bytes(b"image")
        (assets_dir / "style.css").write_text("body{color:#123}", encoding="utf-8")
        (attempt_dir / "preview.png").write_bytes(b"preview")
        (attempt_dir / "designer_author_done.json").write_text(
            '{"provider":"private"}', encoding="utf-8"
        )
        (attempt_dir / "landing_visual_plan.json").write_text(
            '{"private":"plan"}', encoding="utf-8"
        )
        (attempt_dir / "landing_validation.json").write_text(
            '{"accepted":true}', encoding="utf-8"
        )
        candidate = capture_attempt_candidate(
            run_dir=self.runs_dir / run_id,
            attempt_dir=attempt_dir,
            artifact_type="landing",
            attempt=1,
            max_attempts=4,
            source_path="index.html",
            dependency_paths=[
                "assets/figure.png",
                "assets/style.css",
                "designer_author_done.json",
                "landing_visual_plan.json",
            ],
            browser_resource_paths=["assets/figure.png", "assets/style.css"],
            preview_paths=["preview.png"],
            validation_summary_path="landing_validation.json",
            safety_state="ready",
            hard_blockers=[],
            warnings=[],
        )
        store = RunControlStore(self.runs_dir)
        record = store.reserve(run_id, "landing")
        record = store.transition(run_id, record, "queued")
        store.transition(run_id, record, "running")

        for relative in (
            candidate.source_relative_path,
            *candidate.preview_relative_paths,
            *candidate.browser_resource_relative_paths,
        ):
            with self.subTest(relative=relative):
                response = self.client.get(f"/api/files/runs/{run_id}/{relative}")
                self.assertEqual(response.status_code, 200, response.text)

        denied = {
            "landing_author/attempt_01/candidate/designer_author_done.json": 409,
            "landing_author/attempt_01/candidate/landing_visual_plan.json": 409,
            candidate.validation_summary_relative_path: 409,
            "final/index.html": 409,
            "worker_stderr.log": 404,
        }
        for relative, expected_status in denied.items():
            with self.subTest(relative=relative):
                response = self.client.get(f"/api/files/runs/{run_id}/{relative}")
                self.assertEqual(response.status_code, expected_status, response.text)

        store.request_cancel(run_id)
        store.finalize_cancel(
            run_id,
            {"termination_verified": True, "reason": "test"},
        )
        response = self.client.get(
            f"/api/files/runs/{run_id}/{candidate.source_relative_path}"
        )
        self.assertEqual(response.status_code, 410, response.text)

    def test_running_legacy_attempt_snapshot_allows_static_assets_not_json(self) -> None:
        run_id = "running-legacy-browser-snapshot"
        attempt_dir = self.runs_dir / run_id / "landing_author" / "attempt_01"
        assets_dir = attempt_dir / "assets"
        assets_dir.mkdir(parents=True)
        (attempt_dir / "index.html").write_text(
            '<!doctype html><img src="assets/figure.png">', encoding="utf-8"
        )
        (assets_dir / "figure.png").write_bytes(b"image")
        (attempt_dir / "preview.png").write_bytes(b"preview")
        (attempt_dir / "designer_author_done.json").write_text(
            '{"provider":"private"}', encoding="utf-8"
        )
        (attempt_dir / "landing_validation.json").write_text(
            '{"accepted":true}', encoding="utf-8"
        )
        candidate = capture_attempt_candidate(
            run_dir=self.runs_dir / run_id,
            attempt_dir=attempt_dir,
            artifact_type="landing",
            attempt=1,
            max_attempts=4,
            source_path="index.html",
            dependency_paths=["assets/figure.png", "designer_author_done.json"],
            browser_resource_paths=["assets/figure.png"],
            preview_paths=["preview.png"],
            validation_summary_path="landing_validation.json",
            safety_state="ready",
            hard_blockers=[],
            warnings=[],
        )
        manifest_path = attempt_dir / "attempt_candidate.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("browser_resource_relative_paths")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        store = RunControlStore(self.runs_dir)
        record = store.reserve(run_id, "landing")
        record = store.transition(run_id, record, "queued")
        store.transition(run_id, record, "running")

        static_asset = candidate.dependency_relative_paths[0]
        static_response = self.client.get(
            f"/api/files/runs/{run_id}/{static_asset}"
        )
        private_response = self.client.get(
            f"/api/files/runs/{run_id}/"
            "landing_author/attempt_01/candidate/designer_author_done.json"
        )

        self.assertEqual(static_response.status_code, 200, static_response.text)
        self.assertEqual(private_response.status_code, 409, private_response.text)

    def test_completed_run_route_denies_casefolded_internal_names_and_windows_aliases(self) -> None:
        run_id = "completed-canonical-file-policy"
        store = RunControlStore(self.runs_dir)
        record = store.reserve(run_id, "landing")
        record = store.transition(run_id, record, "queued")
        record = store.transition(run_id, record, "running")
        record = store.transition(run_id, record, "completing")
        store.transition(run_id, record, "completed", publishable=True)
        run_dir = self.runs_dir / run_id
        final_dir = run_dir / "final"
        final_dir.mkdir(parents=True)
        (final_dir / "figure.v1.png").write_bytes(b"legitimate-artifact")
        (run_dir / "derived_job.json").write_bytes(b"private-job-descriptor")
        aliases = {
            "final/trailing.png.": b"windows-trailing-dot-alias",
            "final/trailing.png ": b"windows-trailing-space-alias",
            "final/secret:stream": b"windows-ads-alias",
            "final/CON": b"windows-reserved-device",
            "final/aux.txt": b"windows-reserved-device-with-extension",
            "final/CON .txt": b"windows-reserved-device-spaced-alias",
            "final/RUN_CO~1.JSO": b"windows-short-name-alias",
        }
        for relative, content in aliases.items():
            path = run_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        legitimate = self.client.get(
            f"/api/files/runs/{run_id}/final/figure.v1.png"
        )
        denied_relatives = ["RUN_CONTROL.JSON", "DERIVED_JOB.JSON", *aliases]
        denied = [
            self.client.get(f"/api/files/runs/{run_id}/{relative}")
            for relative in denied_relatives
        ]

        self.assertEqual(legitimate.status_code, 200, legitimate.text)
        self.assertEqual(legitimate.content, b"legitimate-artifact")
        self.assertEqual(
            [response.status_code for response in denied],
            [404] * len(denied),
            [response.text for response in denied],
        )

    def test_completed_run_route_requires_and_accepts_owner_file_token(self) -> None:
        run_id = "completed-owner-token-policy"
        store = RunControlStore(self.runs_dir)
        record = store.reserve(run_id, "landing")
        record = store.transition(run_id, record, "queued")
        record = store.transition(run_id, record, "running")
        record = store.transition(run_id, record, "completing")
        store.transition(run_id, record, "completed", publishable=True)
        final_path = self.runs_dir / run_id / "final" / "index.html"
        final_path.parent.mkdir(parents=True)
        final_path.write_text("<html>artifact</html>", encoding="utf-8")

        with (
            patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
            patch.object(
                web_server,
                "_demo_run_access",
                return_value={"token": "owner-token"},
            ),
        ):
            missing = self.client.get(f"/api/files/runs/{run_id}/final/index.html")
            wrong = self.client.get(
                f"/api/files/runs/{run_id}/final/index.html?token=wrong-token"
            )
            valid = self.client.get(
                f"/api/files/runs/{run_id}/final/index.html?token=owner-token"
            )

        self.assertEqual(missing.status_code, 403, missing.text)
        self.assertEqual(wrong.status_code, 403, wrong.text)
        self.assertEqual(valid.status_code, 200, valid.text)
        self.assertEqual(valid.text, "<html>artifact</html>")

    def test_completed_run_route_streams_opened_file_not_swapped_symlink(self) -> None:
        run_id = "completed-open-handle-race"
        store = RunControlStore(self.runs_dir)
        record = store.reserve(run_id, "video")
        record = store.transition(run_id, record, "queued")
        record = store.transition(run_id, record, "running")
        record = store.transition(run_id, record, "completing")
        store.transition(run_id, record, "completed", publishable=True)
        public_path = self.runs_dir / run_id / "final" / "video.mp4"
        public_path.parent.mkdir(parents=True)
        public_path.write_bytes(b"public-video")
        private_path = self.runs_dir / "private-run" / "worker_stderr.log"
        private_path.parent.mkdir(parents=True)
        private_path.write_bytes(b"private-diagnostic")
        self.assertTrue(
            hasattr(web_server, "_open_public_run_file"),
            "public run files must be opened before path names can be swapped",
        )
        original_opener = web_server._open_public_run_file

        def open_then_swap(rel_path: str):
            opened = original_opener(rel_path)
            public_path.unlink()
            public_path.symlink_to(private_path)
            return opened

        with patch.object(
            web_server,
            "_open_public_run_file",
            side_effect=open_then_swap,
        ):
            response = self.client.get(f"/api/files/runs/{run_id}/final/video.mp4")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, b"public-video")

    def test_completed_run_route_preserves_head_and_byte_range_semantics(self) -> None:
        run_id = "completed-range-semantics"
        store = RunControlStore(self.runs_dir)
        record = store.reserve(run_id, "video")
        record = store.transition(run_id, record, "queued")
        record = store.transition(run_id, record, "running")
        record = store.transition(run_id, record, "completing")
        store.transition(run_id, record, "completed", publishable=True)
        media = b"0123456789"
        path = self.runs_dir / run_id / "final" / "video.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(media)

        head = self.client.head(f"/api/files/runs/{run_id}/final/video.mp4")
        partial = self.client.get(
            f"/api/files/runs/{run_id}/final/video.mp4",
            headers={"Range": "bytes=2-5"},
        )

        self.assertEqual(head.status_code, 200, head.text)
        self.assertEqual(head.content, b"")
        self.assertEqual(head.headers["content-length"], str(len(media)))
        self.assertEqual(head.headers["accept-ranges"], "bytes")
        self.assertEqual(partial.status_code, 206, partial.text)
        self.assertEqual(partial.content, b"2345")
        self.assertEqual(partial.headers["content-range"], "bytes 2-5/10")
        self.assertEqual(partial.headers["content-type"], "video/mp4")

    def test_completed_run_multi_range_streams_only_the_opened_file(self) -> None:
        run_id = "completed-multi-range-race"
        store = RunControlStore(self.runs_dir)
        record = store.reserve(run_id, "video")
        record = store.transition(run_id, record, "queued")
        record = store.transition(run_id, record, "running")
        record = store.transition(run_id, record, "completing")
        store.transition(run_id, record, "completed", publishable=True)
        public_path = self.runs_dir / run_id / "final" / "video.mp4"
        public_path.parent.mkdir(parents=True)
        public_path.write_bytes(b"0123456789")
        private_path = self.runs_dir / "private-range-run" / "worker_stderr.log"
        private_path.parent.mkdir(parents=True)
        private_path.write_bytes(b"abcdefghij")
        original_opener = web_server._open_public_run_file

        def open_then_swap(rel_path: str):
            opened = original_opener(rel_path)
            public_path.unlink()
            public_path.symlink_to(private_path)
            return opened

        with patch.object(
            web_server,
            "_open_public_run_file",
            side_effect=open_then_swap,
        ):
            response = self.client.get(
                f"/api/files/runs/{run_id}/final/video.mp4",
                headers={"Range": "bytes=0-1,8-9"},
            )

        self.assertEqual(response.status_code, 206, response.text)
        self.assertTrue(response.headers["content-type"].startswith("multipart/byteranges"))
        self.assertEqual(len(response.content), int(response.headers["content-length"]))
        self.assertIn(b"\r\n01\r\n", response.content)
        self.assertIn(b"\r\n89\r\n", response.content)
        self.assertNotIn(b"\r\nab\r\n", response.content)
        self.assertNotIn(b"\r\nij\r\n", response.content)

    def test_reserve_idempotency_same_digest_reuses_and_different_digest_conflicts(self) -> None:
        first = self._reserve()
        second = self._reserve()
        self.assertEqual(second["run_id"], first["run_id"])
        self.assertTrue(second["reused"])
        self.assertEqual(second["input_slots"], first["input_slots"])
        self.assertEqual(second["request_digest"], first["request_digest"])
        conflict = self.client.post(
            "/api/runs/reserve",
            headers={"Idempotency-Key": "reserve-api-test"},
            json={"brief": "different request", "artifact_type": "landing"},
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)

    def test_reserve_preserves_paper_poster_kimi_auto_switch(self) -> None:
        kimi_settings = replace(
            self.settings,
            designer_model="moonshot/kimi-k2.6",
        )
        content = b"paper"
        with (
            patch.object(web_server, "SETTINGS", kimi_settings),
            patch.object(web_server, "_require_artifact_runtime"),
            patch.object(
                web_server,
                "_web_paper_poster_settings",
                side_effect=lambda value: value,
            ),
            patch.object(
                web_server,
                "_paper_poster_author_cmd_resolution",
                return_value={"available": True, "message": "", "source": "test"},
            ),
        ):
            response = self.client.post(
                "/api/runs/reserve",
                headers={"Idempotency-Key": "kimi-paper-profile"},
                json={
                    "brief": "Make a poster",
                    "artifact_type": "poster",
                    "palette_id": "plum_sage",
                    "input_slots": [
                        {
                            "name": "paper.pdf",
                            "role": "attachment",
                            "sha256": hashlib.sha256(content).hexdigest(),
                            "size": len(content),
                        }
                    ],
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        state = web_server._RUNS[response.json()["run_id"]]
        self.assertEqual(state.designer_model, web_server._OPUS_FALLBACK)

    def test_reserve_and_upload_preserve_generation_history_events(self) -> None:
        content = b"paper"
        payload = self._reserve(content=content)
        run_id = str(payload["run_id"])
        upload = self.client.put(
            f"/api/runs/{run_id}/inputs/attachment-0.pdf",
            headers={"X-AutoDesign-Upload-Token": payload["upload_token"]},
            content=content,
        )
        self.assertEqual(upload.status_code, 200, upload.text)
        events = [
            json.loads(line)
            for path in (self.out_dir / "design_sessions").glob("*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            sum(
                event.get("event") == "message.user_submitted"
                and event.get("run_id") == run_id
                for event in events
            ),
            1,
        )
        self.assertEqual(
            sum(
                event.get("event") == "attachment.added"
                and event.get("run_id") == run_id
                for event in events
            ),
            1,
        )

    def test_slot_upload_same_digest_is_idempotent_and_different_digest_conflicts(self) -> None:
        content = b"paper"
        payload = self._reserve(content=content)
        run_id = str(payload["run_id"])
        token = str(payload["upload_token"])
        headers = {"X-AutoDesign-Upload-Token": token}
        url = f"/api/runs/{run_id}/inputs/attachment-0.pdf"
        first = self.client.put(url, headers=headers, content=content)
        second = self.client.put(url, headers=headers, content=content)
        conflict = self.client.put(url, headers=headers, content=b"other")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()["idempotent"])
        self.assertEqual(conflict.status_code, 409, conflict.text)

    def test_cancel_event_is_persisted_once_and_cancel_is_idempotent(self) -> None:
        payload = self._reserve()
        run_id = str(payload["run_id"])
        first = self.client.post(f"/api/runs/{run_id}/cancel")
        second = self.client.post(f"/api/runs/{run_id}/cancel")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json()["run_state"], "cancelled")
        self.assertEqual(second.json()["run_state"], "cancelled")
        events = [
            json.loads(line)
            for line in (self.runs_dir / run_id / "run_events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(
            sum(event.get("event") == "run.cancel_requested" for event in events),
            1,
        )
        self.assertEqual(
            sum(event.get("event") == "run.cancelled" for event in events),
            1,
        )

    def test_last_event_id_replays_without_duplicate_terminal(self) -> None:
        payload = self._reserve()
        run_id = str(payload["run_id"])
        self.client.post(f"/api/runs/{run_id}/cancel")
        events = [
            json.loads(line)
            for line in (self.runs_dir / run_id / "run_events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        requested = next(
            event for event in events if event.get("event") == "run.cancel_requested"
        )
        with self.client.stream(
            "GET",
            f"/api/runs/{run_id}/events",
            headers={"Last-Event-ID": requested["event_id"]},
        ) as response:
            body = "".join(response.iter_text())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("\nevent:", body)
        self.assertEqual(body.count('"event": "run.cancelled"'), 1)
        self.assertNotIn('"event": "run.cancel_requested"', body)

    def test_unknown_last_event_id_replays_authoritative_log(self) -> None:
        payload = self._reserve()
        run_id = str(payload["run_id"])
        self.client.post(f"/api/runs/{run_id}/cancel")
        with (
            patch.object(web_server, "_SSE_DEADLINE_S", 0.2),
            self.client.stream(
                "GET",
                f"/api/runs/{run_id}/events",
                headers={"Last-Event-ID": "unknown-event-id"},
            ) as response,
        ):
            body = "".join(response.iter_text())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("\nevent:", body)
        self.assertEqual(body.count('"event": "run.cancel_requested"'), 1)
        self.assertEqual(body.count('"event": "run.cancelled"'), 1)

    def test_client_resumes_after_sse_deadline_and_receives_one_terminal_event(
        self,
    ) -> None:
        payload = self._reserve()
        run_id = str(payload["run_id"])
        (self.runs_dir / run_id / "run_events.jsonl").write_text(
            json.dumps({
                "run_id": run_id,
                "event": "run.start",
                "event_id": "before-deadline",
            })
            + "\n",
            encoding="utf-8",
        )
        with (
            patch.object(web_server, "_SSE_DEADLINE_S", 0.1),
            self.client.stream("GET", f"/api/runs/{run_id}/events") as response,
        ):
            before_terminal = "".join(response.iter_text())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('"event": "run.cancelled"', before_terminal)
        event_ids = [
            line.removeprefix("id: ")
            for line in before_terminal.splitlines()
            if line.startswith("id: ")
        ]
        self.assertTrue(event_ids, before_terminal)

        cancelled = self.client.post(f"/api/runs/{run_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        with self.client.stream(
            "GET",
            f"/api/runs/{run_id}/events",
            headers={"Last-Event-ID": event_ids[-1]},
        ) as response:
            resumed = "".join(response.iter_text())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(resumed.count('"event": "run.cancelled"'), 1)
        self.assertNotIn(before_terminal, resumed)

    def test_sse_does_not_expose_persisted_worker_exit_diagnostics(self) -> None:
        payload = self._reserve()
        run_id = str(payload["run_id"])
        self.client.post(f"/api/runs/{run_id}/cancel")
        events_path = self.runs_dir / run_id / "run_events.jsonl"
        events = [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
        ]
        terminal_index = next(
            index
            for index, event in enumerate(events)
            if event.get("event") == "run.cancelled"
        )
        worker_exit = {
            "run_id": run_id,
            "event": "worker.exit",
            "version": 1,
            "returncode": 17,
            "error_code": "worker_result_missing",
            "error_message": "Worker stopped before writing its result.",
            "error_detail": "private diagnostic detail",
            "protocol_error": "worker_result.json is missing",
            "last_event": "fixture.before_exit",
            "last_worker_seq": 2,
            "last_phase": "authoring",
            "last_reason": "process_exit",
            "stdout_tail": "private stdout tail",
            "stderr_tail": "private stderr tail",
            "event_id": "worker-exit-private",
            "seq": int(events[terminal_index]["seq"]),
        }
        events.insert(terminal_index, worker_exit)
        for index, event in enumerate(events, start=1):
            event["seq"] = index
        events_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )

        with self.client.stream("GET", f"/api/runs/{run_id}/events") as response:
            body = "".join(response.iter_text())

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('"event": "worker.exit"', body)
        self.assertNotIn("private diagnostic detail", body)
        self.assertEqual(body.count('"event": "run.cancelled"'), 1)

    def test_status_uses_durable_control_after_runtime_reset(self) -> None:
        payload = self._reserve()
        run_id = str(payload["run_id"])
        web_server._reset_web_run_runtime_for_tests()
        response = self.client.get(f"/api/runs/{run_id}/status")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["run_state"], "reserved")

    def test_cancel_after_server_restart_uses_durable_control(self) -> None:
        payload = self._reserve()
        run_id = str(payload["run_id"])
        web_server._reset_web_run_runtime_for_tests()
        cancelled = self.client.post(f"/api/runs/{run_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["run_state"], "cancelled")
        self.assertEqual(
            RunControlStore(self.runs_dir).read(run_id).state,
            "cancelled",
        )

    def test_cancelled_run_returns_owner_diagnostic_without_artifact_urls(self) -> None:
        payload = self._reserve()
        run_id = str(payload["run_id"])
        self.client.post(f"/api/runs/{run_id}/cancel")
        response = self.client.get(f"/api/runs/{run_id}/artifact")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIsNone(body["artifact"])
        self.assertEqual(body["message"]["failure"]["status"], "cancelled")
        self.assertNotIn("/api/files/runs/", response.text)
        self.assertNotIn("native_file_url", response.text)

    def test_cancelled_run_diagnostic_recovers_without_in_memory_state(self) -> None:
        payload = self._reserve()
        run_id = str(payload["run_id"])
        self.client.post(f"/api/runs/{run_id}/cancel")
        web_server._RUNS.pop(run_id, None)
        web_server._reset_web_run_runtime_for_tests()
        response = self.client.get(f"/api/runs/{run_id}/artifact")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIsNone(body["artifact"])
        self.assertEqual(body["message"]["failure"]["status"], "cancelled")
        self.assertEqual(body["message"]["failure"]["produced_files"], [])
        self.assertNotIn("/api/files/runs/", response.text)

    def test_cancelled_run_rejects_all_source_operations_before_side_effects(self) -> None:
        payload = self._reserve()
        run_id = str(payload["run_id"])
        run_dir = self.runs_dir / run_id
        final_dir = run_dir / "final"
        final_dir.mkdir(parents=True, exist_ok=True)
        (final_dir / "poster.html").write_text("<html></html>", encoding="utf-8")
        (run_dir / "design_spec.json").write_text("{}", encoding="utf-8")
        project = run_dir / "hyperframes-test"
        project.mkdir()
        (project / "index.html").write_text("<html></html>", encoding="utf-8")
        (project / "video_delivery_contract.json").write_text("{}", encoding="utf-8")
        cancelled = self.client.post(f"/api/runs/{run_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)

        draft_run_id = "candidate-draft-source-guard"
        draft_dir = self.runs_dir / draft_run_id
        draft_dir.mkdir()
        (draft_dir / "candidate_draft_lineage.json").write_text(
            json.dumps(
                {
                    "status": "draft",
                    "artifact_type": "poster",
                    "source_run_id": run_id,
                    "source_attempt": 1,
                    "source_candidate_id": "poster-attempt-01-source-guard",
                    "source_candidate_sha256": "a" * 64,
                }
            ),
            encoding="utf-8",
        )
        artifact = {
            "artifact_id": f"art_{run_id}",
            "artifact_type": "poster",
            "native_format": "html",
            "native_file_url": f"/api/files/runs/{run_id}/final/poster.html",
        }

        with (
            patch.object(web_server, "_html_export_source_path") as export_source,
            patch.object(web_server, "_settings_for_code_editor_request") as code_settings,
            patch.object(web_server, "_settings_for_openresearch_request") as research_settings,
            patch.object(web_server, "request_attempt_selection") as select_attempt,
            patch.object(web_server, "load_attempt_candidate") as load_candidate,
            patch.object(web_server, "_validate_candidate_draft") as validate_draft,
            patch.object(web_server, "_settings_for_request") as retry_settings,
            patch.object(web_server, "_require_artifact_runtime") as require_runtime,
        ):
            responses = (
                self.client.post(
                    "/api/artifacts/export",
                    json={"artifact": artifact, "format": "pdf"},
                ),
                self.client.post(
                    "/api/artifacts/export/pptx-run",
                    json={"artifact": artifact},
                ),
                self.client.post(
                    "/api/code-edit/poster",
                    json={
                        "artifact": artifact,
                        "instruction": "tighten the layout",
                        "palette_id": "royal_blue",
                        "source_run_id": run_id,
                    },
                ),
                self.client.post(
                    "/api/openresearch/projects",
                    json={"artifact": artifact, "source_run_id": run_id},
                ),
                self.client.post(
                    f"/api/runs/{run_id}/attempts/1/select",
                    json={
                        "idempotency_key": "cancelled-selection",
                        "expected_candidate_sha256": "a" * 64,
                    },
                ),
                self.client.post(
                    f"/api/runs/{run_id}/attempts/1/fork",
                    json={},
                ),
                self.client.post(
                    f"/api/artifacts/art_{draft_run_id}/publish-candidate-draft",
                    json={},
                ),
                self.client.post(f"/api/runs/{run_id}/retry"),
                self.client.post(f"/api/runs/{run_id}/retry-video-export"),
            )

        self.assertEqual(
            [response.status_code for response in responses],
            [410] * len(responses),
            [response.text for response in responses],
        )
        for blocked_side_effect in (
            export_source,
            code_settings,
            research_settings,
            select_attempt,
            load_candidate,
            validate_draft,
            retry_settings,
            require_runtime,
        ):
            blocked_side_effect.assert_not_called()

    def test_candidate_draft_rejects_unsafe_source_identity_before_source_access(self) -> None:
        draft_run_id = "candidate-draft-unsafe-source"
        draft_dir = self.runs_dir / draft_run_id
        draft_dir.mkdir()
        (draft_dir / "candidate_draft_lineage.json").write_text(
            json.dumps(
                {
                    "status": "draft",
                    "artifact_type": "poster",
                    "source_run_id": "../escaped-source",
                    "source_attempt": 1,
                    "source_candidate_id": "poster-attempt-01-unsafe-source",
                    "source_candidate_sha256": "a" * 64,
                }
            ),
            encoding="utf-8",
        )

        with (
            patch.object(
                web_server,
                "_assert_controlled_run_source_usable",
                wraps=web_server._assert_controlled_run_source_usable,
            ) as source_access,
            patch.object(
                web_server,
                "load_attempt_candidate",
                wraps=web_server.load_attempt_candidate,
            ) as load_candidate,
        ):
            response = self.client.post(
                f"/api/artifacts/art_{draft_run_id}/publish-candidate-draft",
                json={},
            )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            response.json()["detail"],
            {"code": "candidate_draft_lineage_invalid"},
        )
        self.assertEqual(source_access.call_count, 1)
        self.assertEqual(source_access.call_args.args[0], draft_run_id)
        load_candidate.assert_not_called()

    def test_cancelled_run_rejects_apply_edits_before_source_reads(self) -> None:
        payload = self._reserve()
        run_id = str(payload["run_id"])
        final_dir = self.runs_dir / run_id / "final"
        final_dir.mkdir(parents=True, exist_ok=True)
        (final_dir / "index.html").write_text(
            "<html><body data-layer-id=\"root\">draft</body></html>",
            encoding="utf-8",
        )
        cancelled = self.client.post(f"/api/runs/{run_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)

        with (
            patch.object(
                web_server,
                "_settings_for_request",
                return_value=self.settings,
            ) as settings_for_request,
            patch.object(web_server, "_read_json_file") as read_json,
            patch.object(web_server, "_patch_html_for_apply_edits") as patch_html,
        ):
            response = self.client.post(
                "/api/edits/apply",
                data={
                    "run_id": run_id,
                    "artifact_type": "landing",
                    "edits_json": "{}",
                },
            )

        self.assertEqual(response.status_code, 410, response.text)
        settings_for_request.assert_not_called()
        read_json.assert_not_called()
        patch_html.assert_not_called()

    def test_apply_edits_rejects_internal_cross_run_symlink_and_hardlink_images(self) -> None:
        source_run_id = "apply-image-source"
        victim_run_id = "apply-image-victim"
        store = RunControlStore(self.runs_dir)
        source = store.reserve(source_run_id, "landing")
        source = store.transition(source_run_id, source, "queued")
        source = store.transition(source_run_id, source, "running")
        source = store.transition(source_run_id, source, "completing")
        store.transition(source_run_id, source, "completed", publishable=True)
        source_final = self.runs_dir / source_run_id / "final"
        source_final.mkdir(parents=True)
        (source_final / "index.html").write_text(
            """<!doctype html><html><body><main data-autodesign-artifact-root="landing">
            <img data-layer-id="hero" data-kind="image" src="old.png">
            </main></body></html>""",
            encoding="utf-8",
        )
        victim_secret = self.runs_dir / victim_run_id / "final" / "secret.png"
        victim_secret.parent.mkdir(parents=True)
        victim_secret.write_bytes(b"victim-secret-image")
        symlink_alias = source_final / "symlink.png"
        symlink_alias.symlink_to(victim_secret)
        hardlink_alias = source_final / "hardlink.png"
        os.link(victim_secret, hardlink_alias)
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
            with self.subTest(label=label), patch.object(
                web_server,
                "_apply_authored_html_edits",
                side_effect=AssertionError("unauthorized image reached apply stage"),
            ) as apply_authored:
                response = self.client.post(
                    "/api/edits/apply",
                    data={
                        "run_id": source_run_id,
                        "artifact_type": "landing",
                        "edits_json": json.dumps({
                            "layers": {"hero": {"src": image_url}},
                        }),
                    },
                )

                self.assertEqual(response.status_code, 400, response.text)
                apply_authored.assert_not_called()

    def test_apply_edits_allows_same_run_regular_image_via_stable_read(self) -> None:
        source_run_id = "apply-image-valid-source"
        store = RunControlStore(self.runs_dir)
        source = store.reserve(source_run_id, "landing")
        source = store.transition(source_run_id, source, "queued")
        source = store.transition(source_run_id, source, "running")
        source = store.transition(source_run_id, source, "completing")
        store.transition(source_run_id, source, "completed", publishable=True)
        source_final = self.runs_dir / source_run_id / "final"
        source_final.mkdir(parents=True)
        (source_final / "index.html").write_text(
            """<!doctype html><html><head><style>
            body{margin:0}.ld-landing{width:1200px;min-height:800px}
            </style></head><body>
            <main class="ld-landing" data-autodesign-artifact-root="landing" data-w="1200">
            <img data-layer-id="hero" data-kind="image" src="old.png">
            </main></body></html>""",
            encoding="utf-8",
        )
        replacement = source_final / "replacement.png"
        replacement.write_bytes(b"same-run-image")
        child_run_id = "apply-image-valid-result"

        with patch.object(web_server, "new_run_id", return_value=child_run_id):
            response = self.client.post(
                "/api/edits/apply",
                data={
                    "run_id": source_run_id,
                    "artifact_type": "landing",
                    "edits_json": json.dumps({
                        "layers": {
                            "hero": {
                                "src": (
                                    f"/api/files/runs/{source_run_id}/final/replacement.png"
                                )
                            }
                        }
                    }),
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        edited_html = (
            self.runs_dir / child_run_id / "final" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn("data:image/png;base64,", edited_html)
        self.assertIn(
            base64.b64encode(b"same-run-image").decode("ascii"),
            edited_html,
        )

    def test_cancelled_run_rejects_asset_listing_before_collection(self) -> None:
        payload = self._reserve()
        run_id = str(payload["run_id"])
        asset_path = self.runs_dir / run_id / "final" / "layers" / "figure.png"
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_bytes(b"diagnostic-only")
        cancelled = self.client.post(f"/api/runs/{run_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)

        with patch.object(web_server, "_collect_artifact_assets") as collect:
            response = self.client.get(f"/api/artifacts/art_{run_id}/assets")

        self.assertEqual(response.status_code, 410, response.text)
        collect.assert_not_called()

    def test_asset_listing_rejects_cancelled_revision_parent_before_collection(self) -> None:
        store = RunControlStore(self.runs_dir)
        child_run_id = "completed-revision-child"
        parent_run_id = "cancelled-revision-parent"
        child_final = self.runs_dir / child_run_id / "final"
        child_final.mkdir(parents=True)
        (child_final / "poster.html").write_text("<html></html>", encoding="utf-8")
        (child_final / "code_editor_revision_manifest.json").write_text(
            json.dumps({"parent_run_id": parent_run_id}),
            encoding="utf-8",
        )
        child = store.reserve(child_run_id, "poster")
        child = store.transition(child_run_id, child, "queued")
        child = store.transition(child_run_id, child, "running")
        child = store.transition(child_run_id, child, "completing")
        store.transition(
            child_run_id,
            child,
            "completed",
            publishable=True,
        )

        parent_asset = self.runs_dir / parent_run_id / "layers" / "ingest_fig_07.png"
        parent_asset.parent.mkdir(parents=True)
        parent_asset.write_bytes(b"diagnostic-only-parent")
        store.reserve(parent_run_id, "poster")
        store.request_cancel(parent_run_id)
        store.finalize_cancel(
            parent_run_id,
            {"termination_verified": True, "reason": "test"},
        )

        with patch.object(web_server, "_collect_artifact_assets") as collect:
            response = self.client.get(
                f"/api/artifacts/art_{child_run_id}/assets"
            )

        self.assertEqual(response.status_code, 410, response.text)
        collect.assert_not_called()

    def test_asset_listing_authenticates_revision_parent_before_collection(self) -> None:
        store = RunControlStore(self.runs_dir)
        child_run_id = "owned-revision-child"
        parent_run_id = "unowned-revision-parent"
        child_final = self.runs_dir / child_run_id / "final"
        child_final.mkdir(parents=True)
        (child_final / "poster.html").write_text("<html></html>", encoding="utf-8")
        (child_final / "code_editor_revision_manifest.json").write_text(
            json.dumps({"parent_run_id": parent_run_id}),
            encoding="utf-8",
        )
        for run_id in (child_run_id, parent_run_id):
            record = store.reserve(run_id, "poster")
            record = store.transition(run_id, record, "queued")
            record = store.transition(run_id, record, "running")
            record = store.transition(run_id, record, "completing")
            store.transition(run_id, record, "completed", publishable=True)

        def owns_run(run_id: str, user_id: str) -> bool:
            self.assertEqual(user_id, "owner-a")
            return run_id == child_run_id

        with (
            patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
            patch.object(web_server, "_demo_user_id", return_value="owner-a"),
            patch.object(web_server, "_demo_user_owns_run", side_effect=owns_run),
            patch.object(web_server, "_collect_artifact_assets") as collect,
        ):
            response = self.client.get(
                f"/api/artifacts/art_{child_run_id}/assets"
            )

        self.assertEqual(response.status_code, 404, response.text)
        collect.assert_not_called()

    def test_export_rejects_declared_run_and_source_run_mismatch(self) -> None:
        declared_run_id = "completed-export-declaration"
        declared = RunControlStore(self.runs_dir).reserve(
            declared_run_id,
            "landing",
        )
        declared = RunControlStore(self.runs_dir).transition(
            declared_run_id,
            declared,
            "queued",
        )
        declared = RunControlStore(self.runs_dir).transition(
            declared_run_id,
            declared,
            "running",
        )
        declared = RunControlStore(self.runs_dir).transition(
            declared_run_id,
            declared,
            "completing",
        )
        RunControlStore(self.runs_dir).transition(
            declared_run_id,
            declared,
            "completed",
            publishable=True,
        )

        source_payload = self._reserve()
        source_run_id = str(source_payload["run_id"])
        source = self.runs_dir / source_run_id / "final" / "index.html"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("<html><body>diagnostic</body></html>", encoding="utf-8")
        cancelled = self.client.post(f"/api/runs/{source_run_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        artifact = {
            "artifact_id": f"art_{declared_run_id}",
            "artifact_type": "landing",
            "name": "bypass",
            "native_format": "html",
            "native_file_url": (
                f"/api/files/runs/{source_run_id}/final/index.html"
            ),
        }

        before_runs = set(web_server._RUNS)
        with (
            patch.object(web_server, "_write_standalone_html") as write_html,
            patch.object(
                web_server,
                "_settings_for_code_editor_request",
                return_value=self.settings,
            ) as code_settings,
            patch.object(
                web_server,
                "_code_editor_cmd_resolution",
                return_value={"available": True, "cmd": "codex", "source": "test"},
            ),
            patch.object(
                web_server,
                "_run_pptx_export_in_background",
                new=AsyncMock(),
            ) as start_pptx,
        ):
            standalone = self.client.post(
                "/api/artifacts/export",
                json={"artifact": artifact, "format": "standalone_html"},
            )
            pptx = self.client.post(
                "/api/artifacts/export/pptx-run",
                json={"artifact": artifact},
            )

        self.assertIn(standalone.status_code, {400, 409, 410})
        self.assertIn(pptx.status_code, {400, 409, 410})
        write_html.assert_not_called()
        code_settings.assert_not_called()
        start_pptx.assert_not_called()
        self.assertFalse((source.parent / "exports").exists())
        self.assertEqual(set(web_server._RUNS), before_runs)

    def test_export_authenticates_every_referenced_run_before_source_lookup(self) -> None:
        owned_run_id = "owned-export-declaration"
        unowned_run_id = "unowned-export-source"
        artifact = {
            "artifact_id": f"art_{owned_run_id}",
            "artifact_type": "landing",
            "name": "cross-owner-probe",
            "native_format": "html",
            "native_file_url": (
                f"/api/files/runs/{unowned_run_id}/final/index.html"
            ),
        }

        def owns_run(run_id: str, user_id: str) -> bool:
            self.assertEqual(user_id, "owner-a")
            return run_id == owned_run_id

        with (
            patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
            patch.object(web_server, "_demo_user_id", return_value="owner-a"),
            patch.object(web_server, "_demo_user_owns_run", side_effect=owns_run),
            patch.object(web_server, "_html_export_source_path") as source_lookup,
        ):
            standalone = self.client.post(
                "/api/artifacts/export",
                json={"artifact": artifact, "format": "standalone_html"},
            )
            pptx = self.client.post(
                "/api/artifacts/export/pptx-run",
                json={"artifact": artifact},
            )

        self.assertEqual(standalone.status_code, 404, standalone.text)
        self.assertEqual(pptx.status_code, 404, pptx.text)
        source_lookup.assert_not_called()

    def test_cancelled_attempt_diagnostics_do_not_schedule_recovery_or_write(self) -> None:
        payload = self._reserve()
        run_id = str(payload["run_id"])
        selection_dir = self.runs_dir / run_id / "attempt_candidates"
        selection_dir.mkdir()
        selection_path = selection_dir / "selection.json"
        selection_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "candidate_id": "candidate-1",
                    "candidate_sha256": "a" * 64,
                    "source_attempt": 1,
                    "idempotency_key": "selection-1",
                    "state": "requested",
                    "updated_at": "2026-08-03T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        cancelled = self.client.post(f"/api/runs/{run_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        before = selection_path.read_bytes(), selection_path.stat().st_mtime_ns

        with patch.object(web_server, "_schedule_attempt_selection_recovery") as schedule:
            response = self.client.get(f"/api/runs/{run_id}/attempts")

        self.assertEqual(response.status_code, 200, response.text)
        schedule.assert_not_called()
        self.assertEqual(selection_path.read_bytes(), before[0])
        self.assertEqual(selection_path.stat().st_mtime_ns, before[1])

    def test_reservation_expiry_fails_run_and_emits_terminal_event(self) -> None:
        payload = self._reserve()
        run_id = str(payload["run_id"])
        runtime = web_server._web_run_runtime()

        async def expire() -> tuple[str, ...]:
            return await runtime.services.reconcile_expired_reservations(
                now=time.time() + 3600,
            )

        expired = self.client.portal.call(expire)
        self.assertEqual(expired, (run_id,))
        record = RunControlStore(self.runs_dir).read(run_id)
        self.assertEqual(record.state, "failed")
        events = [
            json.loads(line)
            for line in (self.runs_dir / run_id / "run_events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(
            sum(event.get("event") == "run.error" for event in events),
            1,
        )

    def test_cancel_response_waits_for_verified_worker_exit(self) -> None:
        self._use_fixture_supervisor()
        with (
            patch.object(web_server, "_apply_type_prologue", side_effect=lambda brief, _kind: brief),
            patch.object(
                web_server,
                "_apply_conversation_prologue",
                side_effect=lambda brief, **_kwargs: brief,
            ),
        ):
            response = self.client.post(
                "/api/runs/reserve",
                headers={"Idempotency-Key": "verified-exit"},
                json={"brief": json.dumps({"mode": "ignore_term"}), "artifact_type": "landing"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            reserved = response.json()
            run_id = reserved["run_id"]
            token = reserved["upload_token"]
            started = self.client.post(
                f"/api/runs/{run_id}/start",
                headers={"X-AutoDesign-Upload-Token": token},
            )
            self.assertEqual(started.status_code, 200, started.text)
            worker_pid = RunControlStore(self.runs_dir).read(run_id).worker_pid
            self.assertIsNotNone(worker_pid)
            cancelled = self.client.post(f"/api/runs/{run_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["run_state"], "cancelled")
        self.assertIn(worker_pid, cancelled.json()["terminated_pids"])
        time.sleep(0.05)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(worker_pid), 0)

    def test_cancel_confirmation_waits_for_web_completion_monitor(self) -> None:
        self._use_fixture_supervisor()
        with (
            patch.object(web_server, "_apply_type_prologue", side_effect=lambda brief, _kind: brief),
            patch.object(
                web_server,
                "_apply_conversation_prologue",
                side_effect=lambda brief, **_kwargs: brief,
            ),
        ):
            reserved = self.client.post(
                "/api/runs/reserve",
                headers={"Idempotency-Key": "web-monitor-quiescence"},
                json={
                    "brief": json.dumps({"mode": "ignore_term"}),
                    "artifact_type": "landing",
                },
            ).json()
            run_id = str(reserved["run_id"])
            started = self.client.post(
                f"/api/runs/{run_id}/start",
                headers={
                    "X-AutoDesign-Upload-Token": reserved["upload_token"],
                },
            )
            self.assertEqual(started.status_code, 200, started.text)
            worker_pid = RunControlStore(self.runs_dir).read(run_id).worker_pid
            self.assertIsNotNone(worker_pid)

            async def install_delayed_monitor():
                import asyncio

                state = web_server._RUNS[run_id]
                original = state.task
                self.assertIsNotNone(original)
                release = asyncio.Event()

                async def delayed_monitor() -> None:
                    await original
                    await release.wait()

                state.task = asyncio.create_task(delayed_monitor())
                return release, state.task

            release, delayed_task = self.client.portal.call(
                install_delayed_monitor
            )
            with ThreadPoolExecutor(max_workers=1) as pool:
                cancellation = pool.submit(
                    self.client.post,
                    f"/api/runs/{run_id}/cancel",
                )
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    try:
                        os.kill(int(worker_pid), 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.01)
                time.sleep(0.5)
                waited_for_monitor = not cancellation.done()
                self.client.portal.call(release.set)
                response = cancellation.result(timeout=5.0)

        self.assertTrue(waited_for_monitor)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["confirmed"])
        self.assertTrue(delayed_task.done())

    def test_cancel_confirmation_waits_for_attempt_selection_writer(self) -> None:
        self._use_fixture_supervisor()
        with (
            patch.object(web_server, "_apply_type_prologue", side_effect=lambda brief, _kind: brief),
            patch.object(
                web_server,
                "_apply_conversation_prologue",
                side_effect=lambda brief, **_kwargs: brief,
            ),
        ):
            reserved = self.client.post(
                "/api/runs/reserve",
                headers={"Idempotency-Key": "selection-writer-quiescence"},
                json={
                    "brief": json.dumps({"mode": "ignore_term"}),
                    "artifact_type": "landing",
                },
            ).json()
            run_id = str(reserved["run_id"])
            started = self.client.post(
                f"/api/runs/{run_id}/start",
                headers={"X-AutoDesign-Upload-Token": reserved["upload_token"]},
            )
            self.assertEqual(started.status_code, 200, started.text)

            async def install_delayed_selection_writer():
                release = asyncio.Event()

                async def delayed_writer() -> None:
                    await release.wait()

                task = asyncio.create_task(delayed_writer())
                web_server._ATTEMPT_SELECTION_TASKS[run_id] = task
                return release, task

            release, selection_task = self.client.portal.call(
                install_delayed_selection_writer
            )
            with ThreadPoolExecutor(max_workers=1) as pool:
                cancellation = pool.submit(
                    self.client.post,
                    f"/api/runs/{run_id}/cancel",
                )
                time.sleep(0.35)
                waited_for_writer = not cancellation.done()
                self.client.portal.call(release.set)
                response = cancellation.result(timeout=5.0)

            async def cleanup_selection_writer() -> None:
                if web_server._ATTEMPT_SELECTION_TASKS.get(run_id) is selection_task:
                    web_server._ATTEMPT_SELECTION_TASKS.pop(run_id, None)

            self.client.portal.call(cleanup_selection_writer)

        self.assertTrue(waited_for_writer)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["confirmed"])
        self.assertTrue(selection_task.done())

    def test_concurrent_start_creates_exactly_one_worker(self) -> None:
        self._use_fixture_supervisor()
        with (
            patch.object(web_server, "_apply_type_prologue", side_effect=lambda brief, _kind: brief),
            patch.object(
                web_server,
                "_apply_conversation_prologue",
                side_effect=lambda brief, **_kwargs: brief,
            ),
        ):
            reserved = self.client.post(
                "/api/runs/reserve",
                headers={"Idempotency-Key": "concurrent-start"},
                json={
                    "brief": json.dumps({"mode": "ignore_term"}),
                    "artifact_type": "landing",
                },
            ).json()
            run_id = str(reserved["run_id"])
            headers = {"X-AutoDesign-Upload-Token": reserved["upload_token"]}
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
            self.assertEqual(
                [response.status_code for response in responses],
                [200, 200],
                [response.text for response in responses],
            )
            roots = [
                record
                for record in ProcessLedger(self.runs_dir / run_id).read().processes
                if record.role == "root-worker"
            ]
            self.assertEqual(len(roots), 1)
            cancelled = self.client.post(f"/api/runs/{run_id}/cancel")
            self.assertEqual(cancelled.status_code, 200, cancelled.text)

    def test_second_web_server_process_is_rejected_by_singleton_lock(self) -> None:
        script = """
from fastapi.testclient import TestClient
from pathlib import Path
import sys
import scripts.web_server as web_server

out_dir = Path(sys.argv[1])
web_server._BOOT_OUT_DIR = out_dir
web_server.RUNS_DIR = out_dir / "runs"
web_server.RUNS_DIR.mkdir(parents=True, exist_ok=True)
with TestClient(web_server.app) as client:
    client.get("/api/health").raise_for_status()
"""
        completed = subprocess.run(
            [sys.executable, "-c", script, str(self.out_dir)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "another AutoDesign Web server already owns",
            completed.stderr,
        )


class WebRunCompletionOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmp.name) / "out"
        self.runs_dir = self.out_dir / "runs"
        self.runs_dir.mkdir(parents=True)
        self.store = RunControlStore(self.runs_dir)
        self.run_id = "completion-ordering"
        record = self.store.reserve(self.run_id, "landing")
        record = self.store.transition(self.run_id, record, "queued")
        record = self.store.transition(self.run_id, record, "running")
        self.store.transition(self.run_id, record, "completing")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_result_state_is_ready_before_terminal_event_is_accepted(self) -> None:
        run_dir = self.runs_dir / self.run_id
        artifact = web_server.Artifact(
            artifact_id=f"art_{self.run_id}",
            name="Landing",
            artifact_type="landing",
            canvas=web_server.Canvas(w=1440, h=900),
            native_file_url=f"/api/files/runs/{self.run_id}/final/index.html",
            native_format="html",
        )
        state = web_server._RunState(
            artifact_type="landing",
            brief="Make a landing page",
            conversation_id=f"run:{self.run_id}",
        )
        outcome = web_server.WorkerOutcome(
            run_id=self.run_id,
            job_kind="pipeline",
            returncode=0,
            ok=True,
            result={
                "run_id": self.run_id,
                "run_dir": str(run_dir),
                "artifact_type": "landing",
                "terminal_status": "pass",
            },
            error=None,
            relayed_events=0,
        )

        class _OrderingSupervisor:
            ready = False

            async def accept_completion(inner_self, run_id: str, **_kwargs: object):
                self.assertEqual(run_id, self.run_id)
                inner_self.ready = (
                    state.result_artifact is artifact
                    and state.result_message is not None
                    and state.result_message.status == "done"
                )
                current = self.store.read(run_id)
                return self.store.transition(
                    run_id,
                    current,
                    "completed",
                    publishable=True,
                    terminal_event="run.done",
                )

        runtime = SimpleNamespace(
            control_store=self.store,
            supervisor=_OrderingSupervisor(),
        )
        with (
            patch.object(web_server, "RUNS_DIR", self.runs_dir),
            patch.object(web_server, "_web_run_runtime", return_value=runtime),
            patch.object(web_server, "_build_artifact_response", return_value=artifact),
            patch.object(web_server, "_should_publish_artifact", return_value=True),
            patch.object(web_server, "_append_event"),
        ):
            import asyncio

            asyncio.run(
                web_server._monitor_supervised_pipeline(
                    run_id=self.run_id,
                    state=state,
                    recovered_outcome=outcome,
                )
            )
        self.assertTrue(runtime.supervisor.ready)

    def test_pipeline_worker_exit_populates_same_process_failure_fields(self) -> None:
        state = web_server._RunState(
            artifact_type="landing",
            brief="Make a landing page",
            conversation_id=f"run:{self.run_id}",
        )
        diagnostic = WorkerExitDiagnostic(
            version=1,
            returncode=17,
            error_code="worker_result_missing",
            error_message="Worker stopped before writing its result.",
            error_detail="exit=17; final-root-cause",
            protocol_error="worker_result.json is missing",
            last_event="landing.author",
            last_worker_seq=3,
            last_phase="authoring",
            last_reason="process_exit",
        )
        outcome = web_server.WorkerOutcome(
            run_id=self.run_id,
            job_kind="pipeline",
            returncode=17,
            ok=False,
            result=None,
            error=diagnostic.error_message,
            relayed_events=3,
            exit_diagnostic=diagnostic,
        )

        class _FailedSupervisor:
            async def accept_completion(inner_self, run_id: str, **_kwargs: object):
                current = self.store.read(run_id)
                return self.store.transition(
                    run_id,
                    current,
                    "failed",
                    publishable=False,
                    terminal_event="run.error",
                )

        runtime = SimpleNamespace(
            control_store=self.store,
            supervisor=_FailedSupervisor(),
        )
        with (
            patch.object(web_server, "RUNS_DIR", self.runs_dir),
            patch.object(web_server, "_web_run_runtime", return_value=runtime),
            patch.object(web_server, "_append_event"),
        ):
            asyncio.run(
                web_server._monitor_supervised_pipeline(
                    run_id=self.run_id,
                    state=state,
                    recovered_outcome=outcome,
                )
            )

        self.assertIsNotNone(state.result_message)
        assert state.result_message is not None
        failure = state.result_message.failure
        self.assertIsNotNone(failure)
        assert failure is not None
        self.assertEqual(failure.phase, "authoring")
        self.assertEqual(failure.error_code, "worker_result_missing")
        self.assertEqual(failure.error_message, diagnostic.error_message)
        self.assertEqual(failure.error_detail, diagnostic.error_detail)

    def test_cancel_winning_completion_race_does_not_promote_reference(self) -> None:
        run_dir = self.runs_dir / self.run_id
        reference_path = run_dir / "uploads" / "reference-poster.png"
        reference_path.parent.mkdir(parents=True)
        reference_path.write_bytes(b"reference")
        artifact = web_server.Artifact(
            artifact_id=f"art_{self.run_id}",
            name="Landing",
            artifact_type="landing",
            canvas=web_server.Canvas(w=1440, h=900),
            native_file_url=f"/api/files/runs/{self.run_id}/final/index.html",
            native_format="html",
        )
        state = web_server._RunState(
            artifact_type="landing",
            brief="Make a landing page",
            conversation_id=f"run:{self.run_id}",
            reference_poster_path=reference_path,
        )
        state.reference_poster_handle = "ref_cancelled"
        outcome = web_server.WorkerOutcome(
            run_id=self.run_id,
            job_kind="pipeline",
            returncode=0,
            ok=True,
            result={
                "run_id": self.run_id,
                "run_dir": str(run_dir),
                "artifact_type": "landing",
                "terminal_status": "pass",
            },
            error=None,
            relayed_events=0,
        )

        class _CancellationWinsSupervisor:
            async def accept_completion(inner_self, run_id: str, **_kwargs: object):
                self.assertEqual(run_id, self.run_id)
                return SimpleNamespace(state="cancelled")

        runtime = SimpleNamespace(
            control_store=self.store,
            supervisor=_CancellationWinsSupervisor(),
        )
        with (
            patch.object(web_server, "RUNS_DIR", self.runs_dir),
            patch.object(web_server, "_web_run_runtime", return_value=runtime),
            patch.object(web_server, "_build_artifact_response", return_value=artifact),
            patch.object(web_server, "_should_publish_artifact", return_value=True),
            patch.object(web_server, "_promote_completed_run_reference_poster") as promote,
            patch.object(web_server, "_append_event"),
        ):
            import asyncio

            asyncio.run(
                web_server._monitor_supervised_pipeline(
                    run_id=self.run_id,
                    state=state,
                    recovered_outcome=outcome,
                )
            )

        promote.assert_not_called()
        self.assertIsNone(state.result_artifact)


class WebRunShutdownTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmp.name) / "out"
        self.runs_dir = self.out_dir / "runs"
        self.runs_dir.mkdir(parents=True)
        self.store = RunControlStore(self.runs_dir)
        self.run_id = "shutdown-pending"
        self.store.reserve(self.run_id, "landing")
        self.cancel_calls = 0

        owner = self

        class _Supervisor:
            def active_run_ids(inner_self) -> tuple[str, ...]:
                record = owner.store.read(owner.run_id)
                return () if record.state == "cancelled" else (owner.run_id,)

            async def recover(inner_self, _run_id: str) -> None:
                return None

        class _Services:
            async def cancel(inner_self, run_id: str, _reason: str):
                owner.cancel_calls += 1
                if owner.cancel_calls == 1:
                    owner.store.finalize_cancel(
                        run_id,
                        {
                            "termination_verified": False,
                            "cancellation_pending": "worker_monitor_not_joined",
                        },
                    )
                    return SimpleNamespace(
                        state="cancelling",
                        confirmed=False,
                        cancel_request_event_required=True,
                        terminated_pids=(),
                        surviving_pids=(9012,),
                    )
                owner.store.finalize_cancel(
                    run_id,
                    {"termination_verified": True, "reason": "test"},
                )
                return SimpleNamespace(
                    state="cancelled",
                    confirmed=True,
                    cancel_request_event_required=False,
                    terminated_pids=(9012,),
                    surviving_pids=(),
                )

        self.runtime = SimpleNamespace(
            control_store=self.store,
            supervisor=_Supervisor(),
            services=_Services(),
        )
        self._patches = (
            patch.object(web_server, "RUNS_DIR", self.runs_dir),
            patch.object(web_server, "_web_run_runtime", return_value=self.runtime),
        )
        for current in self._patches:
            current.start()

    def tearDown(self) -> None:
        for current in reversed(self._patches):
            current.stop()
        self._tmp.cleanup()

    def test_shutdown_retries_cancellation_pending_until_terminal(self) -> None:
        import asyncio

        asyncio.run(
            web_server._shutdown_supervised_runs(timeout_s=0.2, poll_s=0.001)
        )

        self.assertEqual(self.store.read(self.run_id).state, "cancelled")
        self.assertEqual(self.cancel_calls, 2)
        events = [
            json.loads(line)
            for line in (self.runs_dir / self.run_id / "run_events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(
            sum(event.get("event") == "run.cancel_requested" for event in events),
            1,
        )

    def test_shutdown_timeout_persists_pending_and_does_not_unlock(self) -> None:
        import asyncio

        async def never_confirm(run_id: str, _reason: str):
            self.cancel_calls += 1
            self.store.finalize_cancel(
                run_id,
                {
                    "termination_verified": False,
                    "cancellation_pending": "managed_process_survived",
                },
            )
            return SimpleNamespace(
                state="cancelling",
                confirmed=False,
                cancel_request_event_required=self.cancel_calls == 1,
                terminated_pids=(),
                surviving_pids=(9012,),
            )

        self.runtime.services.cancel = never_confirm
        previous_task = web_server._RUN_LIFECYCLE_TASK
        web_server._RUN_LIFECYCLE_TASK = None
        try:
            with (
                patch.object(web_server, "_WEB_RUN_SHUTDOWN_TIMEOUT_S", 0.01),
                patch.object(web_server, "_WEB_RUN_SHUTDOWN_POLL_S", 0.001),
                patch.object(web_server, "_release_web_server_singleton_lock") as release,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "shutdown could not verify cancellation",
                ):
                    asyncio.run(web_server._shutdown_run_supervision())
                release.assert_not_called()
        finally:
            web_server._RUN_LIFECYCLE_TASK = previous_task

        record = self.store.read(self.run_id)
        self.assertEqual(record.state, "cancelling")
        self.assertEqual(record.cancellation_pending, "managed_process_survived")


class WebRunShutdownProcessRaceTests(unittest.TestCase):
    def test_shutdown_propagates_caller_cancellation(self) -> None:
        import asyncio

        async def scenario() -> bool:
            class _Supervisor:
                @staticmethod
                def active_run_ids() -> tuple[str, ...]:
                    return ()

            runs_dir = Path(self._tmp.name) / "empty-runs"
            runtime = SimpleNamespace(
                control_store=RunControlStore(runs_dir),
                supervisor=_Supervisor(),
            )
            entered = asyncio.Event()
            calls = 0

            async def blocked_demo_stop() -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    entered.set()
                    await asyncio.Event().wait()

            web_server._open_web_run_start_gate()
            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "_web_run_runtime", return_value=runtime),
                patch.object(
                    web_server,
                    "_stop_demo_run_queue",
                    side_effect=blocked_demo_stop,
                ),
            ):
                shutdown_task = asyncio.create_task(
                    web_server._shutdown_supervised_runs(
                        timeout_s=0.2,
                        poll_s=0.001,
                    )
                )
                await asyncio.wait_for(entered.wait(), timeout=1.0)
                shutdown_task.cancel()
                try:
                    await shutdown_task
                except asyncio.CancelledError:
                    return True
                return False

        self._tmp = tempfile.TemporaryDirectory()
        try:
            propagated = asyncio.run(scenario())
        finally:
            self._tmp.cleanup()

        self.assertTrue(propagated)

    def test_shutdown_barrier_rejects_late_demo_enqueue(self) -> None:
        import asyncio

        async def scenario() -> bool:
            class _Supervisor:
                @staticmethod
                def active_run_ids() -> tuple[str, ...]:
                    return ()

            runs_dir = Path(self._tmp.name) / "empty-runs"
            runtime = SimpleNamespace(
                control_store=RunControlStore(runs_dir),
                supervisor=_Supervisor(),
            )
            previous_queue = web_server._DEMO_RUN_QUEUE
            previous_workers = web_server._DEMO_WORKERS[:]
            web_server._DEMO_RUN_QUEUE = None
            web_server._DEMO_WORKERS.clear()
            web_server._open_web_run_start_gate()
            try:
                with (
                    patch.object(web_server, "RUNS_DIR", runs_dir),
                    patch.object(web_server, "_web_run_runtime", return_value=runtime),
                ):
                    await web_server._shutdown_supervised_runs(
                        timeout_s=0.2,
                        poll_s=0.001,
                    )
                    try:
                        await web_server._enqueue_demo_run(
                            SimpleNamespace(run_id="late-demo")
                        )
                    except web_server.RunNotReady:
                        return True
                    return False
            finally:
                web_server._DEMO_RUN_QUEUE = previous_queue
                web_server._DEMO_WORKERS[:] = previous_workers

        self._tmp = tempfile.TemporaryDirectory()
        try:
            rejected = asyncio.run(scenario())
        finally:
            self._tmp.cleanup()

        self.assertTrue(rejected)

    def test_shutdown_stops_demo_background_workers_and_drains_queue(self) -> None:
        import asyncio

        async def scenario() -> tuple[bool, int, int]:
            class _Supervisor:
                @staticmethod
                def active_run_ids() -> tuple[str, ...]:
                    return ()

            runtime = SimpleNamespace(
                control_store=RunControlStore(Path(self._tmp.name) / "empty-runs"),
                supervisor=_Supervisor(),
            )
            queue: asyncio.Queue[object] = asyncio.Queue()
            queue.put_nowait(object())
            worker = asyncio.create_task(asyncio.Event().wait())
            previous_queue = web_server._DEMO_RUN_QUEUE
            previous_workers = web_server._DEMO_WORKERS[:]
            web_server._DEMO_RUN_QUEUE = queue
            web_server._DEMO_WORKERS[:] = [worker]
            try:
                with (
                    patch.object(web_server, "RUNS_DIR", runtime.control_store.runs_dir),
                    patch.object(web_server, "_web_run_runtime", return_value=runtime),
                ):
                    await web_server._shutdown_supervised_runs(
                        timeout_s=0.2,
                        poll_s=0.001,
                    )
                return worker.done(), queue.qsize(), len(web_server._DEMO_WORKERS)
            finally:
                if not worker.done():
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                web_server._DEMO_RUN_QUEUE = previous_queue
                web_server._DEMO_WORKERS[:] = previous_workers

        self._tmp = tempfile.TemporaryDirectory()
        try:
            worker_done, queue_size, registered_workers = asyncio.run(scenario())
        finally:
            self._tmp.cleanup()

        self.assertTrue(worker_done)
        self.assertEqual(queue_size, 0)
        self.assertEqual(registered_workers, 0)

    def test_shutdown_stops_demo_worker_while_it_is_monitoring_a_run(self) -> None:
        import asyncio

        async def scenario() -> tuple[bool, int, str | None]:
            class _Supervisor:
                @staticmethod
                def active_run_ids() -> tuple[str, ...]:
                    return ()

            runs_dir = Path(self._tmp.name) / "empty-runs"
            runtime = SimpleNamespace(
                control_store=RunControlStore(runs_dir),
                supervisor=_Supervisor(),
            )
            state = SimpleNamespace(
                queued=True,
                cancelled=False,
                task=None,
                reference_poster_path=None,
            )
            job = SimpleNamespace(
                run_id="demo-monitoring",
                brief="blocked",
                attach_paths=[],
                template=None,
                state=state,
                settings=None,
            )
            queue: asyncio.Queue[object] = asyncio.Queue()
            queue.put_nowait(job)
            monitor_started = asyncio.Event()

            async def monitor(**_kwargs: object) -> None:
                monitor_started.set()
                await asyncio.Event().wait()

            previous_queue = web_server._DEMO_RUN_QUEUE
            previous_workers = web_server._DEMO_WORKERS[:]
            web_server._DEMO_RUN_QUEUE = queue
            worker = asyncio.create_task(web_server._demo_queue_worker(1))
            web_server._DEMO_WORKERS[:] = [worker]
            shutdown_error = None
            try:
                with (
                    patch.object(web_server, "RUNS_DIR", runs_dir),
                    patch.object(web_server, "_web_run_runtime", return_value=runtime),
                    patch.object(
                        web_server,
                        "_start_legacy_pipeline_worker",
                        new=AsyncMock(return_value=None),
                    ),
                    patch.object(
                        web_server,
                        "_monitor_supervised_pipeline",
                        side_effect=monitor,
                    ),
                ):
                    await asyncio.wait_for(monitor_started.wait(), timeout=1.0)
                    try:
                        await web_server._shutdown_supervised_runs(
                            timeout_s=0.2,
                            poll_s=0.001,
                        )
                    except RuntimeError as exc:
                        shutdown_error = str(exc)
                return worker.done(), len(web_server._DEMO_WORKERS), shutdown_error
            finally:
                if not worker.done():
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                web_server._DEMO_RUN_QUEUE = previous_queue
                web_server._DEMO_WORKERS[:] = previous_workers

        self._tmp = tempfile.TemporaryDirectory()
        try:
            worker_done, registered_workers, shutdown_error = asyncio.run(scenario())
        finally:
            self._tmp.cleanup()

        self.assertTrue(worker_done)
        self.assertEqual(registered_workers, 0)
        self.assertIsNone(shutdown_error)

    def test_shutdown_cancels_queued_start_before_active_registration(self) -> None:
        script = r'''
import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from autodesign.config import Settings
from autodesign.process_supervision import ProcessLedger, process_is_alive
from autodesign.run_control import RunControlStore
from autodesign.run_supervisor import RunSupervisor
from autodesign.web_run_services import WebRunServices
import scripts.web_server as web_server


async def main() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        out_dir = Path(temporary_directory) / "out"
        runs_dir = out_dir / "runs"
        store = RunControlStore(runs_dir)
        spawned = []
        supervisor = RunSupervisor(
            runs_dir,
            control_store=store,
            worker_command=(
                sys.executable,
                str(Path("tests/fixtures/cancellation_worker.py").resolve()),
            ),
            root_registration_delay_s=0.25,
            root_registration_hook=spawned.append,
            grace_s=0.1,
        )
        runtime = web_server._WebRunRuntime(
            runs_dir=runs_dir.resolve(),
            control_store=store,
            supervisor=supervisor,
            services=WebRunServices(
                runs_dir,
                control_store=store,
                supervisor=supervisor,
            ),
        )
        settings = Settings(
            anthropic_api_key="",
            anthropic_base_url=None,
            gemini_api_key="",
            designer_model="designer",
            critic_model="critic",
            repo_root=Path.cwd(),
            out_dir=out_dir,
        )
        run_id = "shutdown-queued-start"
        state = web_server._RunState(
            artifact_type="landing",
            brief="blocked",
            conversation_id=f"run:{run_id}",
        )
        start_task = None
        observed = None
        with (
            patch.object(web_server, "RUNS_DIR", runs_dir),
            patch.object(web_server, "_web_run_runtime", return_value=runtime),
        ):
            try:
                await web_server._reserve_legacy_pipeline_worker(
                    run_id=run_id,
                    brief="blocked",
                    attach_paths=[],
                    reference_poster_path=None,
                    template=None,
                    state=state,
                    settings=settings,
                    resume_run=None,
                )
                start_task = asyncio.create_task(
                    web_server._start_legacy_pipeline_worker(
                        run_id=run_id,
                        brief="blocked",
                        attach_paths=[],
                        reference_poster_path=None,
                        template=None,
                        state=state,
                        settings=settings,
                        resume_run=None,
                    )
                )
                await asyncio.sleep(0.03)
                assert store.read(run_id).state == "queued"
                assert supervisor.active_run_ids() == ()

                await web_server._shutdown_supervised_runs(
                    timeout_s=3.0,
                    poll_s=0.01,
                )
                await asyncio.gather(start_task, return_exceptions=True)
                identities = tuple(
                    item.identity
                    for item in ProcessLedger(runs_dir / run_id).read().processes
                )
                observed = {
                    "state": store.read(run_id).state,
                    "active": supervisor.active_run_ids(),
                    "spawned": tuple(spawned),
                    "alive": tuple(
                        identity.pid
                        for identity in identities
                        if process_is_alive(identity)
                    ),
                }
            finally:
                if start_task is not None and not start_task.done():
                    await asyncio.gather(start_task, return_exceptions=True)
                if supervisor.active_run_ids():
                    await supervisor.cancel(run_id, "test_cleanup")

        assert observed is not None
        assert observed["state"] == "cancelled", observed
        assert observed["active"] == (), observed
        assert observed["spawned"], observed
        assert observed["alive"] == (), observed


asyncio.run(main())
'''
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

        self.assertEqual(
            completed.returncode,
            0,
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
