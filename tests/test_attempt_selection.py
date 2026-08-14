from __future__ import annotations

from contextlib import contextmanager, nullcontext
import hashlib
import json
import inspect
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace
import tempfile
import textwrap
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from autodesign import attempt_candidates as attempt_candidates_module
from autodesign import attempt_selection as attempt_selection_module
from autodesign.agents import external_designer_author as external_designer_author_module
from autodesign.agents import external_landing_author as external_landing_author_module
from autodesign.agents import external_slides_author as external_slides_author_module
from autodesign.agents import external_video_author as external_video_author_module
from autodesign.agents import atomic_artifact_promotion as atomic_promotion_module
from autodesign import run_control as run_control_module
from autodesign.util import io as io_module
from autodesign.agents.external_designer_author import ExternalDesignerAuthor
from autodesign.agents.external_landing_author import ExternalLandingAuthor
from autodesign.agents.external_slides_author import ExternalSlidesAuthor
from autodesign.agents.external_video_author import ExternalVideoAuthor
from autodesign.agents.atomic_artifact_promotion import publish_artifact_directory
from autodesign.attempt_candidates import (
    capture_attempt_candidate,
    clear_selection_adapter_transaction,
    load_selection_adapter_transaction,
    load_selection_journal,
    write_selection_adapter_transaction,
    write_selection_journal,
)
from autodesign.attempt_selection import (
    AttemptPromotionRejected,
    assert_promotion_allowed,
    complete_source_run_with_candidate_fork,
    promote_pending_selection,
    request_attempt_selection,
    selected_candidate_for_run,
    selection_is_pending,
)
from autodesign.schema import AttemptIssue, AttemptSelectionJournal
from autodesign.tools._contract import ToolContext


_STEP3A_DEFAULT_BROWSER_RESOURCES = object()


class _Step3aRoute:
    def __init__(
        self,
        url: str,
        *,
        method: str = "GET",
        fulfill_hook=None,
        abort_hook=None,
        request=None,
    ) -> None:
        self.request = request or SimpleNamespace(url=url, method=method)
        self.fulfill_hook = fulfill_hook
        self.abort_hook = abort_hook
        self.fulfill_calls: list[dict[str, object]] = []
        self.abort_calls: list[str] = []

    def fulfill(self, **kwargs) -> None:
        if self.fulfill_hook is not None:
            self.fulfill_hook()
        self.fulfill_calls.append(dict(kwargs))

    def abort(self, reason: str) -> None:
        self.abort_calls.append(reason)
        if self.abort_hook is not None:
            self.abort_hook()


class _Step3aSocket:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _Step3aBrowserContext:
    def __init__(
        self,
        *,
        websocket: bool = True,
        websocket_unroute: bool = True,
        websocket_install_error: BaseException | None = None,
        on_http_unroute=None,
    ) -> None:
        self.offline_values: list[bool] = []
        self.http_routes: list[tuple[str, object]] = []
        self.http_unroutes: list[tuple[str, object]] = []
        self.active_http_routes: list[tuple[str, object]] = []
        self.websocket_routes: list[tuple[str, object]] = []
        self.websocket_unroutes: list[tuple[str, object]] = []
        self.active_websocket_routes: list[tuple[str, object]] = []
        self.websocket_install_error = websocket_install_error
        self.on_http_unroute = on_http_unroute
        self.route_web_socket = (
            self._route_web_socket if websocket else None
        )
        self.unroute_web_socket = (
            self._unroute_web_socket if websocket_unroute else None
        )

    def set_offline(self, value: bool) -> None:
        self.offline_values.append(value)

    def route(self, pattern: str, handler) -> None:
        entry = (pattern, handler)
        self.http_routes.append(entry)
        self.active_http_routes.append(entry)

    def unroute(self, pattern: str, handler) -> None:
        entry = (pattern, handler)
        self.http_unroutes.append(entry)
        if self.on_http_unroute is not None:
            self.on_http_unroute(handler)
        if entry in self.active_http_routes:
            self.active_http_routes.remove(entry)

    def _route_web_socket(self, pattern: str, handler) -> None:
        if self.websocket_install_error is not None:
            raise self.websocket_install_error
        entry = (pattern, handler)
        self.websocket_routes.append(entry)
        self.active_websocket_routes.append(entry)

    def _unroute_web_socket(self, pattern: str, handler) -> None:
        entry = (pattern, handler)
        self.websocket_unroutes.append(entry)
        if entry in self.active_websocket_routes:
            self.active_websocket_routes.remove(entry)


