from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import subprocess

from autodesign.util.io import sha256_file
from autodesign.util.design_spec_fingerprint import design_spec_sha256
from scripts import web_server
from scripts.web_server import (
    TYPE_PROLOGUE,
    _build_video_artifact,
    _list_produced_artifacts,
    _resolve_editable_video_src,
    _run_editable_video_render,
    _validated_video_delivery,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _passed_delivery(root: Path) -> tuple[Path, Path]:
    project = root / "hyperframes-paper-video"
    renders = project / "renders"
    renders.mkdir(parents=True)
    source = project / "index.html"
    contract = project / "video_delivery_contract.json"
    probe_path = project / "media_probe.json"
    mp4 = renders / "paper-video.mp4"
    narration_dir = project / "narration"
    narration_dir.mkdir()
    assets_dir = project / "assets"
    assets_dir.mkdir()
    narration_audio = assets_dir / "narration.wav"
    transcript = narration_dir / "transcript.en.txt"
    srt = narration_dir / "subtitles.en.srt"
    vtt = narration_dir / "subtitles.en.vtt"
    voice = narration_dir / "voice.json"
    timing = narration_dir / "timing.json"
    figure = assets_dir / "figure.png"
    figure.write_bytes(b"source figure")
    source.write_text(
        '<!doctype html><title>video</title><img src="assets/figure.png">',
        encoding="utf-8",
    )
    contract.write_text('{"target_duration_s":360}\n', encoding="utf-8")
    probe = {
        "video_codec": "h264",
        "pixel_format": "yuv420p",
        "audio_codec": "aac",
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "duration_s": 360.0,
        "subtitle_codec": "mov_text",
        "subtitle_forced": False,
    }
    probe_path.write_text(json.dumps(probe) + "\n", encoding="utf-8")
    mp4.write_bytes(b"fresh manifest-bound mp4")
    for path, content in {
        narration_audio: b"audio",
        transcript: b"English transcript\n",
        srt: b"1\n00:00:00,000 --> 00:00:01,000\nEnglish\n",
        vtt: b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nEnglish\n",
        voice: b'{"preset":"female"}\n',
        timing: b"[]\n",
    }.items():
        path.write_bytes(content)
    design_spec = {
        "brief": "Conference video",
        "artifact_type": "video",
        "canvas": {"w_px": 1920, "h_px": 1080},
    }
    design_spec_hash = design_spec_sha256(design_spec)
    (root / "design_spec.json").write_text(
        json.dumps(
            {
                "artifact_type": "video",
                "revision": 1,
                "design_spec_sha256": design_spec_hash,
                "design_spec": design_spec,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = {
        "status": "passed",
        "design_spec_sha256": design_spec_hash,
        "design_spec_revision": 1,
        "source_html_path": "index.html",
        "contract_path": "video_delivery_contract.json",
        "media_probe_path": "media_probe.json",
        "mp4_path": "renders/paper-video.mp4",
        "narration_audio_path": "assets/narration.wav",
        "transcript_path": "narration/transcript.en.txt",
        "srt_path": "narration/subtitles.en.srt",
        "vtt_path": "narration/subtitles.en.vtt",
        "voice_metadata_path": "narration/voice.json",
        "narration_timing_path": "narration/timing.json",
        "render_started_at": "2026-07-20T12:00:00+00:00",
        "source_html_sha256": sha256_file(source),
        "contract_sha256": sha256_file(contract),
        "media_probe_sha256": sha256_file(probe_path),
        "mp4_sha256": sha256_file(mp4),
        "narration_audio_sha256": sha256_file(narration_audio),
        "transcript_sha256": sha256_file(transcript),
        "srt_sha256": sha256_file(srt),
        "vtt_sha256": sha256_file(vtt),
        "voice_metadata_sha256": sha256_file(voice),
        "narration_timing_sha256": sha256_file(timing),
        "local_asset_sha256": {"assets/figure.png": sha256_file(figure)},
        "media_probe": probe,
    }
    manifest_path = project / "delivery_manifest.json"
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    final_dir = root / "final"
    final_dir.mkdir()
    (final_dir / "video_delivery.json").write_text(
        json.dumps(
            {
                "manifest_path": manifest_path.relative_to(root).as_posix(),
                "manifest_sha256": sha256_file(manifest_path),
                "design_spec_sha256": design_spec_hash,
                "design_spec_revision": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path, mp4


class VideoWebDeliveryTest(unittest.TestCase):
    def _assert_passed_delivery(
        self,
        root: Path,
        mp4_path: Path,
        *,
        run_id: str = "video-run",
    ):
        delivery = _validated_video_delivery(root)
        self.assertTrue(delivery)
        self.assertEqual(delivery.reason_code, "passed")
        self.assertEqual(delivery[0], mp4_path.resolve())
        self.assertEqual(
            delivery.public_paths["mp4"],
            mp4_path.relative_to(root).as_posix(),
        )
        self.assertEqual(
            delivery.snapshots["mp4"].sha256,
            sha256_file(mp4_path),
        )
        artifact = _build_video_artifact(
            root,
            run_id,
            baseline_artifact_json=None,
        )
        self.assertIsNotNone(artifact)
        assert artifact is not None
        self.assertEqual(
            artifact.native_file_url,
            web_server._run_file_url(run_id, delivery.public_paths["mp4"]),
        )
        return delivery, artifact

    def _assert_invalid_delivery(
        self,
        root: Path,
        reason_code: str,
        *,
        run_id: str = "video-run",
    ) -> None:
        delivery = _validated_video_delivery(root)
        self.assertFalse(delivery)
        self.assertEqual(delivery.reason_code, reason_code)
        self.assertEqual(delivery.public_paths, {})
        self.assertEqual(delivery.snapshots, {})
        self.assertIsNone(
            _build_video_artifact(
                root,
                run_id,
                baseline_artifact_json=None,
            )
        )

    def test_current_delivery_rejects_media_drift_from_selected_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, _ = _passed_delivery(root)
            contract_path = manifest_path.parent / "video_delivery_contract.json"
            contract_path.write_text(
                '{"target_duration_s":300}\n',
                encoding="utf-8",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["contract_sha256"] = sha256_file(contract_path)
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            pointer_path = root / "final" / "video_delivery.json"
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            pointer["manifest_sha256"] = sha256_file(manifest_path)
            pointer_path.write_text(json.dumps(pointer) + "\n", encoding="utf-8")

            self._assert_invalid_delivery(root, "integrity_mismatch")

    def test_export_only_retry_endpoint_accepts_an_authored_failed_project(self) -> None:
        handler = getattr(web_server, "retry_video_export", None)
        self.assertTrue(
            callable(handler),
            "web backend must expose an export-only video retry endpoint",
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            runs_dir = Path(raw_tmp) / "runs"
            source_run = runs_dir / "source-video"
            project = source_run / "hyperframes-paper-video"
            project.mkdir(parents=True)
            (project / "index.html").write_text("<!doctype html>", encoding="utf-8")
            (project / "video_delivery_contract.json").write_text("{}\n", encoding="utf-8")
            (source_run / "design_spec.json").write_text("{}\n", encoding="utf-8")
            created: list[object] = []

            async def capture_start(**kwargs):
                created.append(kwargs)

            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(
                    web_server,
                    "_start_supervised_derived_job",
                    side_effect=capture_start,
                ),
                patch.object(web_server, "_RUN_ACCESS_CONTROL", False),
                patch.object(web_server, "_require_artifact_runtime"),
                patch.object(
                    web_server,
                    "_demo_register_derived_run_access",
                    return_value="user:test",
                ) as register_access,
            ):
                ack = asyncio.run(
                    handler(
                        "source-video",
                        SimpleNamespace(headers={}),
                        conversation_id="conversation-video",
                    )
                )

            state = web_server._RUNS.pop(ack.run_id)

        self.assertEqual(ack.progress_mode, "video_export")
        self.assertEqual(state.artifact_type, "video")
        self.assertEqual(state.baseline_artifact_json, '{"artifact_id": "art_source-video"}')
        self.assertEqual(state.conversation_id, "conversation-video")
        self.assertEqual(state.demo_user_id, "user:test")
        register_access.assert_called_once()
        self.assertEqual(len(created), 1)

    def test_export_only_background_preserves_authoring_failure_route(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            runs_dir = Path(raw_tmp) / "runs"
            source_run = runs_dir / "source-video"
            source_project = source_run / "hyperframes-paper-video"
            source_project.mkdir(parents=True)
            (source_project / "index.html").write_text(
                "<!doctype html>",
                encoding="utf-8",
            )
            (source_run / "design_spec.json").write_text("{}\n", encoding="utf-8")
            state = web_server._RunState(
                artifact_type="video",
                conversation_id="conversation-video",
            )
            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "_settings_or_boot", return_value=SimpleNamespace()),
                patch.object(web_server, "_append_event"),
                patch.object(web_server, "_persisted_run_log"),
                patch.object(web_server, "_list_produced_artifacts", return_value=[]),
                patch(
                    "autodesign.tools.export_video.retry_video_export_project",
                    return_value={
                        "ok": False,
                        "phase": "authoring_lint",
                        "error": "invalid authored clip nesting",
                    },
                ),
            ):
                asyncio.run(
                    web_server._retry_video_export_in_background(
                        run_id="retry-video",
                        source_run_id="source-video",
                        source_project=source_project,
                        state=state,
                        conversation_id="conversation-video",
                        baseline_artifact_json='{"artifact_id":"art_source-video"}',
                    )
                )

        self.assertIsNotNone(state.result_message)
        self.assertIsNotNone(state.result_message.failure)
        self.assertEqual(state.result_message.failure.phase, "authoring_lint")
        self.assertEqual(state.result_message.failure.retry_route, "full_authoring")
        self.assertEqual(state.result_message.failure.parent_run_id, "source-video")

    def test_export_only_background_does_not_route_runtime_failures_to_authoring(
        self,
    ) -> None:
        cases = (
            ("tts", "Kokoro narration synthesis failed", "export_only"),
            ("authoring_lint", "permission denied while starting CLI", "setup_required"),
        )
        for phase, error, expected_route in cases:
            with self.subTest(phase=phase, error=error):
                with tempfile.TemporaryDirectory() as raw_tmp:
                    runs_dir = Path(raw_tmp) / "runs"
                    source_run = runs_dir / "source-video"
                    source_project = source_run / "hyperframes-paper-video"
                    source_project.mkdir(parents=True)
                    (source_project / "index.html").write_text(
                        "<!doctype html>",
                        encoding="utf-8",
                    )
                    (source_run / "design_spec.json").write_text(
                        "{}\n",
                        encoding="utf-8",
                    )
                    state = web_server._RunState(
                        artifact_type="video",
                        conversation_id="conversation-video",
                    )
                    with (
                        patch.object(web_server, "RUNS_DIR", runs_dir),
                        patch.object(
                            web_server,
                            "_settings_or_boot",
                            return_value=SimpleNamespace(),
                        ),
                        patch.object(web_server, "_append_event"),
                        patch.object(web_server, "_persisted_run_log"),
                        patch.object(
                            web_server,
                            "_list_produced_artifacts",
                            return_value=[],
                        ),
                        patch(
                            "autodesign.tools.export_video.retry_video_export_project",
                            return_value={
                                "ok": False,
                                "phase": phase,
                                "error": error,
                            },
                        ),
                    ):
                        asyncio.run(
                            web_server._retry_video_export_in_background(
                                run_id="retry-video",
                                source_run_id="source-video",
                                source_project=source_project,
                                state=state,
                                conversation_id="conversation-video",
                                baseline_artifact_json=(
                                    '{"artifact_id":"art_source-video"}'
                                ),
                            )
                        )

                self.assertIsNotNone(state.result_message)
                self.assertIsNotNone(state.result_message.failure)
                self.assertEqual(
                    state.result_message.failure.retry_route,
                    expected_route,
                )

    def test_export_retry_route_separates_content_from_runtime_failures(self) -> None:
        cases = (
            ("tts", "Narration must be shortened for scenes: scene_03", "full_authoring"),
            ("tts", "speech coverage 0.55 is below minimum", "full_authoring"),
            (
                "render",
                "rendered duration 358.0 does not match the authored timeline 360.0",
                "full_authoring",
            ),
            ("render", "ffprobe not found; MP4 cannot be validated", "setup_required"),
            ("tts", "Kokoro narration synthesis failed", "export_only"),
        )

        for phase, error, expected in cases:
            with self.subTest(phase=phase, error=error):
                self.assertEqual(
                    web_server._video_export_retry_route(
                        phase=phase,
                        error=error,
                    ),
                    expected,
                )

    def test_history_rehydrate_preserves_export_retry_failure_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            runs_dir = Path(raw_tmp) / "runs"
            events = [{
                "event": "artifact.generation_failed",
                "run_id": "retry-video",
                "_ts_ms": 1,
                "data": {
                    "status": "error",
                    "artifact_type": "video",
                    "source": "video_export_retry",
                    "parent_run_id": "source-video",
                    "phase": "authoring_lint",
                    "retry_route": "full_authoring",
                    "error": "invalid authored clip nesting",
                },
            }]
            with patch.object(web_server, "RUNS_DIR", runs_dir):
                conversation = web_server._conversation_from_design_events(
                    "conversation-video",
                    events,
                    set(),
                )

        self.assertIsNotNone(conversation)
        failure = conversation["messages"][0]["failure"]
        self.assertEqual(failure["phase"], "authoring_lint")
        self.assertEqual(failure["retry_route"], "full_authoring")
        self.assertEqual(failure["parent_run_id"], "source-video")

    def test_failed_video_scaffold_is_not_reported_as_render_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "runs" / "failed-video" / "hyperframes-video"
            project.mkdir(parents=True)
            (project / "index.html").write_text("<!doctype html>", encoding="utf-8")

            with patch("scripts.web_server._settings_or_boot", return_value=root):
                produced = _list_produced_artifacts("failed-video")

        self.assertEqual(
            produced,
            ["hyperframes-video/index.html (video scaffolded; MP4 not produced)"],
        )

    def test_web_video_prologue_uses_conference_delivery_contract(self) -> None:
        prologue = TYPE_PROLOGUE["video"]

        self.assertIn("300-600 s", prologue)
        self.assertIn("paper complexity", prologue.lower())
        self.assertIn("10-14", prologue)
        self.assertIn("SRT/VTT", prologue)
        self.assertNotIn("first produce a landing page", prologue.lower())
        self.assertNotIn("120 s", prologue)

    def test_only_manifest_bound_mp4_is_publishable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, mp4 = _passed_delivery(root)

            delivery, _artifact = self._assert_passed_delivery(root, mp4)
            original_sha256 = delivery.snapshots["mp4"].sha256

            mp4.write_bytes(b"replaced or stale render")
            self.assertNotEqual(sha256_file(mp4), original_sha256)
            self._assert_invalid_delivery(root, "integrity_mismatch")

    def test_video_artifact_exposes_an_optional_webvtt_track(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _manifest_path, mp4_path = _passed_delivery(root)
            _delivery, artifact = self._assert_passed_delivery(root, mp4_path)

            self.assertEqual(
                artifact.downloads.get("vtt"),
                web_server._run_file_url(
                    "video-run",
                    "hyperframes-paper-video/narration/subtitles.en.vtt",
                ),
            )

    def test_delivery_without_an_optional_subtitle_track_is_not_publishable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, mp4_path = _passed_delivery(root)
            self._assert_passed_delivery(root, mp4_path)
            probe_path = manifest_path.parent / "media_probe.json"
            probe = json.loads(probe_path.read_text(encoding="utf-8"))
            self.assertFalse(probe["subtitle_forced"])
            probe["subtitle_forced"] = True
            probe_path.write_text(json.dumps(probe) + "\n", encoding="utf-8")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["media_probe"] = probe
            manifest["media_probe_sha256"] = sha256_file(probe_path)
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            pointer_path = root / "final" / "video_delivery.json"
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            pointer["manifest_sha256"] = sha256_file(manifest_path)
            pointer_path.write_text(json.dumps(pointer) + "\n", encoding="utf-8")

            self.assertEqual(manifest["media_probe_sha256"], sha256_file(probe_path))
            self.assertEqual(pointer["manifest_sha256"], sha256_file(manifest_path))
            self._assert_invalid_delivery(root, "integrity_mismatch")

    def test_legacy_delivery_without_a_muxed_track_remains_publishable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, mp4_path = _passed_delivery(root)
            probe_path = manifest_path.parent / "media_probe.json"
            probe = json.loads(probe_path.read_text(encoding="utf-8"))
            probe.pop("subtitle_codec")
            probe.pop("subtitle_forced")
            probe_path.write_text(json.dumps(probe) + "\n", encoding="utf-8")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["media_probe"] = probe
            manifest["media_probe_sha256"] = sha256_file(probe_path)
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            pointer_path = root / "final" / "video_delivery.json"
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            pointer["manifest_sha256"] = sha256_file(manifest_path)
            pointer_path.write_text(json.dumps(pointer) + "\n", encoding="utf-8")

            delivery, _artifact = self._assert_passed_delivery(root, mp4_path)
            self.assertEqual(delivery.reason_code, "passed")

    def test_unmanifested_mp4_is_not_publishable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _manifest_path, mp4_path = _passed_delivery(root)
            self._assert_passed_delivery(root, mp4_path)
            (root / "final" / "video_delivery.json").unlink()

            self.assertTrue(mp4_path.is_file())
            self._assert_invalid_delivery(root, "pointer_missing")

    def test_malformed_final_pointer_is_not_publishable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _manifest_path, mp4_path = _passed_delivery(root)
            self._assert_passed_delivery(root, mp4_path)
            pointer_path = root / "final" / "video_delivery.json"
            pointer_path.write_text(
                "[]", encoding="utf-8"
            )

            self.assertEqual(json.loads(pointer_path.read_text(encoding="utf-8")), [])
            self._assert_invalid_delivery(root, "pointer_malformed")

    def test_manifest_for_an_older_persisted_design_spec_is_not_publishable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _manifest_path, mp4_path = _passed_delivery(root)
            self._assert_passed_delivery(root, mp4_path)
            revised = {
                "brief": "Revised conference video",
                "artifact_type": "video",
                "canvas": {"w_px": 1920, "h_px": 1080},
            }
            (root / "design_spec.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "video",
                        "revision": 2,
                        "design_spec_sha256": design_spec_sha256(revised),
                        "design_spec": revised,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            persisted = json.loads(
                (root / "design_spec.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["revision"], 2)
            self.assertEqual(
                persisted["design_spec_sha256"],
                design_spec_sha256(persisted["design_spec"]),
            )
            self._assert_invalid_delivery(root, "stale_design_spec")

    def test_changed_html_referenced_asset_is_not_publishable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, mp4_path = _passed_delivery(root)
            self._assert_passed_delivery(root, mp4_path)
            figure = root / "hyperframes-paper-video" / "assets" / "figure.png"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected_figure_sha256 = manifest["local_asset_sha256"][
                "assets/figure.png"
            ]
            self.assertEqual(sha256_file(figure), expected_figure_sha256)
            figure.write_bytes(b"changed source figure")

            self.assertNotEqual(sha256_file(figure), expected_figure_sha256)
            self._assert_invalid_delivery(root, "integrity_mismatch")

    @patch("scripts.web_server.subprocess.run")
    def test_editable_render_uses_pinned_cli_and_exact_fresh_output(self, run_mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)

            def _render(command, **kwargs):
                output_rel = command[command.index("--output") + 1]
                output = project / output_rel
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"fresh editable mp4")
                return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

            run_mock.side_effect = _render
            _, ok, mp4 = _run_editable_video_render(project, 30)

        self.assertTrue(ok)
        self.assertIsNotNone(mp4)
        command = run_mock.call_args.args[0]
        self.assertEqual(
            command[0],
            str(REPO_ROOT / "runtime" / "video" / "node_modules" / ".bin" / "hyperframes"),
        )
        self.assertIn("--output", command)
        self.assertIn("--strict", command)
        self.assertIn("--no-best-effort", command)

    def test_editable_video_rejects_unapproved_or_escaping_image_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            for source in ("../../.env", "/tmp/private.png", "file:///tmp/private.png"):
                with self.assertRaisesRegex(ValueError, "approved local asset URL"):
                    _resolve_editable_video_src(source, project)


if __name__ == "__main__":
    unittest.main()
