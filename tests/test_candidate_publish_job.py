from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from autodesign.attempt_candidates import (
    capture_attempt_candidate,
    load_selection_journal,
)
from autodesign.attempt_selection import complete_source_run_with_candidate_fork
from autodesign.config import Settings
from autodesign.process_supervision import (
    ProcessLedger,
    process_identity,
    process_is_alive,
)
from autodesign.run_control import RunControlStore
from autodesign.run_supervisor import RunSupervisor, TerminalReconciliation
from autodesign.web_run_services import WebRunServices
import scripts.web_server as web_server


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "derived_worker_process.py"


class CandidatePublishJobTests(unittest.TestCase):
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
        store = RunControlStore(self.runs_dir)
        supervisor = RunSupervisor(
            self.runs_dir,
            control_store=store,
            worker_command=(sys.executable, str(FIXTURE)),
            grace_s=0.05,
            terminal_reconciler=web_server._reconcile_run_terminal_artifact,
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
        self._client_context = TestClient(
            web_server.app,
            raise_server_exceptions=False,
        )
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
        for current in reversed(self._patches):
            current.stop()
        self._tmp.cleanup()

    def _complete_control(
        self,
        run_id: str,
        artifact_type: str,
        *,
        parent_run_id: str | None = None,
    ) -> None:
        store = RunControlStore(self.runs_dir)
        record = store.reserve(
            run_id,
            artifact_type,
            parent_job_id=parent_run_id,
        )
        record = store.transition(run_id, record, "queued")
        record = store.transition(run_id, record, "running")
        record = store.transition(run_id, record, "completing")
        store.transition(run_id, record, "completed", publishable=True)

    def _candidate_draft(
        self,
        *,
        source_active: bool = False,
    ) -> tuple[Path, str]:
        source_run_id = "source-run"
        source_dir = self.runs_dir / source_run_id
        attempt_dir = source_dir / "landing_author" / "attempt_01"
        attempt_dir.mkdir(parents=True)
        (attempt_dir / "index.html").write_text(
            "<!doctype html><main>Attempt one</main>",
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
            max_attempts=4,
            source_path="index.html",
            dependency_paths=[],
            preview_paths=[],
            validation_summary_path="validation.json",
            safety_state="ready",
            hard_blockers=[],
            warnings=[],
        )
        if source_active:
            store = RunControlStore(self.runs_dir)
            record = store.reserve(source_run_id, "landing")
            record = store.transition(source_run_id, record, "queued")
            store.transition(source_run_id, record, "running")
        else:
            self._complete_control(source_run_id, "landing")

        draft_run_id = "editable-draft"
        draft_dir = self.runs_dir / draft_run_id
        final_dir = draft_dir / "final"
        final_dir.mkdir(parents=True)
        (final_dir / "index.html").write_text(
            "<!doctype html><main>Edited draft</main>",
            encoding="utf-8",
        )
        lineage = {
            "schema_version": 1,
            "status": "draft",
            "artifact_type": "landing",
            "source_run_id": source_run_id,
            "source_attempt": 1,
            "source_candidate_id": candidate.candidate_id,
            "source_candidate_sha256": candidate.source_sha256,
            "conversation_id": "conversation",
        }
        (draft_dir / "candidate_draft_lineage.json").write_text(
            json.dumps(lineage, sort_keys=True),
            encoding="utf-8",
        )
        self._complete_control(
            draft_run_id,
            "landing",
            parent_run_id=source_run_id,
        )
        descriptor = {
            "version": 1,
            "job_kind": "attempt_fork",
            "run_id": draft_run_id,
            "parent_run_id": source_run_id,
            "artifact_type": "landing",
            "conversation_id": "conversation",
            "baseline_artifact_json": "",
            "source_artifact_id": f"art_{source_run_id}",
            "artifact_name": "Attempt 1 draft",
            "source_relative_path": "attempt_candidates/attempt_01/candidate.json",
        }
        (draft_dir / "derived_job.json").write_text(
            json.dumps(descriptor, sort_keys=True),
            encoding="utf-8",
        )
        return source_dir, draft_run_id

    def _reserve_publish(self, draft_run_id: str):
        with (
            patch.object(web_server, "new_run_id", return_value="published-child"),
            patch.object(
                web_server,
                "_validate_candidate_draft",
                side_effect=AssertionError(
                    "reserve-only candidate publish ran validation synchronously"
                ),
            ) as validation,
        ):
            response = self.client.post(
                f"/api/artifacts/art_{draft_run_id}/publish-candidate-draft",
                headers={"X-Autodesign-Reserve-Only": "true"},
                json={"conversation_id": "conversation"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        validation.assert_not_called()
        payload = response.json()
        self.assertEqual(payload["run_id"], "published-child")
        self.assertTrue(payload.get("start_token"))
        return payload

    def test_endpoint_reserves_without_synchronous_validation(self) -> None:
        _source_dir, draft_run_id = self._candidate_draft()

        payload = self._reserve_publish(draft_run_id)

        record = RunControlStore(self.runs_dir).read(payload["run_id"])
        self.assertEqual(record.state, "queued")
        self.assertIsNone(record.worker_pid)
        self.assertIsNone(web_server._RUNS[payload["run_id"]].task)
        lineage = json.loads(
            (self.runs_dir / draft_run_id / "candidate_draft_lineage.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(lineage["status"], "draft")

    def test_started_publish_can_be_cancelled_without_selecting_source(self) -> None:
        source_dir, draft_run_id = self._candidate_draft()
        payload = self._reserve_publish(draft_run_id)
        run_id = payload["run_id"]

        started = self.client.post(
            f"/api/runs/{run_id}/start",
            headers={"X-Autodesign-Upload-Token": payload["start_token"]},
        )
        self.assertEqual(started.status_code, 200, started.text)
        heartbeat = (
            self.runs_dir
            / run_id
            / "candidate-publish"
            / "validation-heartbeat.txt"
        )
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not heartbeat.is_file():
            time.sleep(0.01)
        self.assertTrue(heartbeat.is_file(), "candidate publish worker did not start")
        roots = ProcessLedger(self.runs_dir / run_id).read().processes
        recorded = json.loads(
            (self.runs_dir / run_id / "recorded_pids.json")
            .read_text(encoding="utf-8")
        )
        descendants = [process_identity(int(pid)) for pid in recorded["pids"]]

        cancelled = self.client.post(f"/api/runs/{run_id}/cancel")

        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertTrue(cancelled.json()["confirmed"])
        self.assertEqual(
            RunControlStore(self.runs_dir).read(run_id).state,
            "cancelled",
        )
        self.assertIsNone(load_selection_journal(source_dir))
        self.assertFalse((source_dir / "final").exists())
        draft_lineage = json.loads(
            (self.runs_dir / draft_run_id / "candidate_draft_lineage.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(draft_lineage["status"], "draft")
        self.assertTrue(
            all(not process_is_alive(item.identity) for item in roots)
        )
        self.assertTrue(all(not process_is_alive(item) for item in descendants))
        frozen = heartbeat.read_bytes()
        time.sleep(0.12)
        self.assertEqual(heartbeat.read_bytes(), frozen)
        artifact = self.client.get(f"/api/runs/{run_id}/artifact")
        self.assertIsNone(artifact.json().get("artifact"))

    def test_cancelling_source_cancels_active_publish_descendant(self) -> None:
        source_dir, draft_run_id = self._candidate_draft(source_active=True)
        payload = self._reserve_publish(draft_run_id)
        run_id = payload["run_id"]

        started = self.client.post(
            f"/api/runs/{run_id}/start",
            headers={"X-Autodesign-Upload-Token": payload["start_token"]},
        )
        self.assertEqual(started.status_code, 200, started.text)
        heartbeat = (
            self.runs_dir
            / run_id
            / "candidate-publish"
            / "validation-heartbeat.txt"
        )
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not heartbeat.is_file():
            time.sleep(0.01)
        self.assertTrue(heartbeat.is_file(), "candidate publish worker did not start")

        cancelled = self.client.post("/api/runs/source-run/cancel")

        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertTrue(cancelled.json()["confirmed"])
        store = RunControlStore(self.runs_dir)
        self.assertEqual(store.read("source-run").state, "cancelled")
        child = store.read(run_id)
        self.assertEqual(child.state, "cancelled")
        self.assertFalse(child.publishable)
        self.assertIsNone(load_selection_journal(source_dir))
        lineage_path = self.runs_dir / run_id / "candidate_draft_lineage.json"
        if lineage_path.is_file():
            lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
            self.assertNotEqual(lineage["status"], "published")

    def test_completion_preflight_rejects_cancelled_source_before_child_cas(
        self,
    ) -> None:
        source_dir, draft_run_id = self._candidate_draft(source_active=True)
        run_id = "published-child"
        store = RunControlStore(self.runs_dir)
        record = store.reserve(run_id, "landing", parent_job_id=draft_run_id)
        record = store.transition(run_id, record, "queued")
        record = store.transition(run_id, record, "running")
        store.transition(run_id, record, "completing")
        run_dir = self.runs_dir / run_id
        (run_dir / "final").mkdir(parents=True)
        (run_dir / "final" / "index.html").write_text(
            "<!doctype html><main>Validated child</main>",
            encoding="utf-8",
        )
        draft_lineage = json.loads(
            (self.runs_dir / draft_run_id / "candidate_draft_lineage.json")
            .read_text(encoding="utf-8")
        )
        (run_dir / "candidate_draft_lineage.json").write_text(
            json.dumps({**draft_lineage, "status": "validated"}, sort_keys=True),
            encoding="utf-8",
        )
        descriptor = {
            "version": 1,
            "job_kind": "candidate_publish",
            "run_id": run_id,
            "parent_run_id": draft_run_id,
            "artifact_type": "landing",
            "conversation_id": "conversation",
            "baseline_artifact_json": "",
            "source_artifact_id": f"art_{draft_run_id}",
            "artifact_name": "Published candidate",
            "source_relative_path": "candidate_draft_lineage.json",
        }
        (run_dir / "derived_job.json").write_text(
            json.dumps(descriptor, sort_keys=True),
            encoding="utf-8",
        )
        store.request_cancel("source-run")

        async def accept() -> None:
            await web_server._web_run_runtime().supervisor.accept_completion(
                run_id,
                terminal_state="completed",
                publishable=True,
                result_digest="digest",
            )

        with self.assertRaises(Exception):
            self.client.portal.call(accept)

        child = store.read(run_id)
        self.assertEqual(child.state, "completing")
        self.assertFalse(child.publishable)
        self.assertIsNone(load_selection_journal(source_dir))
        lineage = json.loads(
            (run_dir / "candidate_draft_lineage.json").read_text(encoding="utf-8")
        )
        self.assertEqual(lineage["status"], "validated")

        async def recover_twice() -> tuple[bytes, bytes]:
            web_server._RUNS.clear()
            await web_server._recover_web_run_controls()
            pending = [
                state.task
                for state in web_server._RUNS.values()
                if state.task is not None
            ]
            if pending:
                await asyncio.gather(*pending)
            first = (run_dir / "run_control.json").read_bytes()
            await web_server._recover_web_run_controls()
            second = (run_dir / "run_control.json").read_bytes()
            return first, second

        first_control, second_control = self.client.portal.call(recover_twice)
        self.assertEqual(store.read("source-run").state, "cancelled")
        recovered_child = store.read(run_id)
        self.assertEqual(recovered_child.state, "cancelled")
        self.assertFalse(recovered_child.publishable)
        self.assertEqual(first_control, second_control)
        self.assertIsNone(load_selection_journal(source_dir))

    def test_source_cancel_replays_partially_committed_candidate_publish(self) -> None:
        source_dir, draft_run_id = self._candidate_draft(source_active=True)
        run_id = "published-child"
        store = RunControlStore(self.runs_dir)
        record = store.reserve(
            run_id,
            "landing",
            parent_job_id=draft_run_id,
        )
        record = store.transition(run_id, record, "queued")
        record = store.transition(run_id, record, "running")
        record = store.transition(run_id, record, "completing")
        record = store.update_terminal_reconciliation(
            run_id,
            record,
            decision="accept",
            phase="preflight",
            terminal_state="completed",
            status="succeeded",
            diagnostic=None,
        )
        record = store.update_terminal_reconciliation(
            run_id,
            record,
            decision="accept",
            phase="commit",
            terminal_state="completed",
            status="pending",
            diagnostic=None,
        )
        store.transition(
            run_id,
            record,
            "completed",
            publishable=True,
            accepted_terminal_event_id="candidate-publish-terminal",
            terminal_event="run.done",
        )
        run_dir = self.runs_dir / run_id
        final_dir = run_dir / "final"
        final_dir.mkdir(parents=True)
        (final_dir / "index.html").write_text(
            "<!doctype html><main>Published child</main>",
            encoding="utf-8",
        )
        lineage = json.loads(
            (self.runs_dir / draft_run_id / "candidate_draft_lineage.json")
            .read_text(encoding="utf-8")
        )
        validated_lineage = {**lineage, "status": "validated"}
        (run_dir / "candidate_draft_lineage.json").write_text(
            json.dumps(validated_lineage, sort_keys=True),
            encoding="utf-8",
        )
        (run_dir / "derived_job.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "job_kind": "candidate_publish",
                    "run_id": run_id,
                    "parent_run_id": draft_run_id,
                    "artifact_type": "landing",
                    "conversation_id": "conversation",
                    "baseline_artifact_json": "",
                    "source_artifact_id": f"art_{draft_run_id}",
                    "artifact_name": "Published candidate",
                    "source_relative_path": "candidate_draft_lineage.json",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        completion = complete_source_run_with_candidate_fork(
            run_dir=source_dir,
            run_id="source-run",
            attempt=1,
            expected_candidate_sha256=lineage["source_candidate_sha256"],
            artifact_id=f"art_{run_id}",
        )
        self.assertEqual(completion, "completed")

        cancelled = self.client.post("/api/runs/source-run/cancel")

        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertTrue(cancelled.json()["confirmed"])
        self.assertEqual(store.read("source-run").state, "cancelled")
        self.assertEqual(store.read(run_id).state, "completed")
        published_lineage = json.loads(
            (run_dir / "candidate_draft_lineage.json").read_text(encoding="utf-8")
        )
        self.assertEqual(published_lineage["status"], "published")
        selection = load_selection_journal(source_dir)
        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertEqual(selection.artifact_id, f"art_{run_id}")

    def test_recovery_commits_source_selection_once_after_accepted_completion(
        self,
    ) -> None:
        source_dir, draft_run_id = self._candidate_draft()
        run_id = "published-child"
        store = RunControlStore(self.runs_dir)
        record = store.reserve(
            run_id,
            "landing",
            parent_job_id=draft_run_id,
        )
        record = store.transition(run_id, record, "queued")
        record = store.transition(run_id, record, "running")
        store.transition(run_id, record, "completing")
        run_dir = self.runs_dir / run_id
        final_dir = run_dir / "final"
        final_dir.mkdir(parents=True)
        source_path = final_dir / "index.html"
        source_path.write_text(
            "<!doctype html><main>Published child</main>",
            encoding="utf-8",
        )
        draft_lineage = json.loads(
            (self.runs_dir / draft_run_id / "candidate_draft_lineage.json")
            .read_text(encoding="utf-8")
        )
        published_lineage = {
            **draft_lineage,
            "status": "published",
            "published_version_id": f"art_{run_id}:v1",
            "published_at": "2026-08-03T00:00:00+00:00",
        }
        (run_dir / "candidate_draft_lineage.json").write_text(
            json.dumps(published_lineage, sort_keys=True),
            encoding="utf-8",
        )
        descriptor = {
            "version": 1,
            "job_kind": "candidate_publish",
            "run_id": run_id,
            "parent_run_id": draft_run_id,
            "artifact_type": "landing",
            "conversation_id": "conversation",
            "baseline_artifact_json": "",
            "source_artifact_id": f"art_{draft_run_id}",
            "artifact_name": "Published candidate",
            "source_relative_path": "final/index.html",
        }
        (run_dir / "derived_job.json").write_text(
            json.dumps(descriptor, sort_keys=True),
            encoding="utf-8",
        )
        (run_dir / "worker_result.json").write_text(
            json.dumps(
                {
                    "job_kind": "candidate_publish",
                    "run_id": run_id,
                    "ok": True,
                    "result": {
                        "run_id": run_id,
                        "artifact_type": "landing",
                        "source_path": str(source_path),
                        "lineage": published_lineage,
                    },
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        web_server._RUNS.pop(run_id, None)

        before = self.client.get(f"/api/runs/{run_id}/artifact")
        self.assertIn(before.status_code, {409, 423}, before.text)

        async def recover_and_join() -> None:
            await web_server._recover_web_run_controls()
            state = web_server._RUNS[run_id]
            assert state.task is not None
            await state.task

        self.client.portal.call(recover_and_join)

        completed = store.read(run_id)
        self.assertEqual(completed.state, "completed")
        self.assertTrue(completed.publishable)
        selection = load_selection_journal(source_dir)
        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertEqual(selection.artifact_id, f"art_{run_id}")
        source_selection_path = source_dir / "attempt_candidates" / "selection.json"
        first_selection = source_selection_path.read_bytes()

        replay = TerminalReconciliation(
            run_id=run_id,
            decision="accept",
            phase="commit",
            terminal_state="completed",
            record=completed,
        )
        web_server._reconcile_run_terminal_artifact(replay)
        web_server._reconcile_run_terminal_artifact(replay)

        self.assertEqual(source_selection_path.read_bytes(), first_selection)
        published = self.client.get(f"/api/runs/{run_id}/artifact")
        self.assertEqual(published.status_code, 200, published.text)
        self.assertEqual(published.json()["artifact"]["artifact_id"], f"art_{run_id}")


if __name__ == "__main__":
    unittest.main()