class AttemptSelectionTests(unittest.TestCase):
    def test_delivery_assessment_separates_quality_from_hard_safety(self) -> None:
        try:
            from autodesign.candidate_assessment import assess_delivery_issues
        except ImportError:
            self.fail("candidate delivery assessment is missing")

        quality_cases = (
            (
                "deck",
                {
                    "id": "slides_content_clipped",
                    "message": "a figure caption is clipped",
                    "evidence": {
                        "elements": [{"content_role": "caption"}],
                    },
                },
            ),
            (
                "deck",
                {
                    "id": "slides_required_palette_css_variable_mismatch",
                    "message": "palette polish remains",
                },
            ),
            (
                "deck",
                {
                    "id": "insufficient_visual_unit_slides",
                    "message": "expected at least 13 visual-unit slides, found 12",
                },
            ),
            (
                "deck",
                {
                    "id": "slides_content_clipped",
                    "message": "the title is clipped",
                    "evidence": {
                        "elements": [{"content_role": "title"}],
                    },
                },
            ),
            (
                "deck",
                {
                    "id": "slide_count_mismatch",
                    "message": "expected 13 slides, found 12",
                },
            ),
            (
                "deck",
                {
                    "id": "insufficient_unique_source_visuals",
                    "message": "the deck uses fewer paper visuals than requested",
                },
            ),
            (
                "deck",
                {
                    "id": "insufficient_source_visual_placements",
                    "message": "the deck has fewer visual placements than requested",
                },
            ),
            (
                "deck",
                {
                    "id": "source_visual_missing_local_interpretation",
                    "message": "a paper visual is missing an adjacent interpretation",
                },
            ),
            (
                "landing",
                {
                    "issue_id": "landing_motion_without_reduced_motion",
                    "message": "reduced-motion polish remains",
                },
            ),
            (
                "landing",
                {
                    "issue_id": "landing_content_clipped",
                    "message": "a desktop heading is clipped",
                    "evidence": {
                        "elements": [{"content_role": "heading"}],
                    },
                },
            ),
            (
                "poster",
                {
                    "issue_id": "paper_poster_html_local_flow_overflow",
                    "message": "poster content has local overflow",
                    "evidence": {
                        "elements": [{"content_role": "body"}],
                    },
                },
            ),
        )
        for artifact_type, issue in quality_cases:
            with self.subTest(artifact_type=artifact_type, issue=issue):
                assessment = assess_delivery_issues(artifact_type, [issue])
                self.assertEqual(assessment.safety_state, "ready_with_warnings")
                self.assertEqual(
                    [item.issue_id for item in assessment.quality_diagnostics],
                    [str(issue.get("id") or issue.get("issue_id"))],
                )
                self.assertEqual(assessment.hard_blockers, ())

        hard_cases = (
            ("deck", {"id": "source_visual_hash_mismatch", "message": "changed"}),
            ("landing", {"issue_id": "landing_remote_reference", "message": "remote"}),
            ("landing", {"issue_id": "new_unclassified_gate", "message": "unknown"}),
        )
        for artifact_type, issue in hard_cases:
            with self.subTest(artifact_type=artifact_type, issue=issue):
                assessment = assess_delivery_issues(artifact_type, [issue])
                self.assertEqual(assessment.safety_state, "blocked")
                self.assertEqual(
                    [item.issue_id for item in assessment.hard_blockers],
                    [str(issue.get("id") or issue.get("issue_id"))],
                )
                self.assertEqual(assessment.quality_diagnostics, ())

        self.assertEqual(
            assess_delivery_issues("deck", []).safety_state,
            "ready",
        )

    def test_best_delivery_candidate_normalizes_non_object_metrics(self) -> None:
        for metrics in ([], "malformed"):
            with self.subTest(metrics=metrics), tempfile.TemporaryDirectory() as raw:
                run_dir, candidate = self._candidate(Path(raw))
                validation_path = run_dir / candidate.validation_summary_relative_path
                validation_path.write_text(
                    json.dumps({"kind": "landing_validation", "metrics": metrics}),
                    encoding="utf-8",
                )

                selected = attempt_selection_module.best_delivery_candidate(
                    run_dir,
                    artifact_type="landing",
                )

                self.assertIsNotNone(selected)
                assert selected is not None
                self.assertEqual(selected.candidate_id, candidate.candidate_id)

    def test_poster_best_available_fallback_rejects_provenance_and_allows_polish(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            html_path = root / "poster.html"
            html_path.write_text(
                "<!doctype html><main class='paper-poster'><h1>Paper</h1></main>",
                encoding="utf-8",
            )
            ctx = ToolContext(
                settings=SimpleNamespace(
                    repo_root=Path(__file__).resolve().parents[1],
                ),
                run_dir=root,
                layers_dir=root / "layers",
                run_id="poster-fallback",
            )
            base_candidate = {
                "candidate_id": "poster-candidate",
                "_candidate_dir_abs": str(root),
                "_measure_html_abs": str(html_path),
                "_measure_only_fallback": True,
            }

            blocked = (
                external_designer_author_module
                ._best_available_artifact_fallback_acceptance(
                    ctx,
                    {
                        **base_candidate,
                        "payload": {
                            "issue_id": "paper_poster_html_source_coverage_failed",
                            "issues": [{
                                "issue_id": "paper_poster_html_source_coverage_failed",
                                "message": "required source evidence is missing",
                            }],
                        },
                    },
                    None,
                )
            )
            allowed = (
                external_designer_author_module
                ._best_available_artifact_fallback_acceptance(
                    ctx,
                    {
                        **base_candidate,
                        "payload": {
                            "issue_id": "paper_poster_html_typography_contract_failed",
                            "issues": [{
                                "issue_id": "paper_poster_html_typography_contract_failed",
                                "message": "typography polish remains",
                                "severity": "polish",
                                "soft_finalizable": True,
                            }],
                        },
                    },
                    None,
                )
            )
            overflow_allowed = (
                external_designer_author_module
                ._best_available_artifact_fallback_acceptance(
                    ctx,
                    {
                        **base_candidate,
                        "payload": {
                            "issue_id": "paper_poster_html_local_flow_overflow",
                            "issues": [{
                                "issue_id": "paper_poster_html_local_flow_overflow",
                                "message": "poster content overflows a local panel",
                            }],
                        },
                    },
                    None,
                )
            )

            self.assertFalse(blocked["accepted"])
            self.assertEqual(blocked["reason"], "hard_delivery_issue")
            self.assertTrue(allowed["accepted"])
            self.assertEqual(allowed["quality_status"], "ready_with_warnings")
            self.assertEqual(
                allowed["quality_diagnostics"],
                ["paper_poster_html_typography_contract_failed"],
            )
            self.assertTrue(overflow_allowed["accepted"])
            self.assertEqual(
                overflow_allowed["quality_diagnostics"],
                ["paper_poster_html_local_flow_overflow"],
            )

    def test_poster_best_available_fallback_uses_fresh_candidate_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            html_path = root / "poster.html"
            html_path.write_text(
                "<!doctype html><main class='paper-poster'><h1>Paper</h1></main>",
                encoding="utf-8",
            )
            ctx = ToolContext(
                settings=SimpleNamespace(
                    repo_root=Path(__file__).resolve().parents[1],
                ),
                run_dir=root,
                layers_dir=root / "layers",
                run_id="poster-fallback",
            )
            candidate = {
                "candidate_id": "unvalidated-poster",
                "_candidate_dir_abs": str(root),
                "_measure_html_abs": str(html_path),
                "_measure_only_fallback": True,
                "payload": {},
            }
            stale_other_attempt_feedback = {
                "payload": {
                    "issue_id": "paper_poster_html_typography_contract_failed",
                    "issues": [{
                        "issue_id": "paper_poster_html_typography_contract_failed",
                        "message": "typography polish remains",
                    }],
                },
            }
            fresh_candidate_feedback = {
                "payload": {
                    "issue_id": "paper_poster_html_source_coverage_failed",
                    "issues": [{
                        "issue_id": "paper_poster_html_source_coverage_failed",
                        "message": "required source evidence is missing",
                    }],
                },
            }
            author = ExternalDesignerAuthor(ctx.settings, "")

            def record_promotion(*_args, **_kwargs) -> None:
                ctx.state["finalized"] = True

            with (
                patch.object(
                    external_designer_author_module,
                    "_best_available_artifact_fallback_candidates",
                    return_value=[candidate],
                ),
                patch.object(
                    author,
                    "_direct_final_validation_feedback",
                    return_value=fresh_candidate_feedback,
                ),
                patch.object(
                    author,
                    "_promote_html_first_candidate_fallback",
                    side_effect=record_promotion,
                ),
            ):
                promoted = author._try_promote_best_available_artifact_fallback(
                    ctx,
                    attempt_index=2,
                    attempt_dir=root,
                    last_feedback=stale_other_attempt_feedback,
                    source_reason="attempts_exhausted",
                    source_message="all attempts exhausted",
                )

            self.assertFalse(promoted)
            self.assertFalse(ctx.state.get("finalized", False))
            rejection = ctx.state[
                "designer_author_best_available_artifact_fallback_rejected"
            ][0]
            self.assertEqual(rejection["reason"], "hard_delivery_issue")
            self.assertEqual(
                rejection["details"]["issue_ids"],
                ["paper_poster_html_source_coverage_failed"],
            )

    def test_poster_best_available_fallback_fails_closed_when_preflight_crashes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            html_path = root / "poster.html"
            html_path.write_text(
                "<!doctype html><main class='paper-poster'><h1>Paper</h1></main>",
                encoding="utf-8",
            )
            ctx = ToolContext(
                settings=SimpleNamespace(
                    repo_root=Path(__file__).resolve().parents[1],
                ),
                run_dir=root,
                layers_dir=root / "layers",
                run_id="poster-fallback",
            )
            candidate = {
                "candidate_id": "unvalidated-poster",
                "_candidate_dir_abs": str(root),
                "_measure_html_abs": str(html_path),
                "_measure_only_fallback": True,
                "payload": {},
            }
            author = ExternalDesignerAuthor(ctx.settings, "")

            def record_promotion(*_args, **_kwargs) -> None:
                ctx.state["finalized"] = True

            with (
                patch.object(
                    external_designer_author_module,
                    "_best_available_artifact_fallback_candidates",
                    return_value=[candidate],
                ),
                patch.object(
                    author,
                    "_direct_final_validation_feedback",
                    side_effect=RuntimeError("browser unavailable"),
                ),
                patch.object(
                    author,
                    "_promote_html_first_candidate_fallback",
                    side_effect=record_promotion,
                ),
            ):
                promoted = author._try_promote_best_available_artifact_fallback(
                    ctx,
                    attempt_index=1,
                    attempt_dir=root,
                    last_feedback=None,
                    source_reason="attempts_exhausted",
                    source_message="all attempts exhausted",
                )

            self.assertFalse(promoted)
            self.assertFalse(ctx.state.get("finalized", False))
            rejection = ctx.state[
                "designer_author_best_available_artifact_fallback_rejected"
            ][0]
            self.assertEqual(rejection["reason"], "poster_preflight_failed")

    def test_poster_candidate_capture_does_not_trust_declared_warning_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "poster-run"
            attempt_dir = run_dir / "designer_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "poster.html").write_text(
                "<!doctype html><main class='paper-poster'><h1>Paper</h1></main>",
                encoding="utf-8",
            )
            ctx = ToolContext(
                settings=SimpleNamespace(
                    repo_root=Path(__file__).resolve().parents[1],
                ),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id="poster-run",
            )

            def fake_preview(*, preview_path: Path, **_kwargs):
                preview_path.write_bytes(b"preview")
                return SimpleNamespace(warnings=[])

            with patch.object(
                external_designer_author_module,
                "_render_direct_preview",
                side_effect=fake_preview,
            ):
                candidate = (
                    external_designer_author_module.capture_poster_attempt_candidate(
                        ctx=ctx,
                        attempt=1,
                        max_attempts=3,
                        attempt_dir=attempt_dir,
                        diagnostics={
                            "candidate_safety_state": "ready_with_warnings",
                            "payload": {
                                "issue_id": "paper_poster_html_source_coverage_failed",
                                "issues": [{
                                    "issue_id": "paper_poster_html_source_coverage_failed",
                                    "message": "required source evidence is missing",
                                }],
                            },
                        },
                    )
                )

            self.assertEqual(candidate.safety_state, "blocked")
            self.assertEqual(
                [issue.issue_id for issue in candidate.hard_blockers],
                ["paper_poster_html_source_coverage_failed"],
            )
            self.assertEqual(candidate.warnings, [])

    def test_selected_landing_revalidates_tampered_metadata_and_rejects_remote_html(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            attempt_dir = run_dir / "landing_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "index.html").write_text(
                "<!doctype html><html><body><main><h1>Paper</h1>"
                "<img src='https://evil.invalid/source.png' alt='remote'>"
                "</main></body></html>",
                encoding="utf-8",
            )
            sidecars = {
                "designer_author_done.json": {"status": "done"},
                "landing_asset_catalog.json": {"assets": []},
                "landing_visual_plan.json": {},
            }
            for name, payload in sidecars.items():
                (attempt_dir / name).write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )
            (attempt_dir / "landing_validation.json").write_text(
                json.dumps({
                    "accepted": False,
                    "findings": [{
                        "issue_id": "landing_remote_reference",
                        "message": "remote references are forbidden",
                    }],
                    "metrics": {},
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
            candidate = capture_attempt_candidate(
                run_dir=run_dir,
                attempt_dir=attempt_dir,
                artifact_type="landing",
                attempt=1,
                max_attempts=1,
                source_path="index.html",
                dependency_paths=list(sidecars),
                preview_paths=[],
                validation_summary_path="landing_validation.json",
                safety_state="blocked",
                hard_blockers=[
                    AttemptIssue(
                        issue_id="landing_remote_reference",
                        message="remote references are forbidden",
                    )
                ],
                warnings=[],
            )

            manifest_path = attempt_dir / "attempt_candidate.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update({
                "safety_state": "ready",
                "hard_blockers": [],
                "warnings": [],
            })
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            snapshot_validation = (
                run_dir / candidate.validation_summary_relative_path
            )
            snapshot_validation.write_text(
                json.dumps({"accepted": True, "findings": [], "metrics": {}}),
                encoding="utf-8",
            )

            accepted = request_attempt_selection(
                run_dir=run_dir,
                run_id=run_dir.name,
                attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
                idempotency_key="tampered-landing-metadata",
            )
            self.assertEqual(accepted.status, "selection_accepted")
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id=run_dir.name,
            )

            def fake_screenshot(_html_path, out_path, **_kwargs):
                out_path.write_bytes(b"preview")
                return SimpleNamespace(backend="test", warnings=[])

            with (
                patch.object(
                    external_landing_author_module,
                    "audit_landing_html",
                    return_value={
                        "accepted": True,
                        "backend": "test",
                        "findings": [],
                        "metrics": {},
                    },
                ),
                patch.object(
                    external_landing_author_module,
                    "screenshot_html",
                    side_effect=fake_screenshot,
                ),
            ):
                outcome = promote_pending_selection(
                    ctx,
                    promoter=external_landing_author_module.promote_selected_attempt,
                )

            self.assertEqual(outcome, "failed")
            self.assertEqual(load_selection_journal(run_dir).state, "failed")
            self.assertFalse((run_dir / "final" / "index.html").exists())

    def test_selected_deck_revalidates_snapshot_before_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            attempt_dir = run_dir / "slides_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "slides.html").write_text(
                "<!doctype html><html><head><style>"
                ".deck-slide{width:1920px;height:1080px}"
                "</style></head><body><main id='deck'>"
                "<section class='deck-slide' id='slide-1'><h1>Paper</h1></section>"
                "</main><script>document.addEventListener('keydown',e=>{"
                "if(e.key==='ArrowLeft'||e.key==='ArrowRight'){}"
                "})</script></body></html>",
                encoding="utf-8",
            )
            validation = {
                "status": "ok",
                "expected_slide_count": 1,
                "actual_slide_count": 1,
                "source_visual_ids": [],
                "issues": [],
            }
            sidecars = {
                "designer_author_done.json": {"status": "done"},
                "slides_visual_plan.json": {"targets": {}},
                "slides_asset_catalog.json": {"assets": []},
            }
            for name, payload in sidecars.items():
                (attempt_dir / name).write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )
            (attempt_dir / "slides_validation.json").write_text(
                json.dumps(validation),
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
            candidate = capture_attempt_candidate(
                run_dir=run_dir,
                attempt_dir=attempt_dir,
                artifact_type="deck",
                attempt=1,
                max_attempts=1,
                source_path="slides.html",
                dependency_paths=list(sidecars),
                preview_paths=[],
                validation_summary_path="slides_validation.json",
                safety_state="ready",
                hard_blockers=[],
                warnings=[],
            )
            request_attempt_selection(
                run_dir=run_dir,
                run_id=run_dir.name,
                attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
                idempotency_key="fresh-deck-validation",
            )
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id=run_dir.name,
            )
            ctx.state["deck_plan"] = {
                "lock_level": "hard",
                "slide_count": 1,
            }

            def fake_slide_capture(_html_path, slides_dir, **_kwargs):
                slides_dir.mkdir(parents=True, exist_ok=True)
                slide = slides_dir / "slide-1.png"
                slide.write_bytes(b"slide")
                return SimpleNamespace(backend="test", paths=[slide], warnings=[])

            with (
                patch.object(
                    external_slides_author_module,
                    "_validate_slides",
                    return_value={
                        **validation,
                        "status": "error",
                        "issues": [{
                            "id": "source_visual_hash_mismatch",
                            "message": "source bytes changed",
                        }],
                    },
                ),
                patch.object(
                    external_slides_author_module,
                    "audit_slides_html",
                    return_value={
                        "accepted": True,
                        "backend": "test",
                        "findings": [],
                        "metrics": {},
                    },
                ),
                patch.object(
                    external_slides_author_module,
                    "screenshot_deck_slides",
                    side_effect=fake_slide_capture,
                ),
                patch.object(
                    external_slides_author_module,
                    "build_deck_preview_grid",
                    side_effect=lambda _paths, output: output.write_bytes(b"preview"),
                ),
            ):
                outcome = promote_pending_selection(
                    ctx,
                    promoter=external_slides_author_module.promote_selected_attempt,
                )

            self.assertEqual(outcome, "failed")
            self.assertFalse((run_dir / "final" / "deck.html").exists())

    def test_selected_poster_delivers_fresh_quality_warning(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            attempt_dir = run_dir / "designer_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "poster.html").write_text(
                "<!doctype html><html><body>"
                "<main class='paper-poster'><h1>Paper</h1></main>"
                "</body></html>",
                encoding="utf-8",
            )
            (attempt_dir / "validation.json").write_text(
                json.dumps({"accepted": True}),
                encoding="utf-8",
            )
            candidate = capture_attempt_candidate(
                run_dir=run_dir,
                attempt_dir=attempt_dir,
                artifact_type="poster",
                attempt=1,
                max_attempts=1,
                source_path="poster.html",
                dependency_paths=[],
                preview_paths=[],
                validation_summary_path="validation.json",
                safety_state="ready",
                hard_blockers=[],
                warnings=[],
            )
            request_attempt_selection(
                run_dir=run_dir,
                run_id=run_dir.name,
                attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
                idempotency_key="fresh-poster-quality",
            )
            ctx = ToolContext(
                settings=SimpleNamespace(repo_root=Path(raw)),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id=run_dir.name,
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

            quality_feedback = {
                "payload": {
                    "issue_id": "paper_poster_html_typography_contract_failed",
                    "issues": [{
                        "issue_id": "paper_poster_html_typography_contract_failed",
                        "message": "typography polish remains",
                    }],
                }
            }
            with (
                patch.object(
                    ExternalDesignerAuthor,
                    "_direct_final_validation_feedback",
                    return_value=quality_feedback,
                ),
                patch.object(
                    external_designer_author_module,
                    "_render_direct_preview",
                    side_effect=fake_render,
                ),
                patch.object(
                    external_designer_author_module,
                    "_maybe_repair_collapsed_poster_header",
                    return_value=None,
                ),
            ):
                outcome = promote_pending_selection(
                    ctx,
                    promoter=external_designer_author_module.promote_selected_attempt,
                )

            self.assertEqual(outcome, "complete")
            manifest = json.loads(
                (
                    run_dir / "final" / "designer_author_direct_manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["quality_status"], "ready_with_warnings")
            self.assertEqual(
                manifest["quality_diagnostics"],
                ["paper_poster_html_typography_contract_failed"],
            )

    def test_tool_context_captures_only_an_existing_run_directory_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "nested" / "run-1"

            missing = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id="run-1",
            )
            self.assertFalse(run_dir.exists())
            self.assertIsNone(missing.run_directory_identity)

            run_dir.mkdir(parents=True)
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id="run-1",
            )
            metadata = os.lstat(run_dir)
            self.assertEqual(
                ctx.run_directory_identity,
                (metadata.st_dev, metadata.st_ino),
            )

    def _normal_alias_runs(
        self,
        root: Path,
    ) -> tuple[Path, Path, Path, Path, Path]:
        first_parent = root / "first-parent"
        second_parent = root / "second-parent"
        first_run = first_parent / "run-1"
        second_run = second_parent / "run-1"
        first_run.mkdir(parents=True)
        second_run.mkdir(parents=True)
        alias_parent = root / "alias-parent"
        alias_parent.symlink_to(first_parent, target_is_directory=True)
        for run_dir, marker in ((first_run, "A-old"), (second_run, "B-old")):
            final_dir = run_dir / "final"
            final_dir.mkdir()
            (final_dir / "marker.txt").write_text(marker, encoding="utf-8")
        return (
            alias_parent / "run-1",
            first_run,
            second_run,
            alias_parent,
            second_parent,
        )

    def _retarget_normal_alias(
        self,
        *,
        alias_parent: Path,
        second_parent: Path,
        second_run: Path,
        staging_name: str,
    ) -> Path:
        alias_parent.unlink()
        alias_parent.symlink_to(second_parent, target_is_directory=True)
        injected_staging = second_run / staging_name
        injected_staging.mkdir()
        (injected_staging / "marker.txt").write_text(
            "B-injected",
            encoding="utf-8",
        )
        return injected_staging

    def _assert_normal_alias_publish_wrote_neither_run(
        self,
        *,
        first_run: Path,
        second_run: Path,
        artifact_name: str,
        injected_staging: Path,
    ) -> None:
        self.assertEqual(
            (first_run / "final" / "marker.txt").read_text(encoding="utf-8"),
            "A-old",
        )
        self.assertEqual(
            (second_run / "final" / "marker.txt").read_text(encoding="utf-8"),
            "B-old",
        )
        journal_name = f".{artifact_name}-final-promotion.json"
        self.assertFalse((first_run / journal_name).exists())
        self.assertFalse((second_run / journal_name).exists())
        self.assertEqual(
            (injected_staging / "marker.txt").read_text(encoding="utf-8"),
            "B-injected",
        )

    def _candidate(
        self,
        root: Path,
        *,
        attempt: int = 1,
        safety_state: str = "ready",
    ):
        run_dir = root / "run-1"
        attempt_dir = run_dir / "landing_author" / f"attempt_{attempt:02d}"
        attempt_dir.mkdir(parents=True)
        (attempt_dir / "index.html").write_text(
            f"<!doctype html><h1>Attempt {attempt}</h1>",
            encoding="utf-8",
        )
        (attempt_dir / "preview.png").write_bytes(f"preview-{attempt}".encode())
        (attempt_dir / "validation.json").write_text(
            json.dumps({"accepted": safety_state != "blocked"}),
            encoding="utf-8",
        )
        blockers = (
            [AttemptIssue(issue_id="missing_source", message="Missing source")]
            if safety_state == "blocked"
            else []
        )
        candidate = capture_attempt_candidate(
            run_dir=run_dir,
            attempt_dir=attempt_dir,
            artifact_type="landing",
            attempt=attempt,
            max_attempts=4,
            source_path="index.html",
            dependency_paths=[],
            preview_paths=["preview.png"],
            validation_summary_path="validation.json",
            safety_state=safety_state,
            hard_blockers=blockers,
            warnings=[],
        )
        return run_dir, candidate

    def _step3a_browser_candidate(
        self,
        root: Path,
        *,
        document_bytes: bytes = b"<!doctype html><main>document</main>",
        dependencies: dict[str, bytes] | None = None,
        browser_resources=_STEP3A_DEFAULT_BROWSER_RESOURCES,
    ):
        run_dir = root / "run-1"
        attempt_dir = run_dir / "landing_author" / "attempt_01"
        attempt_dir.mkdir(parents=True)
        (attempt_dir / "index.html").write_bytes(document_bytes)
        dependencies = dict(dependencies or {})
        for relative, payload in dependencies.items():
            target = attempt_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        (attempt_dir / "preview.png").write_bytes(b"preview")
        (attempt_dir / "validation.json").write_text("{}", encoding="utf-8")
        selected_browser_resources = (
            list(dependencies)
            if browser_resources is _STEP3A_DEFAULT_BROWSER_RESOURCES
            else list(browser_resources)
        )
        candidate = capture_attempt_candidate(
            run_dir=run_dir,
            attempt_dir=attempt_dir,
            artifact_type="landing",
            attempt=1,
            max_attempts=1,
            source_path="index.html",
            dependency_paths=list(dependencies),
            browser_resource_paths=selected_browser_resources,
            preview_paths=["preview.png"],
            validation_summary_path="validation.json",
            safety_state="ready",
            hard_blockers=[],
            warnings=[],
        )
        return (
            run_dir,
            candidate,
            attempt_dir / "attempt_candidate.json",
        )

    def _step3a_session_factory(self):
        factory = getattr(
            attempt_candidates_module,
            "promotion_browser_document_session",
            None,
        )
        self.assertTrue(
            callable(factory),
            "promotion rendering requires a lease-bound document session",
        )
        return factory

    def _step3a_route_member(
        self,
        session,
        context: _Step3aBrowserContext,
        relative: str,
        **route_kwargs,
    ) -> _Step3aRoute:
        self.assertTrue(context.http_routes, "session did not install an HTTP route")
        parsed = urlsplit(session.url)
        route = _Step3aRoute(
            f"{parsed.scheme}://{parsed.netloc}/{relative}",
            **route_kwargs,
        )
        context.http_routes[-1][1](route)
        return route

    def _step3a_assert_session_build_rejected(
        self,
        run_dir: Path,
        source_relative_path: str,
        *,
        message: str,
    ) -> None:
        caught: BaseException | None = None
        try:
            with attempt_candidates_module.attempt_promotion_lease(
                run_dir
            ) as leased_run_dir:
                with self._step3a_session_factory()(
                    leased_run_dir / source_relative_path
                ):
                    pass
        except (OSError, RuntimeError, ValueError) as exc:
            caught = exc
        self.assertIsNotNone(caught, message)

    @contextmanager
    def _step3a_capture_secure_read_caps(self):
        calls: list[tuple[str, object]] = []
        real_read = attempt_candidates_module.SecureRunMemberAccessor.read_bytes

        def tracked_read(accessor, value, **kwargs):
            calls.append((os.fspath(value), kwargs.get("max_bytes")))
            return real_read(accessor, value, **kwargs)

        with patch.object(
            attempt_candidates_module.SecureRunMemberAccessor,
            "read_bytes",
            new=tracked_read,
        ):
            yield calls

    def _selection_journal_fixture(
        self,
        *,
        run_id: str,
    ) -> AttemptSelectionJournal:
        return AttemptSelectionJournal(
            run_id=run_id,
            candidate_id="landing-attempt-01-ready",
            candidate_sha256="a" * 64,
            source_attempt=1,
            idempotency_key="lease-logical-run-id",
            state="requested",
            updated_at="2026-08-03T00:00:00+00:00",
        )

    def test_selection_journal_round_trips_through_lease_stable_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "logical-run-id"
            run_dir.mkdir()
            journal = self._selection_journal_fixture(run_id=run_dir.name)
            write_selection_journal(run_dir, journal)
            context_type = attempt_candidates_module._OpenedControlContext
            real_read = context_type.read_json
            real_write = context_type.write_json
            read_contexts = []
            write_contexts = []

            def record_read(context, name: str):
                read_contexts.append(context)
                return real_read(context, name)

            def record_write(context, name: str, payload: dict[str, object]):
                write_contexts.append(context)
                return real_write(context, name, payload)

            with attempt_candidates_module.attempt_promotion_lease(
                run_dir
            ) as leased_run_dir:
                with (
                    patch.object(
                        context_type,
                        "read_json",
                        new=record_read,
                    ),
                    patch.object(
                        context_type,
                        "write_json",
                        new=record_write,
                    ),
                ):
                    loaded = load_selection_journal(leased_run_dir)
                    promoting = attempt_selection_module.transition_selection(
                        leased_run_dir,
                        "promoting",
                    )

                self.assertEqual(loaded, journal)
                self.assertEqual(promoting.state, "promoting")
                self.assertTrue(read_contexts)
                self.assertTrue(write_contexts)
                for context in [*read_contexts, *write_contexts]:
                    self.assertIsNotNone(context.binding)
                    self.assertEqual(context.logical_run_id, run_dir.name)
                    self.assertEqual(
                        context.run_identity,
                        attempt_candidates_module.promotion_run_identity(run_dir),
                    )

                stable_run_dir = Path(os.fspath(leased_run_dir))
                delivering = attempt_selection_module.transition_selection(
                    stable_run_dir,
                    "delivering",
                )
                self.assertEqual(delivering.run_id, run_dir.name)
                self.assertEqual(delivering.state, "delivering")

            persisted = load_selection_journal(run_dir)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted.state, "delivering")

    def test_selection_journal_stable_root_does_not_lstat_descriptor_alias(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "logical-run-id"
            run_dir.mkdir()
            journal = self._selection_journal_fixture(run_id=run_dir.name)
            write_selection_journal(run_dir, journal)
            real_lstat = attempt_candidates_module._portable_lstat

            with attempt_candidates_module.attempt_promotion_lease(
                run_dir
            ) as leased_run_dir:
                stable_run_dir = Path(os.fspath(leased_run_dir))

                def reject_stable_alias_lstat(path: Path):
                    candidate = Path(os.path.abspath(os.fspath(path)))
                    if candidate == stable_run_dir:
                        raise AssertionError(
                            "exact stable descriptor aliases must not be lstat-followed"
                        )
                    return real_lstat(path)

                with patch.object(
                    attempt_candidates_module,
                    "_portable_lstat",
                    side_effect=reject_stable_alias_lstat,
                ):
                    loaded = load_selection_journal(stable_run_dir)
                    updated = attempt_selection_module.transition_selection(
                        stable_run_dir,
                        "promoting",
                    )

                self.assertEqual(loaded, journal)
                self.assertEqual(updated.state, "promoting")

    def test_closed_selection_journal_lease_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "logical-run-id"
            run_dir.mkdir()
            journal = self._selection_journal_fixture(run_id=run_dir.name)
            write_selection_journal(run_dir, journal)

            with attempt_candidates_module.attempt_promotion_lease(
                run_dir
            ) as leased_run_dir:
                stale_guarded_path = leased_run_dir
                stale_stable_path = Path(os.fspath(leased_run_dir))

            for path_kind, stale_path in (
                ("guarded", stale_guarded_path),
                ("stable", stale_stable_path),
            ):
                with self.subTest(path_kind=path_kind):
                    with self.assertRaises((OSError, ValueError)):
                        load_selection_journal(stale_path)
                    with self.assertRaises((OSError, ValueError)):
                        write_selection_journal(stale_path, journal)

    def test_unrelated_path_during_active_lease_cannot_inherit_logical_run_id(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "logical-run-id"
            unrelated = root / "unrelated-alias"
            run_dir.mkdir()
            control_dir = unrelated / "attempt_candidates"
            control_dir.mkdir(parents=True)
            journal = self._selection_journal_fixture(run_id=run_dir.name)
            (control_dir / "selection.json").write_text(
                json.dumps(journal.model_dump(mode="json")),
                encoding="utf-8",
            )

            with attempt_candidates_module.attempt_promotion_lease(run_dir):
                with self.assertRaisesRegex(ValueError, "run_id mismatch"):
                    load_selection_journal(unrelated)
                with self.assertRaisesRegex(ValueError, "run_id mismatch"):
                    write_selection_journal(unrelated, journal)

    def test_nonlease_selection_journal_basename_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "physical-alias"
            control_dir = run_dir / "attempt_candidates"
            control_dir.mkdir(parents=True)
            journal = self._selection_journal_fixture(run_id="logical-run-id")
            (control_dir / "selection.json").write_text(
                json.dumps(journal.model_dump(mode="json")),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "run_id mismatch"):
                load_selection_journal(run_dir)
            with self.assertRaisesRegex(ValueError, "run_id mismatch"):
                write_selection_journal(run_dir, journal)

    @unittest.skipUnless(os.name == "posix", "parent aliases use POSIX symlinks")
    def test_selection_journal_rejects_same_inode_parent_alias_before_control_io(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            real_parent = root / "real-parent"
            run_dir = real_parent / "same-run-name"
            run_dir.mkdir(parents=True)
            journal = self._selection_journal_fixture(run_id=run_dir.name)
            write_selection_journal(run_dir, journal)
            alias_parent = root / "alias-parent"
            alias_parent.symlink_to(real_parent, target_is_directory=True)
            alias_run = alias_parent / run_dir.name

            with attempt_candidates_module.attempt_promotion_lease(run_dir):
                with (
                    patch.object(
                        attempt_candidates_module,
                        "_read_control_json",
                        side_effect=AssertionError("journal read reached control I/O"),
                    ) as read_control,
                    self.assertRaisesRegex(ValueError, "exact active lease root"),
                ):
                    load_selection_journal(alias_run)
                read_control.assert_not_called()

                with (
                    patch.object(
                        attempt_candidates_module,
                        "_write_control_json",
                        side_effect=AssertionError("journal write reached control I/O"),
                    ) as write_control,
                    self.assertRaisesRegex(ValueError, "exact active lease root"),
                ):
                    write_selection_journal(alias_run, journal)
                write_control.assert_not_called()

    def test_selection_journal_distinct_same_basename_run_keeps_own_control_io(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            leased_run = root / "leased-parent" / "same-run-name"
            distinct_run = root / "distinct-parent" / "same-run-name"
            leased_run.mkdir(parents=True)
            distinct_run.mkdir(parents=True)
            leased_journal = self._selection_journal_fixture(
                run_id=leased_run.name
            )
            distinct_journal = self._selection_journal_fixture(
                run_id=distinct_run.name
            )
            write_selection_journal(leased_run, leased_journal)
            write_selection_journal(distinct_run, distinct_journal)
            updated_distinct = distinct_journal.model_copy(
                update={"idempotency_key": "distinct-run-update"}
            )
            context_type = attempt_candidates_module._OpenedControlContext
            real_read = context_type.read_json
            real_write = context_type.write_json
            read_paths: list[Path] = []
            write_paths: list[Path] = []

            def record_read(context, name: str):
                read_paths.append(context.requested_run_path)
                self.assertIsNone(context.binding)
                return real_read(context, name)

            def record_write(context, name: str, payload: dict[str, object]):
                write_paths.append(context.requested_run_path)
                self.assertIsNone(context.binding)
                return real_write(context, name, payload)

            with attempt_candidates_module.attempt_promotion_lease(leased_run):
                with (
                    patch.object(
                        context_type,
                        "read_json",
                        new=record_read,
                    ),
                    patch.object(
                        context_type,
                        "write_json",
                        new=record_write,
                    ),
                ):
                    self.assertEqual(
                        load_selection_journal(distinct_run),
                        distinct_journal,
                    )
                    write_selection_journal(distinct_run, updated_distinct)

            self.assertEqual(read_paths, [distinct_run])
            self.assertEqual(write_paths, [distinct_run])
            self.assertEqual(
                load_selection_journal(distinct_run),
                updated_distinct,
            )
            self.assertEqual(
                load_selection_journal(leased_run),
                leased_journal,
            )

    def test_ready_candidate_request_is_durable_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir, candidate = self._candidate(Path(raw))

            accepted = request_attempt_selection(
                run_dir=run_dir,
                run_id="run-1",
                attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
                idempotency_key="choice-1",
            )
            duplicate = request_attempt_selection(
                run_dir=run_dir,
                run_id="run-1",
                attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
                idempotency_key="choice-1",
            )

            self.assertEqual(accepted.status, "selection_accepted")
            self.assertEqual(duplicate.status, "already_selected")
            journal = load_selection_journal(run_dir)
            self.assertIsNotNone(journal)
            assert journal is not None
            self.assertEqual(journal.state, "requested")
            self.assertTrue(selection_is_pending(run_dir))
            self.assertEqual(
                selected_candidate_for_run(run_dir).candidate_id,
                candidate.candidate_id,
            )

    def test_registered_process_moves_journal_to_terminating(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir, candidate = self._candidate(Path(raw))
            with patch(
                "autodesign.attempt_selection.terminate_registered_author_process",
                return_value=True,
            ):
                result = request_attempt_selection(
                    run_dir=run_dir,
                    run_id="run-1",
                    attempt=1,
                    expected_candidate_sha256=candidate.source_sha256,
                    idempotency_key="choice-1",
                )

            self.assertEqual(result.status, "selection_accepted")
            self.assertEqual(load_selection_journal(run_dir).state, "terminating")

    def test_manual_selection_accepts_ready_with_warnings_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir, candidate = self._candidate(
                Path(raw),
                safety_state="ready_with_warnings",
            )

            result = request_attempt_selection(
                run_dir=run_dir,
                run_id="run-1",
                attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
                idempotency_key="warning-candidate",
            )

            self.assertEqual(result.status, "selection_accepted")
            self.assertEqual(load_selection_journal(run_dir).state, "requested")

    def test_blocked_changed_and_post_final_candidates_do_not_stop_authoring(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir, blocked = self._candidate(
                root,
                safety_state="blocked",
            )
            blocked_result = request_attempt_selection(
                run_dir=run_dir,
                run_id="run-1",
                attempt=1,
                expected_candidate_sha256=blocked.source_sha256,
                idempotency_key="blocked",
            )
            self.assertEqual(blocked_result.status, "candidate_blocked")
            self.assertIsNone(load_selection_journal(run_dir))

        with tempfile.TemporaryDirectory() as raw:
            run_dir, ready = self._candidate(Path(raw))
            changed = request_attempt_selection(
                run_dir=run_dir,
                run_id="run-1",
                attempt=1,
                expected_candidate_sha256="f" * 64,
                idempotency_key="changed",
            )
            self.assertEqual(changed.status, "candidate_changed")
            self.assertIsNone(load_selection_journal(run_dir))

            final_dir = run_dir / "final"
            final_dir.mkdir()
            (final_dir / "index.html").write_text("published", encoding="utf-8")
            post_final = request_attempt_selection(
                run_dir=run_dir,
                run_id="run-1",
                attempt=1,
                expected_candidate_sha256=ready.source_sha256,
                idempotency_key="late",
            )
            self.assertEqual(post_final.status, "run_not_selectable")
            self.assertIsNone(load_selection_journal(run_dir))

    def test_conflicting_candidate_is_rejected_after_selection(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir, first = self._candidate(root, attempt=1)
            _, second = self._candidate(root, attempt=2)
            request_attempt_selection(
                run_dir=run_dir,
                run_id="run-1",
                attempt=1,
                expected_candidate_sha256=first.source_sha256,
                idempotency_key="first",
            )

            conflict = request_attempt_selection(
                run_dir=run_dir,
                run_id="run-1",
                attempt=2,
                expected_candidate_sha256=second.source_sha256,
                idempotency_key="second",
            )

            self.assertEqual(conflict.status, "run_not_selectable")
            with self.assertRaises(AttemptPromotionRejected):
                assert_promotion_allowed(
                    run_dir=run_dir,
                    candidate_id=second.candidate_id,
                )
            assert_promotion_allowed(
                run_dir=run_dir,
                candidate_id=first.candidate_id,
            )

    def test_pending_selection_promotes_once_and_records_complete(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir, candidate = self._candidate(Path(raw))
            request_attempt_selection(
                run_dir=run_dir,
                run_id="run-1",
                attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
                idempotency_key="choice-1",
            )
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id="run-1",
            )
            calls: list[str] = []

            outcome = promote_pending_selection(
                ctx,
                promoter=lambda _ctx, chosen: calls.append(chosen.candidate_id),
            )
            repeated = promote_pending_selection(
                ctx,
                promoter=lambda _ctx, chosen: calls.append(chosen.candidate_id),
            )

            self.assertEqual(outcome, "complete")
            self.assertEqual(repeated, "complete")
            self.assertEqual(calls, [candidate.candidate_id])
            journal = load_selection_journal(run_dir)
            self.assertEqual(journal.state, "complete")
            self.assertEqual(journal.artifact_id, "art_run-1")
            self.assertFalse(selection_is_pending(run_dir))

    def test_every_accepted_journal_state_stops_normal_authoring(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for state, expected in (
                ("promoting", "in_progress"),
                ("delivering", "in_progress"),
                ("complete", "complete"),
            ):
                with self.subTest(state=state):
                    run_dir, candidate = self._candidate(root / state)
                    write_selection_journal(
                        run_dir,
                        AttemptSelectionJournal(
                            run_id=run_dir.name,
                            candidate_id=candidate.candidate_id,
                            candidate_sha256=candidate.source_sha256,
                            source_attempt=candidate.attempt,
                            idempotency_key=f"accepted-{state}",
                            state=state,
                            artifact_id=("art_run-1" if state == "complete" else None),
                            updated_at="2026-08-03T00:00:00+00:00",
                        ),
                    )
                    ctx = ToolContext(
                        settings=SimpleNamespace(),
                        run_dir=run_dir,
                        layers_dir=run_dir / "layers",
                        run_id=run_dir.name,
                    )
                    calls: list[str] = []

                    outcome = promote_pending_selection(
                        ctx,
                        promoter=lambda _ctx, selected: calls.append(
                            selected.candidate_id
                        ),
                    )

                    self.assertEqual(outcome, expected)
                    self.assertEqual(calls, [])

    @unittest.skipUnless(os.name == "posix", "owner-crash recovery uses POSIX signals")
    def test_owner_crash_after_adapter_commit_recovers_without_replaying(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir, candidate = self._candidate(root)
            request_attempt_selection(
                run_dir=run_dir,
                run_id=run_dir.name,
                attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
                idempotency_key="owner-crash",
            )
            before_complete = root / "before-complete"
            worker_script = root / "promotion_owner.py"
            worker_script.write_text(
                textwrap.dedent(
                    f"""
                    from pathlib import Path
                    import time
                    from types import SimpleNamespace

                    import autodesign.attempt_selection as selection
                    from autodesign.tools._contract import ToolContext

                    run_dir = Path({str(run_dir)!r})
                    marker = Path({str(before_complete)!r})
                    original_transition = selection.transition_selection

                    def block_before_complete(run_dir, state, **kwargs):
                        if state == "complete":
                            marker.touch()
                            time.sleep(60)
                        return original_transition(run_dir, state, **kwargs)

                    selection.transition_selection = block_before_complete
                    ctx = ToolContext(
                        settings=SimpleNamespace(),
                        run_dir=run_dir,
                        layers_dir=run_dir / "layers",
                        run_id=run_dir.name,
                    )
                    selection.promote_pending_selection(
                        ctx,
                        promoter=lambda _ctx, _candidate: None,
                    )
                    """
                ),
                encoding="utf-8",
            )
            worker = subprocess.Popen(
                [sys.executable, str(worker_script)],
                cwd=Path(__file__).resolve().parents[1],
            )
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not before_complete.is_file():
                    time.sleep(0.01)
                self.assertTrue(before_complete.is_file())
                worker.kill()
                worker.wait(timeout=3)
                self.assertEqual(load_selection_journal(run_dir).state, "promoting")

                replayed: list[str] = []
                ctx = ToolContext(
                    settings=SimpleNamespace(),
                    run_dir=run_dir,
                    layers_dir=run_dir / "layers",
                    run_id=run_dir.name,
                )
                outcome = promote_pending_selection(
                    ctx,
                    promoter=lambda _ctx, selected: replayed.append(
                        selected.candidate_id
                    ),
                )

                self.assertEqual(outcome, "complete")
                self.assertEqual(replayed, [])
                self.assertEqual(load_selection_journal(run_dir).state, "complete")
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait(timeout=3)

    def test_adapters_stop_at_entry_for_complete_selection(self) -> None:
        adapters = (
            (ExternalDesignerAuthor, "designer_author_result"),
            (ExternalLandingAuthor, "landing_author_failure"),
            (ExternalSlidesAuthor, "slides_author_failure"),
            (ExternalVideoAuthor, "video_author_failure"),
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for adapter_type, failure_key in adapters:
                with self.subTest(adapter=adapter_type.__name__):
                    run_dir = root / adapter_type.__name__
                    run_dir.mkdir()
                    write_selection_journal(
                        run_dir,
                        AttemptSelectionJournal(
                            run_id=run_dir.name,
                            candidate_id="landing-attempt-01-selected",
                            candidate_sha256="a" * 64,
                            source_attempt=1,
                            idempotency_key="already-complete",
                            state="complete",
                            artifact_id="art_selected",
                            updated_at="2026-08-03T00:00:00+00:00",
                        ),
                    )
                    settings = SimpleNamespace(
                        designer_author_cmd="",
                        designer_author_harness="custom",
                        designer_author_timeout_s=5,
                    )
                    ctx = ToolContext(
                        settings=settings,
                        run_dir=run_dir,
                        layers_dir=run_dir / "layers",
                        run_id=run_dir.name,
                    )
                    adapter = adapter_type(settings, "")
                    with patch.object(
                        adapter,
                        "_fail",
                        side_effect=AssertionError("adapter continued after selection"),
                    ):
                        adapter.run("brief", ctx)
                    self.assertNotIn(failure_key, ctx.state)

    def test_two_processes_competing_for_promotion_invoke_promoter_once(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir, candidate = self._candidate(root)
            request_attempt_selection(
                run_dir=run_dir,
                run_id="run-1",
                attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
                idempotency_key="cross-process-promotion",
            )
            gate = root / "start-promotion"
            calls = root / "promotion-calls.log"
            worker_script = root / "promotion_worker.py"
            worker_script.write_text(
                textwrap.dedent(
                    f"""
                    import os
                    from pathlib import Path
                    import time
                    from types import SimpleNamespace

                    from autodesign.attempt_selection import promote_pending_selection
                    from autodesign.tools._contract import ToolContext

                    run_dir = Path({str(run_dir)!r})
                    gate = Path({str(gate)!r})
                    calls = Path({str(calls)!r})
                    while not gate.exists():
                        time.sleep(0.005)

                    def promote(_ctx, candidate):
                        descriptor = os.open(
                            calls,
                            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                            0o600,
                        )
                        try:
                            os.write(descriptor, (candidate.candidate_id + "\\n").encode())
                            os.fsync(descriptor)
                        finally:
                            os.close(descriptor)
                        time.sleep(0.25)

                    context = ToolContext(
                        settings=SimpleNamespace(),
                        run_dir=run_dir,
                        layers_dir=run_dir / "layers",
                        run_id=run_dir.name,
                    )
                    promote_pending_selection(context, promoter=promote)
                    """
                ),
                encoding="utf-8",
            )
            workers = [
                subprocess.Popen(
                    [sys.executable, str(worker_script)],
                    cwd=Path(__file__).resolve().parents[1],
                )
                for _ in range(2)
            ]
            try:
                gate.touch()
                for worker in workers:
                    worker.wait(timeout=5)
                    self.assertEqual(worker.returncode, 0)
            finally:
                for worker in workers:
                    if worker.poll() is None:
                        worker.kill()
                        worker.wait(timeout=3)

            recorded = calls.read_text(encoding="utf-8").splitlines()
            self.assertEqual(recorded, [candidate.candidate_id])
            self.assertEqual(load_selection_journal(run_dir).state, "complete")

    def test_promotion_failure_is_durable_and_does_not_resume_authoring(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir, candidate = self._candidate(Path(raw))
            request_attempt_selection(
                run_dir=run_dir,
                run_id="run-1",
                attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
                idempotency_key="choice-1",
            )
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id="run-1",
            )

            secret = "sk-selection-private-secret"
            private_path = "/Users/private-user/private-project/promotion.json"
            with patch("autodesign.attempt_selection.log") as log_call:
                outcome = promote_pending_selection(
                    ctx,
                    promoter=lambda _ctx, _candidate: (_ for _ in ()).throw(
                        RuntimeError(f"promotion broke: {secret} at {private_path}")
                    ),
                )

            self.assertEqual(outcome, "failed")
            journal = load_selection_journal(run_dir)
            self.assertEqual(journal.state, "failed")
            self.assertEqual(journal.error_code, "attempt_promotion_failed")
            self.assertEqual(
                journal.error_message,
                "The selected attempt could not be finalized.",
            )
            public_payload = json.dumps(journal.model_dump(mode="json"))
            logged_payload = repr(log_call.call_args_list)
            for forbidden in (secret, private_path, "private-user"):
                self.assertNotIn(forbidden, public_payload)
                self.assertNotIn(forbidden, logged_payload)
            self.assertFalse(selection_is_pending(run_dir))

            retried = request_attempt_selection(
                run_dir=run_dir,
                run_id="run-1",
                attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
                idempotency_key="choice-2",
            )
            self.assertEqual(retried.status, "selection_accepted")
            retry_journal = load_selection_journal(run_dir)
            self.assertEqual(retry_journal.state, "requested")
            self.assertEqual(retry_journal.idempotency_key, "choice-2")
            self.assertIsNone(retry_journal.error_code)
            self.assertIsNone(retry_journal.error_message)

    def test_committed_marker_failure_after_promoter_return_is_not_retryable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir, candidate = self._candidate(Path(raw))
            request_attempt_selection(
                run_dir=run_dir,
                run_id=run_dir.name,
                attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
                idempotency_key="marker-failure",
            )
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id=run_dir.name,
            )
            promoter_calls: list[str] = []
            original_write = (
                attempt_selection_module.write_selection_adapter_transaction
            )

            def fail_committed_marker(path: Path, payload: dict[str, object]) -> None:
                if payload.get("phase") == "committed":
                    raise OSError("simulated committed marker persistence failure")
                original_write(path, payload)

            with patch(
                "autodesign.attempt_selection.write_selection_adapter_transaction",
                side_effect=fail_committed_marker,
            ):
                first_outcome = promote_pending_selection(
                    ctx,
                    promoter=lambda _ctx, selected: promoter_calls.append(
                        selected.candidate_id
                    ),
                )

            retry = request_attempt_selection(
                run_dir=run_dir,
                run_id=run_dir.name,
                attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
                idempotency_key="marker-failure-retry",
            )
            second_outcome = promote_pending_selection(
                ctx,
                promoter=lambda _ctx, selected: promoter_calls.append(
                    selected.candidate_id
                ),
            )

            self.assertEqual(first_outcome, "in_progress")
            self.assertEqual(retry.status, "run_not_selectable")
            self.assertEqual(second_outcome, "in_progress")
            self.assertEqual(promoter_calls, [candidate.candidate_id])
            self.assertEqual(load_selection_journal(run_dir).state, "promoting")
            transaction = load_selection_adapter_transaction(run_dir)
            self.assertIsNotNone(transaction)
            self.assertEqual(transaction.get("phase"), "started")

    def test_normal_publish_and_selection_request_have_one_linearization_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir, candidate = self._candidate(root)
            publisher_entered = threading.Event()
            release_publisher = threading.Event()
            result_holder: list[object] = []

            def normal_publish() -> None:
                with attempt_selection_module.normal_promotion_lease(
                    run_dir=run_dir,
                    candidate_id=candidate.candidate_id,
                ):
                    publisher_entered.set()
                    self.assertTrue(release_publisher.wait(5))
                    final_dir = run_dir / "final"
                    final_dir.mkdir()
                    (final_dir / "index.html").write_text(
                        "normal-final",
                        encoding="utf-8",
                    )

            publisher = threading.Thread(target=normal_publish)
            publisher.start()
            self.assertTrue(publisher_entered.wait(5))

            selector = threading.Thread(
                target=lambda: result_holder.append(
                    request_attempt_selection(
                        run_dir=run_dir,
                        run_id=run_dir.name,
                        attempt=1,
                        expected_candidate_sha256=candidate.source_sha256,
                        idempotency_key="racing-selection",
                    )
                )
            )
            selector.start()
            time.sleep(0.1)
            self.assertTrue(selector.is_alive())
            self.assertIsNone(load_selection_journal(run_dir))
            release_publisher.set()
            publisher.join(timeout=5)
            selector.join(timeout=5)

            self.assertFalse(publisher.is_alive())
            self.assertFalse(selector.is_alive())
            self.assertEqual(result_holder[0].status, "run_not_selectable")
            self.assertEqual(
                (run_dir / "final" / "index.html").read_text(encoding="utf-8"),
                "normal-final",
            )
            self.assertIsNone(load_selection_journal(run_dir))

            second_run, second_candidate = self._candidate(root / "selection-first")
            accepted = request_attempt_selection(
                run_dir=second_run,
                run_id=second_run.name,
                attempt=1,
                expected_candidate_sha256=second_candidate.source_sha256,
                idempotency_key="selection-first",
            )
            self.assertEqual(accepted.status, "selection_accepted")
            with self.assertRaises(AttemptPromotionRejected):
                with attempt_selection_module.normal_promotion_lease(
                    run_dir=second_run,
                    candidate_id=second_candidate.candidate_id,
                ):
                    self.fail("normal publication began after selection committed")

    def test_normal_publication_blocks_selection_across_processes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir, candidate = self._candidate(root)
            entered = root / "publisher-entered"
            release = root / "release-publisher"
            worker_script = root / "normal_publisher.py"
            worker_script.write_text(
                textwrap.dedent(
                    f"""
                    from pathlib import Path
                    import time

                    from autodesign.attempt_selection import normal_promotion_lease

                    run_dir = Path({str(run_dir)!r})
                    entered = Path({str(entered)!r})
                    release = Path({str(release)!r})
                    with normal_promotion_lease(
                        run_dir=run_dir,
                        candidate_id={candidate.candidate_id!r},
                    ):
                        entered.touch()
                        while not release.exists():
                            time.sleep(0.005)
                        final_dir = run_dir / "final"
                        final_dir.mkdir()
                        (final_dir / "index.html").write_text(
                            "cross-process-final",
                            encoding="utf-8",
                        )
                    """
                ),
                encoding="utf-8",
            )
            worker = subprocess.Popen(
                [sys.executable, str(worker_script)],
                cwd=Path(__file__).resolve().parents[1],
            )
            selector_results: list[object] = []
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not entered.is_file():
                    time.sleep(0.01)
                self.assertTrue(entered.is_file())
                selector = threading.Thread(
                    target=lambda: selector_results.append(
                        request_attempt_selection(
                            run_dir=run_dir,
                            run_id=run_dir.name,
                            attempt=1,
                            expected_candidate_sha256=candidate.source_sha256,
                            idempotency_key="cross-process-race",
                        )
                    )
                )
                selector.start()
                time.sleep(0.1)
                self.assertTrue(selector.is_alive())
                release.touch()
                worker.wait(timeout=5)
                selector.join(timeout=5)
                self.assertFalse(selector.is_alive())
                self.assertEqual(worker.returncode, 0)
                self.assertEqual(selector_results[0].status, "run_not_selectable")
                self.assertIsNone(load_selection_journal(run_dir))
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait(timeout=3)

    def test_all_normal_adapter_publish_paths_use_linearization_lease(self) -> None:
        required = (
            (ExternalDesignerAuthor._promote_direct_final, "normal_promotion_lease"),
            (
                ExternalDesignerAuthor._promote_html_first_candidate_fallback,
                "normal_promotion_lease",
            ),
            (ExternalLandingAuthor._promote, "normal_promotion_lease"),
            (ExternalSlidesAuthor._promote, "normal_promotion_lease"),
            (ExternalVideoAuthor._deliver_normal_candidate, "normal_promotion_lease"),
        )
        for function, marker in required:
            with self.subTest(function=function.__qualname__):
                self.assertIn(marker, inspect.getsource(function))

    def test_all_normal_adapters_bind_identity_and_lease_before_mutation(self) -> None:
        class LeaseEntered(RuntimeError):
            pass

        @contextmanager
        def reject_at_lease(**_kwargs):
            raise LeaseEntered
            yield  # pragma: no cover

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "run-1"
            run_dir.mkdir()
            ctx = ToolContext(
                settings=SimpleNamespace(repo_root=root),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id="run-1",
            )
            before = tuple(run_dir.iterdir())
            cases = (
                (
                    external_designer_author_module,
                    lambda: ExternalDesignerAuthor(ctx.settings, "")._promote_direct_final(
                        ctx,
                        attempt_index=1,
                        attempt_dir=run_dir / "designer_author" / "attempt_01",
                        poster_path=(
                            run_dir / "designer_author" / "attempt_01" / "poster.html"
                        ),
                        poster_sha256="",
                    ),
                ),
                (
                    external_designer_author_module,
                    lambda: ExternalDesignerAuthor(
                        ctx.settings,
                        "",
                    )._promote_html_first_candidate_fallback(
                        ctx,
                        attempt_index=1,
                        attempt_dir=run_dir / "designer_author" / "attempt_01",
                        candidate={"candidate_id": "poster-fallback"},
                        acceptance={},
                        rejected_candidates=[],
                        source_reason="test",
                        source_message="test",
                        last_feedback=None,
                    ),
                ),
                (
                    external_landing_author_module,
                    lambda: ExternalLandingAuthor(ctx.settings, "")._promote(
                        ctx,
                        attempt_dir=run_dir / "landing_author" / "attempt_01",
                        diagnostics={},
                        candidate_id="landing",
                    ),
                ),
                (
                    external_slides_author_module,
                    lambda: ExternalSlidesAuthor(ctx.settings, "")._promote(
                        ctx,
                        attempt_dir=run_dir / "slides_author" / "attempt_01",
                        expected_slide_count=1,
                        validation={},
                        candidate_id="slides",
                    ),
                ),
                (
                    external_video_author_module,
                    lambda: ExternalVideoAuthor(ctx.settings, "")._deliver_normal_candidate(
                        candidate_id="video",
                        project_dir=run_dir / "video_author" / "attempt_01" / "project",
                        manifest={},
                        ctx=ctx,
                    ),
                ),
            )
            for module, invoke in cases:
                with self.subTest(module=module.__name__):
                    with patch.object(
                        module,
                        "normal_promotion_lease",
                        side_effect=reject_at_lease,
                    ) as acquire_lease:
                        with self.assertRaises(LeaseEntered):
                            invoke()
                        acquire_lease.assert_called_once()
                        self.assertEqual(
                            acquire_lease.call_args.kwargs[
                                "expected_run_identity"
                            ],
                            ctx.run_directory_identity,
                        )
                        self.assertEqual(tuple(run_dir.iterdir()), before)

    @unittest.skipUnless(os.name == "posix", "alias retarget uses POSIX symlinks")
    def test_all_normal_adapters_reject_preentry_run_retarget(self) -> None:
        cases = (
            "poster_direct",
            "poster_fallback",
            "landing",
            "slides",
            "video",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for case in cases:
                with self.subTest(case=case):
                    case_root = root / case
                    (
                        requested_run,
                        first_run,
                        second_run,
                        alias_parent,
                        second_parent,
                    ) = self._normal_alias_runs(case_root)
                    ctx = ToolContext(
                        settings=SimpleNamespace(repo_root=case_root),
                        run_dir=requested_run,
                        layers_dir=requested_run / "layers",
                        run_id="run-1",
                    )
                    first_attempt = requested_run / f"{case}_author" / "attempt_01"
                    first_attempt.mkdir(parents=True)
                    project_dir = first_attempt / "project"
                    project_dir.mkdir()
                    candidate = {
                        "candidate_id": "poster-fallback",
                        "_candidate_dir_abs": str(first_attempt),
                        "_measure_html_abs": str(first_attempt / "measure.html"),
                        "_preview_png_abs": "",
                    }
                    if case == "poster_direct":
                        invoke = lambda: ExternalDesignerAuthor(
                            ctx.settings, ""
                        )._promote_direct_final(
                            ctx,
                            attempt_index=1,
                            attempt_dir=first_attempt,
                            poster_path=first_attempt / "poster.html",
                            poster_sha256="",
                        )
                    elif case == "poster_fallback":
                        invoke = lambda: ExternalDesignerAuthor(
                            ctx.settings, ""
                        )._promote_html_first_candidate_fallback(
                            ctx,
                            attempt_index=1,
                            attempt_dir=first_attempt,
                            candidate=candidate,
                            acceptance={},
                            rejected_candidates=[],
                            source_reason="test",
                            source_message="test",
                            last_feedback=None,
                        )
                    elif case == "landing":
                        invoke = lambda: ExternalLandingAuthor(
                            ctx.settings, ""
                        )._promote(
                            ctx,
                            attempt_dir=first_attempt,
                            diagnostics={},
                            candidate_id="landing",
                        )
                    elif case == "slides":
                        invoke = lambda: ExternalSlidesAuthor(
                            ctx.settings, ""
                        )._promote(
                            ctx,
                            attempt_dir=first_attempt,
                            expected_slide_count=1,
                            validation={},
                            candidate_id="slides",
                        )
                    else:
                        invoke = lambda: ExternalVideoAuthor(
                            ctx.settings, ""
                        )._deliver_normal_candidate(
                            candidate_id="video",
                            project_dir=project_dir,
                            manifest={},
                            ctx=ctx,
                        )

                    alias_parent.unlink()
                    alias_parent.symlink_to(second_parent, target_is_directory=True)
                    with self.assertRaisesRegex(ValueError, "changed before lease entry"):
                        invoke()
                    self.assertFalse((first_run / "attempt_candidates").exists())
                    self.assertFalse((second_run / "attempt_candidates").exists())
                    self.assertEqual(
                        (first_run / "final" / "marker.txt").read_text(
                            encoding="utf-8"
                        ),
                        "A-old",
                    )
                    self.assertEqual(
                        (second_run / "final" / "marker.txt").read_text(
                            encoding="utf-8"
                        ),
                        "B-old",
                    )

    @unittest.skipUnless(os.name == "posix", "directory replacement uses POSIX rename")
    def test_stable_root_prevents_post_validation_staging_write_to_replacement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "run-1"
            replacement = root / "replacement"
            original = root / "original"
            run_dir.mkdir()
            replacement.mkdir()
            for directory, marker in ((run_dir, "A-old"), (replacement, "B-old")):
                final_dir = directory / "final"
                final_dir.mkdir()
                (final_dir / "marker.txt").write_text(marker, encoding="utf-8")
            attempt_dir = run_dir / "landing_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "index.html").write_text(
                "<!doctype html><main>A</main>",
                encoding="utf-8",
            )
            (attempt_dir / "designer_author_done.json").write_text(
                "{}",
                encoding="utf-8",
            )
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id="run-1",
            )

            def replace_between_fspath_and_mkdir(*, prefix, dir):
                stable_parent = Path(os.fspath(dir))
                run_dir.rename(original)
                replacement.rename(run_dir)
                target = stable_parent / f"{prefix}race"
                target.mkdir()
                return str(target)

            with patch.object(
                external_landing_author_module.tempfile,
                "mkdtemp",
                side_effect=replace_between_fspath_and_mkdir,
            ):
                with self.assertRaisesRegex(ValueError, "changed during"):
                    ExternalLandingAuthor(ctx.settings, "")._promote(
                        ctx,
                        attempt_dir=attempt_dir,
                        diagnostics={},
                        candidate_id="landing",
                    )

            self.assertEqual(
                (original / "final" / "marker.txt").read_text(encoding="utf-8"),
                "A-old",
            )
            self.assertEqual(
                (run_dir / "final" / "marker.txt").read_text(encoding="utf-8"),
                "B-old",
            )
            self.assertEqual(list(original.glob(".landing-final-staging-*")), [])
            self.assertEqual(list(run_dir.glob(".landing-final-staging-*")), [])

    @unittest.skipUnless(os.name == "posix", "directory replacement uses POSIX rename")
    def test_atomic_json_mkstemp_gap_writes_only_to_stable_run_inode(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "run-1"
            replacement = root / "replacement"
            original = root / "original"
            run_dir.mkdir()
            replacement.mkdir()
            expected_identity = attempt_candidates_module.promotion_run_identity(
                run_dir
            )
            real_mkstemp = tempfile.mkstemp
            swapped = False

            def swap_after_directory_conversion(*, prefix, dir):
                nonlocal swapped
                stable_parent = Path(os.fspath(dir))
                if not swapped:
                    run_dir.rename(original)
                    replacement.rename(run_dir)
                    swapped = True
                return real_mkstemp(prefix=prefix, dir=os.fspath(stable_parent))

            with patch.object(
                io_module.tempfile,
                "mkstemp",
                side_effect=swap_after_directory_conversion,
            ):
                with self.assertRaisesRegex(ValueError, "changed during"):
                    with attempt_candidates_module.attempt_promotion_lease(
                        run_dir,
                        expected_run_identity=expected_identity,
                    ) as leased_run_dir:
                        io_module.atomic_write_json(
                            leased_run_dir / "stable-write.json",
                            {"owner": "A"},
                        )
                        attempt_candidates_module.assert_promotion_run_unchanged()

            self.assertTrue(swapped)
            self.assertEqual(
                json.loads(
                    (original / "stable-write.json").read_text(encoding="utf-8")
                ),
                {"owner": "A"},
            )
            self.assertFalse((run_dir / "stable-write.json").exists())

    def test_guarded_resolve_returns_plain_path_inside_stable_run_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            dependency = run_dir / "landing_author" / "attempt_01" / "layers" / "figure.png"
            dependency.parent.mkdir(parents=True)
            dependency.write_bytes(b"figure")

            with attempt_candidates_module.attempt_promotion_lease(
                run_dir
            ) as leased_run_dir:
                resolved = (
                    leased_run_dir
                    / "landing_author"
                    / "attempt_01"
                    / "layers"
                    / "figure.png"
                ).resolve()
                self.assertIs(type(resolved), type(Path()))
                self.assertEqual(resolved.read_bytes(), b"figure")
                self.assertEqual(
                    resolved.relative_to(Path(os.fspath(leased_run_dir))).as_posix(),
                    "landing_author/attempt_01/layers/figure.png",
                )

    @unittest.skipUnless(os.name == "posix", "entry replacement requires POSIX rename")
    def test_promotion_browser_document_session_uses_synthetic_origin_and_retained_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            html_path = run_dir / "final" / "poster.html"
            html_path.parent.mkdir(parents=True)
            html_path.write_text(
                "<!doctype html><main>ORIGINAL_DOCUMENT_BYTES</main>",
                encoding="utf-8",
            )
            replacement_path = html_path.with_name("replacement.html")
            replacement_path.write_text(
                "<!doctype html><main>REPLACEMENT_DOCUMENT_BYTES</main>",
                encoding="utf-8",
            )

            session_factory = getattr(
                attempt_candidates_module,
                "promotion_browser_document_session",
                None,
            )
            self.assertTrue(
                callable(session_factory),
                "promotion rendering requires a lease-bound routed document session",
            )
            assert session_factory is not None

            with attempt_candidates_module.attempt_promotion_lease(
                run_dir
            ) as leased_run_dir:
                leased_html = leased_run_dir / "final" / "poster.html"
                with session_factory(leased_html) as document_session:
                    navigation = urlsplit(document_session.url)
                    self.assertEqual(navigation.scheme, "https")
                    self.assertTrue(
                        navigation.hostname and navigation.hostname.endswith(".invalid"),
                        document_session.url,
                    )
                    self.assertNotIn("file://", document_session.url)

                    detached_original = html_path.with_name("original-detached.html")
                    html_path.replace(detached_original)
                    replacement_path.replace(html_path)

                    from playwright.sync_api import sync_playwright

                    from autodesign.util.browser_render import _launch_chromium

                    with sync_playwright() as playwright:
                        browser = _launch_chromium(playwright)
                        page = browser.new_page()
                        document_session.install(page)
                        page.goto(document_session.url, wait_until="load")
                        rendered_text = page.locator("body").inner_text()
                        browser.close()

                    self.assertIn("ORIGINAL_DOCUMENT_BYTES", rendered_text)
                    self.assertNotIn("REPLACEMENT_DOCUMENT_BYTES", rendered_text)

    def test_promotion_browser_document_session_rejects_mutated_allowlisted_dependency(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            attempt_dir = run_dir / "landing_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "index.html").write_text(
                '<!doctype html><img src="allowed.png">',
                encoding="utf-8",
            )
            (attempt_dir / "allowed.png").write_bytes(b"captured-image-bytes")
            (attempt_dir / "preview.png").write_bytes(b"preview")
            (attempt_dir / "validation.json").write_text("{}", encoding="utf-8")
            candidate = capture_attempt_candidate(
                run_dir=run_dir,
                attempt_dir=attempt_dir,
                artifact_type="landing",
                attempt=1,
                max_attempts=1,
                source_path="index.html",
                dependency_paths=["allowed.png"],
                browser_resource_paths=["allowed.png"],
                preview_paths=["preview.png"],
                validation_summary_path="validation.json",
                safety_state="ready",
                hard_blockers=[],
                warnings=[],
            )
            allowed_relative = candidate.browser_resource_relative_paths
            assert allowed_relative is not None
            (run_dir / allowed_relative[0]).write_bytes(b"mutated-image-bytes")

            with attempt_candidates_module.attempt_promotion_lease(
                run_dir
            ) as leased_run_dir:
                with self.assertRaisesRegex(ValueError, "dependency fingerprint"):
                    with attempt_candidates_module.promotion_browser_document_session(
                        leased_run_dir / candidate.source_relative_path
                    ):
                        pass

    def test_step3a_promotion_browser_production_limits_are_bounded(self) -> None:
        expected_limits = {
            "_PROMOTION_BROWSER_MAX_DEPENDENCIES": 512,
            "_PROMOTION_BROWSER_MAX_RESOURCES": 512,
            "_PROMOTION_BROWSER_MAX_DEPENDENCY_BYTES": 64 * 1024**2,
            "_PROMOTION_BROWSER_MAX_TOTAL_BYTES": 256 * 1024**2,
            "_PROMOTION_BROWSER_MAX_DOCUMENT_BYTES": 32 * 1024**2,
            "_PROMOTION_BROWSER_MAX_MANIFEST_BYTES": 4 * 1024**2,
        }
        for name, expected in expected_limits.items():
            with self.subTest(limit=name):
                self.assertEqual(
                    getattr(attempt_candidates_module, name, None),
                    expected,
                )

    @unittest.skipUnless(os.name == "posix", "stream injection uses os.read")
    def test_step3a_secure_member_max_bytes_is_streaming_and_optional(self) -> None:
        supports_max_bytes = "max_bytes" in inspect.signature(
            attempt_candidates_module.SecureRunMemberAccessor.read_bytes
        ).parameters
        with self.subTest(contract="optional max_bytes"):
            self.assertTrue(
                supports_max_bytes,
                "secure reads must expose an optional streaming byte cap",
            )
        if not supports_max_bytes:
            return

        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            run_dir.mkdir()
            payload_path = run_dir / "payload.bin"
            payload_path.write_bytes(b"12345678")
            with attempt_candidates_module.secure_run_member_access(
                run_dir
            ) as accessor:
                exact = accessor.read_bytes(
                    payload_path,
                    label="exact capped member",
                    max_bytes=8,
                )
                self.assertEqual(exact.data, b"12345678")

                injected_chunks = [b"12345678", b"9"]
                injected_read_calls: list[int] = []

                def injected_read(_descriptor: int, _size: int) -> bytes:
                    call_index = len(injected_read_calls)
                    injected_read_calls.append(_size)
                    if call_index >= len(injected_chunks):
                        raise AssertionError(
                            "capped read continued after the one-over byte"
                        )
                    return injected_chunks[call_index]

                with (
                    patch.object(
                        attempt_candidates_module.os,
                        "read",
                        side_effect=injected_read,
                    ),
                    self.assertRaisesRegex(ValueError, "bytes|limit|max"),
                ):
                    accessor.read_bytes(
                        payload_path,
                        label="streamed one-over member",
                        max_bytes=8,
                    )
                self.assertEqual(len(injected_read_calls), 2)

                ordinary = accessor.read_bytes(
                    payload_path,
                    label="ordinary uncapped member",
                )
                self.assertEqual(ordinary.data, b"12345678")

    def test_step3a_promotion_browser_document_and_manifest_size_boundaries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            exact_run, exact_candidate, _ = self._step3a_browser_candidate(
                root / "document-exact",
                document_bytes=b"12345678",
                browser_resources=[],
            )
            over_run, over_candidate, _ = self._step3a_browser_candidate(
                root / "document-over",
                document_bytes=b"123456789",
                browser_resources=[],
            )
            with (
                self._step3a_capture_secure_read_caps() as document_read_caps,
                patch.multiple(
                    attempt_candidates_module,
                    _PROMOTION_BROWSER_MAX_DOCUMENT_BYTES=8,
                    _PROMOTION_BROWSER_MAX_MANIFEST_BYTES=1 << 20,
                    _PROMOTION_BROWSER_MAX_DEPENDENCY_BYTES=1 << 20,
                    _PROMOTION_BROWSER_MAX_TOTAL_BYTES=1 << 20,
                    _PROMOTION_BROWSER_MAX_DEPENDENCIES=8,
                    _PROMOTION_BROWSER_MAX_RESOURCES=8,
                    create=True,
                ),
            ):
                with attempt_candidates_module.attempt_promotion_lease(
                    exact_run
                ) as leased_run_dir:
                    with self._step3a_session_factory()(
                        leased_run_dir / exact_candidate.source_relative_path
                    ):
                        pass
                with self.subTest(boundary="document one over"):
                    self._step3a_assert_session_build_rejected(
                        over_run,
                        over_candidate.source_relative_path,
                        message="one-over browser document was accepted",
                    )
            with self.subTest(contract="document uses streaming cap"):
                self.assertEqual(
                    [
                        cap
                        for path, cap in document_read_caps
                        if path.endswith(exact_candidate.source_relative_path)
                    ],
                    [8, 8],
                )

            manifest_run, manifest_candidate, manifest_path = (
                self._step3a_browser_candidate(
                    root / "manifest",
                    browser_resources=[],
                )
            )
            manifest_limit = manifest_path.stat().st_size
            manifest_relative = manifest_path.relative_to(manifest_run).as_posix()
            with (
                self._step3a_capture_secure_read_caps() as manifest_read_caps,
                patch.multiple(
                    attempt_candidates_module,
                    _PROMOTION_BROWSER_MAX_DOCUMENT_BYTES=1 << 20,
                    _PROMOTION_BROWSER_MAX_MANIFEST_BYTES=manifest_limit,
                    _PROMOTION_BROWSER_MAX_DEPENDENCY_BYTES=1 << 20,
                    _PROMOTION_BROWSER_MAX_TOTAL_BYTES=1 << 20,
                    _PROMOTION_BROWSER_MAX_DEPENDENCIES=8,
                    _PROMOTION_BROWSER_MAX_RESOURCES=8,
                    create=True,
                ),
            ):
                with attempt_candidates_module.attempt_promotion_lease(
                    manifest_run
                ) as leased_run_dir:
                    with self._step3a_session_factory()(
                        leased_run_dir / manifest_candidate.source_relative_path
                    ):
                        pass
                manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
                with self.subTest(boundary="manifest one over"):
                    self._step3a_assert_session_build_rejected(
                        manifest_run,
                        manifest_candidate.source_relative_path,
                        message="one-over candidate manifest was accepted",
                    )
            with self.subTest(contract="manifest uses streaming cap"):
                self.assertEqual(
                    [
                        cap
                        for path, cap in manifest_read_caps
                        if path.endswith(manifest_relative)
                    ],
                    [manifest_limit, manifest_limit],
                )

    def test_step3a_promotion_browser_dependency_file_and_aggregate_size_boundaries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            exact_run, exact_candidate, _ = self._step3a_browser_candidate(
                root / "dependency-exact",
                dependencies={"assets/payload.png": b"12345678"},
                browser_resources=[],
            )
            over_run, over_candidate, _ = self._step3a_browser_candidate(
                root / "dependency-over",
                dependencies={"assets/payload.png": b"123456789"},
                browser_resources=[],
            )
            with (
                self._step3a_capture_secure_read_caps() as dependency_read_caps,
                patch.multiple(
                    attempt_candidates_module,
                    _PROMOTION_BROWSER_MAX_DOCUMENT_BYTES=1 << 20,
                    _PROMOTION_BROWSER_MAX_MANIFEST_BYTES=1 << 20,
                    _PROMOTION_BROWSER_MAX_DEPENDENCY_BYTES=8,
                    _PROMOTION_BROWSER_MAX_TOTAL_BYTES=1 << 20,
                    _PROMOTION_BROWSER_MAX_DEPENDENCIES=8,
                    _PROMOTION_BROWSER_MAX_RESOURCES=8,
                    create=True,
                ),
            ):
                with attempt_candidates_module.attempt_promotion_lease(
                    exact_run
                ) as leased_run_dir:
                    with self._step3a_session_factory()(
                        leased_run_dir / exact_candidate.source_relative_path
                    ):
                        pass
                with self.subTest(boundary="dependency one over"):
                    self._step3a_assert_session_build_rejected(
                        over_run,
                        over_candidate.source_relative_path,
                        message="one-over fingerprint dependency was accepted",
                    )
            with self.subTest(contract="dependency uses streaming cap"):
                self.assertEqual(
                    [
                        cap
                        for path, cap in dependency_read_caps
                        if path.endswith("assets/payload.png")
                    ],
                    [8, 8],
                )

            aggregate_run, aggregate_candidate, aggregate_manifest = (
                self._step3a_browser_candidate(
                    root / "aggregate",
                    document_bytes=b"1234",
                    dependencies={"same.png": b"5678"},
                    browser_resources=[],
                )
            )
            aggregate_path = aggregate_candidate.dependency_relative_paths[0]
            aggregate_snapshot_relative = Path(aggregate_path).relative_to(
                Path(aggregate_candidate.source_relative_path).parent
            ).as_posix()
            aggregate_dependency_sha256 = hashlib.sha256(b"5678").hexdigest()

            def repeated_aggregate_fingerprint(count: int) -> str:
                digest = hashlib.sha256()
                for _ in range(count):
                    digest.update(aggregate_snapshot_relative.encode("utf-8"))
                    digest.update(b"\0")
                    digest.update(aggregate_dependency_sha256.encode("ascii"))
                    digest.update(b"\0")
                return digest.hexdigest()

            aggregate_payload = json.loads(
                aggregate_manifest.read_text(encoding="utf-8")
            )
            aggregate_payload["dependency_relative_paths"] = [aggregate_path] * 2
            aggregate_payload["dependency_fingerprint"] = (
                repeated_aggregate_fingerprint(2)
            )
            aggregate_manifest.write_text(
                json.dumps(aggregate_payload),
                encoding="utf-8",
            )
            with (
                self._step3a_capture_secure_read_caps() as aggregate_read_caps,
                patch.multiple(
                    attempt_candidates_module,
                    _PROMOTION_BROWSER_MAX_DOCUMENT_BYTES=16,
                    _PROMOTION_BROWSER_MAX_MANIFEST_BYTES=1 << 20,
                    _PROMOTION_BROWSER_MAX_DEPENDENCY_BYTES=16,
                    _PROMOTION_BROWSER_MAX_TOTAL_BYTES=12,
                    _PROMOTION_BROWSER_MAX_DEPENDENCIES=8,
                    _PROMOTION_BROWSER_MAX_RESOURCES=8,
                    create=True,
                ),
            ):
                with attempt_candidates_module.attempt_promotion_lease(
                    aggregate_run
                ) as leased_run_dir:
                    with self._step3a_session_factory()(
                        leased_run_dir / aggregate_candidate.source_relative_path
                    ):
                        pass
                aggregate_payload["dependency_relative_paths"] = [
                    aggregate_path
                ] * 3
                aggregate_payload["dependency_fingerprint"] = (
                    repeated_aggregate_fingerprint(3)
                )
                aggregate_manifest.write_text(
                    json.dumps(aggregate_payload),
                    encoding="utf-8",
                )
                with self.subTest(boundary="aggregate one over"):
                    self._step3a_assert_session_build_rejected(
                        aggregate_run,
                        aggregate_candidate.source_relative_path,
                        message="one-over repeated dependency aggregate was accepted",
                    )
            with self.subTest(contract="aggregate uses remaining streaming cap"):
                self.assertEqual(
                    [
                        cap
                        for path, cap in aggregate_read_caps
                        if path.endswith(aggregate_candidate.source_relative_path)
                    ],
                    [12, 12],
                )
                same_dependency_caps = [
                    cap
                    for path, cap in aggregate_read_caps
                    if path.endswith("same.png")
                ]
                self.assertEqual(same_dependency_caps, [8, 8])

    def test_step3a_promotion_browser_dependency_and_resource_count_boundaries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            dependency_run, dependency_candidate, dependency_manifest = (
                self._step3a_browser_candidate(
                    root / "dependencies",
                    dependencies={"same.png": b"same"},
                    browser_resources=[],
                )
            )
            dependency_path = dependency_candidate.dependency_relative_paths[0]
            snapshot_relative = Path(dependency_path).relative_to(
                Path(dependency_candidate.source_relative_path).parent
            ).as_posix()
            dependency_sha256 = hashlib.sha256(b"same").hexdigest()

            def repeated_dependency_fingerprint(count: int) -> str:
                digest = hashlib.sha256()
                for _ in range(count):
                    digest.update(snapshot_relative.encode("utf-8"))
                    digest.update(b"\0")
                    digest.update(dependency_sha256.encode("ascii"))
                    digest.update(b"\0")
                return digest.hexdigest()

            dependency_payload = json.loads(
                dependency_manifest.read_text(encoding="utf-8")
            )
            dependency_payload["dependency_relative_paths"] = [dependency_path] * 2
            dependency_payload["dependency_fingerprint"] = (
                repeated_dependency_fingerprint(2)
            )
            dependency_manifest.write_text(
                json.dumps(dependency_payload),
                encoding="utf-8",
            )
            with patch.multiple(
                attempt_candidates_module,
                _PROMOTION_BROWSER_MAX_DOCUMENT_BYTES=1 << 20,
                _PROMOTION_BROWSER_MAX_MANIFEST_BYTES=1 << 20,
                _PROMOTION_BROWSER_MAX_DEPENDENCY_BYTES=1 << 20,
                _PROMOTION_BROWSER_MAX_TOTAL_BYTES=1 << 20,
                _PROMOTION_BROWSER_MAX_DEPENDENCIES=2,
                _PROMOTION_BROWSER_MAX_RESOURCES=8,
                create=True,
            ):
                with attempt_candidates_module.attempt_promotion_lease(
                    dependency_run
                ) as leased_run_dir:
                    with self._step3a_session_factory()(
                        leased_run_dir / dependency_candidate.source_relative_path
                    ):
                        pass
                dependency_payload["dependency_relative_paths"] = [
                    dependency_path
                ] * 3
                dependency_payload["dependency_fingerprint"] = (
                    repeated_dependency_fingerprint(3)
                )
                dependency_manifest.write_text(
                    json.dumps(dependency_payload),
                    encoding="utf-8",
                )
                with self.subTest(boundary="duplicate dependency count one over"):
                    self._step3a_assert_session_build_rejected(
                        dependency_run,
                        dependency_candidate.source_relative_path,
                        message="one-over pre-dedup dependency count was accepted",
                    )

            resource_run, resource_candidate, manifest_path = (
                self._step3a_browser_candidate(
                    root / "resources",
                    dependencies={"same.png": b"same"},
                )
            )
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            resource_path = resource_candidate.dependency_relative_paths[0]
            payload["browser_resource_relative_paths"] = [resource_path] * 2
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.multiple(
                attempt_candidates_module,
                _PROMOTION_BROWSER_MAX_DOCUMENT_BYTES=1 << 20,
                _PROMOTION_BROWSER_MAX_MANIFEST_BYTES=1 << 20,
                _PROMOTION_BROWSER_MAX_DEPENDENCY_BYTES=1 << 20,
                _PROMOTION_BROWSER_MAX_TOTAL_BYTES=1 << 20,
                _PROMOTION_BROWSER_MAX_DEPENDENCIES=8,
                _PROMOTION_BROWSER_MAX_RESOURCES=2,
                create=True,
            ):
                with attempt_candidates_module.attempt_promotion_lease(
                    resource_run
                ) as leased_run_dir:
                    with self._step3a_session_factory()(
                        leased_run_dir / resource_candidate.source_relative_path
                    ):
                        pass
                payload["browser_resource_relative_paths"] = [resource_path] * 3
                manifest_path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(boundary="resource count one over before dedup"):
                    self._step3a_assert_session_build_rejected(
                        resource_run,
                        resource_candidate.source_relative_path,
                        message="one-over pre-dedup browser resource count was accepted",
                    )

    @unittest.skipUnless(os.name == "posix", "entry swaps use POSIX rename")
    def test_step3a_explicit_resources_are_one_accessor_snapshot_and_route_never_rereads(
        self,
    ) -> None:
        originals = {
            "assets/site.css": b"body{color:#123456}",
            "assets/pixel.png": b"original-png",
            "assets/site.woff2": b"original-woff2",
        }
        replacement_payloads = {
            key: b"replacement-" + value for key, value in originals.items()
        }
        expected_mime = {
            "assets/site.css": "text/css",
            "assets/pixel.png": "image/png",
            "assets/site.woff2": "font/woff2",
        }
        with tempfile.TemporaryDirectory() as raw:
            run_dir, candidate, _ = self._step3a_browser_candidate(
                Path(raw),
                dependencies=originals,
            )
            original_by_relative = dict(
                zip(candidate.browser_resource_relative_paths or (), originals.values())
            )
            replacement_by_relative = dict(
                zip(
                    candidate.browser_resource_relative_paths or (),
                    replacement_payloads.values(),
                )
            )
            mime_by_relative = dict(
                zip(candidate.browser_resource_relative_paths or (), expected_mime.values())
            )
            for relative, payload in replacement_by_relative.items():
                target = run_dir / relative
                target.with_name(f"{target.name}.replacement").write_bytes(payload)

            swapped: set[str] = set()
            old_reads: dict[str, int] = {}
            accessor_reads: dict[str, int] = {}
            accessor_read_caps: dict[str, list[object]] = {}
            all_accessor_read_caps: dict[str, list[object]] = {}
            accessor_digests: dict[str, int] = {}
            accessor_entries: list[int] = []
            accessor_exits: list[int] = []
            real_old_reader = getattr(
                attempt_candidates_module,
                "_read_promotion_browser_member",
                None,
            )
            real_accessor_read = (
                attempt_candidates_module.SecureRunMemberAccessor.read_bytes
            )
            real_accessor_digest = (
                attempt_candidates_module.SecureRunMemberAccessor.digest
            )
            real_secure_access = attempt_candidates_module.secure_run_member_access

            def swap_after_first_read(relative: str) -> None:
                if relative not in replacement_by_relative or relative in swapped:
                    return
                target = run_dir / relative
                detached = target.with_name(f"{target.name}.detached")
                replacement = target.with_name(f"{target.name}.replacement")
                target.replace(detached)
                replacement.replace(target)
                swapped.add(relative)

            def tracked_old_reader(binding, relative):
                payload = real_old_reader(binding, relative)
                relative_value = relative.as_posix()
                if relative_value in original_by_relative:
                    old_reads[relative_value] = old_reads.get(relative_value, 0) + 1
                    swap_after_first_read(relative_value)
                return payload

            def tracked_accessor_read(accessor, value, **kwargs):
                snapshot = real_accessor_read(accessor, value, **kwargs)
                relative_value = snapshot.relative_path.as_posix()
                all_accessor_read_caps.setdefault(relative_value, []).append(
                    kwargs.get("max_bytes")
                )
                if relative_value in original_by_relative:
                    accessor_reads[relative_value] = (
                        accessor_reads.get(relative_value, 0) + 1
                    )
                    accessor_read_caps.setdefault(relative_value, []).append(
                        kwargs.get("max_bytes")
                    )
                    swap_after_first_read(relative_value)
                return snapshot

            def tracked_accessor_digest(accessor, value, **kwargs):
                snapshot = real_accessor_digest(accessor, value, **kwargs)
                relative_value = snapshot.relative_path.as_posix()
                if relative_value in original_by_relative:
                    accessor_digests[relative_value] = (
                        accessor_digests.get(relative_value, 0) + 1
                    )
                    swap_after_first_read(relative_value)
                return snapshot

            @contextmanager
            def tracked_secure_access(path):
                accessor_id: int | None = None
                try:
                    with real_secure_access(path) as accessor:
                        accessor_id = id(accessor)
                        accessor_entries.append(accessor_id)
                        yield accessor
                finally:
                    if accessor_id is not None:
                        accessor_exits.append(accessor_id)

            old_reader_patch = (
                patch.object(
                    attempt_candidates_module,
                    "_read_promotion_browser_member",
                    side_effect=tracked_old_reader,
                )
                if callable(real_old_reader)
                else nullcontext()
            )
            responses: dict[str, _Step3aRoute] = {}
            reads_before_routes: dict[str, int] = {}
            reads_after_routes: dict[str, int] = {}
            accessor_exits_during_session: list[int] = []
            with (
                patch.object(
                    attempt_candidates_module,
                    "secure_run_member_access",
                    side_effect=tracked_secure_access,
                ),
                patch.object(
                    attempt_candidates_module.SecureRunMemberAccessor,
                    "read_bytes",
                    new=tracked_accessor_read,
                ),
                patch.object(
                    attempt_candidates_module.SecureRunMemberAccessor,
                    "digest",
                    new=tracked_accessor_digest,
                ),
                old_reader_patch,
                attempt_candidates_module.attempt_promotion_lease(
                    run_dir
                ) as leased_run_dir,
            ):
                with self._step3a_session_factory()(
                    leased_run_dir / candidate.source_relative_path
                ) as session:
                    context = _Step3aBrowserContext()
                    session.install(SimpleNamespace(context=context))
                    reads_before_routes = {
                        relative: old_reads.get(relative, 0)
                        + accessor_reads.get(relative, 0)
                        + accessor_digests.get(relative, 0)
                        for relative in original_by_relative
                    }
                    for relative in original_by_relative:
                        responses[relative] = self._step3a_route_member(
                            session,
                            context,
                            relative,
                        )
                    reads_after_routes = {
                        relative: old_reads.get(relative, 0)
                        + accessor_reads.get(relative, 0)
                        + accessor_digests.get(relative, 0)
                        for relative in original_by_relative
                    }
                    accessor_exits_during_session = list(accessor_exits)

                with self.subTest(contract="one accessor retained for session"):
                    self.assertEqual(len(set(accessor_entries)), 1)
                    self.assertEqual(accessor_exits_during_session, [])
                    self.assertEqual(accessor_exits, accessor_entries)

            for relative, original in original_by_relative.items():
                with self.subTest(resource=relative, contract="immutable bytes and MIME"):
                    self.assertEqual(len(responses[relative].fulfill_calls), 1)
                    response = responses[relative].fulfill_calls[0]
                    self.assertEqual(response.get("body"), original)
                    self.assertEqual(
                        response.get("content_type"),
                        mime_by_relative[relative],
                    )
                with self.subTest(resource=relative, contract="one capped read"):
                    self.assertEqual(old_reads.get(relative, 0), 0)
                    self.assertEqual(accessor_digests.get(relative, 0), 0)
                    self.assertEqual(accessor_reads.get(relative, 0), 1)
                    self.assertEqual(
                        accessor_read_caps.get(relative),
                        [64 * 1024**2],
                    )
                    self.assertEqual(reads_before_routes.get(relative), 1)
                    self.assertEqual(reads_after_routes, reads_before_routes)
            with self.subTest(contract="document and manifest use production caps"):
                self.assertEqual(
                    all_accessor_read_caps.get(candidate.source_relative_path),
                    [32 * 1024**2],
                )
                self.assertEqual(
                    all_accessor_read_caps.get(
                        "landing_author/attempt_01/attempt_candidate.json"
                    ),
                    [4 * 1024**2],
                )

    def test_step3a_browser_resource_policy_distinguishes_explicit_empty_and_legacy(
        self,
    ) -> None:
        scenarios = (
            ("explicit-empty", "empty", False),
            ("explicit-null", "null", True),
            ("omitted-field", "omitted", True),
            ("missing-manifest", "missing", True),
        )
        for directory, manifest_mode, allows_static in scenarios:
            with self.subTest(policy=manifest_mode):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw) / directory
                    resources = {
                        "assets/site.css": b"css",
                        "assets/pixel.png": b"png",
                        "assets/site.woff2": b"woff2",
                    }
                    run_dir, candidate, manifest_path = (
                        self._step3a_browser_candidate(
                            root,
                            dependencies=resources,
                            browser_resources=[],
                        )
                    )
                    if manifest_mode in {"null", "omitted"}:
                        payload = json.loads(
                            manifest_path.read_text(encoding="utf-8")
                        )
                        if manifest_mode == "null":
                            payload["browser_resource_relative_paths"] = None
                        else:
                            payload.pop("browser_resource_relative_paths")
                        manifest_path.write_text(
                            json.dumps(payload),
                            encoding="utf-8",
                        )
                    elif manifest_mode == "missing":
                        manifest_path.unlink()

                    snapshot_root = (
                        run_dir / candidate.source_relative_path
                    ).parent
                    blocked_members = {
                        snapshot_root / "blocked.html": b"html",
                        snapshot_root / "blocked.json": b"json",
                        snapshot_root / "blocked.log": b"log",
                        run_dir / "attempt_candidates" / "secret.css": b"secret",
                    }
                    for path, body in blocked_members.items():
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(body)

                    with attempt_candidates_module.attempt_promotion_lease(
                        run_dir
                    ) as leased_run_dir:
                        with self._step3a_session_factory()(
                            leased_run_dir / candidate.source_relative_path
                        ) as session:
                            context = _Step3aBrowserContext()
                            session.install(SimpleNamespace(context=context))
                            for relative in candidate.dependency_relative_paths:
                                route = self._step3a_route_member(
                                    session,
                                    context,
                                    relative,
                                )
                                if allows_static:
                                    self.assertEqual(len(route.fulfill_calls), 1)
                                    self.assertEqual(route.abort_calls, [])
                                else:
                                    self.assertEqual(route.fulfill_calls, [])
                                    self.assertEqual(
                                        route.abort_calls,
                                        ["blockedbyclient"],
                                    )
                            blocked_relatives = [
                                path.relative_to(run_dir).as_posix()
                                for path in blocked_members
                            ]
                            for relative in blocked_relatives:
                                route = self._step3a_route_member(
                                    session,
                                    context,
                                    relative,
                                )
                                self.assertEqual(route.fulfill_calls, [])
                                self.assertEqual(
                                    route.abort_calls,
                                    ["blockedbyclient"],
                                )

    def test_step3a_snapshot_failure_exits_accessor_before_install(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir, candidate, _ = self._step3a_browser_candidate(
                Path(raw),
                dependencies={
                    "first.png": b"12345678",
                    "second.png": b"123456789",
                },
            )
            real_secure_access = attempt_candidates_module.secure_run_member_access
            entries: list[int] = []
            exits: list[int] = []

            @contextmanager
            def tracked_secure_access(path):
                accessor_id: int | None = None
                try:
                    with real_secure_access(path) as accessor:
                        accessor_id = id(accessor)
                        entries.append(accessor_id)
                        yield accessor
                finally:
                    if accessor_id is not None:
                        exits.append(accessor_id)

            built_session = False
            caught: BaseException | None = None
            with (
                patch.multiple(
                    attempt_candidates_module,
                    _PROMOTION_BROWSER_MAX_DOCUMENT_BYTES=1 << 20,
                    _PROMOTION_BROWSER_MAX_MANIFEST_BYTES=1 << 20,
                    _PROMOTION_BROWSER_MAX_DEPENDENCY_BYTES=8,
                    _PROMOTION_BROWSER_MAX_TOTAL_BYTES=1 << 20,
                    _PROMOTION_BROWSER_MAX_DEPENDENCIES=8,
                    _PROMOTION_BROWSER_MAX_RESOURCES=8,
                    create=True,
                ),
                patch.object(
                    attempt_candidates_module,
                    "secure_run_member_access",
                    side_effect=tracked_secure_access,
                ),
            ):
                try:
                    with attempt_candidates_module.attempt_promotion_lease(
                        run_dir
                    ) as leased_run_dir:
                        with self._step3a_session_factory()(
                            leased_run_dir / candidate.source_relative_path
                        ):
                            built_session = True
                except (OSError, RuntimeError, ValueError) as exc:
                    caught = exc

            with self.subTest(contract="failure occurs before install"):
                self.assertIsNotNone(caught)
                self.assertFalse(built_session)
            with self.subTest(contract="no partial accessor snapshot survives"):
                self.assertEqual(len(entries), 1)
                self.assertEqual(exits, entries)

    def test_step3a_session_unroutes_exact_handlers_before_final_lease_assert(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            document = run_dir / "final" / "poster.html"
            document.parent.mkdir(parents=True)
            document.write_text("<!doctype html><main>poster</main>", encoding="utf-8")
            (document.parent / "style.css").write_bytes(b"retained-css")
            cleanup_started = threading.Event()
            premature_final_asserts: list[str] = []
            synchronous_routes: list[_Step3aRoute] = []
            synchronous_handler_blocked: list[bool] = []
            exiting_session = False

            with attempt_candidates_module.attempt_promotion_lease(
                run_dir
            ) as leased_run_dir:
                binding = attempt_candidates_module._ACTIVE_PROMOTION_LEASE.get()
                self.assertIsNotNone(binding)
                assert binding is not None
                real_assert_unchanged = binding.assert_active_and_unchanged

                def tracked_assert_unchanged() -> None:
                    if exiting_session and not cleanup_started.is_set():
                        premature_final_asserts.append("assert-before-close")
                    real_assert_unchanged()

                def invoke_during_unroute(handler) -> None:
                    cleanup_started.set()
                    route = _Step3aRoute(
                        "https://autodesign.invalid/final/poster.html"
                    )
                    synchronous_routes.append(route)

                    callback = threading.Thread(
                        target=handler,
                        args=(route,),
                        daemon=True,
                    )
                    callback.start()
                    callback.join(0.25)
                    synchronous_handler_blocked.append(callback.is_alive())

                context = _Step3aBrowserContext(
                    on_http_unroute=invoke_during_unroute
                )
                unrelated_handler = lambda _route: None
                context.active_http_routes.append(("unrelated", unrelated_handler))
                with patch.object(
                    binding,
                    "assert_active_and_unchanged",
                    side_effect=tracked_assert_unchanged,
                ):
                    with self._step3a_session_factory()(
                        leased_run_dir / "final" / "poster.html"
                    ) as session:
                        session.install(SimpleNamespace(context=context))
                        http_handler = context.http_routes[-1][1]
                        websocket_handler = context.websocket_routes[-1][1]
                        close = getattr(session, "close", None)
                        with self.subTest(contract="bounded explicit close"):
                            self.assertTrue(callable(close))
                        close_errors: list[BaseException] = []
                        close_thread = None
                        if callable(close):
                            def invoke_close() -> None:
                                try:
                                    close()
                                except BaseException as exc:
                                    close_errors.append(exc)

                            close_thread = threading.Thread(
                                target=invoke_close,
                                daemon=True,
                            )
                            close_thread.start()
                            close_thread.join(2)
                        exiting_session = True

                closed_route = _Step3aRoute(
                    "https://autodesign.invalid/final/style.css"
                )
                old_read_calls: list[str] = []
                accessor_read_calls: list[str] = []
                real_old_reader = getattr(
                    attempt_candidates_module,
                    "_read_promotion_browser_member",
                    None,
                )
                real_accessor_read = (
                    attempt_candidates_module.SecureRunMemberAccessor.read_bytes
                )

                def tracked_old_reader(binding, relative):
                    old_read_calls.append(relative.as_posix())
                    return real_old_reader(binding, relative)

                def tracked_accessor_read(accessor, value, **kwargs):
                    accessor_read_calls.append(os.fspath(value))
                    return real_accessor_read(accessor, value, **kwargs)

                old_reader_patch = (
                    patch.object(
                        attempt_candidates_module,
                        "_read_promotion_browser_member",
                        side_effect=tracked_old_reader,
                    )
                    if callable(real_old_reader)
                    else nullcontext()
                )
                with (
                    old_reader_patch,
                    patch.object(
                        attempt_candidates_module.SecureRunMemberAccessor,
                        "read_bytes",
                        new=tracked_accessor_read,
                    ),
                ):
                    http_handler(closed_route)

                with self.subTest(contract="exact route identities are removed"):
                    self.assertEqual(
                        context.http_unroutes,
                        [("**/*", http_handler)],
                    )
                    self.assertEqual(
                        context.websocket_unroutes,
                        [("**/*", websocket_handler)],
                    )
                    self.assertEqual(
                        context.active_http_routes,
                        [("unrelated", unrelated_handler)],
                    )
                with self.subTest(contract="session invalidates before unroute callback"):
                    self.assertTrue(cleanup_started.is_set())
                    self.assertEqual(len(synchronous_routes), 1)
                    if synchronous_routes:
                        self.assertEqual(
                            synchronous_routes[0].abort_calls,
                            ["blockedbyclient"],
                        )
                        self.assertEqual(synchronous_routes[0].fulfill_calls, [])
                    self.assertEqual(synchronous_handler_blocked, [False])
                    if close_thread is not None:
                        self.assertFalse(close_thread.is_alive())
                    self.assertEqual(close_errors, [])
                with self.subTest(contract="close precedes final lease assertion"):
                    self.assertEqual(premature_final_asserts, [])
                with self.subTest(contract="captured handler stays closed"):
                    self.assertEqual(closed_route.abort_calls, ["blockedbyclient"])
                    self.assertEqual(closed_route.fulfill_calls, [])
                    self.assertEqual(old_read_calls, [])
                    self.assertEqual(accessor_read_calls, [])

    def test_step3a_session_handles_websocket_capabilities_and_install_failure(
        self,
    ) -> None:
        scenarios = (
            ("no websocket support", False, False),
            ("websocket without unroute", True, False),
        )
        for label, websocket, websocket_unroute in scenarios:
            with self.subTest(capability=label):
                with tempfile.TemporaryDirectory() as raw:
                    run_dir = Path(raw) / "run-1"
                    document = run_dir / "final" / "poster.html"
                    document.parent.mkdir(parents=True)
                    document.write_text("<!doctype html>", encoding="utf-8")
                    context = _Step3aBrowserContext(
                        websocket=websocket,
                        websocket_unroute=websocket_unroute,
                    )
                    with attempt_candidates_module.attempt_promotion_lease(
                        run_dir
                    ) as leased_run_dir:
                        with self._step3a_session_factory()(
                            leased_run_dir / "final" / "poster.html"
                        ) as session:
                            session.install(SimpleNamespace(context=context))
                    self.assertEqual(len(context.http_unroutes), 1)
                    if websocket:
                        self.assertEqual(len(context.websocket_routes), 1)
                        self.assertEqual(context.websocket_unroutes, [])
                        socket = _Step3aSocket()
                        context.websocket_routes[0][1](socket)
                        self.assertEqual(socket.close_calls, 1)
                    else:
                        self.assertEqual(context.websocket_routes, [])

        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            document = run_dir / "final" / "poster.html"
            document.parent.mkdir(parents=True)
            document.write_text("<!doctype html>", encoding="utf-8")
            context = _Step3aBrowserContext(
                websocket_install_error=RuntimeError("ws install failed")
            )
            caught: BaseException | None = None
            with attempt_candidates_module.attempt_promotion_lease(
                run_dir
            ) as leased_run_dir:
                with self._step3a_session_factory()(
                    leased_run_dir / "final" / "poster.html"
                ) as session:
                    try:
                        session.install(SimpleNamespace(context=context))
                    except BaseException as exc:
                        caught = exc
            self.assertIsInstance(caught, RuntimeError)
            self.assertIn("ws install failed", str(caught))
            self.assertEqual(len(context.http_routes), 1)
            self.assertEqual(
                context.http_unroutes,
                [context.http_routes[0]],
            )

    def test_step3a_session_lifecycle_rejects_duplicate_and_post_close_install(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            document = run_dir / "final" / "poster.html"
            document.parent.mkdir(parents=True)
            document.write_text("<!doctype html>", encoding="utf-8")
            observed_states: list[object] = []
            with attempt_candidates_module.attempt_promotion_lease(
                run_dir
            ) as leased_run_dir:
                with self._step3a_session_factory()(
                    leased_run_dir / "final" / "poster.html"
                ) as session:
                    context = _Step3aBrowserContext()
                    context.on_http_unroute = lambda _handler: observed_states.append(
                        getattr(session, "_state", None)
                    )
                    page = SimpleNamespace(context=context)
                    initial_state = getattr(session, "_state", None)
                    session.install(page)
                    active_state = getattr(session, "_state", None)
                    installed_counts = (
                        len(context.http_routes),
                        len(context.websocket_routes),
                    )

                    try:
                        session.install(page)
                    except (RuntimeError, ValueError):
                        pass
                    duplicate_state = getattr(session, "_state", None)
                    duplicate_counts = (
                        len(context.http_routes),
                        len(context.websocket_routes),
                    )

                    close = getattr(session, "close", None)
                    with self.subTest(contract="explicit close exists"):
                        self.assertTrue(callable(close))
                    if callable(close):
                        close()
                    closed_state = getattr(session, "_state", None)
                    closed_counts = (
                        len(context.http_routes),
                        len(context.websocket_routes),
                    )

                    try:
                        session.install(page)
                    except (RuntimeError, ValueError):
                        pass
                    post_close_state = getattr(session, "_state", None)
                    post_close_counts = (
                        len(context.http_routes),
                        len(context.websocket_routes),
                    )

                    with self.subTest(contract="NEW ACTIVE CLOSING CLOSED"):
                        self.assertEqual(initial_state, "NEW")
                        self.assertEqual(active_state, "ACTIVE")
                        self.assertEqual(observed_states, ["CLOSING"])
                        self.assertEqual(closed_state, "CLOSED")
                    with self.subTest(contract="duplicate install does not reactivate"):
                        self.assertEqual(duplicate_state, "ACTIVE")
                        self.assertEqual(duplicate_counts, installed_counts)
                    with self.subTest(contract="post-close install stays terminal"):
                        self.assertEqual(post_close_state, "CLOSED")
                        self.assertEqual(post_close_counts, closed_counts)

    def test_step3a_pre_reservation_close_aborts_and_reserved_fulfill_drains(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "pre-reservation" / "run-1"
            document = run_dir / "final" / "poster.html"
            document.parent.mkdir(parents=True)
            document.write_text("<!doctype html>", encoding="utf-8")
            with attempt_candidates_module.attempt_promotion_lease(
                run_dir
            ) as leased_run_dir:
                with self._step3a_session_factory()(
                    leased_run_dir / "final" / "poster.html"
                ) as session:
                    context = _Step3aBrowserContext()
                    session.install(SimpleNamespace(context=context))
                    close = getattr(session, "close", None)
                    with self.subTest(contract="close API"):
                        self.assertTrue(callable(close))
                    if not callable(close):
                        return

                    lookup_completed = threading.Event()
                    release_lookup = threading.Event()
                    close_done = threading.Event()
                    parsed = urlsplit(session.url)
                    route = _Step3aRoute(
                        f"{parsed.scheme}://{parsed.netloc}/final/poster.html",
                    )
                    handler_errors: list[BaseException] = []
                    real_browser_relative_path = (
                        attempt_candidates_module._browser_relative_path
                    )

                    def pause_after_request_lookup(value: str, *, label: str):
                        relative = real_browser_relative_path(value, label=label)
                        if label == "attempt promotion browser request":
                            lookup_completed.set()
                            if not release_lookup.wait(2):
                                raise AssertionError(
                                    "pre-reservation lookup was not released"
                                )
                        return relative

                    def invoke_handler() -> None:
                        try:
                            context.http_routes[-1][1](route)
                        except BaseException as exc:
                            handler_errors.append(exc)

                    def invoke_close() -> None:
                        close()
                        close_done.set()

                    handler_thread = threading.Thread(target=invoke_handler)
                    closer_thread = threading.Thread(target=invoke_close)
                    with patch.object(
                        attempt_candidates_module,
                        "_browser_relative_path",
                        side_effect=pause_after_request_lookup,
                    ):
                        handler_thread.start()
                        lookup_was_reached = lookup_completed.wait(2)
                        closer_thread.start()
                        close_returned_before_release = close_done.wait(0.1)
                        deadline = time.monotonic() + 0.5
                        while (
                            getattr(session, "_state", None) != "CLOSING"
                            and not close_done.is_set()
                            and time.monotonic() < deadline
                        ):
                            time.sleep(0.001)
                        state_while_callback_entered = getattr(
                            session,
                            "_state",
                            None,
                        )
                        release_lookup.set()
                    handler_thread.join(2)
                    closer_thread.join(2)
                    self.assertTrue(lookup_was_reached)
                    self.assertFalse(handler_thread.is_alive())
                    self.assertFalse(closer_thread.is_alive())
                    self.assertFalse(close_returned_before_release)
                    self.assertEqual(state_while_callback_entered, "CLOSING")
                    self.assertTrue(close_done.is_set())
                    self.assertEqual(handler_errors, [])
                    self.assertEqual(route.fulfill_calls, [])
                    self.assertEqual(route.abort_calls, ["blockedbyclient"])

            reserved_run = root / "reserved" / "run-1"
            reserved_document = reserved_run / "final" / "poster.html"
            reserved_document.parent.mkdir(parents=True)
            reserved_document.write_text("<!doctype html>", encoding="utf-8")
            with attempt_candidates_module.attempt_promotion_lease(
                reserved_run
            ) as leased_run_dir:
                with self._step3a_session_factory()(
                    leased_run_dir / "final" / "poster.html"
                ) as session:
                    cleanup_started = threading.Event()
                    context = _Step3aBrowserContext(
                        on_http_unroute=lambda _handler: cleanup_started.set()
                    )
                    session.install(SimpleNamespace(context=context))
                    close = getattr(session, "close", None)
                    if not callable(close):
                        return
                    fulfill_entered = threading.Event()
                    release_fulfill = threading.Event()
                    close_done = threading.Event()
                    handler_errors: list[BaseException] = []

                    def pause_fulfill() -> None:
                        fulfill_entered.set()
                        if not release_fulfill.wait(2):
                            raise AssertionError("fulfill callback was not released")

                    parsed = urlsplit(session.url)
                    route = _Step3aRoute(
                        f"{parsed.scheme}://{parsed.netloc}/final/poster.html",
                        fulfill_hook=pause_fulfill,
                    )

                    def invoke_handler() -> None:
                        try:
                            context.http_routes[-1][1](route)
                        except BaseException as exc:
                            handler_errors.append(exc)

                    def invoke_close() -> None:
                        close()
                        close_done.set()

                    handler_thread = threading.Thread(target=invoke_handler)
                    handler_thread.start()
                    self.assertTrue(fulfill_entered.wait(2))
                    closer_thread = threading.Thread(target=invoke_close)
                    closer_thread.start()
                    close_returned_during_callback = close_done.wait(0.1)
                    cleanup_started_before_release = cleanup_started.wait(0.5)
                    state_before_release = getattr(session, "_state", None)
                    release_fulfill.set()
                    handler_thread.join(2)
                    closer_thread.join(2)
                    self.assertFalse(handler_thread.is_alive())
                    self.assertFalse(closer_thread.is_alive())
                    self.assertFalse(close_returned_during_callback)
                    self.assertTrue(cleanup_started_before_release)
                    self.assertEqual(state_before_release, "CLOSING")
                    self.assertTrue(close_done.is_set())
                    self.assertEqual(handler_errors, [])

    def test_step3a_route_exceptions_drain_and_concurrent_close_cleans_once(
        self,
    ) -> None:
        for failure_kind in ("lookup", "abort", "fulfill"):
            with self.subTest(callback=failure_kind):
                with tempfile.TemporaryDirectory() as raw:
                    run_dir = Path(raw) / "run-1"
                    document = run_dir / "final" / "poster.html"
                    document.parent.mkdir(parents=True)
                    document.write_text("<!doctype html>", encoding="utf-8")
                    with attempt_candidates_module.attempt_promotion_lease(
                        run_dir
                    ) as leased_run_dir:
                        with self._step3a_session_factory()(
                            leased_run_dir / "final" / "poster.html"
                        ) as session:
                            context = _Step3aBrowserContext()
                            session.install(SimpleNamespace(context=context))
                            close = getattr(session, "close", None)
                            self.assertTrue(callable(close))
                            if not callable(close):
                                continue
                            callback_error = RuntimeError(
                                f"{failure_kind} callback failed"
                            )
                            parsed = urlsplit(session.url)
                            route_url = (
                                f"{parsed.scheme}://{parsed.netloc}/final/poster.html"
                            )
                            if failure_kind == "lookup":
                                route = _Step3aRoute(route_url)
                            elif failure_kind == "abort":
                                route = _Step3aRoute(
                                    route_url,
                                    method="POST",
                                    abort_hook=lambda: (_ for _ in ()).throw(
                                        callback_error
                                    ),
                                )
                            else:
                                route = _Step3aRoute(
                                    route_url,
                                    fulfill_hook=lambda: (_ for _ in ()).throw(
                                        callback_error
                                    ),
                                )
                            handler_errors: list[BaseException] = []

                            def invoke_handler() -> None:
                                try:
                                    context.http_routes[-1][1](route)
                                except BaseException as exc:
                                    handler_errors.append(exc)

                            lookup_patch = (
                                patch.object(
                                    attempt_candidates_module,
                                    "_browser_relative_path",
                                    side_effect=callback_error,
                                )
                                if failure_kind == "lookup"
                                else nullcontext()
                            )
                            handler_thread = threading.Thread(target=invoke_handler)
                            with lookup_patch:
                                handler_thread.start()
                                handler_thread.join(2)
                            self.assertFalse(handler_thread.is_alive())
                            close_done = threading.Event()

                            def invoke_close() -> None:
                                close()
                                close_done.set()

                            closer = threading.Thread(target=invoke_close)
                            closer.start()
                            closer.join(2)
                            self.assertFalse(closer.is_alive())
                            self.assertTrue(close_done.is_set())
                            if failure_kind == "lookup":
                                self.assertIn(handler_errors, ([], [callback_error]))
                                self.assertEqual(route.fulfill_calls, [])
                            else:
                                self.assertEqual(handler_errors, [callback_error])

        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            document = run_dir / "final" / "poster.html"
            document.parent.mkdir(parents=True)
            document.write_text("<!doctype html>", encoding="utf-8")
            with attempt_candidates_module.attempt_promotion_lease(
                run_dir
            ) as leased_run_dir:
                with self._step3a_session_factory()(
                    leased_run_dir / "final" / "poster.html"
                ) as session:
                    cleanup_entered = threading.Event()
                    release_cleanup = threading.Event()

                    def block_http_cleanup(_handler) -> None:
                        cleanup_entered.set()
                        if not release_cleanup.wait(2):
                            raise AssertionError("HTTP cleanup was not released")

                    context = _Step3aBrowserContext(
                        on_http_unroute=block_http_cleanup
                    )
                    session.install(SimpleNamespace(context=context))
                    installed_http_route = context.http_routes[0]
                    installed_websocket_route = context.websocket_routes[0]
                    close = getattr(session, "close", None)
                    self.assertTrue(callable(close))
                    if not callable(close):
                        return
                    start = threading.Event()
                    completed: list[int] = []
                    completed_states: list[object] = []
                    close_errors: list[BaseException] = []

                    def concurrent_close(index: int) -> None:
                        if not start.wait(2):
                            close_errors.append(AssertionError("close start timed out"))
                            return
                        try:
                            close()
                            completed_states.append(getattr(session, "_state", None))
                            completed.append(index)
                        except BaseException as exc:
                            close_errors.append(exc)

                    closers = [
                        threading.Thread(target=concurrent_close, args=(index,))
                        for index in range(8)
                    ]
                    for closer in closers:
                        closer.start()
                    start.set()
                    cleanup_was_reached = cleanup_entered.wait(2)
                    no_closer_returned_during_cleanup = not bool(completed)
                    state_during_cleanup = getattr(session, "_state", None)
                    release_cleanup.set()
                    for closer in closers:
                        closer.join(2)
                    self.assertTrue(cleanup_was_reached)
                    self.assertTrue(no_closer_returned_during_cleanup)
                    self.assertEqual(state_during_cleanup, "CLOSING")
                    self.assertTrue(all(not closer.is_alive() for closer in closers))
                    self.assertEqual(close_errors, [])
                    self.assertEqual(sorted(completed), list(range(8)))
                    self.assertEqual(completed_states, ["CLOSED"] * 8)
                self.assertEqual(context.http_unroutes, [installed_http_route])
                self.assertEqual(
                    context.websocket_unroutes,
                    [installed_websocket_route],
                )

    def test_step3a_legacy_callback_keeps_accessor_until_close_drain(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir, candidate, manifest_path = self._step3a_browser_candidate(
                Path(raw),
                dependencies={"assets/site.css": b"original-css"},
            )
            manifest_path.unlink()
            real_secure_access = attempt_candidates_module.secure_run_member_access
            real_accessor_read = (
                attempt_candidates_module.SecureRunMemberAccessor.read_bytes
            )
            accessor_entered = threading.Event()
            accessor_exited = threading.Event()
            callback_read_entered = threading.Event()
            release_callback_read = threading.Event()
            lifecycle_order: list[str] = []
            retained_accessors = []

            @contextmanager
            def tracked_secure_access(path):
                with real_secure_access(path) as accessor:
                    accessor_entered.set()
                    lifecycle_order.append("accessor-enter")
                    retained_accessors.append(accessor)
                    yield accessor
                lifecycle_order.append("accessor-exit")
                accessor_exited.set()

            def tracked_read(accessor, value, **kwargs):
                snapshot = real_accessor_read(accessor, value, **kwargs)
                if snapshot.relative_path.as_posix().endswith("assets/site.css"):
                    callback_read_entered.set()
                    if not release_callback_read.wait(2):
                        raise AssertionError("legacy callback read was not released")
                return snapshot

            with (
                patch.object(
                    attempt_candidates_module,
                    "secure_run_member_access",
                    side_effect=tracked_secure_access,
                ),
                patch.object(
                    attempt_candidates_module.SecureRunMemberAccessor,
                    "read_bytes",
                    new=tracked_read,
                ),
                attempt_candidates_module.attempt_promotion_lease(
                    run_dir
                ) as leased_run_dir,
            ):
                with self._step3a_session_factory()(
                    leased_run_dir / candidate.source_relative_path
                ) as session:
                    context = _Step3aBrowserContext()
                    session.install(SimpleNamespace(context=context))
                    close = getattr(session, "close", None)
                    with self.subTest(contract="close API"):
                        self.assertTrue(callable(close))
                    if not callable(close):
                        return
                    route = _Step3aRoute(
                        "https://autodesign.invalid/"
                        + candidate.dependency_relative_paths[0]
                    )
                    handler_errors: list[BaseException] = []

                    def invoke_handler() -> None:
                        try:
                            context.http_routes[-1][1](route)
                        except BaseException as exc:
                            handler_errors.append(exc)

                    close_done = threading.Event()

                    def invoke_close() -> None:
                        close()
                        lifecycle_order.append("close-return")
                        close_done.set()

                    handler = threading.Thread(target=invoke_handler)
                    handler.start()
                    self.assertTrue(callback_read_entered.wait(2))
                    closer = threading.Thread(target=invoke_close)
                    closer.start()
                    close_returned_early = close_done.wait(0.1)
                    accessor_exited_early = accessor_exited.is_set()
                    release_callback_read.set()
                    handler.join(2)
                    closer.join(2)
                    self.assertFalse(handler.is_alive())
                    self.assertFalse(closer.is_alive())
                    self.assertFalse(close_returned_early)
                    self.assertFalse(accessor_exited_early)
                    self.assertTrue(accessor_entered.is_set())
                    self.assertFalse(accessor_exited.is_set())
                    self.assertEqual(handler_errors, [])
                    self.assertEqual(route.fulfill_calls, [])
                    self.assertEqual(route.abort_calls, ["blockedbyclient"])
                    retained_after_close = retained_accessors[0].read_bytes(
                        run_dir / candidate.source_relative_path,
                        label="session-retained document after close",
                    )
                    self.assertEqual(
                        retained_after_close.data,
                        b"<!doctype html><main>document</main>",
                    )
                    self.assertEqual(
                        lifecycle_order,
                        ["accessor-enter", "close-return"],
                    )
                self.assertTrue(accessor_exited.is_set())
                self.assertEqual(
                    lifecycle_order,
                    ["accessor-enter", "close-return", "accessor-exit"],
                )

    @unittest.skipUnless(os.name == "posix", "mocked Windows branch uses POSIX links")
    def test_step3a_windows_accessor_rejects_nested_swap_and_reparse(self) -> None:
        portable_windows = SimpleNamespace(
            name="nt",
            path=os.path,
            fspath=os.fspath,
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "swap" / "run-1"
            nested = run_dir / "assets"
            nested.mkdir(parents=True)
            (nested / "payload.css").write_bytes(b"original")
            replacement = run_dir / "replacement-assets"
            replacement.mkdir()
            (replacement / "payload.css").write_bytes(b"replacement")
            detached = run_dir / "detached-assets"
            canonical_nested = nested.resolve()
            swapped = False

            @contextmanager
            def swapping_guard(path: Path):
                nonlocal swapped
                if Path(path) == canonical_nested and not swapped:
                    nested.rename(detached)
                    replacement.rename(nested)
                    swapped = True
                yield 1

            with (
                patch.object(
                    attempt_candidates_module,
                    "_RUNTIME_OS",
                    portable_windows,
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_directory_replacement_guard",
                    side_effect=swapping_guard,
                ),
                self.assertRaisesRegex(ValueError, "changed during guarded open"),
            ):
                with attempt_candidates_module.secure_run_member_access(
                    run_dir
                ) as accessor:
                    accessor.read_bytes(
                        run_dir / "assets" / "payload.css",
                        label="nested swapped browser resource",
                    )
            self.assertTrue(swapped)

            reparse_run = root / "reparse" / "run-1"
            reparse_run.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            (outside / "payload.css").write_bytes(b"outside")
            (reparse_run / "assets").symlink_to(
                outside,
                target_is_directory=True,
            )
            with (
                patch.object(
                    attempt_candidates_module,
                    "_RUNTIME_OS",
                    portable_windows,
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_directory_replacement_guard",
                    side_effect=lambda _path: nullcontext(1),
                ),
                self.assertRaisesRegex(ValueError, "reparse|link|no-follow"),
            ):
                with attempt_candidates_module.secure_run_member_access(
                    reparse_run
                ) as accessor:
                    accessor.read_bytes(
                        reparse_run / "assets" / "payload.css",
                        label="nested reparse browser resource",
                    )

    def test_guarded_resolve_normalizes_parent_segments_without_escaping(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            dependency = run_dir / "layers" / "figure.png"
            dependency.parent.mkdir(parents=True)
            dependency.write_bytes(b"figure")

            with attempt_candidates_module.attempt_promotion_lease(
                run_dir
            ) as leased_run_dir:
                resolved = (
                    leased_run_dir
                    / "composites"
                    / "iter_01"
                    / ".."
                    / ".."
                    / "layers"
                    / "figure.png"
                ).resolve()
                self.assertEqual(resolved.read_bytes(), b"figure")
                with self.assertRaisesRegex(ValueError, "escaped"):
                    (
                        leased_run_dir
                        / ".."
                        / "outside.txt"
                    ).resolve()

    def test_guarded_traversal_keeps_descendants_on_stable_filesystem_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            dependency = run_dir / "assets" / "figure.png"
            dependency.parent.mkdir(parents=True)
            dependency.write_bytes(b"figure")

            with attempt_candidates_module.attempt_promotion_lease(
                run_dir
            ) as leased_run_dir:
                stable_root = Path(os.fspath(leased_run_dir))
                traversals = (
                    next((leased_run_dir / "assets").glob("*")),
                    next((leased_run_dir / "assets").rglob("*")),
                    next((leased_run_dir / "assets").iterdir()),
                    next((leased_run_dir / "assets").walk())[0],
                )
                for discovered in traversals[:3]:
                    self.assertEqual(
                        Path(os.fspath(discovered))
                        .relative_to(stable_root)
                        .as_posix(),
                        "assets/figure.png",
                    )
                    self.assertEqual(discovered.read_bytes(), b"figure")
                self.assertEqual(
                    Path(os.fspath(traversals[3]))
                    .relative_to(stable_root)
                    .as_posix(),
                    "assets",
                )
                with self.assertRaisesRegex(ValueError, "cannot follow symlinks"):
                    next((leased_run_dir / "assets").glob("*", recurse_symlinks=True))
                with self.assertRaisesRegex(ValueError, "cannot follow symlinks"):
                    next((leased_run_dir / "assets").rglob("*", recurse_symlinks=True))
                with self.assertRaisesRegex(ValueError, "cannot follow symlinks"):
                    next((leased_run_dir / "assets").walk(follow_symlinks=True))

    def test_landing_dependency_walk_resolves_inside_stable_run_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            attempt_dir = run_dir / "landing_author" / "attempt_01"
            layer_path = attempt_dir / "layers" / "figure.png"
            layer_path.parent.mkdir(parents=True)
            layer_path.write_bytes(b"figure")
            (attempt_dir / "index.html").write_text(
                '<!doctype html><img src="layers/figure.png">',
                encoding="utf-8",
            )

            with attempt_candidates_module.attempt_promotion_lease(
                run_dir
            ) as leased_run_dir:
                leased_attempt = (
                    leased_run_dir / "landing_author" / "attempt_01"
                )
                dependencies = (
                    external_landing_author_module._landing_dependency_closure_files(
                        leased_attempt,
                        leased_attempt / "index.html",
                    )
                )
                stable_root = Path(os.fspath(leased_run_dir))
                self.assertEqual(
                    sorted(
                        path.relative_to(stable_root).as_posix()
                        for path in dependencies
                    ),
                    [
                        "landing_author/attempt_01/index.html",
                        "landing_author/attempt_01/layers/figure.png",
                    ],
                )

    def test_atomic_promotion_accepts_only_active_stable_symlink_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            run_dir.mkdir()

            with attempt_candidates_module.attempt_promotion_lease(
                run_dir
            ) as leased_run_dir:
                final_dir = leased_run_dir / "final"
                paths = atomic_promotion_module._promotion_paths(
                    final_dir,
                    "landing",
                )
                path_type = type(final_dir.parent)
                real_is_symlink = path_type.is_symlink

                def simulated_linux_proc_root(path):
                    if Path(os.fspath(path)) == Path(os.fspath(leased_run_dir)):
                        return True
                    return real_is_symlink(path)

                with patch.object(
                    path_type,
                    "is_symlink",
                    simulated_linux_proc_root,
                ):
                    atomic_promotion_module._validate_promotion_root(
                        final_dir,
                        paths=paths,
                    )

    @unittest.skipUnless(os.name == "posix", "directory replacement uses POSIX rename")
    def test_landing_journal_mkstemp_gap_rolls_back_stable_run_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "run-1"
            replacement = root / "replacement"
            original = root / "original"
            run_dir.mkdir()
            replacement.mkdir()
            for directory, marker in ((run_dir, "A-old"), (replacement, "B-old")):
                final_dir = directory / "final"
                final_dir.mkdir()
                (final_dir / "marker.txt").write_text(marker, encoding="utf-8")
            attempt_dir = run_dir / "landing_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "index.html").write_text(
                "<!doctype html><main>A</main>",
                encoding="utf-8",
            )
            (attempt_dir / "designer_author_done.json").write_text(
                "{}",
                encoding="utf-8",
            )
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id="run-1",
            )
            real_mkstemp = tempfile.mkstemp
            swapped = False

            def swap_during_promotion_journal(*, prefix, dir):
                nonlocal swapped
                stable_parent = Path(os.fspath(dir))
                if prefix == "..landing-final-promotion.json." and not swapped:
                    run_dir.rename(original)
                    replacement.rename(run_dir)
                    swapped = True
                return real_mkstemp(prefix=prefix, dir=os.fspath(stable_parent))

            def render_preview(_html_path, preview_path, **_kwargs):
                preview_path.write_bytes(b"preview")
                return SimpleNamespace(backend="test", warnings=[])

            with (
                patch.object(
                    external_landing_author_module,
                    "ensure_editable_html_contract",
                ),
                patch.object(
                    external_landing_author_module,
                    "screenshot_html",
                    side_effect=render_preview,
                ),
                patch.object(
                    run_control_module.tempfile,
                    "mkstemp",
                    side_effect=swap_during_promotion_journal,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "changed during"):
                    ExternalLandingAuthor(ctx.settings, "")._promote(
                        ctx,
                        attempt_dir=attempt_dir,
                        diagnostics={"accepted": True},
                        candidate_id="landing",
                    )

            self.assertTrue(swapped)
            self.assertEqual(
                (original / "final" / "marker.txt").read_text(encoding="utf-8"),
                "A-old",
            )
            self.assertEqual(
                (run_dir / "final" / "marker.txt").read_text(encoding="utf-8"),
                "B-old",
            )
            for directory in (original, run_dir):
                self.assertFalse(
                    (directory / ".landing-final-promotion.json").exists()
                )
                self.assertEqual(list(directory.glob(".landing-final-staging-*")), [])
                self.assertEqual(list(directory.glob(".landing-final-backup-*")), [])

    @unittest.skipUnless(os.name == "posix", "directory replacement uses POSIX rename")
    def test_landing_replace_gaps_restore_old_final_without_touching_replacement(
        self,
    ) -> None:
        for swap_on_replace in (1, 2):
            with self.subTest(swap_on_replace=swap_on_replace):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    run_dir = root / "run-1"
                    replacement = root / "replacement"
                    original = root / "original"
                    run_dir.mkdir()
                    replacement.mkdir()
                    for directory, marker in (
                        (run_dir, "A-old"),
                        (replacement, "B-old"),
                    ):
                        final_dir = directory / "final"
                        final_dir.mkdir()
                        (final_dir / "marker.txt").write_text(
                            marker,
                            encoding="utf-8",
                        )
                    attempt_dir = run_dir / "landing_author" / "attempt_01"
                    attempt_dir.mkdir(parents=True)
                    (attempt_dir / "index.html").write_text(
                        "<!doctype html><main>A-new</main>",
                        encoding="utf-8",
                    )
                    (attempt_dir / "designer_author_done.json").write_text(
                        "{}",
                        encoding="utf-8",
                    )
                    ctx = ToolContext(
                        settings=SimpleNamespace(),
                        run_dir=run_dir,
                        layers_dir=run_dir / "layers",
                        run_id="run-1",
                    )
                    real_replace = os.replace
                    replace_count = 0
                    swapped = False

                    def swap_during_directory_replace(source, destination):
                        nonlocal replace_count, swapped
                        replace_count += 1
                        stable_source = os.fspath(source)
                        stable_destination = os.fspath(destination)
                        if replace_count == swap_on_replace:
                            run_dir.rename(original)
                            replacement.rename(run_dir)
                            swapped = True
                        real_replace(stable_source, stable_destination)

                    def render_preview(_html_path, preview_path, **_kwargs):
                        preview_path.write_bytes(b"preview")
                        return SimpleNamespace(backend="test", warnings=[])

                    with (
                        patch.object(
                            external_landing_author_module,
                            "ensure_editable_html_contract",
                        ),
                        patch.object(
                            external_landing_author_module,
                            "screenshot_html",
                            side_effect=render_preview,
                        ),
                        patch.object(
                            atomic_promotion_module,
                            "_replace_path",
                            side_effect=swap_during_directory_replace,
                        ),
                    ):
                        with self.assertRaisesRegex(ValueError, "changed during"):
                            ExternalLandingAuthor(ctx.settings, "")._promote(
                                ctx,
                                attempt_dir=attempt_dir,
                                diagnostics={"accepted": True},
                                candidate_id="landing",
                            )

                    self.assertTrue(swapped)
                    self.assertEqual(
                        (original / "final" / "marker.txt").read_text(
                            encoding="utf-8"
                        ),
                        "A-old",
                    )
                    self.assertEqual(
                        (run_dir / "final" / "marker.txt").read_text(
                            encoding="utf-8"
                        ),
                        "B-old",
                    )
                    for directory in (original, run_dir):
                        self.assertFalse(
                            (directory / ".landing-final-promotion.json").exists()
                        )
                        self.assertEqual(
                            list(directory.glob(".landing-final-staging-*")),
                            [],
                        )
                        self.assertEqual(
                            list(directory.glob(".landing-final-backup-*")),
                            [],
                        )

    @unittest.skipUnless(os.name == "posix", "alias retarget uses POSIX symlinks")
    def test_expected_run_identity_rejects_prelease_alias_retarget_without_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            requested_run, first_run, second_run, alias_parent, second_parent = (
                self._normal_alias_runs(root)
            )
            expected_identity = attempt_candidates_module.promotion_run_identity(
                requested_run
            )
            alias_parent.unlink()
            alias_parent.symlink_to(second_parent, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "changed before lease entry"):
                with attempt_selection_module.normal_promotion_lease(
                    run_dir=requested_run,
                    candidate_id="prelease-retarget",
                    expected_run_identity=expected_identity,
                ):
                    self.fail("lease entered after alias retarget")

            self.assertFalse((first_run / "attempt_candidates").exists())
            self.assertFalse((second_run / "attempt_candidates").exists())

    @unittest.skipUnless(os.name == "posix", "alias retarget uses POSIX symlinks")
    def test_post_acquisition_alias_retarget_creates_no_control_in_new_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            requested_run, first_run, second_run, alias_parent, second_parent = (
                self._normal_alias_runs(root)
            )
            original_stable_lease = (
                attempt_candidates_module._stable_coordination_lease
            )

            @contextmanager
            def retarget_after_acquisition(run_dir):
                with original_stable_lease(run_dir) as stable_lease:
                    alias_parent.unlink()
                    alias_parent.symlink_to(second_parent, target_is_directory=True)
                    yield stable_lease

            with patch.object(
                attempt_candidates_module,
                "_stable_coordination_lease",
                side_effect=retarget_after_acquisition,
            ):
                with self.assertRaisesRegex(ValueError, "changed during"):
                    with attempt_candidates_module.attempt_promotion_lease(
                        requested_run
                    ):
                        self.fail("lease entered after alias retarget")

            self.assertTrue((first_run / "attempt_candidates").is_dir())
            self.assertFalse((second_run / "attempt_candidates").exists())

    @unittest.skipUnless(os.name == "posix", "alias retarget uses POSIX symlinks")
    def test_poster_normal_publish_rejects_retargeted_alias_before_writing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            requested_run, first_run, second_run, alias_parent, second_parent = (
                self._normal_alias_runs(root)
            )
            attempt_dir = requested_run / "designer_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            poster_path = attempt_dir / "poster.html"
            poster_path.write_text(
                "<!doctype html><main class='paper-poster'>Poster</main>",
                encoding="utf-8",
            )
            ctx = ToolContext(
                settings=SimpleNamespace(repo_root=root),
                run_dir=requested_run,
                layers_dir=requested_run / "layers",
                run_id="run-1",
            )
            real_publish = external_designer_author_module._publish_poster_final
            injected_staging: list[Path] = []

            def retarget_then_publish(staging_dir, final_dir, checkpoint):
                injected_staging.append(
                    self._retarget_normal_alias(
                        alias_parent=alias_parent,
                        second_parent=second_parent,
                        second_run=second_run,
                        staging_name=staging_dir.name,
                    )
                )
                real_publish(staging_dir, final_dir, checkpoint)

            def render_preview(*, preview_path, **_kwargs):
                preview_path.write_bytes(b"preview")
                return SimpleNamespace(
                    backend="test",
                    warnings=[],
                    scale=1,
                    width_px=100,
                    height_px=50,
                )

            with (
                patch.object(
                    external_designer_author_module,
                    "ensure_poster_katex_document",
                    return_value={"detected": False, "applied": False},
                ),
                patch.object(
                    external_designer_author_module,
                    "_direct_canvas",
                    return_value={"width": 100, "height": 50},
                ),
                patch.object(
                    external_designer_author_module,
                    "_resolve_layer_asset_placeholders",
                    return_value={},
                ),
                patch.object(
                    external_designer_author_module,
                    "_inline_local_assets",
                    return_value={},
                ),
                patch.object(
                    external_designer_author_module,
                    "_maybe_repair_collapsed_poster_header",
                    return_value={},
                ),
                patch.object(
                    external_designer_author_module,
                    "_render_direct_preview",
                    side_effect=render_preview,
                ),
                patch.object(
                    external_designer_author_module,
                    "_publish_poster_final",
                    side_effect=retarget_then_publish,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "changed during"):
                    ExternalDesignerAuthor(ctx.settings, "")._promote_direct_final(
                        ctx,
                        attempt_index=1,
                        attempt_dir=attempt_dir,
                        poster_path=poster_path,
                        poster_sha256="",
                    )

            self._assert_normal_alias_publish_wrote_neither_run(
                first_run=first_run,
                second_run=second_run,
                artifact_name="poster",
                injected_staging=injected_staging[0],
            )
            self.assertEqual(
                list(first_run.glob(".poster-final-staging-*")),
                [],
            )

    @unittest.skipUnless(os.name == "posix", "alias retarget uses POSIX symlinks")
    def test_poster_fallback_normalizes_canonical_assets_under_promotion_lease(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            real_parent = root / "real"
            real_parent.mkdir()
            alias_parent = root / "alias"
            alias_parent.symlink_to(real_parent, target_is_directory=True)
            run_dir = real_parent / "run"
            requested_run_dir = alias_parent / "run"
            source_layers = requested_run_dir / "layers"
            source_layers.mkdir(parents=True)
            source_image = source_layers / "figure.png"
            source_image.write_bytes(b"\x89PNG\r\n\x1a\nposter-asset")
            source_html = requested_run_dir / "html_first" / "measure.html"
            source_html.parent.mkdir(parents=True)
            canonical_run_dir = run_dir.resolve()
            original_html = (
                "<!doctype html>"
                f"<img src='{requested_run_dir}/layers/figure.png'>"
                f"<img src='{canonical_run_dir}/layers/figure.png'>"
            )
            source_html.write_text(original_html, encoding="utf-8")

            with attempt_selection_module.normal_promotion_lease(
                run_dir=requested_run_dir,
                candidate_id="poster-fallback-assets",
            ) as leased_run_dir:
                iter_dir = leased_run_dir / "composites" / "iter_01"
                iter_dir.mkdir(parents=True)
                iter_html = iter_dir / "poster.html"
                shutil.copy2(source_html, iter_html)
                iter_layers = iter_dir / "layers"
                iter_layers.mkdir()
                shutil.copy2(leased_run_dir / "layers" / "figure.png", iter_layers)

                external_designer_author_module._rewrite_run_local_asset_refs(
                    iter_html,
                    leased_run_dir,
                    additional_run_dirs=(requested_run_dir,),
                )
                inline_stats = external_designer_author_module._inline_local_assets(
                    iter_html,
                )
                published_html = iter_html.read_text(encoding="utf-8")

                self.assertEqual(
                    source_html.read_text(encoding="utf-8"),
                    original_html,
                )
                self.assertNotIn(str(requested_run_dir), published_html)
                self.assertNotIn(str(canonical_run_dir), published_html)
                self.assertNotIn(str(leased_run_dir.resolve()), published_html)
                self.assertIn("data:image/png;base64,", published_html)
                self.assertEqual(inline_stats["inlined_count"], 2)
                self.assertEqual(inline_stats["missing_count"], 0)

    @unittest.skipUnless(os.name == "posix", "alias retarget uses POSIX symlinks")
    def test_selected_poster_inlines_requested_symlink_assets_without_mutating_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            real_parent = root / "real"
            real_parent.mkdir()
            alias_parent = root / "alias"
            alias_parent.symlink_to(real_parent, target_is_directory=True)
            real_run_dir = real_parent / "run"
            requested_run_dir = alias_parent / "run"
            attempt_dir = requested_run_dir / "designer_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            layers_dir = requested_run_dir / "layers"
            layers_dir.mkdir()
            asset_bytes = b"\x89PNG\r\n\x1a\nselected-poster-asset"
            (layers_dir / "figure.png").write_bytes(asset_bytes)
            attempt_layers_dir = attempt_dir / "layers"
            attempt_layers_dir.mkdir()
            (attempt_layers_dir / "figure.png").write_bytes(asset_bytes)
            candidate_html = (
                "<!doctype html><html><body>"
                "<main class='paper-poster'>"
                f"<img src='{requested_run_dir}/layers/figure.png'>"
                "</main></body></html>"
            )
            (attempt_dir / "poster.html").write_text(
                candidate_html,
                encoding="utf-8",
            )
            (attempt_dir / "validation.json").write_text(
                '{"accepted":true}',
                encoding="utf-8",
            )
            candidate = capture_attempt_candidate(
                run_dir=requested_run_dir,
                attempt_dir=attempt_dir,
                artifact_type="poster",
                attempt=1,
                max_attempts=1,
                source_path="poster.html",
                dependency_paths=["layers/figure.png"],
                preview_paths=[],
                validation_summary_path="validation.json",
                safety_state="ready",
                hard_blockers=[],
                warnings=[],
            )
            selected_source = requested_run_dir / candidate.source_relative_path
            selected_source_before = selected_source.read_bytes()
            accepted = request_attempt_selection(
                run_dir=requested_run_dir,
                run_id="run",
                attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
                idempotency_key="selected-poster-symlink",
            )
            self.assertEqual(accepted.status, "selection_accepted")
            ctx = ToolContext(
                settings=SimpleNamespace(repo_root=root),
                run_dir=requested_run_dir,
                layers_dir=requested_run_dir / "layers",
                run_id="run",
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

            promotion_errors: list[Exception] = []

            def promote_selected(ctx, selected_candidate) -> None:
                try:
                    external_designer_author_module.promote_selected_attempt(
                        ctx,
                        selected_candidate,
                    )
                except Exception as exc:
                    promotion_errors.append(exc)
                    raise

            with (
                patch.object(
                    ExternalDesignerAuthor,
                    "_direct_final_validation_feedback",
                    return_value=None,
                ),
                patch.object(
                    external_designer_author_module,
                    "_render_direct_preview",
                    side_effect=fake_render,
                ),
                patch.object(
                    external_designer_author_module,
                    "_maybe_repair_collapsed_poster_header",
                    return_value=None,
                ),
            ):
                outcome = promote_pending_selection(ctx, promoter=promote_selected)

            self.assertEqual(outcome, "complete", promotion_errors)
            self.assertEqual(selected_source.read_bytes(), selected_source_before)
            final_html = (real_run_dir / "final" / "poster.html").read_text(
                encoding="utf-8"
            )
            self.assertNotIn(str(requested_run_dir), final_html)
            self.assertNotIn(str(real_run_dir.resolve()), final_html)
            self.assertIn("data:image/png;base64,", final_html)
            self.assertNotIn(
                "attempt_selection_requested_run_dir",
                ctx.state,
            )

    @unittest.skipUnless(os.name == "posix", "alias retarget uses POSIX symlinks")
    def test_poster_fallback_normal_publish_rejects_retargeted_alias_before_writing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            requested_run, first_run, second_run, alias_parent, second_parent = (
                self._normal_alias_runs(root)
            )
            candidate_dir = requested_run / "designer_author" / "attempt_01" / "candidate"
            candidate_dir.mkdir(parents=True)
            measure_html = candidate_dir / "measure.html"
            measure_html.write_text(
                "<!doctype html><main class='paper-poster'>Fallback</main>",
                encoding="utf-8",
            )
            ctx = ToolContext(
                settings=SimpleNamespace(repo_root=root),
                run_dir=requested_run,
                layers_dir=requested_run / "layers",
                run_id="run-1",
            )
            candidate = {
                "candidate_id": "poster-fallback-normal",
                "candidate_score": 1,
                "_candidate_dir_abs": str(candidate_dir),
                "_measure_html_abs": str(measure_html),
                "_preview_png_abs": "",
            }
            real_publish = publish_artifact_directory
            injected_staging: list[Path] = []

            def retarget_then_publish(
                staging_dir,
                final_dir,
                *,
                artifact_name,
                post_publish,
            ):
                injected_staging.append(
                    self._retarget_normal_alias(
                        alias_parent=alias_parent,
                        second_parent=second_parent,
                        second_run=second_run,
                        staging_name=staging_dir.name,
                    )
                )
                real_publish(
                    staging_dir,
                    final_dir,
                    artifact_name=artifact_name,
                    post_publish=post_publish,
                )

            def render_preview(*, preview_path, **_kwargs):
                preview_path.write_bytes(b"preview")
                return SimpleNamespace(
                    backend="test",
                    warnings=[],
                    scale=1,
                    width_px=100,
                    height_px=50,
                )

            with (
                patch.object(
                    external_designer_author_module,
                    "_rewrite_run_local_asset_refs",
                ),
                patch.object(
                    external_designer_author_module,
                    "ensure_poster_katex_document",
                    return_value={"detected": False, "applied": False},
                ),
                patch.object(
                    external_designer_author_module,
                    "_direct_canvas",
                    return_value={"width": 100, "height": 50},
                ),
                patch.object(
                    external_designer_author_module,
                    "_poster_root_scroll_metrics",
                    return_value={},
                ),
                patch.object(
                    external_designer_author_module,
                    "_resolve_layer_asset_placeholders",
                    return_value={},
                ),
                patch.object(
                    external_designer_author_module,
                    "_inline_local_assets",
                    return_value={},
                ),
                patch.object(
                    external_designer_author_module,
                    "apply_poster_typesetting_patch",
                    return_value={"applied": False},
                ),
                patch.object(
                    external_designer_author_module,
                    "_maybe_repair_collapsed_poster_header",
                    return_value={},
                ),
                patch.object(
                    external_designer_author_module,
                    "_render_direct_preview",
                    side_effect=render_preview,
                ),
                patch.object(
                    external_designer_author_module,
                    "publish_artifact_directory",
                    side_effect=retarget_then_publish,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "changed during"):
                    ExternalDesignerAuthor(
                        ctx.settings,
                        "",
                    )._promote_html_first_candidate_fallback(
                        ctx,
                        attempt_index=1,
                        attempt_dir=candidate_dir.parent,
                        candidate=candidate,
                        acceptance={},
                        rejected_candidates=[],
                        source_reason="test",
                        source_message="test",
                        last_feedback=None,
                    )

            self._assert_normal_alias_publish_wrote_neither_run(
                first_run=first_run,
                second_run=second_run,
                artifact_name="poster",
                injected_staging=injected_staging[0],
            )
            self.assertEqual(
                list(first_run.glob(".poster-final-staging-*")),
                [],
            )

    @unittest.skipUnless(os.name == "posix", "alias retarget uses POSIX symlinks")
    def test_landing_normal_publish_rejects_retargeted_alias_before_writing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            requested_run, first_run, second_run, alias_parent, second_parent = (
                self._normal_alias_runs(root)
            )
            attempt_dir = requested_run / "landing_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "index.html").write_text(
                "<!doctype html><main>Landing</main>",
                encoding="utf-8",
            )
            (attempt_dir / "designer_author_done.json").write_text(
                "{}",
                encoding="utf-8",
            )
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=requested_run,
                layers_dir=requested_run / "layers",
                run_id="run-1",
            )
            real_publish = external_landing_author_module._atomic_replace_directory
            injected_staging: list[Path] = []

            def retarget_then_publish(staging_dir, final_dir, *, post_publish):
                injected_staging.append(
                    self._retarget_normal_alias(
                        alias_parent=alias_parent,
                        second_parent=second_parent,
                        second_run=second_run,
                        staging_name=staging_dir.name,
                    )
                )
                real_publish(
                    staging_dir,
                    final_dir,
                    post_publish=post_publish,
                )

            def render_preview(_html_path, preview_path, **_kwargs):
                preview_path.write_bytes(b"preview")
                return SimpleNamespace(backend="test", warnings=[])

            with (
                patch.object(
                    external_landing_author_module,
                    "ensure_editable_html_contract",
                ),
                patch.object(
                    external_landing_author_module,
                    "screenshot_html",
                    side_effect=render_preview,
                ),
                patch.object(
                    external_landing_author_module,
                    "_atomic_replace_directory",
                    side_effect=retarget_then_publish,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "changed during"):
                    ExternalLandingAuthor(ctx.settings, "")._promote(
                        ctx,
                        attempt_dir=attempt_dir,
                        diagnostics={"accepted": True},
                        candidate_id="landing-normal",
                    )

            self._assert_normal_alias_publish_wrote_neither_run(
                first_run=first_run,
                second_run=second_run,
                artifact_name="landing",
                injected_staging=injected_staging[0],
            )

    @unittest.skipUnless(os.name == "posix", "alias retarget uses POSIX symlinks")
    def test_slides_normal_publish_rejects_retargeted_alias_before_writing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            requested_run, first_run, second_run, alias_parent, second_parent = (
                self._normal_alias_runs(root)
            )
            attempt_dir = requested_run / "slides_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "slides.html").write_text(
                "<!doctype html><html><body><main id='deck' data-slide-count='1'>"
                "<section class='deck-slide' id='slide-1'><h1>Slide</h1></section>"
                "</main></body></html>",
                encoding="utf-8",
            )
            for name in (
                "designer_author_done.json",
                "slides_visual_plan.json",
                "slides_asset_catalog.json",
                "slides_validation.json",
            ):
                (attempt_dir / name).write_text("{}", encoding="utf-8")
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=requested_run,
                layers_dir=requested_run / "layers",
                run_id="run-1",
            )
            real_publish = external_slides_author_module._atomic_replace_directory
            injected_staging: list[Path] = []

            def retarget_then_publish(staging_dir, final_dir, *, post_publish):
                injected_staging.append(
                    self._retarget_normal_alias(
                        alias_parent=alias_parent,
                        second_parent=second_parent,
                        second_run=second_run,
                        staging_name=staging_dir.name,
                    )
                )
                real_publish(
                    staging_dir,
                    final_dir,
                    post_publish=post_publish,
                )

            def render_slides(_html_path, slides_dir, **_kwargs):
                slides_dir.mkdir(parents=True)
                slide_path = slides_dir / "slide-01.png"
                slide_path.write_bytes(b"slide")
                return SimpleNamespace(
                    paths=[str(slide_path)],
                    backend="test",
                    warnings=[],
                )

            def build_preview(_paths, preview_path):
                preview_path.write_bytes(b"preview")

            with (
                patch.object(
                    external_slides_author_module,
                    "screenshot_deck_slides",
                    side_effect=render_slides,
                ),
                patch.object(
                    external_slides_author_module,
                    "build_deck_preview_grid",
                    side_effect=build_preview,
                ),
                patch.object(
                    external_slides_author_module,
                    "_atomic_replace_directory",
                    side_effect=retarget_then_publish,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "changed during"):
                    ExternalSlidesAuthor(ctx.settings, "")._promote(
                        ctx,
                        attempt_dir=attempt_dir,
                        expected_slide_count=1,
                        validation={"accepted": True},
                        candidate_id="slides-normal",
                    )

            self._assert_normal_alias_publish_wrote_neither_run(
                first_run=first_run,
                second_run=second_run,
                artifact_name="slides",
                injected_staging=injected_staging[0],
            )

    @unittest.skipUnless(os.name == "posix", "alias retarget uses POSIX symlinks")
    def test_video_normal_delivery_rejects_retargeted_alias_before_writing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            requested_run, first_run, second_run, alias_parent, second_parent = (
                self._normal_alias_runs(root)
            )
            project_relative = Path("video_author/attempt_01/project")
            first_project = first_run / project_relative
            second_project = second_run / project_relative
            first_project.mkdir(parents=True)
            second_project.mkdir(parents=True)
            (first_project / "project-marker.txt").write_text(
                "A-project",
                encoding="utf-8",
            )
            (second_project / "project-marker.txt").write_text(
                "B-project",
                encoding="utf-8",
            )
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=requested_run,
                layers_dir=requested_run / "layers",
                run_id="run-1",
            )
            injected_staging: list[Path] = []

            def retarget_then_deliver(*, project_dir, manifest, ctx):
                del manifest
                staging_dir = ctx.run_dir / ".video-final-staging-observed"
                staging_dir.mkdir()
                (staging_dir / "marker.txt").write_text(
                    "A-staging",
                    encoding="utf-8",
                )
                injected_staging.append(
                    self._retarget_normal_alias(
                        alias_parent=alias_parent,
                        second_parent=second_parent,
                        second_run=second_run,
                        staging_name=staging_dir.name,
                    )
                )
                self.assertEqual(
                    (project_dir / "project-marker.txt").read_text(
                        encoding="utf-8"
                    ),
                    "A-project",
                )
                publish_artifact_directory(
                    staging_dir,
                    ctx.run_dir / "final",
                    artifact_name="video",
                    post_publish=lambda: None,
                )
                return SimpleNamespace(status="ok", error_message=None)

            with (
                patch.object(
                    external_video_author_module,
                    "deliver_authored_video_project",
                    side_effect=retarget_then_deliver,
                ),
                patch.object(
                    external_video_author_module,
                    "invoke_designer_tool",
                    return_value=SimpleNamespace(status="ok", error_message=None),
                ),
            ):
                with self.assertRaisesRegex(ValueError, "changed during"):
                    ExternalVideoAuthor(ctx.settings, "")._deliver_normal_candidate(
                        candidate_id="video-normal",
                        project_dir=requested_run / project_relative,
                        manifest={},
                        ctx=ctx,
                    )

            self._assert_normal_alias_publish_wrote_neither_run(
                first_run=first_run,
                second_run=second_run,
                artifact_name="video",
                injected_staging=injected_staging[0],
            )

    @unittest.skipUnless(os.name == "posix", "symlink hardening uses POSIX dirfds")
    def test_selection_control_files_reject_external_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            outside = root / "outside"
            outside.mkdir()
            journal = AttemptSelectionJournal(
                run_id="run-1",
                candidate_id="landing-attempt-01-ready",
                candidate_sha256="a" * 64,
                source_attempt=1,
                idempotency_key="symlink-test",
                state="requested",
                updated_at="2026-08-03T00:00:00+00:00",
            )

            linked_run = root / "run-1"
            linked_run.symlink_to(outside, target_is_directory=True)
            with self.assertRaises((OSError, ValueError)):
                write_selection_journal(linked_run, journal)
            with self.assertRaises((OSError, ValueError)):
                with attempt_candidates_module.attempt_promotion_lease(linked_run):
                    self.fail("lease entered through a symlinked run directory")
            self.assertFalse((outside / "attempt_candidates" / "selection.json").exists())

            linked_run.unlink()
            linked_run.mkdir()
            control_dir = linked_run / "attempt_candidates"
            control_dir.symlink_to(outside, target_is_directory=True)
            with self.assertRaises((OSError, ValueError)):
                write_selection_journal(linked_run, journal)
            with self.assertRaises((OSError, ValueError)):
                with attempt_selection_module.normal_promotion_lease(
                    run_dir=linked_run,
                    candidate_id="normal-candidate",
                ):
                    self.fail("lease entered through a symlinked control directory")
            self.assertFalse((outside / "selection.json").exists())
            self.assertFalse((outside / ".promotion.lock").exists())

    @unittest.skipUnless(os.name == "posix", "TOCTOU hardening uses POSIX dirfds")
    def test_selection_journal_rename_is_anchored_against_directory_swap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "run-1"
            control_dir = run_dir / "attempt_candidates"
            outside = root / "outside"
            control_dir.mkdir(parents=True)
            outside.mkdir()
            moved_control = run_dir / "attempt_candidates-before-swap"
            journal = AttemptSelectionJournal(
                run_id=run_dir.name,
                candidate_id="landing-attempt-01-ready",
                candidate_sha256="b" * 64,
                source_attempt=1,
                idempotency_key="toctou-test",
                state="requested",
                updated_at="2026-08-03T00:00:00+00:00",
            )
            original_replace = os.replace
            swapped = False

            def replace_after_directory_swap(src, dst, *args, **kwargs):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    control_dir.rename(moved_control)
                    control_dir.symlink_to(outside, target_is_directory=True)
                    if not kwargs.get("src_dir_fd"):
                        source_name = Path(src).name
                        original_replace(
                            moved_control / source_name,
                            outside / source_name,
                        )
                return original_replace(src, dst, *args, **kwargs)

            with patch(
                "autodesign.attempt_candidates.os.replace",
                side_effect=replace_after_directory_swap,
            ):
                try:
                    write_selection_journal(run_dir, journal)
                except (OSError, ValueError):
                    pass

            self.assertTrue(swapped)
            self.assertFalse(
                (outside / "selection.json").exists(),
                "selection journal escaped through a swapped control directory",
            )

    @unittest.skipUnless(os.name == "posix", "cross-process lease race uses flock")
    def test_replacing_local_lock_leaf_cannot_create_a_second_lease(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "run-1"
            run_dir.mkdir()
            first_entered = root / "first-entered"
            second_entered = root / "second-entered"
            release_first = root / "release-first"
            worker_script = root / "lease_worker.py"
            worker_script.write_text(
                textwrap.dedent(
                    """
                    import sys
                    import time
                    from pathlib import Path

                    from autodesign.attempt_candidates import attempt_promotion_lease

                    run_dir = Path(sys.argv[1])
                    entered = Path(sys.argv[2])
                    release = Path(sys.argv[3]) if sys.argv[3] else None
                    with attempt_promotion_lease(run_dir):
                        entered.touch()
                        while release is not None and not release.exists():
                            time.sleep(0.005)
                    """
                ),
                encoding="utf-8",
            )
            first = subprocess.Popen(
                [
                    sys.executable,
                    str(worker_script),
                    str(run_dir),
                    str(first_entered),
                    str(release_first),
                ],
                cwd=Path(__file__).resolve().parents[1],
            )
            second: subprocess.Popen[bytes] | None = None
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not first_entered.is_file():
                    time.sleep(0.01)
                self.assertTrue(first_entered.is_file())
                control_dir = run_dir / "attempt_candidates"
                lock_path = control_dir / ".promotion.lock"
                if lock_path.exists():
                    lock_path.rename(control_dir / ".promotion.lock.replaced")
                lock_path.touch()
                second = subprocess.Popen(
                    [
                        sys.executable,
                        str(worker_script),
                        str(run_dir),
                        str(second_entered),
                        "",
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                )
                time.sleep(0.25)
                self.assertFalse(
                    second_entered.is_file(),
                    "replacement lock inode admitted a simultaneous critical section",
                )
                release_first.touch()
                first.wait(timeout=5)
                second.wait(timeout=5)
                self.assertEqual(first.returncode, 0)
                self.assertEqual(second.returncode, 0)
                self.assertTrue(second_entered.is_file())
            finally:
                release_first.touch()
                for worker in (first, second):
                    if worker is not None and worker.poll() is None:
                        worker.kill()
                        worker.wait(timeout=3)

    @unittest.skipUnless(os.name == "posix", "cross-process lease race uses flock")
    def test_replacing_real_coordination_leaf_cannot_create_a_second_lease(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "run-1"
            run_dir.mkdir()
            lock_path = attempt_candidates_module._coordination_lock_path(run_dir)
            lock_path.touch()
            first_entered = root / "real-first-entered"
            second_entered = root / "real-second-entered"
            release_first = root / "real-release-first"
            worker_script = root / "real_lease_worker.py"
            worker_script.write_text(
                textwrap.dedent(
                    """
                    import sys
                    import time
                    from pathlib import Path

                    from autodesign.attempt_candidates import attempt_promotion_lease

                    run_dir = Path(sys.argv[1])
                    entered = Path(sys.argv[2])
                    release = Path(sys.argv[3]) if sys.argv[3] else None
                    with attempt_promotion_lease(run_dir):
                        entered.touch()
                        while release is not None and not release.exists():
                            time.sleep(0.005)
                    """
                ),
                encoding="utf-8",
            )
            first = subprocess.Popen(
                [
                    sys.executable,
                    str(worker_script),
                    str(run_dir),
                    str(first_entered),
                    str(release_first),
                ],
                cwd=Path(__file__).resolve().parents[1],
            )
            second: subprocess.Popen[bytes] | None = None
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not first_entered.is_file():
                    time.sleep(0.01)
                self.assertTrue(first_entered.is_file())
                self.assertTrue(lock_path.is_file())
                replaced_lock = lock_path.with_name(f"{lock_path.name}.replaced")
                lock_path.rename(replaced_lock)
                lock_path.touch()
                second = subprocess.Popen(
                    [
                        sys.executable,
                        str(worker_script),
                        str(run_dir),
                        str(second_entered),
                        "",
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                )
                time.sleep(0.25)
                self.assertFalse(
                    second_entered.is_file(),
                    "replacement coordination inode admitted a simultaneous lease",
                )
                release_first.touch()
                first.wait(timeout=5)
                second.wait(timeout=5)
                self.assertEqual(first.returncode, 0)
                self.assertEqual(second.returncode, 0)
                self.assertTrue(second_entered.is_file())
            finally:
                release_first.touch()
                for worker in (first, second):
                    if worker is not None and worker.poll() is None:
                        worker.kill()
                        worker.wait(timeout=3)

    @unittest.skipUnless(os.name == "posix", "cross-process lease race uses flock")
    def test_replacing_real_coordination_directory_cannot_create_a_second_lease(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "run-1"
            run_dir.mkdir()
            lock_path = attempt_candidates_module._coordination_lock_path(run_dir)
            lease_directory = lock_path.parent
            replaced_directory = lease_directory.with_name(
                f"{lease_directory.name}.replaced-{run_dir.stat().st_ino}"
            )
            first_entered = root / "directory-first-entered"
            second_entered = root / "directory-second-entered"
            release_first = root / "directory-release-first"
            worker_script = root / "directory_lease_worker.py"
            worker_script.write_text(
                textwrap.dedent(
                    """
                    import sys
                    import time
                    from pathlib import Path

                    from autodesign.attempt_candidates import attempt_promotion_lease

                    run_dir = Path(sys.argv[1])
                    entered = Path(sys.argv[2])
                    release = Path(sys.argv[3]) if sys.argv[3] else None
                    with attempt_promotion_lease(run_dir):
                        entered.touch()
                        while release is not None and not release.exists():
                            time.sleep(0.005)
                    """
                ),
                encoding="utf-8",
            )
            first = subprocess.Popen(
                [
                    sys.executable,
                    str(worker_script),
                    str(run_dir),
                    str(first_entered),
                    str(release_first),
                ],
                cwd=Path(__file__).resolve().parents[1],
            )
            second: subprocess.Popen[bytes] | None = None
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not first_entered.is_file():
                    time.sleep(0.01)
                self.assertTrue(first_entered.is_file())
                lease_directory.rename(replaced_directory)
                lease_directory.mkdir(mode=0o700)
                second = subprocess.Popen(
                    [
                        sys.executable,
                        str(worker_script),
                        str(run_dir),
                        str(second_entered),
                        "",
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                )
                time.sleep(0.25)
                self.assertFalse(
                    second_entered.is_file(),
                    "replacement coordination directory admitted a simultaneous lease",
                )
                release_first.touch()
                first.wait(timeout=5)
                second.wait(timeout=5)
                self.assertEqual(first.returncode, 0)
                self.assertEqual(second.returncode, 0)
                self.assertTrue(second_entered.is_file())
            finally:
                release_first.touch()
                for worker in (first, second):
                    if worker is not None and worker.poll() is None:
                        worker.kill()
                        worker.wait(timeout=3)
                if lease_directory.is_dir():
                    shutil.rmtree(lease_directory)
                if replaced_directory.is_dir():
                    replaced_directory.rename(lease_directory)

    @unittest.skipUnless(os.name == "posix", "directory aliases use POSIX symlinks")
    def test_parent_directory_aliases_share_one_promotion_lease(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            real_parent = root / "real-parent"
            run_dir = real_parent / "run-1"
            run_dir.mkdir(parents=True)
            alias_parent = root / "alias-parent"
            alias_parent.symlink_to(real_parent, target_is_directory=True)
            alias_run_dir = alias_parent / "run-1"
            first_entered = threading.Event()
            second_entered = threading.Event()
            release_first = threading.Event()
            errors: list[BaseException] = []

            def hold_first_lease() -> None:
                try:
                    with attempt_candidates_module.attempt_promotion_lease(run_dir):
                        first_entered.set()
                        release_first.wait(5)
                except BaseException as exc:  # pragma: no cover - assertion reports it
                    errors.append(exc)

            def enter_second_lease() -> None:
                try:
                    with attempt_candidates_module.attempt_promotion_lease(
                        alias_run_dir
                    ):
                        second_entered.set()
                except BaseException as exc:  # pragma: no cover - assertion reports it
                    errors.append(exc)

            first = threading.Thread(target=hold_first_lease)
            second = threading.Thread(target=enter_second_lease)
            first.start()
            try:
                self.assertTrue(first_entered.wait(5))
                second.start()
                self.assertFalse(
                    second_entered.wait(0.25),
                    "a parent-directory alias admitted a simultaneous lease",
                )
            finally:
                release_first.set()
                first.join(timeout=5)
                if second.ident is not None:
                    second.join(timeout=5)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertTrue(second_entered.is_set())
            self.assertEqual(errors, [])

    @unittest.skipUnless(os.name == "posix", "directory aliases use POSIX symlinks")
    def test_parent_alias_retarget_cannot_open_a_run_under_the_old_lease(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first_run = root / "first-parent" / "run-1"
            second_run = root / "second-parent" / "run-1"
            first_run.mkdir(parents=True)
            second_run.mkdir(parents=True)
            alias_parent = root / "alias-parent"
            alias_parent.symlink_to(first_run.parent, target_is_directory=True)
            alias_run = alias_parent / "run-1"
            first_entered = threading.Event()
            second_started = threading.Event()
            second_entered = threading.Event()
            second_finished = threading.Event()
            third_entered = threading.Event()
            release_first = threading.Event()
            release_third = threading.Event()
            errors: list[BaseException] = []

            def hold_first_lease() -> None:
                with attempt_candidates_module.attempt_promotion_lease(first_run):
                    first_entered.set()
                    release_first.wait(5)

            def enter_alias_lease() -> None:
                second_started.set()
                try:
                    with attempt_candidates_module.attempt_promotion_lease(alias_run):
                        second_entered.set()
                except ValueError:
                    pass
                except BaseException as exc:  # pragma: no cover - assertion reports it
                    errors.append(exc)
                finally:
                    second_finished.set()

            def hold_retargeted_run_lease() -> None:
                with attempt_candidates_module.attempt_promotion_lease(second_run):
                    third_entered.set()
                    release_third.wait(5)

            first = threading.Thread(target=hold_first_lease)
            second = threading.Thread(target=enter_alias_lease)
            third = threading.Thread(target=hold_retargeted_run_lease)
            first.start()
            try:
                self.assertTrue(first_entered.wait(5))
                second.start()
                self.assertTrue(second_started.wait(5))
                time.sleep(0.1)
                self.assertFalse(second_entered.is_set())
                alias_parent.unlink()
                alias_parent.symlink_to(second_run.parent, target_is_directory=True)
                third.start()
                self.assertTrue(third_entered.wait(5))
                release_first.set()
                self.assertTrue(second_finished.wait(5))
                self.assertFalse(
                    second_entered.is_set(),
                    "retargeted alias entered under the old run's lease",
                )
            finally:
                release_first.set()
                release_third.set()
                for worker in (first, second, third):
                    if worker.ident is not None:
                        worker.join(timeout=5)

            self.assertEqual(errors, [])
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertFalse(third.is_alive())

    @unittest.skipUnless(os.name == "posix", "directory aliases use POSIX symlinks")
    def test_journal_write_rejects_retargeted_alias_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first_run = root / "first-parent" / "run-1"
            second_run = root / "second-parent" / "run-1"
            first_run.mkdir(parents=True)
            second_run.mkdir(parents=True)
            alias_parent = root / "alias-parent"
            alias_parent.symlink_to(first_run.parent, target_is_directory=True)
            alias_run = alias_parent / "run-1"
            journal = AttemptSelectionJournal(
                run_id="run-1",
                candidate_id="landing-attempt-01-ready",
                candidate_sha256="a" * 64,
                source_attempt=1,
                idempotency_key="retargeted-journal",
                state="requested",
                updated_at="2026-08-03T00:00:00+00:00",
            )

            rejected_before_write = False
            with self.assertRaisesRegex(ValueError, "changed during access"):
                with attempt_candidates_module.attempt_promotion_lease(alias_run):
                    alias_parent.unlink()
                    alias_parent.symlink_to(
                        second_run.parent, target_is_directory=True
                    )
                    with self.assertRaisesRegex(ValueError, "changed during lease"):
                        write_selection_journal(alias_run, journal)
                    rejected_before_write = True
                    self.assertFalse(
                        (first_run / "attempt_candidates" / "selection.json").exists()
                    )
                    self.assertFalse(
                        (second_run / "attempt_candidates" / "selection.json").exists()
                    )
            self.assertTrue(rejected_before_write)

    @unittest.skipUnless(os.name == "posix", "directory aliases use POSIX symlinks")
    def test_transaction_write_rejects_retargeted_alias_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first_run = root / "first-parent" / "run-1"
            second_run = root / "second-parent" / "run-1"
            first_run.mkdir(parents=True)
            second_run.mkdir(parents=True)
            alias_parent = root / "alias-parent"
            alias_parent.symlink_to(first_run.parent, target_is_directory=True)
            alias_run = alias_parent / "run-1"

            rejected_before_write = False
            with self.assertRaisesRegex(ValueError, "changed during access"):
                with attempt_candidates_module.attempt_promotion_lease(alias_run):
                    alias_parent.unlink()
                    alias_parent.symlink_to(
                        second_run.parent, target_is_directory=True
                    )
                    with self.assertRaisesRegex(ValueError, "changed during lease"):
                        write_selection_adapter_transaction(
                            alias_run,
                            {
                                "run_id": "run-1",
                                "candidate_id": "landing-attempt-01-ready",
                                "phase": "started",
                            },
                        )
                    rejected_before_write = True
                    self.assertFalse(
                        (
                            first_run
                            / "attempt_candidates"
                            / "selection_adapter_transaction.json"
                        ).exists()
                    )
                    self.assertFalse(
                        (
                            second_run
                            / "attempt_candidates"
                            / "selection_adapter_transaction.json"
                        ).exists()
                    )
            self.assertTrue(rejected_before_write)

    @unittest.skipUnless(os.name == "posix", "directory aliases use POSIX symlinks")
    def test_selected_artifact_publish_rejects_retargeted_alias_before_writing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first_run, candidate = self._candidate(root / "first-parent")
            second_run = root / "second-parent" / "run-1"
            second_run.mkdir(parents=True)
            alias_parent = root / "alias-parent"
            alias_parent.symlink_to(first_run.parent, target_is_directory=True)
            alias_run = alias_parent / "run-1"
            request_attempt_selection(
                run_dir=alias_run,
                run_id="run-1",
                attempt=1,
                expected_candidate_sha256=candidate.source_sha256,
                idempotency_key="retargeted-publish",
            )
            staged_name = ".landing-final-staging-retargeted"
            second_staging = second_run / staged_name
            second_staging.mkdir()
            (second_staging / "index.html").write_text(
                "must not publish",
                encoding="utf-8",
            )
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=alias_run,
                layers_dir=alias_run / "layers",
                run_id="run-1",
            )

            def retarget_then_publish(promoter_ctx, _candidate) -> None:
                staging_dir = promoter_ctx.run_dir / staged_name
                final_dir = promoter_ctx.run_dir / "final"
                alias_parent.unlink()
                alias_parent.symlink_to(second_run.parent, target_is_directory=True)
                publish_artifact_directory(
                    staging_dir,
                    final_dir,
                    artifact_name="landing",
                    post_publish=lambda: None,
                )

            try:
                promote_pending_selection(ctx, promoter=retarget_then_publish)
            except ValueError:
                pass

            self.assertFalse((first_run / "final").exists())
            self.assertFalse((second_run / "final").exists())
            self.assertFalse(
                (second_run / ".landing-final-promotion.json").exists()
            )

    @unittest.skipUnless(os.name == "posix", "directory aliases use POSIX symlinks")
    def test_guarded_path_without_copied_validator_still_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first_run = root / "first-parent" / "run-1"
            second_run = root / "second-parent" / "run-1"
            first_run.mkdir(parents=True)
            second_run.mkdir(parents=True)
            alias_parent = root / "alias-parent"
            alias_parent.symlink_to(first_run.parent, target_is_directory=True)
            alias_run = alias_parent / "run-1"

            with self.assertRaisesRegex(ValueError, "changed during access"):
                with attempt_candidates_module.attempt_promotion_lease(
                    alias_run
                ) as leased_run_dir:
                    # Python 3.10/3.11 pathlib can derive subclass children
                    # without copying instance attributes.
                    del leased_run_dir._lease_validator
                    alias_parent.unlink()
                    alias_parent.symlink_to(
                        second_run.parent, target_is_directory=True
                    )
                    with self.assertRaisesRegex(ValueError, "changed during lease"):
                        os.fspath(leased_run_dir / "final")

    def test_guarded_promotion_path_cannot_escape_its_lease(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            run_dir.mkdir()

            with attempt_candidates_module.attempt_promotion_lease(
                run_dir
            ) as leased_run_dir:
                guarded_final = leased_run_dir / "final"

            with self.assertRaisesRegex(ValueError, "no longer active"):
                os.fspath(guarded_final)

    @unittest.skipUnless(os.name == "posix", "descriptor aliases use POSIX links")
    def test_run_member_accessor_rejects_unrelated_descriptor_alias_in_lease(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "run-1"
            run_dir.mkdir()
            (run_dir / "payload.txt").write_text("retained", encoding="utf-8")
            alias_root = root / "proc" / "self" / "fd" / "999"
            alias_root.parent.mkdir(parents=True)
            alias_root.symlink_to(run_dir, target_is_directory=True)

            with attempt_candidates_module.attempt_promotion_lease(
                run_dir
            ) as leased_run_dir:
                with attempt_candidates_module.secure_run_member_access(
                    leased_run_dir
                ) as accessor:
                    with self.assertRaisesRegex(
                        ValueError,
                        "outside|exact|unrelated",
                    ):
                        accessor.read_bytes(
                            alias_root / "payload.txt",
                            label="unrelated descriptor member",
                        )

    def test_run_member_accessor_rejects_both_traversal_grammars_and_nul(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            run_dir.mkdir()
            (run_dir / "payload.txt").write_text("retained", encoding="utf-8")

            with attempt_candidates_module.secure_run_member_access(
                run_dir
            ) as accessor:
                for unsafe_value in (
                    "../payload.txt",
                    r"..\payload.txt",
                    "payload\x00.txt",
                ):
                    with (
                        self.subTest(value=repr(unsafe_value)),
                        self.assertRaisesRegex(ValueError, "invalid|traversal|inside"),
                    ):
                        accessor.read_bytes(
                            unsafe_value,
                            label="unsafe member",
                        )
                if os.name == "posix":
                    unrelated_root = Path(raw) / "unrelated-run-alias"
                    unrelated_root.symlink_to(run_dir, target_is_directory=True)
                    with self.assertRaisesRegex(
                        ValueError,
                        "outside the current run",
                    ):
                        accessor.read_bytes(
                            unrelated_root / "payload.txt",
                            label="unrelated direct alias",
                        )

    def test_run_member_accessor_rejects_windows_anchors_before_member_open(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            run_dir.mkdir()
            (run_dir / "payload.txt").write_text("retained", encoding="utf-8")
            windows_anchors = (
                r"C:foo\bar",
                r"\foo\bar",
                r"C:\foo",
                r"\\server\share\file",
                r"\\?\C:\foo",
                r"\\.\C:\foo",
                r"\\?\UNC\server\share\file",
            )

            with attempt_candidates_module.secure_run_member_access(
                run_dir
            ) as accessor:
                with (
                    patch.object(
                        accessor,
                        "_read_from_fd",
                        side_effect=AssertionError("member fd open was reached"),
                    ) as read_from_fd,
                    patch.object(
                        accessor,
                        "_read_from_path",
                        side_effect=AssertionError("member path open was reached"),
                    ) as read_from_path,
                ):
                    for base in (Path("project"), None):
                        for unsafe_value in windows_anchors:
                            with (
                                self.subTest(
                                    base=None if base is None else base.as_posix(),
                                    value=unsafe_value,
                                ),
                                self.assertRaisesRegex(
                                    ValueError,
                                    "windows|anchor|relative|native|outside",
                                ),
                            ):
                                accessor.read_bytes(
                                    unsafe_value,
                                    label="Windows-anchored member",
                                    base=base,
                                )
                    read_from_fd.assert_not_called()
                    read_from_path.assert_not_called()

                retained = accessor.read_bytes(
                    run_dir / "payload.txt",
                    label="native absolute member",
                )
                self.assertEqual(retained.data, b"retained")

    @unittest.skipUnless(os.name == "posix", "mocked Windows reparse uses a symlink")
    def test_windows_run_member_accessor_holds_guard_and_rejects_nested_reparse(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "run-1"
            run_dir.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "payload.txt").write_text("external", encoding="utf-8")
            (run_dir / "nested").symlink_to(outside, target_is_directory=True)
            guard_events: list[tuple[str, Path]] = []

            @contextmanager
            def replacement_guard(path: Path):
                guard_events.append(("enter", path))
                try:
                    yield 41
                finally:
                    guard_events.append(("exit", path))

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
                    attempt_candidates_module,
                    "_windows_directory_replacement_guard",
                    side_effect=replacement_guard,
                ),
            ):
                with attempt_candidates_module.secure_run_member_access(
                    run_dir
                ) as accessor:
                    with self.assertRaisesRegex(
                        ValueError,
                        "reparse|link|no-follow|nofollow",
                    ):
                        accessor.read_bytes(
                            run_dir / "nested" / "payload.txt",
                            label="nested Windows member",
                        )

            self.assertEqual(
                guard_events,
                [("enter", run_dir.resolve()), ("exit", run_dir.resolve())],
            )

    def test_distinct_case_sensitive_runs_do_not_share_a_promotion_lease(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            upper_run = root / "CaseRun"
            lower_run = root / "caserun"
            upper_run.mkdir()
            try:
                lower_run.mkdir()
            except FileExistsError:
                self.skipTest("temporary filesystem is case-insensitive")
            if os.path.samefile(upper_run, lower_run):
                self.skipTest("temporary filesystem is case-insensitive")

            first_entered = threading.Event()
            second_entered = threading.Event()
            release_first = threading.Event()

            def hold_first_lease() -> None:
                with attempt_candidates_module.attempt_promotion_lease(upper_run):
                    first_entered.set()
                    release_first.wait(5)

            def enter_second_lease() -> None:
                with attempt_candidates_module.attempt_promotion_lease(lower_run):
                    second_entered.set()

            first = threading.Thread(target=hold_first_lease)
            second = threading.Thread(target=enter_second_lease)
            first.start()
            try:
                self.assertTrue(first_entered.wait(5))
                second.start()
                self.assertTrue(
                    second_entered.wait(0.25),
                    "distinct case-sensitive runs collided on one lease",
                )
            finally:
                release_first.set()
                first.join(timeout=5)
                if second.ident is not None:
                    second.join(timeout=5)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())

    def test_distinct_case_sensitive_directory_identities_have_distinct_locks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            upper_run = root / "CaseRun"
            lower_run = root / "caserun"
            first_identity_dir = root / "identity-one"
            second_identity_dir = root / "identity-two"
            first_identity_dir.mkdir()
            second_identity_dir.mkdir()
            first_metadata = os.lstat(first_identity_dir)
            second_metadata = os.lstat(second_identity_dir)
            original_lstat = attempt_candidates_module._portable_lstat

            def case_sensitive_lstat(path: Path):
                if path == upper_run:
                    return first_metadata
                if path == lower_run:
                    return second_metadata
                return original_lstat(path)

            with patch.object(
                attempt_candidates_module,
                "_portable_lstat",
                side_effect=case_sensitive_lstat,
            ):
                upper_lock = attempt_candidates_module._coordination_lock_path(
                    upper_run
                )
                lower_lock = attempt_candidates_module._coordination_lock_path(
                    lower_run
                )

            self.assertNotEqual(upper_lock, lower_lock)

    def test_case_aliases_on_case_insensitive_filesystems_share_one_lease(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            upper_run = root / "CaseRun"
            lower_run = root / "caserun"
            upper_run.mkdir()
            if not lower_run.exists() or not os.path.samefile(upper_run, lower_run):
                self.skipTest("temporary filesystem is case-sensitive")

            self.assertEqual(
                attempt_candidates_module._coordination_lock_path(upper_run),
                attempt_candidates_module._coordination_lock_path(lower_run),
            )

    def test_no_getuid_namespace_uses_stable_windows_user_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "run-1"
            run_dir.mkdir()
            portable_os = SimpleNamespace(
                name="nt",
                path=os.path,
                fspath=os.fspath,
                lstat=os.lstat,
            )
            current_sid = ["S-1-5-21-111-222-333-1001"]

            def create_private_directory(path: Path, _sid: str) -> None:
                path.mkdir(exist_ok=True)

            with (
                patch.object(attempt_candidates_module, "os", portable_os),
                patch.object(
                    attempt_candidates_module.tempfile,
                    "gettempdir",
                    return_value=raw,
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_current_user_sid",
                    side_effect=lambda: current_sid[0],
                    create=True,
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_path_owner_sid",
                    side_effect=lambda _path: current_sid[0],
                    create=True,
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_create_private_directory",
                    side_effect=create_private_directory,
                    create=True,
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_directory_is_private",
                    return_value=True,
                    create=True,
                ),
            ):
                first = attempt_candidates_module._coordination_lock_path(run_dir)
                repeated = attempt_candidates_module._coordination_lock_path(run_dir)
                current_sid[0] = "S-1-5-21-111-222-333-1002"
                second_user = attempt_candidates_module._coordination_lock_path(run_dir)

            self.assertEqual(first, repeated)
            self.assertNotEqual(first.parent, second_user.parent)
            self.assertNotIn("portable", first.parent.name)
            self.assertNotIn("portable", second_user.parent.name)

    def test_windows_named_mutex_name_is_local_stable_and_scoped(self) -> None:
        current_user = "S-1-5-21-111-222-333-1001"

        first = attempt_candidates_module._windows_named_mutex_name(
            current_user,
            (101, 202),
        )
        repeated = attempt_candidates_module._windows_named_mutex_name(
            current_user,
            (101, 202),
        )
        other_run = attempt_candidates_module._windows_named_mutex_name(
            current_user,
            (101, 203),
        )
        other_user = attempt_candidates_module._windows_named_mutex_name(
            "S-1-5-21-111-222-333-1002",
            (101, 202),
        )

        self.assertEqual(first, repeated)
        self.assertTrue(first.startswith("Local\\"))
        self.assertFalse(first.startswith("Global\\"))
        self.assertRegex(
            first,
            r"^Local\\AutoDesignAttemptPromotion-[0-9a-f]{64}$",
        )
        self.assertNotIn(current_user, first)
        self.assertNotEqual(first, other_run)
        self.assertNotEqual(first, other_user)

    def test_windows_sid_lookup_failure_fails_closed(self) -> None:
        portable_os = SimpleNamespace(name="nt")
        with (
            patch.object(attempt_candidates_module, "os", portable_os),
            patch.object(
                attempt_candidates_module,
                "_windows_current_user_sid",
                side_effect=OSError("SID unavailable"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "SID unavailable"):
                attempt_candidates_module._coordination_owner_identity()

    def test_windows_named_mutex_accepts_normal_and_abandoned_ownership(
        self,
    ) -> None:
        for wait_result in (0x00000000, 0x00000080):
            with self.subTest(wait_result=wait_result):
                events: list[object] = []

                def create_mutex(_attributes, initial_owner, name):
                    events.append(("create", initial_owner, name))
                    return 41

                def wait_for_single_object(handle, timeout):
                    events.append(("wait", handle, timeout))
                    return wait_result

                def release_mutex(handle):
                    events.append(("release", handle))
                    return True

                def close_handle(handle):
                    events.append(("close", handle))
                    return True

                api = (
                    create_mutex,
                    wait_for_single_object,
                    release_mutex,
                    close_handle,
                    lambda: 0,
                )
                with patch.object(
                    attempt_candidates_module,
                    "_windows_mutex_api",
                    return_value=api,
                    create=True,
                ):
                    with attempt_candidates_module._windows_named_mutex_lease(
                        "Local\\AutoDesignAttemptPromotion-test"
                    ):
                        events.append("inside")

                self.assertEqual(
                    events,
                    [
                        (
                            "create",
                            False,
                            "Local\\AutoDesignAttemptPromotion-test",
                        ),
                        ("wait", 41, 0xFFFFFFFF),
                        "inside",
                        ("release", 41),
                        ("close", 41),
                    ],
                )

    def test_windows_named_mutex_fails_closed_for_wait_errors(self) -> None:
        for wait_result, expected_error in (
            (0x00000102, TimeoutError),
            (0xFFFFFFFF, OSError),
            (0x00000007, OSError),
        ):
            with self.subTest(wait_result=wait_result):
                released: list[int] = []
                closed: list[int] = []
                api = (
                    lambda _attributes, _initial_owner, _name: 52,
                    lambda _handle, _timeout: wait_result,
                    lambda handle: released.append(handle) or True,
                    lambda handle: closed.append(handle) or True,
                    lambda: 123,
                )
                with patch.object(
                    attempt_candidates_module,
                    "_windows_mutex_api",
                    return_value=api,
                    create=True,
                ):
                    with self.assertRaises(expected_error):
                        with attempt_candidates_module._windows_named_mutex_lease(
                            "Local\\AutoDesignAttemptPromotion-test"
                        ):
                            self.fail("failed wait must not enter the lease")

                self.assertEqual(released, [])
                self.assertEqual(closed, [52])

    def test_windows_named_mutex_closes_handle_on_body_and_release_errors(
        self,
    ) -> None:
        for release_succeeds, expected_error in (
            (True, RuntimeError),
            (False, OSError),
        ):
            with self.subTest(release_succeeds=release_succeeds):
                released: list[int] = []
                closed: list[int] = []

                def release_mutex(handle):
                    released.append(handle)
                    return release_succeeds

                api = (
                    lambda _attributes, _initial_owner, _name: 63,
                    lambda _handle, _timeout: 0x00000000,
                    release_mutex,
                    lambda handle: closed.append(handle) or True,
                    lambda: 123,
                )
                with patch.object(
                    attempt_candidates_module,
                    "_windows_mutex_api",
                    return_value=api,
                ):
                    with self.assertRaises(expected_error):
                        with attempt_candidates_module._windows_named_mutex_lease(
                            "Local\\AutoDesignAttemptPromotion-test"
                        ):
                            raise RuntimeError("lease body failed")

                self.assertEqual(released, [63])
                self.assertEqual(closed, [63])

    def test_windows_directory_guard_denies_delete_share_and_closes_handle(
        self,
    ) -> None:
        created: list[tuple[object, ...]] = []
        closed: list[int] = []

        def create_file(*args):
            created.append(args)
            return 71

        with patch.object(
            attempt_candidates_module,
            "_windows_directory_guard_api",
            return_value=(
                create_file,
                lambda handle: closed.append(handle) or True,
                lambda: 0,
            ),
        ):
            with attempt_candidates_module._windows_directory_replacement_guard(
                Path("C:/runs/run-1")
            ) as handle:
                self.assertEqual(handle, 71)

        self.assertEqual(closed, [71])
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][2], 0x00000001 | 0x00000002)
        self.assertEqual(created[0][2] & 0x00000004, 0)
        self.assertEqual(created[0][4], 3)
        self.assertEqual(
            created[0][5],
            0x02000000 | 0x00200000,
        )

    def test_windows_directory_guard_rejects_pointer_sized_invalid_handle(
        self,
    ) -> None:
        import ctypes

        invalid_handle = ctypes.c_void_p(-1).value
        with patch.object(
            attempt_candidates_module,
            "_windows_directory_guard_api",
            return_value=(
                lambda *_args: invalid_handle,
                lambda _handle: True,
                lambda: 5,
            ),
        ):
            with self.assertRaisesRegex(OSError, "directory guard failed"):
                with attempt_candidates_module._windows_directory_replacement_guard(
                    Path("C:/runs/run-1")
                ):
                    self.fail("invalid Windows directory handle was accepted")

    def test_windows_stable_lease_uses_named_mutex_not_lock_leaf(self) -> None:
        run_identity = (101, 202)
        mutex_name = "Local\\AutoDesignAttemptPromotion-test"
        portable_os = SimpleNamespace(name="nt")
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            run_dir.mkdir()
            with (
                patch.object(attempt_candidates_module, "os", portable_os),
                patch.object(
                    attempt_candidates_module,
                    "_coordination_lock_details",
                    return_value=(Path("unused-lock-leaf"), run_identity),
                ),
                patch.object(
                    attempt_candidates_module,
                    "_coordination_owner_identity",
                    return_value=("sid", "S-1-5-21-111-222-333-1001"),
                ),
                patch.object(
                    attempt_candidates_module,
                    "promotion_run_identity",
                    return_value=run_identity,
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_named_mutex_name",
                    return_value=mutex_name,
                    create=True,
                ) as make_name,
                patch.object(
                    attempt_candidates_module,
                    "_windows_named_mutex_lease",
                    return_value=nullcontext(),
                    create=True,
                ) as acquire,
                patch.object(
                    attempt_candidates_module,
                    "_windows_directory_replacement_guard",
                    return_value=nullcontext(81),
                    create=True,
                ) as guard_directory,
            ):
                with attempt_candidates_module._stable_coordination_lease(
                    run_dir
                ) as stable_lease:
                    self.assertEqual(stable_lease.run_identity, run_identity)

        make_name.assert_called_once_with(
            "S-1-5-21-111-222-333-1001",
            run_identity,
        )
        acquire.assert_called_once_with(mutex_name)
        guard_directory.assert_called_once_with(run_dir.resolve())

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX ownership uses numeric uid")
    def test_posix_coordination_namespace_is_owned_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            run_dir.mkdir()

            namespace = attempt_candidates_module._coordination_lock_path(
                run_dir
            ).parent
            metadata = os.lstat(namespace)

            self.assertEqual(metadata.st_uid, os.getuid())
            self.assertEqual(metadata.st_mode & 0o777, 0o700)

    def test_no_getuid_namespace_rejects_wrong_windows_owner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "run-1"
            run_dir.mkdir()
            portable_os = SimpleNamespace(
                name="nt",
                path=os.path,
                fspath=os.fspath,
                lstat=os.lstat,
            )

            def create_private_directory(path: Path, _sid: str) -> None:
                path.mkdir(exist_ok=True)

            with (
                patch.object(attempt_candidates_module, "os", portable_os),
                patch.object(
                    attempt_candidates_module.tempfile,
                    "gettempdir",
                    return_value=raw,
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_current_user_sid",
                    return_value="S-1-5-21-111-222-333-1001",
                    create=True,
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_path_owner_sid",
                    return_value="S-1-5-21-111-222-333-1002",
                    create=True,
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_create_private_directory",
                    side_effect=create_private_directory,
                    create=True,
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_directory_is_private",
                    return_value=True,
                    create=True,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "wrong owner"):
                    attempt_candidates_module._coordination_lock_path(run_dir)

    def test_no_getuid_namespace_rejects_non_private_windows_acl(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "run-1"
            run_dir.mkdir()
            portable_os = SimpleNamespace(
                name="nt",
                path=os.path,
                fspath=os.fspath,
                lstat=os.lstat,
            )

            def create_private_directory(path: Path, _sid: str) -> None:
                path.mkdir(exist_ok=True)

            with (
                patch.object(attempt_candidates_module, "os", portable_os),
                patch.object(
                    attempt_candidates_module.tempfile,
                    "gettempdir",
                    return_value=raw,
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_current_user_sid",
                    return_value="S-1-5-21-111-222-333-1001",
                    create=True,
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_create_private_directory",
                    side_effect=create_private_directory,
                    create=True,
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_path_owner_sid",
                    return_value="S-1-5-21-111-222-333-1001",
                    create=True,
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_directory_is_private",
                    return_value=False,
                    create=True,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "not private"):
                    attempt_candidates_module._coordination_lock_path(run_dir)

    @unittest.skipUnless(os.name == "posix", "portable fallback race uses POSIX workers")
    def test_forced_portable_directory_swap_cannot_bypass_the_lease(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "run-1"
            run_dir.mkdir()
            outside = root / "outside"
            outside.mkdir()
            first_entered = root / "portable-first-entered"
            second_entered = root / "portable-second-entered"
            first_rejected = root / "portable-first-rejected"
            second_rejected = root / "portable-second-rejected"
            release_first = root / "portable-release-first"
            first_script = root / "portable_first.py"
            first_script.write_text(
                textwrap.dedent(
                    f"""
                    import time
                    from pathlib import Path
                    import autodesign.attempt_candidates as candidates

                    candidates._SECURE_DIR_FD_AVAILABLE = False
                    try:
                        with candidates.attempt_promotion_lease(Path({str(run_dir)!r})):
                            Path({str(first_entered)!r}).touch()
                            while not Path({str(release_first)!r}).exists():
                                time.sleep(0.005)
                    except ValueError:
                        Path({str(first_rejected)!r}).touch()
                    """
                ),
                encoding="utf-8",
            )
            second_script = root / "portable_second.py"
            second_script.write_text(
                textwrap.dedent(
                    f"""
                    from pathlib import Path
                    import autodesign.attempt_candidates as candidates

                    run_dir = Path({str(run_dir)!r})
                    candidates._SECURE_DIR_FD_AVAILABLE = False
                    try:
                        with candidates.attempt_promotion_lease(run_dir):
                            Path({str(second_entered)!r}).touch()
                    except (OSError, ValueError):
                        Path({str(second_rejected)!r}).touch()
                    """
                ),
                encoding="utf-8",
            )
            first = subprocess.Popen(
                [sys.executable, str(first_script)],
                cwd=Path(__file__).resolve().parents[1],
            )
            second: subprocess.Popen[bytes] | None = None
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not first_entered.is_file():
                    time.sleep(0.01)
                self.assertTrue(first_entered.is_file())
                second = subprocess.Popen(
                    [sys.executable, str(second_script)],
                    cwd=Path(__file__).resolve().parents[1],
                )
                time.sleep(0.25)
                self.assertFalse(
                    second_entered.is_file(),
                    "portable directory swap admitted a simultaneous critical section",
                )
                control_dir = run_dir / "attempt_candidates"
                moved_control = run_dir / "attempt_candidates-before-portable-swap"
                control_dir.rename(moved_control)
                control_dir.symlink_to(outside, target_is_directory=True)
                self.assertFalse(
                    (outside / ".promotion.lock").exists(),
                    "portable fallback created its lock outside the run",
                )
                release_first.touch()
                first.wait(timeout=5)
                second.wait(timeout=5)
                self.assertEqual(first.returncode, 0)
                self.assertEqual(second.returncode, 0)
                self.assertTrue(first_rejected.is_file())
                self.assertTrue(second_rejected.is_file())
                self.assertFalse(second_entered.is_file())
            finally:
                release_first.touch()
                for worker in (first, second):
                    if worker is not None and worker.poll() is None:
                        worker.kill()
                        worker.wait(timeout=3)

    def test_published_candidate_fork_stops_source_run_without_promoting_blocked_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir, candidate = self._candidate(
                Path(raw),
                safety_state="blocked",
            )
            with patch(
                "autodesign.attempt_selection.terminate_registered_author_process",
                return_value=True,
            ) as terminate:
                outcome = complete_source_run_with_candidate_fork(
                    run_dir=run_dir,
                    run_id="run-1",
                    attempt=1,
                    expected_candidate_sha256=candidate.source_sha256,
                    artifact_id="art_candidate-fork",
                )

            self.assertEqual(outcome, "completed")
            journal = load_selection_journal(run_dir)
            self.assertEqual(journal.state, "complete")
            self.assertEqual(journal.artifact_id, "art_candidate-fork")
            terminate.assert_called_once()
            with self.assertRaises(AttemptPromotionRejected):
                assert_promotion_allowed(
                    run_dir=run_dir,
                    candidate_id="later-ready-candidate",
                )

    @unittest.skipUnless(os.name == "posix", "retarget regression uses parent symlinks")
    def test_step2c_control_operations_reject_distinct_b_retargeted_to_leased_a_before_io(
        self,
    ) -> None:
        operations = ("load", "write", "delete")
        for operation in operations:
            with self.subTest(operation=operation):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    run_a = root / "leased-parent" / "same-run-name"
                    run_b = root / "distinct-parent" / "same-run-name"
                    run_a.mkdir(parents=True)
                    run_b.mkdir(parents=True)
                    alias_parent = root / "alias-parent"
                    alias_parent.symlink_to(run_b.parent, target_is_directory=True)
                    alias_b = alias_parent / run_b.name
                    journal_a = self._selection_journal_fixture(run_id=run_a.name)
                    journal_b = journal_a.model_copy(
                        update={"idempotency_key": "distinct-b-before-retarget"}
                    )
                    updated_b = journal_b.model_copy(
                        update={"idempotency_key": "must-not-reach-leased-a"}
                    )
                    transaction_a = {"owner": "leased-a", "phase": "started"}
                    transaction_b = {"owner": "distinct-b", "phase": "started"}
                    write_selection_journal(run_a, journal_a)
                    write_selection_journal(run_b, journal_b)
                    write_selection_adapter_transaction(run_a, transaction_a)
                    write_selection_adapter_transaction(run_b, transaction_b)
                    selection_a = (
                        run_a / "attempt_candidates" / "selection.json"
                    ).read_bytes()
                    selection_b = (
                        run_b / "attempt_candidates" / "selection.json"
                    ).read_bytes()
                    adapter_a = (
                        run_a
                        / "attempt_candidates"
                        / "selection_adapter_transaction.json"
                    ).read_bytes()
                    adapter_b = (
                        run_b
                        / "attempt_candidates"
                        / "selection_adapter_transaction.json"
                    ).read_bytes()
                    control_io_marker = root / f"{operation}-reached-control-io"
                    retargeted = False

                    def retarget_before_secure_open(*_args, **_kwargs) -> None:
                        nonlocal retargeted
                        if retargeted:
                            return
                        alias_parent.unlink()
                        alias_parent.symlink_to(
                            run_a.parent,
                            target_is_directory=True,
                        )
                        retargeted = True

                    def forbidden_control_io(*_args, **_kwargs):
                        control_io_marker.write_text("called", encoding="utf-8")
                        raise AssertionError(
                            "control I/O ran after B was retargeted to leased A"
                        )

                    low_level_name = {
                        "load": "_read_opened_control_json",
                        "write": "_write_opened_control_json",
                        "delete": "_delete_opened_control_file",
                    }[operation]

                    with attempt_candidates_module.attempt_promotion_lease(run_a):
                        with (
                            patch.object(
                                attempt_candidates_module,
                                "_opened_control_context_before_open",
                                side_effect=retarget_before_secure_open,
                            ),
                            patch.object(
                                attempt_candidates_module,
                                low_level_name,
                                side_effect=forbidden_control_io,
                            ),
                        ):
                            try:
                                if operation == "load":
                                    load_selection_journal(alias_b)
                                elif operation == "write":
                                    write_selection_journal(alias_b, updated_b)
                                else:
                                    clear_selection_adapter_transaction(alias_b)
                            except (AssertionError, OSError, ValueError):
                                pass

                    self.assertTrue(retargeted)
                    self.assertFalse(
                        control_io_marker.exists(),
                        f"{operation} classified B before securely opening its root",
                    )
                    self.assertEqual(
                        (run_a / "attempt_candidates" / "selection.json").read_bytes(),
                        selection_a,
                    )
                    self.assertEqual(
                        (run_b / "attempt_candidates" / "selection.json").read_bytes(),
                        selection_b,
                    )
                    self.assertEqual(
                        (
                            run_a
                            / "attempt_candidates"
                            / "selection_adapter_transaction.json"
                        ).read_bytes(),
                        adapter_a,
                    )
                    self.assertEqual(
                        (
                            run_b
                            / "attempt_candidates"
                            / "selection_adapter_transaction.json"
                        ).read_bytes(),
                        adapter_b,
                    )

    def test_step2c_distinct_b_control_operations_keep_b_identity_while_a_is_leased(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_a = root / "leased-parent" / "same-run-name"
            run_b = root / "distinct-parent" / "same-run-name"
            run_a.mkdir(parents=True)
            run_b.mkdir(parents=True)
            journal_a = self._selection_journal_fixture(run_id=run_a.name)
            journal_b = journal_a.model_copy(
                update={"idempotency_key": "distinct-b-initial"}
            )
            updated_b = journal_b.model_copy(
                update={"idempotency_key": "distinct-b-updated"}
            )
            transaction_a = {"owner": "leased-a", "phase": "started"}
            transaction_b = {"owner": "distinct-b", "phase": "started"}
            write_selection_journal(run_a, journal_a)
            write_selection_journal(run_b, journal_b)
            write_selection_adapter_transaction(run_a, transaction_a)
            write_selection_adapter_transaction(run_b, transaction_b)

            with attempt_candidates_module.attempt_promotion_lease(run_a):
                self.assertEqual(load_selection_journal(run_b), journal_b)
                write_selection_journal(run_b, updated_b)
                self.assertEqual(
                    load_selection_adapter_transaction(run_b),
                    transaction_b,
                )
                clear_selection_adapter_transaction(run_b)

            self.assertEqual(load_selection_journal(run_a), journal_a)
            self.assertEqual(load_selection_journal(run_b), updated_b)
            self.assertEqual(
                load_selection_adapter_transaction(run_a),
                transaction_a,
            )
            self.assertIsNone(load_selection_adapter_transaction(run_b))

    def test_step2c_windows_control_operations_hold_root_and_control_guards(
        self,
    ) -> None:
        operations = ("load", "write", "delete")
        for operation in operations:
            with self.subTest(operation=operation):
                with tempfile.TemporaryDirectory() as raw:
                    run_dir = Path(raw) / "run-1"
                    run_dir.mkdir()
                    journal = self._selection_journal_fixture(run_id=run_dir.name)
                    updated = journal.model_copy(
                        update={"idempotency_key": "windows-guarded-write"}
                    )
                    write_selection_journal(run_dir, journal)
                    write_selection_adapter_transaction(
                        run_dir,
                        {"owner": "windows-guarded-delete"},
                    )
                    events: list[str] = []
                    active_guards: list[str] = []
                    canonical_run = run_dir.resolve()
                    canonical_control = canonical_run / "attempt_candidates"

                    @contextmanager
                    def guarded_directory(path: Path):
                        canonical = Path(path)
                        label = (
                            "root"
                            if canonical == canonical_run
                            else "control"
                        )
                        expected = (
                            canonical_run if label == "root" else canonical_control
                        )
                        self.assertEqual(canonical, expected)
                        events.append(f"enter:{label}")
                        active_guards.append(label)
                        try:
                            yield len(active_guards)
                        finally:
                            self.assertEqual(active_guards.pop(), label)
                            events.append(f"exit:{label}")

                    leaf_name = {
                        "load": "_read_opened_control_json",
                        "write": "_write_opened_control_json",
                        "delete": "_delete_opened_control_file",
                    }[operation]
                    real_leaf = getattr(attempt_candidates_module, leaf_name)

                    def guarded_leaf(*args, **kwargs):
                        self.assertEqual(active_guards, ["root", "control"])
                        events.append(f"leaf:{operation}")
                        return real_leaf(*args, **kwargs)

                    with (
                        patch.object(
                            attempt_candidates_module,
                            "_SECURE_DIR_FD_AVAILABLE",
                            False,
                        ),
                        patch.object(
                            attempt_candidates_module,
                            "_RUNTIME_OS",
                            SimpleNamespace(name="nt"),
                        ),
                        patch.object(
                            attempt_candidates_module,
                            "_windows_directory_replacement_guard",
                            side_effect=guarded_directory,
                        ),
                        patch.object(
                            attempt_candidates_module,
                            leaf_name,
                            side_effect=guarded_leaf,
                        ),
                    ):
                        if operation == "load":
                            self.assertEqual(load_selection_journal(run_dir), journal)
                        elif operation == "write":
                            write_selection_journal(run_dir, updated)
                        else:
                            clear_selection_adapter_transaction(run_dir)

                    self.assertEqual(
                        events,
                        [
                            "enter:root",
                            "enter:control",
                            f"leaf:{operation}",
                            "exit:control",
                            "exit:root",
                        ],
                    )

    @unittest.skipUnless(os.name == "posix", "retarget setup uses parent symlinks")
    def test_step2c_windows_control_guard_rejects_b_to_leased_a_before_leaf_io(
        self,
    ) -> None:
        operations = ("load", "write", "delete")
        for operation in operations:
            with self.subTest(operation=operation):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    run_a = root / "leased-parent" / "same-run-name"
                    run_b = root / "distinct-parent" / "same-run-name"
                    run_a.mkdir(parents=True)
                    run_b.mkdir(parents=True)
                    alias_parent = root / "alias-parent"
                    alias_parent.symlink_to(run_b.parent, target_is_directory=True)
                    alias_b = alias_parent / run_b.name
                    journal_a = self._selection_journal_fixture(run_id=run_a.name)
                    journal_b = journal_a.model_copy(
                        update={"idempotency_key": "windows-distinct-b"}
                    )
                    updated_b = journal_b.model_copy(
                        update={"idempotency_key": "must-not-reach-a"}
                    )
                    write_selection_journal(run_a, journal_a)
                    write_selection_journal(run_b, journal_b)
                    write_selection_adapter_transaction(
                        run_a,
                        {"owner": "leased-a"},
                    )
                    write_selection_adapter_transaction(
                        run_b,
                        {"owner": "distinct-b"},
                    )
                    selection_a = (
                        run_a / "attempt_candidates" / "selection.json"
                    ).read_bytes()
                    selection_b = (
                        run_b / "attempt_candidates" / "selection.json"
                    ).read_bytes()
                    adapter_a = (
                        run_a
                        / "attempt_candidates"
                        / "selection_adapter_transaction.json"
                    ).read_bytes()
                    adapter_b = (
                        run_b
                        / "attempt_candidates"
                        / "selection_adapter_transaction.json"
                    ).read_bytes()
                    leaf_marker = root / f"windows-{operation}-leaf"
                    retargeted = False

                    @contextmanager
                    def retargeting_root_guard(path: Path):
                        nonlocal retargeted
                        self.assertEqual(Path(path), run_b.resolve())
                        alias_parent.unlink()
                        alias_parent.symlink_to(
                            run_a.parent,
                            target_is_directory=True,
                        )
                        retargeted = True
                        yield 1

                    def forbidden_leaf(*_args, **_kwargs):
                        leaf_marker.write_text("called", encoding="utf-8")
                        raise AssertionError("control leaf reached after retarget")

                    leaf_name = {
                        "load": "_read_opened_control_json",
                        "write": "_write_opened_control_json",
                        "delete": "_delete_opened_control_file",
                    }[operation]
                    with attempt_candidates_module.attempt_promotion_lease(run_a):
                        with (
                            patch.object(
                                attempt_candidates_module,
                                "_SECURE_DIR_FD_AVAILABLE",
                                False,
                            ),
                            patch.object(
                                attempt_candidates_module,
                                "_RUNTIME_OS",
                                SimpleNamespace(name="nt"),
                            ),
                            patch.object(
                                attempt_candidates_module,
                                "_windows_directory_replacement_guard",
                                side_effect=retargeting_root_guard,
                            ),
                            patch.object(
                                attempt_candidates_module,
                                leaf_name,
                                side_effect=forbidden_leaf,
                            ),
                            self.assertRaisesRegex(ValueError, "changed during access"),
                        ):
                            if operation == "load":
                                load_selection_journal(alias_b)
                            elif operation == "write":
                                write_selection_journal(alias_b, updated_b)
                            else:
                                clear_selection_adapter_transaction(alias_b)

                    self.assertTrue(retargeted)
                    self.assertFalse(leaf_marker.exists())
                    self.assertEqual(
                        (run_a / "attempt_candidates" / "selection.json").read_bytes(),
                        selection_a,
                    )
                    self.assertEqual(
                        (run_b / "attempt_candidates" / "selection.json").read_bytes(),
                        selection_b,
                    )
                    self.assertEqual(
                        (
                            run_a
                            / "attempt_candidates"
                            / "selection_adapter_transaction.json"
                        ).read_bytes(),
                        adapter_a,
                    )
                    self.assertEqual(
                        (
                            run_b
                            / "attempt_candidates"
                            / "selection_adapter_transaction.json"
                        ).read_bytes(),
                        adapter_b,
                    )

    def test_step2c_non_dirfd_control_context_fails_closed_off_windows(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            run_dir.mkdir()
            with (
                patch.object(
                    attempt_candidates_module,
                    "_SECURE_DIR_FD_AVAILABLE",
                    False,
                ),
                patch.object(
                    attempt_candidates_module,
                    "_RUNTIME_OS",
                    SimpleNamespace(name="unsupported"),
                ),
                patch.object(
                    attempt_candidates_module,
                    "_windows_directory_replacement_guard",
                ) as guard,
                self.assertRaisesRegex(RuntimeError, "primitive is unavailable"),
            ):
                load_selection_journal(run_dir)
            guard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
