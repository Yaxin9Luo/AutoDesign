from __future__ import annotations

from contextlib import ExitStack, nullcontext
from dataclasses import replace
import errno
import hashlib
from importlib import import_module
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from autodesign import attempt_candidates as attempt_candidates_module
from autodesign import video_pointer_transaction as video_pointer_transaction_module
from autodesign.agents.external_video_author import ExternalVideoAuthor
from autodesign.runner import _derive_episode_outcome, _recover_missing_composite
from autodesign.attempt_selection import (
    leased_promotion_tool_context,
    load_selection_journal,
    normal_promotion_lease,
    promote_pending_selection,
    request_attempt_selection,
)
from autodesign.attempt_candidates import (
    capture_attempt_candidate,
    is_active_promotion_filesystem_root,
    load_selection_adapter_transaction,
)
from autodesign.schema import CompositionArtifacts, ToolResultRecord
from autodesign.tools._contract import ToolContext
from autodesign.tools.finalize import finalize
from autodesign.util.design_spec_fingerprint import design_spec_sha256
from autodesign.util.io import sha256_file


finalize_module = import_module("autodesign.tools.finalize")


def _context(root: Path) -> ToolContext:
    ctx = ToolContext(
        settings=SimpleNamespace(poster_harness_mode="production"),
        run_dir=root,
        layers_dir=root / "layers",
        run_id="video-runner-test",
    )
    ctx.state["design_spec"] = SimpleNamespace(
        artifact_type="video",
        model_dump=lambda mode=None: {"artifact_type": "video", "revision": 1},
    )
    ctx.state["artifact_type"] = "video"
    ctx.state["spec_revision_count"] = 1
    return ctx


def _install_passed_delivery(ctx: ToolContext) -> tuple[Path, Path]:
    project = ctx.run_dir / "hyperframes-video"
    project.mkdir(parents=True)
    index_path = project / "index.html"
    contract_path = project / "video_delivery_contract.json"
    probe_path = project / "media_probe.json"
    mp4_path = project / "renders" / "attempt.mp4"
    mp4_path.parent.mkdir()
    narration_dir = project / "narration"
    narration_dir.mkdir()
    assets_dir = project / "assets"
    assets_dir.mkdir()
    narration_audio = assets_dir / "narration.wav"
    local_asset = assets_dir / "local.png"
    transcript = narration_dir / "transcript.en.txt"
    srt = narration_dir / "subtitles.en.srt"
    vtt = narration_dir / "subtitles.en.vtt"
    voice = narration_dir / "voice.json"
    timing = narration_dir / "timing.json"
    index_path.write_text("<!doctype html><title>video</title>", encoding="utf-8")
    contract_path.write_text(
        json.dumps({
            "target_duration_s": 360,
            "scenes": [
                {
                    "scene_id": f"scene_{index:02d}",
                    "start_s": float((index - 1) * 30),
                    "duration_s": 30.0,
                }
                for index in range(1, 13)
            ],
            "narration_contract": {
                "minimum_spoken_wpm": 90,
                "minimum_speech_coverage_ratio": 0.72,
            },
        }) + "\n",
        encoding="utf-8",
    )
    probe = {
        "video_codec": "h264", "pixel_format": "yuv420p", "audio_codec": "aac",
        "width": 1920, "height": 1080, "fps": 30, "duration_s": 360.0,
        "subtitle_codec": "mov_text", "subtitle_forced": False,
    }
    probe_path.write_text(json.dumps(probe) + "\n", encoding="utf-8")
    mp4_path.write_bytes(b"current mp4")
    local_asset.write_bytes(b"local poster frame")
    for path, content in {
        narration_audio: b"audio",
        transcript: b"English transcript\n",
        srt: b"1\n00:00:00,000 --> 00:00:01,000\nEnglish\n",
        vtt: b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nEnglish\n",
        voice: b'{"preset":"female"}\n',
        timing: json.dumps([
            {
                "scene_id": f"scene_{index:02d}",
                "start_s": float((index - 1) * 30),
                "speech_duration_s": 22.0,
                "end_s": float((index - 1) * 30 + 22),
            }
            for index in range(1, 13)
        ]).encode("utf-8") + b"\n",
    }.items():
        path.write_bytes(content)
    spec_hash = design_spec_sha256(ctx.state["design_spec"])
    (ctx.run_dir / "design_spec.json").write_text(
        json.dumps({
            "design_spec": ctx.state["design_spec"].model_dump(mode="json"),
            "design_spec_sha256": spec_hash,
            "revision": 1,
        }) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "status": "passed",
        "design_spec_sha256": spec_hash,
        "design_spec_revision": 1,
        "source_html_path": "index.html",
        "contract_path": "video_delivery_contract.json",
        "media_probe_path": "media_probe.json",
        "mp4_path": "renders/attempt.mp4",
        "narration_audio_path": "assets/narration.wav",
        "transcript_path": "narration/transcript.en.txt",
        "srt_path": "narration/subtitles.en.srt",
        "vtt_path": "narration/subtitles.en.vtt",
        "voice_metadata_path": "narration/voice.json",
        "narration_timing_path": "narration/timing.json",
        "render_started_at": "2026-07-20T12:00:00+00:00",
        "source_html_sha256": sha256_file(index_path),
        "contract_sha256": sha256_file(contract_path),
        "media_probe_sha256": sha256_file(probe_path),
        "mp4_sha256": sha256_file(mp4_path),
        "narration_audio_sha256": sha256_file(narration_audio),
        "transcript_sha256": sha256_file(transcript),
        "srt_sha256": sha256_file(srt),
        "vtt_sha256": sha256_file(vtt),
        "voice_metadata_sha256": sha256_file(voice),
        "narration_timing_sha256": sha256_file(timing),
        "local_asset_sha256": {
            "assets/local.png": sha256_file(local_asset),
            "index.html": sha256_file(index_path),
        },
        "media_probe": probe,
        "speech_duration_s": 264.0,
        "coverage_duration_s": 360.0,
        "speech_coverage_ratio": 264.0 / 360.0,
        "minimum_speech_coverage_ratio": 0.72,
        "measured_speech_scene_count": 12,
        "spoken_word_count": 540,
        "spoken_wpm": 90.0,
        "minimum_spoken_wpm": 90,
    }
    manifest_path = project / "delivery_manifest.json"
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    ctx.state["video_delivery"] = {
        "status": "passed",
        "project_dir": str(project),
        "manifest_path": str(manifest_path),
        "media_probe_path": str(probe_path),
        "mp4_path": str(mp4_path),
        "render_started_at": manifest["render_started_at"],
        "design_spec_sha256": spec_hash,
        "design_spec_revision": manifest["design_spec_revision"],
        "delivery_manifest_sha256": sha256_file(manifest_path),
    }
    ctx.state["composition"] = CompositionArtifacts(
        html_path=str(index_path),
        preview_path=str(mp4_path),
        layer_manifest=[{"kind": "video", "path": str(mp4_path)}],
    )
    return manifest_path, mp4_path


def _passed_delivery_result(ctx: ToolContext) -> ToolResultRecord:
    delivery = ctx.state["video_delivery"]
    return ToolResultRecord(
        status="ok",
        payload={
            "project_dir": delivery["project_dir"],
            "delivery_manifest_path": "delivery_manifest.json",
            "media_probe_path": delivery["media_probe_path"],
            "mp4_path": delivery["mp4_path"],
            "render_ok": True,
        },
    )


def _replace_delivery_path_with_link(
    ctx: ToolContext,
    link_case: str,
    *,
    outside_dir: Path,
) -> None:
    delivery = ctx.state["video_delivery"]
    project_dir = Path(delivery["project_dir"])
    if link_case == "in_run_project_directory":
        real_project = project_dir.with_name(project_dir.name + "-real")
        project_dir.rename(real_project)
        project_dir.symlink_to(real_project.name, target_is_directory=True)
        return

    linked_paths = {
        "in_run_source_html": project_dir / "index.html",
        "in_run_manifest": Path(delivery["manifest_path"]),
        "in_run_probe": Path(delivery["media_probe_path"]),
        "in_run_mp4": Path(delivery["mp4_path"]),
        "external_mp4": Path(delivery["mp4_path"]),
    }
    linked_path = linked_paths[link_case]
    if link_case == "external_mp4":
        target = outside_dir / linked_path.name
        target.write_bytes(linked_path.read_bytes())
        linked_path.unlink()
        linked_path.symlink_to(target)
        return

    target = linked_path.with_name(
        f"{linked_path.stem}.real{linked_path.suffix}"
    )
    linked_path.rename(target)
    linked_path.symlink_to(target.name)


def _authored_video_manifest() -> dict[str, object]:
    return {
        "target_duration_s": 360,
        "voice_preset": "female",
        "scenes": [
            {
                "scene_id": f"scene_{index:02d}",
                "title": f"Scene {index}",
                "duration_s": 30,
                "narration_intent": "Source-grounded narration for the selected video.",
            }
            for index in range(1, 13)
        ],
    }


def _automatic_video_project(run_dir: Path) -> Path:
    project_dir = run_dir / "video_author" / "attempt_01" / "project"
    project_dir.mkdir(parents=True)
    (project_dir / "index.html").write_text(
        "<!doctype html><title>automatic candidate</title>",
        encoding="utf-8",
    )
    return project_dir


def _video_blocker_reason(result: ToolResultRecord) -> str:
    blocker = result.payload.get("video_delivery_final_blocker") or {}
    return str(blocker.get("reason") or "").lower()


class _FakeWindowsRetainedHandleAPI:
    def __init__(self, *, move_error: int | None = None) -> None:
        self.handles: dict[int, int] = {}
        self.next_handle = 100
        self.move_error = move_error
        self.move_calls: list[tuple[Path, Path, int]] = []
        self.create_calls: list[tuple[Path, int, int, object, int, int]] = []
        self.closed: list[int] = []

    def create_file(
        self,
        path,
        desired_access,
        share_mode,
        security_attributes,
        creation_disposition,
        flags_and_attributes,
    ):
        target = Path(path)
        self.create_calls.append(
            (
                target,
                desired_access,
                share_mode,
                security_attributes,
                creation_disposition,
                flags_and_attributes,
            )
        )
        flags = (
            os.O_RDWR | os.O_CREAT | os.O_EXCL
            if creation_disposition == 1
            else os.O_RDONLY
        )
        descriptor = os.open(target, flags, 0o600)
        handle = self.next_handle
        self.next_handle += 1
        self.handles[handle] = descriptor
        return handle

    def get_file_information_by_handle_ex(self, handle, info_class):
        metadata = os.fstat(self.handles[handle])
        if info_class == "FileIdInfo":
            return {
                "volume_serial_number": metadata.st_dev,
                "file_id": int(metadata.st_ino).to_bytes(16, "little"),
            }
        if info_class == "FileStandardInfo":
            return {
                "number_of_links": metadata.st_nlink,
                "end_of_file": metadata.st_size,
                "directory": False,
            }
        if info_class == "FileBasicInfo":
            return {
                "file_attributes": 0,
                "last_write_time": metadata.st_mtime_ns,
                "creation_time": metadata.st_ctime_ns,
                "change_time": metadata.st_ctime_ns,
                "last_access_time": metadata.st_atime_ns,
            }
        raise AssertionError(f"unexpected info class {info_class}")

    def write_file(self, handle, data):
        return os.write(self.handles[handle], bytes(data[:3]))

    def read_file(self, handle, size):
        return os.read(self.handles[handle], min(size, 2))

    def set_file_pointer_ex(self, handle, offset):
        os.lseek(self.handles[handle], offset, os.SEEK_SET)

    def flush_file_buffers(self, handle):
        os.fsync(self.handles[handle])

    def close_handle(self, handle):
        if handle in self.closed:
            raise AssertionError("native HANDLE was closed twice")
        self.closed.append(handle)
        os.close(self.handles.pop(handle))

    def get_last_error(self):
        return int(self.move_error or 183)

    def move_file_ex(self, source, destination, flags):
        source_path = Path(source)
        destination_path = Path(destination)
        self.move_calls.append((source_path, destination_path, flags))
        if self.move_error is not None or destination_path.exists():
            return 0
        os.rename(source_path, destination_path)
        return 1


class _Step2hFakeWindowsRetainedHandleAPI(_FakeWindowsRetainedHandleAPI):
    def __init__(
        self,
        pointer: Path,
        *,
        volume_serial_number: int = 0xFEDCBA9876543210,
        file_id: bytes = bytes.fromhex("1032547698badcfe0123456789abcdef"),
        file_attributes: int = 0x20,
        last_write_time: int = 0x0123456789ABCDEF,
        pointer_read_passes: tuple[bytes, ...] = (),
        use_python_identity: bool = False,
    ) -> None:
        super().__init__()
        pointer_metadata = os.stat(pointer)
        self.pointer = pointer
        self.pointer_identity = (
            int(pointer_metadata.st_dev),
            int(pointer_metadata.st_ino),
        )
        self.volume_serial_number = (
            int(pointer_metadata.st_dev)
            if use_python_identity
            else volume_serial_number
        )
        self.file_id = (
            int(pointer_metadata.st_ino).to_bytes(16, "little", signed=False)
            if use_python_identity
            else file_id
        )
        self.file_attributes = file_attributes
        self.last_write_time = last_write_time
        self.pointer_read_passes = pointer_read_passes
        self.opened_paths: list[tuple[int, Path]] = []
        self.handle_identities: dict[int, tuple[int, int]] = {}
        self.close_counts: dict[int, int] = {}
        self.read_rounds: dict[int, list[int]] = {}
        self._active_read_round: dict[int, int] = {}
        self._read_offsets: dict[int, int] = {}
        self.events: list[tuple[object, ...]] = []

    def create_file(
        self,
        path,
        desired_access,
        share_mode,
        security_attributes,
        creation_disposition,
        flags_and_attributes,
    ):
        handle = super().create_file(
            path,
            desired_access,
            share_mode,
            security_attributes,
            creation_disposition,
            flags_and_attributes,
        )
        target = Path(path)
        metadata = os.fstat(self.handles[handle])
        self.opened_paths.append((handle, target))
        self.handle_identities[handle] = (
            int(metadata.st_dev),
            int(metadata.st_ino),
        )
        self.events.append(("open", handle, target.name))
        return handle

    def _is_original_pointer(self, handle: int) -> bool:
        return self.handle_identities[handle] == self.pointer_identity

    def get_file_information_by_handle_ex(self, handle, info_class):
        metadata = os.fstat(self.handles[handle])
        original_pointer = self._is_original_pointer(handle)
        if info_class == "FileIdInfo":
            file_id = self.file_id if original_pointer else (
                (
                    (0xA11CE00000000000 << 64)
                    | (int(metadata.st_ino) & ((1 << 64) - 1))
                ).to_bytes(16, "little")
            )
            return {
                "volume_serial_number": self.volume_serial_number,
                "file_id": file_id,
            }
        if info_class == "FileStandardInfo":
            size = (
                len(self.pointer_read_passes[0])
                if original_pointer and self.pointer_read_passes
                else int(metadata.st_size)
            )
            return {
                "number_of_links": int(metadata.st_nlink),
                "end_of_file": size,
                "directory": False,
            }
        if info_class == "FileBasicInfo":
            return {
                "file_attributes": (
                    self.file_attributes if original_pointer else 0x20
                ),
                "last_write_time": (
                    self.last_write_time
                    if original_pointer
                    else int(metadata.st_mtime_ns)
                ),
                "creation_time": int(metadata.st_ctime_ns),
                "change_time": int(metadata.st_ctime_ns),
                "last_access_time": int(metadata.st_atime_ns),
            }
        raise AssertionError(f"unexpected info class {info_class}")

    def set_file_pointer_ex(self, handle, offset):
        if self._is_original_pointer(handle) and self.pointer_read_passes:
            if offset != 0:
                raise AssertionError("scripted pointer reads only support rewind")
            read_round = len(self.read_rounds.setdefault(handle, []))
            self.read_rounds[handle].append(read_round)
            self._active_read_round[handle] = read_round
            self._read_offsets[handle] = 0
            self.events.append(("rewind", handle, read_round))
            return
        super().set_file_pointer_ex(handle, offset)

    def read_file(self, handle, size):
        if self._is_original_pointer(handle) and self.pointer_read_passes:
            read_round = self._active_read_round[handle]
            source = self.pointer_read_passes[
                min(read_round, len(self.pointer_read_passes) - 1)
            ]
            offset = self._read_offsets[handle]
            chunk = source[offset : offset + min(size, 2)]
            self._read_offsets[handle] += len(chunk)
            self.events.append(("read", handle, read_round, len(chunk)))
            return chunk
        return super().read_file(handle, size)

    def close_handle(self, handle):
        self.close_counts[handle] = self.close_counts.get(handle, 0) + 1
        self.events.append(("close", handle))
        super().close_handle(handle)

    def pointer_open_handles(self) -> list[int]:
        return [
            handle
            for handle, _path in self.opened_paths
            if self.handle_identities[handle] == self.pointer_identity
        ]

    def close_remaining(self) -> None:
        for handle in list(self.handles):
            self.close_handle(handle)


