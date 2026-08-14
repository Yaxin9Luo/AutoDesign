from __future__ import annotations

import io
from dataclasses import replace
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup
from fastapi import HTTPException, UploadFile
from starlette.requests import Request

from autodesign.agents.external_code_editor import ExternalCodeEditor
from autodesign.artifact_edit_job import ArtifactEditJobError, run_artifact_edit_job
from autodesign.run_control import CancellationToken
from autodesign.schema import ApplyEditsResult
from autodesign.tools._contract import ToolContext
from autodesign.tools.propose_paper_poster_html import (
    _authored_poster_css_variable_values,
    _normalize_authored_html_document_with_root_shell,
    authored_palette_diagnostics,
    propose_paper_poster_html,
)
from autodesign.util.academic_palette import (
    AcademicPaletteCatalogError,
    require_academic_color_system,
)
from scripts import web_server


def _poster_html(
    color_system: dict[str, object],
    *,
    palette_id: str | None = None,
    include_palette_id: bool = True,
    extra_css: str = "",
    extra_body: str = "",
) -> str:
    css_variables = color_system["css_variables"]
    declarations = ";".join(
        f"{name}:{value}" for name, value in css_variables.items()
    )
    selected_id = palette_id or str(color_system["palette_id"])
    palette_attribute = (
        f" data-palette-id='{selected_id}'"
        if include_palette_id
        else ""
    )
    return (
        "<!doctype html><html><head><style>"
        f".paper-poster{{{declarations};width:3072px;height:1536px}}"
        f"{extra_css}"
        "</style></head><body>"
        f"<main class='paper-poster'{palette_attribute}>"
        f"<h1>Paper</h1><p>Grounded poster content.</p>{extra_body}</main>"
        "</body></html>"
    )


def _palette_declarations(color_system: dict[str, object]) -> str:
    css_variables = color_system["css_variables"]
    return ";".join(
        f"{name}:{value}" for name, value in css_variables.items()
    )


