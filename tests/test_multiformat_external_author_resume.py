from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autodesign.config import Settings
from autodesign.runner import (
    PipelineRunner,
    _designer_author_resume_metadata,
    _load_resume_state,
    _make_designer_author,
    _restore_external_author_resume_state,
)
from autodesign.schema import RunResult, ToolResultRecord


class MultiformatExternalAuthorResumeTests(unittest.TestCase):
    def _settings(self, **overrides: object) -> Settings:
        values: dict[str, object] = {
            "anthropic_api_key": "",
            "anthropic_base_url": None,
            "gemini_api_key": "",
            "designer_model": "test-designer",
            "critic_model": "test-critic",
        }
        values.update(overrides)
        return Settings(**values)

    def _write_metadata(
        self,
        run_dir: Path,
        artifact_type: str,
        *,
        author: dict[str, object] | None = None,
    ) -> None:
        run_dir.mkdir(parents=True)
        (run_dir / "run_brief.json").write_text(
            json.dumps({"version": 1}), encoding="utf-8"
        )
        resume_state: dict[str, object] = {"artifact_type": artifact_type}
        if author is not None:
            resume_state["designer_author"] = author
        (run_dir / "resume_state.json").write_text(
            json.dumps(resume_state), encoding="utf-8"
        )
        (run_dir / "canvas_plan.json").write_text(
            json.dumps({"artifact_type": artifact_type}), encoding="utf-8"
        )

    def test_landing_resume_finds_artifact_specific_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "landing-run"
            self._write_metadata(run_dir, "landing")
            attempt_dir = run_dir / "landing_author" / "attempt_03"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "index.html").write_text("<html></html>", encoding="utf-8")
            feedback = {"accepted": False, "findings": [{"issue_id": "landing-gap"}]}
            (attempt_dir / "landing_validation.json").write_text(
                json.dumps(feedback), encoding="utf-8"
            )
            process = {"status": "ok", "reason": "process_exit"}
            (attempt_dir / "landing_author_process.json").write_text(
                json.dumps(process), encoding="utf-8"
            )
            (run_dir / "landing_author" / "attempt_04").mkdir()

            resume = _load_resume_state(run_dir)

            self.assertIsInstance(resume, dict)
            self.assertEqual(resume["artifact_type"], "landing")
            self.assertEqual(resume["author_state_prefix"], "landing_author")
            self.assertEqual(resume["prior_attempts"], 3)
            self.assertEqual(resume["previous_attempt_dir"], attempt_dir)
            self.assertEqual(resume["previous_output_path"], attempt_dir / "index.html")
            self.assertEqual(resume["prior_feedback"], feedback)
            self.assertEqual(resume["attempt_records"][0]["invocation"], process)
            self.assertEqual(
                resume["superseded_attempt_dirs"],
                [run_dir / "landing_author" / "attempt_04"],
            )

    def test_deck_resume_finds_artifact_specific_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "deck-run"
            self._write_metadata(run_dir, "deck")
            attempt_dir = run_dir / "slides_author" / "attempt_12"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "slides.html").write_text("<html></html>", encoding="utf-8")
            feedback = {"status": "error", "issues": [{"id": "slide-overflow"}]}
            (attempt_dir / "slides_validation.json").write_text(
                json.dumps(feedback), encoding="utf-8"
            )
            process = {"returncode": 0, "stdout": "", "stderr": ""}
            (attempt_dir / "designer_author_log.json").write_text(
                json.dumps(process), encoding="utf-8"
            )

            resume = _load_resume_state(run_dir)

            self.assertIsInstance(resume, dict)
            self.assertEqual(resume["artifact_type"], "deck")
            self.assertEqual(resume["author_state_prefix"], "slides_author")
            self.assertEqual(resume["prior_attempts"], 12)
            self.assertEqual(resume["previous_attempt_dir"], attempt_dir)
            self.assertEqual(resume["previous_output_path"], attempt_dir / "slides.html")
            self.assertEqual(resume["prior_feedback"], feedback)

    def test_video_resume_prefers_delivery_errors_over_validation_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "video-run"
            self._write_metadata(run_dir, "video")
            attempt_dir = run_dir / "video_author" / "attempt_04"
            project_dir = attempt_dir / "project"
            project_dir.mkdir(parents=True)
            (project_dir / "index.html").write_text("<html></html>", encoding="utf-8")
            (attempt_dir / "video_author_manifest.json").write_text(
                json.dumps({"version": 1, "scenes": []}), encoding="utf-8"
            )
            validation_feedback = {"errors": ["static manifest validation failed"]}
            (attempt_dir / "video_author_validation_errors.json").write_text(
                json.dumps(validation_feedback), encoding="utf-8"
            )
            feedback = {"errors": ["hyperframes delivery lint failed"]}
            (attempt_dir / "video_author_delivery_errors.json").write_text(
                json.dumps(feedback), encoding="utf-8"
            )

            resume = _load_resume_state(run_dir)

            self.assertIsInstance(resume, dict)
            self.assertEqual(resume["artifact_type"], "video")
            self.assertEqual(resume["author_state_prefix"], "video_author")
            self.assertEqual(resume["prior_attempts"], 4)
            self.assertEqual(resume["previous_attempt_dir"], attempt_dir)
            self.assertEqual(resume["previous_output_path"], project_dir)
            self.assertEqual(resume["prior_feedback"], feedback)
            self.assertEqual(resume["validation_feedback_history"], [feedback])

    def test_video_resume_prefers_finalize_errors_over_delivery_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "video-run"
            self._write_metadata(run_dir, "video")
            attempt_dir = run_dir / "video_author" / "attempt_05"
            project_dir = attempt_dir / "project"
            project_dir.mkdir(parents=True)
            (project_dir / "index.html").write_text("<html></html>", encoding="utf-8")
            (attempt_dir / "video_author_manifest.json").write_text(
                json.dumps({"version": 1, "scenes": []}), encoding="utf-8"
            )
            delivery = {"error_message": "delivery failed"}
            (attempt_dir / "video_author_delivery_errors.json").write_text(
                json.dumps(delivery), encoding="utf-8"
            )
            finalize = {
                "error_message": "final timing contract failed",
                "payload": {"issue_id": "video_finalize_timing"},
            }
            (attempt_dir / "video_author_finalize_errors.json").write_text(
                json.dumps(finalize), encoding="utf-8"
            )

            resume = _load_resume_state(run_dir)

            self.assertIsInstance(resume, dict)
            self.assertEqual(resume["prior_feedback"], finalize)
            self.assertEqual(resume["validation_feedback_history"], [finalize])

    def test_video_resume_falls_back_to_legacy_validation_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "video-run"
            self._write_metadata(run_dir, "video")
            attempt_dir = run_dir / "video_author" / "attempt_01"
            project_dir = attempt_dir / "project"
            project_dir.mkdir(parents=True)
            (project_dir / "index.html").write_text("<html></html>", encoding="utf-8")
            (attempt_dir / "video_author_manifest.json").write_text(
                json.dumps({"version": 1, "scenes": []}), encoding="utf-8"
            )
            feedback = {"errors": ["legacy static validation failed"]}
            (attempt_dir / "video_author_validation_errors.json").write_text(
                json.dumps(feedback), encoding="utf-8"
            )

            resume = _load_resume_state(run_dir)

            self.assertIsInstance(resume, dict)
            self.assertEqual(resume["prior_feedback"], feedback)

    def test_resume_metadata_keeps_only_custom_command_fingerprint(self) -> None:
        command = "company-author --token super-secret --model private"
        settings = self._settings(
            designer_author_mode="external",
            designer_author_harness="custom",
            designer_author_cmd=command,
            designer_author_model="company-model",
        )

        metadata = _designer_author_resume_metadata(settings)

        serialized = json.dumps(metadata, sort_keys=True)
        self.assertEqual(metadata["mode"], "external")
        self.assertEqual(metadata["harness"], "custom")
        self.assertEqual(metadata["model"], "company-model")
        self.assertRegex(str(metadata["custom_command_sha256"]), r"^[0-9a-f]{64}$")
        self.assertNotIn(command, serialized)
        self.assertNotIn("super-secret", serialized)

    def test_bare_resume_restores_standard_external_harness(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir) / "out"
            run_dir = out_dir / "runs" / "resume-codex"
            run_dir.mkdir(parents=True)
            persisted = {
                "mode": "external",
                "harness": "codex",
                "model": "gpt-5.5",
                "custom_command_sha256": None,
            }
            resume_ctx = {
                "resume_state_json": {"designer_author": persisted},
            }
            observed: dict[str, object] = {}

            def fake_run_inner(runner_self: PipelineRunner, *args, **kwargs) -> RunResult:
                del args, kwargs
                observed["mode"] = runner_self.settings.designer_author_mode
                observed["harness"] = runner_self.settings.designer_author_harness
                observed["model"] = runner_self.settings.designer_author_model
                observed["command"] = runner_self.settings.designer_author_cmd
                observed["author"] = _make_designer_author(
                    runner_self.settings,
                    "system",
                    artifact_hint="landing",
                )
                return RunResult(
                    run_id=run_dir.name,
                    run_dir=str(run_dir),
                    artifact_type="landing",
                    terminal_status="pass",
                )

            settings = self._settings(
                out_dir=out_dir,
                designer_author_mode="internal",
                designer_author_harness="custom",
                designer_author_cmd="",
                designer_author_model=None,
            )
            with (
                patch("autodesign.runner._load_resume_state", return_value=resume_ctx),
                patch.object(PipelineRunner, "_run_inner", fake_run_inner),
                patch(
                    "autodesign.runner.DesignerLoop",
                    side_effect=AssertionError("bare external resume used DesignerLoop"),
                ),
                patch("autodesign.runner.ExternalLandingAuthor", return_value="landing-author"),
            ):
                result = PipelineRunner(settings).run("", resume_run=run_dir.name)

            self.assertEqual(result.terminal_status, "pass")
            self.assertEqual(observed["mode"], "external")
            self.assertEqual(observed["harness"], "codex")
            self.assertEqual(observed["model"], "gpt-5.5")
            self.assertIn("codex", str(observed["command"]))
            self.assertEqual(observed["author"], "landing-author")

    def test_custom_resume_rejects_missing_or_mismatched_command(self) -> None:
        persisted_settings = self._settings(
            designer_author_mode="external",
            designer_author_harness="custom",
            designer_author_cmd="company-author --profile expected",
        )
        persisted = _designer_author_resume_metadata(persisted_settings)
        resume_ctx = {"resume_state_json": {"designer_author": persisted}}

        for current_command in ("", "company-author --profile different"):
            with self.subTest(current_command=current_command or "<missing>"):
                with tempfile.TemporaryDirectory() as temp_dir:
                    out_dir = Path(temp_dir) / "out"
                    run_dir = out_dir / "runs" / "resume-custom"
                    run_dir.mkdir(parents=True)
                    settings = self._settings(
                        out_dir=out_dir,
                        designer_author_mode="internal",
                        designer_author_harness="custom",
                        designer_author_cmd=current_command,
                    )
                    with (
                        patch("autodesign.runner._load_resume_state", return_value=resume_ctx),
                        patch.object(
                            PipelineRunner,
                            "_run_inner",
                            side_effect=AssertionError("resume must refuse before authoring"),
                        ),
                    ):
                        result = PipelineRunner(settings).run("", resume_run=run_dir.name)

                    self.assertEqual(result.terminal_status, "fail")
                    self.assertIn("custom command", result.finalize_notes.lower())
                    self.assertNotIn("expected", result.finalize_notes)

    def test_custom_resume_accepts_matching_command(self) -> None:
        command = "company-author --profile expected"
        persisted = _designer_author_resume_metadata(
            self._settings(
                designer_author_mode="external",
                designer_author_harness="custom",
                designer_author_cmd=command,
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir) / "out"
            run_dir = out_dir / "runs" / "resume-custom"
            run_dir.mkdir(parents=True)
            settings = self._settings(
                out_dir=out_dir,
                designer_author_mode="internal",
                designer_author_harness="custom",
                designer_author_cmd=command,
            )
            resume_ctx = {"resume_state_json": {"designer_author": persisted}}

            def fake_run_inner(runner_self: PipelineRunner, *args, **kwargs) -> RunResult:
                del args, kwargs
                self.assertEqual(runner_self.settings.designer_author_mode, "external")
                self.assertEqual(runner_self.settings.designer_author_cmd, command)
                return RunResult(
                    run_id=run_dir.name,
                    run_dir=str(run_dir),
                    artifact_type="video",
                    terminal_status="pass",
                )

            with (
                patch("autodesign.runner._load_resume_state", return_value=resume_ctx),
                patch.object(PipelineRunner, "_run_inner", fake_run_inner),
            ):
                result = PipelineRunner(settings).run("", resume_run=run_dir.name)

            self.assertEqual(result.terminal_status, "pass")

    def test_restore_maps_resume_state_to_artifact_specific_counter(self) -> None:
        for artifact_type, state_prefix in (
            ("landing", "landing_author"),
            ("deck", "slides_author"),
            ("video", "video_author"),
        ):
            with self.subTest(artifact_type=artifact_type):
                ctx = SimpleNamespace(state={})
                previous = Path("/tmp/run") / state_prefix / "attempt_02"
                resume = {
                    "artifact_type": artifact_type,
                    "author_state_prefix": state_prefix,
                    "prior_attempts": 2,
                    "attempt_records": [{"attempt": 2}],
                    "validation_feedback_history": [{"status": "error"}],
                    "prior_feedback": {"status": "error"},
                    "previous_attempt_dir": previous,
                    "previous_output_path": previous / "output.html",
                    "source_run_dir": "/tmp/run",
                    "incremental_budget": True,
                }

                _restore_external_author_resume_state(ctx, resume)

                self.assertEqual(ctx.state["artifact_type"], artifact_type)
                self.assertEqual(ctx.state[f"{state_prefix}_attempts"], 2)
                self.assertEqual(
                    ctx.state[f"{state_prefix}_resume"],
                    ctx.state["external_author_resume"],
                )
                self.assertEqual(
                    ctx.state["external_author_resume"]["previous_attempt_dir"],
                    str(previous),
                )

    def test_restore_preserves_poster_resume_keys(self) -> None:
        ctx = SimpleNamespace(state={})
        previous = Path("/tmp/run/designer_author/attempt_07")
        resume = {
            "artifact_type": "poster",
            "author_state_prefix": "designer_author",
            "prior_attempts": 7,
            "attempt_records": [{"attempt": 7}],
            "validation_feedback_history": [{"attempt": 7}],
            "prior_feedback": {"attempt": 7},
            "previous_attempt_dir": previous,
            "previous_output_path": previous / "poster.html",
            "source_run_dir": "/tmp/run",
            "incremental_budget": True,
        }

        _restore_external_author_resume_state(ctx, resume)

        self.assertEqual(ctx.state["designer_author_attempts"], 7)
        self.assertEqual(ctx.state["designer_author_attempt_records"], [{"attempt": 7}])
        self.assertEqual(ctx.state["designer_author_validation_feedback"], [{"attempt": 7}])
        self.assertEqual(ctx.state["designer_author_last_feedback"], {"attempt": 7})
        self.assertEqual(
            ctx.state["designer_author_resume"],
            {
                "prior_attempts": 7,
                "previous_attempt_dir": str(previous),
                "repair_feedback": {"attempt": 7},
                "source_run_dir": "/tmp/run",
                "incremental_budget": True,
            },
        )
        self.assertNotIn("external_author_resume", ctx.state)

    def test_resume_ingest_reload_failure_returns_complete_error_message(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir) / "out"
            run_id = "resume-video"
            run_dir = out_dir / "runs" / run_id
            run_dir.mkdir(parents=True)
            settings = self._settings(
                out_dir=out_dir,
                designer_author_mode="external",
                designer_author_harness="codex",
                designer_author_model="gpt-5.5",
            )
            resume_ctx = {
                "run_brief_json": {
                    "version": 1,
                    "raw_user_brief": "Create a conference video.",
                    "final_designer_input": "Create a conference video.",
                    "skip_enhancer": True,
                },
                "resume_state_json": {
                    "attachments": [],
                    "skill_bundle_ids": [],
                },
                "canvas_plan": {"artifact_type": "video"},
                "deck_plan": {},
                "artifact_type": "video",
                "author_state_prefix": "video_author",
                "source_run_dir": str(run_dir),
                "prior_attempts": 2,
                "attempt_records": [],
                "validation_feedback_history": [],
                "superseded_attempt_dirs": [],
                "prior_feedback": {},
                "previous_attempt_dir": (
                    run_dir / "video_author" / "attempt_02"
                ),
                "previous_output_path": (
                    run_dir / "video_author" / "attempt_02" / "project"
                ),
                "prior_feedback_issue_id": "",
                "incremental_budget": True,
            }
            reload_error = ToolResultRecord(
                status="error",
                error_message=(
                    "resume ingest is missing required artifacts: "
                    "paper_visual_provenance.json"
                ),
                error_category="validation",
                payload={"issue_id": "reuse_ingest_artifacts_missing"},
            )

            with (
                patch("autodesign.runner._preflight_video_runtime"),
                patch(
                    "autodesign.tools.ingest_document._load_ingest_state_from_dir",
                    return_value=reload_error,
                ),
            ):
                result = PipelineRunner(settings)._run_inner(
                    "",
                    [],
                    None,
                    True,
                    True,
                    run_id,
                    None,
                    resume_ctx,
                )

        self.assertEqual(result.terminal_status, "fail")
        self.assertIn(
            "reuse_ingest_artifacts_missing",
            result.finalize_notes,
        )
        self.assertIn(
            "paper_visual_provenance.json",
            result.finalize_notes,
        )


if __name__ == "__main__":
    unittest.main()
