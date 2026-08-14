from __future__ import annotations

import ast
import asyncio
from contextlib import ExitStack, contextmanager
from io import BytesIO
import importlib
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import autodesign.run_worker as run_worker_module
from autodesign.attempt_candidates import VideoDeliveryPointerUpdate
from autodesign.config import Settings
from autodesign.process_supervision import process_identity, process_is_alive
from autodesign.run_control import CancellationToken, RunCancelled, RunControlStore
from autodesign.schema import (
    VideoDeliveryContract,
    VideoMediaProbe,
    VideoSceneContract,
)
from autodesign.run_supervisor import RunSupervisor
from autodesign.run_worker_protocol import (
    EditableVideoRenderWorkerRequest,
    PptxExportWorkerRequest,
    VideoExportRetryWorkerRequest,
    encode_request,
)
from autodesign.util.design_spec_fingerprint import design_spec_sha256


export_video_module = importlib.import_module("autodesign.tools.export_video")


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "derived_worker_process.py"
DETACHED_HYPERFRAMES_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "fake_hyperframes_detached_writer.py"
)


async def _wait_for(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.015)
    raise AssertionError("condition was not met before polling deadline")


def _read_recorded_pids(path: Path) -> list[int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [int(pid) for pid in payload["pids"]]


def _directory_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class _RaiseExactCancellationAtPhase:
    def __init__(self, target_phase: str, cancellation: RunCancelled) -> None:
        self.target_phase = target_phase
        self.cancellation = cancellation
        self.calls: list[str] = []

    def raise_if_cancelled(self, phase: str) -> None:
        self.calls.append(phase)
        if phase == self.target_phase:
            raise self.cancellation

    def is_cancelled(self) -> bool:
        return False


def _make_retry_project(run_dir: Path) -> Path:
    scenes = [
        VideoSceneContract(
            scene_id=f"scene_{index:02d}",
            title=f"Scene {index}",
            start_s=(index - 1) * 36,
            duration_s=36,
            narration_text=(
                "This scene explains grounded paper evidence for the conference audience."
            ),
        )
        for index in range(1, 11)
    ]
    project_dir = run_dir / "hyperframes-paper-video"
    (project_dir / "assets").mkdir(parents=True)
    (project_dir / "renders").mkdir()
    (project_dir / "narration").mkdir()
    (project_dir / "index.html").write_text(
        "<!doctype html><html><body><main id=\"root\"></main></body></html>",
        encoding="utf-8",
    )
    contract = VideoDeliveryContract(scenes=scenes)
    (project_dir / "video_delivery_contract.json").write_text(
        json.dumps(contract.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    (project_dir / "meta.json").write_text(
        json.dumps({"id": "paper-video"}) + "\n",
        encoding="utf-8",
    )
    spec = {"artifact_type": "video", "name": "Paper video"}
    (run_dir / "design_spec.json").write_text(
        json.dumps(
            {
                "design_spec": spec,
                "design_spec_sha256": design_spec_sha256(spec),
                "revision": 1,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return project_dir


@contextmanager
def _patched_successful_retry_path(
    *,
    invalidation_warning: str,
    publication_warning: str,
    nested_cancellation: tuple[str, RunCancelled] | None = None,
):
    base_probe = VideoMediaProbe(
        video_codec="h264",
        pixel_format="yuv420p",
        audio_codec="aac",
        width=1920,
        height=1080,
        fps=30,
        duration_s=360,
    )
    captioned_probe = base_probe.model_copy(
        update={"subtitle_codec": "mov_text", "subtitle_forced": False}
    )

    def update_pointer(
        _run_dir: Path,
        *,
        mode: str,
        **_kwargs: object,
    ) -> VideoDeliveryPointerUpdate:
        if mode == "invalidate_if_present":
            return VideoDeliveryPointerUpdate(
                status="absent",
                pointer_snapshot=None,
                payload=None,
                cleanup_warnings=(invalidation_warning, invalidation_warning),
            )
        return VideoDeliveryPointerUpdate(
            status="published",
            pointer_snapshot=None,
            payload={},
            cleanup_warnings=(
                invalidation_warning,
                publication_warning,
                publication_warning,
            ),
        )

    def write_narration_artifacts(
        project_dir: Path,
        _scene_manifest: list[dict[str, object]],
        _voice_preset: str,
        *,
        speech_timing: object = None,
    ) -> dict[str, object]:
        narration_dir = project_dir / "narration"
        narration_dir.mkdir(parents=True, exist_ok=True)
        files = {
            "transcript_path": narration_dir / "transcript.en.txt",
            "srt_path": narration_dir / "subtitles.en.srt",
            "vtt_path": narration_dir / "subtitles.en.vtt",
            "voice_metadata_path": narration_dir / "voice.json",
            "narration_timing_path": narration_dir / "timing.json",
        }
        for name, path in files.items():
            path.write_text(
                "{}\n" if name == "voice_metadata_path" else "test\n",
                encoding="utf-8",
            )
        return {
            name: path.relative_to(project_dir).as_posix()
            for name, path in files.items()
        } | {
            "subtitle_diagnostics": [],
            "subtitle_soft_limit_exceeded": False,
        }

    def synthesize(
        project_dir: Path,
        *,
        scene_manifest: list[dict[str, object]],
        **_kwargs: object,
    ) -> tuple[str, bool, Path, list[dict[str, object]]]:
        audio_path = project_dir / "assets" / "narration.wav"
        audio_path.write_bytes(b"narration")
        timings = [
            {
                "scene_id": scene["scene_id"],
                "start_s": scene["start_s"],
                "speech_duration_s": 30.0,
                "end_s": float(scene["start_s"]) + 30.0,
                "speed": 1.0,
            }
            for scene in scene_manifest
        ]
        return "tts ok", True, audio_path, timings

    def render(
        project_dir: Path,
        *_args: object,
        **_kwargs: object,
    ) -> tuple[str, bool, Path, VideoMediaProbe]:
        raw_mp4 = project_dir / "renders" / "retry.mp4"
        raw_mp4.write_bytes(b"rendered mp4")
        return "render ok", True, raw_mp4, base_probe

    def caption(
        raw_mp4: Path,
        _subtitle_path: Path,
        **_kwargs: object,
    ) -> tuple[str, bool, Path, VideoMediaProbe]:
        captioned = raw_mp4.with_name("retry-captioned.mp4")
        captioned.write_bytes(b"captioned mp4")
        return "caption ok", True, captioned, captioned_probe

    nested_role = nested_cancellation[0] if nested_cancellation else None
    nested_error = nested_cancellation[1] if nested_cancellation else None
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(
                export_video_module,
                "validate_authored_video_html",
                return_value=[],
            )
        )
        stack.enter_context(
            mock.patch.object(
                export_video_module,
                "update_video_delivery_pointer",
                side_effect=update_pointer,
            )
        )
        stack.enter_context(
            mock.patch.object(
                export_video_module,
                "_write_narration_artifacts",
                side_effect=write_narration_artifacts,
            )
        )
        stack.enter_context(
            mock.patch.object(
                export_video_module,
                "_run_hyperframes_authoring_lint",
                side_effect=(
                    nested_error
                    if nested_role == "authoring"
                    else None
                ),
                return_value=("authoring lint ok", True),
            )
        )
        stack.enter_context(
            mock.patch.object(
                export_video_module,
                "_synthesize_timed_narration",
                side_effect=synthesize,
            )
        )
        stack.enter_context(
            mock.patch.object(
                export_video_module,
                "_speech_delivery_metrics",
                return_value=(
                    {
                        "spoken_word_count": 720,
                        "spoken_wpm": 120.0,
                        "speech_coverage_ratio": 0.8,
                    },
                    None,
                ),
            )
        )
        stack.enter_context(
            mock.patch.object(
                export_video_module,
                "_run_hyperframes_lint",
                return_value=("lint ok", True),
            )
        )
        stack.enter_context(
            mock.patch.object(
                export_video_module,
                "_run_hyperframes_render",
                side_effect=(nested_error if nested_role == "render" else render),
            )
        )
        stack.enter_context(
            mock.patch.object(
                export_video_module,
                "_prepare_captioned_delivery_mp4",
                side_effect=caption,
            )
        )
        stack.enter_context(
            mock.patch.object(
                export_video_module,
                "authored_video_local_asset_paths",
                return_value={},
            )
        )
        yield


class DerivedWorkerCancellationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out_dir = self.root / "out"
        self.runs_dir = self.out_dir / "runs"
        self.settings = Settings(
            anthropic_api_key="",
            anthropic_base_url=None,
            gemini_api_key="",
            designer_model="designer",
            critic_model="critic",
            repo_root=REPO_ROOT,
            out_dir=self.out_dir,
        )
        self.store = RunControlStore(self.runs_dir)
        self.supervisors: list[RunSupervisor] = []

    async def asyncTearDown(self) -> None:
        for supervisor in self.supervisors:
            for run_id in list(supervisor.active_run_ids()):
                try:
                    await supervisor.cancel(run_id, "test_cleanup")
                except BaseException:
                    pass
        self._tmp.cleanup()

    def _queue(self, run_id: str, artifact_type: str) -> None:
        reserved = self.store.reserve(run_id, artifact_type)
        self.store.transition(run_id, reserved, "queued")

    def _supervisor(self) -> RunSupervisor:
        supervisor = RunSupervisor(
            self.runs_dir,
            control_store=self.store,
            worker_command=(sys.executable, str(FIXTURE)),
            grace_s=0.1,
        )
        self.supervisors.append(supervisor)
        return supervisor

    def _worker_cancellation_payload(
        self,
        request: VideoExportRetryWorkerRequest | EditableVideoRenderWorkerRequest,
        cancellation: RunCancelled,
    ) -> tuple[int, dict[str, object]]:
        stdin = SimpleNamespace(buffer=BytesIO(encode_request(request)))
        with (
            mock.patch.object(
                run_worker_module,
                "_dispatch",
                side_effect=cancellation,
            ),
            mock.patch.object(
                sys,
                "argv",
                ["run_worker", "--run-id", request.run_id],
            ),
            mock.patch.object(sys, "stdin", stdin),
            mock.patch.object(run_worker_module.signal, "signal"),
            mock.patch.object(
                run_worker_module.os,
                "getsid",
                return_value=os.getpid(),
            ),
        ):
            exit_code = run_worker_module.worker_main()
        payload = json.loads(
            (self.runs_dir / request.run_id / "worker_result.json").read_text(
                encoding="utf-8"
            )
        )
        return exit_code, payload

    async def test_editable_video_cancel_kills_writer_and_freezes_output(self) -> None:
        run_id = "editable-video-cancel"
        self._queue(run_id, "video")
        request = EditableVideoRenderWorkerRequest(
            job_kind="editable_video_render",
            run_id=run_id,
            parent_run_id="source-video",
            artifact={},
            conversation_id="conversation",
            baseline_artifact_json="{}",
            settings=self.settings,
        )
        supervisor = self._supervisor()
        supervised = await supervisor.start(request)
        run_dir = self.runs_dir / run_id
        pid_path = run_dir / "recorded_pids.json"
        sentinel = run_dir / "editable-video" / "render-heartbeat.txt"
        await _wait_for(
            lambda: pid_path.is_file()
            and sentinel.is_file()
            and sentinel.stat().st_size >= 15
        )
        identities = [
            process_identity(supervised.process.pid),
            *(process_identity(pid) for pid in _read_recorded_pids(pid_path)),
        ]

        outcome = await supervisor.cancel(run_id, "user_requested")
        frozen = sentinel.read_bytes()
        await asyncio.sleep(0.2)

        self.assertEqual(outcome.state, "cancelled")
        self.assertEqual(outcome.surviving_pids, ())
        self.assertEqual(sentinel.read_bytes(), frozen)
        for identity in identities:
            self.assertFalse(process_is_alive(identity), identity)

    async def test_pptx_cancel_kills_detached_tree_without_mutating_source_run(self) -> None:
        parent_run_id = "pptx-source"
        parent_dir = self.runs_dir / parent_run_id
        parent_dir.mkdir(parents=True)
        source = parent_dir / "poster.html"
        source.write_text("<html><body>source poster</body></html>\n", encoding="utf-8")
        before = _directory_snapshot(parent_dir)

        run_id = "pptx-cancel"
        self._queue(run_id, "pptx")
        request = PptxExportWorkerRequest(
            job_kind="pptx_export",
            run_id=run_id,
            parent_run_id=parent_run_id,
            source_html=str(source),
            artifact={"name": "Poster"},
            artifact_name="Poster",
            conversation_id="conversation",
            settings=self.settings,
        )
        supervisor = self._supervisor()
        supervised = await supervisor.start(request)
        run_dir = self.runs_dir / run_id
        pid_path = run_dir / "recorded_pids.json"
        await _wait_for(pid_path.is_file)
        identities = [
            process_identity(supervised.process.pid),
            *(process_identity(pid) for pid in _read_recorded_pids(pid_path)),
        ]

        outcome = await supervisor.cancel(run_id, "user_requested")
        await asyncio.sleep(0.1)

        self.assertEqual(outcome.state, "cancelled")
        self.assertEqual(outcome.surviving_pids, ())
        self.assertEqual(_directory_snapshot(parent_dir), before)
        self.assertFalse((run_dir / "exports" / "Poster.pptx").exists())
        for identity in identities:
            self.assertFalse(process_is_alive(identity), identity)
            if os.name == "posix":
                with self.assertRaises(ProcessLookupError):
                    os.kill(identity.pid, 0)

    async def test_retry_video_cancel_stops_current_stage_and_never_promotes(self) -> None:
        parent_run_id = "video-source"
        source_project = self.runs_dir / parent_run_id / "hyperframes-source"
        source_project.mkdir(parents=True)
        (source_project / "index.html").write_text("<html></html>\n", encoding="utf-8")

        run_id = "video-retry-cancel"
        self._queue(run_id, "video")
        request = VideoExportRetryWorkerRequest(
            job_kind="video_export_retry",
            run_id=run_id,
            parent_run_id=parent_run_id,
            source_project=str(source_project),
            conversation_id="conversation",
            baseline_artifact_json="{}",
            runs_dir=str(self.runs_dir),
        )
        supervisor = self._supervisor()
        supervised = await supervisor.start(request)
        run_dir = self.runs_dir / run_id
        pid_path = run_dir / "recorded_pids.json"
        sentinel = run_dir / "video-retry" / "stage-one-heartbeat.txt"
        await _wait_for(
            lambda: pid_path.is_file()
            and (run_dir / "video-retry" / "stage-one-started").is_file()
            and sentinel.is_file()
            and sentinel.stat().st_size >= 15
        )
        identities = [
            process_identity(supervised.process.pid),
            *(process_identity(pid) for pid in _read_recorded_pids(pid_path)),
        ]

        outcome = await supervisor.cancel(run_id, "user_requested")
        frozen = sentinel.read_bytes()
        await asyncio.sleep(0.2)

        self.assertEqual(outcome.state, "cancelled")
        self.assertEqual(outcome.surviving_pids, ())
        self.assertEqual(sentinel.read_bytes(), frozen)
        self.assertFalse((run_dir / "video-retry" / "stage-two-started").exists())
        self.assertFalse((run_dir / "final" / "video_delivery.json").exists())
        for identity in identities:
            self.assertFalse(process_is_alive(identity), identity)

    @unittest.skipUnless(os.name == "posix", "detached-process probe requires fork")
    async def test_real_video_retry_cancel_kills_detached_hyperframes_writer(self) -> None:
        from autodesign.schema import VideoDeliveryContract, VideoSceneContract
        from autodesign.util.design_spec_fingerprint import design_spec_sha256

        parent_run_id = "video-production-source"
        source_project = self.runs_dir / parent_run_id / "hyperframes-paper-video"
        source_project.mkdir(parents=True)
        scenes = [
            VideoSceneContract(
                scene_id=f"scene_{index:02d}",
                title=f"Scene {index}",
                start_s=(index - 1) * 30,
                duration_s=30,
                narration_text=(
                    f"Scene {index}. This narration explains the paper method and "
                    "evidence for a conference audience using only grounded claims."
                ),
            )
            for index in range(1, 13)
        ]
        scene_html = "\n".join(
            f'<section id="{scene.scene_id}" class="clip" '
            f'data-start="{scene.start_s:g}" data-duration="{scene.duration_s:g}" '
            f'data-track-index="{index}" '
            f'data-narration="{scene.narration_text}"></section>'
            for index, scene in enumerate(scenes, start=1)
        )
        (source_project / "index.html").write_text(
            "<!doctype html><html lang=\"en\"><body>"
            '<div id="root" data-composition-id="main" data-start="0" '
            'data-no-timeline data-duration="360" data-width="1920" data-height="1080">'
            f"{scene_html}"
            '<audio id="narration-audio" class="clip" src="assets/narration.wav" '
            'data-start="0" data-duration="360" data-track-index="100" '
            'data-media-start="0"></audio></div></body></html>',
            encoding="utf-8",
        )
        contract = VideoDeliveryContract(scenes=scenes)
        (source_project / "video_delivery_contract.json").write_text(
            json.dumps(contract.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        spec = {"artifact_type": "video", "name": "Paper video"}
        (self.runs_dir / parent_run_id / "design_spec.json").write_text(
            json.dumps(
                {
                    "design_spec": spec,
                    "design_spec_sha256": design_spec_sha256(spec),
                    "revision": 1,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        fake_hyperframes = self.root / "hyperframes"
        shutil.copy2(DETACHED_HYPERFRAMES_FIXTURE, fake_hyperframes)
        fake_hyperframes.chmod(0o755)

        run_id = "video-production-cancel"
        self._queue(run_id, "video")
        request = VideoExportRetryWorkerRequest(
            job_kind="video_export_retry",
            run_id=run_id,
            parent_run_id=parent_run_id,
            source_project=str(source_project),
            conversation_id="conversation",
            baseline_artifact_json="{}",
            runs_dir=str(self.runs_dir),
        )
        supervisor = RunSupervisor(
            self.runs_dir,
            control_store=self.store,
            grace_s=0.1,
        )
        self.supervisors.append(supervisor)
        run_project = self.runs_dir / run_id / source_project.name
        pid_path = run_project / "detached-writer.pid"
        heartbeat = run_project / "detached-writer-heartbeat.txt"
        detached_pid: int | None = None
        try:
            with mock.patch.dict(
                "autodesign.run_supervisor._WORKER_ENV_SOURCE",
                {"AUTODESIGN_HYPERFRAMES_BIN": str(fake_hyperframes)},
                clear=False,
            ):
                await supervisor.start(request)
                await _wait_for(
                    lambda: pid_path.is_file()
                    and heartbeat.is_file()
                    and heartbeat.stat().st_size >= 15,
                )
                detached_pid = int(pid_path.read_text(encoding="utf-8"))
                detached_identity = process_identity(detached_pid)
                outcome = await supervisor.cancel(run_id, "user_requested")
            frozen = heartbeat.read_bytes()
            await asyncio.sleep(0.2)

            self.assertEqual(outcome.state, "cancelled")
            self.assertEqual(outcome.surviving_pids, ())
            self.assertEqual(heartbeat.read_bytes(), frozen)
            self.assertFalse(process_is_alive(detached_identity))
            self.assertFalse(
                (self.runs_dir / run_id / "final" / "video_delivery.json").exists()
            )
        finally:
            if detached_pid is not None:
                try:
                    os.kill(detached_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_run_worker_does_not_import_web_transport(self) -> None:
        worker_path = REPO_ROOT / "autodesign" / "run_worker.py"
        tree = ast.parse(worker_path.read_text(encoding="utf-8"), filename=str(worker_path))
        violations: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "scripts.web_server":
                violations.append(f"line {node.lineno}: from scripts.web_server import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "scripts.web_server":
                        violations.append(f"line {node.lineno}: import scripts.web_server")

        self.assertEqual(
            violations,
            [],
            "core worker must call autodesign-owned derived-task modules, not Web transport: "
            + "; ".join(violations),
        )

    def test_editable_video_worker_uses_explicit_parent_authority_and_exact_result(self) -> None:
        from autodesign.run_worker import _run_editable_video_render

        parent_dir = self.runs_dir / "video-parent"
        parent_dir.mkdir(parents=True)
        request = EditableVideoRenderWorkerRequest(
            job_kind="editable_video_render",
            run_id="video-child",
            parent_run_id="video-parent",
            artifact={"video_project": {}},
            conversation_id="conversation",
            baseline_artifact_json="{}",
            settings=self.settings,
        )
        expanded = {
            "run_id": "video-child",
            "project_dir": "diagnostic-project",
            "mp4_path": "render.mp4",
            "fps": 30,
            "render": {"status": "ok"},
        }

        with mock.patch(
            "autodesign.editable_video_job.run_editable_video_job",
            return_value=expanded,
        ) as core:
            result = _run_editable_video_render(
                request,
                CancellationToken.never("video-child"),
            )

        self.assertEqual(result, {"run_id": "video-child", "mp4_path": "render.mp4"})
        self.assertEqual(core.call_args.kwargs["source_run_dir"], parent_dir.resolve())
        self.assertEqual(core.call_args.kwargs["run_dir"], (self.runs_dir / "video-child").resolve())

    def test_pptx_worker_uses_core_and_exact_result(self) -> None:
        from autodesign.run_worker import _run_pptx_export

        parent_dir = self.runs_dir / "pptx-parent"
        parent_dir.mkdir(parents=True)
        source = parent_dir / "poster.html"
        source.write_text("<html></html>", encoding="utf-8")
        request = PptxExportWorkerRequest(
            job_kind="pptx_export",
            run_id="pptx-child",
            parent_run_id="pptx-parent",
            source_html=str(source),
            artifact={"name": "Poster"},
            artifact_name="Poster",
            conversation_id="conversation",
            settings=self.settings,
        )
        expanded = {
            "run_id": "pptx-child",
            "pptx_path": "export.pptx",
            "manifest_path": "manifest.json",
            "attempts": [],
            "canvas": {"w": 1, "h": 1},
        }

        with mock.patch(
            "autodesign.pptx_export_job.run_pptx_export_job",
            return_value=expanded,
        ) as core:
            result = _run_pptx_export(request, CancellationToken.never("pptx-child"))

        self.assertEqual(result, {"run_id": "pptx-child", "pptx_path": "export.pptx"})
        self.assertEqual(
            Path(core.call_args.kwargs["source_html"]).resolve(),
            source.resolve(),
        )
        self.assertEqual(core.call_args.kwargs["run_dir"], (self.runs_dir / "pptx-child").resolve())

    def test_video_retry_cancellation_after_invalidation_carries_known_warnings(
        self,
    ) -> None:
        run_dir = self.runs_dir / "cancel-after-invalidation"
        project_dir = _make_retry_project(run_dir)
        warning = "invalidation cleanup warning"
        cancellation = RunCancelled(
            run_dir.name,
            "video.retry.after_delivery_invalidation",
        )
        token = _RaiseExactCancellationAtPhase(
            "video.retry.after_delivery_invalidation",
            cancellation,
        )

        with _patched_successful_retry_path(
            invalidation_warning=warning,
            publication_warning="unused publication warning",
        ):
            with self.assertRaises(RunCancelled) as raised:
                export_video_module.retry_video_export_project(
                    run_dir,
                    project_dir,
                    cancellation_token=token,
                )

        self.assertIs(raised.exception, cancellation)
        self.assertEqual(
            getattr(raised.exception, "pointer_cleanup_warnings", None),
            (warning,),
        )

    def test_video_retry_nested_cancellation_carries_invalidation_warnings(
        self,
    ) -> None:
        warning = "nested cancellation invalidation warning"
        for role in ("authoring", "render"):
            with self.subTest(role=role):
                run_dir = self.runs_dir / f"cancel-inside-{role}"
                project_dir = _make_retry_project(run_dir)
                cancellation = RunCancelled(
                    run_dir.name,
                    f"video.retry.{role}.nested",
                )
                with _patched_successful_retry_path(
                    invalidation_warning=warning,
                    publication_warning="unused publication warning",
                    nested_cancellation=(role, cancellation),
                ):
                    with self.assertRaises(RunCancelled) as raised:
                        export_video_module.retry_video_export_project(
                            run_dir,
                            project_dir,
                            cancellation_token=CancellationToken.never(run_dir.name),
                        )

                self.assertIs(raised.exception, cancellation)
                self.assertEqual(
                    getattr(raised.exception, "pointer_cleanup_warnings", None),
                    (warning,),
                )

    def test_video_retry_cancellation_after_publication_carries_all_warnings(
        self,
    ) -> None:
        run_dir = self.runs_dir / "cancel-after-publication"
        project_dir = _make_retry_project(run_dir)
        invalidation_warning = "first invalidation cleanup warning"
        publication_warning = "second publication cleanup warning"
        cancellation = RunCancelled(
            run_dir.name,
            "video.retry.after_final_pointer",
        )
        token = _RaiseExactCancellationAtPhase(
            "video.retry.after_final_pointer",
            cancellation,
        )

        with _patched_successful_retry_path(
            invalidation_warning=invalidation_warning,
            publication_warning=publication_warning,
        ):
            with self.assertRaises(RunCancelled) as raised:
                export_video_module.retry_video_export_project(
                    run_dir,
                    project_dir,
                    cancellation_token=token,
                )

        self.assertIs(raised.exception, cancellation)
        self.assertEqual(
            getattr(raised.exception, "pointer_cleanup_warnings", None),
            (invalidation_warning, publication_warning),
        )

    def test_worker_after_retry_cancellation_snapshots_warnings_before_validation(
        self,
    ) -> None:
        invalidation_warning = "outer invalidation warning"
        publication_warning = "outer publication warning"
        cases = {
            "valid_warning_list": (
                {
                    "ok": "malformed-result",
                    "pointer_cleanup_warnings": [
                        invalidation_warning,
                        invalidation_warning,
                        publication_warning,
                    ],
                },
                (invalidation_warning, publication_warning),
            ),
            "malformed_warning_container": (
                {
                    "ok": "malformed-result",
                    "pointer_cleanup_warnings": ("not-a-list",),
                },
                (),
            ),
        }

        for case, (retry_result, expected_warnings) in cases.items():
            with self.subTest(case=case):
                parent_run_id = f"after-retry-parent-{case}"
                run_id = f"after-retry-child-{case}"
                parent_dir = self.runs_dir / parent_run_id
                source_project = parent_dir / "hyperframes-source"
                source_project.mkdir(parents=True)
                (source_project / "index.html").write_text(
                    "<html></html>",
                    encoding="utf-8",
                )
                (parent_dir / "design_spec.json").write_text(
                    "{}\n",
                    encoding="utf-8",
                )
                request = VideoExportRetryWorkerRequest(
                    job_kind="video_export_retry",
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                    source_project=str(source_project),
                    conversation_id="conversation",
                    baseline_artifact_json="{}",
                    runs_dir=str(self.runs_dir),
                )
                cancellation = RunCancelled(
                    run_id,
                    "worker.video_export_retry.after_retry",
                )
                token = _RaiseExactCancellationAtPhase(
                    "worker.video_export_retry.after_retry",
                    cancellation,
                )

                with mock.patch.object(
                    export_video_module,
                    "retry_video_export_project",
                    return_value=retry_result,
                ):
                    with self.assertRaises(RunCancelled) as raised:
                        run_worker_module._run_video_export_retry(request, token)

                self.assertIs(raised.exception, cancellation)
                self.assertEqual(
                    getattr(raised.exception, "pointer_cleanup_warnings", None),
                    expected_warnings,
                )
                self.assertEqual(
                    token.calls[-1],
                    "worker.video_export_retry.after_retry",
                )

    def test_video_worker_cancellation_envelope_has_exact_warnings_and_phase(
        self,
    ) -> None:
        run_id = "video-cancellation-envelope"
        request = VideoExportRetryWorkerRequest(
            job_kind="video_export_retry",
            run_id=run_id,
            parent_run_id="video-cancellation-parent",
            source_project=str(self.runs_dir / "unused-source"),
            conversation_id="conversation",
            baseline_artifact_json="{}",
            runs_dir=str(self.runs_dir),
        )
        cancellation = RunCancelled(run_id, "video.retry.render.nested")
        cancellation.pointer_cleanup_warnings = (
            "first cleanup warning",
            "first cleanup warning",
            "second cleanup warning",
        )

        exit_code, payload = self._worker_cancellation_payload(
            request,
            cancellation,
        )
        error = payload.get("error")
        self.assertIsInstance(error, dict)
        assert isinstance(error, dict)
        self.assertEqual(
            {
                "exit_code": exit_code,
                "payload_ok": payload.get("ok"),
                "error_fields": set(error),
                "warnings": error.get("pointer_cleanup_warnings"),
                "phase": error.get("phase"),
                "message_bounded": len(str(error.get("message") or "")) <= 2000,
            },
            {
                "exit_code": 2,
                "payload_ok": False,
                "error_fields": {
                    "type",
                    "message",
                    "traceback",
                    "pointer_cleanup_warnings",
                    "phase",
                },
                "warnings": [
                    "first cleanup warning",
                    "second cleanup warning",
                ],
                "phase": "video.retry.render.nested",
                "message_bounded": True,
            },
        )

    def test_non_video_worker_cancellation_omits_video_diagnostics(self) -> None:
        run_id = "nonvideo-cancellation-envelope"
        request = EditableVideoRenderWorkerRequest(
            job_kind="editable_video_render",
            run_id=run_id,
            parent_run_id="nonvideo-cancellation-parent",
            artifact={},
            conversation_id="conversation",
            baseline_artifact_json="{}",
            settings=self.settings,
        )
        cancellation = RunCancelled(run_id, "editable-video.render")
        cancellation.pointer_cleanup_warnings = ("video-only warning",)

        exit_code, payload = self._worker_cancellation_payload(
            request,
            cancellation,
        )
        error = payload.get("error")
        self.assertIsInstance(error, dict)
        assert isinstance(error, dict)
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload.get("ok"), False)
        self.assertEqual(set(error), {"type", "message", "traceback"})
        self.assertNotIn("pointer_cleanup_warnings", error)
        self.assertNotIn("phase", error)

    async def test_video_retry_long_failure_retains_cleanup_warning_in_worker_and_outcome(
        self,
    ) -> None:
        parent_run_id = "warning-source"
        run_id = "warning-child"
        parent_dir = self.runs_dir / parent_run_id
        source_project = parent_dir / "hyperframes-warning"
        source_project.mkdir(parents=True)
        (source_project / "index.html").write_text("<html></html>", encoding="utf-8")
        (parent_dir / "design_spec.json").write_text("{}\n", encoding="utf-8")
        self._queue(run_id, "video")
        queued = self.store.read(run_id)
        self.store.transition(run_id, queued, "running")
        request = VideoExportRetryWorkerRequest(
            job_kind="video_export_retry",
            run_id=run_id,
            parent_run_id=parent_run_id,
            source_project=str(source_project),
            conversation_id="conversation",
            baseline_artifact_json="{}",
            runs_dir=str(self.runs_dir),
        )
        base_error = "base-error-" + ("x" * 2400)
        warnings = [
            "invalidation cleanup warning survived",
            "publication cleanup warning survived",
        ]
        stdin = SimpleNamespace(buffer=BytesIO(encode_request(request)))

        with (
            mock.patch(
                "autodesign.tools.export_video.retry_video_export_project",
                return_value={
                    "ok": False,
                    "phase": "final_pointer",
                    "error": base_error,
                    "pointer_cleanup_warnings": warnings,
                },
            ),
            mock.patch.object(sys, "argv", ["run_worker", "--run-id", run_id]),
            mock.patch.object(sys, "stdin", stdin),
            mock.patch.object(run_worker_module.signal, "signal"),
            mock.patch.object(
                run_worker_module.os,
                "getsid",
                return_value=os.getpid(),
            ),
        ):
            exit_code = run_worker_module.worker_main()

        worker_payload = json.loads(
            (self.runs_dir / run_id / "worker_result.json").read_text(
                encoding="utf-8"
            )
        )
        supervisor = RunSupervisor(self.runs_dir, control_store=self.store)

        class _FinishedProcess:
            async def wait(self) -> int:
                return exit_code

        async def done(value):
            return value

        outcome = await supervisor._monitor(
            request,
            _FinishedProcess(),
            stdout_task=asyncio.create_task(done(None)),
            stderr_task=asyncio.create_task(done(None)),
            relay_task=asyncio.create_task(done(0)),
        )
        persisted_error = json.dumps(
            worker_payload.get("error"),
            ensure_ascii=False,
            sort_keys=True,
        )

        self.assertEqual(exit_code, 1)
        self.assertFalse(worker_payload.get("ok"))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.failure_phase, "final_pointer")
        worker_error = worker_payload.get("error")
        worker_error_message = (
            worker_error.get("message")
            if isinstance(worker_error, dict)
            else None
        )
        self.assertIsInstance(worker_error_message, str)
        self.assertLessEqual(len(worker_error_message), 2000)
        self.assertLessEqual(len(outcome.error or ""), 2000)
        for warning in warnings:
            with self.subTest(warning=warning):
                self.assertIn(warning, persisted_error)
                self.assertIn(warning, outcome.error or "")

    def test_worker_rejects_symlinked_parent_and_derived_run_dirs(self) -> None:
        from autodesign.run_worker import _run_editable_video_render

        victim = self.runs_dir / "victim"
        victim.mkdir(parents=True)
        sentinel = victim / "must-not-change.txt"
        sentinel.write_text("original", encoding="utf-8")
        parent = self.runs_dir / "parent"
        parent.mkdir()

        for alias_kind, run_id, parent_run_id in (
            ("parent", "child", "declared-parent"),
            ("derived", "declared-child", "parent"),
        ):
            with self.subTest(alias_kind=alias_kind):
                alias = self.runs_dir / (parent_run_id if alias_kind == "parent" else run_id)
                try:
                    alias.symlink_to(victim, target_is_directory=True)
                except (OSError, NotImplementedError):
                    self.skipTest("symlinks unavailable")
                request = EditableVideoRenderWorkerRequest(
                    job_kind="editable_video_render",
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                    artifact={"video_project": {}},
                    conversation_id="conversation",
                    baseline_artifact_json="{}",
                    settings=self.settings,
                )
                with mock.patch("autodesign.editable_video_job.run_editable_video_job") as core:
                    with self.assertRaisesRegex(RuntimeError, "symlink|canonical"):
                        _run_editable_video_render(
                            request,
                            CancellationToken.never(run_id),
                        )
                core.assert_not_called()
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "original")
                alias.unlink()


if __name__ == "__main__":
    unittest.main()