class WebPosterPaletteTest(unittest.TestCase):
    def test_poster_requires_known_palette(self) -> None:
        with self.assertRaises(HTTPException) as missing:
            web_server._validated_web_palette_id("poster", None)
        self.assertEqual(missing.exception.status_code, 422)
        self.assertEqual(missing.exception.detail["code"], "poster_palette_required")
        with self.assertRaises(HTTPException) as unknown:
            web_server._validated_web_palette_id("poster", "unknown")
        self.assertEqual(unknown.exception.status_code, 422)
        self.assertEqual(unknown.exception.detail["code"], "unknown_poster_palette")
        self.assertEqual(
            web_server._validated_web_palette_id("poster", "plum_sage"),
            "plum_sage",
        )

    def test_poster_rejects_palette_display_name(self) -> None:
        with self.assertRaises(HTTPException) as rejected:
            web_server._validated_web_palette_id("poster", "Teal")
        self.assertEqual(rejected.exception.status_code, 422)
        self.assertEqual(
            rejected.exception.detail["code"],
            "unknown_poster_palette",
        )

    def test_poster_rejects_normalized_palette_label_variant(self) -> None:
        with self.assertRaises(HTTPException) as rejected:
            web_server._validated_web_palette_id("poster", "plum-sage")
        self.assertEqual(rejected.exception.status_code, 422)
        self.assertEqual(
            rejected.exception.detail["code"],
            "unknown_poster_palette",
        )

    def test_non_poster_rejects_palette_but_allows_omission(self) -> None:
        self.assertIsNone(web_server._validated_web_palette_id("deck", None))
        with self.assertRaises(HTTPException) as supplied:
            web_server._validated_web_palette_id("deck", "plum_sage")
        self.assertEqual(supplied.exception.status_code, 422)
        self.assertEqual(
            supplied.exception.detail["code"],
            "palette_not_supported_for_artifact",
        )

    def test_catalog_is_poster_only_and_reports_unavailable(self) -> None:
        payload = web_server.palettes("poster")
        self.assertEqual(payload["kind"], "academic_poster_color_palettes")
        with self.assertRaises(HTTPException) as unsupported:
            web_server.palettes("deck")
        self.assertEqual(unsupported.exception.status_code, 400)
        self.assertEqual(
            unsupported.exception.detail["code"],
            "unsupported_palette_artifact_type",
        )
        with patch.object(
            web_server,
            "academic_palette_catalog_payload",
            side_effect=AcademicPaletteCatalogError("catalog broken"),
        ):
            with self.assertRaises(HTTPException) as unavailable:
                web_server.palettes("poster")
        self.assertEqual(unavailable.exception.status_code, 503)
        self.assertEqual(
            unavailable.exception.detail["code"],
            "palette_catalog_unavailable",
        )

    def test_catalog_failure_is_503_during_palette_validation(self) -> None:
        with patch.object(
            web_server,
            "academic_palette_catalog_payload",
            side_effect=AcademicPaletteCatalogError("catalog broken"),
        ):
            with self.assertRaises(HTTPException) as unavailable:
                web_server._validated_web_palette_id("poster", "plum_sage")
        self.assertEqual(unavailable.exception.status_code, 503)
        self.assertEqual(
            unavailable.exception.detail["code"],
            "palette_catalog_unavailable",
        )

    def test_history_rebuild_preserves_palette_task_payload(self) -> None:
        conversation = web_server._conversation_from_design_events(
            "conv",
            [{
                "event": "message.user_submitted",
                "run_id": "run_palette",
                "_ts_ms": 1,
                "data": {
                    "brief": "Create a poster",
                    "artifact_type": "poster",
                    "palette_id": "deep_cyan",
                },
            }],
            set(),
        )
        payload = conversation["messages"][0]["task_payload"]
        self.assertEqual(conversation["messages"][0]["task_type"], "generate")
        self.assertEqual(payload["palette_id"], "deep_cyan")

    def test_history_rebuild_uses_successful_apply_edits_palette(self) -> None:
        artifact = {
            "artifact_id": "art_edited",
            "name": "Poster revision",
            "artifact_type": "poster",
        }
        with patch.object(
            web_server,
            "_history_artifact_for_event",
            return_value=artifact,
        ):
            conversation = web_server._conversation_from_design_events(
                "conv",
                [{
                    "event": "edits.applied",
                    "run_id": "edited",
                    "_ts_ms": 2,
                    "data": {
                        "palette_id": "oxide_red",
                    },
                }],
                set(),
            )
        self.assertEqual(conversation["poster_palette_id"], "oxide_red")

    def test_disk_import_preserves_palette_id(self) -> None:
        artifact = web_server.Artifact(
            artifact_id="art_run_palette",
            name="Poster",
            artifact_type="poster",
            canvas=web_server.Canvas(w=3072, h=1536),
            native_format="html",
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            runs_dir = Path(raw_tmp)
            run_dir = runs_dir / "run_palette"
            run_dir.mkdir()
            (run_dir / "run_brief.json").write_text(
                '{"palette_id":"deep_cyan"}',
                encoding="utf-8",
            )
            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "_detect_artifact_type_for_run", return_value="poster"),
                patch.object(web_server, "_build_artifact_response", return_value=artifact),
            ):
                conversation = web_server._conversation_from_disk_run("run_palette")
        self.assertEqual(conversation["poster_palette_id"], "deep_cyan")

    def test_code_editor_disk_import_uses_revision_manifest_palette(self) -> None:
        artifact = web_server.Artifact(
            artifact_id="art_run_revision",
            name="Poster revision",
            artifact_type="poster",
            canvas=web_server.Canvas(w=3072, h=1536),
            native_format="html",
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            runs_dir = Path(raw_tmp)
            final_dir = runs_dir / "run_revision" / "final"
            final_dir.mkdir(parents=True)
            (final_dir / "code_editor_revision_manifest.json").write_text(
                '{"palette_id":"oxide_red"}',
                encoding="utf-8",
            )
            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "_detect_artifact_type_for_run", return_value="poster"),
                patch.object(web_server, "_build_artifact_response", return_value=artifact),
            ):
                conversation = web_server._conversation_from_disk_run("run_revision")
        self.assertEqual(conversation["poster_palette_id"], "oxide_red")

    def test_apply_edits_disk_import_uses_palette_manifests(self) -> None:
        artifact = web_server.Artifact(
            artifact_id="art_run_edit",
            name="Poster revision",
            artifact_type="poster",
            canvas=web_server.Canvas(w=3072, h=1536),
            native_format="html",
        )
        for manifest_name, palette_id in (
            ("authored_poster_edit_manifest.json", "plum_sage"),
            ("apply_edits_palette_manifest.json", "deep_cyan"),
        ):
            with self.subTest(manifest=manifest_name):
                with tempfile.TemporaryDirectory() as raw_tmp:
                    runs_dir = Path(raw_tmp)
                    final_dir = runs_dir / "run_edit" / "final"
                    final_dir.mkdir(parents=True)
                    (final_dir / manifest_name).write_text(
                        json.dumps({"palette_id": palette_id}),
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
                        conversation = web_server._conversation_from_disk_run("run_edit")
                self.assertEqual(conversation["poster_palette_id"], palette_id)

    def test_required_palette_validator_blocks_shell_extra_but_allows_source_visual(self) -> None:
        required = require_academic_color_system("plum_sage")
        source_visual = (
            "<svg data-source-id='figure:1' data-block-kind='chart'>"
            "<path></path></svg>"
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            shell_path = root / "shell.html"
            shell_path.write_text(
                _poster_html(
                    required,
                    extra_css=".poster-section{background:#f0a}",
                    extra_body="<section class='poster-section'>Methods</section>",
                ),
                encoding="utf-8",
            )
            with self.assertRaises(HTTPException) as invalid:
                web_server._validate_required_poster_palette_html(shell_path, required)
            self.assertEqual(invalid.exception.status_code, 422)
            diagnostics = invalid.exception.detail["palette_diagnostics"]
            self.assertEqual(diagnostics[0]["shell_extra_colors"], ["#FF00AA"])

            source_path = root / "source.html"
            source_path.write_text(
                _poster_html(
                    required,
                    extra_css="[data-source-id='figure:1'] path{fill:#f0a}",
                    extra_body=source_visual,
                ),
                encoding="utf-8",
            )
            web_server._validate_required_poster_palette_html(source_path, required)


class WebPosterPaletteRequestTest(unittest.IsolatedAsyncioTestCase):
    def _run_generated_poster_edit(
        self,
        *,
        out_dir: Path,
        generated_html: str,
        required_color_system: dict[str, object],
    ) -> dict[str, object]:
        source_run = out_dir / "runs" / "source"
        source_html = source_run / "final" / "poster.html"
        child_run = out_dir / "runs" / "generated"
        uploads_dir = child_run / "uploads"
        uploads_dir.mkdir(parents=True)
        staged_html = uploads_dir / "poster.html"
        staged_html.write_text(source_html.read_text(encoding="utf-8"), encoding="utf-8")
        input_path = uploads_dir / "artifact_edit.json"
        input_path.write_text(
            json.dumps({
                "version": 1,
                "artifact_type": "poster",
                "source_relative_path": "final/poster.html",
                "edited_html_relative_path": "uploads/poster.html",
                "edits": {},
                "required_color_system": required_color_system,
                "candidate_lineage": {},
            }),
            encoding="utf-8",
        )

        def fake_apply(*_args: object, **kwargs: object) -> ApplyEditsResult:
            work_dir = Path(kwargs["out_dir"])
            generated_final = work_dir / "final"
            generated_final.mkdir(parents=True)
            (generated_final / "poster.html").write_text(
                generated_html,
                encoding="utf-8",
            )
            return ApplyEditsResult(
                run_id="generated",
                run_dir=str(work_dir),
                parent_run_id="source",
                restored_layer_ids=[],
                skipped=[],
                artifact_type="poster",
            )

        settings = replace(web_server._runtime_only_settings(), out_dir=out_dir)
        with (
            patch(
                "autodesign.artifact_edit_job._is_authored_paper_poster_html",
                return_value=False,
            ),
            patch(
                "autodesign.artifact_edit_job.apply_edits",
                side_effect=fake_apply,
            ),
        ):
            return run_artifact_edit_job(
                run_id="generated",
                parent_run_id="source",
                input_path=input_path,
                settings=settings,
                cancellation_token=CancellationToken.never("generated"),
            )

    async def test_generate_rejects_non_image_reference_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            uploads_dir = Path(raw_tmp)
            request = Request({"type": "http", "headers": []})
            reference_poster = UploadFile(
                filename="reference.pdf",
                file=io.BytesIO(b"not a reference image"),
            )
            runs_before = set(web_server._RUNS)
            with (
                patch.object(web_server, "UPLOADS_DIR", uploads_dir),
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
                        conversation_id=None,
                        template=None,
                        files=[],
                        reference_poster=reference_poster,
                    )
            self.assertEqual(rejected.exception.status_code, 400)
            self.assertEqual(
                rejected.exception.detail["code"],
                "unsupported_reference_poster_image",
            )
            self.assertEqual(list(uploads_dir.iterdir()), [])
            self.assertEqual(set(web_server._RUNS), runs_before)

    async def test_generate_rejects_missing_palette_before_upload_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            uploads_dir = Path(raw_tmp)
            request = Request({"type": "http", "headers": []})
            with (
                patch.object(web_server, "UPLOADS_DIR", uploads_dir),
                patch.object(web_server, "_settings_for_request", return_value=SimpleNamespace()),
            ):
                with self.assertRaises(HTTPException) as missing:
                    await web_server.generate(
                        request,
                        brief="Create a poster",
                        artifact_type="poster",
                        palette_id=None,
                        baseline_artifact=None,
                        conversation_history=None,
                        prior_artifacts=None,
                        attachment_refs=None,
                        reference_poster_ref=None,
                        conversation_id=None,
                        template=None,
                        files=[],
                        reference_poster=None,
                    )
            self.assertEqual(missing.exception.status_code, 422)
            self.assertEqual(list(uploads_dir.iterdir()), [])

    async def test_edits_apply_requires_poster_palette_before_staging(self) -> None:
        required = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            out_dir = root / "out"
            final_dir = out_dir / "runs" / "source" / "final"
            final_dir.mkdir(parents=True)
            (final_dir / "poster.html").write_text(
                _poster_html(required),
                encoding="utf-8",
            )
            uploads_dir = root / "uploads"
            uploads_dir.mkdir()
            request = Request({"type": "http", "headers": []})
            with (
                patch.object(web_server, "UPLOADS_DIR", uploads_dir),
                patch.object(
                    web_server,
                    "_settings_for_request",
                    return_value=SimpleNamespace(out_dir=out_dir),
                ),
            ):
                with self.assertRaises(HTTPException) as missing:
                    await web_server.edits_apply(
                        request,
                        run_id="source",
                        artifact_type="poster",
                        palette_id=None,
                        edits_json="{}",
                        conversation_id="conv",
                    )
            self.assertEqual(missing.exception.status_code, 422)
            self.assertEqual(missing.exception.detail["code"], "poster_palette_required")
            self.assertEqual(list(uploads_dir.iterdir()), [])

    async def test_edits_apply_uses_detected_non_poster_type_for_palette_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            out_dir = root / "out"
            final_dir = out_dir / "runs" / "source" / "final"
            final_dir.mkdir(parents=True)
            (final_dir / "index.html").write_text(
                "<!doctype html><html><body><main>Landing page</main></body></html>",
                encoding="utf-8",
            )
            uploads_dir = root / "uploads"
            uploads_dir.mkdir()
            request = Request({"type": "http", "headers": []})
            with (
                patch.object(web_server, "UPLOADS_DIR", uploads_dir),
                patch.object(
                    web_server,
                    "_settings_for_request",
                    return_value=SimpleNamespace(out_dir=out_dir),
                ),
            ):
                with self.assertRaises(HTTPException) as unsupported:
                    await web_server.edits_apply(
                        request,
                        run_id="source",
                        artifact_type="poster",
                        palette_id="plum_sage",
                        edits_json="{}",
                        conversation_id="conv",
                    )
            self.assertEqual(unsupported.exception.status_code, 422)
            self.assertEqual(
                unsupported.exception.detail["code"],
                "palette_not_supported_for_artifact",
            )
            self.assertEqual(list(uploads_dir.iterdir()), [])

    async def test_edits_apply_blocks_wrong_final_poster_palette(self) -> None:
        required = require_academic_color_system("plum_sage")
        wrong = require_academic_color_system("mulberry_mint")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            poster_path = root / "poster.html"
            poster_path.write_text(
                _poster_html(wrong),
                encoding="utf-8",
            )
            with self.assertRaises(HTTPException) as invalid:
                web_server._validate_required_poster_palette_html(
                    poster_path,
                    required,
                )
            self.assertEqual(invalid.exception.status_code, 422)
            self.assertEqual(
                invalid.exception.detail["code"],
                "poster_palette_validation_failed",
            )

    async def test_edits_apply_validates_generated_final_poster(self) -> None:
        required = require_academic_color_system("plum_sage")
        wrong = require_academic_color_system("mulberry_mint")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            out_dir = root / "out"
            source_final = out_dir / "runs" / "source" / "final"
            source_final.mkdir(parents=True)
            (source_final / "poster.html").write_text(
                _poster_html(required),
                encoding="utf-8",
            )
            generated_run = out_dir / "runs" / "generated"
            with self.assertRaises(ArtifactEditJobError) as invalid:
                self._run_generated_poster_edit(
                    out_dir=out_dir,
                    generated_html=_poster_html(wrong),
                    required_color_system=required,
                )
            self.assertEqual(
                invalid.exception.detail["code"],
                "poster_palette_validation_failed",
            )
            self.assertFalse((generated_run / "final").exists())
            self.assertFalse(
                list(generated_run.rglob("authored_poster_edit_manifest.json"))
            )
            self.assertTrue(
                (
                    generated_run
                    / "quarantine"
                    / "palette_validation_failed"
                    / "poster.html"
                ).exists()
            )
            failure = json.loads(
                (generated_run / "apply_edits_palette_validation_failure.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(failure["status"], "error")
            self.assertEqual(failure["palette_id"], "plum_sage")
            self.assertEqual(
                failure["error"]["code"],
                "poster_palette_validation_failed",
            )

    async def test_edits_apply_blocks_staged_foreign_shell_style(self) -> None:
        required = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source_html = root / "source.html"
            source_html.write_text(
                _poster_html(
                    required,
                    extra_body=(
                        "<section class='poster-section' data-block-id='methods'>"
                        "<h2>Methods</h2></section>"
                    ),
                ),
                encoding="utf-8",
            )
            edits = {
                "layout": [{
                    "kind": "poster_style",
                    "scope": "section",
                    "section_id": "methods",
                    "styles": {"background": "#ff00aa"},
                }],
            }
            staged_html = root / "staged.html"
            web_server._patch_html_for_apply_edits(
                source_html,
                staged_html,
                edits,
                source_run_id="source",
            )
            with self.assertRaises(HTTPException) as invalid:
                web_server._validate_required_poster_palette_html(
                    staged_html,
                    required,
                )
            self.assertEqual(invalid.exception.status_code, 422)
            self.assertEqual(
                invalid.exception.detail["code"],
                "poster_palette_validation_failed",
            )

    async def test_edits_apply_blocks_generated_final_foreign_shell_style(self) -> None:
        required = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            out_dir = root / "out"
            source_final = out_dir / "runs" / "source" / "final"
            source_final.mkdir(parents=True)
            (source_final / "poster.html").write_text(
                _poster_html(required),
                encoding="utf-8",
            )
            generated_run = out_dir / "runs" / "generated"
            with self.assertRaises(ArtifactEditJobError) as invalid:
                self._run_generated_poster_edit(
                    out_dir=out_dir,
                    generated_html=_poster_html(
                        required,
                        extra_css=".poster-section{background:#f0a}",
                        extra_body="<section class='poster-section'>Methods</section>",
                    ),
                    required_color_system=required,
                )
            diagnostics = invalid.exception.detail["palette_diagnostics"]
            self.assertEqual(diagnostics[0]["shell_extra_colors"], ["#FF00AA"])
            self.assertTrue(
                (
                    generated_run
                    / "quarantine"
                    / "palette_validation_failed"
                    / "poster.html"
                ).exists()
            )

    async def test_edits_apply_allows_source_visual_extra_in_staged_and_final_html(self) -> None:
        required = require_academic_color_system("plum_sage")
        source_html = _poster_html(
            required,
            extra_css="[data-source-id='figure:1'] path{fill:#f0a}",
            extra_body=(
                "<svg data-source-id='figure:1' data-block-kind='chart'>"
                "<path></path></svg>"
            ),
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            out_dir = root / "out"
            source_final = out_dir / "runs" / "source" / "final"
            source_final.mkdir(parents=True)
            (source_final / "poster.html").write_text(source_html, encoding="utf-8")
            generated_run = out_dir / "runs" / "generated"
            result = self._run_generated_poster_edit(
                out_dir=out_dir,
                generated_html=source_html,
                required_color_system=required,
            )
            self.assertEqual(result["run_id"], "generated")
            self.assertTrue(
                (generated_run / "final" / "apply_edits_palette_manifest.json").exists()
            )


class WebPosterPaletteBackgroundTest(unittest.IsolatedAsyncioTestCase):
    async def test_background_runner_receives_state_palette(self) -> None:
        state = web_server._RunState(
            "poster",
            palette_id="plum_sage",
            conversation_id="conv",
        )
        web_server._RUNS["run_palette"] = state
        try:
            payload = web_server._legacy_pipeline_payload(
                brief="Create a poster",
                attach_paths=[],
                reference_poster_path=None,
                template=None,
                state=state,
                resume_run=None,
            )
            request = web_server._pipeline_request_factory(
                "run_palette",
                SimpleNamespace(),
                payload,
                {},
            )
        finally:
            web_server._RUNS.pop("run_palette", None)
        self.assertEqual(request.palette_id, "plum_sage")


class WebPosterPaletteCodeEditorTest(unittest.TestCase):
    def test_initial_candidate_blocks_missing_required_palette_id(self) -> None:
        required = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            layers_dir = run_dir / "layers"
            layers_dir.mkdir()
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=layers_dir,
                run_id="required-palette-missing-id",
            )
            ctx.state["required_color_system"] = required
            result = propose_paper_poster_html(
                {
                    "canvas": {"w_px": 900, "h_px": 640},
                    "html": _poster_html(required, include_palette_id=False),
                },
                ctx=ctx,
            )
        self.assertEqual(result.status, "error")
        self.assertEqual(result.payload.get("validation_stage"), "required_palette")
        self.assertTrue(
            any(
                issue.get("issue_id") == "paper_poster_html_palette_id_missing"
                for issue in result.payload.get("issues") or []
            )
        )

    def test_initial_candidate_without_required_palette_uses_legacy_path(self) -> None:
        selected = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            layers_dir = run_dir / "layers"
            layers_dir.mkdir()
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=layers_dir,
                run_id="legacy-no-required-palette",
            )
            result = propose_paper_poster_html(
                {
                    "canvas": {"w_px": 900, "h_px": 640},
                    "html": _poster_html(selected),
                },
                ctx=ctx,
            )
        self.assertNotEqual(
            result.payload.get("issue_id"),
            "paper_poster_html_required_palette_validation_failed",
        )
        self.assertNotEqual(result.payload.get("validation_stage"), "required_palette")

    def test_initial_candidate_blocks_another_known_required_palette(self) -> None:
        required = require_academic_color_system("plum_sage")
        other = require_academic_color_system("mulberry_mint")
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            layers_dir = run_dir / "layers"
            layers_dir.mkdir()
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=layers_dir,
                run_id="required-palette",
            )
            ctx.state["required_color_system"] = required
            result = propose_paper_poster_html(
                {
                    "canvas": {"w_px": 900, "h_px": 640},
                    "html": _poster_html(other),
                },
                ctx=ctx,
            )
        self.assertEqual(result.status, "error")
        self.assertEqual(
            result.payload.get("issue_id"),
            "paper_poster_html_required_palette_validation_failed",
        )
        self.assertEqual(
            (ctx.state.get("paper_poster_html_latest_candidate") or {}).get("status"),
            "validation_error",
        )

    def test_initial_candidate_blocks_required_css_variable_mismatch(self) -> None:
        required = require_academic_color_system("plum_sage")
        mismatched = {
            **required,
            "css_variables": {
                **required["css_variables"],
                "--poster-primary": "#010203",
            },
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            layers_dir = run_dir / "layers"
            layers_dir.mkdir()
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=layers_dir,
                run_id="required-palette-vars",
            )
            ctx.state["required_color_system"] = required
            result = propose_paper_poster_html(
                {
                    "canvas": {"w_px": 900, "h_px": 640},
                    "html": _poster_html(mismatched, palette_id="plum_sage"),
                },
                ctx=ctx,
            )
        self.assertEqual(result.status, "error")
        self.assertTrue(
            any(
                issue.get("issue_id")
                == "paper_poster_html_palette_css_variable_mismatch"
                for issue in result.payload.get("issues") or []
            )
        )

    def test_initial_candidate_blocks_foreign_required_palette_shell_color(self) -> None:
        required = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            layers_dir = run_dir / "layers"
            layers_dir.mkdir()
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=layers_dir,
                run_id="required-palette-foreign-shell",
            )
            ctx.state["required_color_system"] = required
            result = propose_paper_poster_html(
                {
                    "canvas": {"w_px": 900, "h_px": 640},
                    "html": _poster_html(
                        required,
                        extra_css=".poster-section{background:#f0a}",
                        extra_body="<section class='poster-section'>Methods</section>",
                    ),
                },
                ctx=ctx,
            )
        self.assertEqual(result.status, "error")
        self.assertEqual(result.payload.get("validation_stage"), "required_palette")
        extra_issues = [
            issue
            for issue in result.payload.get("issues") or []
            if issue.get("issue_id") == "paper_poster_html_palette_extra_authored_hex"
        ]
        self.assertEqual(extra_issues[0].get("shell_extra_colors"), ["#FF00AA"])

    def test_initial_candidate_allows_source_only_required_palette_extra(self) -> None:
        required = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            layers_dir = run_dir / "layers"
            layers_dir.mkdir()
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=layers_dir,
                run_id="required-palette-source-extra",
            )
            ctx.state["required_color_system"] = required
            result = propose_paper_poster_html(
                {
                    "canvas": {"w_px": 900, "h_px": 640},
                    "html": _poster_html(
                        required,
                        extra_css="[data-source-id='figure:1'] path{fill:#f0a}",
                        extra_body=(
                            "<svg data-source-id='figure:1' data-block-kind='chart'>"
                            "<path></path></svg>"
                        ),
                    ),
                },
                ctx=ctx,
            )
        self.assertNotEqual(result.payload.get("validation_stage"), "required_palette")
        diagnostics = ctx.state.get("paper_poster_html_palette_diagnostics") or []
        extra_issues = [
            issue
            for issue in diagnostics
            if issue.get("issue_id") == "paper_poster_html_palette_extra_authored_hex"
        ]
        self.assertEqual(extra_issues[0].get("shell_extra_colors"), [])
        self.assertEqual(extra_issues[0].get("source_visual_extra_colors"), ["#FF00AA"])

    def test_initial_candidate_without_required_palette_keeps_foreign_color_advisory(self) -> None:
        selected = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            layers_dir = run_dir / "layers"
            layers_dir.mkdir()
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=layers_dir,
                run_id="legacy-foreign-shell",
            )
            result = propose_paper_poster_html(
                {
                    "canvas": {"w_px": 900, "h_px": 640},
                    "html": _poster_html(
                        selected,
                        extra_css=".poster-section{background:#f0a}",
                        extra_body="<section class='poster-section'>Methods</section>",
                    ),
                },
                ctx=ctx,
            )
        self.assertNotEqual(result.payload.get("validation_stage"), "required_palette")

    def test_initial_candidate_unions_embedded_and_explicit_stylesheets(self) -> None:
        required = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            layers_dir = run_dir / "layers"
            layers_dir.mkdir()
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=layers_dir,
                run_id="required-palette-stylesheet-union",
            )
            ctx.state["required_color_system"] = required
            result = propose_paper_poster_html(
                {
                    "canvas": {"w_px": 900, "h_px": 640},
                    "html": _poster_html(
                        required,
                        extra_body="<section class='poster-section'>Methods</section>",
                    ),
                    "css": ".poster-section{background:#f0a}",
                },
                ctx=ctx,
            )
        self.assertEqual(result.status, "error")
        self.assertEqual(result.payload.get("validation_stage"), "required_palette")
        self.assertTrue(
            any(
                issue.get("shell_extra_colors") == ["#FF00AA"]
                for issue in result.payload.get("issues") or []
            )
        )

    def test_initial_normalization_preserves_embedded_style_media_semantics(self) -> None:
        required = require_academic_color_system("plum_sage")
        foreign_rule = ".poster-section{background:#f0a}"
        base_html = _poster_html(
            required,
            extra_body="<section class='poster-section'>Methods</section>",
        )
        cases = (
            (None, True, None),
            ("all", True, None),
            ("screen", True, "@media screen"),
            ("not all", False, "@media not all"),
        )
        for media, expected_shell_extra, expected_wrapper in cases:
            with self.subTest(media=media):
                media_attr = "" if media is None else f" media='{media}'"
                html = base_html.replace(
                    "</head>",
                    f"<style{media_attr}>{foreign_rule}</style></head>",
                )
                intact_diagnostics = authored_palette_diagnostics(
                    html,
                    "",
                    required,
                    require_selected=True,
                )
                normalized_body, normalized_css, _root_shell = (
                    _normalize_authored_html_document_with_root_shell(html, "")
                )
                normalized_html = (
                    "<main class='paper-poster' data-palette-id='plum_sage'>"
                    f"{normalized_body}</main>"
                )
                normalized_diagnostics = authored_palette_diagnostics(
                    normalized_html,
                    normalized_css,
                    required,
                    require_selected=True,
                )
                for diagnostics in (intact_diagnostics, normalized_diagnostics):
                    shell_extras = {
                        color
                        for diagnostic in diagnostics
                        for color in diagnostic.get("shell_extra_colors") or []
                    }
                    self.assertEqual(
                        shell_extras == {"#FF00AA"},
                        expected_shell_extra,
                        diagnostics,
                    )
                if expected_wrapper:
                    self.assertIn(expected_wrapper, normalized_css)
                else:
                    self.assertNotIn("@media all", normalized_css)

        html = base_html.replace(
            "</head>",
            f"<style media='not all'>{foreign_rule}</style></head>",
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            layers_dir = run_dir / "layers"
            layers_dir.mkdir()
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=layers_dir,
                run_id="required-palette-inactive-media",
            )
            ctx.state["required_color_system"] = required
            result = propose_paper_poster_html(
                {
                    "canvas": {"w_px": 900, "h_px": 640},
                    "html": html,
                },
                ctx=ctx,
            )
            rendered_candidate = Path(
                result.payload["candidate_measure_html"]
            ).read_text(encoding="utf-8")
        self.assertNotEqual(result.payload.get("validation_stage"), "required_palette")
        self.assertIn("@media not all", rendered_candidate)
        self.assertIn(foreign_rule, rendered_candidate)

    def test_revision_diagnostics_union_embedded_and_explicit_stylesheets(self) -> None:
        required = require_academic_color_system("plum_sage")
        diagnostics = authored_palette_diagnostics(
            _poster_html(
                required,
                extra_body="<section class='poster-section'>Methods</section>",
            ),
            ".poster-section{background:#f0a}",
            required,
            require_selected=True,
        )
        extra_issues = [
            issue
            for issue in diagnostics
            if issue.get("issue_id") == "paper_poster_html_palette_extra_authored_hex"
        ]
        self.assertEqual(extra_issues[0].get("shell_extra_colors"), ["#FF00AA"])

    def test_initial_candidate_blocks_registered_and_image_property_colors(self) -> None:
        required = require_academic_color_system("plum_sage")
        cases = (
            (
                "@property --foreign{syntax:'<color>';inherits:false;initial-value:#f0a}"
                ".poster-section{background:var(--foreign)}",
                "<section class='poster-section'>Methods</section>",
            ),
            (
                ".poster-section li{list-style-image:linear-gradient(#f0a,#f0a)}",
                "<section class='poster-section'><ul><li>Result</li></ul></section>",
            ),
            (
                ".poster-section{border:12px solid transparent;"
                "-webkit-border-image:linear-gradient(#f0a,#f0a) 1}",
                "<section class='poster-section'>Methods</section>",
            ),
        )
        for extra_css, extra_body in cases:
            with self.subTest(extra_css=extra_css), tempfile.TemporaryDirectory() as raw_tmp:
                run_dir = Path(raw_tmp)
                layers_dir = run_dir / "layers"
                layers_dir.mkdir()
                ctx = ToolContext(
                    settings=SimpleNamespace(),
                    run_dir=run_dir,
                    layers_dir=layers_dir,
                    run_id="required-palette-visible-property",
                )
                ctx.state["required_color_system"] = required
                result = propose_paper_poster_html(
                    {
                        "canvas": {"w_px": 900, "h_px": 640},
                        "html": _poster_html(
                            required,
                            extra_css=extra_css,
                            extra_body=extra_body,
                        ),
                    },
                    ctx=ctx,
                )
            self.assertEqual(
                result.payload.get("validation_stage"),
                "required_palette",
                (extra_css, result.payload),
            )

    def test_code_editor_blocks_another_known_palette(self) -> None:
        required = require_academic_color_system("plum_sage")
        other = require_academic_color_system("mulberry_mint")
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(_poster_html(other), encoding="utf-8")
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertFalse(result["ok"])
        self.assertTrue(any("required palette" in item.lower() for item in result["errors"]))

    def test_code_editor_blocks_required_css_variable_mismatch(self) -> None:
        required = require_academic_color_system("plum_sage")
        mismatched = {
            **required,
            "css_variables": {
                **required["css_variables"],
                "--poster-primary": "#010203",
            },
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(
                _poster_html(mismatched, palette_id="plum_sage"),
                encoding="utf-8",
            )
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertFalse(result["ok"])
        self.assertTrue(any("css variable" in item.lower() for item in result["errors"]))

    def test_code_editor_accepts_exact_required_palette(self) -> None:
        required = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(_poster_html(required), encoding="utf-8")
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertTrue(result["ok"], result)

    def test_code_editor_blocks_foreign_section_shell_color(self) -> None:
        required = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(
                _poster_html(
                    required,
                    extra_css=".poster-section{background:#6F2DA8}",
                    extra_body="<section class='poster-section'>Methods</section>",
                ),
                encoding="utf-8",
            )
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertFalse(result["ok"])
        self.assertTrue(any("foreign shell" in item.lower() for item in result["errors"]))

    def test_code_editor_blocks_practical_foreign_shell_color_syntaxes(self) -> None:
        required = require_academic_color_system("plum_sage")
        declarations = (
            "background:#f0a",
            "background:#f0af",
            "background:#ff00aacc",
            "background:rgb(255,0,170)",
            "background:rgba(255,0,170,0.5)",
            "background:hsl(320 100% 50%)",
            "background:hsla(320,100%,50%,0.5)",
            "background:deeppink",
            "background:linear-gradient(90deg,var(--poster-bg),rgb(255 0 170 / 50%))",
        )
        for declaration in declarations:
            with self.subTest(declaration=declaration), tempfile.TemporaryDirectory() as raw_tmp:
                attempt_dir = Path(raw_tmp)
                poster = attempt_dir / "poster.html"
                poster.write_text(
                    _poster_html(
                        required,
                        extra_css=f".poster-section{{{declaration}}}",
                        extra_body="<section class='poster-section'>Methods</section>",
                    ),
                    encoding="utf-8",
                )
                result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                    attempt_dir,
                    poster,
                    required_color_system=required,
                )
            self.assertFalse(result["ok"], (declaration, result))
            self.assertTrue(
                any("foreign shell" in item.lower() for item in result["errors"]),
                (declaration, result),
            )

    def test_code_editor_blocks_css_color_four_absolute_functions(self) -> None:
        required = require_academic_color_system("plum_sage")
        declarations = (
            "background:lab(60% 80 -20)",
            "background:lch(60% 90 330)",
            "background:hwb(330 0% 0%)",
            "background:oklab(65% .2 -.1)",
            "background:oklch(65% .25 330)",
            "background:color(display-p3 1 0 .4)",
            "background:device-cmyk(0% 100% 0% 0%)",
            "background:linear-gradient(90deg,oklch(65% .25 330),lab(60% 80 -20))",
        )
        for declaration in declarations:
            with self.subTest(declaration=declaration), tempfile.TemporaryDirectory() as raw_tmp:
                attempt_dir = Path(raw_tmp)
                poster = attempt_dir / "poster.html"
                poster.write_text(
                    _poster_html(
                        required,
                        extra_css=f".poster-section{{{declaration}}}",
                        extra_body="<section class='poster-section'>Methods</section>",
                    ),
                    encoding="utf-8",
                )
                result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                    attempt_dir,
                    poster,
                    required_color_system=required,
                )
            self.assertFalse(result["ok"], (declaration, result))
            self.assertTrue(
                any("foreign shell" in item.lower() for item in result["errors"]),
                (declaration, result),
            )

    def test_code_editor_warns_for_source_only_css_color_four_function(self) -> None:
        required = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(
                _poster_html(
                    required,
                    extra_css=(
                        "[data-source-id='figure:1'] path{"
                        "fill:linear-gradient(oklch(65% .25 330),lab(60% 80 -20))}"
                    ),
                    extra_body=(
                        "<svg data-source-id='figure:1' data-block-kind='chart'>"
                        "<path></path></svg>"
                    ),
                ),
                encoding="utf-8",
            )
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("source visual" in item.lower() for item in result["warnings"]))

    def test_code_editor_blocks_colors_in_recursive_css_rules(self) -> None:
        required = require_academic_color_system("plum_sage")
        rules = (
            "@keyframes flash{from{background:#f0a}to{background:var(--poster-bg)}}",
            "@-webkit-keyframes flash{0%{color:#f0a}100%{color:var(--poster-text)}}",
            "@scope (.paper-poster){.poster-section{background:#f0a}}",
            "@document url-prefix(){.poster-section{background:#f0a}}",
            ".poster-section{&:hover{background:#f0a}}",
        )
        for css_rule in rules:
            with self.subTest(css_rule=css_rule), tempfile.TemporaryDirectory() as raw_tmp:
                attempt_dir = Path(raw_tmp)
                poster = attempt_dir / "poster.html"
                poster.write_text(
                    _poster_html(
                        required,
                        extra_css=css_rule,
                        extra_body="<section class='poster-section'>Methods</section>",
                    ),
                    encoding="utf-8",
                )
                result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                    attempt_dir,
                    poster,
                    required_color_system=required,
                )
            self.assertFalse(result["ok"], (css_rule, result))
            self.assertTrue(
                any("foreign shell" in item.lower() for item in result["errors"]),
                (css_rule, result),
            )

    def test_code_editor_preserves_explicit_source_scope_and_nested_rule_exemption(self) -> None:
        required = require_academic_color_system("plum_sage")
        source_rules = (
            "@scope ([data-source-id='figure:1']){path{fill:#f0a}}",
            "[data-source-id='figure:1']{path{fill:#f0a}}",
        )
        for css_rule in source_rules:
            with self.subTest(css_rule=css_rule), tempfile.TemporaryDirectory() as raw_tmp:
                attempt_dir = Path(raw_tmp)
                poster = attempt_dir / "poster.html"
                poster.write_text(
                    _poster_html(
                        required,
                        extra_css=css_rule,
                        extra_body=(
                            "<svg data-source-id='figure:1' data-block-kind='chart'>"
                            "<path></path></svg>"
                        ),
                    ),
                    encoding="utf-8",
                )
                result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                    attempt_dir,
                    poster,
                    required_color_system=required,
                )
            self.assertTrue(result["ok"], (css_rule, result))
            self.assertTrue(
                any("source visual" in item.lower() for item in result["warnings"]),
                (css_rule, result),
            )

    def test_code_editor_blocks_registered_and_image_property_colors(self) -> None:
        required = require_academic_color_system("plum_sage")
        cases = (
            (
                "@property --foreign{syntax:'<color>';inherits:false;initial-value:#f0a}"
                ".poster-section{background:var(--foreign)}",
                "<section class='poster-section'>Methods</section>",
            ),
            (
                ".poster-section li{list-style-image:linear-gradient(#f0a,#f0a)}",
                "<section class='poster-section'><ul><li>Result</li></ul></section>",
            ),
            (
                ".poster-section{mask-image:linear-gradient(#f0a,#f0a);"
                "shape-outside:linear-gradient(#f0a,#f0a)}",
                "<section class='poster-section'>Methods</section>",
            ),
            (
                ".poster-section{border:12px solid transparent;"
                "-webkit-border-image:linear-gradient(#f0a,#f0a) 1}",
                "<section class='poster-section'>Methods</section>",
            ),
        )
        for extra_css, extra_body in cases:
            with self.subTest(extra_css=extra_css), tempfile.TemporaryDirectory() as raw_tmp:
                attempt_dir = Path(raw_tmp)
                poster = attempt_dir / "poster.html"
                poster.write_text(
                    _poster_html(required, extra_css=extra_css, extra_body=extra_body),
                    encoding="utf-8",
                )
                result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                    attempt_dir,
                    poster,
                    required_color_system=required,
                )
            self.assertFalse(result["ok"], (extra_css, result))
            self.assertTrue(
                any("foreign shell" in item.lower() for item in result["errors"]),
                (extra_css, result),
            )

    def test_code_editor_ignores_quoted_and_url_image_property_payloads(self) -> None:
        required = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(
                _poster_html(
                    required,
                    extra_css=(
                        ".poster-section::before{content:'red #f0a';"
                        "list-style-image:url('#f0a');mask-image:url('#f0a')}"
                    ),
                    extra_body="<section class='poster-section'>Methods</section>",
                ),
                encoding="utf-8",
            )
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertTrue(result["ok"], result)

    def test_code_editor_allows_source_scope_root_and_dynamic_pseudo_colors(self) -> None:
        required = require_academic_color_system("plum_sage")
        source_rules = (
            (
                "@scope ([data-source-id='figure:1']){"
                ":scope{--plot:#f0a;fill:var(--plot)}}"
            ),
            (
                "@scope ([data-source-id='figure:1']){"
                "rect:hover{fill:#f0a}}"
            ),
            (
                "[data-source-id='figure:1'] rect:is(:hover,:focus){fill:#f0a}"
            ),
            (
                "[data-source-id='figure:1'] rect:where(:hover,:focus){fill:#f0a}"
            ),
            (
                "[data-source-id='figure:1'] rect:not(:hover){fill:#f0a}"
            ),
            (
                "[data-source-id='figure:1']:has(rect:hover){fill:#f0a}"
            ),
        )
        for css_rule in source_rules:
            with self.subTest(css_rule=css_rule), tempfile.TemporaryDirectory() as raw_tmp:
                attempt_dir = Path(raw_tmp)
                poster = attempt_dir / "poster.html"
                poster.write_text(
                    _poster_html(
                        required,
                        extra_css=css_rule,
                        extra_body=(
                            "<svg data-source-id='figure:1' data-block-kind='chart'>"
                            "<rect width='20' height='20'></rect></svg>"
                        ),
                    ),
                    encoding="utf-8",
                )
                result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                    attempt_dir,
                    poster,
                    required_color_system=required,
                )
            self.assertTrue(result["ok"], (css_rule, result))
            self.assertTrue(
                any("source visual" in item.lower() for item in result["warnings"]),
                (css_rule, result),
            )

        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(
                _poster_html(
                    required,
                    extra_css=(
                        "[data-source-id='figure:2'] figcaption::before{"
                        "content:'';color:#f0a}"
                    ),
                    extra_body=(
                        "<figure data-source-id='figure:2' data-block-kind='chart'>"
                        "<figcaption>Result</figcaption></figure>"
                    ),
                ),
                encoding="utf-8",
            )
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("source visual" in item.lower() for item in result["warnings"]))

    def test_code_editor_blocks_shell_and_mixed_dynamic_scope_colors(self) -> None:
        required = require_academic_color_system("plum_sage")
        controls = (
            (
                "@scope (.poster-section){:scope{background:#f0a}}",
                "<section class='poster-section'>Methods</section>",
            ),
            (
                "@scope (.poster-section){div:hover{background:#f0a}}",
                "<section class='poster-section'><div>Methods</div></section>",
            ),
            (
                ".poster-section::before{content:'';color:#f0a}",
                "<section class='poster-section'>Methods</section>",
            ),
            (
                "@scope ([data-source-id='figure:1'],.poster-section){"
                ":scope{fill:#f0a}}",
                (
                    "<svg data-source-id='figure:1' data-block-kind='chart'>"
                    "<rect></rect></svg><section class='poster-section'>Methods</section>"
                ),
            ),
            (
                "[data-source-id='figure:1'] rect:hover,.poster-section:hover{fill:#f0a}",
                (
                    "<svg data-source-id='figure:1' data-block-kind='chart'>"
                    "<rect></rect></svg><section class='poster-section'>Methods</section>"
                ),
            ),
            (
                ".poster-section div:is(:hover,:focus){background:#f0a}",
                "<section class='poster-section'><div>Methods</div></section>",
            ),
            (
                ".poster-section div:where(:hover,:focus){background:#f0a}",
                "<section class='poster-section'><div>Methods</div></section>",
            ),
            (
                ".poster-section div:not(:hover){background:#f0a}",
                "<section class='poster-section'><div>Methods</div></section>",
            ),
            (
                ".poster-section:has(div:hover){background:#f0a}",
                "<section class='poster-section'><div>Methods</div></section>",
            ),
            (
                ":is([data-source-id='figure:1'] rect:hover,.poster-section:hover)"
                "{fill:#f0a}",
                (
                    "<svg data-source-id='figure:1' data-block-kind='chart'>"
                    "<rect></rect></svg><section class='poster-section'>Methods</section>"
                ),
            ),
            (
                "[data-source-id='figure:1'] rect:is(:hover,,.active){fill:#f0a}",
                (
                    "<svg data-source-id='figure:1' data-block-kind='chart'>"
                    "<rect></rect></svg>"
                ),
            ),
        )
        for css_rule, extra_body in controls:
            with self.subTest(css_rule=css_rule), tempfile.TemporaryDirectory() as raw_tmp:
                attempt_dir = Path(raw_tmp)
                poster = attempt_dir / "poster.html"
                poster.write_text(
                    _poster_html(required, extra_css=css_rule, extra_body=extra_body),
                    encoding="utf-8",
                )
                result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                    attempt_dir,
                    poster,
                    required_color_system=required,
                )
            self.assertFalse(result["ok"], (css_rule, result))
            self.assertTrue(
                any("foreign shell" in item.lower() for item in result["errors"]),
                (css_rule, result),
            )

    def test_code_editor_accepts_canonical_palette_rgb_with_alpha(self) -> None:
        required = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(
                _poster_html(
                    required,
                    extra_css=(
                        ".poster-section{background:rgb(0 126 120 / 50%);"
                        "border-color:#007E7880;box-shadow:0 0 2px rgba(0,126,120,.1)}"
                    ),
                    extra_body="<section class='poster-section'>Methods</section>",
                ),
                encoding="utf-8",
            )
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertTrue(result["ok"], result)

    def test_code_editor_accepts_canonical_variables_and_ignored_color_keywords(self) -> None:
        required = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(
                _poster_html(
                    required,
                    extra_css=(
                        ".poster-section{--local-bg:rgb(255 255 255);"
                        "background:var(--local-bg);color:currentColor;"
                        "border-color:transparent;outline-color:inherit;fill:none;"
                        "background-image:url('#f0a')}"
                    ),
                    extra_body="<section class='poster-section'>Methods</section>",
                ),
                encoding="utf-8",
            )
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertTrue(result["ok"], result)

    def test_code_editor_blocks_foreign_shell_svg_presentation_colors(self) -> None:
        required = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(
                _poster_html(
                    required,
                    extra_body=(
                        "<svg><defs><linearGradient id='g'>"
                        "<stop stop-color='hsl(320 100% 50%)'></stop>"
                        "</linearGradient></defs><path fill='#f0a' stroke='rgb(255,0,170)' "
                        "color='deeppink' flood-color='#f0af' lighting-color='#ff00aacc'></path>"
                        "</svg>"
                    ),
                ),
                encoding="utf-8",
            )
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertFalse(result["ok"], result)
        self.assertTrue(any("foreign shell" in item.lower() for item in result["errors"]))

    def test_code_editor_does_not_exempt_unprovenanced_scientific_class(self) -> None:
        required = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(
                _poster_html(
                    required,
                    extra_css=".scientific-mark{color:#010203}",
                    extra_body="<span class='scientific-mark'>Result</span>",
                ),
                encoding="utf-8",
            )
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertFalse(result["ok"])
        self.assertTrue(any("foreign shell" in item.lower() for item in result["errors"]))

    def test_code_editor_warns_for_source_visual_hex(self) -> None:
        required = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(
                _poster_html(
                    required,
                    extra_css="[data-source-id='figure:1'] .scientific-mark{fill:#010203}",
                    extra_body=(
                        "<svg data-source-id='figure:1' data-block-kind='chart'>"
                        "<path class='scientific-mark'></path></svg>"
                    ),
                ),
                encoding="utf-8",
            )
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("source visual" in item.lower() for item in result["warnings"]))

    def test_code_editor_warns_for_source_visual_svg_presentation_colors(self) -> None:
        required = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(
                _poster_html(
                    required,
                    extra_body=(
                        "<svg data-source-id='figure:1' data-block-kind='chart'>"
                        "<defs><linearGradient id='g'><stop stop-color='hsl(320 100% 50%)'>"
                        "</stop></linearGradient></defs>"
                        "<path fill='#f0a' stroke='rgb(255,0,170)' color='deeppink' "
                        "flood-color='#f0af' lighting-color='#ff00aacc'></path></svg>"
                    ),
                ),
                encoding="utf-8",
            )
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("source visual" in item.lower() for item in result["warnings"]))

    def test_unrelated_and_commented_variables_cannot_satisfy_root_palette(self) -> None:
        required = require_academic_color_system("plum_sage")
        wrong = require_academic_color_system("mulberry_mint")
        canonical = _palette_declarations(required)
        wrong_declarations = _palette_declarations(wrong)
        html = (
            "<!doctype html><html><head><style>"
            f".paper-poster{{{wrong_declarations};width:3072px;height:1536px}}"
            f"/* .paper-poster{{{canonical}}} */"
            f".unrelated,.paper-poster .child{{{canonical}}}"
            "</style></head><body>"
            "<main class='paper-poster' data-palette-id='plum_sage'>"
            "<p class='child'>Grounded poster content with enough text.</p>"
            "</main></body></html>"
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(html, encoding="utf-8")
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertFalse(result["ok"])
        self.assertTrue(any("css variable" in item.lower() for item in result["errors"]))

    def test_conflicting_root_override_is_blocking(self) -> None:
        required = require_academic_color_system("plum_sage")
        wrong = require_academic_color_system("mulberry_mint")
        html = (
            "<!doctype html><html><head><style>"
            f".paper-poster{{{_palette_declarations(wrong)}}}"
            f"main.paper-poster{{{_palette_declarations(required)};width:3072px;height:1536px}}"
            "</style></head><body>"
            "<main class='paper-poster' data-palette-id='plum_sage'>"
            "<p>Grounded poster content with enough text for validation.</p>"
            "</main></body></html>"
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(html, encoding="utf-8")
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertFalse(result["ok"])
        self.assertTrue(any("css variable" in item.lower() for item in result["errors"]))

    def test_comments_inside_real_root_rule_are_supported(self) -> None:
        required = require_academic_color_system("plum_sage")
        declarations = ";".join(
            f"{name}:/* exact */{value}"
            for name, value in required["css_variables"].items()
        )
        html = (
            "<!doctype html><html><head><style>"
            f".paper-poster/* selected root */{{{declarations};width:3072px;height:1536px}}"
            "</style></head><body>"
            "<main class='paper-poster' data-palette-id='plum_sage'>"
            "<p>Grounded poster content with enough text for validation.</p>"
            "</main></body></html>"
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(html, encoding="utf-8")
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertTrue(result["ok"], result)

    def test_conflicting_root_inline_override_is_blocking(self) -> None:
        required = require_academic_color_system("plum_sage")
        wrong = require_academic_color_system("mulberry_mint")
        html = (
            "<!doctype html><html><head><style>"
            f".paper-poster{{{_palette_declarations(wrong)};width:3072px;height:1536px}}"
            "</style></head><body>"
            f"<main class='paper-poster' data-palette-id='plum_sage' "
            f"style='{_palette_declarations(required)}'>"
            "<p>Grounded poster content with enough text for validation.</p>"
            "</main></body></html>"
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(html, encoding="utf-8")
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertFalse(result["ok"])

    def test_noncanonical_root_declarations_cannot_satisfy_palette(self) -> None:
        required = require_academic_color_system("plum_sage")
        declarations = _palette_declarations(required)
        uppercase_declarations = declarations.replace("--poster-", "--POSTER-")
        html = (
            "<!doctype html><html><head><style>"
            f".paper-poster{{{uppercase_declarations};{declarations};"
            "--poster-bg:var(--poster-surface);width:3072px;height:1536px}"
            "</style></head><body>"
            "<main class='paper-poster' data-palette-id='plum_sage'>"
            "<p>Grounded poster content with enough text for validation.</p>"
            "</main></body></html>"
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(html, encoding="utf-8")
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertFalse(result["ok"])
        self.assertTrue(any("css variable" in item.lower() for item in result["errors"]))

    def test_conditional_only_root_variables_do_not_satisfy_palette(self) -> None:
        required = require_academic_color_system("plum_sage")
        declarations = _palette_declarations(required)
        conditional_sources = (
            f"@media all{{.paper-poster{{{declarations}}}}}",
            f"@supports (display:grid){{.paper-poster{{{declarations}}}}}",
            f"@container poster (min-width:1px){{.paper-poster{{{declarations}}}}}",
        )
        for source in conditional_sources:
            with self.subTest(source=source.split("{", 1)[0]):
                html = (
                    "<!doctype html><html><head><style>"
                    f"{source}"
                    ".paper-poster{width:3072px;height:1536px}"
                    "</style></head><body>"
                    "<main class='paper-poster' data-palette-id='plum_sage'>"
                    "<p>Grounded poster content with enough text for validation.</p>"
                    "</main></body></html>"
                )
                with tempfile.TemporaryDirectory() as raw_tmp:
                    attempt_dir = Path(raw_tmp)
                    poster = attempt_dir / "poster.html"
                    poster.write_text(html, encoding="utf-8")
                    result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                        attempt_dir,
                        poster,
                        required_color_system=required,
                    )
                self.assertFalse(result["ok"])
                self.assertTrue(
                    any("css variable" in item.lower() for item in result["errors"])
                )

    def test_style_media_screen_does_not_supply_foundational_variables(self) -> None:
        required = require_academic_color_system("plum_sage")
        html = (
            "<!doctype html><html><head>"
            f"<style media='screen'>.paper-poster{{{_palette_declarations(required)}}}</style>"
            "<style>.paper-poster{width:3072px;height:1536px}</style>"
            "</head><body>"
            "<main class='paper-poster' data-palette-id='plum_sage'>"
            "<p>Grounded poster content with enough text for validation.</p>"
            "</main></body></html>"
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(html, encoding="utf-8")
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertFalse(result["ok"])
        self.assertTrue(any("css variable" in item.lower() for item in result["errors"]))

    def test_style_media_all_supplies_foundational_variables(self) -> None:
        required = require_academic_color_system("plum_sage")
        html = (
            "<!doctype html><html><head>"
            f"<style media='all'>.paper-poster{{{_palette_declarations(required)}}}</style>"
            "</head><body>"
            "<main class='paper-poster' data-palette-id='plum_sage'>"
            "<p>Grounded poster content with enough text for validation.</p>"
            "</main></body></html>"
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(html, encoding="utf-8")
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertTrue(result["ok"], result)

    def test_noncanonical_potential_conditional_overrides_are_blocking(self) -> None:
        required = require_academic_color_system("plum_sage")
        declarations = _palette_declarations(required)
        wrong_override = "--poster-bg:#010203"
        conditional_sources = (
            f"<style>.paper-poster{{{declarations}}}@media all{{.paper-poster{{{wrong_override}}}}}</style>",
            f"<style>.paper-poster{{{declarations}}}@supports (display:grid){{.paper-poster{{{wrong_override}}}}}</style>",
            f"<style>.paper-poster{{{declarations}}}@container poster (min-width:1px){{.paper-poster{{{wrong_override}}}}}</style>",
            f"<style>.paper-poster{{{declarations}}}</style><style media='screen'>.paper-poster{{{wrong_override}}}</style>",
        )
        for style_source in conditional_sources:
            with self.subTest(style=style_source):
                html = (
                    "<!doctype html><html><head>"
                    f"{style_source}"
                    "<style>.paper-poster{width:3072px;height:1536px}</style>"
                    "</head><body>"
                    "<main class='paper-poster' data-palette-id='plum_sage'>"
                    "<p>Grounded poster content with enough text for validation.</p>"
                    "</main></body></html>"
                )
                with tempfile.TemporaryDirectory() as raw_tmp:
                    attempt_dir = Path(raw_tmp)
                    poster = attempt_dir / "poster.html"
                    poster.write_text(html, encoding="utf-8")
                    result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                        attempt_dir,
                        poster,
                        required_color_system=required,
                    )
                self.assertFalse(result["ok"])
                self.assertTrue(
                    any("css variable" in item.lower() for item in result["errors"])
                )

    def test_canonical_potential_conditional_overrides_are_allowed(self) -> None:
        required = require_academic_color_system("plum_sage")
        declarations = _palette_declarations(required)
        html = (
            "<!doctype html><html><head>"
            f"<style>.paper-poster{{{declarations}}}"
            f"@media all{{.paper-poster{{{declarations}}}}}"
            f"@supports (display:grid){{.paper-poster{{{declarations}}}}}"
            f"@container poster (min-width:1px){{.paper-poster{{{declarations}}}}}"
            "</style>"
            f"<style media='screen'>.paper-poster{{{declarations}}}</style>"
            "</head><body>"
            "<main class='paper-poster' data-palette-id='plum_sage'>"
            "<p>Grounded poster content with enough text for validation.</p>"
            "</main></body></html>"
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(html, encoding="utf-8")
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertTrue(result["ok"], result)

    def test_explicit_not_all_overrides_are_ignored(self) -> None:
        required = require_academic_color_system("plum_sage")
        declarations = _palette_declarations(required)
        wrong_override = "--poster-bg:#010203"
        html = (
            "<!doctype html><html><head>"
            f"<style>.paper-poster{{{declarations}}}"
            f"@media /* inactive */ not /**/ all{{.paper-poster{{{wrong_override}}}}}"
            "</style>"
            f"<style media='  not /**/ all  '>.paper-poster{{{wrong_override}}}</style>"
            "</head><body>"
            "<main class='paper-poster' data-palette-id='plum_sage'>"
            "<p>Grounded poster content with enough text for validation.</p>"
            "</main></body></html>"
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(html, encoding="utf-8")
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertTrue(result["ok"], result)

    def test_not_all_with_condition_is_a_potential_override(self) -> None:
        required = require_academic_color_system("plum_sage")
        declarations = _palette_declarations(required)
        wrong_override = "--poster-bg:#010203"
        conditional_sources = (
            f"<style>.paper-poster{{{declarations}}}"
            f"@media not all and (min-width:1px){{.paper-poster{{{wrong_override}}}}}"
            "</style>",
            f"<style>.paper-poster{{{declarations}}}</style>"
            f"<style media='not all and (min-width:1px)'>"
            f".paper-poster{{{wrong_override}}}</style>",
        )
        for style_source in conditional_sources:
            with self.subTest(style=style_source):
                html = (
                    "<!doctype html><html><head>"
                    f"{style_source}"
                    "</head><body>"
                    "<main class='paper-poster' data-palette-id='plum_sage'>"
                    "<p>Grounded poster content with enough text for validation.</p>"
                    "</main></body></html>"
                )
                with tempfile.TemporaryDirectory() as raw_tmp:
                    attempt_dir = Path(raw_tmp)
                    poster = attempt_dir / "poster.html"
                    poster.write_text(html, encoding="utf-8")
                    result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                        attempt_dir,
                        poster,
                        required_color_system=required,
                    )
                self.assertFalse(result["ok"])
                self.assertTrue(
                    any("css variable" in item.lower() for item in result["errors"])
                )

    def test_unconditional_layer_can_supply_foundational_variables(self) -> None:
        required = require_academic_color_system("plum_sage")
        declarations = _palette_declarations(required)
        html = (
            "<!doctype html><html><head>"
            f"<style>@layer palette{{.paper-poster{{{declarations}}}}}</style>"
            "</head><body>"
            "<main class='paper-poster' data-palette-id='plum_sage'>"
            "<p>Grounded poster content with enough text for validation.</p>"
            "</main></body></html>"
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            attempt_dir = Path(raw_tmp)
            poster = attempt_dir / "poster.html"
            poster.write_text(html, encoding="utf-8")
            result = ExternalCodeEditor(SimpleNamespace())._validate_output(
                attempt_dir,
                poster,
                required_color_system=required,
            )
        self.assertTrue(result["ok"], result)

    def test_duplicate_extracted_stylesheet_is_parsed_once(self) -> None:
        required = require_academic_color_system("plum_sage")
        css = f".paper-poster{{{_palette_declarations(required)}}}"
        soup = BeautifulSoup(
            "<!doctype html><html><head>"
            f"<style>{css}</style>"
            "</head><body>"
            "<main class='paper-poster' data-palette-id='plum_sage'></main>"
            "</body></html>",
            "html.parser",
        )
        values = _authored_poster_css_variable_values(soup, css)
        self.assertTrue(values)
        self.assertTrue(all(len(items) == 1 for items in values.values()))

    def test_authored_edit_manifest_persists_required_palette(self) -> None:
        required = require_academic_color_system("plum_sage")
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source_final = root / "source" / "final"
            source_final.mkdir(parents=True)
            source = source_final / "poster.html"
            source.write_text(_poster_html(required), encoding="utf-8")
            staged = root / "staged.html"
            staged.write_text(_poster_html(required), encoding="utf-8")
            settings = SimpleNamespace(out_dir=root, poster_preview_max_edge=1024)
            browser_result = SimpleNamespace(
                backend="test",
                warnings=[],
                scale=1.0,
                width_px=3072,
                height_px=1536,
            )
            with (
                patch.object(web_server, "new_run_id", return_value="edited"),
                patch.object(web_server, "screenshot_html", return_value=browser_result),
            ):
                result = web_server._apply_authored_paper_poster_edits(
                    source,
                    staged,
                    settings,
                    "source",
                    {},
                    required_color_system=required,
                )
            final_dir = Path(result.run_dir) / "final"
            manifest_path = final_dir / "authored_poster_edit_manifest.json"
            self.assertFalse(manifest_path.exists())
            web_server._validate_required_poster_palette_html(
                final_dir / "poster.html",
                required,
            )
            web_server._persist_apply_edits_palette_manifest(
                Path(result.run_dir),
                "plum_sage",
                required,
            )
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        self.assertEqual(manifest["palette_id"], "plum_sage")
        self.assertEqual(manifest["required_color_system"], required)


if __name__ == "__main__":
    unittest.main()
