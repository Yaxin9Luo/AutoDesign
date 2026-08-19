from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile
from starlette.requests import Request

from scripts import web_server


class WebCanvasPresetTest(unittest.IsolatedAsyncioTestCase):
    def test_catalog_is_ordered_and_uses_canonical_dimensions(self) -> None:
        payload = web_server.canvas_presets("poster")

        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["kind"], "poster_canvas_presets")
        self.assertEqual(payload["default_preset_id"], "cvpr-landscape")
        self.assertEqual(
            [item["id"] for item in payload["presets"]],
            [
                "auto",
                "cvpr-landscape",
                "academic-landscape-5x3",
                "academic-landscape-1.4",
                "poster-classic-4x3",
                "neurips-portrait",
            ],
        )
        self.assertEqual(payload["presets"][0]["template"], None)
        self.assertEqual(
            [
                (item.get("canvas") or {}).get("w_px")
                for item in payload["presets"][1:]
            ],
            [3072, 2560, 2150, 2048, 1536],
        )

    def test_catalog_rejects_non_poster_artifact_type(self) -> None:
        with self.assertRaises(HTTPException) as rejected:
            web_server.canvas_presets("deck")

        self.assertEqual(rejected.exception.status_code, 400)
        self.assertEqual(
            rejected.exception.detail["code"],
            "unsupported_canvas_preset_artifact_type",
        )

    def test_auto_and_explicit_canvas_selection_validate_without_inference(self) -> None:
        self.assertEqual(
            web_server._validated_web_canvas_selection("poster", None, "auto"),
            (None, "auto"),
        )
        self.assertEqual(
            web_server._validated_web_canvas_selection(
                "poster",
                "academic-landscape-5x3",
                "academic-landscape-5x3",
            ),
            ("academic-landscape-5x3", "academic-landscape-5x3"),
        )
        self.assertEqual(
            web_server._validated_web_canvas_selection("poster", None, None),
            (None, None),
        )

    def test_unknown_or_mismatched_canvas_selection_is_stable_422(self) -> None:
        for template, preset_id in (
            ("missing-preset", None),
            (None, "missing-preset"),
            ("cvpr-landscape", "neurips-portrait"),
            ("cvpr-landscape", "auto"),
        ):
            with self.subTest(template=template, preset_id=preset_id):
                with self.assertRaises(HTTPException) as rejected:
                    web_server._validated_web_canvas_selection(
                        "poster",
                        template,
                        preset_id,
                    )
                self.assertEqual(rejected.exception.status_code, 422)
                self.assertIn(
                    rejected.exception.detail["code"],
                    {"unknown_canvas_preset", "canvas_preset_mismatch"},
                )

    def test_prompt_poster_template_rejects_explicit_nonposter_route(self) -> None:
        with self.assertRaises(HTTPException) as rejected:
            web_server._validated_canvas_prompt(
                "Template: cvpr-landscape",
                "deck",
            )

        self.assertEqual(rejected.exception.status_code, 422)
        self.assertEqual(
            rejected.exception.detail["code"],
            "conflicting_canvas_directives",
        )

    def test_reservation_auto_keeps_template_none_before_planning(self) -> None:
        request = Request({"type": "http", "headers": []})
        payload = web_server.RunReserveRequest(
            brief="Create an academic poster",
            artifact_type="poster",
            palette_id="plum_sage",
            template=None,
            canvas_preset_id="auto",
        )
        settings = web_server._runtime_only_settings()
        with (
            patch.object(web_server, "_settings_for_request", return_value=settings),
            patch.object(web_server, "_require_artifact_runtime"),
        ):
            _artifact_type, _settings, run_payload, state = (
                web_server._prepare_reservation(request, payload)
            )

        self.assertIsNone(run_payload["template"])
        self.assertEqual(run_payload["canvas_preset_id"], "auto")
        self.assertIsNone(state.template)
        self.assertEqual(state.canvas_preset_id, "auto")

    async def test_generate_conflict_returns_422_before_run_or_upload_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            uploads_dir = Path(raw_tmp)
            request = Request({"type": "http", "headers": []})
            runs_before = set(web_server._RUNS)
            with (
                patch.object(web_server, "UPLOADS_DIR", uploads_dir),
                patch.object(
                    web_server,
                    "_settings_for_request",
                    return_value=web_server._runtime_only_settings(),
                ),
                patch.object(web_server, "_require_artifact_runtime"),
            ):
                with self.assertRaises(HTTPException) as rejected:
                    await web_server.generate(
                        request,
                        brief="Poster, exact canvas 2400x1350 px and ratio 4:3",
                        artifact_type="poster",
                        palette_id="plum_sage",
                        baseline_artifact=None,
                        conversation_history=None,
                        prior_artifacts=None,
                        attachment_refs=None,
                        reference_poster_ref=None,
                        conversation_id=None,
                        template=None,
                        canvas_preset_id="auto",
                        files=[],
                        reference_poster=None,
                    )

            self.assertEqual(rejected.exception.status_code, 422)
            self.assertEqual(
                rejected.exception.detail["code"],
                "conflicting_canvas_directives",
            )
            self.assertEqual(list(uploads_dir.iterdir()), [])
            self.assertEqual(set(web_server._RUNS), runs_before)

    async def test_generate_auto_keeps_template_none_before_planning(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            request = Request({
                "type": "http",
                "headers": [(b"x-autodesign-reserve-only", b"true")],
            })
            paper = UploadFile(filename="paper.pdf", file=io.BytesIO(b"%PDF fake"))
            reserve = AsyncMock()
            settings = web_server._runtime_only_settings()
            with (
                patch.object(web_server, "UPLOADS_DIR", root / "uploads"),
                patch.object(web_server, "_settings_for_request", return_value=settings),
                patch.object(web_server, "_require_artifact_runtime"),
                patch.object(web_server, "_web_paper_poster_settings", return_value=settings),
                patch.object(
                    web_server,
                    "_paper_poster_author_cmd_resolution",
                    return_value={"available": True, "source": "test"},
                ),
                patch(
                    "autodesign.util.paper_source_sanity.assert_valid_paper_source_pdf"
                ),
                patch.object(web_server, "_append_event"),
                patch.object(web_server, "_reserve_legacy_pipeline_worker", reserve),
            ):
                ack = await web_server.generate(
                    request,
                    brief="Create an academic poster",
                    artifact_type="poster",
                    palette_id="plum_sage",
                    baseline_artifact=None,
                    conversation_history=None,
                    prior_artifacts=None,
                    attachment_refs=None,
                    reference_poster_ref=None,
                    conversation_id="conv",
                    template=None,
                    canvas_preset_id="auto",
                    authoring_max_attempts=None,
                    files=[paper],
                    reference_poster=None,
                )

            self.assertIsNone(reserve.await_args.kwargs["template"])
            self.assertEqual(reserve.await_args.kwargs["state"].canvas_preset_id, "auto")
            web_server._RUNS.pop(ack.run_id, None)

    def test_history_rebuild_preserves_template_and_canvas_selection(self) -> None:
        conversation = web_server._conversation_from_design_events(
            "conv",
            [{
                "event": "message.user_submitted",
                "run_id": "run_canvas",
                "_ts_ms": 1,
                "data": {
                    "brief": "Create a poster",
                    "artifact_type": "poster",
                    "palette_id": "plum_sage",
                    "template": "poster-classic-4x3",
                    "canvas_preset_id": "poster-classic-4x3",
                },
            }],
            set(),
        )

        task_payload = conversation["messages"][0]["task_payload"]
        self.assertEqual(task_payload["template"], "poster-classic-4x3")
        self.assertEqual(
            task_payload["canvas_preset_id"],
            "poster-classic-4x3",
        )
        self.assertEqual(
            conversation["poster_canvas_preset_id"],
            "poster-classic-4x3",
        )

    def test_disk_import_preserves_canvas_selection_from_run_brief(self) -> None:
        artifact = web_server.Artifact(
            artifact_id="art_run_canvas",
            name="Poster",
            artifact_type="poster",
            canvas=web_server.Canvas(w=2048, h=1536),
            native_format="html",
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            runs_dir = Path(raw_tmp)
            run_dir = runs_dir / "run_canvas"
            run_dir.mkdir()
            (run_dir / "run_brief.json").write_text(
                '{"effective_template":"poster-classic-4x3",'
                '"canvas_preset_id":"poster-classic-4x3"}',
                encoding="utf-8",
            )
            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(
                    web_server,
                    "_detect_artifact_type_for_run",
                    return_value="poster",
                ),
                patch.object(
                    web_server,
                    "_build_artifact_response",
                    return_value=artifact,
                ),
            ):
                conversation = web_server._conversation_from_disk_run("run_canvas")

        self.assertEqual(
            conversation["poster_canvas_preset_id"],
            "poster-classic-4x3",
        )


if __name__ == "__main__":
    unittest.main()
