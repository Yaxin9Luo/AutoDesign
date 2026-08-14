from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from autodesign.config import Settings
from autodesign.runner import PipelineRunner
from autodesign.video_runtime import VideoRuntimeUnavailableError
from scripts import web_server


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


class EnvironmentDiagnosticsTest(unittest.TestCase):
    @staticmethod
    def _settings(out_dir: Path) -> Settings:
        return Settings(
            anthropic_api_key="",
            anthropic_base_url=None,
            gemini_api_key="",
            designer_model="designer",
            critic_model="critic",
            repo_root=Path(__file__).resolve().parents[1],
            out_dir=out_dir,
        )

    def test_core_video_runtime_guard_stops_before_enhancer_or_author(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            out_dir = Path(raw_tmp)
            settings = self._settings(out_dir)
            missing_profile = {
                "ready": False,
                "missing": ["hyperframes==0.7.86", "ffmpeg"],
                "repair": "Run `autodesign setup` and install ffmpeg.",
            }

            with (
                patch(
                    "autodesign.runner.video_environment_profile",
                    return_value=missing_profile,
                ),
                patch(
                    "autodesign.runner._run_enhancer",
                    side_effect=AssertionError("enhancer called"),
                ),
                patch(
                    "autodesign.runner._make_designer_author",
                    side_effect=AssertionError("designer called"),
                ),
                self.assertRaises(VideoRuntimeUnavailableError) as raised,
            ):
                PipelineRunner(settings).run(
                    "Create a 1920x1080 conference video.",
                    run_id="video-runtime-preflight",
                )

        self.assertEqual(
            raised.exception.missing,
            ["hyperframes==0.7.86", "ffmpeg"],
        )
        self.assertIn("autodesign setup", raised.exception.repair)

    def test_resumed_video_uses_persisted_artifact_type_for_runtime_guard(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            out_dir = Path(raw_tmp)
            run_dir = out_dir / "runs" / "resume-video"
            run_dir.mkdir(parents=True)
            settings = self._settings(out_dir)
            resume_ctx = {
                "run_brief_json": {
                    "version": 1,
                    "raw_user_brief": "Create a conference video.",
                    "final_designer_input": "Create a conference video.",
                    "effective_template": None,
                    "skip_enhancer": True,
                },
                "resume_state_json": {"attachments": []},
                "canvas_plan": {},
                "deck_plan": {},
                "artifact_type": "video",
                "source_run_dir": str(run_dir),
                "prior_attempts": 1,
                "prior_feedback_issue_id": "",
                "incremental_budget": True,
            }
            missing_profile = {
                "ready": False,
                "missing": ["ffmpeg"],
                "repair": "Run `autodesign setup`.",
            }

            with (
                patch("autodesign.runner._load_resume_state", return_value=resume_ctx),
                patch(
                    "autodesign.runner.video_environment_profile",
                    return_value=missing_profile,
                ),
                patch(
                    "autodesign.runner._make_designer_author",
                    side_effect=AssertionError("designer called"),
                ),
                self.assertRaises(VideoRuntimeUnavailableError) as raised,
            ):
                PipelineRunner(settings).run("", resume_run=run_dir.name)

        self.assertEqual(raised.exception.missing, ["ffmpeg"])

    def test_video_runtime_guard_fails_before_generation_with_repair_details(self) -> None:
        guard = getattr(web_server, "_require_artifact_runtime", None)
        self.assertTrue(
            callable(guard),
            "artifact admission must use a runtime guard",
        )
        profile = {
            "video": {
                "ready": False,
                "missing": ["hyperframes==0.7.86", "ffmpeg"],
                "repair": "Run `autodesign setup` and install ffmpeg.",
            }
        }

        with self.assertRaises(HTTPException) as raised:
            guard("video", environment=profile)

        self.assertEqual(raised.exception.status_code, 412)
        self.assertEqual(raised.exception.detail["code"], "video_runtime_unavailable")
        self.assertEqual(
            raised.exception.detail["missing"],
            ["hyperframes==0.7.86", "ffmpeg"],
        )
        self.assertIn("autodesign setup", raised.exception.detail["repair"])

    def test_non_video_artifacts_do_not_require_video_runtime(self) -> None:
        guard = getattr(web_server, "_require_artifact_runtime", None)
        self.assertTrue(
            callable(guard),
            "artifact admission must use a runtime guard",
        )
        guard(
            "poster",
            environment={
                "video": {
                    "ready": False,
                    "missing": ["hyperframes==0.7.86"],
                    "repair": "Run `autodesign setup`.",
                }
            },
        )

    def test_environment_profile_reports_complete_video_runtime(self) -> None:
        profile_builder = getattr(web_server, "_environment_profile", None)
        self.assertTrue(
            callable(profile_builder),
            "backend must expose one environment profile builder",
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runtime = (
                root
                / "runtime"
                / "video"
                / "node_modules"
                / ".bin"
                / "hyperframes"
            )
            fake_bin = root / "bin"
            _write_executable(runtime, "#!/bin/sh\necho 0.7.86\n")
            _write_executable(fake_bin / "node", "#!/bin/sh\necho v22.14.0\n")
            _write_executable(fake_bin / "ffmpeg", "#!/bin/sh\necho ffmpeg 7.1\n")
            _write_executable(fake_bin / "ffprobe", "#!/bin/sh\necho ffprobe 7.1\n")

            profile = profile_builder(
                repo_root=root,
                path_env=str(fake_bin),
            )

        video = profile["video"]
        self.assertTrue(video["ready"])
        self.assertEqual(video["hyperframes"]["version"], "0.7.86")
        self.assertEqual(video["hyperframes"]["source"], "managed_runtime")
        self.assertTrue(video["node"]["compatible"])
        self.assertTrue(video["ffmpeg"]["available"])
        self.assertTrue(video["ffprobe"]["available"])
        self.assertEqual(video["missing"], [])

    def test_environment_profile_names_every_missing_video_prerequisite(self) -> None:
        profile_builder = getattr(web_server, "_environment_profile", None)
        self.assertTrue(
            callable(profile_builder),
            "backend must expose one environment profile builder",
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            profile = profile_builder(
                repo_root=Path(raw_tmp),
                path_env="",
            )

        video = profile["video"]
        self.assertFalse(video["ready"])
        self.assertEqual(
            video["missing"],
            ["node>=22", "hyperframes==0.7.86", "ffmpeg", "ffprobe"],
        )
        self.assertIn("autodesign setup", video["repair"])


if __name__ == "__main__":
    unittest.main()
