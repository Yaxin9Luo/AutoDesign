from __future__ import annotations

from dataclasses import asdict, fields, replace
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

import autodesign.run_worker as run_worker
import autodesign.run_worker_protocol as worker_protocol
from autodesign.config import Settings
from autodesign.schema import RunResult
from autodesign.run_worker_protocol import (
    ArtifactEditWorkerRequest,
    AttemptForkWorkerRequest,
    EditableVideoRenderWorkerRequest,
    PipelineWorkerRequest,
    PosterCodeEditWorkerRequest,
    ProtocolError,
    PptxExportWorkerRequest,
    VideoExportRetryWorkerRequest,
    decode_worker_result,
    decode_request,
    encode_request,
)
from autodesign.run_control import CancellationToken
from autodesign.run_supervisor import _read_worker_result
from autodesign.run_worker import _run_poster_code_edit


def _settings(root: Path, secret: str = "stdin-only-secret") -> Settings:
    return Settings(
        anthropic_api_key=secret,
        anthropic_base_url="https://example.invalid/anthropic",
        gemini_api_key="gemini-secret",
        designer_model="designer/model:v1",
        critic_model="critic/model:v2",
        anthropic_auth_token="auth-token",
        anthropic_custom_headers={"X-Trace": "trace", "X-Private": "header-secret"},
        openai_compat_api_key="openai-secret",
        openrouter_api_key="openrouter-secret",
        harness_api_key="harness-secret",
        openresearch_token="openresearch-secret",
        repo_root=root / "repo",
        fonts_dir=root / "fonts",
        prompts_dir=root / "prompts",
        skills_dir=root / "skills",
        out_dir=root / "out",
        fonts={"Exact Font": "ExactFont.ttf"},
        designer_author_model="author/model",
        code_editor_model="editor/model",
    )