class VideoFinalizeContractTest(unittest.TestCase):
    def test_finalize_maps_delivery_state_through_promotion_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)

            with normal_promotion_lease(
                run_dir=run_dir,
                candidate_id="normal-video-candidate",
            ) as leased_run_dir:
                with leased_promotion_tool_context(ctx, leased_run_dir):
                    result = finalize({"notes": "leased video complete"}, ctx=ctx)
                    self.assertEqual(result.status, "ok", result.error_message)
                    pointer = json.loads(
                        (ctx.run_dir / "final" / "video_delivery.json").read_text(
                            encoding="utf-8"
                        )
                    )

            self.assertTrue(ctx.state["finalized"])
            self.assertEqual(
                pointer["manifest_path"],
                "hyperframes-video/delivery_manifest.json",
            )

            unsafe_ctx = _context(run_dir / "unsafe-run")
            _install_passed_delivery(unsafe_ctx)
            unsafe_ctx.state["video_delivery"]["project_dir"] = str(
                run_dir / "outside-project"
            )
            with normal_promotion_lease(
                run_dir=unsafe_ctx.run_dir,
                candidate_id="unsafe-video-candidate",
            ) as leased_run_dir:
                with leased_promotion_tool_context(unsafe_ctx, leased_run_dir):
                    unsafe_result = finalize({}, ctx=unsafe_ctx)

            self.assertEqual(unsafe_result.status, "error")
            blocker = unsafe_result.payload["video_delivery_final_blocker"]
            self.assertIn("outside the current run", blocker["reason"])

    def test_finalize_rejects_every_persisted_video_delivery_link(self) -> None:
        cases = (
            ("control", True),
            ("in_run_project_directory", False),
            ("in_run_source_html", False),
            ("in_run_manifest", False),
            ("in_run_probe", False),
            ("in_run_mp4", False),
            ("external_mp4", False),
        )
        for link_case, should_finalize in cases:
            with (
                self.subTest(link_case=link_case),
                tempfile.TemporaryDirectory() as tmp,
                tempfile.TemporaryDirectory() as outside,
            ):
                run_dir = Path(tmp)
                ctx = _context(run_dir)
                _install_passed_delivery(ctx)
                if link_case != "control":
                    _replace_delivery_path_with_link(
                        ctx,
                        link_case,
                        outside_dir=Path(outside),
                    )

                with normal_promotion_lease(
                    run_dir=run_dir,
                    candidate_id=f"{link_case}-video-candidate",
                ) as leased_run_dir:
                    with leased_promotion_tool_context(ctx, leased_run_dir):
                        result = finalize({}, ctx=ctx)

                pointer_path = run_dir / "final" / "video_delivery.json"
                if should_finalize:
                    self.assertEqual(result.status, "ok", result.error_message)
                    self.assertTrue(ctx.state["finalized"])
                    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
                    self.assertEqual(
                        pointer["manifest_path"],
                        "hyperframes-video/delivery_manifest.json",
                    )
                else:
                    self.assertEqual(result.status, "error")
                    blocker = result.payload["video_delivery_final_blocker"]
                    reason = str(blocker.get("reason") or "").lower()
                    self.assertTrue(
                        any(
                            marker in reason
                            for marker in (
                                "link",
                                "reparse",
                                "no-follow",
                                "no follow",
                                "nofollow",
                            )
                        ),
                        f"expected an explicit link/reparse/no-follow blocker, got: {reason}",
                    )
                    self.assertFalse(ctx.state.get("finalized", False))
                    self.assertFalse(pointer_path.exists())

    def test_selected_attempt_promotion_finalizes_video_delivery_in_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "selected-video-run"
            attempt_dir = run_dir / "video_author" / "attempt_01"
            project_dir = attempt_dir / "project"
            project_dir.mkdir(parents=True)
            (project_dir / "index.html").write_text(
                "<!doctype html><title>candidate</title>", encoding="utf-8"
            )
            (attempt_dir / "video_author_manifest.json").write_text(
                json.dumps(_authored_video_manifest()) + "\n",
                encoding="utf-8",
            )
            (attempt_dir / "video_author_validation_errors.json").write_text(
                '{"errors":[]}', encoding="utf-8"
            )
            candidate = capture_attempt_candidate(
                run_dir=run_dir,
                attempt_dir=attempt_dir,
                artifact_type="video",
                attempt=1,
                max_attempts=2,
                source_path="project/index.html",
                dependency_paths=["video_author_manifest.json"],
                preview_paths=[],
                validation_summary_path="video_author_validation_errors.json",
                safety_state="ready",
                hard_blockers=[],
                warnings=[],
            )
            request_attempt_selection(
                run_dir=run_dir,
                run_id=run_dir.name,
                attempt=candidate.attempt,
                expected_candidate_sha256=candidate.source_sha256,
                idempotency_key="selected-video-candidate",
            )
            ctx = _context(run_dir)
            ctx.run_id = run_dir.name

            def deliver_selected_project(*, project_dir, manifest, ctx):
                self.assertTrue(project_dir.is_dir())
                self.assertEqual(manifest["target_duration_s"], 360)
                _install_passed_delivery(ctx)
                return _passed_delivery_result(ctx)

            with patch(
                "autodesign.agents.external_video_author."
                "deliver_authored_video_project",
                side_effect=deliver_selected_project,
            ):
                outcome = promote_pending_selection(ctx)

            self.assertEqual(outcome, "complete")
            self.assertTrue(ctx.state["finalized"])
            journal = load_selection_journal(run_dir)
            self.assertIsNotNone(journal)
            assert journal is not None
            self.assertEqual(journal.state, "complete")
            self.assertEqual(journal.candidate_id, candidate.candidate_id)
            self.assertEqual(journal.artifact_id, f"art_{ctx.run_id}")
            self.assertIsNone(journal.error_code)
            self.assertIsNone(journal.error_message)
            transaction = load_selection_adapter_transaction(run_dir)
            self.assertIsNotNone(transaction)
            assert transaction is not None
            self.assertEqual(transaction["phase"], "committed")
            pointer = json.loads(
                (run_dir / "final" / "video_delivery.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                pointer["manifest_path"],
                "hyperframes-video/delivery_manifest.json",
            )

    def test_automatic_video_candidate_uses_real_lease_finalize_and_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "automatic-video-run"
            project_dir = run_dir / "video_author" / "attempt_01" / "project"
            project_dir.mkdir(parents=True)
            (project_dir / "index.html").write_text(
                "<!doctype html><title>automatic candidate</title>",
                encoding="utf-8",
            )
            ctx = _context(run_dir)
            author = ExternalVideoAuthor(ctx.settings, "video system prompt")
            lease_observed = False

            def deliver_normal_project(*, project_dir, manifest, ctx):
                nonlocal lease_observed
                lease_observed = is_active_promotion_filesystem_root(ctx.run_dir)
                self.assertTrue(project_dir.is_dir())
                self.assertEqual(manifest["target_duration_s"], 360)
                _install_passed_delivery(ctx)
                return _passed_delivery_result(ctx)

            with patch(
                "autodesign.agents.external_video_author."
                "deliver_authored_video_project",
                side_effect=deliver_normal_project,
            ):
                delivery_result, finalize_result = author._deliver_normal_candidate(
                    candidate_id="automatic-video-candidate",
                    project_dir=project_dir,
                    manifest=_authored_video_manifest(),
                    ctx=ctx,
                )

            self.assertTrue(lease_observed)
            self.assertEqual(delivery_result.status, "ok")
            self.assertIsNotNone(finalize_result)
            assert finalize_result is not None
            self.assertEqual(
                finalize_result.status,
                "ok",
                finalize_result.error_message,
            )
            self.assertTrue(ctx.state["finalized"])
            pointer = json.loads(
                (run_dir / "final" / "video_delivery.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                pointer["manifest_path"],
                "hyperframes-video/delivery_manifest.json",
            )

    @unittest.skipUnless(os.name == "posix", "descriptor roots use POSIX links")
    def test_automatic_video_candidate_accepts_exact_descriptor_like_stable_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "automatic-video-run"
            project_dir = _automatic_video_project(run_dir)
            ctx = _context(run_dir)
            author = ExternalVideoAuthor(ctx.settings, "video system prompt")
            descriptor_paths: list[Path] = []

            def descriptor_like_stable_path(
                run_descriptor: int,
                _run_identity: tuple[int, int],
            ) -> Path:
                descriptor_path = (
                    root / "proc" / "self" / "fd" / str(run_descriptor)
                )
                descriptor_path.parent.mkdir(parents=True, exist_ok=True)
                descriptor_path.symlink_to(run_dir, target_is_directory=True)
                descriptor_paths.append(descriptor_path)
                return descriptor_path

            def deliver_normal_project(*, project_dir, manifest, ctx):
                self.assertTrue(project_dir.is_dir())
                self.assertEqual(manifest["target_duration_s"], 360)
                _install_passed_delivery(ctx)
                stable_root = descriptor_paths[-1]
                delivery = ctx.state["video_delivery"]
                for key in (
                    "project_dir",
                    "manifest_path",
                    "media_probe_path",
                    "mp4_path",
                ):
                    relative = Path(delivery[key]).relative_to(run_dir.resolve())
                    delivery[key] = str(stable_root / relative)
                return _passed_delivery_result(ctx)

            with (
                patch.object(
                    attempt_candidates_module,
                    "_posix_stable_run_path",
                    side_effect=descriptor_like_stable_path,
                ),
                patch(
                    "autodesign.agents.external_video_author."
                    "deliver_authored_video_project",
                    side_effect=deliver_normal_project,
                ),
            ):
                delivery_result, finalize_result = author._deliver_normal_candidate(
                    candidate_id="descriptor-root-video-candidate",
                    project_dir=project_dir,
                    manifest=_authored_video_manifest(),
                    ctx=ctx,
                )

            self.assertTrue(descriptor_paths)
            self.assertEqual(delivery_result.status, "ok")
            self.assertIsNotNone(finalize_result)
            assert finalize_result is not None
            self.assertEqual(
                finalize_result.status,
                "ok",
                finalize_result.error_message,
            )
            pointer = json.loads(
                (run_dir / "final" / "video_delivery.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                pointer["manifest_path"],
                "hyperframes-video/delivery_manifest.json",
            )

    @unittest.skipUnless(os.name == "posix", "no-follow swaps use dir fds")
    def test_finalize_rejects_manifest_member_swapped_at_secure_open(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as outside_tmp,
        ):
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)
            source_path = run_dir / "hyperframes-video" / "index.html"
            outside_path = Path(outside_tmp) / "index.html"
            outside_path.write_bytes(source_path.read_bytes())
            real_open = os.open
            swapped = False

            def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if (
                    not swapped
                    and dir_fd is not None
                    and os.fspath(path) == "index.html"
                ):
                    source_path.unlink()
                    source_path.symlink_to(outside_path)
                    swapped = True
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with normal_promotion_lease(
                run_dir=run_dir,
                candidate_id="manifest-member-swap",
            ) as leased_run_dir:
                with leased_promotion_tool_context(ctx, leased_run_dir):
                    with patch.object(
                        attempt_candidates_module.os,
                        "open",
                        side_effect=racing_open,
                    ):
                        result = finalize({}, ctx=ctx)

            self.assertTrue(swapped, "the secure-open boundary was not exercised")
            self.assertEqual(result.status, "error")
            reason = _video_blocker_reason(result)
            self.assertTrue(
                any(marker in reason for marker in ("link", "no-follow", "nofollow")),
                reason,
            )
            self.assertFalse((run_dir / "final" / "video_delivery.json").exists())

    @unittest.skipUnless(os.name == "posix", "no-follow swaps use dir fds")
    def test_finalize_rejects_nested_directory_swapped_at_secure_open(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as outside_tmp,
        ):
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)
            renders_dir = run_dir / "hyperframes-video" / "renders"
            outside_renders = Path(outside_tmp) / "renders"
            shutil.copytree(renders_dir, outside_renders)
            moved_renders = renders_dir.with_name("renders-before-swap")
            real_open = os.open
            swapped = False

            def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if (
                    not swapped
                    and dir_fd is not None
                    and os.fspath(path) == "renders"
                ):
                    renders_dir.rename(moved_renders)
                    renders_dir.symlink_to(outside_renders, target_is_directory=True)
                    swapped = True
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with normal_promotion_lease(
                run_dir=run_dir,
                candidate_id="nested-directory-swap",
            ) as leased_run_dir:
                with leased_promotion_tool_context(ctx, leased_run_dir):
                    with patch.object(
                        attempt_candidates_module.os,
                        "open",
                        side_effect=racing_open,
                    ):
                        result = finalize({}, ctx=ctx)

            self.assertTrue(swapped, "the nested secure-open boundary was not exercised")
            self.assertEqual(result.status, "error")
            reason = _video_blocker_reason(result)
            self.assertTrue(
                any(marker in reason for marker in ("link", "no-follow", "nofollow")),
                reason,
            )
            self.assertFalse((run_dir / "final" / "video_delivery.json").exists())

    @unittest.skipUnless(os.name == "posix", "no-follow swaps use dir fds")
    def test_automatic_video_rejects_project_manifest_and_member_link_swaps(
        self,
    ) -> None:
        cases = {
            "project": "hyperframes-video",
            "manifest": "delivery_manifest.json",
            "manifest_relative": "index.html",
        }
        for link_case, target_component in cases.items():
            with (
                self.subTest(link_case=link_case),
                tempfile.TemporaryDirectory() as tmp,
                tempfile.TemporaryDirectory() as outside_tmp,
            ):
                root = Path(tmp)
                run_dir = root / "automatic-video-run"
                project_dir = _automatic_video_project(run_dir)
                ctx = _context(run_dir)
                author = ExternalVideoAuthor(ctx.settings, "video system prompt")
                outside = Path(outside_tmp)
                real_open = os.open
                swapped = False

                def swap_delivery_link() -> None:
                    project = run_dir / "hyperframes-video"
                    if link_case == "project":
                        outside_project = outside / "hyperframes-video"
                        shutil.copytree(project, outside_project)
                        project.rename(project.with_name("hyperframes-video-before-swap"))
                        project.symlink_to(outside_project, target_is_directory=True)
                        return
                    member = project / (
                        "delivery_manifest.json"
                        if link_case == "manifest"
                        else "index.html"
                    )
                    outside_member = outside / member.name
                    outside_member.write_bytes(member.read_bytes())
                    member.unlink()
                    member.symlink_to(outside_member)

                def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                    nonlocal swapped
                    if (
                        not swapped
                        and dir_fd is not None
                        and os.fspath(path) == target_component
                    ):
                        swap_delivery_link()
                        swapped = True
                    return real_open(path, flags, mode, dir_fd=dir_fd)

                def deliver_normal_project(*, project_dir, manifest, ctx):
                    _install_passed_delivery(ctx)
                    return _passed_delivery_result(ctx)

                with (
                    patch(
                        "autodesign.agents.external_video_author."
                        "deliver_authored_video_project",
                        side_effect=deliver_normal_project,
                    ),
                    patch.object(
                        attempt_candidates_module.os,
                        "open",
                        side_effect=racing_open,
                    ),
                ):
                    delivery_result, finalize_result = author._deliver_normal_candidate(
                        candidate_id=f"automatic-{link_case}-swap",
                        project_dir=project_dir,
                        manifest=_authored_video_manifest(),
                        ctx=ctx,
                    )

                self.assertTrue(
                    swapped,
                    f"the production {link_case} secure-open boundary was not exercised",
                )
                self.assertEqual(delivery_result.status, "ok")
                self.assertIsNotNone(finalize_result)
                assert finalize_result is not None
                self.assertEqual(finalize_result.status, "error")
                reason = _video_blocker_reason(finalize_result)
                self.assertTrue(
                    any(
                        marker in reason
                        for marker in ("link", "no-follow", "nofollow")
                    ),
                    reason,
                )
                self.assertFalse(
                    (run_dir / "final" / "video_delivery.json").exists()
                )

    def test_finalize_rejects_external_final_directory_link_before_pointer_write(
        self,
    ) -> None:
        for link_case in ("directory", "destination"):
            with (
                self.subTest(link_case=link_case),
                tempfile.TemporaryDirectory() as tmp,
                tempfile.TemporaryDirectory() as outside_tmp,
            ):
                run_dir = Path(tmp) / "video-run"
                ctx = _context(run_dir)
                _install_passed_delivery(ctx)
                outside_final = Path(outside_tmp) / "final"
                outside_final.mkdir()
                outside_pointer = outside_final / "video_delivery.json"
                if link_case == "directory":
                    (run_dir / "final").symlink_to(
                        outside_final,
                        target_is_directory=True,
                    )
                else:
                    (run_dir / "final").mkdir()
                    outside_pointer.write_text("external\n", encoding="utf-8")
                    (run_dir / "final" / "video_delivery.json").symlink_to(
                        outside_pointer
                    )

                result = finalize({}, ctx=ctx)

                self.assertEqual(result.status, "error")
                reason = _video_blocker_reason(result)
                self.assertTrue(
                    any(
                        marker in reason
                        for marker in ("final", "link", "reparse", "no-follow")
                    ),
                    reason,
                )
                if link_case == "directory":
                    self.assertFalse(outside_pointer.exists())
                else:
                    self.assertEqual(
                        outside_pointer.read_text(encoding="utf-8"),
                        "external\n",
                    )
                self.assertFalse(ctx.state.get("finalized", False))

    @unittest.skipUnless(hasattr(os, "link"), "hard links are unavailable")
    def test_finalize_rejects_hard_linked_manifest_member(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as outside_tmp,
        ):
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)
            source_path = run_dir / "hyperframes-video" / "index.html"
            outside_path = Path(outside_tmp) / "index.html"
            outside_path.write_bytes(source_path.read_bytes())
            source_path.unlink()
            os.link(outside_path, source_path)

            result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "error")
            reason = _video_blocker_reason(result)
            self.assertTrue(
                any(marker in reason for marker in ("hard", "unsafe", "link", "nlink")),
                reason,
            )
            self.assertFalse((run_dir / "final" / "video_delivery.json").exists())

    @unittest.skipUnless(os.name == "posix", "stream replacement uses POSIX rename")
    def test_finalize_rejects_canonical_run_retargeted_during_streaming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "video-run"
            ctx = _context(run_dir)
            manifest_path, mp4_path = _install_passed_delivery(ctx)
            large_mp4 = b"video-stream" * 200_000
            mp4_path.write_bytes(large_mp4)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["mp4_sha256"] = sha256_file(mp4_path)
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            ctx.state["video_delivery"]["delivery_manifest_sha256"] = sha256_file(
                manifest_path
            )
            moved_run = root / "video-run-before-retarget"
            real_read = os.read
            swapped = False

            def racing_read(descriptor: int, count: int) -> bytes:
                nonlocal swapped
                payload = real_read(descriptor, count)
                if (
                    not swapped
                    and payload
                    and os.fstat(descriptor).st_size == len(large_mp4)
                ):
                    run_dir.rename(moved_run)
                    run_dir.mkdir()
                    swapped = True
                return payload

            result: ToolResultRecord | None = None
            with self.assertRaisesRegex(ValueError, "run directory changed"):
                with normal_promotion_lease(
                    run_dir=run_dir,
                    candidate_id="streaming-run-retarget",
                ) as leased_run_dir:
                    with leased_promotion_tool_context(ctx, leased_run_dir):
                        with patch.object(
                            attempt_candidates_module.os,
                            "read",
                            side_effect=racing_read,
                        ):
                            result = finalize({}, ctx=ctx)

            self.assertTrue(swapped, "the streaming accessor was not exercised")
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.status, "error")
            self.assertFalse((run_dir / "final" / "video_delivery.json").exists())
            self.assertFalse((moved_run / "final" / "video_delivery.json").exists())

    def test_finalize_rejects_windows_relative_anchor_from_delivery_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            manifest_path, _ = _install_passed_delivery(ctx)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source_html_path"] = r"C:outside\index.html"
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            ctx.state["video_delivery"]["delivery_manifest_sha256"] = sha256_file(
                manifest_path
            )

            result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "error")
            self.assertRegex(
                _video_blocker_reason(result),
                "windows.*anchor|anchor.*windows",
            )
            self.assertFalse((run_dir / "final" / "video_delivery.json").exists())
            self.assertFalse(ctx.state.get("finalized", False))

    @unittest.skipUnless(os.name == "posix", "writer checks use POSIX dir fds")
    def test_pointer_writer_final_failure_rolls_back_absent_and_sentinel(self) -> None:
        for prior_pointer in (None, b'{"sentinel":"prior"}\n'):
            with self.subTest(prior_pointer=prior_pointer is not None):
                with tempfile.TemporaryDirectory() as tmp:
                    run_dir = Path(tmp) / "video-run"
                    ctx = _context(run_dir)
                    _install_passed_delivery(ctx)
                    pointer = run_dir / "final" / "video_delivery.json"
                    if prior_pointer is not None:
                        pointer.parent.mkdir()
                        pointer.write_bytes(prior_pointer)
                    expected_before = prior_pointer
                    real_write_pointer = finalize_module._write_video_delivery_pointer
                    real_assert_root = (
                        attempt_candidates_module.SecureRunMemberAccessor
                        ._assert_root_unchanged
                    )
                    armed = False

                    def write_then_arm(*args, **kwargs):
                        nonlocal armed
                        real_write_pointer(*args, **kwargs)
                        armed = True

                    def fail_precommit_validation(accessor):
                        real_assert_root(accessor)
                        if armed:
                            raise ValueError("writer final validation failed")

                    with (
                        patch.object(
                            finalize_module,
                            "_write_video_delivery_pointer",
                            side_effect=write_then_arm,
                        ),
                        patch.object(
                            attempt_candidates_module.SecureRunMemberAccessor,
                            "_assert_root_unchanged",
                            autospec=True,
                            side_effect=fail_precommit_validation,
                        ),
                    ):
                        result = finalize({"notes": "must not commit"}, ctx=ctx)

                    self.assertTrue(armed, "writer final boundary was not reached")
                    self.assertEqual(result.status, "error")
                    self.assertIn(
                        "writer final validation failed",
                        _video_blocker_reason(result),
                    )
                    self.assertFalse(ctx.state.get("finalized", False))
                    self.assertNotIn("finalize_notes", ctx.state)
                    if expected_before is None:
                        self.assertFalse(pointer.exists())
                    else:
                        self.assertEqual(pointer.read_bytes(), expected_before)

    def test_pointer_context_exit_failure_rolls_back_absent_and_sentinel(self) -> None:
        for prior_pointer in (None, b'{"sentinel":"prior"}\n'):
            with self.subTest(prior_pointer=prior_pointer is not None):
                with tempfile.TemporaryDirectory() as tmp:
                    run_dir = Path(tmp) / "video-run"
                    ctx = _context(run_dir)
                    _install_passed_delivery(ctx)
                    pointer = run_dir / "final" / "video_delivery.json"
                    if prior_pointer is not None:
                        pointer.parent.mkdir()
                        pointer.write_bytes(prior_pointer)
                    expected_before = prior_pointer
                    real_write_pointer = finalize_module._write_video_delivery_pointer
                    real_assert_root = (
                        attempt_candidates_module.SecureRunMemberAccessor
                        ._assert_root_unchanged
                    )
                    fail_context_exit = False

                    def write_then_arm_failure(*args, **kwargs):
                        nonlocal fail_context_exit
                        real_write_pointer(*args, **kwargs)
                        fail_context_exit = True

                    def fail_only_after_writer(accessor):
                        real_assert_root(accessor)
                        if fail_context_exit:
                            raise ValueError("accessor context exit validation failed")

                    with (
                        patch.object(
                            finalize_module,
                            "_write_video_delivery_pointer",
                            side_effect=write_then_arm_failure,
                        ),
                        patch.object(
                            attempt_candidates_module.SecureRunMemberAccessor,
                            "_assert_root_unchanged",
                            autospec=True,
                            side_effect=fail_only_after_writer,
                        ),
                    ):
                        result = finalize({"notes": "must not commit"}, ctx=ctx)

                    self.assertTrue(fail_context_exit)
                    self.assertEqual(result.status, "error")
                    self.assertIn(
                        "accessor context exit validation failed",
                        _video_blocker_reason(result),
                    )
                    self.assertFalse(ctx.state.get("finalized", False))
                    self.assertNotIn("finalize_notes", ctx.state)
                    if expected_before is None:
                        self.assertFalse(pointer.exists())
                    else:
                        self.assertEqual(pointer.read_bytes(), expected_before)

    @unittest.skipUnless(os.name == "posix", "rollback race uses POSIX renameat")
    def test_pointer_rollback_preserves_foreign_bytes_raced_into_destination(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)
            pointer = run_dir / "final" / "video_delivery.json"
            foreign_bytes = b"foreign concurrent pointer\n"
            saved_original = pointer.parent / "attacker-saved-original"
            real_write_pointer = finalize_module._write_video_delivery_pointer
            real_assert_root = (
                attempt_candidates_module.SecureRunMemberAccessor
                ._assert_root_unchanged
            )
            real_atomic_move = (
                video_pointer_transaction_module._atomic_no_replace_rename
            )
            real_rename = os.rename
            armed = False
            raced = False

            def write_then_arm(*args, **kwargs):
                nonlocal armed
                real_write_pointer(*args, **kwargs)
                armed = True

            def fail_writer_final(accessor):
                real_assert_root(accessor)
                if armed:
                    raise ValueError("writer final validation failed")

            def race_rollback_rename(parent, source, destination):
                nonlocal raced
                source_name = os.fspath(source)
                destination_name = os.fspath(destination)
                if (
                    not raced
                    and armed
                    and source_name == pointer.name
                    and destination_name.endswith(".displaced")
                ):
                    assert isinstance(parent, int)
                    real_rename(
                        source_name,
                        saved_original.name,
                        src_dir_fd=parent,
                        dst_dir_fd=parent,
                    )
                    descriptor = os.open(
                        source_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=parent,
                    )
                    try:
                        os.write(descriptor, foreign_bytes)
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    raced = True
                return real_atomic_move(parent, source_name, destination_name)

            with (
                patch.object(
                    finalize_module,
                    "_write_video_delivery_pointer",
                    side_effect=write_then_arm,
                ),
                patch.object(
                    attempt_candidates_module.SecureRunMemberAccessor,
                    "_assert_root_unchanged",
                    autospec=True,
                    side_effect=fail_writer_final,
                ),
                patch.object(
                    video_pointer_transaction_module,
                    "_atomic_no_replace_rename",
                    side_effect=race_rollback_rename,
                ),
            ):
                result = finalize({}, ctx=ctx)

            self.assertTrue(armed)
            self.assertTrue(raced, "rollback quarantine boundary was not reached")
            self.assertEqual(result.status, "error")
            reason = _video_blocker_reason(result)
            self.assertIn("writer final validation failed", reason)
            survivors = []
            if pointer.is_file():
                survivors.append(pointer.read_bytes())
            survivors.extend(
                path.read_bytes()
                for path in pointer.parent.glob(f".{pointer.name}.*")
                if path.is_file()
            )
            self.assertIn(foreign_bytes, survivors)
            self.assertFalse(ctx.state.get("finalized", False))

    @unittest.skipUnless(os.name == "posix", "rollback race uses POSIX dir fds")
    def test_pointer_rollback_rechecks_quarantine_before_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)
            pointer = run_dir / "final" / "video_delivery.json"
            real_write_pointer = finalize_module._write_video_delivery_pointer
            real_assert_root = (
                attempt_candidates_module.SecureRunMemberAccessor
                ._assert_root_unchanged
            )
            real_unlink = os.unlink
            armed = False
            transaction_unlink_called = False

            def write_then_arm(*args, **kwargs):
                nonlocal armed
                real_write_pointer(*args, **kwargs)
                armed = True

            def fail_writer_final(accessor):
                real_assert_root(accessor)
                if armed:
                    raise ValueError("writer final validation failed")

            def forbid_transaction_unlink(path, *args, **kwargs):
                nonlocal transaction_unlink_called
                name = os.fspath(path)
                if name == pointer.name or name.startswith(f".{pointer.name}."):
                    transaction_unlink_called = True
                    raise AssertionError("transaction entries are append-only")
                return real_unlink(path, *args, **kwargs)

            with (
                patch.object(
                    finalize_module,
                    "_write_video_delivery_pointer",
                    side_effect=write_then_arm,
                ),
                patch.object(
                    attempt_candidates_module.SecureRunMemberAccessor,
                    "_assert_root_unchanged",
                    autospec=True,
                    side_effect=fail_writer_final,
                ),
                patch.object(
                    attempt_candidates_module.os,
                    "unlink",
                    side_effect=forbid_transaction_unlink,
                ),
            ):
                result = finalize({}, ctx=ctx)

            self.assertTrue(armed)
            self.assertEqual(result.status, "error")
            self.assertFalse(transaction_unlink_called)
            self.assertFalse(pointer.exists())
            self.assertTrue(
                any(path.name.endswith(".displaced") for path in pointer.parent.iterdir())
            )
            self.assertFalse(ctx.state.get("finalized", False))

    @unittest.skipUnless(os.name == "posix", "rollback errors use POSIX renameat")
    def test_pointer_rollback_failure_surfaces_original_and_rollback_errors(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)
            pointer = run_dir / "final" / "video_delivery.json"
            real_write_pointer = finalize_module._write_video_delivery_pointer
            real_assert_root = (
                attempt_candidates_module.SecureRunMemberAccessor
                ._assert_root_unchanged
            )
            real_atomic_move = (
                video_pointer_transaction_module._atomic_no_replace_rename
            )
            armed = False
            rollback_failed = False

            def write_then_arm(*args, **kwargs):
                nonlocal armed
                real_write_pointer(*args, **kwargs)
                armed = True

            def fail_writer_final(accessor):
                real_assert_root(accessor)
                if armed:
                    raise ValueError("writer final validation failed")

            def fail_rollback_rename(parent, source, destination):
                nonlocal rollback_failed
                if (
                    armed
                    and
                    os.fspath(source) == pointer.name
                    and os.fspath(destination).endswith(".displaced")
                ):
                    rollback_failed = True
                    raise OSError("rollback quarantine failed")
                return real_atomic_move(parent, source, destination)

            with (
                patch.object(
                    finalize_module,
                    "_write_video_delivery_pointer",
                    side_effect=write_then_arm,
                ),
                patch.object(
                    attempt_candidates_module.SecureRunMemberAccessor,
                    "_assert_root_unchanged",
                    autospec=True,
                    side_effect=fail_writer_final,
                ),
                patch.object(
                    video_pointer_transaction_module,
                    "_atomic_no_replace_rename",
                    side_effect=fail_rollback_rename,
                ),
            ):
                result = finalize({}, ctx=ctx)

            self.assertTrue(armed)
            self.assertTrue(rollback_failed)
            self.assertEqual(result.status, "error")
            reason = _video_blocker_reason(result)
            self.assertIn("writer final validation failed", reason)
            self.assertIn("rollback quarantine failed", reason)
            self.assertFalse(ctx.state.get("finalized", False))

    @unittest.skipUnless(os.name == "posix", "durability order uses directory fsync")
    def test_pointer_publication_and_rollback_follow_posix_durability_order(
        self,
    ) -> None:
        for prior_pointer in (None, b'{"sentinel":"prior"}\n'):
            with self.subTest(prior_pointer=prior_pointer is not None):
                with tempfile.TemporaryDirectory() as tmp:
                    run_dir = Path(tmp) / "video-run"
                    ctx = _context(run_dir)
                    _install_passed_delivery(ctx)
                    pointer = run_dir / "final" / "video_delivery.json"
                    if prior_pointer is not None:
                        pointer.parent.mkdir()
                        pointer.write_bytes(prior_pointer)
                    expected_before = prior_pointer
                    events: list[str] = []
                    real_fsync = os.fsync
                    real_atomic_move = (
                        video_pointer_transaction_module._atomic_no_replace_rename
                    )
                    real_write_pointer = finalize_module._write_video_delivery_pointer
                    real_assert_root = (
                        attempt_candidates_module.SecureRunMemberAccessor
                        ._assert_root_unchanged
                    )
                    armed = False

                    def record_fsync(descriptor):
                        kind = (
                            "file_fsync"
                            if stat.S_ISREG(os.fstat(descriptor).st_mode)
                            else "parent_fsync"
                        )
                        events.append(kind)
                        return real_fsync(descriptor)

                    def record_move(parent, source, destination):
                        events.append(
                            f"move:{os.fspath(source)}->{os.fspath(destination)}"
                        )
                        return real_atomic_move(parent, source, destination)

                    def write_then_arm(*args, **kwargs):
                        nonlocal armed
                        real_write_pointer(*args, **kwargs)
                        armed = True

                    def fail_writer_final(accessor):
                        real_assert_root(accessor)
                        if armed:
                            raise ValueError("writer final validation failed")

                    with (
                        patch.object(
                            finalize_module,
                            "_write_video_delivery_pointer",
                            side_effect=write_then_arm,
                        ),
                        patch.object(
                            attempt_candidates_module.SecureRunMemberAccessor,
                            "_assert_root_unchanged",
                            autospec=True,
                            side_effect=fail_writer_final,
                        ),
                        patch.object(
                            video_pointer_transaction_module.os,
                            "fsync",
                            side_effect=record_fsync,
                        ),
                        patch.object(
                            video_pointer_transaction_module,
                            "_atomic_no_replace_rename",
                            side_effect=record_move,
                        ),
                    ):
                        result = finalize({}, ctx=ctx)

                    self.assertEqual(result.status, "error")
                    publish_event = next(
                        event
                        for event in events
                        if event.endswith(f"->{pointer.name}")
                        and ".new->" in event
                    )
                    publish_index = events.index(publish_event)
                    self.assertIn("file_fsync", events[:publish_index])
                    forward_fsync = events.index("parent_fsync", publish_index + 1)
                    displace_event = next(
                        event
                        for event in events[forward_fsync + 1 :]
                        if event.startswith(f"move:{pointer.name}->")
                        and event.endswith(".displaced")
                    )
                    displace_index = events.index(displace_event)
                    rollback_fsync = events.index("parent_fsync", displace_index + 1)
                    if expected_before is not None:
                        restore_event = next(
                            event
                            for event in events[rollback_fsync + 1 :]
                            if event.endswith(f"->{pointer.name}")
                            and ".prior->" in event
                        )
                        restore_index = events.index(restore_event)
                        events.index("parent_fsync", restore_index + 1)

    def test_mocked_windows_pointer_publication_uses_movefileex_write_through(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)
            native = _FakeWindowsRetainedHandleAPI()
            portable_os = SimpleNamespace(name="nt", path=os.path, fspath=os.fspath)
            with (
                patch.object(attempt_candidates_module, "_RUNTIME_OS", portable_os),
                patch.object(
                    video_pointer_transaction_module,
                    "_RUNTIME_OS",
                    portable_os,
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_directory_replacement_guard",
                    side_effect=lambda _path: nullcontext(),
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_current_user_sid",
                    return_value="S-1-5-21-step2c-test",
                ),
                patch.object(
                    video_pointer_transaction_module,
                    "_windows_native_handle_api_factory",
                    return_value=native,
                ),
                patch.object(
                    video_pointer_transaction_module,
                    "_windows_pointer_security_attributes_factory",
                    return_value=(object(), lambda: None),
                ),
            ):
                result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "ok", result.error_message)
            self.assertGreaterEqual(len(native.move_calls), 2)
            self.assertTrue(
                all(source.parent == destination.parent for source, destination, _ in native.move_calls)
            )
            self.assertTrue(all(flags == 0x8 for _, _, flags in native.move_calls))
            self.assertTrue(ctx.state["finalized"])

    def test_mocked_windows_pointer_rollback_uses_write_through_no_replace(
        self,
    ) -> None:
        for prior_pointer in (None, b'{"sentinel":"prior"}\n'):
            with self.subTest(prior_pointer=prior_pointer is not None):
                with tempfile.TemporaryDirectory() as tmp:
                    run_dir = Path(tmp) / "video-run"
                    ctx = _context(run_dir)
                    _install_passed_delivery(ctx)
                    pointer = run_dir / "final" / "video_delivery.json"
                    if prior_pointer is not None:
                        pointer.parent.mkdir()
                        pointer.write_bytes(prior_pointer)
                    native = _FakeWindowsRetainedHandleAPI()
                    arm_context_failure = False
                    real_write_pointer = finalize_module._write_video_delivery_pointer
                    real_assert_root = (
                        attempt_candidates_module.SecureRunMemberAccessor
                        ._assert_root_unchanged
                    )

                    def write_then_arm(*args, **kwargs):
                        nonlocal arm_context_failure
                        real_write_pointer(*args, **kwargs)
                        arm_context_failure = True

                    def fail_context_exit(accessor):
                        real_assert_root(accessor)
                        if arm_context_failure:
                            raise ValueError("accessor context exit validation failed")

                    portable_os = SimpleNamespace(
                        name="nt",
                        path=os.path,
                        fspath=os.fspath,
                    )
                    with (
                        patch.object(
                            attempt_candidates_module,
                            "_RUNTIME_OS",
                            portable_os,
                        ),
                        patch.object(
                            video_pointer_transaction_module,
                            "_RUNTIME_OS",
                            portable_os,
                        ),
                        patch.object(
                            attempt_candidates_module,
                            "_windows_directory_replacement_guard",
                            side_effect=lambda _path: nullcontext(),
                        ),
                        patch.object(
                            attempt_candidates_module,
                            "_windows_current_user_sid",
                            return_value="S-1-5-21-step2c-test",
                        ),
                        patch.object(
                            video_pointer_transaction_module,
                            "_windows_native_handle_api_factory",
                            return_value=native,
                        ),
                        patch.object(
                            video_pointer_transaction_module,
                            "_windows_pointer_security_attributes_factory",
                            return_value=(object(), lambda: None),
                        ),
                        patch.object(
                            finalize_module,
                            "_write_video_delivery_pointer",
                            side_effect=write_then_arm,
                        ),
                        patch.object(
                            attempt_candidates_module.SecureRunMemberAccessor,
                            "_assert_root_unchanged",
                            autospec=True,
                            side_effect=fail_context_exit,
                        ),
                    ):
                        result = finalize({}, ctx=ctx)

                    self.assertEqual(result.status, "error")
                    self.assertGreaterEqual(len(native.move_calls), 2)
                    self.assertTrue(
                        all(flags == 0x8 for _, _, flags in native.move_calls)
                    )
                    if prior_pointer is None:
                        self.assertFalse(pointer.exists())
                    else:
                        self.assertEqual(pointer.read_bytes(), prior_pointer)
                    self.assertFalse(ctx.state.get("finalized", False))

    def test_mocked_windows_pointer_move_failures_fail_closed(self) -> None:
        cases = ("unavailable", "move-error")
        for failure_case in cases:
            with self.subTest(failure_case=failure_case):
                with tempfile.TemporaryDirectory() as tmp:
                    run_dir = Path(tmp) / "video-run"
                    ctx = _context(run_dir)
                    _install_passed_delivery(ctx)
                    pointer = run_dir / "final" / "video_delivery.json"
                    pointer.parent.mkdir()
                    sentinel = b'{"sentinel":"prior"}\n'
                    pointer.write_bytes(sentinel)
                    portable_os = SimpleNamespace(
                        name="nt",
                        path=os.path,
                        fspath=os.fspath,
                    )
                    if failure_case == "unavailable":
                        api_patch = patch.object(
                            video_pointer_transaction_module,
                            "_windows_native_handle_api_factory",
                            side_effect=RuntimeError(
                                "Windows retained HANDLE API unavailable"
                            ),
                        )
                    else:
                        native = _FakeWindowsRetainedHandleAPI(move_error=5)
                        api_patch = patch.object(
                            video_pointer_transaction_module,
                            "_windows_native_handle_api_factory",
                            return_value=native,
                        )
                    with (
                        patch.object(
                            attempt_candidates_module,
                            "_RUNTIME_OS",
                            portable_os,
                        ),
                        patch.object(
                            video_pointer_transaction_module,
                            "_RUNTIME_OS",
                            portable_os,
                        ),
                        patch.object(
                            attempt_candidates_module,
                            "_windows_directory_replacement_guard",
                            side_effect=lambda _path: nullcontext(),
                        ),
                        patch.object(
                            attempt_candidates_module,
                            "_windows_current_user_sid",
                            return_value="S-1-5-21-step2c-test",
                        ),
                        patch.object(
                            video_pointer_transaction_module,
                            "_windows_pointer_security_attributes_factory",
                            return_value=(object(), lambda: None),
                        ),
                        api_patch,
                    ):
                        result = finalize({}, ctx=ctx)

                    self.assertEqual(result.status, "error")
                    self.assertIn(
                        "windows retained handle api"
                        if failure_case == "unavailable"
                        else "movefileex",
                        _video_blocker_reason(result),
                    )
                    self.assertEqual(pointer.read_bytes(), sentinel)
                    self.assertFalse(ctx.state.get("finalized", False))

    @unittest.skipUnless(os.name == "nt", "requires the real Windows API")
    def test_real_windows_pointer_publication_is_durable_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)

            result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "ok", result.error_message)
            self.assertTrue((run_dir / "final" / "video_delivery.json").is_file())
            self.assertTrue(ctx.state["finalized"])

    def test_direct_finalize_uses_anchored_accessor_and_retained_digests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)

            with (
                patch.object(
                    finalize_module,
                    "_video_delivery_state_paths",
                    side_effect=AssertionError("legacy checked Path reopened"),
                    create=True,
                ),
                patch.object(
                    finalize_module,
                    "sha256_file",
                    side_effect=AssertionError("ordinary Path hash reopened"),
                ),
            ):
                result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "ok", result.error_message)
            pointer = json.loads(
                (run_dir / "final" / "video_delivery.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                pointer["manifest_path"],
                "hyperframes-video/delivery_manifest.json",
            )
            self.assertEqual(
                pointer["manifest_sha256"],
                ctx.state["video_delivery"]["delivery_manifest_sha256"],
            )

    def test_direct_finalize_fails_closed_without_secure_member_primitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)

            with patch.object(
                attempt_candidates_module,
                "_SECURE_DIR_FD_AVAILABLE",
                False,
            ):
                result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "error")
            reason = _video_blocker_reason(result)
            self.assertTrue(
                any(marker in reason for marker in ("secure", "unavailable", "primitive")),
                reason,
            )
            self.assertFalse((run_dir / "final" / "video_delivery.json").exists())

    def test_finalize_requires_current_passed_delivery_manifest_probe_and_exact_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            _install_passed_delivery(ctx)

            result = finalize({"notes": "video complete"}, ctx=ctx)

            self.assertEqual(result.status, "ok")
            self.assertTrue(ctx.state["finalized"])
            self.assertTrue((ctx.run_dir / "final" / "video_delivery.json").is_file())

    def test_finalize_rejects_stale_or_changed_video_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            _, mp4_path = _install_passed_delivery(ctx)
            mp4_path.write_bytes(b"older or replaced mp4")

            result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "error")
            self.assertIn("current passed video delivery", result.error_message or "")
            self.assertFalse(ctx.state.get("finalized", False))

    def test_finalize_rejects_failed_delivery_state_even_when_old_files_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            _install_passed_delivery(ctx)
            ctx.state["video_delivery"]["status"] = "failed"

            result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "error")
            self.assertIn("current passed video delivery", result.error_message or "")

    def test_finalize_rejects_delivery_for_an_older_design_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            _install_passed_delivery(ctx)
            ctx.state["design_spec"] = SimpleNamespace(
                artifact_type="video",
                model_dump=lambda mode=None: {"artifact_type": "video", "revision": 2},
            )

            result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "error")
            blocker = result.payload["video_delivery_final_blocker"]
            self.assertIn("current DesignSpec", blocker["reason"])

    def test_finalize_rejects_delivery_for_an_older_design_spec_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            manifest_path, _ = _install_passed_delivery(ctx)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["design_spec_revision"] = 0
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            ctx.state["video_delivery"]["delivery_manifest_sha256"] = sha256_file(
                manifest_path
            )

            result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "error")
            blocker = result.payload["video_delivery_final_blocker"]
            self.assertIn("DesignSpec revision", blocker["reason"])

    def test_finalize_rejects_replaced_subtitle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            manifest_path, _ = _install_passed_delivery(ctx)
            (manifest_path.parent / "narration" / "subtitles.en.srt").write_text(
                "changed\n",
                encoding="utf-8",
            )

            result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "error")
            blocker = result.payload["video_delivery_final_blocker"]
            self.assertIn("srt_sha256", blocker["reason"])

    def test_finalize_rejects_delivery_without_selectable_subtitles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            manifest_path, _ = _install_passed_delivery(ctx)
            probe_path = manifest_path.parent / "media_probe.json"
            probe = json.loads(probe_path.read_text(encoding="utf-8"))
            probe["subtitle_codec"] = None
            probe_path.write_text(json.dumps(probe) + "\n", encoding="utf-8")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["media_probe"] = probe
            manifest["media_probe_sha256"] = sha256_file(probe_path)
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            ctx.state["video_delivery"]["delivery_manifest_sha256"] = sha256_file(
                manifest_path
            )

            result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "error")
            blocker = result.payload["video_delivery_final_blocker"]
            self.assertIn("selectable non-forced", blocker["reason"])

    def test_finalize_rejects_delivery_manifest_changed_after_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            manifest_path, _ = _install_passed_delivery(ctx)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["unexpected_mutation"] = True
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

            result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "error")
            blocker = result.payload["video_delivery_final_blocker"]
            self.assertIn("delivery manifest", blocker["reason"])

    def test_finalize_rejects_delivery_below_formal_speech_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            manifest_path, _ = _install_passed_delivery(ctx)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["speech_duration_s"] = 60.0
            manifest["speech_coverage_ratio"] = 1 / 6
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            ctx.state["video_delivery"]["delivery_manifest_sha256"] = sha256_file(
                manifest_path
            )

            result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "error")
            blocker = result.payload["video_delivery_final_blocker"]
            self.assertIn("speech coverage", blocker["reason"])

    def test_finalize_rejects_malformed_measured_speech_timing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            manifest_path, _ = _install_passed_delivery(ctx)
            timing_path = manifest_path.parent / "narration" / "timing.json"
            timing = json.loads(timing_path.read_text(encoding="utf-8"))
            timing.append("not-a-timing-record")
            timing_path.write_text(json.dumps(timing) + "\n", encoding="utf-8")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["narration_timing_sha256"] = sha256_file(timing_path)
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            ctx.state["video_delivery"]["delivery_manifest_sha256"] = sha256_file(
                manifest_path
            )

            result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "error")
            blocker = result.payload["video_delivery_final_blocker"]
            self.assertIn("timing record", blocker["reason"])

    def test_finalize_rejects_probe_duration_drift_from_authored_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            manifest_path, _ = _install_passed_delivery(ctx)
            probe_path = manifest_path.parent / "media_probe.json"
            probe = json.loads(probe_path.read_text(encoding="utf-8"))
            probe["duration_s"] = 361.0
            probe_path.write_text(json.dumps(probe) + "\n", encoding="utf-8")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["media_probe"] = probe
            manifest["media_probe_sha256"] = sha256_file(probe_path)
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            ctx.state["video_delivery"]["delivery_manifest_sha256"] = sha256_file(
                manifest_path
            )

            result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "error")
            blocker = result.payload["video_delivery_final_blocker"]
            self.assertIn("authored timeline", blocker["reason"])

    def test_finalize_rejects_delivery_drift_from_selected_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            manifest_path, _ = _install_passed_delivery(ctx)
            contract_path = manifest_path.parent / "video_delivery_contract.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["target_duration_s"] = 300
            contract_path.write_text(json.dumps(contract) + "\n", encoding="utf-8")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["contract_sha256"] = sha256_file(contract_path)
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            ctx.state["video_delivery"]["delivery_manifest_sha256"] = sha256_file(
                manifest_path
            )

            result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "error")
            blocker = result.payload["video_delivery_final_blocker"]
            self.assertIn("selected target", blocker["reason"])

    def test_finalize_recomputes_coverage_against_probe_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            manifest_path, _ = _install_passed_delivery(ctx)
            project = manifest_path.parent
            probe_path = project / "media_probe.json"
            timing_path = project / "narration" / "timing.json"
            probe = json.loads(probe_path.read_text(encoding="utf-8"))
            probe["duration_s"] = 360.5
            probe_path.write_text(json.dumps(probe) + "\n", encoding="utf-8")
            timing = json.loads(timing_path.read_text(encoding="utf-8"))
            for item in timing:
                item["speech_duration_s"] = 21.6
                item["end_s"] = item["start_s"] + 21.6
            timing_path.write_text(json.dumps(timing) + "\n", encoding="utf-8")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update({
                "media_probe": probe,
                "media_probe_sha256": sha256_file(probe_path),
                "narration_timing_sha256": sha256_file(timing_path),
                "speech_duration_s": 259.2,
                "speech_coverage_ratio": 259.2 / 360.0,
            })
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            ctx.state["video_delivery"]["delivery_manifest_sha256"] = sha256_file(
                manifest_path
            )

            result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "error")
            blocker = result.payload["video_delivery_final_blocker"]
            self.assertIn("speech coverage", blocker["reason"])

    def test_finalize_rejects_timing_scene_ids_out_of_contract_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            manifest_path, _ = _install_passed_delivery(ctx)
            timing_path = manifest_path.parent / "narration" / "timing.json"
            timing = json.loads(timing_path.read_text(encoding="utf-8"))
            timing[0], timing[1] = timing[1], timing[0]
            timing_path.write_text(json.dumps(timing) + "\n", encoding="utf-8")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["narration_timing_sha256"] = sha256_file(timing_path)
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            ctx.state["video_delivery"]["delivery_manifest_sha256"] = sha256_file(
                manifest_path
            )

            result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "error")
            blocker = result.payload["video_delivery_final_blocker"]
            self.assertIn("scene ids and order", blocker["reason"])

    def test_finalize_rejects_non_finite_or_inconsistent_timing_boundaries(self) -> None:
        for mutation in ("non_finite", "inconsistent"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                ctx = _context(Path(tmp))
                manifest_path, _ = _install_passed_delivery(ctx)
                timing_path = manifest_path.parent / "narration" / "timing.json"
                timing = json.loads(timing_path.read_text(encoding="utf-8"))
                if mutation == "non_finite":
                    timing[0]["start_s"] = float("nan")
                else:
                    timing[0]["end_s"] += 1.0
                timing_path.write_text(json.dumps(timing) + "\n", encoding="utf-8")
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["narration_timing_sha256"] = sha256_file(timing_path)
                manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
                ctx.state["video_delivery"]["delivery_manifest_sha256"] = sha256_file(
                    manifest_path
                )

                result = finalize({}, ctx=ctx)

                self.assertEqual(result.status, "error")
                blocker = result.payload["video_delivery_final_blocker"]
                self.assertIn("timing boundaries", blocker["reason"])


class Task4BVideoSnapshotRedTest(unittest.TestCase):
    _EXPECTED_FINALIZE_GRAPH = frozenset({
        "design_spec.json",
        "hyperframes-video/delivery_manifest.json",
        "hyperframes-video/index.html",
        "hyperframes-video/video_delivery_contract.json",
        "hyperframes-video/media_probe.json",
        "hyperframes-video/renders/attempt.mp4",
        "hyperframes-video/assets/narration.wav",
        "hyperframes-video/assets/local.png",
        "hyperframes-video/narration/transcript.en.txt",
        "hyperframes-video/narration/subtitles.en.srt",
        "hyperframes-video/narration/subtitles.en.vtt",
        "hyperframes-video/narration/voice.json",
        "hyperframes-video/narration/timing.json",
    })
    _EXPECTED_CAPTURED = frozenset({
        "design_spec.json",
        "hyperframes-video/delivery_manifest.json",
        "hyperframes-video/video_delivery_contract.json",
        "hyperframes-video/media_probe.json",
        "hyperframes-video/narration/timing.json",
    })

    @staticmethod
    def _phase_tokens(final_dir: Path) -> set[str]:
        tokens: set[str] = set()
        if not final_dir.is_dir():
            return tokens
        pattern = re.compile(
            r"^\.video_delivery\.json\.[0-9a-f]{32}\."
            r"phase-[0-9]{6}-([a-z][a-z0-9-]{0,47})\.json$"
        )
        for path in final_dir.iterdir():
            match = pattern.fullmatch(path.name)
            if match is not None:
                tokens.add(match.group(1))
        return tokens

    def test_task4b_finalize_freezes_exact_graph_and_capture_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            ctx = _context(run_dir)
            manifest_path, _ = _install_passed_delivery(ctx)
            calls: list[tuple[object, ...]] = []

            def record_snapshots(_accessor, snapshots):
                calls.append(tuple(snapshots))

            with patch.object(
                attempt_candidates_module.SecureRunMemberAccessor,
                "assert_snapshots_unchanged",
                new=record_snapshots,
                create=True,
            ):
                result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "ok", result.error_message)
            pointer = json.loads(
                (run_dir / "final" / "video_delivery.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                {
                    "manifest_path": pointer.get("manifest_path"),
                    "manifest_sha256": pointer.get("manifest_sha256"),
                    "design_spec_sha256": pointer.get("design_spec_sha256"),
                    "design_spec_revision": pointer.get("design_spec_revision"),
                },
                {
                    "manifest_path": "hyperframes-video/delivery_manifest.json",
                    "manifest_sha256": sha256_file(manifest_path),
                    "design_spec_sha256": design_spec_sha256(
                        ctx.state["design_spec"]
                    ),
                    "design_spec_revision": 1,
                },
            )
            self.assertEqual(
                len(calls),
                2,
                "frozen graph must be rechecked before tentative publication and commit",
            )
            for call_index, snapshots in enumerate(calls):
                with self.subTest(call_index=call_index):
                    paths = tuple(
                        snapshot.relative_path.as_posix()
                        for snapshot in snapshots
                    )
                    self.assertEqual(len(paths), len(set(paths)))
                    self.assertEqual(set(paths), self._EXPECTED_FINALIZE_GRAPH)
                    self.assertEqual(
                        {
                            snapshot.relative_path.as_posix()
                            for snapshot in snapshots
                            if snapshot.data is not None
                        },
                        self._EXPECTED_CAPTURED,
                    )

    def test_task4b_finalize_rejects_inconsistent_persisted_design_spec(
        self,
    ) -> None:
        for case in ("embedded_fingerprint", "revision"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                run_dir = Path(tmp)
                ctx = _context(run_dir)
                _install_passed_delivery(ctx)
                spec_path = run_dir / "design_spec.json"
                persisted = json.loads(spec_path.read_text(encoding="utf-8"))
                if case == "embedded_fingerprint":
                    persisted["design_spec_sha256"] = "0" * 64
                else:
                    persisted["revision"] = 99
                spec_path.write_text(
                    json.dumps(persisted) + "\n",
                    encoding="utf-8",
                )

                result = finalize({}, ctx=ctx)

                self.assertEqual(
                    {
                        "status": result.status,
                        "finalized": bool(ctx.state.get("finalized", False)),
                        "pointer_absent": not (
                            run_dir / "final" / "video_delivery.json"
                        ).exists(),
                    },
                    {
                        "status": "error",
                        "finalized": False,
                        "pointer_absent": True,
                    },
                )

    def test_task4b_finalize_uses_frozen_pointer_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            ctx = _context(run_dir)
            manifest_path, _ = _install_passed_delivery(ctx)
            expected_spec_hash = design_spec_sha256(ctx.state["design_spec"])
            real_write = finalize_module._write_video_delivery_pointer
            state_mutated = False

            def mutate_state_then_write(write_ctx, *args, **kwargs):
                nonlocal state_mutated
                write_ctx.state["video_delivery"]["design_spec_sha256"] = "f" * 64
                write_ctx.state["video_delivery"]["design_spec_revision"] = 99
                state_mutated = True
                return real_write(write_ctx, *args, **kwargs)

            with patch.object(
                finalize_module,
                "_write_video_delivery_pointer",
                side_effect=mutate_state_then_write,
            ):
                result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "ok", result.error_message)
            pointer = json.loads(
                (run_dir / "final" / "video_delivery.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                {
                    "state_mutated": state_mutated,
                    "manifest_path": pointer.get("manifest_path"),
                    "manifest_sha256": pointer.get("manifest_sha256"),
                    "design_spec_sha256": pointer.get("design_spec_sha256"),
                    "design_spec_revision": pointer.get("design_spec_revision"),
                },
                {
                    "state_mutated": True,
                    "manifest_path": "hyperframes-video/delivery_manifest.json",
                    "manifest_sha256": sha256_file(manifest_path),
                    "design_spec_sha256": expected_spec_hash,
                    "design_spec_revision": 1,
                },
            )

    def test_task4b_finalize_rejects_graph_mutation_before_real_stage(self) -> None:
        cases = {
            "design_spec": Path("design_spec.json"),
            "manifest": Path("hyperframes-video/delivery_manifest.json"),
            "mp4": Path("hyperframes-video/renders/attempt.mp4"),
            "srt": Path("hyperframes-video/narration/subtitles.en.srt"),
            "contract": Path("hyperframes-video/video_delivery_contract.json"),
            "timing": Path("hyperframes-video/narration/timing.json"),
            "local_asset": Path("hyperframes-video/assets/local.png"),
        }
        real_stage = (
            attempt_candidates_module.SecureRunMemberAccessor
            .stage_video_delivery_pointer
        )
        for case, relative_path in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                run_dir = Path(tmp)
                ctx = _context(run_dir)
                _install_passed_delivery(ctx)
                target = run_dir / relative_path
                mutated = False

                def mutate_then_stage(accessor, *args, **kwargs):
                    nonlocal mutated
                    target.write_bytes(target.read_bytes() + b"\nTask4B mutation")
                    mutated = True
                    return real_stage(accessor, *args, **kwargs)

                with patch.object(
                    attempt_candidates_module.SecureRunMemberAccessor,
                    "stage_video_delivery_pointer",
                    new=mutate_then_stage,
                ):
                    result = finalize({}, ctx=ctx)

                self.assertEqual(
                    {
                        "mutated": mutated,
                        "status": result.status,
                        "finalized": bool(ctx.state.get("finalized", False)),
                        "pointer_absent": not (
                            run_dir / "final" / "video_delivery.json"
                        ).exists(),
                    },
                    {
                        "mutated": True,
                        "status": "error",
                        "finalized": False,
                        "pointer_absent": True,
                    },
                )

    def test_task4b_finalize_precondition_runs_after_prepublication_hooks(self) -> None:
        for mutation_phase in ("prepared", "publish-intent"):
            with (
                self.subTest(mutation_phase=mutation_phase),
                tempfile.TemporaryDirectory() as tmp,
            ):
                run_dir = Path(tmp)
                ctx = _context(run_dir)
                _, mp4_path = _install_passed_delivery(ctx)
                pointer_path = run_dir / "final" / "video_delivery.json"
                pointer_path.parent.mkdir(parents=True)
                sentinel = b'{"sentinel":"Task4B prior"}\n'
                pointer_path.write_bytes(sentinel)
                observed_phases: list[str] = []
                mutated = False

                def mutate_at_phase(phase, **_details):
                    nonlocal mutated
                    token = str(getattr(phase, "value", phase)).lower().replace(
                        "_", "-"
                    )
                    observed_phases.append(token)
                    if token == mutation_phase and not mutated:
                        mp4_path.write_bytes(mp4_path.read_bytes() + b" mutated")
                        mutated = True

                with patch.object(
                    video_pointer_transaction_module,
                    "_video_pointer_transaction_phase_hook",
                    side_effect=mutate_at_phase,
                ):
                    result = finalize({}, ctx=ctx)

                durable_phases = self._phase_tokens(pointer_path.parent)
                all_phases = set(observed_phases) | durable_phases
                self.assertEqual(
                    {
                        "mutated": mutated,
                        "status": result.status,
                        "finalized": bool(ctx.state.get("finalized", False)),
                        "prior_restored": pointer_path.read_bytes() == sentinel,
                        "publish_intent_seen": "publish-intent" in all_phases,
                        "published_absent": "published" not in all_phases,
                        "committed_absent": "committed" not in all_phases,
                        "aborted_seen": "aborted" in all_phases,
                    },
                    {
                        "mutated": True,
                        "status": "error",
                        "finalized": False,
                        "prior_restored": True,
                        "publish_intent_seen": True,
                        "published_absent": True,
                        "committed_absent": True,
                        "aborted_seen": True,
                    },
                )

    def test_task4b_snapshot_assertion_rejects_replacement_and_conflicts(self) -> None:
        for platform in ("posix", "mocked_windows_path_branch"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                operation = getattr(
                    attempt_candidates_module.SecureRunMemberAccessor,
                    "assert_snapshots_unchanged",
                    None,
                )
                self.assertTrue(
                    callable(operation),
                    "SecureRunMemberAccessor.assert_snapshots_unchanged is required",
                )
                run_dir = Path(tmp)
                member = run_dir / "member.bin"
                member.write_bytes(b"same bytes")
                portable_os = SimpleNamespace(
                    name="nt", path=os.path, fspath=os.fspath
                )
                with ExitStack() as stack:
                    if platform == "mocked_windows_path_branch":
                        stack.enter_context(
                            patch.object(
                                attempt_candidates_module,
                                "_RUNTIME_OS",
                                portable_os,
                            )
                        )
                        stack.enter_context(
                            patch.object(
                                attempt_candidates_module,
                                "_windows_directory_replacement_guard",
                                side_effect=lambda _path: nullcontext(),
                            )
                        )
                    with attempt_candidates_module.secure_run_member_access(
                        run_dir
                    ) as accessor:
                        snapshot = accessor.read_bytes(
                            Path("member.bin"), label="Task4B member"
                        )
                        operation(accessor, (snapshot, snapshot))
                        inconsistent = replace(
                            snapshot,
                            sha256="0" * 64,
                        )
                        with self.assertRaises(ValueError):
                            operation(accessor, (snapshot, inconsistent))
                        replacement = run_dir / "replacement.bin"
                        replacement.write_bytes(snapshot.data or b"")
                        os.replace(replacement, member)
                        with self.assertRaises(ValueError):
                            operation(accessor, (snapshot,))


class Step2cVideoPublicationRedTest(unittest.TestCase):
    _PHASE_NAME = re.compile(
        r"^\.video_delivery\.json\.([0-9a-f]{32})\."
        r"phase-([0-9]{6})-([a-z][a-z0-9-]{0,47})\.json$"
    )
    _CONFLICT_NAME = re.compile(
        r"^\.video_delivery\.json\.[0-9a-f]{32}\."
        r"conflict-[0-9]{4}-[0-9a-f]+$"
    )

    @classmethod
    def _phase_tokens(cls, final_dir: Path, txid: str | None = None) -> list[str]:
        tokens: list[str] = []
        for path in final_dir.iterdir():
            match = cls._PHASE_NAME.fullmatch(path.name)
            if match and (txid is None or match.group(1) == txid):
                tokens.append(match.group(3))
        return tokens

    @staticmethod
    def _retained_bytes(final_dir: Path) -> list[bytes]:
        return [path.read_bytes() for path in final_dir.iterdir() if path.is_file()]

    @staticmethod
    def _reopen_delivery_context(run_dir: Path) -> ToolContext:
        ctx = _context(run_dir)
        project = run_dir / "hyperframes-video"
        manifest_path = project / "delivery_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        mp4_path = project / str(manifest["mp4_path"])
        probe_path = project / str(manifest["media_probe_path"])
        ctx.state["video_delivery"] = {
            "status": "passed",
            "project_dir": str(project),
            "manifest_path": str(manifest_path),
            "media_probe_path": str(probe_path),
            "mp4_path": str(mp4_path),
            "render_started_at": manifest["render_started_at"],
            "design_spec_sha256": manifest["design_spec_sha256"],
            "design_spec_revision": manifest["design_spec_revision"],
            "delivery_manifest_sha256": sha256_file(manifest_path),
        }
        ctx.state["composition"] = CompositionArtifacts(
            html_path=str(project / str(manifest["source_html_path"])),
            preview_path=str(mp4_path),
            layer_manifest=[{"kind": "video", "path": str(mp4_path)}],
        )
        return ctx

    @unittest.skipUnless(os.name == "posix", "transaction unlink audit uses dir fds")
    def test_step2c_abort_retains_artifacts_without_any_transaction_unlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)
            final_dir = run_dir / "final"
            pointer = final_dir / "video_delivery.json"
            unlink_marker = run_dir / "transaction-unlink-was-called"
            armed = False
            real_write_pointer = finalize_module._write_video_delivery_pointer
            real_assert_root = (
                attempt_candidates_module.SecureRunMemberAccessor
                ._assert_root_unchanged
            )
            real_unlink = os.unlink

            def write_then_arm(*args, **kwargs):
                nonlocal armed
                real_write_pointer(*args, **kwargs)
                armed = True

            def fail_context_exit(accessor):
                real_assert_root(accessor)
                if armed:
                    raise ValueError("pre-commit context validation failed")

            def forbid_transaction_unlink(path, *args, **kwargs):
                name = os.fspath(path)
                if name == pointer.name or name.startswith(f".{pointer.name}."):
                    unlink_marker.write_text(name, encoding="utf-8")
                    raise AssertionError("transaction entries are append-only")
                return real_unlink(path, *args, **kwargs)

            with (
                patch.object(
                    finalize_module,
                    "_write_video_delivery_pointer",
                    side_effect=write_then_arm,
                ),
                patch.object(
                    attempt_candidates_module.SecureRunMemberAccessor,
                    "_assert_root_unchanged",
                    autospec=True,
                    side_effect=fail_context_exit,
                ),
                patch.object(
                    attempt_candidates_module.os,
                    "unlink",
                    side_effect=forbid_transaction_unlink,
                ),
            ):
                result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "error")
            self.assertIn("pre-commit context validation failed", _video_blocker_reason(result))
            self.assertFalse(
                unlink_marker.exists(),
                "abort attempted destructive cleanup of a transaction entry",
            )
            self.assertFalse(pointer.exists())
            self.assertFalse(ctx.state.get("finalized", False))
            self.assertIn("aborted", self._phase_tokens(final_dir))
            self.assertTrue(
                any(path.name.endswith(".displaced") for path in final_dir.iterdir())
            )

    @unittest.skipUnless(os.name == "posix", "destination race uses dir-fd rename")
    def test_step2c_forward_publish_preserves_a_destination_winner_and_hidden_new(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)
            final_dir = run_dir / "final"
            pointer = final_dir / "video_delivery.json"
            winner = b'{"writer":"concurrent-winner"}\n'
            real_rename = os.rename
            injected = False

            def desired_no_replace(parent, source_name, destination_name):
                nonlocal injected
                destination_text = os.fspath(destination_name)
                if destination_text == pointer.name and not injected:
                    injected = True
                    if isinstance(parent, int):
                        descriptor = os.open(
                            pointer.name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=parent,
                        )
                        try:
                            os.write(descriptor, winner)
                            os.fsync(descriptor)
                        finally:
                            os.close(descriptor)
                    else:
                        pointer.write_bytes(winner)
                    raise FileExistsError(errno.EEXIST, "destination won the race")
                if isinstance(parent, int):
                    try:
                        os.stat(
                            destination_text,
                            dir_fd=parent,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        raise FileExistsError(errno.EEXIST, "destination exists")
                    real_rename(
                        source_name,
                        destination_name,
                        src_dir_fd=parent,
                        dst_dir_fd=parent,
                    )
                else:
                    source = Path(parent) / os.fspath(source_name)
                    destination = Path(parent) / destination_text
                    if destination.exists():
                        raise FileExistsError(errno.EEXIST, "destination exists")
                    real_rename(source, destination)

            def vulnerable_legacy_rename(source, destination, *args, **kwargs):
                nonlocal injected
                if os.fspath(destination) == pointer.name and not injected:
                    injected = True
                    pointer.write_bytes(winner)
                return real_rename(source, destination, *args, **kwargs)

            with (
                patch.object(
                    video_pointer_transaction_module,
                    "_atomic_no_replace_rename",
                    side_effect=desired_no_replace,
                    create=True,
                ),
                patch.object(
                    video_pointer_transaction_module.os,
                    "rename",
                    side_effect=vulnerable_legacy_rename,
                ),
            ):
                result = finalize({}, ctx=ctx)

            self.assertTrue(injected)
            self.assertEqual(result.status, "error")
            self.assertEqual(pointer.read_bytes(), winner)
            self.assertFalse(ctx.state.get("finalized", False))
            self.assertTrue(
                any(
                    re.fullmatch(
                        r"\.video_delivery\.json\.[0-9a-f]{32}\.new",
                        path.name,
                    )
                    for path in final_dir.iterdir()
                ),
                "the unpublished new file must remain retained",
            )
            self.assertIn("reconciliation-required", self._phase_tokens(final_dir))

    @unittest.skipUnless(os.name == "posix", "source races use dir-fd rename")
    def test_step2c_source_swaps_remain_recoverable_in_forward_and_abort_moves(
        self,
    ) -> None:
        for race_point in ("new-before-publish", "target-before-displace"):
            with self.subTest(race_point=race_point):
                with tempfile.TemporaryDirectory() as tmp:
                    run_dir = Path(tmp) / "video-run"
                    ctx = _context(run_dir)
                    _install_passed_delivery(ctx)
                    final_dir = run_dir / "final"
                    pointer = final_dir / "video_delivery.json"
                    foreign = f"foreign-{race_point}\n".encode()
                    saved_original = final_dir / f"attacker-saved-{race_point}"
                    real_rename = os.rename
                    armed = False
                    injected = False
                    real_write_pointer = finalize_module._write_video_delivery_pointer
                    real_assert_root = (
                        attempt_candidates_module.SecureRunMemberAccessor
                        ._assert_root_unchanged
                    )

                    def write_then_arm(*args, **kwargs):
                        nonlocal armed
                        real_write_pointer(*args, **kwargs)
                        armed = True

                    def fail_context_exit(accessor):
                        real_assert_root(accessor)
                        if race_point == "target-before-displace" and armed:
                            raise ValueError("force abort after publish")

                    def inject_source_swap(
                        source_name,
                        destination_name,
                        *,
                        parent_fd: int | None,
                    ) -> None:
                        nonlocal injected
                        source_text = os.fspath(source_name)
                        destination_text = os.fspath(destination_name)
                        forward = (
                            race_point == "new-before-publish"
                            and destination_text == pointer.name
                            and source_text != pointer.name
                        )
                        aborting = (
                            race_point == "target-before-displace"
                            and armed
                            and source_text == pointer.name
                            and destination_text != pointer.name
                        )
                        if not injected and (forward or aborting):
                            injected = True
                            if parent_fd is None:
                                source_path = final_dir / source_text
                                source_path.rename(saved_original)
                                source_path.write_bytes(foreign)
                            else:
                                real_rename(
                                    source_text,
                                    saved_original.name,
                                    src_dir_fd=parent_fd,
                                    dst_dir_fd=parent_fd,
                                )
                                descriptor = os.open(
                                    source_text,
                                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                    0o600,
                                    dir_fd=parent_fd,
                                )
                                try:
                                    os.write(descriptor, foreign)
                                    os.fsync(descriptor)
                                finally:
                                    os.close(descriptor)

                    def desired_no_replace(parent, source_name, destination_name):
                        parent_fd = parent if isinstance(parent, int) else None
                        inject_source_swap(
                            source_name,
                            destination_name,
                            parent_fd=parent_fd,
                        )
                        if parent_fd is not None:
                            try:
                                os.stat(
                                    destination_name,
                                    dir_fd=parent_fd,
                                    follow_symlinks=False,
                                )
                            except FileNotFoundError:
                                pass
                            else:
                                raise FileExistsError(errno.EEXIST, "destination exists")
                            real_rename(
                                source_name,
                                destination_name,
                                src_dir_fd=parent_fd,
                                dst_dir_fd=parent_fd,
                            )
                        else:
                            source = Path(parent) / os.fspath(source_name)
                            destination = Path(parent) / os.fspath(destination_name)
                            if destination.exists():
                                raise FileExistsError(errno.EEXIST, "destination exists")
                            real_rename(source, destination)

                    def legacy_rename(source, destination, *args, **kwargs):
                        inject_source_swap(
                            source,
                            destination,
                            parent_fd=kwargs.get("src_dir_fd"),
                        )
                        return real_rename(source, destination, *args, **kwargs)

                    with (
                        patch.object(
                            video_pointer_transaction_module,
                            "_atomic_no_replace_rename",
                            side_effect=desired_no_replace,
                            create=True,
                        ),
                        patch.object(
                            video_pointer_transaction_module.os,
                            "rename",
                            side_effect=legacy_rename,
                        ),
                        patch.object(
                            finalize_module,
                            "_write_video_delivery_pointer",
                            side_effect=write_then_arm,
                        ),
                        patch.object(
                            attempt_candidates_module.SecureRunMemberAccessor,
                            "_assert_root_unchanged",
                            autospec=True,
                            side_effect=fail_context_exit,
                        ),
                    ):
                        result = finalize({}, ctx=ctx)

                    self.assertTrue(injected, f"race point was not reached: {race_point}")
                    self.assertEqual(result.status, "error")
                    self.assertTrue(saved_original.is_file())
                    saved_payload = saved_original.read_bytes()
                    self.assertNotEqual(saved_payload, foreign)
                    self.assertIn(foreign, self._retained_bytes(final_dir))
                    self.assertFalse(ctx.state.get("finalized", False))
                    self.assertIn(
                        "reconciliation-required",
                        self._phase_tokens(final_dir),
                    )
                    self.assertTrue(
                        any(
                            self._CONFLICT_NAME.fullmatch(path.name)
                            for path in final_dir.iterdir()
                        )
                    )

    def test_step2c_crash_gaps_recover_absent_and_prior_pointer_states(self) -> None:
        cases = (
            (None, "no-prior-confirmed"),
            (None, "publish-intent"),
            (b'{"sentinel":"prior"}\n', "prior-quarantined"),
            (b'{"sentinel":"prior"}\n', "published"),
        )
        for prior_pointer, crash_phase in cases:
            with self.subTest(
                prior=prior_pointer is not None,
                crash_phase=crash_phase,
            ):
                with tempfile.TemporaryDirectory() as tmp:
                    run_dir = Path(tmp) / "video-run"
                    script = (
                        "import os,sys\n"
                        "from pathlib import Path\n"
                        "from unittest.mock import patch\n"
                        "from autodesign import video_pointer_transaction as m\n"
                        "from autodesign.tools.finalize import finalize\n"
                        "from tests.test_video_runner_finalize import _context,_install_passed_delivery\n"
                        "run=Path(sys.argv[1]); phase=sys.argv[2]\n"
                        "ctx=_context(run); _install_passed_delivery(ctx)\n"
                        "def crash(observed_phase, **details):\n"
                        " value=str(getattr(observed_phase, 'value', observed_phase)).lower().replace('_','-')\n"
                        " if value == phase: os._exit(91)\n"
                        "with patch.object(m, '_video_pointer_transaction_phase_hook', side_effect=crash, create=True):\n"
                        " finalize({}, ctx=ctx)\n"
                    )
                    if prior_pointer is not None:
                        pointer = run_dir / "final" / "video_delivery.json"
                        pointer.parent.mkdir(parents=True)
                        pointer.write_bytes(prior_pointer)
                    crashed = subprocess.run(
                        [sys.executable, "-c", script, str(run_dir), crash_phase],
                        cwd=Path(__file__).resolve().parents[1],
                        check=False,
                    )

                    self.assertEqual(
                        crashed.returncode,
                        91,
                        f"transaction did not expose durable crash checkpoint {crash_phase}",
                    )
                    final_dir = run_dir / "final"
                    phase_records = [
                        self._PHASE_NAME.fullmatch(path.name)
                        for path in final_dir.iterdir()
                    ]
                    txids = {
                        match.group(1)
                        for match in phase_records
                        if match is not None
                    }
                    self.assertEqual(len(txids), 1)
                    crashed_txid = next(iter(txids))

                    recovery_ctx = self._reopen_delivery_context(run_dir)
                    recovered = finalize({}, ctx=recovery_ctx)

                    self.assertEqual(recovered.status, "ok", recovered.error_message)
                    self.assertTrue(recovery_ctx.state["finalized"])
                    self.assertTrue(
                        (final_dir / "video_delivery.json").is_file(),
                        "recovery stranded an empty pointer",
                    )
                    self.assertIn(
                        "aborted",
                        self._phase_tokens(final_dir, crashed_txid),
                    )

    def test_step2c_committed_crash_gets_a_fresh_recovery_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            script = (
                "import os,sys\n"
                "from pathlib import Path\n"
                "from unittest.mock import patch\n"
                "from autodesign import video_pointer_transaction as m\n"
                "from autodesign.tools.finalize import finalize\n"
                "from tests.test_video_runner_finalize import _context,_install_passed_delivery\n"
                "run=Path(sys.argv[1]); ctx=_context(run); _install_passed_delivery(ctx)\n"
                "def crash(observed_phase, **details):\n"
                " value=str(getattr(observed_phase, 'value', observed_phase)).lower().replace('_','-')\n"
                " if value == 'committed': os._exit(91)\n"
                "with patch.object(m, '_video_pointer_transaction_phase_hook', side_effect=crash, create=True):\n"
                " finalize({}, ctx=ctx)\n"
            )
            crashed = subprocess.run(
                [sys.executable, "-c", script, str(run_dir)],
                cwd=Path(__file__).resolve().parents[1],
                check=False,
            )
            self.assertEqual(crashed.returncode, 91)
            final_dir = run_dir / "final"
            txids = {
                match.group(1)
                for path in final_dir.iterdir()
                if (match := self._PHASE_NAME.fullmatch(path.name)) is not None
            }
            self.assertEqual(len(txids), 1)
            committed_txid = next(iter(txids))

            recovery_ctx = self._reopen_delivery_context(run_dir)
            recovered = finalize({}, ctx=recovery_ctx)

            self.assertEqual(recovered.status, "ok", recovered.error_message)
            self.assertTrue(recovery_ctx.state["finalized"])
            committed_phases = self._phase_tokens(final_dir, committed_txid)
            self.assertIn("committed", committed_phases)
            self.assertIn("recovery-committed-confirmed", committed_phases)
            self.assertNotIn("aborted", committed_phases)

    def test_step2c_unsafe_or_noncontiguous_active_phase_records_fail_closed(self) -> None:
        cases = ("gap", "hardlink", "oversize")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                run_dir = Path(tmp) / "video-run"
                ctx = _context(run_dir)
                _install_passed_delivery(ctx)
                final_dir = run_dir / "final"
                final_dir.mkdir(exist_ok=True)
                txid = "a" * 32
                sequence = 2 if case == "gap" else 1
                phase = final_dir / (
                    f".video_delivery.json.{txid}."
                    f"phase-{sequence:06d}-prepared.json"
                )
                payload = b"{}" if case != "oversize" else b"x" * (64 * 1024 + 1)
                phase.write_bytes(payload)
                phase.chmod(0o600)
                if case == "hardlink":
                    os.link(phase, final_dir / "phase-hardlink-alias")

                result = finalize({}, ctx=ctx)

                self.assertEqual(result.status, "error")
                self.assertFalse(ctx.state.get("finalized", False))
                self.assertFalse((final_dir / "video_delivery.json").exists())

    @unittest.skipUnless(os.name == "posix", "native flag matrix uses dir fds")
    def test_step2c_posix_native_no_replace_uses_exclusive_flags_for_every_move(
        self,
    ) -> None:
        cases = (("linux", 0x1), ("darwin", 0x4))
        for platform_name, expected_flag in cases:
            with self.subTest(platform=platform_name):
                with tempfile.TemporaryDirectory() as tmp:
                    run_dir = Path(tmp) / "video-run"
                    ctx = _context(run_dir)
                    _install_passed_delivery(ctx)
                    pointer = run_dir / "final" / "video_delivery.json"
                    legacy_rename_marker = run_dir / f"legacy-{platform_name}-rename"
                    real_rename = os.rename

                    def native_rename(
                        source_parent,
                        source_name,
                        destination_parent,
                        destination_name,
                        flags,
                    ):
                        if flags != expected_flag:
                            raise AssertionError(
                                f"{platform_name} no-replace flag was {flags:#x}"
                            )
                        if source_parent != destination_parent:
                            raise AssertionError("transaction move crossed parents")
                        source = os.fsdecode(source_name)
                        destination = os.fsdecode(destination_name)
                        try:
                            os.stat(
                                destination,
                                dir_fd=destination_parent,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            pass
                        else:
                            return -1
                        real_rename(
                            source,
                            destination,
                            src_dir_fd=source_parent,
                            dst_dir_fd=destination_parent,
                        )
                        return 0

                    def record_legacy_rename(source, destination, *args, **kwargs):
                        legacy_rename_marker.write_text("used", encoding="utf-8")
                        return real_rename(source, destination, *args, **kwargs)

                    runtime_os = SimpleNamespace(
                        name="posix",
                        path=os.path,
                        fspath=os.fspath,
                    )
                    with (
                        patch.object(
                            video_pointer_transaction_module,
                            "_RUNTIME_OS",
                            runtime_os,
                        ),
                        patch.object(
                            video_pointer_transaction_module.sys,
                            "platform",
                            platform_name,
                        ),
                        patch.object(
                            video_pointer_transaction_module,
                            "_native_no_replace_rename_api_factory",
                            return_value=(native_rename, lambda: errno.EEXIST),
                            create=True,
                        ),
                        patch.object(
                            video_pointer_transaction_module.os,
                            "rename",
                            side_effect=record_legacy_rename,
                        ),
                    ):
                        result = finalize({}, ctx=ctx)

                    self.assertEqual(result.status, "ok", result.error_message)
                    self.assertTrue(ctx.state["finalized"])
                    self.assertTrue(pointer.is_file())
                    self.assertFalse(
                        legacy_rename_marker.exists(),
                        "publication bypassed the native no-replace boundary",
                    )

    def test_step2c_native_no_replace_unavailable_or_unsupported_fails_closed(
        self,
    ) -> None:
        cases = (
            ("unavailable", None),
            ("enosys", errno.ENOSYS),
            ("einval", errno.EINVAL),
            ("exdev", errno.EXDEV),
        )
        for failure_case, error_number in cases:
            with self.subTest(failure_case=failure_case):
                with tempfile.TemporaryDirectory() as tmp:
                    run_dir = Path(tmp) / "video-run"
                    ctx = _context(run_dir)
                    _install_passed_delivery(ctx)
                    pointer = run_dir / "final" / "video_delivery.json"
                    legacy_fallback_marker = run_dir / "legacy-rename-fallback"
                    real_rename = os.rename

                    def legacy_fallback(source, destination, *args, **kwargs):
                        legacy_fallback_marker.write_text("used", encoding="utf-8")
                        return real_rename(source, destination, *args, **kwargs)

                    if error_number is None:
                        factory_patch = patch.object(
                            video_pointer_transaction_module,
                            "_native_no_replace_rename_api_factory",
                            side_effect=RuntimeError("native no-replace API unavailable"),
                            create=True,
                        )
                    else:
                        factory_patch = patch.object(
                            video_pointer_transaction_module,
                            "_native_no_replace_rename_api_factory",
                            return_value=(lambda *_args: -1, lambda: error_number),
                            create=True,
                        )
                    with (
                        factory_patch,
                        patch.object(
                            video_pointer_transaction_module.os,
                            "rename",
                            side_effect=legacy_fallback,
                        ),
                    ):
                        result = finalize({}, ctx=ctx)

                    self.assertEqual(result.status, "error")
                    self.assertFalse(ctx.state.get("finalized", False))
                    self.assertFalse(pointer.exists())
                    self.assertFalse(
                        legacy_fallback_marker.exists(),
                        "native failure fell back to replace-capable os.rename",
                    )

    def test_step2c_precommit_original_rollback_and_close_errors_are_aggregated(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)
            pointer = run_dir / "final" / "video_delivery.json"
            armed = False
            real_write_pointer = finalize_module._write_video_delivery_pointer
            real_assert_root = (
                attempt_candidates_module.SecureRunMemberAccessor
                ._assert_root_unchanged
            )

            def write_then_arm(*args, **kwargs):
                nonlocal armed
                real_write_pointer(*args, **kwargs)
                armed = True

            def fail_context_exit(accessor):
                real_assert_root(accessor)
                if armed:
                    raise ValueError("original pre-commit validation error")

            def fail_new_close(*_args, **kwargs):
                if not kwargs.get("durable_committed", False):
                    raise OSError("pre-commit retained-handle close error")

            with (
                patch.object(
                    finalize_module,
                    "_write_video_delivery_pointer",
                    side_effect=write_then_arm,
                ),
                patch.object(
                    attempt_candidates_module.SecureRunMemberAccessor,
                    "_assert_root_unchanged",
                    autospec=True,
                    side_effect=fail_context_exit,
                ),
                patch.object(
                    video_pointer_transaction_module,
                    "_publication_resource_close_hook",
                    side_effect=fail_new_close,
                    create=True,
                ),
            ):
                result = finalize({}, ctx=ctx)

            reason = _video_blocker_reason(result)
            self.assertEqual(result.status, "error")
            self.assertIn("original pre-commit validation error", reason)
            self.assertIn("retained-handle close error", reason)
            self.assertIn("rollback", reason)
            self.assertFalse(pointer.exists())
            self.assertFalse(ctx.state.get("finalized", False))

    def test_step2c_postcommit_close_error_preserves_success_and_surfaces_warning(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)
            pointer = run_dir / "final" / "video_delivery.json"
            def fail_new_close(*_args, **kwargs):
                if kwargs.get("durable_committed", False):
                    raise OSError("close after durable commit")

            with (
                patch.object(
                    video_pointer_transaction_module,
                    "_publication_resource_close_hook",
                    side_effect=fail_new_close,
                    create=True,
                ),
            ):
                result = finalize({}, ctx=ctx)

            diagnostics = json.dumps(
                {"payload": result.payload, "state": ctx.state},
                default=str,
            ).lower()
            self.assertEqual(result.status, "ok", result.error_message)
            self.assertTrue(ctx.state["finalized"])
            self.assertTrue(pointer.is_file())
            self.assertIn("close after durable commit", diagnostics)

    def test_step2c_committed_hook_error_preserves_success_and_surfaces_warning(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)

            def fail_committed_hook(phase, **_details):
                if phase == "committed":
                    raise OSError("committed diagnostic hook failed")

            with patch.object(
                video_pointer_transaction_module,
                "_video_pointer_transaction_phase_hook",
                side_effect=fail_committed_hook,
            ):
                result = finalize({}, ctx=ctx)

            diagnostics = json.dumps(
                {"payload": result.payload, "state": ctx.state},
                default=str,
            ).lower()
            self.assertEqual(result.status, "ok", result.error_message)
            self.assertTrue(ctx.state["finalized"])
            self.assertTrue((run_dir / "final" / "video_delivery.json").is_file())
            self.assertIn("committed diagnostic hook failed", diagnostics)

    def test_step2c_recovery_hook_error_is_a_visible_warning_on_second_finalize(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            first_ctx = _context(run_dir)
            _install_passed_delivery(first_ctx)
            first = finalize({}, ctx=first_ctx)
            self.assertEqual(first.status, "ok", first.error_message)

            second_ctx = self._reopen_delivery_context(run_dir)

            def fail_recovery_hook(phase, **_details):
                if phase == "recovery-committed-confirmed":
                    raise OSError("recovery confirmation hook failed")

            with patch.object(
                video_pointer_transaction_module,
                "_video_pointer_transaction_phase_hook",
                side_effect=fail_recovery_hook,
            ):
                second = finalize({}, ctx=second_ctx)

            diagnostics = json.dumps(
                {"payload": second.payload, "state": second_ctx.state},
                default=str,
            ).lower()
            self.assertEqual(second.status, "ok", second.error_message)
            self.assertTrue(second_ctx.state["finalized"])
            self.assertIn("recovery confirmation hook failed", diagnostics)

    def test_step2c_recovery_hook_and_close_errors_are_aggregated_as_warnings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            first_ctx = _context(run_dir)
            _install_passed_delivery(first_ctx)
            first = finalize({}, ctx=first_ctx)
            self.assertEqual(first.status, "ok", first.error_message)

            second_ctx = self._reopen_delivery_context(run_dir)

            def fail_recovery_hook(phase, **_details):
                if phase == "recovery-committed-confirmed":
                    raise OSError("recovery confirmation hook failed")

            def fail_recovery_close(*, transaction, **_kwargs):
                if transaction.phase == "recovery-committed-confirmed":
                    raise OSError("recovery retained handle close failed")

            with (
                patch.object(
                    video_pointer_transaction_module,
                    "_video_pointer_transaction_phase_hook",
                    side_effect=fail_recovery_hook,
                ),
                patch.object(
                    video_pointer_transaction_module,
                    "_publication_resource_close_hook",
                    side_effect=fail_recovery_close,
                ),
            ):
                second = finalize({}, ctx=second_ctx)

            diagnostics = json.dumps(
                {"payload": second.payload, "state": second_ctx.state},
                default=str,
            ).lower()
            self.assertEqual(second.status, "ok", second.error_message)
            self.assertTrue(second_ctx.state["finalized"])
            self.assertIn("recovery confirmation hook failed", diagnostics)
            self.assertIn("recovery retained handle close failed", diagnostics)

    @unittest.skipUnless(os.name == "posix", "descriptor leak test uses fstat")
    def test_step2c_posix_create_new_failure_closes_retained_prior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)
            pointer = run_dir / "final" / "video_delivery.json"
            pointer.parent.mkdir()
            sentinel = b'{"sentinel":"prior"}\n'
            pointer.write_bytes(sentinel)
            prior_descriptors: list[int] = []
            real_open = video_pointer_transaction_module._VideoPointerBackend.open
            real_create = video_pointer_transaction_module._VideoPointerBackend.create

            def record_open(backend, name, **kwargs):
                retained = real_open(backend, name, **kwargs)
                if name == pointer.name:
                    prior_descriptors.append(int(retained.handle))
                return retained

            def fail_new_create(backend, name, data):
                if name.endswith(".new"):
                    raise FileExistsError("simulated CREATE_NEW failure")
                return real_create(backend, name, data)

            with (
                patch.object(
                    video_pointer_transaction_module._VideoPointerBackend,
                    "open",
                    autospec=True,
                    side_effect=record_open,
                ),
                patch.object(
                    video_pointer_transaction_module._VideoPointerBackend,
                    "create",
                    autospec=True,
                    side_effect=fail_new_create,
                ),
            ):
                result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "error")
            self.assertTrue(prior_descriptors)
            for descriptor in prior_descriptors:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)
            self.assertEqual(pointer.read_bytes(), sentinel)

    def test_step2c_windows_create_new_failure_closes_retained_prior_handle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)
            pointer = run_dir / "final" / "video_delivery.json"
            pointer.parent.mkdir()
            sentinel = b'{"sentinel":"prior"}\n'
            pointer.write_bytes(sentinel)
            native = _FakeWindowsRetainedHandleAPI()
            portable_os = SimpleNamespace(name="nt", path=os.path, fspath=os.fspath)
            real_create = video_pointer_transaction_module._VideoPointerBackend.create

            def fail_new_create(backend, name, data):
                if name.endswith(".new"):
                    raise FileExistsError("simulated CREATE_NEW failure")
                return real_create(backend, name, data)

            with (
                patch.object(attempt_candidates_module, "_RUNTIME_OS", portable_os),
                patch.object(
                    video_pointer_transaction_module,
                    "_RUNTIME_OS",
                    portable_os,
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_directory_replacement_guard",
                    side_effect=lambda _path: nullcontext(),
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_current_user_sid",
                    return_value="S-1-5-21-step2c-test",
                ),
                patch.object(
                    video_pointer_transaction_module,
                    "_windows_native_handle_api_factory",
                    return_value=native,
                ),
                patch.object(
                    video_pointer_transaction_module,
                    "_windows_pointer_security_attributes_factory",
                    return_value=(object(), lambda: None),
                ),
                patch.object(
                    video_pointer_transaction_module._VideoPointerBackend,
                    "create",
                    autospec=True,
                    side_effect=fail_new_create,
                ),
            ):
                result = finalize({}, ctx=ctx)

            self.assertEqual(result.status, "error")
            self.assertTrue(native.closed)
            self.assertFalse(native.handles)
            self.assertEqual(pointer.read_bytes(), sentinel)

    def test_step2c_windows_native_wrapper_declares_every_io_signature(self) -> None:
        import ctypes
        from ctypes import wintypes

        class FakeFunction:
            def __init__(self) -> None:
                self.argtypes = None
                self.restype = None

            def __call__(self, *_args, **_kwargs):
                return 1

        function_names = (
            "WriteFile",
            "ReadFile",
            "SetFilePointerEx",
            "FlushFileBuffers",
            "CloseHandle",
            "MoveFileExW",
        )
        kernel32 = SimpleNamespace(
            **{name: FakeFunction() for name in function_names}
        )
        with (
            patch.object(video_pointer_transaction_module.os, "name", "nt"),
            patch.object(ctypes, "WinDLL", return_value=kernel32, create=True),
        ):
            video_pointer_transaction_module._WindowsNativeHandleAPI()

        for name in function_names:
            function = getattr(kernel32, name)
            self.assertIsNotNone(function.argtypes, name)
            self.assertIs(function.restype, wintypes.BOOL, name)

    @unittest.skipUnless(os.name == "posix", "outer descriptor test uses fstat")
    def test_step2c_outer_close_after_commit_is_a_visible_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)
            run_identity = (run_dir.stat().st_dev, run_dir.stat().st_ino)
            armed = False
            failed = False
            real_close = os.close

            def arm_after_commit(phase, **_details):
                nonlocal armed
                if phase == "committed":
                    armed = True

            def fail_outer_close(descriptor):
                nonlocal failed
                metadata = os.fstat(descriptor)
                real_close(descriptor)
                if (
                    armed
                    and not failed
                    and (metadata.st_dev, metadata.st_ino) == run_identity
                ):
                    failed = True
                    raise OSError("outer retained run close failed")

            with (
                patch.object(
                    video_pointer_transaction_module,
                    "_video_pointer_transaction_phase_hook",
                    side_effect=arm_after_commit,
                ),
                patch.object(
                    attempt_candidates_module.os,
                    "close",
                    side_effect=fail_outer_close,
                ),
            ):
                result = finalize({}, ctx=ctx)

            diagnostics = json.dumps(
                {"payload": result.payload, "state": ctx.state},
                default=str,
            ).lower()
            self.assertTrue(failed)
            self.assertEqual(result.status, "ok", result.error_message)
            self.assertTrue(ctx.state["finalized"])
            self.assertIn("outer retained run close failed", diagnostics)

    @unittest.skipUnless(os.name == "posix", "outer descriptor test uses fstat")
    def test_step2c_outer_close_before_commit_is_aggregated_with_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)
            run_identity = (run_dir.stat().st_dev, run_dir.stat().st_ino)
            failed = False
            real_close = os.close

            def fail_outer_close(descriptor):
                nonlocal failed
                metadata = os.fstat(descriptor)
                real_close(descriptor)
                if not failed and (metadata.st_dev, metadata.st_ino) == run_identity:
                    failed = True
                    raise OSError("outer retained run close failed before commit")

            with (
                patch.object(
                    finalize_module,
                    "_write_video_delivery_pointer",
                    side_effect=ValueError("original pre-commit body error"),
                ),
                patch.object(
                    attempt_candidates_module.os,
                    "close",
                    side_effect=fail_outer_close,
                ),
            ):
                result = finalize({}, ctx=ctx)

            reason = _video_blocker_reason(result)
            self.assertTrue(failed)
            self.assertEqual(result.status, "error")
            self.assertIn("original pre-commit body error", reason)
            self.assertIn("outer retained run close failed before commit", reason)
            self.assertFalse(ctx.state.get("finalized", False))

    def test_step2c_mocked_windows_transaction_uses_one_retained_handle_api(self) -> None:
        class RetainedHandleAPI:
            def __init__(self) -> None:
                self.handles: dict[int, int] = {}
                self.next_handle = 100
                self.create_calls: list[tuple[Path, int, int, object, int, int]] = []
                self.info_classes: list[str] = []
                self.move_calls: list[tuple[Path, Path, int]] = []
                self.closed: list[int] = []

            def create_file(
                self,
                path,
                desired_access,
                share_mode,
                security_attributes,
                creation_disposition,
                flags_and_attributes,
            ):
                target = Path(path)
                self.create_calls.append(
                    (
                        target,
                        desired_access,
                        share_mode,
                        security_attributes,
                        creation_disposition,
                        flags_and_attributes,
                    )
                )
                if creation_disposition == 1:
                    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
                elif creation_disposition == 3:
                    flags = os.O_RDONLY
                else:
                    raise AssertionError(f"unexpected disposition {creation_disposition}")
                descriptor = os.open(target, flags, 0o600)
                handle = self.next_handle
                self.next_handle += 1
                self.handles[handle] = descriptor
                return handle

            def get_file_information_by_handle_ex(self, handle, info_class):
                self.info_classes.append(info_class)
                metadata = os.fstat(self.handles[handle])
                if info_class == "FileIdInfo":
                    return {
                        "volume_serial_number": metadata.st_dev,
                        "file_id": int(metadata.st_ino).to_bytes(16, "little"),
                    }
                if info_class == "FileStandardInfo":
                    return {
                        "number_of_links": metadata.st_nlink,
                        "end_of_file": metadata.st_size,
                        "directory": False,
                    }
                if info_class == "FileBasicInfo":
                    return {
                        "file_attributes": 0,
                        "last_write_time": metadata.st_mtime_ns,
                        "creation_time": metadata.st_ctime_ns,
                        "change_time": metadata.st_ctime_ns,
                        "last_access_time": metadata.st_atime_ns,
                    }
                raise AssertionError(f"unexpected info class {info_class}")

            def write_file(self, handle, data):
                return os.write(self.handles[handle], bytes(data[:3]))

            def read_file(self, handle, size):
                return os.read(self.handles[handle], min(size, 2))

            def set_file_pointer_ex(self, handle, offset):
                os.lseek(self.handles[handle], offset, os.SEEK_SET)

            def flush_file_buffers(self, handle):
                os.fsync(self.handles[handle])

            def close_handle(self, handle):
                if handle in self.closed:
                    raise AssertionError("native HANDLE was closed twice")
                self.closed.append(handle)
                os.close(self.handles.pop(handle))

            def get_last_error(self):
                return 183

            def move_file_ex(self, source, destination, flags):
                source_path = Path(source)
                destination_path = Path(destination)
                self.move_calls.append((source_path, destination_path, flags))
                if destination_path.exists():
                    return 0
                os.rename(source_path, destination_path)
                return 1

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)
            pointer = run_dir / "final" / "video_delivery.json"
            native = RetainedHandleAPI()
            security_attributes = object()
            security_cleanup_calls: list[object] = []
            portable_os = SimpleNamespace(name="nt", path=os.path, fspath=os.fspath)

            with (
                patch.object(attempt_candidates_module, "_RUNTIME_OS", portable_os),
                patch.object(
                    video_pointer_transaction_module,
                    "_RUNTIME_OS",
                    portable_os,
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_current_user_sid",
                    return_value="S-1-5-21-step2c-test",
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_directory_replacement_guard",
                    side_effect=lambda _path: nullcontext(),
                ),
                patch.object(
                    video_pointer_transaction_module,
                    "_windows_native_handle_api_factory",
                    return_value=native,
                    create=True,
                ),
                patch.object(
                    video_pointer_transaction_module,
                    "_windows_pointer_security_attributes_factory",
                    return_value=(
                        security_attributes,
                        lambda: security_cleanup_calls.append(security_attributes),
                    ),
                    create=True,
                ),
            ):
                result = finalize({}, ctx=ctx)

            self.assertEqual(
                result.status,
                "ok",
                {"error": result.error_message, "payload": result.payload},
            )
            self.assertTrue(ctx.state["finalized"])
            self.assertTrue(pointer.is_file())
            self.assertTrue(native.create_calls)
            created = [call for call in native.create_calls if call[4] == 1]
            opened = [call for call in native.create_calls if call[4] == 3]
            self.assertTrue(created)
            self.assertTrue(opened)
            self.assertTrue(all(call[2] == 0x1 | 0x2 | 0x4 for call in native.create_calls))
            self.assertTrue(
                all(call[5] & 0x00200000 for call in native.create_calls),
                "every native open must reject reparse-point traversal",
            )
            self.assertTrue(all(call[3] is security_attributes for call in created))
            self.assertTrue(all(call[3] is None for call in opened))
            self.assertTrue(
                {"FileIdInfo", "FileStandardInfo", "FileBasicInfo"}
                .issubset(native.info_classes)
            )
            self.assertTrue(native.move_calls)
            self.assertTrue(all(flags == 0x8 for _, _, flags in native.move_calls))
            self.assertEqual(len(native.closed), len(set(native.closed)))
            self.assertEqual(len(security_cleanup_calls), len(created))


