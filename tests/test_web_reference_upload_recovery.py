from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from PIL import Image
from starlette.requests import Request

from scripts import web_server


class ReferenceUploadRecoveryTest(unittest.TestCase):
    @staticmethod
    def _image_bytes(image_format: str) -> bytes:
        payload = BytesIO()
        Image.new("RGB", (8, 4), (120, 20, 30)).save(payload, format=image_format)
        return payload.getvalue()

    @classmethod
    def _truncated_jpeg_bytes(cls) -> bytes:
        payload = BytesIO()
        Image.new("RGB", (8, 4), (120, 20, 30)).save(
            payload,
            format="JPEG",
            quality=90,
        )
        return payload.getvalue()[:-2]

    def test_web_reference_accepts_only_raster_image_suffixes(self) -> None:
        for name in ("poster.png", "poster.jpg", "poster.JPEG", "poster.webp"):
            with self.subTest(name=name):
                self.assertEqual(
                    web_server._validated_web_reference_poster_name(name),
                    Path(name).name,
                )
        for name in (
            "poster.pdf",
            "poster.pptx",
            "poster.html",
            "poster.svg",
            "poster.gif",
        ):
            with self.subTest(name=name):
                with self.assertRaises(HTTPException) as rejected:
                    web_server._validated_web_reference_poster_name(name)
                self.assertEqual(rejected.exception.status_code, 400)
                self.assertEqual(
                    rejected.exception.detail["code"],
                    "unsupported_reference_poster_image",
                )

    def test_web_reference_validates_image_contents_and_format(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            valid_png = root / "poster.png"
            valid_png.write_bytes(self._image_bytes("PNG"))
            web_server._validate_web_reference_poster_file(valid_png)

            corrupt_png = root / "corrupt.png"
            corrupt_png.write_bytes(b"not an image")
            with self.assertRaises(HTTPException) as corrupt:
                web_server._validate_web_reference_poster_file(corrupt_png)
            self.assertEqual(corrupt.exception.status_code, 400)
            self.assertEqual(
                corrupt.exception.detail["code"],
                "invalid_reference_poster_image",
            )

            renamed_jpeg = root / "renamed.png"
            renamed_jpeg.write_bytes(self._image_bytes("JPEG"))
            with self.assertRaises(HTTPException) as mismatch:
                web_server._validate_web_reference_poster_file(renamed_jpeg)
            self.assertEqual(mismatch.exception.status_code, 400)
            self.assertEqual(
                mismatch.exception.detail["code"],
                "reference_poster_image_format_mismatch",
            )

            oversized_png = bytearray(self._image_bytes("PNG"))
            oversized_png[16:24] = struct.pack(">II", 200_000, 200_000)
            oversized_png[29:33] = struct.pack(
                ">I",
                zlib.crc32(oversized_png[12:29]) & 0xFFFFFFFF,
            )
            oversized_path = root / "oversized.png"
            oversized_path.write_bytes(oversized_png)
            with self.assertRaises(HTTPException) as oversized:
                web_server._validate_web_reference_poster_file(oversized_path)
            self.assertEqual(oversized.exception.status_code, 400)
            self.assertEqual(
                oversized.exception.detail["code"],
                "invalid_reference_poster_image",
            )

            truncated_jpeg = root / "truncated.jpg"
            truncated_jpeg.write_bytes(self._truncated_jpeg_bytes())
            with Image.open(truncated_jpeg) as image:
                image.verify()
            with self.assertRaises(HTTPException) as truncated:
                web_server._validate_web_reference_poster_file(truncated_jpeg)
            self.assertEqual(truncated.exception.status_code, 400)
            self.assertEqual(
                truncated.exception.detail["code"],
                "invalid_reference_poster_image",
            )

    def test_history_rebuild_preserves_style_reference_role(self) -> None:
        conversation = web_server._conversation_from_design_events(
            "conv",
            [
                {
                    "event": "attachment.added",
                    "run_id": "run_reference",
                    "_ts_ms": 1,
                    "data": {
                        "name": "paper.pdf",
                        "suffix": ".pdf",
                        "size": 100,
                    },
                },
                {
                    "event": "attachment.added",
                    "run_id": "run_reference",
                    "_ts_ms": 2,
                    "data": {
                        "name": "reference.png",
                        "suffix": ".png",
                        "size": 200,
                        "role": "style_reference",
                        "reference_handle": "ref_historical",
                    },
                },
                {
                    "event": "message.user_submitted",
                    "run_id": "run_reference",
                    "_ts_ms": 3,
                    "data": {
                        "brief": "Create a poster",
                        "artifact_type": "poster",
                        "palette_id": "deep_cyan",
                    },
                },
            ],
            set(),
        )

        message = conversation["messages"][0]
        self.assertEqual(message["attachments"][1]["role"], "style_reference")
        self.assertEqual(
            message["task_payload"]["attachment_refs"],
            [message["attachments"][0]],
        )
        self.assertNotIn(
            "reference.png",
            [item["name"] for item in message["task_payload"]["attachment_refs"]],
        )
        self.assertEqual(
            message["task_payload"]["reference_poster_ref"]["name"],
            "reference.png",
        )
        self.assertEqual(
            message["task_payload"]["reference_poster_ref"]["reference_handle"],
            "ref_historical",
        )

    def test_history_rebuild_keeps_legacy_attachment_as_content(self) -> None:
        conversation = web_server._conversation_from_design_events(
            "conv",
            [
                {
                    "event": "attachment.added",
                    "run_id": "run_legacy",
                    "_ts_ms": 1,
                    "data": {
                        "name": "legacy-reference.png",
                        "suffix": ".png",
                        "size": 200,
                    },
                },
                {
                    "event": "message.user_submitted",
                    "run_id": "run_legacy",
                    "_ts_ms": 2,
                    "data": {
                        "brief": "Create a poster",
                        "artifact_type": "poster",
                        "palette_id": "deep_cyan",
                    },
                },
            ],
            set(),
        )

        message = conversation["messages"][0]
        self.assertNotIn("role", message["attachments"][0])
        self.assertEqual(
            message["task_payload"]["attachment_refs"][0]["name"],
            "legacy-reference.png",
        )
        self.assertIsNone(message["task_payload"]["reference_poster_ref"])

    def test_history_rebuild_keeps_handleless_style_reference_legacy(self) -> None:
        conversation = web_server._conversation_from_design_events(
            "conv",
            [
                {
                    "event": "attachment.added",
                    "run_id": "run_legacy_reference",
                    "_ts_ms": 1,
                    "data": {
                        "name": "legacy-reference.png",
                        "suffix": ".png",
                        "size": 200,
                        "role": "style_reference",
                    },
                },
                {
                    "event": "message.user_submitted",
                    "run_id": "run_legacy_reference",
                    "_ts_ms": 2,
                    "data": {
                        "brief": "Create a poster",
                        "artifact_type": "poster",
                        "palette_id": "deep_cyan",
                    },
                },
            ],
            set(),
        )

        reference_ref = conversation["messages"][0]["task_payload"]["reference_poster_ref"]
        self.assertEqual(reference_ref["name"], "legacy-reference.png")
        self.assertNotIn("id", reference_ref)
        self.assertNotIn("reference_handle", reference_ref)

    def test_reference_recovery_is_scoped_to_conversation_owner(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            uploads_dir = root / "uploads"
            alice_reference = uploads_dir / "alice" / "reference_poster" / "poster.pdf"
            bob_reference = uploads_dir / "bob" / "reference_poster" / "poster.pdf"
            for path, content in ((alice_reference, b"alice"), (bob_reference, b"bob")):
                path.parent.mkdir(parents=True)
                path.write_bytes(content)

            with (
                patch.object(web_server, "UPLOADS_DIR", uploads_dir),
                patch.object(web_server, "UPLOADS_INDEX_PATH", uploads_dir / "conversation_attachments.json"),
                patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
            ):
                web_server._record_conversation_reference_poster(
                    "shared-conversation",
                    alice_reference,
                    owner_id="user:alice",
                )
                web_server._record_conversation_reference_poster(
                    "shared-conversation",
                    bob_reference,
                    owner_id="user:bob",
                )

                self.assertEqual(
                    web_server._latest_persisted_conversation_reference_poster(
                        "shared-conversation",
                        owner_id="user:alice",
                    ),
                    alice_reference.resolve(),
                )
                self.assertEqual(
                    web_server._latest_persisted_conversation_reference_poster(
                        "shared-conversation",
                        owner_id="user:bob",
                    ),
                    bob_reference.resolve(),
                )
                self.assertIsNone(
                    web_server._latest_persisted_conversation_reference_poster(
                        "shared-conversation",
                        owner_id="user:mallory",
                    )
                )


class ReferenceUploadRecoveryEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_upload_mints_new_handle_instead_of_reusing_client_handle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            uploads_dir = Path(raw_tmp) / "uploads"
            index_path = uploads_dir / "conversation_attachments.json"
            old_reference = uploads_dir / "old" / "reference_poster" / "old.jpg"
            old_reference.parent.mkdir(parents=True)
            old_reference.write_bytes(ReferenceUploadRecoveryTest._image_bytes("JPEG"))
            reference_upload = web_server.UploadFile(
                BytesIO(ReferenceUploadRecoveryTest._image_bytes("JPEG")),
                filename="new.jpg",
            )
            paper_upload = web_server.UploadFile(
                BytesIO(b"paper fixture"),
                filename="paper.pdf",
            )
            request = Request({"type": "http", "headers": []})
            settings = SimpleNamespace(
                designer_model="gpt-5.4",
                designer_author_mode="external",
                designer_author_harness="codex",
            )

            with (
                patch.object(web_server, "UPLOADS_DIR", uploads_dir),
                patch.object(web_server, "UPLOADS_INDEX_PATH", index_path),
                patch.object(web_server, "_RUN_ACCESS_CONTROL", False),
                patch.object(web_server, "_DEMO_MODE", False),
                patch.object(web_server, "_settings_for_request", return_value=settings),
                patch.object(web_server, "_web_paper_poster_settings", return_value=settings),
                patch.object(
                    web_server,
                    "_paper_poster_author_cmd_resolution",
                    return_value={"available": True, "source": "test", "message": ""},
                ),
                patch.object(web_server, "_append_event"),
                patch.object(
                    web_server,
                    "_start_legacy_pipeline_worker",
                    new_callable=AsyncMock,
                ),
                patch.object(
                    web_server,
                    "_monitor_supervised_pipeline",
                    new_callable=AsyncMock,
                ),
                patch(
                    "autodesign.util.paper_source_sanity.assert_valid_paper_source_pdf"
                ),
            ):
                web_server._record_conversation_reference_poster(
                    "conversation-owner",
                    old_reference,
                    reference_handle="ref_old",
                )
                ack = await web_server.generate(
                    request,
                    brief="Create a poster",
                    artifact_type="poster",
                    palette_id="plum_sage",
                    baseline_artifact=None,
                    conversation_history=None,
                    prior_artifacts=None,
                    attachment_refs=None,
                    reference_poster_ref=(
                        '[{"name":"old.jpg","reference_handle":"ref_old"}]'
                    ),
                    conversation_id="conversation-owner",
                    template=None,
                    files=[paper_upload],
                    reference_poster=reference_upload,
                )
                await web_server._RUNS[ack.run_id].task

                self.assertIsNotNone(ack.reference_poster_handle)
                self.assertNotEqual(ack.reference_poster_handle, "ref_old")
                self.assertEqual(
                    web_server._persisted_conversation_reference_poster_by_handle(
                        "conversation-owner",
                        "ref_old",
                    ),
                    old_reference.resolve(),
                )
                new_reference = (
                    web_server._persisted_conversation_reference_poster_by_handle(
                        "conversation-owner",
                        ack.reference_poster_handle,
                    )
                )
                self.assertIsNotNone(new_reference)
                self.assertEqual(new_reference.name, "new.jpg")
                self.assertNotEqual(new_reference, old_reference.resolve())
                web_server._RUNS.pop(ack.run_id, None)

    async def test_generate_fails_closed_for_live_reference_without_handle(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            uploads_dir = Path(raw_tmp) / "uploads"
            latest_reference = uploads_dir / "latest" / "reference_poster" / "latest.jpg"
            latest_reference.parent.mkdir(parents=True)
            latest_reference.write_bytes(ReferenceUploadRecoveryTest._image_bytes("JPEG"))
            request = Request({"type": "http", "headers": []})

            with (
                patch.object(web_server, "UPLOADS_DIR", uploads_dir),
                patch.object(web_server, "_RUN_ACCESS_CONTROL", False),
                patch.object(web_server, "_DEMO_MODE", False),
                patch.object(
                    web_server,
                    "_settings_for_request",
                    return_value=SimpleNamespace(),
                ),
                patch.object(
                    web_server,
                    "_latest_conversation_reference_poster",
                    new_callable=AsyncMock,
                    return_value=latest_reference,
                ) as latest_reference_lookup,
            ):
                with self.assertRaises(HTTPException) as rejected:
                    await web_server.generate(
                        request,
                        brief="Create a poster",
                        artifact_type="poster",
                        palette_id="plum_sage",
                        baseline_artifact=None,
                        conversation_history=None,
                        prior_artifacts=None,
                        attachment_refs=None,
                        reference_poster_ref=(
                            '[{"id":"att_1_live","name":"first.jpg",'
                            '"size":634,"kind":"image","role":"style_reference"}]'
                        ),
                        conversation_id="conversation-owner",
                        template=None,
                        files=[],
                        reference_poster=None,
                    )

            self.assertEqual(rejected.exception.status_code, 422)
            self.assertEqual(
                rejected.exception.detail["code"],
                "reference_poster_handle_required",
            )
            latest_reference_lookup.assert_not_awaited()

    async def test_generate_resolves_historical_reference_handle_not_latest(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            uploads_dir = Path(raw_tmp) / "uploads"
            index_path = uploads_dir / "conversation_attachments.json"
            first_reference = uploads_dir / "first" / "reference_poster" / "first.jpg"
            latest_reference = uploads_dir / "latest" / "reference_poster" / "latest.jpg"
            first_reference.parent.mkdir(parents=True)
            latest_reference.parent.mkdir(parents=True)
            first_reference.write_bytes(ReferenceUploadRecoveryTest._truncated_jpeg_bytes())
            latest_reference.write_bytes(ReferenceUploadRecoveryTest._image_bytes("JPEG"))
            request = Request({"type": "http", "headers": []})

            with (
                patch.object(web_server, "UPLOADS_DIR", uploads_dir),
                patch.object(web_server, "UPLOADS_INDEX_PATH", index_path),
                patch.object(web_server, "_RUN_ACCESS_CONTROL", False),
                patch.object(web_server, "_DEMO_MODE", False),
                patch.object(
                    web_server,
                    "_settings_for_request",
                    return_value=SimpleNamespace(),
                ),
                patch.object(
                    web_server,
                    "_latest_conversation_reference_poster",
                    new_callable=AsyncMock,
                    return_value=latest_reference,
                ) as latest_reference_lookup,
            ):
                web_server._record_conversation_reference_poster(
                    "conversation-owner",
                    first_reference,
                    reference_handle="ref_first",
                )
                web_server._record_conversation_reference_poster(
                    "conversation-owner",
                    latest_reference,
                    reference_handle="ref_latest",
                )

                with self.assertRaises(HTTPException) as rejected:
                    await web_server.generate(
                        request,
                        brief="Create a poster",
                        artifact_type="poster",
                        palette_id="plum_sage",
                        baseline_artifact=None,
                        conversation_history=None,
                        prior_artifacts=None,
                        attachment_refs=None,
                        reference_poster_ref=(
                            '[{"name":"first.jpg","reference_handle":"ref_first"}]'
                        ),
                        conversation_id="conversation-owner",
                        template=None,
                        files=[],
                        reference_poster=None,
                    )

            self.assertEqual(rejected.exception.status_code, 400)
            self.assertEqual(
                rejected.exception.detail["code"],
                "invalid_reference_poster_image",
            )
            latest_reference_lookup.assert_not_awaited()

    async def test_generate_rejects_recovered_non_image_reference_for_owner(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            uploads_dir = Path(raw_tmp) / "uploads"
            recovered_reference = (
                uploads_dir
                / "user:alice"
                / "reference_poster"
                / "reference.png"
            )
            recovered_reference.parent.mkdir(parents=True)
            recovered_reference.write_bytes(b"not a reference image")
            staged_files_before = {
                path.relative_to(uploads_dir)
                for path in uploads_dir.rglob("*")
                if path.is_file()
            }
            runs_before = set(web_server._RUNS)
            request = Request({
                "type": "http",
                "headers": [(b"x-demo-user", b"alice")],
            })

            with (
                patch.object(web_server, "UPLOADS_DIR", uploads_dir),
                patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
                patch.object(web_server, "_DEMO_MODE", False),
                patch.object(
                    web_server,
                    "_settings_for_request",
                    return_value=SimpleNamespace(),
                ),
                patch.object(
                    web_server,
                    "_latest_conversation_reference_poster",
                    new_callable=AsyncMock,
                    return_value=recovered_reference,
                ) as recover_reference,
            ):
                with self.assertRaises(HTTPException) as rejected:
                    await web_server.generate(
                        request,
                        brief="Create a poster",
                        artifact_type="poster",
                        palette_id="plum_sage",
                        baseline_artifact=None,
                        conversation_history=None,
                        prior_artifacts=None,
                        attachment_refs=None,
                        reference_poster_ref='[{"name":"reference.pdf"}]',
                        conversation_id="conversation-owner",
                        template=None,
                        files=[],
                        reference_poster=None,
                    )

            self.assertEqual(rejected.exception.status_code, 400)
            self.assertEqual(
                rejected.exception.detail["code"],
                "invalid_reference_poster_image",
            )
            recover_reference.assert_awaited_once_with(
                "conversation-owner",
                owner_id="user:alice",
            )
            self.assertEqual(set(web_server._RUNS), runs_before)
            staged_files_after = {
                path.relative_to(uploads_dir)
                for path in uploads_dir.rglob("*")
                if path.is_file()
            }
            self.assertEqual(staged_files_after, staged_files_before)

    async def test_generate_rejects_mismatched_uploaded_reference_before_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            uploads_dir = Path(raw_tmp) / "uploads"
            jpeg_payload = ReferenceUploadRecoveryTest._image_bytes("JPEG")
            reference_upload = web_server.UploadFile(
                BytesIO(jpeg_payload),
                filename="reference.png",
            )
            runs_before = set(web_server._RUNS)
            request = Request({"type": "http", "headers": []})

            with (
                patch.object(web_server, "UPLOADS_DIR", uploads_dir),
                patch.object(web_server, "_RUN_ACCESS_CONTROL", False),
                patch.object(web_server, "_DEMO_MODE", False),
                patch.object(
                    web_server,
                    "_settings_for_request",
                    return_value=SimpleNamespace(),
                ),
            ):
                with self.assertRaises(HTTPException) as rejected:
                    await web_server.generate(
                        request,
                        brief="Create a poster",
                        artifact_type="poster",
                        palette_id="plum_sage",
                        baseline_artifact=None,
                        conversation_history=None,
                        prior_artifacts=None,
                        attachment_refs=None,
                        reference_poster_ref=None,
                        conversation_id="conversation-owner",
                        template=None,
                        files=[],
                        reference_poster=reference_upload,
                    )

            self.assertEqual(rejected.exception.status_code, 400)
            self.assertEqual(
                rejected.exception.detail["code"],
                "reference_poster_image_format_mismatch",
            )
            self.assertEqual(set(web_server._RUNS), runs_before)
            self.assertEqual(
                [path for path in uploads_dir.rglob("*") if path.is_file()],
                [],
            )

    async def test_generate_rejects_truncated_uploaded_jpeg_before_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            uploads_dir = Path(raw_tmp) / "uploads"
            reference_upload = web_server.UploadFile(
                BytesIO(ReferenceUploadRecoveryTest._truncated_jpeg_bytes()),
                filename="reference.jpg",
            )
            runs_before = set(web_server._RUNS)
            request = Request({"type": "http", "headers": []})

            with (
                patch.object(web_server, "UPLOADS_DIR", uploads_dir),
                patch.object(web_server, "_RUN_ACCESS_CONTROL", False),
                patch.object(web_server, "_DEMO_MODE", False),
                patch.object(
                    web_server,
                    "_settings_for_request",
                    return_value=SimpleNamespace(),
                ),
            ):
                with self.assertRaises(HTTPException) as rejected:
                    await web_server.generate(
                        request,
                        brief="Create a poster",
                        artifact_type="poster",
                        palette_id="plum_sage",
                        baseline_artifact=None,
                        conversation_history=None,
                        prior_artifacts=None,
                        attachment_refs=None,
                        reference_poster_ref=None,
                        conversation_id="conversation-owner",
                        template=None,
                        files=[],
                        reference_poster=reference_upload,
                    )

            self.assertEqual(rejected.exception.status_code, 400)
            self.assertEqual(
                rejected.exception.detail["code"],
                "invalid_reference_poster_image",
            )
            self.assertEqual(set(web_server._RUNS), runs_before)
            self.assertEqual(
                [path for path in uploads_dir.rglob("*") if path.is_file()],
                [],
            )


if __name__ == "__main__":
    unittest.main()