class RunWorkerProtocolTests(unittest.TestCase):
    @staticmethod
    def _candidate_publish_request_type():
        request_type = getattr(
            worker_protocol,
            "CandidatePublishWorkerRequest",
            None,
        )
        if request_type is None:
            raise AssertionError(
                "candidate publish must be a first-class supervised worker request"
            )
        return request_type

    @staticmethod
    def _legacy_candidate_publish_envelope(request) -> BytesIO:
        payload = asdict(request)
        payload["settings"] = worker_protocol.settings_to_payload(request.settings)
        payload.pop("source_attempt", None)
        payload.pop("expected_candidate_sha256", None)
        body = json.dumps(
            {"version": worker_protocol.PROTOCOL_VERSION, "request": payload},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        return BytesIO(worker_protocol._LENGTH.pack(len(body)) + body)

    def test_artifact_edit_request_and_result_round_trip(self) -> None:
        request_type = getattr(
            worker_protocol,
            "ArtifactEditWorkerRequest",
            None,
        )
        self.assertIsNotNone(
            request_type,
            "artifact edits must be a first-class supervised worker request",
        )
        assert request_type is not None
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            settings = _settings(root)
            input_path = root / "out" / "runs" / "edited-child" / "inputs" / "artifact_edit.json"
            request = request_type(
                job_kind="artifact_edit",
                run_id="edited-child",
                parent_run_id="source-run",
                input_path=str(input_path),
                conversation_id="conversation-edit",
                settings=settings,
            )

            decoded = decode_request(BytesIO(encode_request(request)))

            self.assertEqual(decoded, request)
            result = {
                "run_id": request.run_id,
                "artifact_type": "landing",
                "source_path": str(
                    settings.out_dir
                    / "runs"
                    / request.run_id
                    / "final"
                    / "index.html"
                ),
                "restored_layer_ids": ["title"],
                "skipped": [],
                "candidate_lineage": {},
            }
            payload = {
                "job_kind": request.job_kind,
                "run_id": request.run_id,
                "ok": True,
                "result": result,
            }
            self.assertEqual(
                decode_worker_result(
                    payload,
                    expected_run_id=request.run_id,
                    expected_job_kind=request.job_kind,
                ),
                payload,
            )

    def test_artifact_edit_worker_threads_exact_cancellation_token(self) -> None:
        request_type = getattr(worker_protocol, "ArtifactEditWorkerRequest", None)
        runner = getattr(run_worker, "_run_artifact_edit", None)
        self.assertIsNotNone(request_type)
        self.assertIsNotNone(runner)
        assert request_type is not None
        assert runner is not None
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            settings = _settings(root)
            runs_dir = settings.out_dir / "runs"
            (runs_dir / "source-run").mkdir(parents=True)
            input_path = runs_dir / "edited-child" / "inputs" / "artifact_edit.json"
            input_path.parent.mkdir(parents=True)
            input_path.write_text("{}", encoding="utf-8")
            request = request_type(
                job_kind="artifact_edit",
                run_id="edited-child",
                parent_run_id="source-run",
                input_path=str(input_path),
                conversation_id="conversation-edit",
                settings=settings,
            )
            token = CancellationToken.never(request.run_id)
            expected = {
                "run_id": request.run_id,
                "artifact_type": "landing",
                "source_path": str(runs_dir / request.run_id / "final" / "index.html"),
                "restored_layer_ids": [],
                "skipped": [],
                "candidate_lineage": {},
            }

            with patch(
                "autodesign.artifact_edit_job.run_artifact_edit_job",
                return_value=expected,
            ) as edit_job:
                result = runner(request, token)

            self.assertEqual(result, expected)
            self.assertIs(edit_job.call_args.kwargs["cancellation_token"], token)
            self.assertEqual(edit_job.call_args.kwargs["run_id"], request.run_id)
            self.assertEqual(
                edit_job.call_args.kwargs["parent_run_id"],
                request.parent_run_id,
            )
            self.assertEqual(
                edit_job.call_args.kwargs["input_path"],
                input_path.resolve(),
            )

    def test_candidate_publish_request_and_result_round_trip(self) -> None:
        request_type = self._candidate_publish_request_type()
        with tempfile.TemporaryDirectory() as raw_tmp:
            settings = _settings(Path(raw_tmp))
            request = request_type(
                job_kind="candidate_publish",
                run_id="published-child",
                parent_run_id="editable-draft",
                conversation_id="conversation-publish",
                settings=settings,
            )

            decoded = decode_request(BytesIO(encode_request(request)))

            self.assertEqual(decoded, request)
            result = {
                "job_kind": "candidate_publish",
                "run_id": request.run_id,
                "ok": True,
                "result": {
                    "run_id": request.run_id,
                    "artifact_type": "landing",
                    "source_path": str(
                        settings.out_dir
                        / "runs"
                        / request.run_id
                        / "final"
                        / "index.html"
                    ),
                    "lineage": {
                        "status": "published",
                        "source_run_id": "source-run",
                    },
                },
            }
            self.assertEqual(
                decode_worker_result(
                    result,
                    expected_run_id=request.run_id,
                    expected_job_kind=request.job_kind,
                ),
                result,
            )

    def test_candidate_publish_protocol_decodes_legacy_request_shape(self) -> None:
        request_type = self._candidate_publish_request_type()
        with tempfile.TemporaryDirectory() as raw_tmp:
            settings = _settings(Path(raw_tmp))
            request = request_type(
                job_kind="candidate_publish",
                run_id="legacy-published-child",
                parent_run_id="legacy-editable-draft",
                conversation_id="legacy-conversation",
                settings=settings,
            )

            decoded = decode_request(
                self._legacy_candidate_publish_envelope(request)
            )
            encoded = encode_request(request)
            (length,) = worker_protocol._LENGTH.unpack(
                encoded[:worker_protocol._LENGTH.size]
            )
            encoded_payload = json.loads(
                encoded[
                    worker_protocol._LENGTH.size:
                    worker_protocol._LENGTH.size + length
                ].decode("utf-8")
            )["request"]

        self.assertEqual(decoded.job_kind, "candidate_publish")
        self.assertEqual(decoded.run_id, request.run_id)
        self.assertEqual(decoded.parent_run_id, request.parent_run_id)
        self.assertIsNone(getattr(decoded, "source_attempt", None))
        self.assertIsNone(
            getattr(decoded, "expected_candidate_sha256", None)
        )
        self.assertNotIn("source_attempt", encoded_payload)
        self.assertNotIn("expected_candidate_sha256", encoded_payload)

    def test_candidate_publish_protocol_round_trips_direct_attempt_identity(
        self,
    ) -> None:
        request_type = self._candidate_publish_request_type()
        request_fields = {field.name for field in fields(request_type)}
        required = {"source_attempt", "expected_candidate_sha256"}
        self.assertTrue(
            required.issubset(request_fields),
            "direct candidate publication must carry attempt and immutable SHA-256 identity",
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            settings = _settings(Path(raw_tmp))
            request = request_type(
                job_kind="candidate_publish",
                run_id="direct-published-child",
                parent_run_id="failed-source",
                conversation_id="direct-conversation",
                settings=settings,
                source_attempt=2,
                expected_candidate_sha256="a" * 64,
            )

            decoded = decode_request(BytesIO(encode_request(request)))

        self.assertEqual(decoded, request)
        self.assertEqual(decoded.source_attempt, 2)
        self.assertEqual(decoded.expected_candidate_sha256, "a" * 64)

    def test_candidate_publish_protocol_rejects_half_direct_identity(self) -> None:
        request_type = self._candidate_publish_request_type()
        request_fields = {field.name for field in fields(request_type)}
        required = {"source_attempt", "expected_candidate_sha256"}
        self.assertTrue(
            required.issubset(request_fields),
            "direct candidate publication must carry attempt and immutable SHA-256 identity",
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            settings = _settings(Path(raw_tmp))
            cases = (
                {
                    "source_attempt": 1,
                    "expected_candidate_sha256": None,
                },
                {
                    "source_attempt": None,
                    "expected_candidate_sha256": "a" * 64,
                },
            )
            for values in cases:
                with self.subTest(values=values):
                    request = request_type(
                        job_kind="candidate_publish",
                        run_id="invalid-direct-child",
                        parent_run_id="failed-source",
                        conversation_id="direct-conversation",
                        settings=settings,
                        **values,
                    )
                    with self.assertRaisesRegex(
                        ProtocolError,
                        "source_attempt|expected_candidate_sha256|together",
                    ):
                        encode_request(request)

    def test_candidate_publish_protocol_rejects_invalid_direct_identity(self) -> None:
        request_type = self._candidate_publish_request_type()
        request_fields = {field.name for field in fields(request_type)}
        required = {"source_attempt", "expected_candidate_sha256"}
        self.assertTrue(
            required.issubset(request_fields),
            "direct candidate publication must carry attempt and immutable SHA-256 identity",
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            settings = _settings(Path(raw_tmp))
            cases = (
                (0, "a" * 64),
                (1, "A" * 64),
                (1, "g" * 64),
                (1, "a" * 63),
            )
            for attempt, digest in cases:
                with self.subTest(attempt=attempt, digest=digest):
                    request = request_type(
                        job_kind="candidate_publish",
                        run_id="invalid-direct-child",
                        parent_run_id="failed-source",
                        conversation_id="direct-conversation",
                        settings=settings,
                        source_attempt=attempt,
                        expected_candidate_sha256=digest,
                    )
                    with self.assertRaises(ProtocolError):
                        encode_request(request)

    def test_poster_code_edit_worker_threads_exact_cancellation_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            settings = _settings(root)
            runs_dir = settings.out_dir / "runs"
            parent_dir = runs_dir / "source"
            parent_dir.mkdir(parents=True)
            source_poster = parent_dir / "poster.html"
            source_poster.write_text("<main>poster</main>", encoding="utf-8")
            request = PosterCodeEditWorkerRequest(
                job_kind="poster_code_edit",
                run_id="edit",
                parent_run_id="source",
                source_poster=str(source_poster),
                artifact={"artifact_type": "poster"},
                instruction="tighten spacing",
                conversation_history=(),
                selection_context=None,
                palette_id=None,
                required_color_system={},
                conversation_id="conversation-edit",
                baseline_artifact_json="{}",
                settings=settings,
            )
            token = CancellationToken.never("edit")
            captured: dict[str, object] = {}

            def fake_edit(**kwargs: object) -> dict[str, object]:
                captured.update(kwargs)
                final_dir = runs_dir / "edit" / "final"
                final_dir.mkdir(parents=True)
                (final_dir / "poster.html").write_text("<main>final</main>", encoding="utf-8")
                (final_dir / "preview.png").write_bytes(b"preview")
                return {
                    "run_dir": str(runs_dir / "edit"),
                    "attempt_dir": str(runs_dir / "edit" / "code_editor" / "attempt_01"),
                    "attempts": [],
                    "validation_summary": {},
                    "promoted_assets": [],
                }

            with patch(
                "autodesign.poster_code_edit.run_poster_code_edit_sync",
                side_effect=fake_edit,
            ):
                result = _run_poster_code_edit(request, token)

            self.assertIs(captured["cancellation_token"], token)
            self.assertEqual(result["run_id"], "edit")
            self.assertEqual(result["selection_context_summary"], {})

    def test_poster_code_worker_owns_final_promotion_and_preview(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            settings = _settings(root)
            runs_dir = settings.out_dir / "runs"
            parent_dir = runs_dir / "source"
            parent_dir.mkdir(parents=True)
            source_poster = parent_dir / "poster.html"
            source_poster.write_text("<main>poster</main>", encoding="utf-8")
            request = PosterCodeEditWorkerRequest(
                job_kind="poster_code_edit",
                run_id="edit",
                parent_run_id="source",
                source_poster=str(source_poster),
                artifact={"artifact_type": "poster"},
                instruction="tighten spacing",
                conversation_history=(),
                selection_context=None,
                palette_id=None,
                required_color_system={},
                conversation_id="conversation-edit",
                baseline_artifact_json="{}",
                settings=settings,
            )
            token = CancellationToken.never("edit")
            final_poster = runs_dir / "edit" / "final" / "poster.html"
            final_preview = runs_dir / "edit" / "final" / "preview.png"

            def fake_edit(**kwargs: object) -> dict[str, object]:
                self.assertIs(kwargs["cancellation_token"], token)
                final_poster.parent.mkdir(parents=True, exist_ok=True)
                final_poster.write_text("<main>final</main>", encoding="utf-8")
                final_preview.write_bytes(b"preview")
                return {
                    "run_dir": str(runs_dir / "edit"),
                    "attempts": [],
                    "validation_summary": {},
                    "selection_context_summary": {"block_id": "results"},
                    "promoted_assets": [],
                }

            with patch(
                "autodesign.poster_code_edit.run_poster_code_edit_sync",
                side_effect=fake_edit,
            ) as edit:
                result = _run_poster_code_edit(request, token)

            edit.assert_called_once()
            self.assertEqual(result["poster_path"], str(final_poster))
            self.assertEqual(result["preview_path"], str(final_preview))
            self.assertEqual(
                result["selection_context_summary"],
                {"block_id": "results"},
            )

    def test_settings_json_round_trip_preserves_paths_headers_and_models(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            settings = replace(
                _settings(root),
                allow_private_network=False,
                allow_remote_image_urls=False,
            )
            request = PipelineWorkerRequest(
                job_kind="pipeline",
                run_id="round-trip",
                brief="make a poster",
                attachments=(str(root / "paper.pdf"),),
                template="cvpr-landscape",
                palette_id="academic",
                resume_run=None,
                reference_poster=str(root / "reference.png"),
                settings=settings,
            )

            decoded = decode_request(BytesIO(encode_request(request)))

            self.assertEqual(decoded, request)
            self.assertEqual(asdict(decoded.settings), asdict(settings))
            self.assertIsInstance(decoded.settings.repo_root, Path)
            self.assertEqual(decoded.settings.anthropic_custom_headers, settings.anthropic_custom_headers)
            self.assertEqual(decoded.settings.designer_model, "designer/model:v1")
            self.assertEqual(decoded.settings.code_editor_model, "editor/model")
            self.assertFalse(decoded.settings.allow_private_network)
            self.assertFalse(decoded.settings.allow_remote_image_urls)
            self.assertEqual(
                set(asdict(decoded.settings)),
                {field.name for field in fields(Settings)},
            )

    def test_truncated_worker_protocol_fails_before_pipeline_start(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_id = f"truncated-protocol-{uuid.uuid4().hex}"
            runs_dir = Path(raw_tmp) / "out" / "runs"
            request = PipelineWorkerRequest(
                job_kind="pipeline",
                run_id=run_id,
                brief="success",
                attachments=(),
                template=None,
                palette_id=None,
                resume_run=None,
                reference_poster=None,
                settings=_settings(Path(raw_tmp)),
            )
            complete = encode_request(request)
            declared = struct.unpack(">I", complete[:4])[0]
            completed = subprocess.run(
                [sys.executable, "-m", "autodesign.run_worker", "--run-id", run_id],
                input=struct.pack(">I", declared) + complete[4:17],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=Path(__file__).resolve().parents[1],
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse((runs_dir / run_id).exists())
            self.assertIn(b"truncated", completed.stderr.lower())

    def test_protocol_rejects_unknown_missing_and_oversized_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            request = PipelineWorkerRequest(
                job_kind="pipeline",
                run_id="strict",
                brief="success",
                attachments=(),
                template=None,
                palette_id=None,
                resume_run=None,
                reference_poster=None,
                settings=_settings(Path(raw_tmp)),
            )
            encoded = encode_request(request)
            declared = struct.unpack(">I", encoded[:4])[0]
            body = encoded[4:4 + declared]
            import json

            payload = json.loads(body)
            payload["request"]["unknown"] = True
            bad_body = json.dumps(payload).encode()
            with self.assertRaises(ProtocolError):
                decode_request(BytesIO(struct.pack(">I", len(bad_body)) + bad_body))

            payload = json.loads(body)
            del payload["request"]["brief"]
            bad_body = json.dumps(payload).encode()
            with self.assertRaises(ProtocolError):
                decode_request(BytesIO(struct.pack(">I", len(bad_body)) + bad_body))

            with self.assertRaises(ProtocolError):
                decode_request(BytesIO(struct.pack(">I", 100_000_000)))

    def test_all_worker_request_variants_round_trip_with_discriminator(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            settings = _settings(root)
            requests = (
                ArtifactEditWorkerRequest(
                    job_kind="artifact_edit",
                    run_id="artifact-edit",
                    parent_run_id="source",
                    input_path=str(root / "artifact_edit.json"),
                    conversation_id="conversation-artifact-edit",
                    settings=settings,
                ),
                EditableVideoRenderWorkerRequest(
                    job_kind="editable_video_render", run_id="video", parent_run_id="source",
                    artifact={"artifact_type": "video"}, conversation_id="conversation-video",
                    baseline_artifact_json="{}", settings=settings,
                ),
                PosterCodeEditWorkerRequest(
                    job_kind="poster_code_edit", run_id="edit", parent_run_id="source",
                    source_poster=str(root / "poster.html"), artifact={"artifact_type": "poster"},
                    instruction="tighten spacing", conversation_history=({"role": "user", "text": "edit"},),
                    selection_context=None, palette_id=None,
                    required_color_system={"primary": "#123456"},
                    conversation_id="conversation-edit", baseline_artifact_json="{}",
                    settings=settings,
                ),
                PptxExportWorkerRequest(
                    job_kind="pptx_export", run_id="pptx", parent_run_id="source",
                    source_html=str(root / "slides.html"), artifact={"artifact_type": "slides"},
                    artifact_name="Paper deck", conversation_id="conversation-pptx",
                    settings=settings,
                ),
                VideoExportRetryWorkerRequest(
                    job_kind="video_export_retry", run_id="retry", parent_run_id="source",
                    source_project=str(root / "hyperframes-demo"),
                    conversation_id="conversation-retry", baseline_artifact_json="{}",
                    runs_dir=str(root / "runs"),
                ),
                AttemptForkWorkerRequest(
                    job_kind="attempt_fork",
                    run_id="fork",
                    parent_run_id="source",
                    attempt=2,
                    expected_candidate_sha256="a" * 64,
                    conversation_id="conversation-fork",
                    settings=settings,
                ),
            )
            for request in requests:
                with self.subTest(job_kind=request.job_kind):
                    self.assertEqual(decode_request(BytesIO(encode_request(request))), request)

            for request in requests:
                with self.subTest(job_kind=request.job_kind, invalid="same-parent"):
                    with self.assertRaisesRegex(
                        ProtocolError,
                        "derived run_id must differ",
                    ):
                        encode_request(
                            replace(request, run_id=request.parent_run_id)
                        )

    def test_protocol_rejects_wrong_types_duplicates_versions_and_second_frames(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            request = PipelineWorkerRequest(
                job_kind="pipeline", run_id="strict-types", brief="success", attachments=(),
                template=None, palette_id=None, resume_run=None, reference_poster=None,
                settings=_settings(Path(raw_tmp)),
            )
            encoded = encode_request(request)
            import json
            payload = json.loads(encoded[4:])
            mutations = []
            wrong_bool = json.loads(encoded[4:])
            wrong_bool["request"]["settings"]["enable_skills"] = 1
            mutations.append(wrong_bool)
            wrong_literal = json.loads(encoded[4:])
            wrong_literal["request"]["settings"]["designer_provider"] = "not-a-provider"
            mutations.append(wrong_literal)
            wrong_number = json.loads(encoded[4:])
            wrong_number["request"]["settings"]["max_designer_turns"] = True
            mutations.append(wrong_number)
            unknown_setting = json.loads(encoded[4:])
            unknown_setting["request"]["settings"]["unknown_setting"] = "x"
            mutations.append(unknown_setting)
            missing_setting = json.loads(encoded[4:])
            del missing_setting["request"]["settings"]["critic_model"]
            mutations.append(missing_setting)
            bad_version = json.loads(encoded[4:])
            bad_version["version"] = 99
            mutations.append(bad_version)
            for mutation in mutations:
                body = json.dumps(mutation, allow_nan=False).encode()
                with self.assertRaises(ProtocolError):
                    decode_request(BytesIO(struct.pack(">I", len(body)) + body))

            duplicate = b'{"version":1,"version":1,"request":{}}'
            with self.assertRaises(ProtocolError):
                decode_request(BytesIO(struct.pack(">I", len(duplicate)) + duplicate))
            nonfinite = encoded[4:].replace(b'"llm_http_timeout":180.0', b'"llm_http_timeout":NaN')
            with self.assertRaises(ProtocolError):
                decode_request(BytesIO(struct.pack(">I", len(nonfinite)) + nonfinite))
            with self.assertRaises(ProtocolError):
                decode_request(BytesIO(encoded + encoded))

    def test_protocol_and_control_reject_cross_platform_unsafe_run_ids(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            settings = _settings(Path(raw_tmp))
            invalid = (".", "..", "../escape", "..\\escape", "/absolute", "C:\\escape", "\\\\server\\x", "bad\x00id")
            for run_id in invalid:
                with self.subTest(run_id=run_id):
                    request = PipelineWorkerRequest(
                        job_kind="pipeline", run_id=run_id, brief="success", attachments=(),
                        template=None, palette_id=None, resume_run=None, reference_poster=None,
                        settings=settings,
                    )
                    with self.assertRaises(ProtocolError):
                        encode_request(request)

    def test_worker_results_validate_all_discriminators_and_request_identity(self) -> None:
        results = {
            "pipeline": RunResult(
                run_id="result-run", run_dir="/tmp/result-run", artifact_type="poster",
                terminal_status="pass",
            ).model_dump(mode="json"),
            "artifact_edit": {
                "run_id": "result-run",
                "artifact_type": "landing",
                "source_path": "/tmp/index.html",
                "restored_layer_ids": ["title"],
                "skipped": [],
                "candidate_lineage": {},
            },
            "editable_video_render": {"run_id": "result-run", "mp4_path": "/tmp/video.mp4"},
            "poster_code_edit": {
                "run_id": "result-run", "run_dir": "/tmp/result-run",
                "attempt_dir": "/tmp/attempt", "poster_path": "/tmp/poster.html",
                "preview_path": "/tmp/preview.png", "attempts": [],
                "validation_summary": {}, "promoted_assets": [],
                "selection_context_summary": {"block_id": "results"},
            },
            "pptx_export": {"run_id": "result-run", "pptx_path": "/tmp/slides.pptx"},
            "video_export_retry": {
                "run_id": "result-run", "ok": True, "phase": "done",
                "project_dir": "/tmp/project", "manifest_path": "/tmp/manifest.json",
                "mp4_path": "/tmp/video.mp4", "media_probe_path": "/tmp/probe.json",
                "render_started_at": datetime.now(timezone.utc).isoformat(),
            },
            "attempt_fork": {
                "run_id": "result-run",
                "artifact_type": "poster",
                "source_path": "/tmp/poster.html",
                "lineage": {"source_attempt": 2},
            },
        }
        for kind, result in results.items():
            with self.subTest(kind=kind):
                payload = {"job_kind": kind, "run_id": "result-run", "ok": True, "result": result}
                self.assertEqual(
                    decode_worker_result(payload, expected_run_id="result-run", expected_job_kind=kind),
                    payload,
                )
                failure = {
                    "job_kind": kind,
                    "run_id": "result-run",
                    "ok": False,
                    "error": {
                        "type": "RuntimeError",
                        "message": "failed",
                        **(
                            {"pointer_cleanup_warnings": []}
                            if kind == "video_export_retry"
                            else {}
                        ),
                    },
                }
                self.assertEqual(
                    decode_worker_result(
                        failure, expected_run_id="result-run", expected_job_kind=kind,
                    ),
                    failure,
                )
                with self.assertRaises(ProtocolError):
                    decode_worker_result(payload, expected_run_id="other", expected_job_kind=kind)
                with self.assertRaises(ProtocolError):
                    decode_worker_result(payload, expected_run_id="result-run", expected_job_kind="pipeline" if kind != "pipeline" else "pptx_export")
                with self.assertRaises(ProtocolError):
                    decode_worker_result(
                        {"job_kind": kind, "run_id": "result-run", "ok": True, "result": {}},
                        expected_run_id="result-run", expected_job_kind=kind,
                    )
                extra = dict(result)
                extra["undeclared"] = True
                with self.assertRaises(ProtocolError):
                    decode_worker_result(
                        {"job_kind": kind, "run_id": "result-run", "ok": True, "result": extra},
                        expected_run_id="result-run", expected_job_kind=kind,
                    )
                removable = next(name for name in result if name != "run_id")
                missing = dict(result)
                del missing[removable]
                with self.assertRaises(ProtocolError):
                    decode_worker_result(
                        {"job_kind": kind, "run_id": "result-run", "ok": True, "result": missing},
                        expected_run_id="result-run", expected_job_kind=kind,
                    )

    def test_video_export_retry_accepts_production_iso_render_timestamp(self) -> None:
        result = {
            "run_id": "result-run",
            "ok": True,
            "phase": "done",
            "project_dir": "/tmp/project",
            "manifest_path": "/tmp/manifest.json",
            "mp4_path": "/tmp/video.mp4",
            "media_probe_path": "/tmp/probe.json",
            "render_started_at": datetime.now(timezone.utc).isoformat(),
        }
        payload = {
            "job_kind": "video_export_retry",
            "run_id": "result-run",
            "ok": True,
            "result": result,
        }

        self.assertEqual(
            decode_worker_result(
                payload,
                expected_run_id="result-run",
                expected_job_kind="video_export_retry",
            ),
            payload,
        )
        for invalid_timestamp in ("", "not-a-timestamp", "2026-08-03T12:00:00", 123.5):
            with self.subTest(invalid_timestamp=invalid_timestamp):
                invalid_result = dict(result)
                invalid_result["render_started_at"] = invalid_timestamp
                with self.assertRaises(ProtocolError):
                    decode_worker_result(
                        {**payload, "result": invalid_result},
                        expected_run_id="result-run",
                        expected_job_kind="video_export_retry",
                    )

    def test_video_retry_success_cleanup_warnings_are_strict_with_one_legacy_shape(
        self,
    ) -> None:
        legacy_result = {
            "run_id": "result-run",
            "ok": True,
            "phase": "done",
            "project_dir": "/tmp/project",
            "manifest_path": "/tmp/manifest.json",
            "mp4_path": "/tmp/video.mp4",
            "media_probe_path": "/tmp/probe.json",
            "render_started_at": "2026-08-05T12:00:00+00:00",
        }

        def decode(result: dict[str, object]) -> tuple[dict[str, object] | None, str | None]:
            envelope = {
                "job_kind": "video_export_retry",
                "run_id": "result-run",
                "ok": True,
                "result": result,
            }
            try:
                decoded = decode_worker_result(
                    envelope,
                    expected_run_id="result-run",
                    expected_job_kind="video_export_retry",
                )
            except ProtocolError as exc:
                return None, str(exc)
            return decoded["result"], None

        observed: dict[str, object] = {}
        for label, warnings in (
            ("empty", []),
            ("populated", ["recovery close warning", "directory sync warning"]),
        ):
            decoded, error = decode(
                {**legacy_result, "pointer_cleanup_warnings": warnings}
            )
            observed[label] = {
                "accepted": error is None,
                "warnings": (
                    decoded.get("pointer_cleanup_warnings")
                    if decoded is not None
                    else None
                ),
            }

        decoded_legacy, legacy_error = decode(dict(legacy_result))
        observed["legacy"] = {
            "accepted": legacy_error is None,
            "warnings": (
                decoded_legacy.get("pointer_cleanup_warnings")
                if decoded_legacy is not None
                else None
            ),
            "exact_fields": (
                set(decoded_legacy or {})
                == set(legacy_result) | {"pointer_cleanup_warnings"}
            ),
        }

        malformed = {
            "tuple": {**legacy_result, "pointer_cleanup_warnings": ("warning",)},
            "non_string": {**legacy_result, "pointer_cleanup_warnings": ["warning", 7]},
            "extra": {
                **legacy_result,
                "pointer_cleanup_warnings": [],
                "undeclared": True,
            },
            "missing_required": {
                key: value
                for key, value in {
                    **legacy_result,
                    "pointer_cleanup_warnings": [],
                }.items()
                if key != "mp4_path"
            },
        }
        observed["malformed_rejected"] = {
            label: decode(candidate)[1] is not None
            for label, candidate in malformed.items()
        }

        self.assertEqual(
            observed,
            {
                "empty": {"accepted": True, "warnings": []},
                "populated": {
                    "accepted": True,
                    "warnings": [
                        "recovery close warning",
                        "directory sync warning",
                    ],
                },
                "legacy": {
                    "accepted": True,
                    "warnings": [],
                    "exact_fields": True,
                },
                "malformed_rejected": {
                    "tuple": True,
                    "non_string": True,
                    "extra": True,
                    "missing_required": True,
                },
            },
        )

    def test_video_retry_failure_adapts_exact_pre_upgrade_error_shapes(
        self,
    ) -> None:
        for label, legacy_error in (
            (
                "without_traceback",
                {"type": "RuntimeError", "message": "legacy retry failed"},
            ),
            (
                "with_traceback",
                {
                    "type": "RuntimeError",
                    "message": "legacy retry failed",
                    "traceback": "legacy traceback",
                },
            ),
        ):
            with self.subTest(label=label):
                payload = {
                    "job_kind": "video_export_retry",
                    "run_id": "result-run",
                    "ok": False,
                    "error": dict(legacy_error),
                }

                decoded = decode_worker_result(
                    payload,
                    expected_run_id="result-run",
                    expected_job_kind="video_export_retry",
                )

                self.assertEqual(
                    decoded["error"],
                    {**legacy_error, "pointer_cleanup_warnings": []},
                )

    def test_video_retry_failure_warning_contract_remains_strict(
        self,
    ) -> None:
        video_error = {
            "type": "RuntimeError",
            "message": "retry failed",
            "phase": "render",
            "pointer_cleanup_warnings": [],
        }
        accepted = {
            "job_kind": "video_export_retry",
            "run_id": "result-run",
            "ok": False,
            "error": video_error,
        }
        self.assertEqual(
            decode_worker_result(
                accepted,
                expected_run_id="result-run",
                expected_job_kind="video_export_retry",
            ),
            accepted,
        )

        rejected_errors = {
            "phase_without_warnings": {
                "type": "RuntimeError",
                "message": "retry failed",
                "phase": "render",
            },
            "traceback_and_phase_without_warnings": {
                "type": "RuntimeError",
                "message": "retry failed",
                "traceback": "traceback",
                "phase": "render",
            },
            "unknown_extra_field": {
                "type": "RuntimeError",
                "message": "retry failed",
                "pointer_cleanup_warnings": [],
                "unknown": "field",
            },
            "missing_type": {
                "message": "retry failed",
                "pointer_cleanup_warnings": [],
            },
            "missing_message": {
                "type": "RuntimeError",
                "pointer_cleanup_warnings": [],
            },
            "wrong_type_type": {
                "type": 7,
                "message": "retry failed",
            },
            "wrong_message_type": {
                "type": "RuntimeError",
                "message": 7,
            },
            "wrong_traceback_type": {
                "type": "RuntimeError",
                "message": "retry failed",
                "traceback": 7,
            },
            "wrong_phase_type": {
                "type": "RuntimeError",
                "message": "retry failed",
                "phase": 7,
                "pointer_cleanup_warnings": [],
            },
            "warnings_not_list": {
                "type": "RuntimeError",
                "message": "retry failed",
                "pointer_cleanup_warnings": "warning",
            },
            "warnings_with_non_string": {
                "type": "RuntimeError",
                "message": "retry failed",
                "pointer_cleanup_warnings": ["warning", 7],
            },
        }
        for label, error in rejected_errors.items():
            with self.subTest(label=label), self.assertRaises(ProtocolError):
                decode_worker_result(
                    {
                        "job_kind": "video_export_retry",
                        "run_id": "result-run",
                        "ok": False,
                        "error": error,
                    },
                    expected_run_id="result-run",
                    expected_job_kind="video_export_retry",
                )

        non_video_legacy = {
            "job_kind": "pptx_export",
            "run_id": "result-run",
            "ok": False,
            "error": {"type": "RuntimeError", "message": "export failed"},
        }
        self.assertEqual(
            decode_worker_result(
                non_video_legacy,
                expected_run_id="result-run",
                expected_job_kind="pptx_export",
            ),
            non_video_legacy,
        )
        self.assertNotIn(
            "pointer_cleanup_warnings",
            non_video_legacy["error"],
        )

    def test_non_video_failure_rejects_video_diagnostics(self) -> None:
        for field_name, field_value in (
            ("phase", "final_pointer"),
            ("pointer_cleanup_warnings", []),
        ):
            with self.subTest(field_name=field_name):
                payload = {
                    "job_kind": "pptx_export",
                    "run_id": "result-run",
                    "ok": False,
                    "error": {
                        "type": "RuntimeError",
                        "message": "export failed",
                        field_name: field_value,
                    },
                }
                with self.assertRaises(ProtocolError):
                    decode_worker_result(
                        payload,
                        expected_run_id="result-run",
                        expected_job_kind="pptx_export",
                    )

    def test_worker_error_formatter_keeps_short_primary_with_oversized_warning(
        self,
    ) -> None:
        primary = "primary retry failure"
        warnings = ["warning-first-" + ("w" * 4000)]
        expected_warnings = list(warnings)

        formatted = worker_protocol.format_worker_error_message(primary, warnings)

        self.assertLessEqual(
            len(formatted),
            worker_protocol.WORKER_ERROR_MESSAGE_LIMIT,
        )
        self.assertTrue(formatted.startswith(primary), formatted[:120])
        self.assertIn("\nPointer cleanup warnings: warning-first-", formatted)
        self.assertEqual(warnings, expected_warnings)

    def test_worker_error_formatter_keeps_long_primary_and_stable_warning_prefix(
        self,
    ) -> None:
        primary = "primary-first-" + ("p" * 3500)
        warnings = [
            "warning-one-" + ("a" * 1600),
            "warning-two-" + ("b" * 1600),
        ]
        expected_warnings = list(warnings)

        formatted = worker_protocol.format_worker_error_message(primary, warnings)
        marker = "\nPointer cleanup warnings: "
        suffix = formatted[formatted.index(marker) + len(marker):]

        self.assertLessEqual(
            len(formatted),
            worker_protocol.WORKER_ERROR_MESSAGE_LIMIT,
        )
        self.assertTrue(formatted.startswith("primary-first-"), formatted[:120])
        self.assertTrue(suffix.startswith("warning-one-"), suffix[:120])
        if "warning-two-" in suffix:
            self.assertLess(suffix.index("warning-one-"), suffix.index("warning-two-"))
        self.assertEqual(warnings, expected_warnings)

        envelope = {
            "job_kind": "video_export_retry",
            "run_id": "result-run",
            "ok": False,
            "error": {
                "type": "RuntimeError",
                "message": formatted,
                "pointer_cleanup_warnings": warnings,
            },
        }
        decoded = decode_worker_result(
            envelope,
            expected_run_id="result-run",
            expected_job_kind="video_export_retry",
        )
        self.assertEqual(decoded["error"]["pointer_cleanup_warnings"], expected_warnings)

    def test_worker_error_formatter_honors_exact_boundaries(self) -> None:
        marker = "\nPointer cleanup warnings: "
        warnings = ["boundary-warning"]
        suffix = marker + warnings[0]
        limit = worker_protocol.WORKER_ERROR_MESSAGE_LIMIT
        exact_primary = "p" * (limit - len(suffix))

        exact = worker_protocol.format_worker_error_message(
            exact_primary,
            warnings,
        )
        overflow = worker_protocol.format_worker_error_message(
            exact_primary + "overflow",
            warnings,
        )
        no_warnings = worker_protocol.format_worker_error_message(
            "n" * (limit + 1),
            [],
        )

        self.assertEqual(exact, exact_primary + suffix)
        self.assertEqual(len(exact), limit)
        self.assertEqual(len(overflow), limit)
        self.assertTrue(overflow.startswith(exact_primary))
        self.assertTrue(overflow.endswith(suffix))
        self.assertEqual(no_warnings, "n" * limit)

    def test_worker_error_formatter_is_idempotent_with_truncated_suffix(
        self,
    ) -> None:
        marker = "\nPointer cleanup warnings: "
        primary = "primary remains visible"
        warnings = ["oversized-warning-" + ("z" * 5000)]
        canonical_suffix = marker + warnings[0]

        first = worker_protocol.format_worker_error_message(primary, warnings)
        second = worker_protocol.format_worker_error_message(first, warnings)
        third = worker_protocol.format_worker_error_message(second, warnings)
        suffix_tail = first[first.index(marker):]

        self.assertTrue(first.startswith(primary), first[:120])
        self.assertNotEqual(suffix_tail, canonical_suffix)
        self.assertTrue(canonical_suffix.startswith(suffix_tail))
        self.assertEqual(second, first)
        self.assertEqual(third, first)
        self.assertEqual(first.count(marker), 1)
        self.assertLessEqual(
            len(first),
            worker_protocol.WORKER_ERROR_MESSAGE_LIMIT,
        )

    def test_worker_error_formatter_does_not_strip_plain_marker_from_primary(
        self,
    ) -> None:
        marker = "\nPointer cleanup warnings: "
        primary = (
            "primary failure"
            + marker
            + "user-authored diagnostic text, not the canonical system suffix"
        )
        warnings = ["real cleanup warning"]

        first = worker_protocol.format_worker_error_message(primary, warnings)
        second = worker_protocol.format_worker_error_message(first, warnings)

        self.assertTrue(first.startswith(primary))
        self.assertEqual(second, first)
        self.assertEqual(first.count(marker), 2)

    def test_worker_result_reader_returns_validated_warning_tuple(self) -> None:
        warnings = ("first decoded warning", "second decoded warning")
        success_result = {
            "run_id": "result-run",
            "ok": True,
            "phase": "done",
            "project_dir": "/tmp/project",
            "manifest_path": "/tmp/manifest.json",
            "mp4_path": "/tmp/video.mp4",
            "media_probe_path": "/tmp/probe.json",
            "render_started_at": "2026-08-05T12:00:00+00:00",
            "pointer_cleanup_warnings": list(warnings),
        }
        cases = (
            (
                "success",
                {
                    "job_kind": "video_export_retry",
                    "run_id": "result-run",
                    "ok": True,
                    "result": success_result,
                },
                {
                    "result": success_result,
                    "error": None,
                    "phase": None,
                    "warnings_type": tuple,
                    "warnings": warnings,
                },
            ),
            (
                "failure",
                {
                    "job_kind": "video_export_retry",
                    "run_id": "result-run",
                    "ok": False,
                    "error": {
                        "type": "VideoExportRetryError",
                        "message": "retry failed",
                        "phase": "final_pointer",
                        "pointer_cleanup_warnings": list(warnings),
                    },
                },
                {
                    "result": None,
                    "error": (
                        "retry failed\nPointer cleanup warnings: "
                        "first decoded warning | second decoded warning"
                    ),
                    "phase": "final_pointer",
                    "warnings_type": tuple,
                    "warnings": warnings,
                },
            ),
        )

        with tempfile.TemporaryDirectory() as raw_tmp:
            result_path = Path(raw_tmp) / "worker_result.json"
            for label, envelope, expected in cases:
                with self.subTest(label=label):
                    result_path.write_text(
                        json.dumps(envelope),
                        encoding="utf-8",
                    )
                    decoded = _read_worker_result(
                        result_path,
                        expected_run_id="result-run",
                        expected_job_kind="video_export_retry",
                    )
                    self.assertEqual(len(decoded), 4)
                    result, error, phase, decoded_warnings = decoded
                    self.assertEqual(
                        {
                            "result": result,
                            "error": error,
                            "phase": phase,
                            "warnings_type": type(decoded_warnings),
                            "warnings": decoded_warnings,
                        },
                        expected,
                    )

    def test_worker_result_reader_rejects_duplicate_video_retry_warning_key(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            result_path = Path(raw_tmp) / "worker_result.json"
            result_path.write_text(
                """
{
  "job_kind": "video_export_retry",
  "run_id": "result-run",
  "ok": true,
  "result": {
    "run_id": "result-run",
    "ok": true,
    "phase": "done",
    "project_dir": "/tmp/project",
    "manifest_path": "/tmp/manifest.json",
    "mp4_path": "/tmp/video.mp4",
    "media_probe_path": "/tmp/probe.json",
    "render_started_at": "2026-08-05T12:00:00+00:00",
    "pointer_cleanup_warnings": [],
    "pointer_cleanup_warnings": ["shadowed duplicate warning"]
  }
}
""".strip(),
                encoding="utf-8",
            )

            decoded = _read_worker_result(
                result_path,
                expected_run_id="result-run",
                expected_job_kind="video_export_retry",
            )
            self.assertEqual(len(decoded), 4)
            result, error, phase, warnings = decoded

        normalized_error = str(error or "").lower()
        self.assertEqual(
            {
                "result": result,
                "phase": phase,
                "warnings_type": type(warnings),
                "warnings": warnings,
                "duplicate_rejected": (
                    "duplicate" in normalized_error
                    and "pointer_cleanup_warnings" in normalized_error
                ),
            },
            {
                "result": None,
                "phase": None,
                "warnings_type": tuple,
                "warnings": (),
                "duplicate_rejected": True,
            },
        )

    def test_protocol_rejects_windows_alias_and_ads_run_ids_cross_platform(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            settings = _settings(Path(raw_tmp))
            for run_id in ("CON", "con.txt", "NUL", "COM1", "LPT9", "run.", "run ", "run:stream"):
                with self.subTest(run_id=run_id):
                    request = PipelineWorkerRequest(
                        job_kind="pipeline", run_id=run_id, brief="success", attachments=(),
                        template=None, palette_id=None, resume_run=None, reference_poster=None,
                        settings=settings,
                    )
                    with self.assertRaises(ProtocolError):
                        encode_request(request)


if __name__ == "__main__":
    unittest.main()