class Step2eAbortValidationRedTest(unittest.TestCase):
    def _assert_replaced_final_parent_rejected_during_rollback(
        self,
        *,
        platform: str,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            ctx = _context(run_dir)
            _install_passed_delivery(ctx)
            final_dir = run_dir / "final"
            pointer = final_dir / "video_delivery.json"
            retained_final = run_dir / "retained-final"
            replacement_bytes = (
                f'{{"replacement":"{platform}-guarded-parent"}}\n'.encode()
            )
            published_bytes: bytes | None = None
            outer_failure_armed = False
            real_write_pointer = finalize_module._write_video_delivery_pointer
            real_assert_root = (
                attempt_candidates_module.SecureRunMemberAccessor
                ._assert_root_unchanged
            )

            def write_then_replace_final_parent(*args, **kwargs):
                nonlocal outer_failure_armed, published_bytes
                result = real_write_pointer(*args, **kwargs)
                published_bytes = pointer.read_bytes()
                final_dir.rename(retained_final)
                final_dir.mkdir()
                pointer.write_bytes(replacement_bytes)
                outer_failure_armed = True
                return result

            def fail_outer_lifecycle(accessor):
                real_assert_root(accessor)
                if outer_failure_armed:
                    raise ValueError("injected outer accessor lifecycle failure")

            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(
                        finalize_module,
                        "_write_video_delivery_pointer",
                        side_effect=write_then_replace_final_parent,
                    )
                )
                stack.enter_context(
                    patch.object(
                        attempt_candidates_module.SecureRunMemberAccessor,
                        "_assert_root_unchanged",
                        autospec=True,
                        side_effect=fail_outer_lifecycle,
                    )
                )
                if platform == "windows":
                    portable_os = SimpleNamespace(
                        name="nt",
                        path=os.path,
                        fspath=os.fspath,
                    )
                    native = _FakeWindowsRetainedHandleAPI()
                    stack.enter_context(
                        patch.object(
                            attempt_candidates_module,
                            "_RUNTIME_OS",
                            portable_os,
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            video_pointer_transaction_module,
                            "_RUNTIME_OS",
                            portable_os,
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            attempt_candidates_module,
                            "_windows_directory_replacement_guard",
                            side_effect=lambda _path: nullcontext(),
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            attempt_candidates_module,
                            "_windows_current_user_sid",
                            return_value="S-1-5-21-step2e-test",
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            video_pointer_transaction_module,
                            "_windows_native_handle_api_factory",
                            return_value=native,
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            video_pointer_transaction_module,
                            "_windows_pointer_security_attributes_factory",
                            return_value=(object(), lambda: None),
                        )
                    )
                result = finalize({"notes": "must not commit"}, ctx=ctx)

            reason = _video_blocker_reason(result)
            self.assertTrue(outer_failure_armed)
            self.assertIsNotNone(published_bytes)
            self.assertEqual(result.status, "error")
            self.assertIn("injected outer accessor lifecycle failure", reason)
            self.assertIn("secure publication rollback failed", reason)
            self.assertIn("directory changed during access", reason)
            self.assertFalse(ctx.state.get("finalized", False))
            self.assertEqual(pointer.read_bytes(), replacement_bytes)
            self.assertEqual(
                (retained_final / pointer.name).read_bytes(),
                published_bytes,
            )
            self.assertIn(
                "published",
                Step2cVideoPublicationRedTest._phase_tokens(retained_final),
            )
            self.assertNotIn(
                "committed",
                Step2cVideoPublicationRedTest._phase_tokens(retained_final),
            )

    @unittest.skipUnless(os.name == "posix", "descriptor parent uses POSIX fds")
    def test_step2e_posix_rollback_rejects_replaced_retained_final_parent(
        self,
    ) -> None:
        self._assert_replaced_final_parent_rejected_during_rollback(
            platform="posix"
        )

    def test_step2e_mocked_windows_rollback_rejects_replaced_guarded_final_parent(
        self,
    ) -> None:
        self._assert_replaced_final_parent_rejected_during_rollback(
            platform="windows"
        )


