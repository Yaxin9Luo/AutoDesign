from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from autodesign.config import Settings
from autodesign.run_control import RunControlStore
from scripts import web_server


class WebApplyEditsCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmp.name) / "out"
        self.runs_dir = self.out_dir / "runs"
        self.runs_dir.mkdir(parents=True)
        self.settings = Settings(
            anthropic_api_key="test-key",
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

    def tearDown(self) -> None:
        self._client_context.__exit__(None, None, None)
        web_server._reset_web_run_runtime_for_tests()
        for current in reversed(self._patches):
            current.stop()
        self._tmp.cleanup()

    def _install_blocked_artifact_edit_worker(self) -> None:
        worker_script = Path(self._tmp.name) / "blocked_artifact_edit_worker.py"
        worker_script.write_text(
            """from pathlib import Path
import sys
import time

from autodesign.run_worker_protocol import decode_request

request = decode_request(sys.stdin.buffer)
run_dir = Path(request.settings.out_dir) / "runs" / request.run_id
(run_dir / "fixture_observation.json").write_text("started", encoding="utf-8")
while True:
    time.sleep(0.02)
""",
            encoding="utf-8",
        )
        runtime = web_server._web_run_runtime()
        runtime.supervisor._worker_command = (sys.executable, str(worker_script))

    def _assert_child_output_stays_frozen(self, child_run_id: str) -> None:
        child_dir = self.runs_dir / child_run_id
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

    def test_confirmed_source_cancel_blocks_inflight_apply_edits_publication(self) -> None:
        source_run_id = "apply-edits-cancel-source"
        child_run_id = "apply-edits-late-child"
        store = RunControlStore(self.runs_dir)
        source = store.reserve(source_run_id, "landing")
        source = store.transition(source_run_id, source, "queued")
        store.transition(source_run_id, source, "running")
        source_html = self.runs_dir / source_run_id / "final" / "index.html"
        source_html.parent.mkdir(parents=True)
        source_html.write_text(
            """<!doctype html><main data-autodesign-artifact-root="landing">
            <div class="layer" data-layer-id="title" data-kind="text">Title</div>
            </main>""",
            encoding="utf-8",
        )
        self._install_blocked_artifact_edit_worker()
        edit_started = self.runs_dir / child_run_id / "fixture_observation.json"

        with patch.object(web_server, "new_run_id", return_value=child_run_id):
            with ThreadPoolExecutor(max_workers=1) as pool:
                edit_request = pool.submit(
                    self.client.post,
                    "/api/edits/apply",
                    data={
                        "run_id": source_run_id,
                        "artifact_type": "landing",
                        "edits_json": "{}",
                    },
                )
                deadline = time.monotonic() + 3.0
                while not edit_started.is_file() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(
                    edit_started.is_file(),
                    "apply-edits did not start a supervised child worker",
                )
                child = store.read(child_run_id)
                self.assertEqual(child.parent_job_id, source_run_id)
                descriptor = json.loads(
                    (self.runs_dir / child_run_id / "derived_job.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(descriptor["job_kind"], "artifact_edit")
                self.assertEqual(descriptor["parent_run_id"], source_run_id)

                cancelled = self.client.post(
                    f"/api/runs/{source_run_id}/cancel"
                )
                self.assertEqual(cancelled.status_code, 200, cancelled.text)
                self.assertTrue(cancelled.json()["confirmed"], cancelled.text)
                self.assertEqual(store.read(source_run_id).state, "cancelled")
                edited = edit_request.result(timeout=5.0)

        late_final = self.runs_dir / child_run_id / "final" / "index.html"
        self.assertEqual(
            {
                "apply_request_succeeded": edited.status_code == 200,
                "late_final_exists": late_final.is_file(),
            },
            {
                "apply_request_succeeded": False,
                "late_final_exists": False,
            },
        )
        self._assert_child_output_stays_frozen(child_run_id)

    def test_confirmed_ancestor_cancel_blocks_inflight_apply_edits_publication(self) -> None:
        ancestor_run_id = "apply-edits-cancel-ancestor"
        source_run_id = "apply-edits-ancestor-source"
        child_run_id = "apply-edits-ancestor-child"
        store = RunControlStore(self.runs_dir)
        ancestor = store.reserve(ancestor_run_id, "landing")
        ancestor = store.transition(ancestor_run_id, ancestor, "queued")
        store.transition(ancestor_run_id, ancestor, "running")
        source = store.reserve(
            source_run_id,
            "landing",
            parent_job_id=ancestor_run_id,
        )
        source = store.transition(source_run_id, source, "queued")
        source = store.transition(source_run_id, source, "running")
        source = store.transition(source_run_id, source, "completing")
        store.transition(source_run_id, source, "completed", publishable=True)
        source_dir = self.runs_dir / source_run_id
        source_html = source_dir / "final" / "index.html"
        source_html.parent.mkdir(parents=True)
        source_html.write_text(
            """<!doctype html><main data-autodesign-artifact-root="landing">
            <div class="layer" data-layer-id="title" data-kind="text">Title</div>
            </main>""",
            encoding="utf-8",
        )
        (source_dir / "derived_job.json").write_text(
            json.dumps({
                "version": 1,
                "job_kind": "attempt_fork",
                "run_id": source_run_id,
                "parent_run_id": ancestor_run_id,
                "artifact_type": "landing",
                "conversation_id": "ancestor-cancel-test",
                "baseline_artifact_json": "",
                "source_artifact_id": f"art_{ancestor_run_id}",
                "artifact_name": "index",
                "source_relative_path": "final/index.html",
            }),
            encoding="utf-8",
        )
        self._install_blocked_artifact_edit_worker()
        edit_started = self.runs_dir / child_run_id / "fixture_observation.json"

        with patch.object(web_server, "new_run_id", return_value=child_run_id):
            with ThreadPoolExecutor(max_workers=1) as pool:
                edit_request = pool.submit(
                    self.client.post,
                    "/api/edits/apply",
                    data={
                        "run_id": source_run_id,
                        "artifact_type": "landing",
                        "edits_json": "{}",
                    },
                )
                deadline = time.monotonic() + 3.0
                while not edit_started.is_file() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(edit_started.is_file())

                cancelled = self.client.post(
                    f"/api/runs/{ancestor_run_id}/cancel"
                )
                self.assertEqual(cancelled.status_code, 200, cancelled.text)
                self.assertTrue(cancelled.json()["confirmed"], cancelled.text)
                edited = edit_request.result(timeout=5.0)

        self.assertNotEqual(edited.status_code, 200)
        self.assertEqual(store.read(ancestor_run_id).state, "cancelled")
        self.assertEqual(store.read(child_run_id).state, "cancelled")
        self.assertFalse(
            (self.runs_dir / child_run_id / "final" / "index.html").is_file()
        )
        self._assert_child_output_stays_frozen(child_run_id)

    def test_supervised_apply_edits_publishes_landing_with_durable_lineage(self) -> None:
        source_run_id = "apply-edits-success-source"
        child_run_id = "apply-edits-success-child"
        store = RunControlStore(self.runs_dir)
        source = store.reserve(source_run_id, "landing")
        source = store.transition(source_run_id, source, "queued")
        source = store.transition(source_run_id, source, "running")
        source = store.transition(source_run_id, source, "completing")
        store.transition(source_run_id, source, "completed", publishable=True)
        source_html = self.runs_dir / source_run_id / "final" / "index.html"
        source_html.parent.mkdir(parents=True)
        source_html.write_text(
            """<!doctype html><html><head><style>
            body{margin:0}.ld-landing{width:1200px;min-height:800px}
            </style></head><body>
            <main class="ld-landing" data-autodesign-artifact-root="landing" data-w="1200">
              <section class="ld-section" data-layer-id="hero" data-layer-name="Hero">
                <div class="layer text" data-layer-id="title" data-kind="text">Original</div>
              </section>
            </main></body></html>""",
            encoding="utf-8",
        )

        with patch.object(web_server, "new_run_id", return_value=child_run_id):
            response = self.client.post(
                "/api/edits/apply",
                data={
                    "run_id": source_run_id,
                    "artifact_type": "landing",
                    "edits_json": json.dumps({"title": {"text": "Edited title"}}),
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["message"]["run_id"], child_run_id)
        child = store.read(child_run_id)
        self.assertEqual(child.parent_job_id, source_run_id)
        self.assertEqual(child.state, "completed")
        self.assertTrue(child.publishable)
        final_html = self.runs_dir / child_run_id / "final" / "index.html"
        self.assertIn("Edited title", final_html.read_text(encoding="utf-8"))
        self.assertFalse(
            (self.runs_dir / child_run_id / ".landing-final-promotion.json").exists()
        )

    def test_supervised_apply_edits_preserves_video_candidate_lineage(self) -> None:
        source_run_id = "apply-edits-video-source"
        child_run_id = "apply-edits-video-child"
        store = RunControlStore(self.runs_dir)
        source = store.reserve(source_run_id, "video")
        source = store.transition(source_run_id, source, "queued")
        source = store.transition(source_run_id, source, "running")
        source = store.transition(source_run_id, source, "completing")
        store.transition(source_run_id, source, "completed", publishable=True)
        source_dir = self.runs_dir / source_run_id
        source_html = source_dir / "final" / "deck.html"
        source_html.parent.mkdir(parents=True)
        source_html.write_text(
            """<!doctype html><html><head><style>body{margin:0}</style></head>
            <body><main data-autodesign-artifact-root="video">
            <h1 data-layer-id="title" data-kind="text">Original title</h1>
            </main></body></html>""",
            encoding="utf-8",
        )
        lineage = {
            "schema_version": 1,
            "status": "published",
            "artifact_type": "video",
            "source_run_id": "original-generation",
            "source_attempt": 2,
            "source_candidate_id": "video-attempt-02",
            "source_candidate_sha256": "b" * 64,
            "published_version_id": "art_apply-edits-video-source:v1",
            "published_at": "2026-08-03T00:00:00Z",
        }
        (source_dir / "candidate_draft_lineage.json").write_text(
            json.dumps(lineage),
            encoding="utf-8",
        )

        with patch.object(web_server, "new_run_id", return_value=child_run_id):
            response = self.client.post(
                "/api/edits/apply",
                data={
                    "run_id": source_run_id,
                    "artifact_type": "video",
                    "edits_json": json.dumps(
                        {"title": {"text": "Edited video title"}}
                    ),
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        artifact = response.json()["artifact"]
        self.assertTrue(artifact["candidate_draft"])
        self.assertEqual(artifact["artifact_type"], "video")
        child_lineage = json.loads(
            (self.runs_dir / child_run_id / "candidate_draft_lineage.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(child_lineage["status"], "draft")
        self.assertEqual(child_lineage["parent_draft_run_id"], source_run_id)
        self.assertEqual(
            child_lineage["published_artifact_id_at_fork"],
            f"art_{source_run_id}",
        )
        self.assertNotIn("published_version_id", child_lineage)
        self.assertNotIn("published_at", child_lineage)
        project_html = (
            self.runs_dir / child_run_id / "final" / "project" / "index.html"
        )
        self.assertIn("Edited video title", project_html.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
