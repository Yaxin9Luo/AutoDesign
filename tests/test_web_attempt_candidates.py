from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from starlette.requests import Request

from autodesign.attempt_candidates import capture_attempt_candidate
from autodesign.attempt_candidates import load_attempt_candidate
from autodesign.attempt_fork import run_attempt_fork_job
from autodesign.candidate_publish import (
    _publish_video_delivery_context,
    reconcile_video_delivery_context_promotion,
    run_candidate_publish_job,
    validate_video_delivery_context_promotion,
)
from autodesign.attempt_selection import complete_source_run_with_candidate_fork
from autodesign.agents.atomic_artifact_promotion import publish_artifact_directory
from autodesign.attempt_selection import request_attempt_selection
from autodesign.artifact_edit_job import run_artifact_edit_job
from autodesign.run_control import CancellationToken, RunCancelled, RunControlStore
from autodesign.run_supervisor import TerminalReconciliation
from autodesign.schema import ToolResultRecord
from autodesign.tools import ToolContext
from autodesign.tools.ingest_document import _load_ingest_state_from_dir
from autodesign.util.io import sha256_file
from scripts import web_server


def _request() -> Request:
    return Request({"type": "http", "headers": []})


class WebAttemptCandidateTests(unittest.TestCase):
    def _run_artifact_edit(
        self,
        *,
        out_dir: Path,
        parent_run_id: str,
        child_run_id: str,
        artifact_type: str,
        source_name: str,
        edited_html: str,
        edits: dict[str, object],
        candidate_lineage: dict[str, object],
        required_color_system: dict[str, object] | None = None,
    ) -> dict[str, object]:
        child_dir = out_dir / "runs" / child_run_id
        uploads_dir = child_dir / "uploads"
        uploads_dir.mkdir(parents=True)
        staged_html = uploads_dir / source_name
        staged_html.write_text(edited_html, encoding="utf-8")
        input_path = uploads_dir / "artifact_edit.json"
        input_path.write_text(
            json.dumps({
                "version": 1,
                "artifact_type": artifact_type,
                "source_relative_path": f"final/{source_name}",
                "edited_html_relative_path": f"uploads/{source_name}",
                "edits": edits,
                "required_color_system": required_color_system or {},
                "candidate_lineage": candidate_lineage,
            }),
            encoding="utf-8",
        )
        settings = replace(web_server._runtime_only_settings(), out_dir=out_dir)
        return run_artifact_edit_job(
            run_id=child_run_id,
            parent_run_id=parent_run_id,
            input_path=input_path,
            settings=settings,
            cancellation_token=CancellationToken.never(child_run_id),
        )

    def _materialize_fork(self, runs_dir: Path, attempt: int = 1):
        candidate = load_attempt_candidate(runs_dir / "run-source", attempt)
        draft_run_dir = runs_dir / "draft-run"
        draft_run_dir.mkdir()
        result = run_attempt_fork_job(
            run_id="draft-run",
            parent_run_id="run-source",
            attempt=attempt,
            expected_candidate_sha256=candidate.source_sha256,
            conversation_id="conv-1",
            settings=web_server._runtime_only_settings(),
            cancellation_token=CancellationToken.never("draft-run"),
            runs_dir=runs_dir,
        )
        return web_server._candidate_draft_artifact_from_lineage(
            draft_run_dir,
            "draft-run",
            str(result["artifact_type"]),
            result["lineage"],
            source=Path(str(result["source_path"])),
        )

    def _landing_candidate(self, runs_dir: Path):
        run_dir = runs_dir / "run-source"
        attempt_dir = run_dir / "landing_author" / "attempt_01"
        attempt_dir.mkdir(parents=True)
        (attempt_dir / "index.html").write_text(
            "<!doctype html><html><body><main><h1>Draft</h1></main></body></html>",
            encoding="utf-8",
        )
        (attempt_dir / "preview.png").write_bytes(b"preview")
        (attempt_dir / "validation.json").write_text(
            '{"accepted":true}',
            encoding="utf-8",
        )
        (attempt_dir / "designer_author_done.json").write_text(
            '{"status":"done"}',
            encoding="utf-8",
        )
        candidate = capture_attempt_candidate(
            run_dir=run_dir,
            attempt_dir=attempt_dir,
            artifact_type="landing",
            attempt=1,
            max_attempts=4,
            source_path="index.html",
            dependency_paths=["designer_author_done.json"],
            preview_paths=["preview.png"],
            validation_summary_path="validation.json",
            safety_state="ready",
            hard_blockers=[],
            warnings=[],
        )
        return run_dir, candidate

    def test_attempt_listing_exposes_urls_without_server_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runs_dir = Path(raw)
            self._landing_candidate(runs_dir)
            with patch.object(web_server, "RUNS_DIR", runs_dir):
                payload = asyncio.run(
                    web_server.run_attempts("run-source", _request())
                )

        candidate = payload["candidates"][0]
        self.assertTrue(candidate["source_url"].startswith("/api/files/runs/"))
        self.assertTrue(candidate["preview_urls"][0].startswith("/api/files/runs/"))
        self.assertNotIn("source_relative_path", candidate)
        self.assertNotIn("dependency_relative_paths", candidate)
        self.assertNotIn("validation_summary_relative_path", candidate)

    def test_background_selection_recovery_rolls_back_when_run_is_cancelled(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runs_dir = Path(raw) / "runs"
            run_dir, candidate = self._landing_candidate(runs_dir)
            store = RunControlStore(runs_dir)
            record = store.reserve(run_dir.name, "landing")
            record = store.transition(run_dir.name, record, "queued")
            store.transition(run_dir.name, record, "running")
            request_attempt_selection(
                run_dir=run_dir,
                run_id=run_dir.name,
                attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
                idempotency_key="cancel-recovery",
            )
            publish_installed = threading.Event()
            release_publish = threading.Event()

            def promote_with_barrier(ctx, selected) -> None:
                staging = run_dir / ".landing-final-staging-test"
                staging.mkdir()
                source = run_dir / selected.source_relative_path
                (staging / "index.html").write_text(
                    source.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )

                def post_publish() -> None:
                    publish_installed.set()
                    if not release_publish.wait(timeout=3):
                        raise RuntimeError("test publish barrier timed out")
                    ctx.raise_if_cancelled(
                        "attempt_selection.test.after_publish_barrier"
                    )

                publish_artifact_directory(
                    staging,
                    run_dir / "final",
                    artifact_name="landing",
                    post_publish=post_publish,
                )

            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch(
                    "autodesign.attempt_selection._default_promoter",
                    side_effect=promote_with_barrier,
                ),
                ThreadPoolExecutor(max_workers=1) as executor,
            ):
                future = executor.submit(
                    web_server._complete_attempt_selection_sync,
                    run_dir.name,
                    SimpleNamespace(),
                )
                self.assertTrue(publish_installed.wait(timeout=3))
                store.request_cancel(run_dir.name)
                release_publish.set()
                with self.assertRaises(RunCancelled):
                    future.result(timeout=3)

            self.assertFalse((run_dir / "final").exists())
            cancelled = store.finalize_cancel(
                run_dir.name,
                {"termination_verified": True, "reason": "test"},
            )
            self.assertEqual(cancelled.state, "cancelled")
            self.assertFalse(cancelled.publishable)

    def test_landing_fork_uses_the_same_materialized_contract_as_final(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runs_dir = Path(raw)
            self._landing_candidate(runs_dir)

            def fake_screenshot(_html_path, output_path, **_kwargs):
                output_path.write_bytes(b"preview")
                return SimpleNamespace(
                    backend="test",
                    warnings=[],
                    scale=1.0,
                    width_px=1440,
                    height_px=900,
                )

            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "new_run_id", return_value="draft-run"),
                patch(
                    "autodesign.agents.external_landing_author.screenshot_html",
                    side_effect=fake_screenshot,
                ),
            ):
                forked = self._materialize_fork(runs_dir)

            final_dir = runs_dir / "draft-run" / "final"
            html = (final_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn("data-autodesign-artifact-root", html)
            self.assertTrue((final_dir / "landing_author_manifest.json").is_file())
            self.assertTrue((final_dir / "preview.png").is_file())
            self.assertEqual(forked.native_file_url, "/api/files/runs/draft-run/final/index.html")

    def test_poster_fork_materializes_math_and_direct_final_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runs_dir = Path(raw)
            source_run = runs_dir / "run-source"
            attempt_dir = source_run / "designer_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "poster.html").write_text(
                "<!doctype html><html><head></head><body>"
                "<main class='paper-poster'>"
                "<h1 data-layer-id='title' data-kind='text'>Draft</h1>"
                "<p>Objective: \\\\(x^2 + y^2\\\\)</p>"
                "</main></body></html>",
                encoding="utf-8",
            )
            (attempt_dir / "validation.json").write_text(
                '{"accepted":true}',
                encoding="utf-8",
            )
            capture_attempt_candidate(
                run_dir=source_run,
                attempt_dir=attempt_dir,
                artifact_type="poster",
                attempt=1,
                max_attempts=4,
                source_path="poster.html",
                dependency_paths=[],
                preview_paths=[],
                validation_summary_path="validation.json",
                safety_state="ready",
                hard_blockers=[],
                warnings=[],
            )

            def fake_render(*, preview_path, **_kwargs):
                preview_path.write_bytes(b"preview")
                return SimpleNamespace(
                    ok=True,
                    backend="test",
                    warnings=[],
                    scale=1.0,
                    width_px=3072,
                    height_px=1536,
                )

            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "new_run_id", return_value="draft-run"),
                patch(
                    "autodesign.agents.external_designer_author."
                    "_render_direct_preview",
                    side_effect=fake_render,
                ),
                patch(
                    "autodesign.agents.external_designer_author."
                    "_maybe_repair_collapsed_poster_header",
                    return_value=None,
                ),
            ):
                forked = self._materialize_fork(runs_dir)

            final_dir = runs_dir / "draft-run" / "final"
            html = (final_dir / "poster.html").read_text(encoding="utf-8")
            self.assertIn("data-autodesign-katex", html)
            self.assertTrue(
                (final_dir / "designer_author_direct_manifest.json").is_file()
            )
            self.assertTrue((final_dir / "preview.png").is_file())
            self.assertEqual(forked.native_file_url, "/api/files/runs/draft-run/final/poster.html")
            self.assertEqual((forked.canvas.w, forked.canvas.h), (3072, 1536))

    def test_poster_candidate_draft_preserves_canvas_plan_without_using_it_as_size(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "poster-draft"
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            source = final_dir / "poster.html"
            source.write_text(
                "<!doctype html><style>.paper-poster { width: 3072px; height: 1536px; }</style>"
                "<main class='paper-poster'>Draft</main>",
                encoding="utf-8",
            )
            canvas_plan = {
                "canvas": {"w_px": 1440, "h_px": 1200},
                "sections": [{"id": "title"}],
            }
            (run_dir / "canvas_plan.json").write_text(
                json.dumps(canvas_plan),
                encoding="utf-8",
            )

            artifact = web_server._candidate_draft_artifact_from_lineage(
                run_dir,
                "poster-draft",
                "poster",
                {"source_attempt": 1},
                source=source,
            )

        self.assertEqual((artifact.canvas.w, artifact.canvas.h), (3072, 1536))
        self.assertEqual(artifact.canvas_plan, canvas_plan)

    def test_candidate_draft_keeps_deck_and_video_canvas_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for artifact_type in ("deck", "video"):
                with self.subTest(artifact_type=artifact_type):
                    run_dir = root / artifact_type
                    final_dir = run_dir / "final"
                    final_dir.mkdir(parents=True)
                    source = final_dir / "deck.html"
                    source.write_text(
                        "<!doctype html><main>Draft</main>",
                        encoding="utf-8",
                    )
                    artifact = web_server._candidate_draft_artifact_from_lineage(
                        run_dir,
                        artifact_type,
                        artifact_type,
                        {"source_attempt": 1},
                        source=source,
                    )
                    self.assertEqual(
                        (artifact.canvas.w, artifact.canvas.h),
                        (1920, 1080),
                    )

    def test_deck_fork_uses_the_same_materialized_contract_as_final(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runs_dir = Path(raw)
            source_run = runs_dir / "run-source"
            attempt_dir = source_run / "slides_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "slides.html").write_text(
                "<!doctype html><html><body><main class='deck'>"
                "<section class='deck-slide'><h1>Draft deck</h1></section>"
                "</main></body></html>",
                encoding="utf-8",
            )
            sidecars = {
                "designer_author_done.json": {"status": "done"},
                "slides_visual_plan.json": {},
                "slides_asset_catalog.json": {},
                "slides_validation.json": {
                    "status": "ok",
                    "expected_slide_count": 1,
                    "issues": [],
                },
            }
            for name, payload in sidecars.items():
                (attempt_dir / name).write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )
            capture_attempt_candidate(
                run_dir=source_run,
                attempt_dir=attempt_dir,
                artifact_type="deck",
                attempt=1,
                max_attempts=4,
                source_path="slides.html",
                dependency_paths=[
                    "designer_author_done.json",
                    "slides_visual_plan.json",
                    "slides_asset_catalog.json",
                ],
                preview_paths=[],
                validation_summary_path="slides_validation.json",
                safety_state="ready",
                hard_blockers=[],
                warnings=[],
            )

            def fake_grid(_paths, output_path):
                output_path.write_bytes(b"preview")

            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "new_run_id", return_value="draft-run"),
                patch(
                    "autodesign.agents.external_slides_author."
                    "screenshot_deck_slides",
                    return_value=SimpleNamespace(
                        paths=["/tmp/slide-1.png"],
                        backend="test",
                        warnings=[],
                    ),
                ),
                patch(
                    "autodesign.agents.external_slides_author."
                    "build_deck_preview_grid",
                    side_effect=fake_grid,
                ),
            ):
                forked = self._materialize_fork(runs_dir)

            final_dir = runs_dir / "draft-run" / "final"
            html = (final_dir / "deck.html").read_text(encoding="utf-8")
            self.assertIn("data-autodesign-artifact-root", html)
            self.assertTrue((final_dir / "slides.html").is_file())
            self.assertTrue((final_dir / "slides_author_manifest.json").is_file())
            self.assertTrue((final_dir / "preview.png").is_file())
            self.assertEqual(forked.native_file_url, "/api/files/runs/draft-run/final/deck.html")

    def test_real_deck_edit_regenerates_outputs_without_mutating_parent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out_dir = Path(raw) / "out"
            source_run = out_dir / "runs" / "deck-draft"
            source_final = source_run / "final"
            assets_dir = source_final / "assets"
            assets_dir.mkdir(parents=True)
            (assets_dir / "old.png").write_bytes(b"old-image")
            (assets_dir / "new.png").write_bytes(b"new-image")
            source_html = (
                "<!doctype html><html><body>"
                "<main id='deck' data-slide-count='2'>"
                "<section class='deck-slide'>"
                "<h1 data-layer-id='title-1' data-kind='text'>Original title</h1>"
                "<p data-layer-id='body-1' data-kind='text'>First body</p>"
                "<img data-layer-id='image-1' data-kind='image' src='assets/old.png'>"
                "</section><section class='deck-slide'>"
                "<h2 data-layer-id='title-2' data-kind='text'>Second title</h2>"
                "<p data-layer-id='body-2' data-kind='text'>Second body</p>"
                "<img data-layer-id='image-2' data-kind='image' src='assets/old.png'>"
                "</section></main></body></html>"
            )
            source_path = source_final / "deck.html"
            source_path.write_text(source_html, encoding="utf-8")
            (source_final / "slides.html").write_text(source_html, encoding="utf-8")
            source_hash = sha256_file(source_path)
            (source_final / "slides_author_manifest.json").write_text(
                json.dumps({
                    "artifact_type": "deck",
                    "html_sha256": source_hash,
                    "quality_status": "ready_with_warnings",
                    "quality_diagnostics": ["source_only_quality"],
                }),
                encoding="utf-8",
            )
            source_bytes = source_path.read_bytes()
            edited_html = source_html.replace(
                "Original title", "Edited first slide"
            ).replace("assets/old.png", "assets/new.png", 1)

            def fake_slides(_html_path, slides_dir, **_kwargs):
                slides_dir.mkdir(parents=True, exist_ok=True)
                paths = []
                for index in range(1, 3):
                    slide_path = slides_dir / f"slide_{index:02d}.png"
                    slide_path.write_bytes(f"slide-{index}".encode("ascii"))
                    paths.append(str(slide_path))
                return SimpleNamespace(
                    paths=paths,
                    backend="test",
                    warnings=[],
                )

            def fake_grid(_slide_paths, output_path):
                output_path.write_bytes(b"grid")

            def fake_pdf(_html_path, output_path, **_kwargs):
                output_path.write_bytes(b"pdf")

            with (
                patch(
                    "autodesign.artifact_edit_job.screenshot_deck_slides",
                    side_effect=fake_slides,
                ),
                patch(
                    "autodesign.artifact_edit_job.build_deck_preview_grid",
                    side_effect=fake_grid,
                ),
                patch(
                    "autodesign.artifact_edit_job.export_deck_pdf",
                    side_effect=fake_pdf,
                ),
            ):
                result = self._run_artifact_edit(
                    out_dir=out_dir,
                    parent_run_id="deck-draft",
                    child_run_id="deck-edited",
                    artifact_type="deck",
                    source_name="deck.html",
                    edited_html=edited_html,
                    edits={
                        "title-1": {"text": "Edited first slide"},
                        "image-1": {"src": "assets/new.png"},
                    },
                    candidate_lineage={},
                )

            edited_final = out_dir / "runs" / "deck-edited" / "final"
            deck_path = edited_final / "deck.html"
            slides_path = edited_final / "slides.html"
            deck_doc = BeautifulSoup(
                deck_path.read_text(encoding="utf-8"), "html.parser"
            )
            manifest = json.loads(
                (edited_final / "slides_author_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            edit_manifest = json.loads(
                (edited_final / "authored_html_edit_manifest.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(result["artifact_type"], "deck")
            self.assertEqual(source_path.read_bytes(), source_bytes)
            self.assertEqual(deck_path.read_bytes(), slides_path.read_bytes())
            self.assertEqual(len(deck_doc.select(".deck-slide")), 2)
            self.assertEqual(
                deck_doc.select_one("main#deck").get("data-autodesign-artifact-root"),
                "deck",
            )
            self.assertEqual(deck_doc.find("h1").get_text(strip=True), "Edited first slide")
            self.assertEqual(deck_doc.select_one("img").get("src"), "assets/new.png")
            self.assertTrue((edited_final / "assets" / "old.png").is_file())
            self.assertTrue((edited_final / "assets" / "new.png").is_file())
            self.assertEqual(len(list((edited_final / "slides").glob("*.png"))), 2)
            self.assertTrue((edited_final / "preview.png").is_file())
            self.assertTrue((edited_final / "deck.pdf").is_file())
            self.assertEqual(manifest["html_sha256"], source_hash)
            self.assertEqual(edit_manifest["html_sha256"], sha256_file(deck_path))

            reopened = web_server._build_artifact_response(
                out_dir / "runs" / "deck-edited",
                "deck-edited",
                "deck",
                baseline_artifact_json=None,
            )
            self.assertIsNotNone(reopened)
            assert reopened is not None
            self.assertEqual(len(reopened.layers), 6)
            self.assertEqual(len({layer["layer_id"] for layer in reopened.layers}), 6)
            self.assertIsNone(reopened.quality_status)
            self.assertEqual(reopened.quality_diagnostics, [])

    def test_video_fork_materializes_an_editable_project_without_rendering_mp4(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runs_dir = Path(raw)
            source_run = runs_dir / "run-source"
            attempt_dir = source_run / "video_author" / "attempt_01"
            project_dir = attempt_dir / "project"
            project_dir.mkdir(parents=True)
            (project_dir / "index.html").write_text(
                "<!doctype html><html><body><main><h1>Draft video</h1></main>"
                "</body></html>",
                encoding="utf-8",
            )
            (attempt_dir / "video_author_manifest.json").write_text(
                '{"target_duration_s":10,"scenes":[]}',
                encoding="utf-8",
            )
            (attempt_dir / "validation.json").write_text(
                '{"errors":[]}',
                encoding="utf-8",
            )
            trusted_context = (
                b'{"kind":"video_trusted_source_context","version":1,'
                b'"source":"pre_author_actual_source_bytes",'
                b'"eligible_asset_ids":[],"eligible_asset_roles":{},'
                b'"eligible_asset_hashes":{},"required_asset_ids":[],'
                b'"minimum_required_visual_count":0}\n'
            )
            (source_run / "video_trusted_source_context.json").write_bytes(
                trusted_context
            )
            capture_attempt_candidate(
                run_dir=source_run,
                attempt_dir=attempt_dir,
                artifact_type="video",
                attempt=1,
                max_attempts=4,
                source_path="project/index.html",
                dependency_paths=["video_author_manifest.json"],
                preview_paths=[],
                validation_summary_path="validation.json",
                safety_state="ready",
                hard_blockers=[],
                warnings=[],
            )

            def fake_screenshot(_html_path, output_path, **_kwargs):
                output_path.write_bytes(b"preview")
                return SimpleNamespace(
                    backend="test",
                    warnings=[],
                    scale=1.0,
                    width_px=1920,
                    height_px=1080,
                )

            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "new_run_id", return_value="draft-run"),
                patch(
                    "autodesign.agents.external_video_author.screenshot_html",
                    side_effect=fake_screenshot,
                ),
            ):
                forked = self._materialize_fork(runs_dir)

            final_dir = runs_dir / "draft-run" / "final"
            editable = (final_dir / "deck.html").read_text(encoding="utf-8")
            project = (
                final_dir / "project" / "index.html"
            ).read_text(encoding="utf-8")
            self.assertIn("data-autodesign-artifact-root", editable)
            self.assertEqual(project, editable)
            self.assertTrue(
                (final_dir / "video_candidate_draft_manifest.json").is_file()
            )
            self.assertTrue((final_dir / "preview.png").is_file())
            self.assertEqual(
                (
                    runs_dir
                    / "draft-run"
                    / "video_trusted_source_context.json"
                ).read_bytes(),
                trusted_context,
            )
            self.assertEqual(forked.native_file_url, "/api/files/runs/draft-run/final/deck.html")

    def test_fork_records_artifact_type_and_keeps_source_snapshot_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runs_dir = Path(raw)
            source_run_dir, candidate = self._landing_candidate(runs_dir)
            artifact = web_server.Artifact(
                artifact_id="art_draft-run",
                name="Attempt draft",
                artifact_type="landing",
                canvas=web_server.Canvas(w=1440, h=1000),
                native_format="html",
            )
            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "new_run_id", return_value="draft-run"),
                patch.object(
                    web_server,
                    "_build_artifact_response",
                    return_value=artifact,
                ),
            ):
                forked = self._materialize_fork(runs_dir)

            lineage = json.loads(
                (runs_dir / "draft-run" / "candidate_draft_lineage.json")
                .read_text(encoding="utf-8")
            )
            self.assertTrue(forked.candidate_draft)
            self.assertEqual(lineage["artifact_type"], "landing")
            self.assertEqual(lineage["materialization_version"], 2)
            self.assertEqual(lineage["source_candidate_id"], candidate.candidate_id)
            (runs_dir / "draft-run" / "final" / "index.html").write_text(
                "<!doctype html><h1>Edited fork</h1>",
                encoding="utf-8",
            )
            original = (
                source_run_dir
                / candidate.source_relative_path
            ).read_text(encoding="utf-8")
            self.assertIn("Draft", original)

    def test_poster_fork_copies_the_authoritative_validation_context(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runs_dir = Path(raw)
            source_run = runs_dir / "run-source"
            attempt_dir = source_run / "designer_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "poster.html").write_text(
                "<!doctype html><main class='paper-poster'>Draft</main>",
                encoding="utf-8",
            )
            (attempt_dir / "validation.json").write_text(
                '{"accepted":true}',
                encoding="utf-8",
            )
            capture_attempt_candidate(
                run_dir=source_run,
                attempt_dir=attempt_dir,
                artifact_type="poster",
                attempt=1,
                max_attempts=4,
                source_path="poster.html",
                dependency_paths=[],
                preview_paths=[],
                validation_summary_path="validation.json",
                safety_state="ready",
                hard_blockers=[],
                warnings=[],
            )
            context_files = (
                "paper_visual_provenance.json",
                "paper_memory.json",
                "paper_memory_dossier.json",
                "paper_visual_storyboard.json",
                "poster_content_brief.json",
                "poster_plan_contract.json",
                "poster_contract_preflight.json",
                "canvas_plan.json",
            )
            for name in context_files:
                (source_run / name).write_text("{}", encoding="utf-8")
            layers_dir = source_run / "layers"
            layers_dir.mkdir()
            (layers_dir / "ingest_fig_01.png").write_bytes(b"source")
            artifact = web_server.Artifact(
                artifact_id="art_draft-run",
                name="Poster attempt draft",
                artifact_type="poster",
                canvas=web_server.Canvas(w=3072, h=1536),
                native_format="html",
            )

            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "new_run_id", return_value="draft-run"),
                patch.object(
                    web_server,
                    "_build_artifact_response",
                    return_value=artifact,
                ),
            ):
                self._materialize_fork(runs_dir)

            draft_run = runs_dir / "draft-run"
            for name in context_files:
                self.assertTrue((draft_run / name).is_file(), name)
            self.assertTrue(
                (draft_run / "layers" / "ingest_fig_01.png").is_file()
            )

    def test_publish_worker_marks_only_its_child_after_validation_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runs_dir = Path(raw)
            self._landing_candidate(runs_dir)
            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "new_run_id", return_value="draft-run"),
            ):
                self._materialize_fork(runs_dir)
            (runs_dir / "published-child").mkdir()
            with patch(
                "autodesign.candidate_publish.validate_candidate_draft",
                return_value=[],
            ):
                result = run_candidate_publish_job(
                    run_id="published-child",
                    parent_run_id="draft-run",
                    conversation_id="conv-1",
                    settings=web_server._runtime_only_settings(),
                    cancellation_token=CancellationToken.never("published-child"),
                    runs_dir=runs_dir,
                )

            parent_lineage = json.loads(
                (runs_dir / "draft-run" / "candidate_draft_lineage.json")
                .read_text(encoding="utf-8")
            )
            child_lineage = json.loads(
                (runs_dir / "published-child" / "candidate_draft_lineage.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(parent_lineage["status"], "draft")
            self.assertEqual(child_lineage["status"], "validated")
            self.assertEqual(child_lineage["artifact_type"], "landing")
            self.assertEqual(result["run_id"], "published-child")

    def test_attempt_fork_copies_landing_trusted_source_hash_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runs_dir = Path(raw)
            source_run, _candidate = self._landing_candidate(runs_dir)
            anchor = {
                "kind": "landing_trusted_source_hashes",
                "version": 1,
                "source": "pre_author_actual_source_bytes",
                "hashes": {},
            }
            (source_run / "landing_trusted_source_hashes.json").write_text(
                json.dumps(anchor),
                encoding="utf-8",
            )

            self._materialize_fork(runs_dir)

            copied = json.loads(
                (runs_dir / "draft-run" / "landing_trusted_source_hashes.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(copied, anchor)

    def test_candidate_fork_edit_publish_preserves_exact_source_anchor_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out_dir = Path(raw) / "out"
            runs_dir = out_dir / "runs"

            def fake_screenshot(_html_path, output_path, **_kwargs):
                output_path.write_bytes(b"preview")
                return SimpleNamespace(
                    backend="test",
                    warnings=[],
                    scale=1.0,
                    width_px=1440,
                    height_px=900,
                )

            def fake_slides(_html_path, slides_dir, **_kwargs):
                slides_dir.mkdir(parents=True, exist_ok=True)
                slide_path = slides_dir / "slide_01.png"
                slide_path.write_bytes(b"slide")
                return SimpleNamespace(
                    paths=[str(slide_path)],
                    backend="test",
                    warnings=[],
                )

            def fake_grid(_slide_paths, output_path):
                output_path.write_bytes(b"grid")

            def fake_pdf(_html_path, output_path, **_kwargs):
                output_path.write_bytes(b"pdf")

            def validate_publish(run_dir, artifact_type, *_args, **_kwargs):
                (run_dir / "candidate_delivery_assessment.json").write_text(
                    json.dumps({
                        "schema_version": 1,
                        "artifact_type": artifact_type,
                        "quality_status": "ready",
                        "quality_diagnostics": [],
                        "hard_blockers": [],
                    }),
                    encoding="utf-8",
                )
                return []

            with (
                patch(
                    "autodesign.artifact_edit_job.screenshot_html",
                    side_effect=fake_screenshot,
                ),
                patch(
                    "autodesign.artifact_edit_job.screenshot_deck_slides",
                    side_effect=fake_slides,
                ),
                patch(
                    "autodesign.artifact_edit_job.build_deck_preview_grid",
                    side_effect=fake_grid,
                ),
                patch(
                    "autodesign.artifact_edit_job.export_deck_pdf",
                    side_effect=fake_pdf,
                ),
                patch(
                    "autodesign.candidate_publish.validate_candidate_draft",
                    side_effect=validate_publish,
                ),
            ):
                for artifact_type, source_name, anchor_name, manifest_name in (
                    (
                        "deck",
                        "deck.html",
                        "slides_trusted_source_hashes.json",
                        "slides_author_manifest.json",
                    ),
                    (
                        "landing",
                        "index.html",
                        "landing_trusted_source_hashes.json",
                        "landing_author_manifest.json",
                    ),
                ):
                    with self.subTest(artifact_type=artifact_type):
                        parent_run_id = f"{artifact_type}-fork"
                        edited_run_id = f"{artifact_type}-edited"
                        published_run_id = f"{artifact_type}-published"
                        parent_dir = runs_dir / parent_run_id
                        parent_final = parent_dir / "final"
                        parent_final.mkdir(parents=True)
                        html = (
                            "<!doctype html><html><body><main>"
                            "<section class='deck-slide'>"
                            "<h1 data-layer-id='title' data-kind='text'>Draft</h1>"
                            "</section></main></body></html>"
                        )
                        source_path = parent_final / source_name
                        source_path.write_text(html, encoding="utf-8")
                        if artifact_type == "deck":
                            (parent_final / "slides.html").write_text(
                                html,
                                encoding="utf-8",
                            )
                        (parent_final / manifest_name).write_text(
                            json.dumps({
                                "artifact_type": artifact_type,
                                "html_sha256": sha256_file(source_path),
                            }),
                            encoding="utf-8",
                        )
                        anchor_bytes = (
                            b'{"kind":"trusted_source_hashes","version":1,'
                            b'"hashes":{"source-1":"' + b"a" * 64 + b'"}}\n'
                        )
                        (parent_dir / anchor_name).write_bytes(anchor_bytes)
                        lineage = {
                            "schema_version": 1,
                            "status": "draft",
                            "artifact_type": artifact_type,
                            "source_run_id": "source-run",
                            "source_attempt": 1,
                            "source_candidate_id": f"{artifact_type}-attempt-01",
                            "source_candidate_sha256": "b" * 64,
                            "conversation_id": "conv-1",
                        }
                        (parent_dir / "candidate_draft_lineage.json").write_text(
                            json.dumps(lineage),
                            encoding="utf-8",
                        )

                        self._run_artifact_edit(
                            out_dir=out_dir,
                            parent_run_id=parent_run_id,
                            child_run_id=edited_run_id,
                            artifact_type=artifact_type,
                            source_name=source_name,
                            edited_html=html.replace("Draft", "Edited"),
                            edits={"title": {"text": "Edited"}},
                            candidate_lineage=lineage,
                        )
                        self.assertEqual(
                            (runs_dir / edited_run_id / anchor_name).read_bytes(),
                            anchor_bytes,
                        )

                        (runs_dir / published_run_id).mkdir()
                        run_candidate_publish_job(
                            run_id=published_run_id,
                            parent_run_id=edited_run_id,
                            conversation_id="conv-1",
                            settings=replace(
                                web_server._runtime_only_settings(),
                                out_dir=out_dir,
                            ),
                            cancellation_token=CancellationToken.never(
                                published_run_id
                            ),
                            runs_dir=runs_dir,
                        )
                        self.assertEqual(
                            (runs_dir / published_run_id / anchor_name).read_bytes(),
                            anchor_bytes,
                        )

    def test_publication_writes_fresh_quality_metadata_to_final_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runs_dir = Path(raw)
            self._landing_candidate(runs_dir)
            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "new_run_id", return_value="draft-run"),
            ):
                self._materialize_fork(runs_dir)
            child_dir = runs_dir / "published-child"
            child_dir.mkdir()

            def quality_validation(run_dir, *_args, **_kwargs):
                (run_dir / "candidate_delivery_assessment.json").write_text(
                    json.dumps({
                        "schema_version": 1,
                        "artifact_type": "landing",
                        "quality_status": "ready_with_warnings",
                        "quality_diagnostics": [
                            "landing_motion_without_reduced_motion"
                        ],
                        "hard_blockers": [],
                    }),
                    encoding="utf-8",
                )
                return []

            with patch(
                "autodesign.candidate_publish.validate_candidate_draft",
                side_effect=quality_validation,
            ):
                run_candidate_publish_job(
                    run_id="published-child",
                    parent_run_id="draft-run",
                    conversation_id="conv-1",
                    settings=web_server._runtime_only_settings(),
                    cancellation_token=CancellationToken.never("published-child"),
                    runs_dir=runs_dir,
                )

            manifest = json.loads(
                (child_dir / "final" / "landing_author_manifest.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["quality_status"], "ready_with_warnings")
            self.assertEqual(
                manifest["quality_diagnostics"],
                ["landing_motion_without_reduced_motion"],
            )
            self.assertEqual(
                manifest["html_sha256"],
                sha256_file(child_dir / "final" / "index.html"),
            )
            self.assertEqual(
                manifest["html"],
                str((child_dir / "final" / "index.html").resolve()),
            )
            self.assertEqual(
                manifest["preview"],
                str((child_dir / "final" / "preview.png").resolve()),
            )

    def test_failed_candidate_publication_never_exposes_final_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runs_dir = Path(raw)
            self._landing_candidate(runs_dir)
            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "new_run_id", return_value="draft-run"),
            ):
                self._materialize_fork(runs_dir)
            (runs_dir / "published-child").mkdir()

            with (
                patch(
                    "autodesign.candidate_publish.validate_candidate_draft",
                    return_value=[{
                        "issue_id": "landing_remote_reference",
                        "message": "unsafe dependency",
                    }],
                ),
                self.assertRaisesRegex(ValueError, "validation failed"),
            ):
                run_candidate_publish_job(
                    run_id="published-child",
                    parent_run_id="draft-run",
                    conversation_id="conv-1",
                    settings=web_server._runtime_only_settings(),
                    cancellation_token=CancellationToken.never("published-child"),
                    runs_dir=runs_dir,
                )

            self.assertFalse((runs_dir / "published-child" / "final").exists())

    def test_source_artifact_resolves_to_its_published_candidate_fork(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out_dir = Path(raw) / "out"
            runs_dir = out_dir / "runs"
            runs_dir.mkdir(parents=True)
            source_run, candidate = self._landing_candidate(runs_dir)
            derived_run = runs_dir / "derived-run"
            final_dir = derived_run / "final"
            final_dir.mkdir(parents=True)
            (final_dir / "index.html").write_text(
                "<!doctype html><html><body><main>"
                "<h1 data-layer-id='title' data-kind='text'>Published fork</h1>"
                "</main></body></html>",
                encoding="utf-8",
            )
            (derived_run / "candidate_draft_lineage.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "status": "published",
                    "artifact_type": "landing",
                    "source_run_id": "run-source",
                    "source_attempt": 1,
                    "source_candidate_id": candidate.candidate_id,
                    "source_candidate_sha256": candidate.source_sha256,
                }),
                encoding="utf-8",
            )
            complete_source_run_with_candidate_fork(
                run_dir=source_run,
                run_id="run-source",
                attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
                artifact_id="art_derived-run",
            )

            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "_BOOT_OUT_DIR", out_dir),
            ):
                response = asyncio.run(
                    web_server.run_artifact("run-source", _request())
                )

            self.assertIsNotNone(response.artifact)
            assert response.artifact is not None
            self.assertEqual(response.artifact.artifact_id, "art_derived-run")
            self.assertEqual(response.message.status, "done")
            self.assertEqual(response.message.run_id, "run-source")

    def test_source_artifact_rejects_cancelled_published_candidate_fork(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out_dir = Path(raw) / "out"
            runs_dir = out_dir / "runs"
            runs_dir.mkdir(parents=True)
            source_run, candidate = self._landing_candidate(runs_dir)
            derived_run = runs_dir / "derived-run"
            (derived_run / "final").mkdir(parents=True)
            (derived_run / "final" / "index.html").write_text(
                "<html><body>cancelled derived</body></html>",
                encoding="utf-8",
            )
            (derived_run / "candidate_draft_lineage.json").write_text(
                json.dumps({
                    "status": "published",
                    "artifact_type": "landing",
                    "source_run_id": "run-source",
                }),
                encoding="utf-8",
            )
            complete_source_run_with_candidate_fork(
                run_dir=source_run,
                run_id="run-source",
                attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
                artifact_id="art_derived-run",
            )
            store = RunControlStore(runs_dir)
            source = store.reserve("run-source", "landing")
            source = store.transition("run-source", source, "queued")
            source = store.transition("run-source", source, "running")
            source = store.transition("run-source", source, "completing")
            store.transition("run-source", source, "completed", publishable=True)
            store.reserve("derived-run", "landing")
            store.request_cancel("derived-run")
            store.finalize_cancel(
                "derived-run",
                {"termination_verified": True, "reason": "test"},
            )

            original_read_json = web_server._read_json_file
            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "_BOOT_OUT_DIR", out_dir),
                patch.object(
                    web_server,
                    "_read_json_file",
                    side_effect=original_read_json,
                ) as read_lineage,
            ):
                with self.assertRaises(web_server.HTTPException) as raised:
                    asyncio.run(
                        web_server.run_artifact("run-source", _request())
                    )

            self.assertEqual(raised.exception.status_code, 410)
            read_lineage.assert_not_called()

    def test_source_artifact_authenticates_published_candidate_fork_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out_dir = Path(raw) / "out"
            runs_dir = out_dir / "runs"
            runs_dir.mkdir(parents=True)
            source_run, candidate = self._landing_candidate(runs_dir)
            derived_run = runs_dir / "derived-run"
            (derived_run / "final").mkdir(parents=True)
            (derived_run / "final" / "index.html").write_text(
                "<html><body>foreign derived</body></html>",
                encoding="utf-8",
            )
            (derived_run / "candidate_draft_lineage.json").write_text(
                json.dumps({
                    "status": "published",
                    "artifact_type": "landing",
                    "source_run_id": "run-source",
                }),
                encoding="utf-8",
            )
            complete_source_run_with_candidate_fork(
                run_dir=source_run,
                run_id="run-source",
                attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
                artifact_id="art_derived-run",
            )
            store = RunControlStore(runs_dir)
            for run_id in ("run-source", "derived-run"):
                record = store.reserve(run_id, "landing")
                record = store.transition(run_id, record, "queued")
                record = store.transition(run_id, record, "running")
                record = store.transition(run_id, record, "completing")
                store.transition(run_id, record, "completed", publishable=True)

            def owns_run(run_id: str, user_id: str) -> bool:
                self.assertEqual(user_id, "owner-a")
                return run_id == "run-source"

            original_read_json = web_server._read_json_file
            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "_BOOT_OUT_DIR", out_dir),
                patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
                patch.object(web_server, "_demo_user_id", return_value="owner-a"),
                patch.object(web_server, "_demo_user_owns_run", side_effect=owns_run),
                patch.object(
                    web_server,
                    "_read_json_file",
                    side_effect=original_read_json,
                ) as read_lineage,
            ):
                with self.assertRaises(web_server.HTTPException) as raised:
                    asyncio.run(
                        web_server.run_artifact("run-source", _request())
                    )

            self.assertEqual(raised.exception.status_code, 404)
            read_lineage.assert_not_called()

    def test_malformed_selection_journal_does_not_hide_an_existing_final(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out_dir = Path(raw) / "out"
            runs_dir = out_dir / "runs"
            run_dir = runs_dir / "run-final"
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            (final_dir / "index.html").write_text(
                "<!doctype html><html><body><main>Existing final</main>"
                "</body></html>",
                encoding="utf-8",
            )
            selection_dir = run_dir / "attempt_candidates"
            selection_dir.mkdir()
            (selection_dir / "selection.json").write_text(
                "{}",
                encoding="utf-8",
            )

            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "_BOOT_OUT_DIR", out_dir),
            ):
                response = asyncio.run(
                    web_server.run_artifact("run-final", _request())
                )

            self.assertIsNotNone(response.artifact)
            assert response.artifact is not None
            self.assertEqual(response.artifact.artifact_id, "art_run-final")

    def test_video_publish_syncs_canvas_html_before_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "video-draft"
            final_dir = run_dir / "final"
            project_dir = final_dir / "project"
            project_dir.mkdir(parents=True)
            edited = "<!doctype html><html><body>Edited video</body></html>"
            (final_dir / "deck.html").write_text(edited, encoding="utf-8")
            (project_dir / "index.html").write_text("old", encoding="utf-8")
            (final_dir / "video_author_manifest.json").write_text(
                json.dumps({"target_duration_s": 10, "scenes": []}),
                encoding="utf-8",
            )
            def fake_finalize(_name, _args, ctx):
                (ctx.run_dir / "final" / "video_delivery.json").write_text(
                    json.dumps({"status": "passed"}),
                    encoding="utf-8",
                )
                return ToolResultRecord(status="ok", payload={})

            with (
                patch(
                    "autodesign.agents.external_video_author."
                    "deliver_authored_video_project",
                    return_value=ToolResultRecord(status="ok", payload={}),
                ) as deliver,
                patch(
                    "autodesign.candidate_publish.invoke_designer_tool",
                    side_effect=fake_finalize,
                ),
            ):
                blockers = web_server._deliver_video_candidate_draft(
                    run_dir,
                    SimpleNamespace(),
                )

            self.assertEqual(blockers, [])
            self.assertEqual(
                (project_dir / "index.html").read_text(encoding="utf-8"),
                edited,
            )
            deliver.assert_called_once()

    def test_video_publish_finalizes_with_the_delivery_tool_context(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "video-draft"
            final_dir = run_dir / "final"
            project_dir = final_dir / "project"
            project_dir.mkdir(parents=True)
            (final_dir / "deck.html").write_text(
                "<!doctype html><html><body>Edited video</body></html>",
                encoding="utf-8",
            )
            (project_dir / "index.html").write_text("old", encoding="utf-8")
            (final_dir / "video_author_manifest.json").write_text(
                json.dumps({"target_duration_s": 10, "scenes": []}),
                encoding="utf-8",
            )
            delivered_contexts: list[ToolContext] = []

            def fake_delivery(*, project_dir, manifest, ctx):
                delivered_contexts.append(ctx)
                ctx.state["video_delivery"] = {"status": "passed"}
                return ToolResultRecord(status="ok", payload={})

            def fake_finalize(name, args, ctx):
                self.assertEqual(name, "finalize")
                self.assertIs(ctx, delivered_contexts[0])
                (ctx.run_dir / "final" / "video_delivery.json").write_text(
                    json.dumps({"status": "passed"}),
                    encoding="utf-8",
                )
                return ToolResultRecord(status="ok", payload={})

            with (
                patch(
                    "autodesign.agents.external_video_author."
                    "deliver_authored_video_project",
                    side_effect=fake_delivery,
                ),
                patch(
                    "autodesign.candidate_publish.invoke_designer_tool",
                    side_effect=fake_finalize,
                    create=True,
                ) as finalize,
            ):
                blockers = web_server._deliver_video_candidate_draft(
                    run_dir,
                    SimpleNamespace(),
                )

            self.assertEqual(blockers, [])
            finalize.assert_called_once()
            self.assertTrue((final_dir / "video_delivery.json").is_file())

    def test_video_candidate_publication_retains_delivery_graph_after_staging_cleanup(
        self,
    ) -> None:
        from tests.test_video_web_delivery import _passed_delivery

        with tempfile.TemporaryDirectory() as raw:
            runs_dir = Path(raw) / "runs"
            draft_dir = runs_dir / "video-draft"
            draft_final = draft_dir / "final"
            (draft_final / "project").mkdir(parents=True)
            deck_html = "<!doctype html><html><body><main>Video draft</main></body></html>"
            (draft_final / "deck.html").write_text(deck_html, encoding="utf-8")
            (draft_final / "project" / "index.html").write_text(
                deck_html,
                encoding="utf-8",
            )
            (draft_dir / "candidate_draft_lineage.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "status": "draft",
                    "artifact_type": "video",
                    "source_run_id": "source-run",
                    "source_attempt": 1,
                    "source_candidate_id": "video-attempt-01",
                    "source_candidate_sha256": "a" * 64,
                    "conversation_id": "conv-1",
                }),
                encoding="utf-8",
            )
            published_dir = runs_dir / "video-published"
            record = RunControlStore(runs_dir).reserve(
                published_dir.name,
                "video",
            )

            def fake_delivery(run_dir, _settings, _cancellation_token):
                shutil.rmtree(run_dir / "final")
                _passed_delivery(run_dir)
                (run_dir / "final" / "deck.html").write_text(
                    deck_html,
                    encoding="utf-8",
                )
                return []

            with (
                patch(
                    "autodesign.candidate_publish.validate_candidate_draft",
                    return_value=[],
                ),
                patch(
                    "autodesign.candidate_publish.deliver_video_candidate_draft",
                    side_effect=fake_delivery,
                ),
            ):
                result = run_candidate_publish_job(
                    run_id="video-published",
                    parent_run_id="video-draft",
                    conversation_id="conv-1",
                    settings=web_server._runtime_only_settings(),
                    cancellation_token=CancellationToken.never("video-published"),
                    runs_dir=runs_dir,
                )

            self.assertEqual(result["artifact_type"], "video")
            self.assertFalse(any(
                path.name.startswith(".candidate-publish-staging-")
                for path in published_dir.iterdir()
            ))
            validation = web_server._validated_video_delivery(published_dir)
            self.assertTrue(validation.is_passed, validation.reason_code)
            artifact = web_server._build_video_artifact(
                published_dir,
                "video-published",
                baseline_artifact_json=None,
            )
            self.assertIsNotNone(artifact)
            with patch.object(web_server, "RUNS_DIR", runs_dir):
                web_server._reconcile_run_terminal_artifact(TerminalReconciliation(
                    run_id=published_dir.name,
                    decision="accept",
                    phase="commit",
                    terminal_state="completed",
                    record=record,
                ))
            self.assertTrue((published_dir / "final").is_dir())
            self.assertTrue((published_dir / "hyperframes-paper-video").is_dir())
            self.assertTrue((published_dir / "design_spec.json").is_file())
            self.assertFalse(any("promotion" in path.name for path in published_dir.iterdir()))
            accepted = web_server._validated_video_delivery(published_dir)
            self.assertTrue(accepted.is_passed, accepted.reason_code)

    def test_video_delivery_context_failure_preserves_preexisting_run_state(self) -> None:
        from tests.test_video_web_delivery import _passed_delivery

        with tempfile.TemporaryDirectory() as raw:
            work_dir = Path(raw) / "work"
            run_dir = Path(raw) / "published"
            work_dir.mkdir()
            run_dir.mkdir()
            _passed_delivery(work_dir)
            existing_spec = run_dir / "design_spec.json"
            existing_spec.write_bytes(b"preexisting run state")

            with self.assertRaisesRegex(ValueError, "destination already exists"):
                _publish_video_delivery_context(work_dir, run_dir)

            self.assertEqual(existing_spec.read_bytes(), b"preexisting run state")
            self.assertFalse((run_dir / "hyperframes-paper-video").exists())

    def test_video_delivery_context_accept_without_journal_revalidates_current_state(
        self,
    ) -> None:
        from tests.test_video_web_delivery import _passed_delivery

        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "video-published"
            _passed_delivery(run_dir)

            validate_video_delivery_context_promotion(run_dir)
            (run_dir / "design_spec.json").unlink()

            with self.assertRaisesRegex(ValueError, "current-context validation"):
                validate_video_delivery_context_promotion(run_dir)

    def test_video_delivery_context_reject_refuses_a_symlink_journal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "video-published"
            run_dir.mkdir()
            graph = run_dir / "hyperframes-preexisting"
            graph.mkdir()
            spec = run_dir / "design_spec.json"
            spec.write_text("preexisting", encoding="utf-8")
            external_journal = root / "external.json"
            external_journal.write_text(json.dumps({
                "version": 1,
                "transaction_owner": "autodesign.video_candidate_delivery.v1",
                "phase": "installed",
                "run_name": run_dir.name,
                "entries": [
                    {"name": graph.name, "kind": "directory"},
                    {"name": spec.name, "kind": "file"},
                ],
            }), encoding="utf-8")
            (run_dir / ".video-candidate-delivery-promotion.json").symlink_to(
                external_journal,
            )

            with self.assertRaisesRegex(ValueError, "journal path"):
                reconcile_video_delivery_context_promotion(run_dir, accept=False)

            self.assertTrue(graph.is_dir())
            self.assertEqual(spec.read_text(encoding="utf-8"), "preexisting")

    def test_video_delivery_context_reject_retains_journal_when_cleanup_fails(
        self,
    ) -> None:
        from tests.test_video_web_delivery import _passed_delivery

        with tempfile.TemporaryDirectory() as raw:
            work_dir = Path(raw) / "work"
            run_dir = Path(raw) / "video-published"
            _passed_delivery(work_dir)
            _publish_video_delivery_context(work_dir, run_dir)
            journal = run_dir / ".video-candidate-delivery-promotion.json"

            with (
                patch(
                    "autodesign.candidate_publish.shutil.rmtree",
                    return_value=None,
                ),
                self.assertRaisesRegex(RuntimeError, "could not remove"),
            ):
                reconcile_video_delivery_context_promotion(run_dir, accept=False)

            self.assertTrue(journal.is_file())
            self.assertTrue((run_dir / "hyperframes-paper-video").is_dir())

            reconcile_video_delivery_context_promotion(run_dir, accept=False)
            self.assertFalse(journal.exists())
            self.assertFalse((run_dir / "hyperframes-paper-video").exists())
            self.assertFalse((run_dir / "design_spec.json").exists())

    def test_video_delivery_context_recovery_is_idempotent_across_journal_phases(
        self,
    ) -> None:
        from tests.test_video_web_delivery import _passed_delivery

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            accept_work = root / "accept-work"
            accept_run = root / "accept-run"
            _passed_delivery(accept_work)
            _publish_video_delivery_context(accept_work, accept_run)
            shutil.copytree(accept_work / "final", accept_run / "final")

            reconcile_video_delivery_context_promotion(accept_run, accept=True)
            reconcile_video_delivery_context_promotion(accept_run, accept=True)
            self.assertTrue((accept_run / "hyperframes-paper-video").is_dir())
            self.assertTrue((accept_run / "design_spec.json").is_file())

            reject_work = root / "reject-work"
            reject_run = root / "reject-run"
            _passed_delivery(reject_work)
            _publish_video_delivery_context(reject_work, reject_run)
            journal = reject_run / ".video-candidate-delivery-promotion.json"

            reconcile_video_delivery_context_promotion(reject_run, accept=False)
            reconcile_video_delivery_context_promotion(reject_run, accept=False)
            self.assertFalse((reject_run / "hyperframes-paper-video").exists())
            self.assertFalse((reject_run / "design_spec.json").exists())
            self.assertFalse(journal.exists())

    def test_video_delivery_context_partial_install_rolls_back_and_can_retry(
        self,
    ) -> None:
        from tests.test_video_web_delivery import _passed_delivery

        with tempfile.TemporaryDirectory() as raw:
            work_dir = Path(raw) / "work"
            run_dir = Path(raw) / "video-published"
            _passed_delivery(work_dir)

            with (
                patch(
                    "autodesign.candidate_publish._copy_file_atomically",
                    side_effect=RuntimeError("injected DesignSpec copy failure"),
                ),
                self.assertRaisesRegex(RuntimeError, "injected DesignSpec"),
            ):
                _publish_video_delivery_context(work_dir, run_dir)

            self.assertFalse((run_dir / "hyperframes-paper-video").exists())
            self.assertFalse((run_dir / "design_spec.json").exists())
            self.assertFalse(
                (run_dir / ".video-candidate-delivery-promotion.json").exists()
            )
            self.assertFalse(any("staging" in path.name for path in run_dir.iterdir()))

            _publish_video_delivery_context(work_dir, run_dir)
            self.assertTrue((run_dir / "hyperframes-paper-video").is_dir())
            self.assertTrue((run_dir / "design_spec.json").is_file())

    def test_video_delivery_context_prepared_recovery_removes_bound_staging(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "video-published"
            run_dir.mkdir()
            graph_staging = run_dir / ".hyperframes-paper-video-staging-crash"
            spec_staging = run_dir / ".design_spec.json-staging-crash"
            graph_staging.mkdir()
            (graph_staging / "partial.bin").write_bytes(b"partial")
            spec_staging.write_bytes(b"partial")
            journal = run_dir / ".video-candidate-delivery-promotion.json"
            journal.write_text(json.dumps({
                "version": 2,
                "transaction_owner": "autodesign.video_candidate_delivery.v1",
                "phase": "prepared",
                "run_name": run_dir.name,
                "entries": [
                    {
                        "name": "hyperframes-paper-video",
                        "kind": "directory",
                        "staging_name": graph_staging.name,
                    },
                    {
                        "name": "design_spec.json",
                        "kind": "file",
                        "staging_name": spec_staging.name,
                    },
                ],
            }), encoding="utf-8")

            reconcile_video_delivery_context_promotion(run_dir, accept=False)

            self.assertFalse(graph_staging.exists())
            self.assertFalse(spec_staging.exists())
            self.assertFalse(journal.exists())

    def test_video_delivery_context_rolled_back_replay_removes_only_journal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "video-published"
            run_dir.mkdir()
            journal = run_dir / ".video-candidate-delivery-promotion.json"
            journal.write_text(json.dumps({
                "version": 2,
                "transaction_owner": "autodesign.video_candidate_delivery.v1",
                "phase": "rolled_back",
                "run_name": run_dir.name,
                "entries": [
                    {
                        "name": "hyperframes-paper-video",
                        "kind": "directory",
                        "staging_name": ".hyperframes-paper-video-staging-replay",
                    },
                    {
                        "name": "design_spec.json",
                        "kind": "file",
                        "staging_name": ".design_spec.json-staging-replay",
                    },
                ],
            }), encoding="utf-8")

            reconcile_video_delivery_context_promotion(run_dir, accept=False)

            self.assertFalse(journal.exists())

    def test_video_delivery_context_reject_detects_journal_identity_swap(
        self,
    ) -> None:
        from autodesign import candidate_publish
        from tests.test_video_web_delivery import _passed_delivery

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            work_dir = root / "work"
            run_dir = root / "video-published"
            _passed_delivery(work_dir)
            _publish_video_delivery_context(work_dir, run_dir)
            journal = run_dir / ".video-candidate-delivery-promotion.json"
            original_journal = root / "original-journal.json"
            replacement = root / "replacement.json"
            replacement.write_text("{}", encoding="utf-8")
            trusted_read = candidate_publish._read_trusted_video_context_journal

            def swap_after_read(path: Path):
                payload, identity = trusted_read(path)
                path.rename(original_journal)
                path.symlink_to(replacement)
                return payload, identity

            with (
                patch(
                    "autodesign.candidate_publish._read_trusted_video_context_journal",
                    side_effect=swap_after_read,
                ),
                self.assertRaisesRegex(ValueError, "changed during recovery"),
            ):
                reconcile_video_delivery_context_promotion(run_dir, accept=False)

            self.assertTrue((run_dir / "hyperframes-paper-video").is_dir())
            self.assertTrue((run_dir / "design_spec.json").is_file())
            self.assertTrue(original_journal.is_file())
            self.assertTrue(journal.is_symlink())

    def test_video_delivery_context_hard_crash_recovers_bound_staging_and_retries(
        self,
    ) -> None:
        from tests.test_video_web_delivery import _passed_delivery

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            work_dir = root / "work"
            run_dir = root / "video-published"
            _passed_delivery(work_dir)
            child = textwrap.dedent("""
                import os
                from pathlib import Path
                import sys
                from autodesign import candidate_publish

                def crash_during_tree_copy(source, destination, *, staging, label):
                    staging.mkdir(parents=True)
                    (staging / "partial.bin").write_bytes(b"partial")
                    os._exit(77)

                candidate_publish._copy_tree_atomically = crash_during_tree_copy
                candidate_publish._publish_video_delivery_context(
                    Path(sys.argv[1]),
                    Path(sys.argv[2]),
                )
            """)

            completed = subprocess.run(
                [sys.executable, "-c", child, str(work_dir), str(run_dir)],
                cwd=Path(__file__).resolve().parents[1],
                check=False,
            )

            self.assertEqual(completed.returncode, 77)
            journal = run_dir / ".video-candidate-delivery-promotion.json"
            self.assertTrue(journal.is_file())
            self.assertTrue(any("staging" in path.name for path in run_dir.iterdir()))

            reconcile_video_delivery_context_promotion(run_dir, accept=False)
            self.assertFalse(journal.exists())
            self.assertFalse(any("staging" in path.name for path in run_dir.iterdir()))

            _publish_video_delivery_context(work_dir, run_dir)
            self.assertTrue((run_dir / "hyperframes-paper-video").is_dir())
            self.assertTrue((run_dir / "design_spec.json").is_file())

    def test_video_delivery_context_hard_crash_after_first_install_rolls_back_and_retries(
        self,
    ) -> None:
        from tests.test_video_web_delivery import _passed_delivery

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            work_dir = root / "work"
            run_dir = root / "video-published"
            _passed_delivery(work_dir)
            child = textwrap.dedent("""
                import os
                from pathlib import Path
                import sys
                from autodesign import candidate_publish

                copy_tree_atomically = candidate_publish._copy_tree_atomically

                def crash_after_tree_install(source, destination, *, staging, label):
                    copy_tree_atomically(
                        source,
                        destination,
                        staging=staging,
                        label=label,
                    )
                    os._exit(78)

                candidate_publish._copy_tree_atomically = crash_after_tree_install
                candidate_publish._publish_video_delivery_context(
                    Path(sys.argv[1]),
                    Path(sys.argv[2]),
                )
            """)

            completed = subprocess.run(
                [sys.executable, "-c", child, str(work_dir), str(run_dir)],
                cwd=Path(__file__).resolve().parents[1],
                check=False,
            )

            self.assertEqual(completed.returncode, 78)
            journal = run_dir / ".video-candidate-delivery-promotion.json"
            self.assertTrue(journal.is_file())
            self.assertTrue((run_dir / "hyperframes-paper-video").is_dir())
            self.assertFalse((run_dir / "design_spec.json").exists())

            reconcile_video_delivery_context_promotion(run_dir, accept=False)
            self.assertFalse(journal.exists())
            self.assertFalse((run_dir / "hyperframes-paper-video").exists())
            self.assertFalse((run_dir / "design_spec.json").exists())

            _publish_video_delivery_context(work_dir, run_dir)
            self.assertTrue((run_dir / "hyperframes-paper-video").is_dir())
            self.assertTrue((run_dir / "design_spec.json").is_file())

    def test_cancelled_video_publication_terminal_reject_removes_delivery_context(
        self,
    ) -> None:
        from tests.test_video_web_delivery import _passed_delivery

        class CancelAfterLineage:
            def raise_if_cancelled(self, phase: str) -> None:
                if phase == "candidate_publish.after_lineage":
                    raise RunCancelled("video-published", phase)

        with tempfile.TemporaryDirectory() as raw:
            runs_dir = Path(raw) / "runs"
            parent_dir = runs_dir / "video-draft"
            parent_dir.mkdir(parents=True)
            _passed_delivery(parent_dir)
            (parent_dir / "final" / "deck.html").write_text(
                "<!doctype html><html><body>Video draft</body></html>",
                encoding="utf-8",
            )
            (parent_dir / "candidate_draft_lineage.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "status": "draft",
                    "artifact_type": "video",
                    "source_run_id": "source-run",
                    "source_attempt": 1,
                    "source_candidate_id": "video-attempt-01",
                    "source_candidate_sha256": "a" * 64,
                    "conversation_id": "conv-1",
                }),
                encoding="utf-8",
            )
            run_dir = runs_dir / "video-published"
            store = RunControlStore(runs_dir)
            record = store.reserve(run_dir.name, "video")

            def fake_delivery(work_dir, _settings, _cancellation_token):
                shutil.rmtree(work_dir / "final")
                _passed_delivery(work_dir)
                (work_dir / "final" / "deck.html").write_text(
                    "<!doctype html><html><body>Video published</body></html>",
                    encoding="utf-8",
                )
                return []

            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch(
                    "autodesign.candidate_publish.validate_candidate_draft",
                    return_value=[],
                ),
                patch(
                    "autodesign.candidate_publish.deliver_video_candidate_draft",
                    side_effect=fake_delivery,
                ),
                self.assertRaises(RunCancelled),
            ):
                run_candidate_publish_job(
                    run_id=run_dir.name,
                    parent_run_id=parent_dir.name,
                    conversation_id="conv-1",
                    settings=web_server._runtime_only_settings(),
                    cancellation_token=CancelAfterLineage(),
                    runs_dir=runs_dir,
                )

            self.assertTrue((run_dir / "final").is_dir())
            self.assertTrue((run_dir / "hyperframes-paper-video").is_dir())
            with patch.object(web_server, "RUNS_DIR", runs_dir):
                web_server._reconcile_run_terminal_artifact(TerminalReconciliation(
                    run_id=run_dir.name,
                    decision="reject",
                    phase="commit",
                    terminal_state="cancelled",
                    record=record,
                ))
            self.assertFalse((run_dir / "final").exists())
            self.assertFalse((run_dir / "hyperframes-paper-video").exists())
            self.assertFalse((run_dir / "design_spec.json").exists())
            self.assertFalse((run_dir / "specs").exists())
            self.assertFalse(any("promotion" in path.name for path in run_dir.iterdir()))

    def test_video_publication_revalidates_source_ids_paths_hashes_and_coverage(
        self,
    ) -> None:
        from tests.test_external_video_author import (
            _scene_manifest,
            _write_validation_project,
        )

        with tempfile.TemporaryDirectory() as raw:
            runs_dir = Path(raw) / "runs"
            run_dir = runs_dir / "video-draft"
            final_dir = run_dir / "final"
            scenes = _scene_manifest()
            project_dir, source_paths = _write_validation_project(final_dir, scenes)
            html_path = project_dir / "index.html"
            html = html_path.read_text(encoding="utf-8")
            html = html.replace(" data-source-id=\"", " data-removed-source-id=\"")
            html_path.write_text(html, encoding="utf-8")
            (final_dir / "deck.html").write_text(html, encoding="utf-8")
            (final_dir / "video_author_manifest.json").write_text(
                json.dumps({
                    "version": 1,
                    "language": "en",
                    "target_duration_s": 360,
                    "project_path": "project",
                    "scenes": scenes,
                }),
                encoding="utf-8",
            )
            asset_ids = sorted(source_paths)
            roles = {
                asset_id: (
                    "method" if index < 4
                    else "results" if index < 8
                    else "qualitative"
                )
                for index, asset_id in enumerate(asset_ids)
            }
            (run_dir / "video_trusted_source_context.json").write_text(
                json.dumps({
                    "kind": "video_trusted_source_context",
                    "version": 1,
                    "source": "pre_author_actual_source_bytes",
                    "eligible_asset_ids": asset_ids,
                    "eligible_asset_roles": roles,
                    "eligible_asset_hashes": {
                        asset_id: sha256_file(source_paths[asset_id])
                        for asset_id in asset_ids
                    },
                    "required_asset_ids": asset_ids,
                    "minimum_required_visual_count": 8,
                }),
                encoding="utf-8",
            )
            (run_dir / "candidate_draft_lineage.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "status": "draft",
                    "artifact_type": "video",
                    "source_run_id": "source-run",
                    "source_attempt": 1,
                    "source_candidate_id": "video-attempt-01",
                    "source_candidate_sha256": "a" * 64,
                    "conversation_id": "conv-1",
                }),
                encoding="utf-8",
            )

            with patch(
                "autodesign.candidate_publish.screenshot_html"
            ) as screenshot:
                blockers = web_server._validate_candidate_draft(
                    run_dir,
                    "video",
                    SimpleNamespace(),
                )

            self.assertEqual(
                [item["issue_id"] for item in blockers],
                ["video_source_contract_invalid"],
            )
            self.assertIn(
                "manifest visual_ids missing matching HTML data-source-id",
                blockers[0]["message"],
            )
            screenshot.assert_not_called()

            (runs_dir / "video-published").mkdir()
            with (
                patch(
                    "autodesign.candidate_publish.deliver_video_candidate_draft"
                ) as delivery,
                self.assertRaisesRegex(
                    ValueError,
                    "video_source_contract_invalid",
                ),
            ):
                run_candidate_publish_job(
                    run_id="video-published",
                    parent_run_id="video-draft",
                    conversation_id="conv-1",
                    settings=web_server._runtime_only_settings(),
                    cancellation_token=CancellationToken.never(
                        "video-published"
                    ),
                    runs_dir=runs_dir,
                )
            delivery.assert_not_called()
            self.assertFalse((runs_dir / "video-published" / "final").exists())

    def test_video_publication_fails_closed_without_trusted_source_context(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "video-draft"
            final_dir = run_dir / "final"
            project_dir = final_dir / "project"
            project_dir.mkdir(parents=True)
            html = "<!doctype html><html><body><main>Video</main></body></html>"
            (final_dir / "deck.html").write_text(html, encoding="utf-8")
            (project_dir / "index.html").write_text(html, encoding="utf-8")
            (final_dir / "video_author_manifest.json").write_text(
                json.dumps({
                    "version": 1,
                    "language": "en",
                    "target_duration_s": 360,
                    "project_path": "project",
                    "scenes": [],
                }),
                encoding="utf-8",
            )

            with patch(
                "autodesign.candidate_publish.screenshot_html"
            ) as screenshot:
                blockers = web_server._validate_candidate_draft(
                    run_dir,
                    "video",
                    SimpleNamespace(),
                )

            self.assertEqual(
                [item["issue_id"] for item in blockers],
                ["video_trusted_source_context_invalid"],
            )
            screenshot.assert_not_called()

    def test_poster_candidate_validation_does_not_mutate_accepted_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "poster-draft"
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            source = final_dir / "poster.html"
            source.write_text(
                "<!doctype html><html><body>"
                "<main class='paper-poster'><h1>Draft</h1></main>"
                "</body></html>",
                encoding="utf-8",
            )
            (final_dir / "preview.png").write_bytes(b"accepted-preview")
            before_html = sha256_file(source)
            before_preview = sha256_file(final_dir / "preview.png")

            with (
                patch(
                    "autodesign.tools.ingest_document."
                    "_load_ingest_state_from_dir",
                    return_value={},
                ),
                patch(
                    "autodesign.agents.external_designer_author."
                    "ExternalDesignerAuthor._direct_final_validation_feedback",
                    return_value=None,
                ),
                patch.object(web_server, "screenshot_html"),
            ):
                blockers = web_server._validate_candidate_draft(
                    run_dir,
                    "poster",
                    SimpleNamespace(),
                )

            self.assertEqual(blockers, [])
            self.assertEqual(sha256_file(source), before_html)
            self.assertEqual(
                sha256_file(final_dir / "preview.png"),
                before_preview,
            )

    def test_landing_quality_warning_is_publishable_after_fresh_audits(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "landing-draft"
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            (final_dir / "index.html").write_text(
                "<!doctype html><html><body><main>Draft</main></body></html>",
                encoding="utf-8",
            )
            (final_dir / "landing_asset_catalog.json").write_text(
                json.dumps({"assets": []}),
                encoding="utf-8",
            )
            (final_dir / "landing_visual_plan.json").write_text(
                json.dumps({"validation_targets": {}}),
                encoding="utf-8",
            )
            (final_dir / "landing_author_manifest.json").write_text(
                json.dumps({
                    "sidecar_sha256": {
                        name: sha256_file(final_dir / name)
                        for name in (
                            "landing_asset_catalog.json",
                            "landing_visual_plan.json",
                        )
                    },
                }),
                encoding="utf-8",
            )
            (run_dir / "landing_trusted_source_hashes.json").write_text(
                json.dumps({
                    "kind": "landing_trusted_source_hashes",
                    "version": 1,
                    "hashes": {},
                }),
                encoding="utf-8",
            )
            static = {
                "accepted": False,
                "findings": [{
                    "issue_id": "landing_motion_without_reduced_motion",
                    "message": "Motion lacks reduced-motion fallback.",
                }],
                "metrics": {"used_source_visual_ids": []},
            }
            with (
                patch(
                    "autodesign.agents.external_landing_author."
                    "_validate_landing_output",
                    return_value=static,
                ),
                patch(
                    "autodesign.agents.external_landing_author."
                    "audit_landing_html",
                    return_value={"accepted": True, "findings": [], "metrics": {}},
                ),
            ):
                blockers = web_server._validate_candidate_draft(
                    run_dir,
                    "landing",
                    SimpleNamespace(),
                )

            self.assertEqual(blockers, [])
            assessment = json.loads(
                (run_dir / "candidate_delivery_assessment.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(assessment["quality_status"], "ready_with_warnings")
            self.assertEqual(
                assessment["quality_diagnostics"],
                ["landing_motion_without_reduced_motion"],
            )

    def test_landing_publication_fails_closed_without_trusted_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "landing-draft"
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            (final_dir / "index.html").write_text(
                "<!doctype html><html><body><main>Draft</main></body></html>",
                encoding="utf-8",
            )
            (final_dir / "landing_asset_catalog.json").write_text(
                json.dumps({"assets": []}),
                encoding="utf-8",
            )
            with patch(
                "autodesign.agents.external_landing_author."
                "_validate_landing_output",
                return_value={"accepted": True, "findings": [], "metrics": {}},
            ) as validate:
                blockers = web_server._validate_candidate_draft(
                    run_dir,
                    "landing",
                    SimpleNamespace(),
                )

            self.assertEqual(
                [item["issue_id"] for item in blockers],
                ["landing_trusted_source_anchor_invalid"],
            )
            validate.assert_not_called()

    def test_landing_publication_rejects_catalog_that_drops_trusted_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "landing-draft"
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            (final_dir / "index.html").write_text(
                "<!doctype html><html><body><main>Draft</main></body></html>",
                encoding="utf-8",
            )
            (final_dir / "landing_asset_catalog.json").write_text(
                json.dumps({"assets": []}),
                encoding="utf-8",
            )
            (run_dir / "landing_trusted_source_hashes.json").write_text(
                json.dumps({
                    "kind": "landing_trusted_source_hashes",
                    "version": 1,
                    "hashes": {"source_01": "0" * 64},
                }),
                encoding="utf-8",
            )
            with patch(
                "autodesign.agents.external_landing_author."
                "_validate_landing_output",
                return_value={"accepted": True, "findings": [], "metrics": {}},
            ) as validate:
                blockers = web_server._validate_candidate_draft(
                    run_dir,
                    "landing",
                    SimpleNamespace(),
                )

            self.assertEqual(
                [item["issue_id"] for item in blockers],
                ["landing_trusted_source_catalog_mismatch"],
            )
            validate.assert_not_called()

    def test_deck_publication_requires_byte_identical_html_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "deck-draft"
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            (final_dir / "deck.html").write_text("deck", encoding="utf-8")
            (final_dir / "slides.html").write_text("slides", encoding="utf-8")
            (final_dir / "slides_asset_catalog.json").write_text(
                json.dumps({"assets": []}),
                encoding="utf-8",
            )
            (run_dir / "slides_trusted_source_hashes.json").write_text(
                json.dumps({
                    "kind": "slides_trusted_source_hashes",
                    "version": 1,
                    "hashes": {},
                }),
                encoding="utf-8",
            )

            blockers = web_server._validate_candidate_draft(
                run_dir,
                "deck",
                SimpleNamespace(),
            )

            self.assertEqual(
                blockers[0]["issue_id"],
                "deck_html_alias_mismatch",
            )

    def test_poster_candidate_validation_uses_normal_html_first_preflight(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "poster-draft"
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            (final_dir / "poster.html").write_text(
                "<!doctype html><html><body>"
                "<main class='paper-poster'><h1>Draft</h1></main>"
                "</body></html>",
                encoding="utf-8",
            )
            feedback = {
                "error_message": "A required paper source figure is missing.",
                "summary": {
                    "issue_id": "paper_poster_html_source_coverage_failed",
                },
                "payload": {
                    "issue_id": "paper_poster_html_source_coverage_failed",
                    "issues": [{
                        "issue_id": "missing_source",
                        "message": "A required paper source figure is missing.",
                    }],
                },
            }
            with (
                patch(
                    "autodesign.tools.ingest_document."
                    "_load_ingest_state_from_dir",
                    return_value={},
                ),
                patch(
                    "autodesign.agents.external_designer_author."
                    "ExternalDesignerAuthor._direct_final_validation_feedback",
                    return_value=feedback,
                ),
                patch.object(web_server, "screenshot_html"),
            ):
                blockers = web_server._validate_candidate_draft(
                    run_dir,
                    "poster",
                    SimpleNamespace(),
                )

            self.assertEqual(blockers, [{
                "issue_id": "missing_source",
                "message": "A required paper source figure is missing.",
            }])

    def test_poster_validation_context_allows_degraded_memory_without_dossier(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "poster-draft"
            layers_dir = run_dir / "layers"
            layers_dir.mkdir(parents=True)
            (layers_dir / "source.png").write_bytes(b"source")
            payloads = {
                "paper_visual_provenance.json": {
                    "assets": [{
                        "asset_id": "source",
                        "output_file": "layers/source.png",
                        "caption_short": "Source figure",
                    }],
                },
                "paper_memory.json": {"title": "Paper"},
                "paper_visual_storyboard.json": {"selected_assets": []},
                "poster_content_brief.json": {},
                "poster_plan_contract.json": {},
            }
            for name, payload in payloads.items():
                (run_dir / name).write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=layers_dir,
                run_id=run_dir.name,
            )

            loaded = _load_ingest_state_from_dir(ctx, run_dir)

            self.assertIsInstance(loaded, dict)
            self.assertEqual(ctx.state["paper_memory_dossier"], {})
            self.assertIn("source", ctx.state["rendered_layers"])

    def test_saving_a_legacy_raw_poster_attempt_materializes_its_math(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out_dir = Path(raw) / "out"
            source_final = out_dir / "runs" / "poster-draft" / "final"
            source_final.mkdir(parents=True)
            html = (
                "<!doctype html><html><head></head><body>"
                "<main class='paper-poster'>"
                "<h1 data-layer-id='title' data-kind='text'>Edited</h1>"
                "<p>Loss: \\\\(L = x^2\\\\)</p>"
                "</main></body></html>"
            )
            source_html = source_final / "poster.html"
            source_html.write_text(html, encoding="utf-8")
            staged_html = Path(raw) / "staged.html"
            staged_html.write_text(html, encoding="utf-8")

            def fake_screenshot(_html_path, output_path, **_kwargs):
                output_path.write_bytes(b"preview")
                return SimpleNamespace(
                    backend="test",
                    warnings=[],
                    scale=1.0,
                    width_px=3072,
                    height_px=1536,
                )

            settings = SimpleNamespace(
                out_dir=out_dir,
                repo_root=Path(__file__).resolve().parents[1],
                poster_preview_max_edge=2048,
            )
            with (
                patch.object(web_server, "new_run_id", return_value="poster-edited"),
                patch.object(
                    web_server,
                    "screenshot_html",
                    side_effect=fake_screenshot,
                ),
            ):
                result = web_server._apply_authored_paper_poster_edits(
                    source_html,
                    staged_html,
                    settings,
                    "poster-draft",
                    {"title": {"text": "Edited"}},
                    required_color_system={"palette_id": "academic_blue"},
                )

            saved_html = (
                Path(result.run_dir) / "final" / "poster.html"
            ).read_text(encoding="utf-8")
            self.assertIn("data-autodesign-katex", saved_html)

    def test_editing_a_poster_candidate_preserves_its_validation_context(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out_dir = Path(raw) / "out"
            source_run = out_dir / "runs" / "poster-draft"
            final_dir = source_run / "final"
            final_dir.mkdir(parents=True)
            html = (
                "<!doctype html><html><body>"
                "<main class='paper-poster'>"
                "<h1 data-layer-id='title' data-kind='text'>Original</h1>"
                "</main></body></html>"
            )
            (final_dir / "poster.html").write_text(html, encoding="utf-8")
            (source_run / "paper_memory.json").write_text(
                '{"title":"Paper"}',
                encoding="utf-8",
            )
            layers_dir = source_run / "layers"
            layers_dir.mkdir()
            (layers_dir / "ingest_fig_01.png").write_bytes(b"source")
            (source_run / "candidate_draft_lineage.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "status": "draft",
                    "artifact_type": "poster",
                    "source_run_id": "run-source",
                    "source_attempt": 1,
                    "source_candidate_id": "poster-attempt-01",
                    "source_candidate_sha256": "c" * 64,
                }),
                encoding="utf-8",
            )
            original_run = out_dir / "runs" / "run-source"
            original_run.mkdir(parents=True)
            (original_run / "run_brief.json").write_text(
                '{"palette_id":"academic_blue"}',
                encoding="utf-8",
            )
            edited_run = out_dir / "runs" / "poster-edited"

            def fake_screenshot(_html_path, output_path, **_kwargs):
                output_path.write_bytes(b"preview")
                return SimpleNamespace(
                    backend="test",
                    warnings=[],
                    scale=1.0,
                    width_px=3072,
                    height_px=1536,
                )

            with patch(
                "autodesign.artifact_edit_job.screenshot_html",
                side_effect=fake_screenshot,
            ):
                result = self._run_artifact_edit(
                    out_dir=out_dir,
                    parent_run_id="poster-draft",
                    child_run_id="poster-edited",
                    artifact_type="poster",
                    source_name="poster.html",
                    edited_html=html.replace("Original", "Edited"),
                    edits={"title": {"text": "Edited"}},
                    candidate_lineage=json.loads(
                        (source_run / "candidate_draft_lineage.json").read_text(
                            encoding="utf-8"
                        )
                    ),
                )

            self.assertEqual(result["artifact_type"], "poster")
            self.assertTrue((edited_run / "paper_memory.json").is_file())
            self.assertTrue(
                (edited_run / "layers" / "ingest_fig_01.png").is_file()
            )

    def test_video_candidate_edit_preserves_draft_lineage_and_editable_html(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out_dir = Path(raw) / "out"
            source_run = out_dir / "runs" / "video-draft"
            final_dir = source_run / "final"
            project_dir = final_dir / "project"
            project_dir.mkdir(parents=True)
            html = (
                "<!doctype html><html><body>"
                "<main data-autodesign-artifact-root='video'>"
                "<h1 data-layer-id='title' data-kind='text'>Original</h1>"
                "</main></body></html>"
            )
            (final_dir / "deck.html").write_text(html, encoding="utf-8")
            (project_dir / "index.html").write_text(html, encoding="utf-8")
            lineage = {
                "schema_version": 1,
                "status": "draft",
                "artifact_type": "video",
                "source_run_id": "run-source",
                "source_attempt": 1,
                "source_candidate_id": "video-attempt-01",
                "source_candidate_sha256": "a" * 64,
            }
            (source_run / "candidate_draft_lineage.json").write_text(
                json.dumps(lineage),
                encoding="utf-8",
            )
            trusted_context_bytes = (
                b'{"kind":"video_trusted_source_context","version":1,'
                b'"source":"pre_author_actual_source_bytes",'
                b'"eligible_asset_ids":[],"eligible_asset_roles":{},'
                b'"eligible_asset_hashes":{},"required_asset_ids":[],'
                b'"minimum_required_visual_count":0}\n'
            )
            (source_run / "video_trusted_source_context.json").write_bytes(
                trusted_context_bytes
            )

            def fake_screenshot(_html_path, output_path, **_kwargs):
                output_path.write_bytes(b"preview")
                return SimpleNamespace(
                    backend="test",
                    warnings=[],
                    scale=1.0,
                    width_px=1920,
                    height_px=1080,
                )

            with patch(
                "autodesign.artifact_edit_job.screenshot_html",
                side_effect=fake_screenshot,
            ):
                result = self._run_artifact_edit(
                    out_dir=out_dir,
                    parent_run_id="video-draft",
                    child_run_id="video-edited",
                    artifact_type="video",
                    source_name="deck.html",
                    edited_html=html.replace("Original", "Edited title"),
                    edits={"title": {"text": "Edited title"}},
                    candidate_lineage=lineage,
                )

            edited_run = out_dir / "runs" / "video-edited"
            edited_lineage = json.loads(
                (edited_run / "candidate_draft_lineage.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(result["artifact_type"], "video")
            self.assertEqual(result["candidate_lineage"]["status"], "draft")
            self.assertEqual(edited_lineage["status"], "draft")
            self.assertEqual(edited_lineage["parent_draft_run_id"], "video-draft")
            self.assertEqual(
                (edited_run / "video_trusted_source_context.json").read_bytes(),
                trusted_context_bytes,
            )
            self.assertIn(
                "Edited title",
                (edited_run / "final" / "project" / "index.html")
                .read_text(encoding="utf-8"),
            )

    def test_editing_a_published_video_creates_a_new_publishable_draft(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out_dir = Path(raw) / "out"
            source_run = out_dir / "runs" / "video-published"
            final_dir = source_run / "final"
            project_dir = final_dir / "project"
            project_dir.mkdir(parents=True)
            html = (
                "<!doctype html><html><body>"
                "<main data-autodesign-artifact-root='video'>"
                "<h1 data-layer-id='title' data-kind='text'>Published</h1>"
                "</main></body></html>"
            )
            (final_dir / "deck.html").write_text(html, encoding="utf-8")
            (project_dir / "index.html").write_text(html, encoding="utf-8")
            lineage = {
                "schema_version": 1,
                "status": "published",
                "artifact_type": "video",
                "source_run_id": "run-source",
                "source_attempt": 2,
                "source_candidate_id": "video-attempt-02",
                "source_candidate_sha256": "b" * 64,
                "published_version_id": "art_video-published:v1",
                "published_at": "2026-07-29T00:00:00Z",
            }
            (source_run / "candidate_draft_lineage.json").write_text(
                json.dumps(lineage),
                encoding="utf-8",
            )

            def fake_screenshot(_html_path, output_path, **_kwargs):
                output_path.write_bytes(b"preview")
                return SimpleNamespace(
                    backend="test",
                    warnings=[],
                    scale=1.0,
                    width_px=1920,
                    height_px=1080,
                )

            with patch(
                "autodesign.artifact_edit_job.screenshot_html",
                side_effect=fake_screenshot,
            ):
                result = self._run_artifact_edit(
                    out_dir=out_dir,
                    parent_run_id="video-published",
                    child_run_id="video-edited",
                    artifact_type="video",
                    source_name="deck.html",
                    edited_html=html.replace("Published", "Edited after publish"),
                    edits={"title": {"text": "Edited after publish"}},
                    candidate_lineage=lineage,
                )

            edited_run = out_dir / "runs" / "video-edited"
            edited_lineage = json.loads(
                (edited_run / "candidate_draft_lineage.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(result["candidate_lineage"]["status"], "draft")
            self.assertEqual(edited_lineage["status"], "draft")
            self.assertEqual(
                edited_lineage["published_artifact_id_at_fork"],
                "art_video-published",
            )
            self.assertNotIn("published_version_id", edited_lineage)
            self.assertNotIn("published_at", edited_lineage)
            self.assertIn(
                "Edited after publish",
                (edited_run / "final" / "project" / "index.html")
                .read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
