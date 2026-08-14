from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from autodesign.config import Settings
from autodesign.process_supervision import (
    ProcessLedger,
    process_identity,
    process_is_alive,
    terminate_process_identities,
)
from autodesign.run_control import RunControlStore
from autodesign.run_supervisor import RunSupervisor
from autodesign.web_run_services import WebRunServices
import scripts.web_server as web_server


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKER_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "cancellation_worker.py"
ARTIFACT_TYPES = ("poster", "deck", "landing", "video")


class WebPaperBundleCancellationTests(unittest.TestCase):
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
            patch.object(web_server, "_require_artifact_runtime"),
            patch.object(
                web_server,
                "_paper_poster_author_cmd_resolution",
                return_value={
                    "available": True,
                    "cmd": "fixture",
                    "command": "fixture",
                    "message": "",
                },
            ),
            patch.object(
                web_server,
                "_validated_web_palette_id",
                side_effect=lambda artifact_type, value: (
                    "royal_blue" if artifact_type == "poster" else None
                ),
            ),
        )
        for current in self._patches:
            current.start()
        web_server._reset_web_run_runtime_for_tests()
        if hasattr(web_server, "_reset_paper_bundle_store_for_tests"):
            web_server._reset_paper_bundle_store_for_tests()
        store = RunControlStore(self.runs_dir)
        supervisor = RunSupervisor(
            self.runs_dir,
            control_store=store,
            worker_command=(sys.executable, str(WORKER_FIXTURE)),
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
            if (self.runs_dir / run_id / "run_control.json").is_file():
                try:
                    self.client.post(f"/api/runs/{run_id}/cancel")
                except BaseException:
                    pass
        self._client_context.__exit__(None, None, None)
        web_server._reset_web_run_runtime_for_tests()
        if hasattr(web_server, "_reset_paper_bundle_store_for_tests"):
            web_server._reset_paper_bundle_store_for_tests()
        for current in reversed(self._patches):
            current.stop()
        self._tmp.cleanup()

    @staticmethod
    def _source() -> bytes:
        return b"paper-pdf"

    def _create_payload(self, job_id: str = "paper-bundle-test") -> dict[str, object]:
        source = self._source()
        digest = hashlib.sha256(source).hexdigest()
        children = {
            artifact_type: {
                "brief": "{\"mode\":\"idle\"}",
                "artifact_type": artifact_type,
                "conversation_id": f"conversation:{artifact_type}",
                "template": "cvpr-landscape" if artifact_type == "poster" else None,
                "palette_id": "royal_blue" if artifact_type == "poster" else None,
                "authoring_max_attempts": 4,
                "input_slots": [
                    {
                        "name": "paper.pdf",
                        "role": "attachment",
                        "sha256": digest,
                        "size": len(source),
                    }
                ],
            }
            for artifact_type in ARTIFACT_TYPES
        }
        return {
            "job_id": job_id,
            "conversation_id": "conversation",
            "source_name": "paper.pdf",
            "prompt_version": "paper-bundle-v1",
            "children": children,
        }

    def _create_bundle(self, job_id: str = "paper-bundle-test"):
        return self.client.post(
            "/api/paper-bundles",
            headers={"Idempotency-Key": f"create:{job_id}"},
            json=self._create_payload(job_id),
        )

    def test_create_reserves_four_children_before_upload(self) -> None:
        response = self._create_bundle()

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["job_id"], "paper-bundle-test")
        self.assertEqual(set(payload["children"]), set(ARTIFACT_TYPES))
        self.assertEqual(payload["state"], "reserved")
        for artifact_type in ARTIFACT_TYPES:
            child = payload["children"][artifact_type]
            self.assertEqual(child["artifact_type"], artifact_type)
            self.assertTrue(child["upload_token"])
            self.assertEqual(child["input_slots"][0]["name"], "paper.pdf")
            control = RunControlStore(self.runs_dir).read(child["run_id"])
            self.assertEqual(control.parent_job_id, "paper-bundle-test")
            self.assertEqual(control.state, "reserved")

    def test_create_retry_returns_same_children_and_upload_tokens(self) -> None:
        first = self._create_bundle("paper-bundle-replay")
        second = self._create_bundle("paper-bundle-replay")

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        first_payload = first.json()
        second_payload = second.json()
        self.assertFalse(first_payload["reused"])
        self.assertTrue(second_payload["reused"])
        for artifact_type in ARTIFACT_TYPES:
            self.assertEqual(
                second_payload["children"][artifact_type]["run_id"],
                first_payload["children"][artifact_type]["run_id"],
            )
            self.assertEqual(
                second_payload["children"][artifact_type]["upload_token"],
                first_payload["children"][artifact_type]["upload_token"],
            )

    def test_cancel_all_confirms_only_after_reserved_children_are_cancelled(self) -> None:
        created = self._create_bundle("paper-bundle-cancel")
        self.assertEqual(created.status_code, 200, created.text)

        cancelled = self.client.post("/api/paper-bundles/paper-bundle-cancel/cancel")

        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        payload = cancelled.json()
        self.assertTrue(payload["confirmed"])
        self.assertEqual(payload["state"], "cancelled")
        for child in payload["children"].values():
            control = RunControlStore(self.runs_dir).read(child["run_id"])
            self.assertEqual(control.state, "cancelled")

    def test_child_snapshot_requires_durable_process_quiescence(self) -> None:
        run_id = "paper-bundle-terminal-live-worker"
        controls = RunControlStore(self.runs_dir)
        record = controls.reserve(run_id, "video", parent_job_id="paper-bundle")
        record = controls.transition(run_id, record, "queued")
        controls.transition(run_id, record, "failed", publishable=False)
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        identity = process_identity(process.pid)
        nonce = "paper-bundle-live-worker"
        ProcessLedger(self.runs_dir / run_id).register_existing(
            identity,
            role="root-worker",
            nonce=nonce,
        )

        try:
            snapshot = web_server._paper_bundle_child_snapshot(run_id)

            self.assertTrue(snapshot.terminal)
            self.assertFalse(snapshot.process_free)
        finally:
            if process_is_alive(identity):
                terminate_process_identities(
                    (identity,),
                    root_pid=identity.pid,
                    grace_s=0.05,
                    owner_nonces=(nonce,),
                )
            process.wait(timeout=2)

    def test_cancel_all_cancels_reserved_uploading_queued_and_running_children(self) -> None:
        created = self._create_bundle("paper-bundle-mixed")
        self.assertEqual(created.status_code, 200, created.text)
        children = created.json()["children"]
        for artifact_type in ("poster", "deck"):
            child = children[artifact_type]
            uploaded = self.client.put(
                f"/api/runs/{child['run_id']}/inputs/paper.pdf",
                headers={"X-Autodesign-Upload-Token": child["upload_token"]},
                content=self._source(),
            )
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
        poster = children["poster"]
        started = self.client.post(
            f"/api/runs/{poster['run_id']}/start",
            headers={"X-Autodesign-Upload-Token": poster["upload_token"]},
        )
        self.assertEqual(started.status_code, 200, started.text)
        video = children["video"]
        controls = RunControlStore(self.runs_dir)
        video_record = controls.read(video["run_id"])
        controls.transition(video["run_id"], video_record, "uploading")

        cancelled = self.client.post("/api/paper-bundles/paper-bundle-mixed/cancel")

        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertTrue(cancelled.json()["confirmed"])
        for child in children.values():
            self.assertEqual(controls.read(child["run_id"]).state, "cancelled")
            self.assertNotIn(
                child["run_id"],
                web_server._web_run_runtime().supervisor.active_run_ids(),
            )

    def test_late_child_upload_after_parent_cancel_never_starts(self) -> None:
        created = self._create_bundle("paper-bundle-late-upload")
        self.assertEqual(created.status_code, 200, created.text)
        child = created.json()["children"]["video"]
        cancelled = self.client.post(
            "/api/paper-bundles/paper-bundle-late-upload/cancel"
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)

        uploaded = self.client.put(
            f"/api/runs/{child['run_id']}/inputs/paper.pdf",
            headers={"X-Autodesign-Upload-Token": child["upload_token"]},
            content=self._source(),
        )

        self.assertEqual(uploaded.status_code, 409, uploaded.text)
        self.assertEqual(
            RunControlStore(self.runs_dir).read(child["run_id"]).state,
            "cancelled",
        )
        self.assertIsNone(
            RunControlStore(self.runs_dir).read(child["run_id"]).worker_pid
        )

    def test_closed_parent_barrier_rejects_upload_before_child_cancel_dispatch(self) -> None:
        created = self._create_bundle("paper-bundle-upload-barrier")
        self.assertEqual(created.status_code, 200, created.text)
        child = created.json()["children"]["landing"]
        web_server._paper_bundle_store().request_cancel(
            "paper-bundle-upload-barrier",
            "local",
        )

        uploaded = self.client.put(
            f"/api/runs/{child['run_id']}/inputs/paper.pdf",
            headers={"X-Autodesign-Upload-Token": child["upload_token"]},
            content=self._source(),
        )

        self.assertEqual(uploaded.status_code, 409, uploaded.text)
        self.assertEqual(
            RunControlStore(self.runs_dir).read(child["run_id"]).state,
            "reserved",
        )

    def test_closed_parent_barrier_rejects_child_start_without_spawning(self) -> None:
        created = self._create_bundle("paper-bundle-start-barrier")
        self.assertEqual(created.status_code, 200, created.text)
        child = created.json()["children"]["deck"]
        uploaded = self.client.put(
            f"/api/runs/{child['run_id']}/inputs/paper.pdf",
            headers={"X-Autodesign-Upload-Token": child["upload_token"]},
            content=self._source(),
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        web_server._paper_bundle_store().request_cancel(
            "paper-bundle-start-barrier",
            "local",
        )

        started = self.client.post(
            f"/api/runs/{child['run_id']}/start",
            headers={"X-Autodesign-Upload-Token": child["upload_token"]},
        )

        self.assertEqual(started.status_code, 409, started.text)
        control = RunControlStore(self.runs_dir).read(child["run_id"])
        self.assertEqual(control.state, "queued")
        self.assertIsNone(control.worker_pid)

    def test_parent_cancel_waits_for_committed_child_start_to_abort(self) -> None:
        created = self._create_bundle("paper-bundle-start-race")
        self.assertEqual(created.status_code, 200, created.text)
        child = created.json()["children"]["poster"]
        uploaded = self.client.put(
            f"/api/runs/{child['run_id']}/inputs/paper.pdf",
            headers={"X-Autodesign-Upload-Token": child["upload_token"]},
            content=self._source(),
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        web_server._web_run_runtime().supervisor._root_registration_delay_s = 0.2

        with ThreadPoolExecutor(max_workers=2) as executor:
            start_future = executor.submit(
                self.client.post,
                f"/api/runs/{child['run_id']}/start",
                headers={"X-Autodesign-Upload-Token": child["upload_token"]},
            )
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                intents = web_server._paper_bundle_store().pending_child_start_intents(
                    "paper-bundle-start-race",
                    "local",
                )
                if intents:
                    break
                time.sleep(0.01)
            else:
                self.fail("child start never reached its committed barrier")
            cancel_response = self.client.post(
                "/api/paper-bundles/paper-bundle-start-race/cancel"
            )
            start_response = start_future.result(timeout=5)

        self.assertEqual(start_response.status_code, 409, start_response.text)
        self.assertEqual(cancel_response.status_code, 200, cancel_response.text)
        self.assertTrue(cancel_response.json()["confirmed"])
        control = RunControlStore(self.runs_dir).read(child["run_id"])
        self.assertEqual(control.state, "cancelled")
        self.assertNotIn(
            child["run_id"],
            web_server._web_run_runtime().supervisor.active_run_ids(),
        )

    def test_cancel_known_parent_id_during_creation_blocks_late_children(self) -> None:
        runtime = web_server._web_run_runtime()
        original_reserve = runtime.services.reserve
        entered = threading.Event()
        release = threading.Event()

        async def blocked_reserve(**kwargs):
            entered.set()
            await asyncio.to_thread(release.wait)
            return await original_reserve(**kwargs)

        with (
            patch.object(runtime.services, "reserve", side_effect=blocked_reserve),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            create_future = executor.submit(
                self._create_bundle,
                "paper-bundle-create-cancel",
            )
            self.assertTrue(entered.wait(timeout=2))
            cancel_future = executor.submit(
                self.client.post,
                "/api/paper-bundles/paper-bundle-create-cancel/cancel",
            )
            time.sleep(0.05)
            release.set()
            created = create_future.result(timeout=5)
            cancelled = cancel_future.result(timeout=5)

        self.assertIn(created.status_code, {409, 422}, created.text)
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertTrue(cancelled.json()["confirmed"])
        self.assertEqual(cancelled.json()["state"], "cancelled")
        self.assertEqual(cancelled.json()["owner_id"], "local")
        self.assertTrue(cancelled.json()["pending_creation"])
        self.assertTrue(cancelled.json()["factory_quiesced"])
        fetched = self.client.get(
            "/api/paper-bundles/paper-bundle-create-cancel"
        )
        self.assertEqual(fetched.status_code, 404, fetched.text)
        for control_path in self.runs_dir.glob(
            "paper-bundle-create-cancel-*/run_control.json"
        ):
            record = RunControlStore(self.runs_dir).read(control_path.parent.name)
            self.assertEqual(record.state, "cancelled")
            self.assertNotIn(
                record.run_id,
                runtime.supervisor.active_run_ids(),
            )

    def test_shutdown_waits_for_bundle_factory_before_first_child_reserve(self) -> None:
        runtime = web_server._web_run_runtime()
        original_reserve = runtime.services.reserve
        original_cancel = web_server._cancel_controlled_run
        entered = threading.Event()
        release = self.client.portal.call(asyncio.Event)
        cancellation_targets: list[str] = []

        async def blocked_reserve(**kwargs):
            entered.set()
            await release.wait()
            return await original_reserve(**kwargs)

        async def shutdown() -> None:
            await web_server._shutdown_supervised_runs(
                timeout_s=3.0,
                poll_s=0.01,
            )

        async def tracked_cancel(run_id: str, reason: str):
            cancellation_targets.append(run_id)
            return await original_cancel(run_id, reason)

        created = None
        states_after_shutdown: dict[str, str] = {}
        with (
            patch.object(runtime.services, "reserve", side_effect=blocked_reserve),
            patch.object(
                web_server,
                "_cancel_controlled_run",
                side_effect=tracked_cancel,
            ),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                create_future = executor.submit(
                    self._create_bundle,
                    "paper-bundle-shutdown-create",
                )
                self.assertTrue(entered.wait(timeout=2.0))
                stopping = executor.submit(self.client.portal.call, shutdown)
                time.sleep(0.1)
                waited_for_factory = not stopping.done()
                self.client.portal.call(release.set)
                created = create_future.result(timeout=5.0)
                stopping.result(timeout=5.0)

        self.assertIsNotNone(created)
        self.assertEqual(created.status_code, 200, created.text)
        children = created.json()["children"]
        controls = RunControlStore(self.runs_dir)
        for child in children.values():
            run_id = child["run_id"]
            states_after_shutdown[run_id] = controls.read(run_id).state
            if states_after_shutdown[run_id] not in {
                "completed",
                "failed",
                "cancelled",
            }:
                self.client.post(f"/api/runs/{run_id}/cancel")

        self.assertTrue(waited_for_factory)
        self.assertEqual(set(states_after_shutdown.values()), {"cancelled"})
        self.assertNotIn(
            "paper-bundle-create:paper-bundle-shutdown-create",
            cancellation_targets,
        )

    def test_get_and_list_redact_tokens_and_internal_start_intents(self) -> None:
        created = self._create_bundle("paper-bundle-redacted")
        self.assertEqual(created.status_code, 200, created.text)

        fetched = self.client.get("/api/paper-bundles/paper-bundle-redacted")
        listed = self.client.get("/api/paper-bundles")

        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json()), 1)
        for payload in (fetched.json(), listed.json()[0]):
            self.assertNotIn("start_intents", payload)
            self.assertNotIn("diagnostics", payload)
            for child in payload["children"].values():
                self.assertNotIn("upload_token", child)
                self.assertNotIn("diagnostic", child)

    def test_wrong_owner_observes_not_found_for_parent(self) -> None:
        with patch.object(web_server, "_run_owner_id", return_value="owner-a"):
            created = self._create_bundle("paper-bundle-owned")
        self.assertEqual(created.status_code, 200, created.text)

        with patch.object(web_server, "_run_owner_id", return_value="owner-b"):
            fetched = self.client.get("/api/paper-bundles/paper-bundle-owned")
            cancelled = self.client.post(
                "/api/paper-bundles/paper-bundle-owned/cancel"
            )

        self.assertEqual(fetched.status_code, 404, fetched.text)
        self.assertEqual(cancelled.status_code, 404, cancelled.text)

    def test_cancel_completed_bundle_is_idempotent_already_terminal(self) -> None:
        created = self._create_bundle("paper-bundle-completed")
        self.assertEqual(created.status_code, 200, created.text)
        controls = RunControlStore(self.runs_dir)
        for child in created.json()["children"].values():
            record = controls.read(child["run_id"])
            record = controls.transition(child["run_id"], record, "queued")
            record = controls.transition(child["run_id"], record, "running")
            record = controls.transition(child["run_id"], record, "completing")
            controls.transition(
                child["run_id"],
                record,
                "completed",
                publishable=True,
            )
        fetched = self.client.get("/api/paper-bundles/paper-bundle-completed")
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(fetched.json()["state"], "completed")

        cancelled = self.client.post(
            "/api/paper-bundles/paper-bundle-completed/cancel"
        )

        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertTrue(cancelled.json()["confirmed"])
        self.assertEqual(cancelled.json()["status"], "already_terminal")
        self.assertEqual(cancelled.json()["state"], "completed")
        for child in created.json()["children"].values():
            self.assertEqual(controls.read(child["run_id"]).state, "completed")

    def test_cancel_all_confirms_after_preserving_completed_child(self) -> None:
        created = self._create_bundle("paper-bundle-partial-cancel")
        self.assertEqual(created.status_code, 200, created.text)
        children = created.json()["children"]
        controls = RunControlStore(self.runs_dir)
        completed = children["poster"]
        record = controls.read(completed["run_id"])
        record = controls.transition(completed["run_id"], record, "queued")
        record = controls.transition(completed["run_id"], record, "running")
        record = controls.transition(completed["run_id"], record, "completing")
        controls.transition(
            completed["run_id"],
            record,
            "completed",
            publishable=True,
        )

        cancelled = self.client.post(
            "/api/paper-bundles/paper-bundle-partial-cancel/cancel"
        )

        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        payload = cancelled.json()
        self.assertTrue(payload["confirmed"])
        self.assertEqual(payload["status"], "cancelled")
        self.assertEqual(payload["state"], "cancelled")
        self.assertEqual(payload["completed_children"], ["poster"])
        self.assertEqual(controls.read(completed["run_id"]).state, "completed")
        for artifact_type in ("deck", "landing", "video"):
            self.assertEqual(
                controls.read(children[artifact_type]["run_id"]).state,
                "cancelled",
            )

    def test_cancel_all_quiesces_completed_child_process_before_parent_converges(self) -> None:
        created = self._create_bundle("paper-bundle-completed-child-process")
        self.assertEqual(created.status_code, 200, created.text)
        children = created.json()["children"]
        controls = RunControlStore(self.runs_dir)
        completed = children["poster"]
        run_id = completed["run_id"]
        record = controls.read(run_id)
        record = controls.transition(run_id, record, "queued")
        record = controls.transition(run_id, record, "running")
        record = controls.transition(run_id, record, "completing")
        controls.transition(run_id, record, "completed", publishable=True)
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        identity = process_identity(process.pid)
        nonce = "paper-bundle-completed-child-process"
        ProcessLedger(self.runs_dir / run_id).register_existing(
            identity,
            role="root-worker",
            nonce=nonce,
        )

        try:
            cancelled = self.client.post(
                "/api/paper-bundles/paper-bundle-completed-child-process/cancel"
            )

            self.assertEqual(cancelled.status_code, 200, cancelled.text)
            self.assertTrue(cancelled.json()["confirmed"])
            self.assertFalse(process_is_alive(identity))
            snapshot = web_server._paper_bundle_child_snapshot(run_id)
            self.assertTrue(snapshot.terminal)
            self.assertTrue(snapshot.process_free)
        finally:
            if process_is_alive(identity):
                terminate_process_identities(
                    (identity,),
                    root_pid=identity.pid,
                    grace_s=0.05,
                    owner_nonces=(nonce,),
                )
            process.wait(timeout=2)

    def test_backend_wide_reconciliation_recovers_nonlocal_owner_after_restart(self) -> None:
        with patch.object(web_server, "_run_owner_id", return_value="owner-a"):
            created = self._create_bundle("paper-bundle-restart-owner")
        self.assertEqual(created.status_code, 200, created.text)
        children = created.json()["children"]
        store = web_server._paper_bundle_store()
        store.request_cancel("paper-bundle-restart-owner", "owner-a")

        async def cancel_children_without_parent_reconcile() -> None:
            for child in children.values():
                result = await web_server._web_run_runtime().services.cancel(
                    child["run_id"],
                    "restart-test",
                )
                self.assertTrue(result.confirmed)

        asyncio.run(cancel_children_without_parent_reconcile())
        self.assertEqual(
            store.read_owned("paper-bundle-restart-owner", "owner-a").state,
            "cancelling",
        )

        asyncio.run(web_server._reconcile_all_paper_bundles())

        recovered = store.read_owned("paper-bundle-restart-owner", "owner-a")
        self.assertEqual(recovered.state, "cancelled")
        self.assertTrue(recovered.terminal)

    def test_restart_applies_published_parent_cancel_before_child_recovery(self) -> None:
        created = self._create_bundle("paper-bundle-restart-barrier")
        self.assertEqual(created.status_code, 200, created.text)
        children = created.json()["children"]
        store = web_server._paper_bundle_store()
        store.request_cancel("paper-bundle-restart-barrier", "local")

        asyncio.run(web_server._recover_web_run_controls())
        asyncio.run(web_server._reconcile_all_paper_bundles())

        recovered = store.read_owned("paper-bundle-restart-barrier", "local")
        self.assertEqual(recovered.state, "cancelled")
        controls = RunControlStore(self.runs_dir)
        self.assertEqual(
            {controls.read(child["run_id"]).state for child in children.values()},
            {"cancelled"},
        )
        self.assertEqual(
            {child.state for child in recovered.children.values()},
            {"cancelled"},
        )

    def test_restart_finishes_cancelled_unpublished_creation_claim(self) -> None:
        store = web_server._paper_bundle_store()
        owner_id = "local"
        job_id = "paper-bundle-restart-creation"
        idempotency_digest = store._idempotency_digest(owner_id, "restart-creation")
        decision = store._claim_creation(
            owner_id,
            idempotency_digest,
            "1" * 64,
            job_id,
        )
        run_id = decision["assigned_runs"]["poster"]
        RunControlStore(self.runs_dir).reserve(
            run_id,
            "poster",
            parent_job_id=job_id,
        )
        cancellation = store._claim_pending_creation_cancel(job_id, owner_id)
        self.assertEqual(cancellation["action"], "acquired")

        asyncio.run(web_server._recover_web_run_controls())

        claim = store._read_claim_unlocked(owner_id, idempotency_digest)
        self.assertIsNotNone(claim)
        self.assertEqual(claim["state"], "cancelled")
        self.assertTrue(claim["tombstone_cleanup_complete"])
        self.assertTrue(claim["factory_quiesced"])
        self.assertEqual(RunControlStore(self.runs_dir).read(run_id).state, "cancelled")

    def test_single_child_cancel_does_not_cancel_siblings(self) -> None:
        created = self._create_bundle("paper-bundle-single-child")
        self.assertEqual(created.status_code, 200, created.text)
        children = created.json()["children"]

        cancelled = self.client.post(
            f"/api/runs/{children['video']['run_id']}/cancel"
        )

        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        controls = RunControlStore(self.runs_dir)
        self.assertEqual(
            controls.read(children["video"]["run_id"]).state,
            "cancelled",
        )
        for artifact_type in ("poster", "deck", "landing"):
            self.assertEqual(
                controls.read(children[artifact_type]["run_id"]).state,
                "reserved",
            )
        parent = self.client.get("/api/paper-bundles/paper-bundle-single-child")
        self.assertEqual(parent.status_code, 200, parent.text)
        self.assertEqual(parent.json()["state"], "running")


class WebPaperBundleStorePathTests(unittest.TestCase):
    def test_store_factory_does_not_resolve_a_symlinked_jobs_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            runs_dir = out_dir / "runs"
            external = Path(tmp) / "external"
            runs_dir.mkdir(parents=True)
            external.mkdir()
            jobs_link = out_dir / "paper-bundles"
            try:
                os.symlink(external, jobs_link, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")
            with patch.object(web_server, "RUNS_DIR", runs_dir):
                web_server._reset_paper_bundle_store_for_tests()
                with self.assertRaises(web_server.PaperBundleError):
                    web_server._paper_bundle_store()
                web_server._reset_paper_bundle_store_for_tests()
            self.assertEqual(list(external.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
