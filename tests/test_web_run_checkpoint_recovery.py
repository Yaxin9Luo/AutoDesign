from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from autodesign.config import Settings
from autodesign.util.io import sha256_file
from scripts import web_server


class WebRunCheckpointRecoveryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _settings(out_dir: Path) -> Settings:
        return Settings(
            anthropic_api_key="",
            anthropic_base_url=None,
            gemini_api_key="",
            designer_model="designer",
            critic_model="critic",
            out_dir=out_dir,
        )

    @staticmethod
    def _write_interrupted_run(run_dir: Path) -> None:
        run_dir.mkdir(parents=True)
        (run_dir / "run_brief.json").write_text(
            json.dumps({"version": 1, "brief": "Create a poster"}),
            encoding="utf-8",
        )
        (run_dir / "resume_state.json").write_text("{}", encoding="utf-8")
        (run_dir / "canvas_plan.json").write_text(
            json.dumps({"artifact_type": "poster"}),
            encoding="utf-8",
        )
        (run_dir / "layers").mkdir()

        attempt_2 = run_dir / "designer_author" / "attempt_02"
        attempt_2.mkdir(parents=True)
        poster_path = attempt_2 / "poster.html"
        poster_path.write_text("<html><body>attempt 2</body></html>", encoding="utf-8")
        poster_sha = sha256_file(poster_path)
        (attempt_2 / "designer_author_log.json").write_text(
            json.dumps({
                "returncode": 0,
                "reason": "done_marker",
                "timeout": False,
                "poster_sha256": poster_sha,
            }),
            encoding="utf-8",
        )
        (attempt_2 / "validation_feedback.json").write_text(
            json.dumps({
                "attempt": 2,
                "attempt_dir": str(attempt_2),
                "validated_attempt": 2,
                "validated_attempt_dir": str(attempt_2.resolve()),
                "validated_poster_sha256": poster_sha,
                "invocation_poster_sha256": poster_sha,
                "summary": {"issue_id": "repair-layout"},
            }),
            encoding="utf-8",
        )

        attempt_3 = run_dir / "designer_author" / "attempt_03"
        attempt_3.mkdir()
        (attempt_3 / "designer_author_log.json").write_text(
            json.dumps({
                "returncode": 1,
                "reason": "process_exit",
                "timeout": False,
                "poster_sha256": "",
            }),
            encoding="utf-8",
        )

        events = [
            {
                "event": "designer_author.attempt_start",
                "attempt": 3,
                "max_attempts": 4,
            },
            {
                "event": "designer_author.agent_output",
                "attempt": 3,
                "status": "error",
                "reason": "designer_author_process_exit",
                "elapsed_s": 189.7,
                "stdout_excerpt": (
                    "API Error: Request rejected (429) · "
                    "每分钟请求次数超过限制 · "
                    "API_KEY=sk-supersecret123456789"
                ),
                "stderr_excerpt": "",
            },
            {"event": "run.done", "terminal_status": "fail"},
        ]
        (run_dir / "run_events.jsonl").write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
            encoding="utf-8",
        )

    async def test_failure_reports_rate_limit_and_last_usable_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            out_dir = Path(raw_tmp) / "out"
            run_id = "20260729-151946-deadbeef"
            run_dir = out_dir / "runs" / run_id
            self._write_interrupted_run(run_dir)
            settings = self._settings(out_dir)

            with patch.object(web_server, "SETTINGS", settings):
                failure = web_server._failure_from_disk(
                    run_id=run_id,
                    a_type="poster",
                    status="fail",
                    designer_model="designer",
                    has_pdf=True,
                    elapsed_ms=690_000,
                )

            self.assertEqual(failure.phase, "authoring")
            self.assertEqual(failure.error_code, "provider_rate_limit")
            self.assertIn("per-minute rate limit", failure.error_message)
            self.assertIn("resume from the saved checkpoint", failure.error_message)
            self.assertIn("每分钟请求次数超过限制", failure.error_detail)
            self.assertIn("API_KEY=[redacted]", failure.error_detail)
            self.assertNotIn("sk-supersecret", failure.error_detail)
            self.assertTrue(failure.resume_available)
            self.assertEqual(failure.resume_from_attempt, 2)
            self.assertEqual(failure.next_attempt, 3)

    async def test_retry_mints_new_run_and_passes_resume_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            out_dir = Path(raw_tmp) / "out"
            run_id = "20260729-151946-deadbeef"
            run_dir = out_dir / "runs" / run_id
            self._write_interrupted_run(run_dir)
            settings = self._settings(out_dir)
            original = web_server._RunState(
                artifact_type="poster",
                designer_model="designer",
                has_pdf=True,
                brief="Create a poster",
                attach_paths=[],
                conversation_id="conversation-1",
                palette_id="plum_sage",
                authoring_max_attempts=4,
            )
            request = Request({"type": "http", "headers": []})
            runs_before = dict(web_server._RUNS)
            web_server._RUNS[run_id] = original

            try:
                with (
                    patch.object(web_server, "SETTINGS", settings),
                    patch.object(web_server, "_RUN_ACCESS_CONTROL", False),
                    patch.object(web_server, "_DEMO_MODE", False),
                    patch.object(web_server, "_settings_for_request", return_value=settings),
                    patch.object(web_server, "_append_event"),
                    patch.object(
                        web_server,
                        "_start_legacy_pipeline_worker",
                        new_callable=AsyncMock,
                    ) as start_worker,
                    patch.object(
                        web_server,
                        "_monitor_supervised_pipeline",
                        new_callable=AsyncMock,
                    ) as monitor_worker,
                ):
                    ack = await web_server.run_retry(
                        run_id,
                        request,
                        designer_override=None,
                        planner_override=None,
                    )
                    await web_server._RUNS[ack.run_id].task

                self.assertNotEqual(ack.run_id, run_id)
                start_worker.assert_awaited_once()
                self.assertEqual(
                    start_worker.await_args.kwargs["resume_run"],
                    run_id,
                )
                monitor_worker.assert_awaited_once_with(
                    run_id=ack.run_id,
                    state=web_server._RUNS[ack.run_id],
                )
            finally:
                web_server._RUNS.clear()
                web_server._RUNS.update(runs_before)

    async def test_runtime_exception_is_preserved_in_safe_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            out_dir = Path(raw_tmp) / "out"
            run_id = "20260729-160000-feedface"
            run_dir = out_dir / "runs" / run_id
            (run_dir / "layers").mkdir(parents=True)
            (run_dir / "run_events.jsonl").write_text(
                json.dumps({
                    "event": "run.error",
                    "msg": "ConnectionError: upstream reset API_KEY=sk-secret123456789",
                }) + "\n",
                encoding="utf-8",
            )
            settings = self._settings(out_dir)

            with patch.object(web_server, "SETTINGS", settings):
                failure = web_server._failure_from_disk(
                    run_id=run_id,
                    a_type="poster",
                    status="error",
                    designer_model="designer",
                    has_pdf=True,
                    elapsed_ms=1_000,
                )

            self.assertEqual(failure.error_code, "runtime_error")
            self.assertIn("unexpected runtime error", failure.error_message)
            self.assertIn("ConnectionError: upstream reset", failure.error_detail)
            self.assertNotIn("sk-secret", failure.error_detail)

    async def test_worker_exit_fallback_survives_disk_reconstruction(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            out_dir = Path(raw_tmp) / "out"
            run_id = "worker-exit-fallback"
            run_dir = out_dir / "runs" / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "run_events.jsonl").write_text(
                json.dumps({
                    "run_id": run_id,
                    "event": "worker.exit",
                    "version": 1,
                    "returncode": 17,
                    "error_code": "worker_result_missing",
                    "error_message": (
                        "The worker exited before writing its result. "
                        "Review diagnostics before retrying."
                    ),
                    "error_detail": (
                        "Worker exit status: 17.\n"
                        "Result protocol: worker_result.json is missing.\n"
                        "stderr tail: final-root-cause API_KEY=sk-secret123456789"
                    ),
                    "protocol_error": "worker_result.json is missing",
                    "last_event": "fixture.before_exit",
                    "last_worker_seq": 2,
                    "last_phase": "authoring",
                    "last_reason": "fixture_crash",
                    "stdout_tail": "stdout-final-marker",
                    "stderr_tail": "final-root-cause [REDACTED]",
                }) + "\n",
                encoding="utf-8",
            )
            settings = self._settings(out_dir)

            with patch.object(web_server, "SETTINGS", settings):
                failure = web_server._failure_from_disk(
                    run_id=run_id,
                    a_type="poster",
                    status="error",
                    designer_model="designer",
                    has_pdf=True,
                    elapsed_ms=1_000,
                )

        self.assertEqual(failure.phase, "authoring")
        self.assertEqual(failure.error_code, "worker_result_missing")
        self.assertIn("before writing its result", failure.error_message or "")
        self.assertIn("final-root-cause", failure.error_detail or "")
        self.assertNotIn("sk-secret", failure.error_detail or "")

    async def test_video_author_failure_preserves_delivery_diagnostics_and_attempt_3(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp) / "video-run"
            attempt_dir = run_dir / "video_author" / "attempt_02"
            attempt_dir.mkdir(parents=True)
            message = (
                "English subtitle readability hard limit exceeded: "
                "scene_10 cue 2 at 26.42 CPS (hard limit 24.00 CPS)"
            )
            (run_dir / "run_events.jsonl").write_text(
                json.dumps({
                    "event": "video_author.fail",
                    "reason": "video_author_delivery_failed",
                    "message": message,
                    "attempt_dir": str(attempt_dir),
                }) + "\n",
                encoding="utf-8",
            )

            with patch.object(
                web_server,
                "_load_resume_state",
                return_value={
                    "previous_attempt_dir": attempt_dir,
                    "prior_attempts": 2,
                },
            ):
                diagnostics = web_server._failure_diagnostics_from_disk(run_dir)

        self.assertEqual(diagnostics["phase"], "authoring")
        self.assertEqual(
            diagnostics["error_code"],
            "video_author_delivery_failed",
        )
        self.assertEqual(diagnostics["error_message"], message)
        self.assertIn(message, diagnostics["error_detail"])
        self.assertTrue(diagnostics["resume_available"])
        self.assertEqual(diagnostics["resume_from_attempt"], 2)
        self.assertEqual(diagnostics["next_attempt"], 3)


if __name__ == "__main__":
    unittest.main()