class Step2hNativeIdentityOwnershipRedTest(unittest.TestCase):
    WINDOWS_STABLE_KEYS = frozenset({
        "volume_serial_number",
        "file_id",
        "file_attributes",
        "reparse",
        "nlink",
        "size",
        "last_write_time",
        "sha256",
    })
    POSIX_STABLE_KEYS = frozenset({
        "dev",
        "ino",
        "mode",
        "nlink",
        "size",
        "mtime_ns",
        "sha256",
    })

    @staticmethod
    def _enter_mocked_windows(
        stack: ExitStack,
        native: _Step2hFakeWindowsRetainedHandleAPI,
    ) -> None:
        portable_os = SimpleNamespace(name="nt", path=os.path, fspath=os.fspath)
        stack.enter_context(
            patch.object(attempt_candidates_module, "_RUNTIME_OS", portable_os)
        )
        stack.enter_context(
            patch.object(
                video_pointer_transaction_module,
                "_RUNTIME_OS",
                portable_os,
            )
        )
        stack.enter_context(
            patch.object(
                attempt_candidates_module,
                "_windows_directory_replacement_guard",
                side_effect=lambda _path: nullcontext(),
            )
        )
        stack.enter_context(
            patch.object(
                attempt_candidates_module,
                "_windows_current_user_sid",
                return_value="S-1-5-21-step2h-test",
            )
        )
        stack.enter_context(
            patch.object(
                video_pointer_transaction_module,
                "_windows_native_handle_api_factory",
                return_value=native,
            )
        )
        stack.enter_context(
            patch.object(
                video_pointer_transaction_module,
                "_windows_pointer_security_attributes_factory",
                return_value=(object(), lambda: None),
            )
        )

    @staticmethod
    def _accessor_for_path(run_dir: Path):
        metadata = os.stat(run_dir)
        return attempt_candidates_module.SecureRunMemberAccessor(
            root_reference=run_dir,
            root_identity=(int(metadata.st_dev), int(metadata.st_ino)),
            accepted_roots=(run_dir,),
            requested_root=run_dir,
            canonical_root=run_dir,
            binding=None,
        )

    def test_step2h_mocked_windows_hook_uses_three_native_owned_leaves(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            pointer = final_dir / "video_delivery.json"
            prior_bytes = b'{"sentinel":"native-prior"}\n'
            pointer.write_bytes(prior_bytes)
            prior_metadata = os.stat(pointer)
            native = _Step2hFakeWindowsRetainedHandleAPI(pointer)
            payload = {"replacement": "native-identity"}
            adapter_records: list[dict[str, object]] = []
            payload_records: list[dict[str, object]] = []
            match_records: list[dict[str, object]] = []
            preflight_handle: list[int] = []
            phase_states: list[tuple[str, bool, int]] = []
            publication_error: BaseException | None = None
            real_expected_prior_matches = (
                video_pointer_transaction_module._expected_prior_matches
            )

            def record_expected_prior_match(
                backend,
                *,
                expected_prior,
                observed_prior,
            ):
                pointer_handles = tuple(native.pointer_open_handles())
                if observed_prior is not None:
                    preflight_handle[:] = [int(observed_prior.handle)]
                matched = real_expected_prior_matches(
                    backend,
                    expected_prior=expected_prior,
                    observed_prior=observed_prior,
                )
                match_records.append({
                    "expected": {
                        "platform": expected_prior.get("platform"),
                        "stable": dict(expected_prior.get("stable") or {}),
                    },
                    "observed": {
                        "platform": observed_prior.snapshot.get("platform"),
                        "stable": dict(
                            observed_prior.snapshot.get("stable") or {}
                        ),
                    },
                    "matched": matched,
                    "pointer_handles": pointer_handles,
                    "closed_at_match": tuple(native.closed),
                })
                return matched

            def record_adapter(event, **details):
                adapter_records.append({
                    "event": event,
                    "snapshot": details.get("snapshot"),
                    "pointer_handles": tuple(native.pointer_open_handles()),
                    "closed": tuple(native.closed),
                })

            def select_payload(snapshot):
                payload_records.append({
                    "snapshot": snapshot,
                    "pointer_handles": tuple(native.pointer_open_handles()),
                    "closed": tuple(native.closed),
                })
                return payload

            def record_phase(phase, **_details):
                handle = preflight_handle[0] if preflight_handle else -1
                phase_states.append(
                    (
                        phase,
                        handle in native.handles,
                        native.close_counts.get(handle, 0),
                    )
                )

            try:
                with ExitStack() as stack:
                    self._enter_mocked_windows(stack, native)
                    stack.enter_context(
                        patch.object(
                            attempt_candidates_module,
                            "_video_delivery_pointer_adapter_hook",
                            side_effect=record_adapter,
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            video_pointer_transaction_module,
                            "_expected_prior_matches",
                            side_effect=record_expected_prior_match,
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            video_pointer_transaction_module,
                            "_video_pointer_transaction_phase_hook",
                            side_effect=record_phase,
                        )
                    )
                    try:
                        with attempt_candidates_module.secure_run_member_access(
                            run_dir
                        ) as accessor:
                            accessor.stage_video_delivery_pointer(
                                Path("final/video_delivery.json"),
                                label="Step 2h native Video pointer",
                                payload_factory=select_payload,
                                invoke_adapter_hook=True,
                            )
                    except BaseException as exc:
                        publication_error = exc

                expected_native_stable = {
                    "volume_serial_number": native.volume_serial_number,
                    "file_id": native.file_id.hex(),
                    "file_attributes": native.file_attributes,
                    "reparse": False,
                    "nlink": int(prior_metadata.st_nlink),
                    "size": len(prior_bytes),
                    "last_write_time": native.last_write_time,
                    "sha256": hashlib.sha256(prior_bytes).hexdigest(),
                }
                self.assertEqual(
                    tuple(record["event"] for record in adapter_records),
                    ("classified_present",),
                )
                self.assertEqual(len(payload_records), 1)
                hook_snapshot = adapter_records[0]["snapshot"]
                payload_snapshot = payload_records[0]["snapshot"]
                self.assertIs(hook_snapshot, payload_snapshot)
                self.assertEqual(getattr(hook_snapshot, "data", None), prior_bytes)
                self.assertEqual(
                    getattr(hook_snapshot, "native_platform", None),
                    "windows",
                )
                native_stable = getattr(hook_snapshot, "native_stable", None)
                self.assertIsNotNone(native_stable)
                self.assertEqual(dict(native_stable), expected_native_stable)
                with self.assertRaises(TypeError):
                    native_stable["file_id"] = "0" * 32

                self.assertEqual(len(match_records), 1)
                record = match_records[0]
                hook_pointer_handles = tuple(
                    adapter_records[0]["pointer_handles"]
                )
                payload_pointer_handles = tuple(
                    payload_records[0]["pointer_handles"]
                )
                pointer_handles = tuple(record["pointer_handles"])
                initial_observation_handle = (
                    hook_pointer_handles[0]
                    if len(hook_pointer_handles) == 1
                    else None
                )
                recheck_observation_handle = (
                    payload_pointer_handles[1]
                    if len(payload_pointer_handles) == 2
                    else None
                )
                transaction_handle = (
                    pointer_handles[2] if len(pointer_handles) == 3 else None
                )
                actual_contract = {
                    "error_type": (
                        type(publication_error).__name__
                        if publication_error is not None
                        else None
                    ),
                    "stable_keys": frozenset(record["expected"]["stable"]),
                    "native_match": record["matched"],
                    "hook_pointer_handle_count": len(hook_pointer_handles),
                    "initial_observation_closed_before_hook": (
                        initial_observation_handle is not None
                        and native.close_counts.get(
                            initial_observation_handle,
                            0,
                        )
                        == 1
                        and initial_observation_handle
                        in adapter_records[0]["closed"]
                    ),
                    "payload_pointer_handle_count": len(
                        payload_pointer_handles
                    ),
                    "recheck_is_distinct": (
                        initial_observation_handle is not None
                        and recheck_observation_handle is not None
                        and initial_observation_handle
                        != recheck_observation_handle
                    ),
                    "both_observations_closed_before_payload": (
                        len(payload_pointer_handles) == 2
                        and all(
                            native.close_counts.get(handle, 0) == 1
                            and handle in payload_records[0]["closed"]
                            for handle in payload_pointer_handles
                        )
                    ),
                    "pointer_handle_count_before_preflight": len(pointer_handles),
                    "same_observations_at_preflight": (
                        pointer_handles[:2] == payload_pointer_handles
                    ),
                    "observations_closed_at_match": (
                        len(pointer_handles) == 3
                        and all(
                            native.close_counts.get(handle, 0) == 1
                            and handle in record["closed_at_match"]
                            for handle in pointer_handles[:2]
                        )
                    ),
                    "preflight_open_at_match": (
                        transaction_handle is not None
                        and transaction_handle not in record["closed_at_match"]
                    ),
                    "phase_states": tuple(phase_states),
                    "preflight_close_count": native.close_counts.get(
                        transaction_handle,
                        0,
                    ),
                }
                expected_phases = (
                    "prepared",
                    "quarantine-intent",
                    "prior-quarantined",
                    "publish-intent",
                    "published",
                    "committed",
                )
                self.assertEqual(
                    actual_contract,
                    {
                        "error_type": None,
                        "stable_keys": self.WINDOWS_STABLE_KEYS,
                        "native_match": True,
                        "hook_pointer_handle_count": 1,
                        "initial_observation_closed_before_hook": True,
                        "payload_pointer_handle_count": 2,
                        "recheck_is_distinct": True,
                        "both_observations_closed_before_payload": True,
                        "pointer_handle_count_before_preflight": 3,
                        "same_observations_at_preflight": True,
                        "observations_closed_at_match": True,
                        "preflight_open_at_match": True,
                        "phase_states": tuple(
                            (phase, True, 0) for phase in expected_phases
                        ),
                        "preflight_close_count": 1,
                    },
                )
                self.assertEqual(record["expected"], record["observed"])
                stable = record["expected"]["stable"]
                self.assertEqual(
                    stable["volume_serial_number"],
                    0xFEDCBA9876543210,
                )
                file_id = bytes.fromhex(stable["file_id"])
                self.assertNotEqual(file_id[8:], b"\0" * 8)
                self.assertNotEqual(
                    int.from_bytes(file_id, "little"),
                    int(prior_metadata.st_ino),
                )
                self.assertEqual(json.loads(pointer.read_bytes()), payload)
                self.assertTrue(
                    all(count == 1 for count in native.close_counts.values())
                )
            finally:
                native.close_remaining()

    def test_step2h_windows_preflight_leaf_is_retained_through_abort_and_closed_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            pointer = final_dir / "video_delivery.json"
            prior_bytes = b'{"sentinel":"abort-prior"}\n'
            pointer.write_bytes(prior_bytes)
            prior_metadata = os.stat(pointer)
            native = _Step2hFakeWindowsRetainedHandleAPI(pointer)
            preflight_handle: list[int] = []
            preflight_records: list[dict[str, object]] = []
            phase_states: list[tuple[str, bool, int]] = []
            retained_parents = []
            rollback_error: BaseException | None = None
            real_expected_prior_matches = (
                video_pointer_transaction_module._expected_prior_matches
            )
            real_stage = (
                video_pointer_transaction_module.stage_video_delivery_pointer
            )

            def record_expected_prior_match(
                backend,
                *,
                expected_prior,
                observed_prior,
            ):
                pointer_handles = tuple(native.pointer_open_handles())
                if observed_prior is not None:
                    preflight_handle[:] = [int(observed_prior.handle)]
                matched = real_expected_prior_matches(
                    backend,
                    expected_prior=expected_prior,
                    observed_prior=observed_prior,
                )
                preflight_records.append({
                    "matched": matched,
                    "expected": {
                        "platform": expected_prior.get("platform"),
                        "stable": dict(expected_prior.get("stable") or {}),
                    },
                    "observed": {
                        "platform": observed_prior.snapshot.get("platform"),
                        "stable": dict(
                            observed_prior.snapshot.get("stable") or {}
                        ),
                    },
                    "pointer_handles": pointer_handles,
                    "closed_at_match": tuple(native.closed),
                })
                return matched

            def record_phase(phase, **_details):
                handle = preflight_handle[0] if preflight_handle else -1
                phase_states.append(
                    (
                        phase,
                        handle in native.handles,
                        native.close_counts.get(handle, 0),
                    )
                )

            def capture_parent(
                parent,
                encoded,
                *,
                expected_prior,
                recovery_warnings=(),
            ):
                retained_parents.append(parent)
                return real_stage(
                    parent,
                    encoded,
                    expected_prior=expected_prior,
                    recovery_warnings=recovery_warnings,
                )

            try:
                with ExitStack() as stack:
                    self._enter_mocked_windows(stack, native)
                    stack.enter_context(
                        patch.object(
                            attempt_candidates_module,
                            "stage_video_delivery_pointer",
                            side_effect=capture_parent,
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            video_pointer_transaction_module,
                            "_expected_prior_matches",
                            side_effect=record_expected_prior_match,
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            video_pointer_transaction_module,
                            "_video_pointer_transaction_phase_hook",
                            side_effect=record_phase,
                        )
                    )
                    try:
                        with attempt_candidates_module.secure_run_member_access(
                            run_dir
                        ) as accessor:
                            accessor.stage_video_delivery_pointer(
                                Path("final/video_delivery.json"),
                                {"replacement": "abort-after-forward"},
                                label="Step 2h abort-owned Video pointer",
                            )
                            raise RuntimeError(
                                "injected accessor body failure after publication"
                            )
                    except BaseException as exc:
                        rollback_error = exc

                self.assertEqual(len(preflight_records), 1)
                record = preflight_records[0]
                pointer_handles = tuple(record["pointer_handles"])
                observation_handle = (
                    pointer_handles[0] if len(pointer_handles) == 2 else None
                )
                transaction_handle = (
                    pointer_handles[1] if len(pointer_handles) == 2 else None
                )
                retained_parent = (
                    retained_parents[0] if len(retained_parents) == 1 else None
                )
                expected_phases = (
                    "prepared",
                    "quarantine-intent",
                    "prior-quarantined",
                    "publish-intent",
                    "published",
                    "aborting",
                    "displace-intent",
                    "displaced",
                    "restore-intent",
                    "aborted",
                )
                expected_native_stable = {
                    "volume_serial_number": native.volume_serial_number,
                    "file_id": native.file_id.hex(),
                    "file_attributes": native.file_attributes,
                    "reparse": False,
                    "nlink": int(prior_metadata.st_nlink),
                    "size": len(prior_bytes),
                    "last_write_time": native.last_write_time,
                    "sha256": hashlib.sha256(prior_bytes).hexdigest(),
                }
                self.assertEqual(
                    {
                        "error_type": (
                            type(rollback_error).__name__
                            if rollback_error is not None
                            else None
                        ),
                        "error_text": str(rollback_error),
                        "native_match": record["matched"],
                        "expected_prior": record["expected"],
                        "observed_prior": record["observed"],
                        "pointer_handle_count_before_preflight": len(
                            pointer_handles
                        ),
                        "observation_closed_at_match": (
                            observation_handle is not None
                            and observation_handle in record["closed_at_match"]
                            and native.close_counts.get(observation_handle, 0) == 1
                        ),
                        "preflight_open_at_match": (
                            transaction_handle is not None
                            and transaction_handle not in record["closed_at_match"]
                        ),
                        "phase_states": tuple(phase_states),
                        "preflight_close_count": native.close_counts.get(
                            transaction_handle,
                            0,
                        ),
                        "parent_transferred": getattr(
                            retained_parent,
                            "transferred",
                            None,
                        ),
                        "parent_closed": getattr(
                            retained_parent,
                            "closed",
                            None,
                        ),
                        "pointer_bytes": pointer.read_bytes(),
                        "all_native_handles_closed": not native.handles,
                    },
                    {
                        "error_type": "RuntimeError",
                        "error_text": (
                            "injected accessor body failure after publication"
                        ),
                        "native_match": True,
                        "expected_prior": {
                            "platform": "windows",
                            "stable": expected_native_stable,
                        },
                        "observed_prior": {
                            "platform": "windows",
                            "stable": expected_native_stable,
                        },
                        "pointer_handle_count_before_preflight": 2,
                        "observation_closed_at_match": True,
                        "preflight_open_at_match": True,
                        "phase_states": tuple(
                            (phase, True, 0) for phase in expected_phases
                        ),
                        "preflight_close_count": 1,
                        "parent_transferred": True,
                        "parent_closed": True,
                        "pointer_bytes": prior_bytes,
                        "all_native_handles_closed": True,
                    },
                )
                self.assertTrue(
                    all(count == 1 for count in native.close_counts.values())
                )
                self.assertNotEqual(
                    int.from_bytes(native.file_id, "little"),
                    int(prior_metadata.st_ino),
                )
            finally:
                native.close_remaining()

    def test_step2h_observation_rejects_bytes_outside_complete_native_snapshot(
        self,
    ) -> None:
        snapshot_bytes = b'{"snapshot":"A"}\n'
        mismatched_bytes = b'{"bytes":"B-is-different"}\n'
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            pointer = final_dir / "video_delivery.json"
            pointer.write_bytes(mismatched_bytes)
            native = _Step2hFakeWindowsRetainedHandleAPI(
                pointer,
                pointer_read_passes=(
                    snapshot_bytes,
                    mismatched_bytes,
                    snapshot_bytes,
                ),
            )
            validation_calls: list[object] = []
            parent_close_calls: list[object] = []
            parent = video_pointer_transaction_module.RetainedParent(
                native_parent=final_dir,
                platform="windows",
                validate_callback=lambda: validation_calls.append(object()),
                close_callback=lambda: parent_close_calls.append(object()),
            )
            accessor = self._accessor_for_path(run_dir)
            observation_error: BaseException | None = None

            try:
                with patch.object(
                    video_pointer_transaction_module,
                    "_windows_native_handle_api_factory",
                    return_value=native,
                ):
                    try:
                        accessor._read_video_pointer_from_parent(
                            parent,
                            Path("final/video_delivery.json"),
                            label="Step 2h observed Video pointer",
                        )
                    except BaseException as exc:
                        observation_error = exc

                pointer_handles = native.pointer_open_handles()
                handle = pointer_handles[0] if len(pointer_handles) == 1 else -1
                self.assertEqual(
                    {
                        "error_type": (
                            type(observation_error).__name__
                            if observation_error is not None
                            else None
                        ),
                        "pointer_open_count": len(pointer_handles),
                        "read_rounds": tuple(native.read_rounds.get(handle, ())),
                        "leaf_close_count": native.close_counts.get(handle, 0),
                        "parent_transferred": parent.transferred,
                        "parent_closed": parent.closed,
                        "parent_close_calls": len(parent_close_calls),
                        "parent_validated": bool(validation_calls),
                    },
                    {
                        "error_type": "ValueError",
                        "pointer_open_count": 1,
                        "read_rounds": (0, 1, 2),
                        "leaf_close_count": 1,
                        "parent_transferred": False,
                        "parent_closed": False,
                        "parent_close_calls": 0,
                        "parent_validated": True,
                    },
                )
            finally:
                native.close_remaining()
                parent.close()

    def test_step2h_observation_mismatch_stops_before_adapter_payload_and_txid(
        self,
    ) -> None:
        snapshot_bytes = b'{"snapshot":"A"}\n'
        mismatched_bytes = b'{"bytes":"B-is-different"}\n'
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            pointer = final_dir / "video_delivery.json"
            pointer.write_bytes(mismatched_bytes)
            native = _Step2hFakeWindowsRetainedHandleAPI(
                pointer,
                pointer_read_passes=(
                    snapshot_bytes,
                    mismatched_bytes,
                    snapshot_bytes,
                ),
            )
            adapter_calls: list[str] = []
            payload_calls: list[object] = []
            uuid_calls: list[object] = []
            publication_error: BaseException | None = None
            real_uuid4 = video_pointer_transaction_module.uuid.uuid4

            def payload_factory(snapshot):
                payload_calls.append(snapshot)
                return {"replacement": "must-not-be-selected"}

            def record_uuid4():
                uuid_calls.append(object())
                return real_uuid4()

            try:
                with ExitStack() as stack:
                    self._enter_mocked_windows(stack, native)
                    stack.enter_context(
                        patch.object(
                            attempt_candidates_module,
                            "_video_delivery_pointer_adapter_hook",
                            side_effect=lambda event, **_details: adapter_calls.append(
                                event
                            ),
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            video_pointer_transaction_module.uuid,
                            "uuid4",
                            side_effect=record_uuid4,
                        )
                    )
                    try:
                        with attempt_candidates_module.secure_run_member_access(
                            run_dir
                        ) as accessor:
                            accessor.stage_video_delivery_pointer(
                                Path("final/video_delivery.json"),
                                label="Step 2h mismatched observation",
                                payload_factory=payload_factory,
                                invoke_adapter_hook=True,
                            )
                    except BaseException as exc:
                        publication_error = exc

                pointer_handles = native.pointer_open_handles()
                handle = pointer_handles[0] if len(pointer_handles) == 1 else -1
                transaction_artifacts = tuple(
                    sorted(
                        path.name
                        for path in final_dir.iterdir()
                        if path.name.startswith(f".{pointer.name}.")
                    )
                )
                self.assertEqual(
                    {
                        "error_type": (
                            type(publication_error).__name__
                            if publication_error is not None
                            else None
                        ),
                        "adapter_calls": tuple(adapter_calls),
                        "payload_calls": len(payload_calls),
                        "uuid_calls": len(uuid_calls),
                        "pointer_bytes": pointer.read_bytes(),
                        "transaction_artifacts": transaction_artifacts,
                        "pointer_open_count": len(pointer_handles),
                        "leaf_close_count": native.close_counts.get(handle, 0),
                    },
                    {
                        "error_type": "ValueError",
                        "adapter_calls": (),
                        "payload_calls": 0,
                        "uuid_calls": 0,
                        "pointer_bytes": mismatched_bytes,
                        "transaction_artifacts": (),
                        "pointer_open_count": 1,
                        "leaf_close_count": 1,
                    },
                )
            finally:
                native.close_remaining()

    def test_step2h_posix_observation_reuses_one_retained_leaf_for_read_and_snapshots(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            pointer = final_dir / "video_delivery.json"
            pointer_bytes = b'{"sentinel":"posix-native"}\n'
            pointer.write_bytes(pointer_bytes)
            pointer_metadata = os.stat(pointer)
            root_metadata = os.stat(run_dir)
            root_fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
            parent_fd = os.open(final_dir, os.O_RDONLY | os.O_DIRECTORY)
            parent_close_calls: list[object] = []
            validation_calls: list[object] = []
            leaf_open_attempts: list[object] = []
            leaf_descriptors: list[int] = []
            leaf_close_counts: dict[int, int] = {}
            snapshot_descriptors: list[int] = []
            read_descriptors: list[int] = []

            def close_parent() -> None:
                parent_close_calls.append(object())
                os.close(parent_fd)

            parent = video_pointer_transaction_module.RetainedParent(
                native_parent=parent_fd,
                platform="posix",
                validate_callback=lambda: validation_calls.append(object()),
                close_callback=close_parent,
            )
            accessor = attempt_candidates_module.SecureRunMemberAccessor(
                root_reference=root_fd,
                root_identity=(
                    int(root_metadata.st_dev),
                    int(root_metadata.st_ino),
                ),
                accepted_roots=(run_dir,),
                requested_root=run_dir,
                canonical_root=run_dir,
                binding=None,
            )
            real_open = os.open
            real_close = os.close
            real_stat = os.stat
            backend_type = video_pointer_transaction_module._VideoPointerBackend
            real_snapshot = backend_type._posix_snapshot
            real_read_bytes = backend_type.read_bytes
            observation_error: BaseException | None = None
            snapshot = None

            def open_leaf_once(path, *args, **kwargs):
                if (
                    os.fspath(path) == pointer.name
                    and kwargs.get("dir_fd") == parent_fd
                ):
                    leaf_open_attempts.append(object())
                    if len(leaf_open_attempts) != 1:
                        raise AssertionError(
                            "video pointer observation reopened the leaf by path"
                        )
                    descriptor = real_open(path, *args, **kwargs)
                    leaf_descriptors.append(descriptor)
                    return descriptor
                return real_open(path, *args, **kwargs)

            def record_close(descriptor):
                if descriptor in leaf_descriptors:
                    leaf_close_counts[descriptor] = (
                        leaf_close_counts.get(descriptor, 0) + 1
                    )
                return real_close(descriptor)

            def reject_pointer_path_stat(path, *args, **kwargs):
                if (
                    os.fspath(path) == pointer.name
                    and kwargs.get("dir_fd") == parent_fd
                ):
                    raise AssertionError(
                        "video pointer observation fell back to path stat"
                    )
                return real_stat(path, *args, **kwargs)

            def record_snapshot(backend, descriptor, *, max_bytes):
                snapshot_descriptors.append(descriptor)
                return real_snapshot(
                    backend,
                    descriptor,
                    max_bytes=max_bytes,
                )

            def record_read(backend, retained, *, max_bytes):
                read_descriptors.append(retained.handle)
                return real_read_bytes(
                    backend,
                    retained,
                    max_bytes=max_bytes,
                )

            try:
                with (
                    patch.object(os, "open", side_effect=open_leaf_once),
                    patch.object(os, "close", side_effect=record_close),
                    patch.object(os, "stat", side_effect=reject_pointer_path_stat),
                    patch.object(
                        backend_type,
                        "_posix_snapshot",
                        autospec=True,
                        side_effect=record_snapshot,
                    ),
                    patch.object(
                        backend_type,
                        "read_bytes",
                        autospec=True,
                        side_effect=record_read,
                    ),
                ):
                    try:
                        snapshot = accessor._read_video_pointer_from_parent(
                            parent,
                            Path("final/video_delivery.json"),
                            label="Step 2h POSIX Video pointer",
                        )
                    except BaseException as exc:
                        observation_error = exc

                expected_native_stable = {
                    "dev": int(pointer_metadata.st_dev),
                    "ino": int(pointer_metadata.st_ino),
                    "mode": int(pointer_metadata.st_mode),
                    "nlink": int(pointer_metadata.st_nlink),
                    "size": len(pointer_bytes),
                    "mtime_ns": int(pointer_metadata.st_mtime_ns),
                    "sha256": hashlib.sha256(pointer_bytes).hexdigest(),
                }
                leaf_descriptor = (
                    leaf_descriptors[0] if len(leaf_descriptors) == 1 else -1
                )
                expected_prior = (
                    accessor._video_pointer_expected_prior(
                        snapshot,
                        platform="posix",
                    )
                    if snapshot is not None
                    else None
                )
                self.assertEqual(
                    {
                        "error_type": (
                            type(observation_error).__name__
                            if observation_error is not None
                            else None
                        ),
                        "leaf_open_attempts": len(leaf_open_attempts),
                        "leaf_open_count": len(leaf_descriptors),
                        "snapshot_descriptors": tuple(snapshot_descriptors),
                        "read_descriptors": tuple(read_descriptors),
                        "leaf_close_count": leaf_close_counts.get(
                            leaf_descriptor,
                            0,
                        ),
                        "snapshot_data": getattr(snapshot, "data", None),
                        "native_platform": getattr(
                            snapshot,
                            "native_platform",
                            None,
                        ),
                        "native_stable": dict(
                            getattr(snapshot, "native_stable", None) or {}
                        ),
                        "expected_prior": expected_prior,
                        "parent_transferred": parent.transferred,
                        "parent_closed": parent.closed,
                        "parent_close_calls": len(parent_close_calls),
                        "parent_validated": bool(validation_calls),
                    },
                    {
                        "error_type": None,
                        "leaf_open_attempts": 1,
                        "leaf_open_count": 1,
                        "snapshot_descriptors": (
                            leaf_descriptor,
                            leaf_descriptor,
                        ),
                        "read_descriptors": (leaf_descriptor,),
                        "leaf_close_count": 1,
                        "snapshot_data": pointer_bytes,
                        "native_platform": "posix",
                        "native_stable": expected_native_stable,
                        "expected_prior": {
                            "platform": "posix",
                            "stable": expected_native_stable,
                        },
                        "parent_transferred": False,
                        "parent_closed": False,
                        "parent_close_calls": 0,
                        "parent_validated": True,
                    },
                )
                self.assertEqual(
                    frozenset(expected_native_stable),
                    self.POSIX_STABLE_KEYS,
                )
            finally:
                for descriptor in leaf_descriptors:
                    try:
                        real_close(descriptor)
                    except OSError:
                        pass
                parent.close()
                os.close(root_fd)

    def test_step2h_expected_prior_requires_exact_native_stable_keysets(
        self,
    ) -> None:
        platform_stables = {
            "posix": {
                "dev": 17,
                "ino": 23,
                "mode": stat.S_IFREG | 0o600,
                "nlink": 1,
                "size": 12,
                "mtime_ns": 29,
                "sha256": "a" * 64,
            },
            "windows": {
                "volume_serial_number": 0xFEDCBA9876543210,
                "file_id": "1032547698badcfe0123456789abcdef",
                "file_attributes": 0x20,
                "reparse": False,
                "nlink": 1,
                "size": 12,
                "last_write_time": 31,
                "sha256": "b" * 64,
            },
        }
        for platform, stable in platform_stables.items():
            with self.subTest(platform=platform):
                parent = video_pointer_transaction_module.RetainedParent(
                    native_parent=0 if platform == "posix" else Path("."),
                    platform=platform,
                    validate_callback=lambda: None,
                    close_callback=lambda: None,
                )
                backend = video_pointer_transaction_module._VideoPointerBackend(
                    parent
                )

                def retained(observed_stable):
                    return video_pointer_transaction_module._RetainedPublicationFile(
                        "video_delivery.json",
                        object(),
                        {
                            "platform": platform,
                            "stable": dict(observed_stable),
                            "audit": {"excluded_from_stability": 37},
                        },
                        lambda _handle: None,
                    )

                exact_expected = {
                    "platform": platform,
                    "stable": dict(stable),
                }
                self.assertTrue(
                    video_pointer_transaction_module._expected_prior_matches(
                        backend,
                        expected_prior=exact_expected,
                        observed_prior=retained(stable),
                    )
                )
                for missing_key in stable:
                    missing = dict(stable)
                    missing.pop(missing_key)
                    self.assertFalse(
                        video_pointer_transaction_module._expected_prior_matches(
                            backend,
                            expected_prior={
                                "platform": platform,
                                "stable": missing,
                            },
                            observed_prior=retained(stable),
                        ),
                        missing_key,
                    )
                expected_with_extra = dict(stable, creation_time=41)
                observed_with_extra = dict(stable, creation_time=41)
                self.assertEqual(
                    (
                        video_pointer_transaction_module._expected_prior_matches(
                            backend,
                            expected_prior={
                                "platform": platform,
                                "stable": expected_with_extra,
                            },
                            observed_prior=retained(observed_with_extra),
                        ),
                        video_pointer_transaction_module._expected_prior_matches(
                            backend,
                            expected_prior=exact_expected,
                            observed_prior=retained(observed_with_extra),
                        ),
                    ),
                    (False, False),
                )

    def _run_windows_metadata_mutation(self, mutation: str) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            pointer = final_dir / "video_delivery.json"
            prior_bytes = b'{"sentinel":"metadata-prior"}\n'
            pointer.write_bytes(prior_bytes)
            native = _Step2hFakeWindowsRetainedHandleAPI(
                pointer,
                use_python_identity=True,
            )
            adapter_calls: list[str] = []
            payload_calls: list[object] = []
            pre_begin_order: list[tuple[int, int]] = []
            match_records: list[dict[str, object]] = []
            uuid_calls: list[object] = []
            retained_parents = []
            publication_error: BaseException | None = None
            real_stage = (
                video_pointer_transaction_module.stage_video_delivery_pointer
            )
            real_expected_prior_matches = (
                video_pointer_transaction_module._expected_prior_matches
            )
            real_uuid4 = video_pointer_transaction_module.uuid.uuid4

            def payload_factory(snapshot):
                payload_calls.append(snapshot)
                return {"replacement": mutation}

            def mutate_before_real_begin(
                parent,
                encoded,
                *,
                expected_prior,
                recovery_warnings=(),
            ):
                retained_parents.append(parent)
                pre_begin_order.append((len(adapter_calls), len(payload_calls)))
                if mutation == "last_write_time":
                    native.last_write_time += 1
                elif mutation == "file_attributes":
                    native.file_attributes ^= 0x1
                elif mutation == "reparse":
                    native.file_attributes |= 0x400
                else:
                    raise AssertionError(f"unknown mutation {mutation}")
                return real_stage(
                    parent,
                    encoded,
                    expected_prior=expected_prior,
                    recovery_warnings=recovery_warnings,
                )

            def record_expected_prior_match(
                backend,
                *,
                expected_prior,
                observed_prior,
            ):
                matched = real_expected_prior_matches(
                    backend,
                    expected_prior=expected_prior,
                    observed_prior=observed_prior,
                )
                match_records.append({
                    "matched": matched,
                    "expected": {
                        "platform": expected_prior.get("platform"),
                        "stable": dict(expected_prior.get("stable") or {}),
                    },
                    "observed": {
                        "platform": observed_prior.snapshot.get("platform"),
                        "stable": dict(
                            observed_prior.snapshot.get("stable") or {}
                        ),
                    },
                })
                return matched

            def record_uuid4():
                uuid_calls.append(object())
                return real_uuid4()

            try:
                with ExitStack() as stack:
                    self._enter_mocked_windows(stack, native)
                    stack.enter_context(
                        patch.object(
                            attempt_candidates_module,
                            "_video_delivery_pointer_adapter_hook",
                            side_effect=lambda event, **_details: adapter_calls.append(
                                event
                            ),
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            attempt_candidates_module,
                            "stage_video_delivery_pointer",
                            side_effect=mutate_before_real_begin,
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            video_pointer_transaction_module,
                            "_expected_prior_matches",
                            side_effect=record_expected_prior_match,
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            video_pointer_transaction_module.uuid,
                            "uuid4",
                            side_effect=record_uuid4,
                        )
                    )
                    try:
                        with attempt_candidates_module.secure_run_member_access(
                            run_dir
                        ) as accessor:
                            accessor.stage_video_delivery_pointer(
                                Path("final/video_delivery.json"),
                                label=f"Step 2h {mutation} mutation",
                                payload_factory=payload_factory,
                                invoke_adapter_hook=True,
                            )
                    except BaseException as exc:
                        publication_error = exc

                transaction_artifacts = tuple(
                    sorted(
                        path.name
                        for path in final_dir.iterdir()
                        if path.name.startswith(f".{pointer.name}.")
                    )
                )
                return {
                    "error": publication_error,
                    "adapter_calls": tuple(adapter_calls),
                    "payload_call_count": len(payload_calls),
                    "pre_begin_order": tuple(pre_begin_order),
                    "match_records": tuple(match_records),
                    "uuid_call_count": len(uuid_calls),
                    "pointer_bytes": pointer.read_bytes(),
                    "transaction_artifacts": transaction_artifacts,
                    "parent_transferred": (
                        retained_parents[0].transferred
                        if retained_parents
                        else None
                    ),
                    "parent_closed": (
                        retained_parents[0].closed if retained_parents else None
                    ),
                    "all_native_handles_closed": not native.handles,
                    "native_close_counts": tuple(native.close_counts.values()),
                }
            finally:
                native.close_remaining()

    def test_step2h_windows_metadata_mutation_fails_in_expected_prior_match(
        self,
    ) -> None:
        for mutation in ("last_write_time", "file_attributes"):
            with self.subTest(mutation=mutation):
                outcome = self._run_windows_metadata_mutation(mutation)
                self.assertEqual(outcome["adapter_calls"], ("classified_present",))
                self.assertEqual(outcome["payload_call_count"], 1)
                self.assertEqual(outcome["pre_begin_order"], ((1, 1),))
                records = outcome["match_records"]
                self.assertEqual(
                    tuple(record["matched"] for record in records),
                    (False,),
                )
                record = records[0]
                expected_stable = record["expected"]["stable"]
                observed_stable = record["observed"]["stable"]
                self.assertEqual(
                    frozenset(expected_stable),
                    self.WINDOWS_STABLE_KEYS,
                )
                self.assertEqual(
                    frozenset(observed_stable),
                    self.WINDOWS_STABLE_KEYS,
                )
                self.assertEqual(
                    {
                        key
                        for key in self.WINDOWS_STABLE_KEYS
                        if expected_stable[key] != observed_stable[key]
                    },
                    {mutation},
                )
                error = outcome["error"]
                self.assertIsInstance(error, ValueError)
                self.assertIn(
                    "changed before transaction publication",
                    str(error),
                )
                self.assertEqual(outcome["uuid_call_count"], 0)
                self.assertEqual(
                    outcome["pointer_bytes"],
                    b'{"sentinel":"metadata-prior"}\n',
                )
                self.assertEqual(outcome["transaction_artifacts"], ())
                self.assertFalse(outcome["parent_transferred"])
                self.assertTrue(outcome["parent_closed"])
                self.assertTrue(outcome["all_native_handles_closed"])
                self.assertTrue(
                    all(count == 1 for count in outcome["native_close_counts"])
                )

    def test_step2h_windows_reparse_mutation_remains_a_negative_control(
        self,
    ) -> None:
        outcome = self._run_windows_metadata_mutation("reparse")
        self.assertEqual(outcome["adapter_calls"], ("classified_present",))
        self.assertEqual(outcome["payload_call_count"], 1)
        self.assertEqual(outcome["pre_begin_order"], ((1, 1),))
        self.assertEqual(outcome["match_records"], ())
        self.assertIsInstance(outcome["error"], ValueError)
        self.assertIn("unsafe", str(outcome["error"]))
        self.assertEqual(outcome["uuid_call_count"], 0)
        self.assertEqual(
            outcome["pointer_bytes"],
            b'{"sentinel":"metadata-prior"}\n',
        )
        self.assertEqual(outcome["transaction_artifacts"], ())
        self.assertFalse(outcome["parent_transferred"])
        self.assertTrue(outcome["parent_closed"])
        self.assertTrue(outcome["all_native_handles_closed"])
        self.assertTrue(
            all(count == 1 for count in outcome["native_close_counts"])
        )

    def test_step2h_uuid_constructor_failure_closes_prior_and_parent_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            final_dir = Path(tmp) / "final"
            final_dir.mkdir()
            pointer = final_dir / "video_delivery.json"
            prior_bytes = b'{"sentinel":"uuid-prior"}\n'
            pointer.write_bytes(prior_bytes)
            pointer_metadata = os.stat(pointer)
            native = _Step2hFakeWindowsRetainedHandleAPI(pointer)
            validation_calls: list[object] = []
            parent_close_calls: list[object] = []
            uuid_calls: list[object] = []
            parent = video_pointer_transaction_module.RetainedParent(
                native_parent=final_dir,
                platform="windows",
                validate_callback=lambda: validation_calls.append(object()),
                close_callback=lambda: parent_close_calls.append(object()),
                windows_user_sid="S-1-5-21-step2h-test",
            )
            expected_prior = {
                "platform": "windows",
                "stable": {
                    "volume_serial_number": native.volume_serial_number,
                    "file_id": native.file_id.hex(),
                    "file_attributes": native.file_attributes,
                    "reparse": False,
                    "nlink": int(pointer_metadata.st_nlink),
                    "size": len(prior_bytes),
                    "last_write_time": native.last_write_time,
                    "sha256": hashlib.sha256(prior_bytes).hexdigest(),
                },
            }
            constructor_error: BaseException | None = None

            def fail_uuid4():
                uuid_calls.append(object())
                raise RuntimeError("injected transaction UUID constructor failure")

            try:
                with (
                    patch.object(
                        video_pointer_transaction_module,
                        "_windows_native_handle_api_factory",
                        return_value=native,
                    ),
                    patch.object(
                        video_pointer_transaction_module.uuid,
                        "uuid4",
                        side_effect=fail_uuid4,
                    ),
                ):
                    try:
                        video_pointer_transaction_module.stage_video_delivery_pointer(
                            parent,
                            b'{"replacement":"must-not-publish"}\n',
                            expected_prior=expected_prior,
                        )
                    except BaseException as exc:
                        constructor_error = exc

                pointer_handles = native.pointer_open_handles()
                prior_handle = (
                    pointer_handles[0] if len(pointer_handles) == 1 else -1
                )
                transaction_artifacts = tuple(
                    sorted(
                        path.name
                        for path in final_dir.iterdir()
                        if path.name.startswith(f".{pointer.name}.")
                    )
                )
                self.assertEqual(
                    {
                        "error_type": (
                            type(constructor_error).__name__
                            if constructor_error is not None
                            else None
                        ),
                        "error_text": str(constructor_error),
                        "uuid_call_count": len(uuid_calls),
                        "prior_open_count": len(pointer_handles),
                        "prior_close_count": native.close_counts.get(
                            prior_handle,
                            0,
                        ),
                        "parent_close_count": len(parent_close_calls),
                        "parent_transferred": parent.transferred,
                        "parent_closed": parent.closed,
                        "parent_validated": bool(validation_calls),
                        "pointer_bytes": pointer.read_bytes(),
                        "transaction_artifacts": transaction_artifacts,
                    },
                    {
                        "error_type": "RuntimeError",
                        "error_text": (
                            "injected transaction UUID constructor failure"
                        ),
                        "uuid_call_count": 1,
                        "prior_open_count": 1,
                        "prior_close_count": 1,
                        "parent_close_count": 1,
                        "parent_transferred": False,
                        "parent_closed": True,
                        "parent_validated": True,
                        "pointer_bytes": prior_bytes,
                        "transaction_artifacts": (),
                    },
                )
            finally:
                native.close_remaining()

    @staticmethod
    def _exception_chain(error: BaseException | None) -> tuple[BaseException, ...]:
        chain: list[BaseException] = []
        seen: set[int] = set()
        current = error
        while current is not None and id(current) not in seen:
            chain.append(current)
            seen.add(id(current))
            current = current.__cause__
        return tuple(chain)

    def _run_uuid_constructor_cleanup_case(
        self,
        platform: str,
        *,
        root_error: BaseException,
        prior_close_error: BaseException | None = None,
        parent_close_error: BaseException | None = None,
        caller_recovery_warnings: tuple[str, ...] = (),
        internal_recovery_warnings: tuple[str, ...] = (),
        top_level_recovery_warnings: tuple[str, ...] | None = None,
        expected_prior_matches: bool = True,
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as tmp:
            final_dir = Path(tmp) / "final"
            final_dir.mkdir()
            pointer = final_dir / "video_delivery.json"
            prior_bytes = b'{"sentinel":"uuid-cleanup-prior"}\n'
            pointer.write_bytes(prior_bytes)
            pointer_metadata = os.stat(pointer)
            parent_close_calls: list[object] = []
            validation_calls: list[object] = []
            prior_close_attempts: list[object] = []
            uuid_calls: list[object] = []
            phase_calls: list[str] = []
            retained_prior = []
            raw_prior_closers = []
            construction_aggregates: list[BaseException] = []
            parent_resource_open = True
            native = None

            if platform == "posix":
                parent_fd = os.open(final_dir, os.O_RDONLY | os.O_DIRECTORY)
                native_parent = parent_fd
                expected_stable = {
                    "dev": int(pointer_metadata.st_dev),
                    "ino": int(pointer_metadata.st_ino),
                    "mode": int(pointer_metadata.st_mode),
                    "nlink": int(pointer_metadata.st_nlink),
                    "size": len(prior_bytes),
                    "mtime_ns": int(pointer_metadata.st_mtime_ns),
                    "sha256": hashlib.sha256(prior_bytes).hexdigest(),
                }
            elif platform == "windows":
                native = _Step2hFakeWindowsRetainedHandleAPI(pointer)
                native_parent = final_dir
                expected_stable = {
                    "volume_serial_number": native.volume_serial_number,
                    "file_id": native.file_id.hex(),
                    "file_attributes": native.file_attributes,
                    "reparse": False,
                    "nlink": int(pointer_metadata.st_nlink),
                    "size": len(prior_bytes),
                    "last_write_time": native.last_write_time,
                    "sha256": hashlib.sha256(prior_bytes).hexdigest(),
                }
            else:
                raise AssertionError(f"unknown platform {platform}")

            expected_prior_stable = dict(expected_stable)
            if not expected_prior_matches:
                expected_prior_stable["sha256"] = "0" * 64

            def close_parent() -> None:
                nonlocal parent_resource_open
                parent_close_calls.append(object())
                if parent_close_error is not None:
                    raise parent_close_error
                if platform == "posix":
                    os.close(parent_fd)
                    parent_resource_open = False

            parent = video_pointer_transaction_module.RetainedParent(
                native_parent=native_parent,
                platform=platform,
                validate_callback=lambda: validation_calls.append(object()),
                close_callback=close_parent,
                windows_user_sid=(
                    "S-1-5-21-step2h-test" if platform == "windows" else None
                ),
            )
            real_open = video_pointer_transaction_module._VideoPointerBackend.open
            real_construction_cleanup_error = (
                video_pointer_transaction_module._construction_cleanup_error
            )

            def record_prior_open(backend, name, *, max_bytes=None):
                retained = real_open(backend, name, max_bytes=max_bytes)
                if name == pointer.name:
                    retained_prior.append(retained)
                    raw_close = retained.close_callback
                    raw_prior_closers.append(raw_close)

                    def close_prior(handle) -> None:
                        prior_close_attempts.append(handle)
                        if prior_close_error is not None:
                            raise prior_close_error
                        raw_close(handle)

                    retained.close_callback = close_prior
                return retained

            def fail_uuid4():
                uuid_calls.append(object())
                raise root_error

            def construction_cleanup_error_with_warnings(
                original_error: BaseException,
                cleanup_errors: tuple[tuple[str, BaseException], ...],
            ) -> RuntimeError:
                aggregate = real_construction_cleanup_error(
                    original_error,
                    cleanup_errors,
                )
                aggregate.recovery_warnings = top_level_recovery_warnings
                construction_aggregates.append(aggregate)
                return aggregate

            caught: BaseException | None = None
            outcome: dict[str, object]
            try:
                with ExitStack() as stack:
                    stack.enter_context(
                        patch.object(
                            video_pointer_transaction_module._VideoPointerBackend,
                            "open",
                            autospec=True,
                            side_effect=record_prior_open,
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            video_pointer_transaction_module.uuid,
                            "uuid4",
                            side_effect=fail_uuid4,
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            video_pointer_transaction_module,
                            "_video_pointer_transaction_phase_hook",
                            side_effect=lambda phase, **_details: phase_calls.append(
                                phase
                            ),
                        )
                    )
                    if top_level_recovery_warnings is not None:
                        stack.enter_context(
                            patch.object(
                                video_pointer_transaction_module,
                                "_construction_cleanup_error",
                                autospec=True,
                                side_effect=(
                                    construction_cleanup_error_with_warnings
                                ),
                            )
                        )
                    if internal_recovery_warnings:
                        stack.enter_context(
                            patch.object(
                                video_pointer_transaction_module,
                                "_recover_video_pointer_transactions",
                                return_value=list(internal_recovery_warnings),
                            )
                        )
                    if native is not None:
                        stack.enter_context(
                            patch.object(
                                video_pointer_transaction_module,
                                "_windows_native_handle_api_factory",
                                return_value=native,
                            )
                        )
                    try:
                        video_pointer_transaction_module.stage_video_delivery_pointer(
                            parent,
                            b'{"replacement":"must-not-publish"}\n',
                            expected_prior={
                                "platform": platform,
                                "stable": expected_prior_stable,
                            },
                            recovery_warnings=caller_recovery_warnings,
                        )
                    except BaseException as exc:
                        caught = exc

                transaction_artifacts = tuple(
                    sorted(
                        path.name
                        for path in final_dir.iterdir()
                        if path.name.startswith(f".{pointer.name}.")
                    )
                )
                outcome = {
                    "error": caught,
                    "error_type": type(caught) if caught is not None else None,
                    "error_text": str(caught),
                    "cleanup_errors": getattr(caught, "cleanup_errors", None),
                    "recovery_warnings": getattr(
                        caught,
                        "recovery_warnings",
                        None,
                    ),
                    "cause": caught.__cause__ if caught is not None else None,
                    "chain": self._exception_chain(caught),
                    "construction_aggregates": tuple(construction_aggregates),
                    "uuid_call_count": len(uuid_calls),
                    "phase_calls": tuple(phase_calls),
                    "prior_open_count": len(retained_prior),
                    "prior_close_attempt_count": len(prior_close_attempts),
                    "parent_close_count": len(parent_close_calls),
                    "parent_transferred": parent.transferred,
                    "parent_closed": parent.closed,
                    "parent_validated": bool(validation_calls),
                    "pointer_bytes": pointer.read_bytes(),
                    "transaction_artifacts": transaction_artifacts,
                }
            finally:
                for retained, raw_close in zip(
                    retained_prior,
                    raw_prior_closers,
                    strict=True,
                ):
                    handle = retained.handle
                    if platform == "posix":
                        try:
                            os.fstat(handle)
                        except OSError:
                            continue
                        raw_close(handle)
                    elif native is not None and handle in native.handles:
                        raw_close(handle)
                if platform == "posix" and parent_resource_open:
                    os.close(parent_fd)
            return outcome

    def _run_synthetic_begin_cleanup_case(
        self,
        *,
        root_error: BaseException,
        new_close_error: BaseException,
        prior_close_error: BaseException,
        extra_close_errors: tuple[BaseException, ...] = (),
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as tmp:
            final_dir = Path(tmp)
            pointer = final_dir / "video_delivery.json"
            prior_bytes = b'{"sentinel":"synthetic-prior"}\n'
            pointer.write_bytes(prior_bytes)
            close_order: list[str] = []
            close_counts: dict[str, int] = {}
            transfer_calls: list[object] = []
            phase_attempts: list[str] = []
            phase_hook_calls: list[str] = []
            txid = "1" * 32
            stable = {
                "dev": 1,
                "ino": 2,
                "mode": stat.S_IFREG | 0o600,
                "nlink": 1,
                "size": len(prior_bytes),
                "mtime_ns": 3,
                "sha256": hashlib.sha256(prior_bytes).hexdigest(),
            }

            def retained_file(
                name: str,
                role: str,
                close_error: BaseException,
            ):
                def fail_close(_handle) -> None:
                    close_order.append(role)
                    close_counts[role] = close_counts.get(role, 0) + 1
                    raise close_error

                return video_pointer_transaction_module._RetainedPublicationFile(
                    name=name,
                    handle=object(),
                    snapshot={"platform": "posix", "stable": dict(stable)},
                    close_callback=fail_close,
                )

            prior = retained_file(pointer.name, "prior_file", prior_close_error)
            extras = tuple(
                retained_file(f"extra-{index}", f"extra_{index}", error)
                for index, error in enumerate(extra_close_errors)
            )
            new_holder = []

            class SyntheticParent:
                def transfer(self) -> None:
                    transfer_calls.append(object())

            class SyntheticBackend:
                is_posix = True
                retained_parent = SyntheticParent()

                def try_open(self, name):
                    if name != pointer.name:
                        raise AssertionError(f"unexpected open {name}")
                    return prior

                def create(self, name, data):
                    new_path = final_dir / name
                    new_path.write_bytes(data)
                    retained = retained_file(name, "new_file", new_close_error)
                    new_holder.append(retained)
                    return retained

            real_remember = (
                video_pointer_transaction_module._VideoPointerTransaction._remember
            )
            extras_inserted = False

            def remember_with_extras(transaction, retained) -> None:
                nonlocal extras_inserted
                real_remember(transaction, retained)
                if retained is prior and extras and not extras_inserted:
                    extras_inserted = True
                    for extra in extras:
                        real_remember(transaction, extra)

            def fail_before_phase(transaction, phase, **_details) -> None:
                phase_attempts.append(phase)
                raise root_error

            caught: BaseException | None = None
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(
                        video_pointer_transaction_module,
                        "_recover_video_pointer_transactions",
                        return_value=[],
                    )
                )
                stack.enter_context(
                    patch.object(
                        video_pointer_transaction_module.uuid,
                        "uuid4",
                        return_value=SimpleNamespace(hex=txid),
                    )
                )
                stack.enter_context(
                    patch.object(
                        video_pointer_transaction_module._VideoPointerTransaction,
                        "_remember",
                        autospec=True,
                        side_effect=remember_with_extras,
                    )
                )
                stack.enter_context(
                    patch.object(
                        video_pointer_transaction_module._VideoPointerTransaction,
                        "append_phase",
                        autospec=True,
                        side_effect=fail_before_phase,
                    )
                )
                stack.enter_context(
                    patch.object(
                        video_pointer_transaction_module,
                        "_video_pointer_transaction_phase_hook",
                        side_effect=lambda phase, **_details: phase_hook_calls.append(
                            phase
                        ),
                    )
                )
                try:
                    video_pointer_transaction_module._VideoPointerTransaction.begin(
                        SyntheticBackend(),
                        b'{"replacement":"synthetic-new"}\n',
                        expected_prior={
                            "platform": "posix",
                            "stable": stable,
                        },
                    )
                except BaseException as exc:
                    caught = exc

            cleanup_errors = getattr(caught, "cleanup_errors", None)
            structured_exceptions = ()
            if type(cleanup_errors) is tuple and all(
                type(item) is tuple and len(item) == 2
                for item in cleanup_errors
            ):
                structured_exceptions = tuple(
                    item[1] for item in cleanup_errors
                )
            artifact_names = tuple(sorted(path.name for path in final_dir.iterdir()))
            return {
                "error": caught,
                "error_type": type(caught) if caught is not None else None,
                "error_text": str(caught),
                "cleanup_errors": cleanup_errors,
                "structured_exceptions": structured_exceptions,
                "cause": caught.__cause__ if caught is not None else None,
                "close_order": tuple(close_order),
                "close_counts": dict(close_counts),
                "phase_attempts": tuple(phase_attempts),
                "phase_hook_calls": tuple(phase_hook_calls),
                "transfer_count": len(transfer_calls),
                "pointer_bytes": pointer.read_bytes(),
                "artifact_names": artifact_names,
                "new_name": new_holder[0].name if new_holder else None,
            }

    def test_step2h_uuid_prior_close_failure_is_aggregated_for_both_platforms(
        self,
    ) -> None:
        for platform in ("posix", "windows"):
            with self.subTest(platform=platform):
                root_error = ValueError(
                    f"injected {platform} UUID constructor failure"
                )
                close_error = OSError(
                    f"injected {platform} prior leaf close failure"
                )
                outcome = self._run_uuid_constructor_cleanup_case(
                    platform,
                    root_error=root_error,
                    prior_close_error=close_error,
                )
                message = str(outcome["error"])
                self.assertEqual(
                    {
                        "error_type": outcome["error_type"],
                        "cleanup_errors": outcome["cleanup_errors"],
                        "cause_is_root": outcome["cause"] is root_error,
                        "message_has_root": str(root_error) in message,
                        "message_has_role": "prior_file" in message,
                        "message_has_close": str(close_error) in message,
                        "prior_open_count": outcome["prior_open_count"],
                        "prior_close_attempt_count": outcome[
                            "prior_close_attempt_count"
                        ],
                        "parent_close_count": outcome["parent_close_count"],
                        "parent_transferred": outcome["parent_transferred"],
                        "parent_closed": outcome["parent_closed"],
                        "pointer_bytes": outcome["pointer_bytes"],
                        "transaction_artifacts": outcome[
                            "transaction_artifacts"
                        ],
                    },
                    {
                        "error_type": RuntimeError,
                        "cleanup_errors": (("prior_file", close_error),),
                        "cause_is_root": True,
                        "message_has_root": True,
                        "message_has_role": True,
                        "message_has_close": True,
                        "prior_open_count": 1,
                        "prior_close_attempt_count": 1,
                        "parent_close_count": 1,
                        "parent_transferred": False,
                        "parent_closed": True,
                        "pointer_bytes": b'{"sentinel":"uuid-cleanup-prior"}\n',
                        "transaction_artifacts": (),
                    },
                )

    def test_step2h_uuid_clean_cleanup_reraises_the_same_error_object(
        self,
    ) -> None:
        for platform in ("posix", "windows"):
            with self.subTest(platform=platform):
                root_error = ValueError(
                    f"injected clean {platform} UUID constructor failure"
                )
                outcome = self._run_uuid_constructor_cleanup_case(
                    platform,
                    root_error=root_error,
                )
                self.assertIs(outcome["error"], root_error)
                self.assertEqual(outcome["prior_close_attempt_count"], 1)
                self.assertEqual(outcome["parent_close_count"], 1)
                self.assertFalse(outcome["parent_transferred"])
                self.assertTrue(outcome["parent_closed"])
                self.assertEqual(outcome["transaction_artifacts"], ())

    def test_step2h_begin_collects_both_leaf_close_failures_in_reverse_order(
        self,
    ) -> None:
        root_error = ValueError("injected failure before prepared phase")
        new_close_error = OSError("injected new-file close failure")
        prior_close_error = OSError("injected prior-file close failure")
        outcome = self._run_synthetic_begin_cleanup_case(
            root_error=root_error,
            new_close_error=new_close_error,
            prior_close_error=prior_close_error,
        )
        message = str(outcome["error"])
        expected_new_name = f".video_delivery.json.{'1' * 32}.new"
        self.assertEqual(
            {
                "error_type": outcome["error_type"],
                "cleanup_errors": outcome["cleanup_errors"],
                "cause_is_root": outcome["cause"] is root_error,
                "message_has_root": str(root_error) in message,
                "message_has_new_role": "new_file" in message,
                "message_has_prior_role": "prior_file" in message,
                "message_has_new_error": str(new_close_error) in message,
                "message_has_prior_error": str(prior_close_error) in message,
                "close_order": outcome["close_order"],
                "close_counts": outcome["close_counts"],
                "phase_attempts": outcome["phase_attempts"],
                "phase_hook_calls": outcome["phase_hook_calls"],
                "transfer_count": outcome["transfer_count"],
                "pointer_bytes": outcome["pointer_bytes"],
                "artifact_names": outcome["artifact_names"],
                "new_name": outcome["new_name"],
            },
            {
                "error_type": RuntimeError,
                "cleanup_errors": (
                    ("new_file", new_close_error),
                    ("prior_file", prior_close_error),
                ),
                "cause_is_root": True,
                "message_has_root": True,
                "message_has_new_role": True,
                "message_has_prior_role": True,
                "message_has_new_error": True,
                "message_has_prior_error": True,
                "close_order": ("new_file", "prior_file"),
                "close_counts": {"new_file": 1, "prior_file": 1},
                "phase_attempts": ("prepared",),
                "phase_hook_calls": (),
                "transfer_count": 0,
                "pointer_bytes": b'{"sentinel":"synthetic-prior"}\n',
                "artifact_names": (expected_new_name, "video_delivery.json"),
                "new_name": expected_new_name,
            },
        )

    def test_step2h_stage_collects_leaf_and_parent_close_failures(
        self,
    ) -> None:
        root_error = ValueError("injected stage constructor failure")
        leaf_close_error = OSError("injected stage prior leaf close failure")
        parent_close_error = OSError("injected retained parent close failure")
        outcome = self._run_uuid_constructor_cleanup_case(
            "windows",
            root_error=root_error,
            prior_close_error=leaf_close_error,
            parent_close_error=parent_close_error,
        )
        message = str(outcome["error"])
        self.assertEqual(
            {
                "error_type": outcome["error_type"],
                "cleanup_errors_type": type(outcome["cleanup_errors"]),
                "cleanup_errors": outcome["cleanup_errors"],
                "chain_reaches_root": root_error in outcome["chain"],
                "message_has_root": str(root_error) in message,
                "message_has_leaf_role": "prior_file" in message,
                "message_has_parent_role": "retained_parent" in message,
                "message_has_leaf_error": str(leaf_close_error) in message,
                "message_has_parent_error": str(parent_close_error) in message,
                "bounded_message": len(message) <= 2000,
                "prior_close_attempt_count": outcome[
                    "prior_close_attempt_count"
                ],
                "parent_close_count": outcome["parent_close_count"],
                "parent_transferred": outcome["parent_transferred"],
                "parent_closed": outcome["parent_closed"],
                "pointer_bytes": outcome["pointer_bytes"],
                "transaction_artifacts": outcome["transaction_artifacts"],
            },
            {
                "error_type": RuntimeError,
                "cleanup_errors_type": tuple,
                "cleanup_errors": (
                    ("prior_file", leaf_close_error),
                    ("retained_parent", parent_close_error),
                ),
                "chain_reaches_root": True,
                "message_has_root": True,
                "message_has_leaf_role": True,
                "message_has_parent_role": True,
                "message_has_leaf_error": True,
                "message_has_parent_error": True,
                "bounded_message": True,
                "prior_close_attempt_count": 1,
                "parent_close_count": 1,
                "parent_transferred": False,
                "parent_closed": True,
                "pointer_bytes": b'{"sentinel":"uuid-cleanup-prior"}\n',
                "transaction_artifacts": (),
            },
        )

    def test_step2h_constructor_cleanup_diagnostics_are_bounded(self) -> None:
        root_prefix = "ROOT|" + "r" * (1000 - len("ROOT|"))
        new_prefix = "NEW|" + "n" * (256 - len("NEW|"))
        prior_prefix = "PRIOR|" + "p" * (256 - len("PRIOR|"))
        root_error = ValueError(root_prefix + "ROOT_OVERFLOW")
        new_close_error = OSError(new_prefix + "NEW_OVERFLOW")
        prior_close_error = OSError(prior_prefix + "PRIOR_OVERFLOW")

        with self.subTest(boundary="individual-segments"):
            outcome = self._run_synthetic_begin_cleanup_case(
                root_error=root_error,
                new_close_error=new_close_error,
                prior_close_error=prior_close_error,
            )
            message = str(outcome["error"])
            self.assertEqual(
                {
                    "root_prefix_retained": root_prefix in message,
                    "root_overflow_removed": "ROOT_OVERFLOW" not in message,
                    "new_prefix_retained": new_prefix in message,
                    "new_overflow_removed": "NEW_OVERFLOW" not in message,
                    "prior_prefix_retained": prior_prefix in message,
                    "prior_overflow_removed": "PRIOR_OVERFLOW" not in message,
                    "combined_at_most_2000": len(message) <= 2000,
                    "cleanup_errors": outcome["cleanup_errors"],
                },
                {
                    "root_prefix_retained": True,
                    "root_overflow_removed": True,
                    "new_prefix_retained": True,
                    "new_overflow_removed": True,
                    "prior_prefix_retained": True,
                    "prior_overflow_removed": True,
                    "combined_at_most_2000": True,
                    "cleanup_errors": (
                        ("new_file", new_close_error),
                        ("prior_file", prior_close_error),
                    ),
                },
            )

        with self.subTest(boundary="combined-display"):
            extra_errors = tuple(
                OSError(f"EXTRA_{index}|" + chr(97 + index) * 300)
                for index in range(6)
            )
            outcome = self._run_synthetic_begin_cleanup_case(
                root_error=root_error,
                new_close_error=new_close_error,
                prior_close_error=prior_close_error,
                extra_close_errors=extra_errors,
            )
            message = str(outcome["error"])
            expected_close_order = (
                "new_file",
                *(f"extra_{index}" for index in reversed(range(6))),
                "prior_file",
            )
            expected_exceptions = (
                new_close_error,
                *reversed(extra_errors),
                prior_close_error,
            )
            self.assertEqual(
                {
                    "message_length": len(message),
                    "trailing_ellipsis": message.endswith("..."),
                    "close_order": outcome["close_order"],
                    "one_attempt_each": set(outcome["close_counts"].values()),
                    "structured_exceptions": outcome[
                        "structured_exceptions"
                    ],
                },
                {
                    "message_length": 2000,
                    "trailing_ellipsis": True,
                    "close_order": expected_close_order,
                    "one_attempt_each": {1},
                    "structured_exceptions": expected_exceptions,
                },
            )

    def test_step2h_caller_warnings_survive_clean_constructor_cleanup(
        self,
    ) -> None:
        root_error = ValueError("injected clean constructor failure")
        raw_warnings = (
            "caller recovery warning one",
            "caller recovery warning one",
            "caller recovery warning two",
        )
        expected_warnings = (
            "caller recovery warning one",
            "caller recovery warning two",
        )
        outcome = self._run_uuid_constructor_cleanup_case(
            "posix",
            root_error=root_error,
            caller_recovery_warnings=raw_warnings,
        )

        self.assertEqual(
            {
                "same_error": outcome["error"] is root_error,
                "error_text": outcome["error_text"],
                "warnings_type": type(outcome["recovery_warnings"]),
                "warnings": outcome["recovery_warnings"],
                "cleanup_errors": outcome["cleanup_errors"],
                "cause": outcome["cause"],
                "uuid_call_count": outcome["uuid_call_count"],
                "phase_calls": outcome["phase_calls"],
                "prior_open_count": outcome["prior_open_count"],
                "prior_close_attempt_count": outcome[
                    "prior_close_attempt_count"
                ],
                "parent_close_count": outcome["parent_close_count"],
                "parent_transferred": outcome["parent_transferred"],
                "parent_closed": outcome["parent_closed"],
                "pointer_bytes": outcome["pointer_bytes"],
                "transaction_artifacts": outcome["transaction_artifacts"],
            },
            {
                "same_error": True,
                "error_text": str(root_error),
                "warnings_type": tuple,
                "warnings": expected_warnings,
                "cleanup_errors": None,
                "cause": None,
                "uuid_call_count": 1,
                "phase_calls": (),
                "prior_open_count": 1,
                "prior_close_attempt_count": 1,
                "parent_close_count": 1,
                "parent_transferred": False,
                "parent_closed": True,
                "pointer_bytes": b'{"sentinel":"uuid-cleanup-prior"}\n',
                "transaction_artifacts": (),
            },
        )

    def test_step2h_caller_warnings_survive_leaf_cleanup_aggregate(
        self,
    ) -> None:
        root_error = ValueError("injected caller-warning constructor failure")
        leaf_close_error = OSError("injected caller-warning leaf close failure")
        raw_warnings = (
            "caller aggregate warning",
            "caller aggregate warning",
            "caller aggregate warning two",
        )
        expected_warnings = (
            "caller aggregate warning",
            "caller aggregate warning two",
        )
        outcome = self._run_uuid_constructor_cleanup_case(
            "posix",
            root_error=root_error,
            prior_close_error=leaf_close_error,
            caller_recovery_warnings=raw_warnings,
        )
        message = str(outcome["error"])

        self.assertEqual(
            {
                "error_type": outcome["error_type"],
                "cleanup_errors": outcome["cleanup_errors"],
                "cause_is_root": outcome["cause"] is root_error,
                "warnings_type": type(outcome["recovery_warnings"]),
                "warnings": outcome["recovery_warnings"],
                "bounded_message": len(message) <= 2000,
                "message_has_root": str(root_error) in message,
                "message_has_role": "prior_file" in message,
                "message_has_close": str(leaf_close_error) in message,
                "uuid_call_count": outcome["uuid_call_count"],
                "prior_close_attempt_count": outcome[
                    "prior_close_attempt_count"
                ],
                "parent_close_count": outcome["parent_close_count"],
                "parent_transferred": outcome["parent_transferred"],
                "parent_closed": outcome["parent_closed"],
                "pointer_bytes": outcome["pointer_bytes"],
                "transaction_artifacts": outcome["transaction_artifacts"],
            },
            {
                "error_type": RuntimeError,
                "cleanup_errors": (("prior_file", leaf_close_error),),
                "cause_is_root": True,
                "warnings_type": tuple,
                "warnings": expected_warnings,
                "bounded_message": True,
                "message_has_root": True,
                "message_has_role": True,
                "message_has_close": True,
                "uuid_call_count": 1,
                "prior_close_attempt_count": 1,
                "parent_close_count": 1,
                "parent_transferred": False,
                "parent_closed": True,
                "pointer_bytes": b'{"sentinel":"uuid-cleanup-prior"}\n',
                "transaction_artifacts": (),
            },
        )

    def test_step2h_caller_warnings_survive_leaf_and_parent_aggregate(
        self,
    ) -> None:
        root_error = ValueError("injected caller-warning parent failure")
        leaf_close_error = OSError("injected caller-warning leaf failure")
        parent_close_error = OSError("injected caller-warning parent failure")
        raw_warnings = (
            "caller parent warning",
            "caller parent warning",
            "caller parent warning two",
        )
        expected_warnings = (
            "caller parent warning",
            "caller parent warning two",
        )
        outcome = self._run_uuid_constructor_cleanup_case(
            "windows",
            root_error=root_error,
            prior_close_error=leaf_close_error,
            parent_close_error=parent_close_error,
            caller_recovery_warnings=raw_warnings,
        )
        message = str(outcome["error"])

        self.assertEqual(
            {
                "error_type": outcome["error_type"],
                "cleanup_errors": outcome["cleanup_errors"],
                "cause_is_root": outcome["cause"] is root_error,
                "warnings_type": type(outcome["recovery_warnings"]),
                "warnings": outcome["recovery_warnings"],
                "bounded_message": len(message) <= 2000,
                "uuid_call_count": outcome["uuid_call_count"],
                "prior_close_attempt_count": outcome[
                    "prior_close_attempt_count"
                ],
                "parent_close_count": outcome["parent_close_count"],
                "parent_transferred": outcome["parent_transferred"],
                "parent_closed": outcome["parent_closed"],
                "pointer_bytes": outcome["pointer_bytes"],
                "transaction_artifacts": outcome["transaction_artifacts"],
            },
            {
                "error_type": RuntimeError,
                "cleanup_errors": (
                    ("prior_file", leaf_close_error),
                    ("retained_parent", parent_close_error),
                ),
                "cause_is_root": True,
                "warnings_type": tuple,
                "warnings": expected_warnings,
                "bounded_message": True,
                "uuid_call_count": 1,
                "prior_close_attempt_count": 1,
                "parent_close_count": 1,
                "parent_transferred": False,
                "parent_closed": True,
                "pointer_bytes": b'{"sentinel":"uuid-cleanup-prior"}\n',
                "transaction_artifacts": (),
            },
        )

    def test_step2h_constructor_aggregate_stably_merges_all_warning_sources(
        self,
    ) -> None:
        root_error = ValueError("injected all-source constructor failure")
        leaf_close_error = OSError("injected all-source prior close failure")
        caller_warnings = (
            "warning-z-caller-first",
            "warning-shared-everywhere",
            "warning-m-caller-last",
            "warning-z-caller-first",
        )
        top_level_warnings = (
            "warning-b-top-first",
            "warning-shared-everywhere",
            "warning-m-caller-last",
            "warning-y-top-last",
        )
        root_warnings = (
            "warning-a-root-first",
            "warning-b-top-first",
            "warning-shared-everywhere",
            "warning-x-root-last",
        )
        expected_warnings = (
            "warning-z-caller-first",
            "warning-shared-everywhere",
            "warning-m-caller-last",
            "warning-b-top-first",
            "warning-y-top-last",
            "warning-a-root-first",
            "warning-x-root-last",
        )
        root_error.recovery_warnings = root_warnings
        outcome = self._run_uuid_constructor_cleanup_case(
            "posix",
            root_error=root_error,
            prior_close_error=leaf_close_error,
            caller_recovery_warnings=caller_warnings,
            top_level_recovery_warnings=top_level_warnings,
        )
        construction_aggregates = outcome["construction_aggregates"]
        construction_aggregate = (
            construction_aggregates[0]
            if len(construction_aggregates) == 1
            else None
        )
        aggregate_message = str(construction_aggregate)

        self.assertEqual(
            {
                "construction_aggregate_count": len(construction_aggregates),
                "aggregate_type": type(construction_aggregate),
                "aggregate_cleanup_errors": getattr(
                    construction_aggregate,
                    "cleanup_errors",
                    None,
                ),
                "aggregate_cause_is_root": (
                    construction_aggregate.__cause__ is root_error
                    if construction_aggregate is not None
                    else False
                ),
                "root_warnings": getattr(root_error, "recovery_warnings", None),
                "bounded_message": len(aggregate_message) <= 2000,
                "message_has_root": str(root_error) in aggregate_message,
                "message_has_role": "prior_file" in aggregate_message,
                "message_has_close": str(leaf_close_error) in aggregate_message,
                "uuid_call_count": outcome["uuid_call_count"],
                "phase_calls": outcome["phase_calls"],
                "prior_open_count": outcome["prior_open_count"],
                "prior_close_attempt_count": outcome[
                    "prior_close_attempt_count"
                ],
                "parent_close_count": outcome["parent_close_count"],
                "parent_transferred": outcome["parent_transferred"],
                "parent_closed": outcome["parent_closed"],
                "parent_validated": outcome["parent_validated"],
                "pointer_bytes": outcome["pointer_bytes"],
                "transaction_artifacts": outcome["transaction_artifacts"],
            },
            {
                "construction_aggregate_count": 1,
                "aggregate_type": RuntimeError,
                "aggregate_cleanup_errors": (("prior_file", leaf_close_error),),
                "aggregate_cause_is_root": True,
                "root_warnings": root_warnings,
                "bounded_message": True,
                "message_has_root": True,
                "message_has_role": True,
                "message_has_close": True,
                "uuid_call_count": 1,
                "phase_calls": (),
                "prior_open_count": 1,
                "prior_close_attempt_count": 1,
                "parent_close_count": 1,
                "parent_transferred": False,
                "parent_closed": True,
                "parent_validated": True,
                "pointer_bytes": b'{"sentinel":"uuid-cleanup-prior"}\n',
                "transaction_artifacts": (),
            },
        )
        aggregate_warnings = getattr(
            construction_aggregate,
            "recovery_warnings",
            None,
        )
        self.assertIs(type(aggregate_warnings), tuple)
        self.assertEqual(aggregate_warnings, expected_warnings)
        self.assertEqual(
            {
                "same_aggregate": outcome["error"] is construction_aggregate,
                "error_type": outcome["error_type"],
                "cleanup_errors": outcome["cleanup_errors"],
                "cause_is_root": outcome["cause"] is root_error,
                "direct_chain": outcome["chain"] == (
                    construction_aggregate,
                    root_error,
                ),
                "warnings_type": type(outcome["recovery_warnings"]),
                "warnings": outcome["recovery_warnings"],
            },
            {
                "same_aggregate": True,
                "error_type": RuntimeError,
                "cleanup_errors": (("prior_file", leaf_close_error),),
                "cause_is_root": True,
                "direct_chain": True,
                "warnings_type": tuple,
                "warnings": expected_warnings,
            },
        )

    def test_step2h_expected_prior_mismatch_warnings_survive_cleanup_aggregates(
        self,
    ) -> None:
        recovery_warnings = (
            "recovered transaction warning one",
            "recovered transaction warning two",
        )
        for parent_fails in (False, True):
            with self.subTest(parent_fails=parent_fails):
                unexpected_uuid_error = AssertionError(
                    "UUID must not be reached after expected-prior mismatch"
                )
                leaf_close_error = OSError(
                    "injected mismatch prior leaf close failure"
                )
                parent_close_error = (
                    OSError("injected mismatch retained parent close failure")
                    if parent_fails
                    else None
                )
                outcome = self._run_uuid_constructor_cleanup_case(
                    "windows",
                    root_error=unexpected_uuid_error,
                    prior_close_error=leaf_close_error,
                    parent_close_error=parent_close_error,
                    internal_recovery_warnings=recovery_warnings,
                    expected_prior_matches=False,
                )
                direct_root = outcome["cause"]
                expected_cleanup_errors = (
                    (("prior_file", leaf_close_error),)
                    if parent_close_error is None
                    else (
                        ("prior_file", leaf_close_error),
                        ("retained_parent", parent_close_error),
                    )
                )

                self.assertEqual(
                    {
                        "error_type": outcome["error_type"],
                        "cleanup_errors": outcome["cleanup_errors"],
                        "root_type": type(direct_root),
                        "root_text": str(direct_root),
                        "root_warnings": getattr(
                            direct_root,
                            "recovery_warnings",
                            None,
                        ),
                        "warnings_type": type(outcome["recovery_warnings"]),
                        "warnings": outcome["recovery_warnings"],
                        "direct_chain": outcome["chain"] == (
                            outcome["error"],
                            direct_root,
                        ),
                        "bounded_message": len(str(outcome["error"])) <= 2000,
                        "uuid_call_count": outcome["uuid_call_count"],
                        "prior_close_attempt_count": outcome[
                            "prior_close_attempt_count"
                        ],
                        "parent_close_count": outcome["parent_close_count"],
                        "parent_transferred": outcome["parent_transferred"],
                        "parent_closed": outcome["parent_closed"],
                        "pointer_bytes": outcome["pointer_bytes"],
                        "transaction_artifacts": outcome[
                            "transaction_artifacts"
                        ],
                    },
                    {
                        "error_type": RuntimeError,
                        "cleanup_errors": expected_cleanup_errors,
                        "root_type": (
                            video_pointer_transaction_module
                            ._ExpectedPriorMismatch
                        ),
                        "root_text": (
                            "video delivery pointer changed before "
                            "transaction publication"
                        ),
                        "root_warnings": recovery_warnings,
                        "warnings_type": tuple,
                        "warnings": recovery_warnings,
                        "direct_chain": True,
                        "bounded_message": True,
                        "uuid_call_count": 0,
                        "prior_close_attempt_count": 1,
                        "parent_close_count": 1,
                        "parent_transferred": False,
                        "parent_closed": True,
                        "pointer_bytes": b'{"sentinel":"uuid-cleanup-prior"}\n',
                        "transaction_artifacts": (),
                    },
                )

    def test_step2h_clean_expected_prior_warning_display_is_bounded_once(
        self,
    ) -> None:
        warning_prefix = "step2h bounded expected-prior warning prefix"
        long_warning = warning_prefix + "|" + ("w" * 6000)
        recovery_warnings = (long_warning,)
        self.assertGreater(len(long_warning), 5000)
        mismatch_errors: list[BaseException] = []
        mismatch_type = (
            video_pointer_transaction_module._ExpectedPriorMismatch
        )
        real_mismatch_init = mismatch_type.__init__

        def record_mismatch(
            error: BaseException,
            warnings: tuple[str, ...],
        ) -> None:
            real_mismatch_init(error, warnings)
            mismatch_errors.append(error)

        with patch.object(mismatch_type, "__init__", new=record_mismatch):
            outcome = self._run_uuid_constructor_cleanup_case(
                "posix",
                root_error=AssertionError(
                    "UUID must not be reached after expected-prior mismatch"
                ),
                internal_recovery_warnings=recovery_warnings,
                expected_prior_matches=False,
            )

        error = outcome["error"]
        args_before = error.args
        first_display = str(error)
        args_after_first = error.args
        second_display = str(error)
        args_after_second = error.args

        self.assertEqual(len(mismatch_errors), 1)
        self.assertIs(error, mismatch_errors[0])
        self.assertIs(type(outcome["recovery_warnings"]), tuple)
        self.assertEqual(outcome["recovery_warnings"], recovery_warnings)
        self.assertEqual(
            {
                "error_type": type(error),
                "cleanup_errors": outcome["cleanup_errors"],
                "cause": outcome["cause"],
                "label_occurrences": first_display.count(
                    "recovery warnings:"
                ),
                "prefix_occurrences": first_display.count(warning_prefix),
                "bounded_display": len(first_display) <= 2000,
                "repeated_display_stable": first_display == second_display,
                "args_stable": (
                    args_before == args_after_first == args_after_second
                ),
                "args_identity_stable": (
                    args_before is args_after_first is args_after_second
                ),
                "uuid_call_count": outcome["uuid_call_count"],
                "parent_transferred": outcome["parent_transferred"],
                "parent_closed": outcome["parent_closed"],
                "pointer_bytes": outcome["pointer_bytes"],
                "transaction_artifacts": outcome["transaction_artifacts"],
            },
            {
                "error_type": mismatch_type,
                "cleanup_errors": None,
                "cause": None,
                "label_occurrences": 1,
                "prefix_occurrences": 1,
                "bounded_display": True,
                "repeated_display_stable": True,
                "args_stable": True,
                "args_identity_stable": True,
                "uuid_call_count": 0,
                "parent_transferred": False,
                "parent_closed": True,
                "pointer_bytes": b'{"sentinel":"uuid-cleanup-prior"}\n',
                "transaction_artifacts": (),
            },
        )

    def test_step2h_post_begin_forward_failure_keeps_generic_warning_behavior(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            final_dir = Path(tmp) / "final"
            final_dir.mkdir()
            parent_fd = os.open(final_dir, os.O_RDONLY | os.O_DIRECTORY)
            parent_resource_open = True
            parent_close_calls: list[object] = []
            validation_calls: list[object] = []
            phase_calls: list[str] = []
            raw_warnings = (
                "post-begin recovery warning one",
                "post-begin recovery warning one",
                "post-begin recovery warning two",
            )
            expected_warnings = (
                "post-begin recovery warning one",
                "post-begin recovery warning two",
            )
            forward_error = OSError("injected real forward failure")

            def close_parent() -> None:
                nonlocal parent_resource_open
                parent_close_calls.append(object())
                os.close(parent_fd)
                parent_resource_open = False

            def fail_during_forward(phase: str, **_details: object) -> None:
                phase_calls.append(phase)
                if phase == "no-prior-confirmed":
                    raise forward_error

            parent = video_pointer_transaction_module.RetainedParent(
                native_parent=parent_fd,
                platform="posix",
                validate_callback=lambda: validation_calls.append(object()),
                close_callback=close_parent,
            )
            caught: BaseException | None = None
            publication = None
            try:
                with (
                    patch.object(
                        video_pointer_transaction_module,
                        "_recover_video_pointer_transactions",
                        return_value=list(raw_warnings),
                    ),
                    patch.object(
                        video_pointer_transaction_module,
                        "_video_pointer_transaction_phase_hook",
                        side_effect=fail_during_forward,
                    ),
                ):
                    try:
                        publication = (
                            video_pointer_transaction_module
                            .stage_video_delivery_pointer(
                                parent,
                                b'{"replacement":"post-begin"}\n',
                                expected_prior=None,
                            )
                        )
                    except BaseException as exc:
                        caught = exc

                artifact_names = tuple(
                    sorted(path.name for path in final_dir.iterdir())
                )
                message = str(caught)
                self.assertEqual(
                    {
                        "error_type": type(caught),
                        "cause_is_forward": (
                            caught.__cause__ is forward_error
                            if caught is not None
                            else False
                        ),
                        "message_has_forward_error": str(forward_error) in message,
                        "warning_occurrences": tuple(
                            message.count(warning)
                            for warning in expected_warnings
                        ),
                        "cleanup_errors": getattr(
                            caught,
                            "cleanup_errors",
                            None,
                        ),
                        "phase_calls": tuple(phase_calls),
                        "parent_transferred": parent.transferred,
                        "parent_closed": parent.closed,
                        "parent_close_count": len(parent_close_calls),
                        "parent_validated": bool(validation_calls),
                        "pointer_exists": (
                            final_dir / "video_delivery.json"
                        ).exists(),
                        "new_artifact_count": sum(
                            name.endswith(".new") for name in artifact_names
                        ),
                        "prepared_record_count": sum(
                            "phase-000001-prepared.json" in name
                            for name in artifact_names
                        ),
                    },
                    {
                        "error_type": ValueError,
                        "cause_is_forward": True,
                        "message_has_forward_error": True,
                        "warning_occurrences": (1, 1),
                        "cleanup_errors": None,
                        "phase_calls": (
                            "prepared",
                            "no-prior-confirmed",
                            "reconciliation-required",
                        ),
                        "parent_transferred": True,
                        "parent_closed": True,
                        "parent_close_count": 1,
                        "parent_validated": True,
                        "pointer_exists": False,
                        "new_artifact_count": 1,
                        "prepared_record_count": 1,
                    },
                )
            finally:
                if publication is not None:
                    publication.close()
                elif parent_resource_open:
                    os.close(parent_fd)


class VideoRunnerFlowTest(unittest.TestCase):
    @patch("autodesign.runner.TOOL_HANDLERS")
    def test_recovery_does_not_replace_failed_external_video_author(self, handlers) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            ctx.state["designer_contract_abort"] = True
            ctx.state["designer_api_error"] = {
                "type": "external_video_author",
                "reason": "video_author_delivery_failed",
            }

            _recover_missing_composite(ctx, brief="type: video\nCreate an MP4.")

        handlers.__getitem__.assert_not_called()
        self.assertFalse(ctx.state.get("finalized", False))

    @patch("autodesign.runner.TOOL_HANDLERS")
    def test_recovery_exports_video_directly_then_finalizes(self, handlers) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))

            def _export(args, *, ctx):
                _install_passed_delivery(ctx)
                return ToolResultRecord(status="ok", payload={})

            def _finalize(args, *, ctx):
                ctx.state["finalized"] = True
                return ToolResultRecord(status="ok", payload={})

            handlers.__getitem__.side_effect = {
                "export_video": _export,
                "finalize": _finalize,
            }.__getitem__

            _recover_missing_composite(ctx, brief="type: video\nCreate an MP4.")
            outcome, _ = _derive_episode_outcome(
                ctx,
                finalized=bool(ctx.state.get("finalized")),
                spec_present=True,
                composition_present=ctx.state.get("composition") is not None,
            )

        self.assertTrue(ctx.state["finalized"])
        self.assertEqual(outcome, "pass")
        requested_handlers = [call.args[0] for call in handlers.__getitem__.call_args_list]
        self.assertEqual(requested_handlers, ["export_video", "finalize"])

    @patch("autodesign.runner.TOOL_HANDLERS")
    def test_recovery_reexports_delivery_from_an_older_revision(self, handlers) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            _install_passed_delivery(ctx)
            ctx.state["video_delivery"]["design_spec_revision"] = 0

            def _export(args, *, ctx):
                ctx.state["video_delivery"]["design_spec_revision"] = 1
                return ToolResultRecord(status="ok", payload={})

            handlers.__getitem__.side_effect = {
                "export_video": _export,
                "finalize": lambda args, *, ctx: ToolResultRecord(
                    status="ok", payload={}
                ),
            }.__getitem__

            _recover_missing_composite(ctx, brief="type: video\nCreate an MP4.")

        requested_handlers = [call.args[0] for call in handlers.__getitem__.call_args_list]
        self.assertEqual(requested_handlers, ["export_video", "finalize"])


if __name__ == "__main__":
    unittest.main()
