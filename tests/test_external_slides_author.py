from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from bs4 import BeautifulSoup

from autodesign.agents.external_slides_author import (
    ExternalSlidesAuthor,
    _compact_validation_findings,
    _expected_slide_count,
    _merge_slides_browser_audit,
    _recover_interrupted_promotion,
    _stage_runtime_skills,
    _trusted_slides_source_hashes,
    _validate_slides,
    _write_process_log,
    capture_slides_attempt_candidate,
)
from autodesign.runner import _write_runtime_skill_snapshot
from autodesign.skills.registry import SkillBundle, SkillRegistry
from autodesign.tools._contract import ToolContext
from autodesign.util.io import sha256_file
from autodesign.util.slides_visual_plan import (
    build_slides_asset_catalog,
    build_slides_visual_plan,
)


class ExternalSlidesAuthorTest(unittest.TestCase):
    def test_exhaustion_promotes_complete_deck_with_fewer_slides_as_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=14, slide_count=13)
            settings.designer_author_max_attempts = 1
            settings.designer_author_cmd = (
                f"{sys.executable} {_write_fake_agent(root, emitted_slide_count=12)}"
            )

            with patch(
                "autodesign.agents.external_slides_author.screenshot_deck_slides",
                side_effect=_fake_slide_capture,
            ):
                ExternalSlidesAuthor(settings, "system").run(
                    "Create an academic paper deck.",
                    ctx,
                )

            manifest = json.loads(
                (root / "final" / "slides_author_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["acceptance_path"], "best_available_artifact_fallback")
            self.assertEqual(manifest["slide_count"], 12)
            self.assertEqual(manifest["quality_status"], "ready_with_warnings")
            self.assertIn("slide_count_mismatch", manifest["quality_diagnostics"])

    def test_caption_clipping_candidate_is_captured_with_quality_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            attempt_dir = run_dir / "slides_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "slides.html").write_text(
                "<!doctype html><section class='deck-slide' id='slide-1'>Caption</section>",
                encoding="utf-8",
            )
            validation = {
                "kind": "external_slides_validation",
                "version": 3,
                "status": "error",
                "expected_slide_count": 1,
                "issues": [{
                    "id": "slides_content_clipped",
                    "message": "caption clipping remains",
                    "evidence": {"elements": [{"content_role": "caption"}]},
                }],
            }
            (attempt_dir / "slides_validation.json").write_text(
                json.dumps(validation),
                encoding="utf-8",
            )
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id="run",
            )

            with patch(
                "autodesign.agents.external_slides_author.screenshot_deck_slides",
                side_effect=_fake_slide_capture,
            ):
                candidate = capture_slides_attempt_candidate(
                    ctx=ctx,
                    attempt_dir=attempt_dir,
                    attempt=1,
                    max_attempts=4,
                    validation=validation,
                )

            self.assertEqual(candidate.safety_state, "ready_with_warnings")
            self.assertEqual(candidate.hard_blockers, [])
            self.assertEqual(
                [issue.issue_id for issue in candidate.warnings],
                ["slides_content_clipped"],
            )

    def test_exhaustion_promotes_most_recent_safe_deck_before_hard_regression(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=14, slide_count=12)
            settings.designer_author_max_attempts = 4
            settings.designer_author_cmd = (
                f"{sys.executable} {_write_fake_agent(root, emitted_slide_count=None)}"
            )

            def report(*issues: dict[str, object]) -> dict[str, object]:
                return {
                    "kind": "external_slides_validation",
                    "version": 3,
                    "status": "error",
                    "expected_slide_count": 12,
                    "actual_slide_count": 12,
                    "source_visual_ids": [],
                    "issues": list(issues),
                }

            quality = report(
                {
                    "id": "insufficient_visual_unit_slides",
                    "message": "expected at least 13 visual-unit slides, found 12",
                },
                {
                    "id": "slides_required_palette_id_missing",
                    "message": "document root must declare the required palette",
                },
            )
            hard = report({
                "id": "source_visual_hash_mismatch",
                "message": "source bytes changed",
            })

            with (
                patch(
                    "autodesign.agents.external_slides_author._validate_slides",
                    side_effect=[hard, quality, quality, hard, quality],
                ),
                patch(
                    "autodesign.agents.external_slides_author.screenshot_deck_slides",
                    side_effect=_fake_slide_capture,
                ),
            ):
                ExternalSlidesAuthor(settings, "system").run(
                    "Create an academic paper deck.",
                    ctx,
                )

            manifest_path = root / "final" / "slides_author_manifest.json"
            self.assertTrue(
                manifest_path.is_file(),
                "safe prior Deck attempt was not promoted at exhaustion",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(ctx.state["finalized"])
            self.assertFalse(ctx.state.get("designer_contract_abort", False))
            self.assertEqual(
                manifest["acceptance_path"],
                "best_available_artifact_fallback",
            )
            self.assertEqual(manifest.get("quality_status"), "ready_with_warnings")
            self.assertEqual(
                manifest.get("quality_diagnostics"),
                [
                    "insufficient_visual_unit_slides",
                    "slides_required_palette_id_missing",
                ],
            )
            self.assertIn("attempt_03", manifest["attempt_dir"])
            self.assertEqual(
                ctx.state["designer_author_direct_final"]["acceptance_path"],
                "best_available_artifact_fallback",
            )
            deck_html = root / "final" / "deck.html"
            slides_html = root / "final" / "slides.html"
            self.assertEqual(deck_html.read_bytes(), slides_html.read_bytes())
            self.assertEqual(manifest["html_sha256"], sha256_file(deck_html))
            self.assertEqual(
                manifest["slides_html_sha256"],
                sha256_file(slides_html),
            )
            self.assertTrue({
                "slides_asset_catalog.json",
                "slides_visual_plan.json",
                "slides_validation.json",
            }.issubset(manifest["sidecar_sha256"]))

    def test_fallback_revalidation_persists_rejected_candidate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=14, slide_count=12)
            settings.designer_author_max_attempts = 1
            settings.designer_author_cmd = (
                f"{sys.executable} {_write_fake_agent(root, emitted_slide_count=None)}"
            )
            quality = {
                "kind": "external_slides_validation",
                "version": 3,
                "status": "error",
                "expected_slide_count": 12,
                "actual_slide_count": 12,
                "source_visual_ids": [],
                "issues": [{
                    "id": "slides_content_clipped",
                    "message": "caption clipping remains",
                    "evidence": {"elements": [{"content_role": "caption"}]},
                }],
            }
            hard = {
                **quality,
                "issues": [{
                    "id": "source_visual_hash_mismatch",
                    "message": "source bytes changed before fallback promotion",
                }],
            }

            with (
                patch(
                    "autodesign.agents.external_slides_author._validate_slides",
                    side_effect=[quality, hard],
                ),
                patch(
                    "autodesign.agents.external_slides_author.audit_slides_html",
                    return_value={
                        "status": "ok",
                        "accepted": True,
                        "backend": "playwright",
                        "findings": [],
                        "warnings": [],
                        "metrics": {"snapshot_count": 1},
                    },
                ),
                patch(
                    "autodesign.agents.external_slides_author.screenshot_deck_slides",
                    side_effect=_fake_slide_capture,
                ),
            ):
                ExternalSlidesAuthor(settings, "system").run(
                    "Create an academic paper deck.",
                    ctx,
                )

            evidence = json.loads(
                (root / "slides_best_available_rejected.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(evidence["candidate_id"].startswith("deck-attempt-01-"))
            self.assertEqual(
                [item["issue_id"] for item in evidence["hard_blockers"]],
                ["source_visual_hash_mismatch"],
            )
            self.assertFalse((root / "final" / "deck.html").exists())

    def test_fallback_stages_fresh_validation_and_browser_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=14, slide_count=12)
            settings.designer_author_max_attempts = 1
            settings.designer_author_cmd = (
                f"{sys.executable} {_write_fake_agent(root, emitted_slide_count=None)}"
            )
            static_ok = {
                "kind": "external_slides_validation",
                "version": 3,
                "status": "ok",
                "expected_slide_count": 12,
                "actual_slide_count": 12,
                "source_visual_ids": [],
                "issues": [],
            }
            fresh_quality = {
                **static_ok,
                "status": "error",
                "issues": [{
                    "id": "missing_speaker_notes",
                    "message": "speaker-note polish remains",
                }],
            }
            stale_browser = {
                "accepted": False,
                "backend": "playwright",
                "findings": [{
                    "id": "slides_content_clipped",
                    "message": "stale caption clipping",
                    "evidence": {
                        "elements": [{"content_role": "caption"}],
                    },
                }],
                "warnings": [],
                "metrics": {"audit_generation": "stale"},
            }
            fresh_browser = {
                "accepted": True,
                "backend": "playwright",
                "findings": [],
                "warnings": [],
                "metrics": {"audit_generation": "fresh"},
            }

            with (
                patch(
                    "autodesign.agents.external_slides_author._validate_slides",
                    side_effect=[static_ok, fresh_quality],
                ),
                patch(
                    "autodesign.agents.external_slides_author.audit_slides_html",
                    side_effect=[stale_browser, fresh_browser],
                ),
                patch(
                    "autodesign.agents.external_slides_author.screenshot_deck_slides",
                    side_effect=_fake_slide_capture,
                ),
            ):
                ExternalSlidesAuthor(settings, "system").run(
                    "Create an academic paper deck.",
                    ctx,
                )

            final_dir = root / "final"
            manifest = json.loads(
                (final_dir / "slides_author_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            validation_sidecar = json.loads(
                (final_dir / "slides_validation.json").read_text(encoding="utf-8")
            )
            self.assertTrue(
                (final_dir / "slides_browser_qa.json").is_file(),
                "fresh fallback browser audit was not staged",
            )
            browser_sidecar = json.loads(
                (final_dir / "slides_browser_qa.json").read_text(encoding="utf-8")
            )
            self.assertEqual(validation_sidecar, manifest["validation"])
            self.assertEqual(browser_sidecar, fresh_browser)
            self.assertEqual(
                manifest["sidecar_sha256"]["slides_validation.json"],
                sha256_file(final_dir / "slides_validation.json"),
            )
            self.assertEqual(
                manifest["sidecar_sha256"]["slides_browser_qa.json"],
                sha256_file(final_dir / "slides_browser_qa.json"),
            )

    def test_fallback_skips_fresh_blocked_top_and_promotes_next_safe_deck(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=14, slide_count=12)
            settings.designer_author_max_attempts = 2
            settings.designer_author_cmd = (
                f"{sys.executable} {_write_fake_agent(root, emitted_slide_count=None)}"
            )
            quality = {
                "kind": "external_slides_validation",
                "version": 3,
                "status": "error",
                "expected_slide_count": 12,
                "actual_slide_count": 12,
                "source_visual_ids": [],
                "issues": [{
                    "id": "slides_content_clipped",
                    "message": "caption clipping remains",
                    "evidence": {"elements": [{"content_role": "caption"}]},
                }],
            }
            hard = {
                **quality,
                "issues": [{
                    "id": "source_visual_hash_mismatch",
                    "message": "source bytes changed before fallback promotion",
                }],
            }

            with (
                patch(
                    "autodesign.agents.external_slides_author._validate_slides",
                    side_effect=[quality, quality, hard, quality],
                ),
                patch(
                    "autodesign.agents.external_slides_author.audit_slides_html",
                    return_value={
                        "status": "ok",
                        "accepted": True,
                        "backend": "playwright",
                        "findings": [],
                        "warnings": [],
                        "metrics": {"snapshot_count": 1},
                    },
                ),
                patch(
                    "autodesign.agents.external_slides_author.screenshot_deck_slides",
                    side_effect=_fake_slide_capture,
                ),
            ):
                ExternalSlidesAuthor(settings, "system").run(
                    "Create an academic paper deck.",
                    ctx,
                )

            manifest = json.loads(
                (root / "final" / "slides_author_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn("attempt_01", manifest["attempt_dir"])
            rejected = json.loads(
                (root / "slides_best_available_rejected.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn("attempt-02", rejected["candidate_id"])

    def test_browser_repair_findings_preserve_slide_evidence(self) -> None:
        validation = {
            "status": "ok",
            "expected_slide_count": 12,
            "actual_slide_count": 12,
            "visual_slide_count": 8,
            "visual_placement_count": 10,
            "issues": [],
        }
        browser_audit = {
            "accepted": False,
            "backend": "playwright",
            "metrics": {"snapshot_count": 2},
            "findings": [
                {
                    "id": "slides_content_clipped",
                    "message": "slide slide-7 contains clipped content",
                    "evidence": {
                        "slide_id": "slide-7",
                        "elements": [
                            {
                                "tag": "p",
                                "text": "A clipped result explanation",
                                "overflow_y_px": 24,
                            }
                        ],
                    },
                }
            ],
        }

        merged = _merge_slides_browser_audit(validation, browser_audit)
        compact = _compact_validation_findings(merged)

        self.assertEqual(merged["status"], "error")
        self.assertEqual(compact["issues"][0]["evidence"]["slide_id"], "slide-7")
        self.assertEqual(
            compact["issues"][0]["evidence"]["elements"][0]["overflow_y_px"],
            24,
        )

    def test_runtime_skill_snapshot_integrity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx = _isolated_skill_context(root, artifact="deck", skill_id="deck.isolated")
            staged_skill = root / "run/runtime_skills/packs/deck.isolated/SKILL.md"
            staged_skill.write_text("tampered", encoding="utf-8")
            attempt = root / "attempt"
            attempt.mkdir()

            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                _stage_runtime_skills(ctx, attempt, stage="plan")

    def test_runtime_skill_plan_and_repair_resources_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx = _isolated_skill_context(root, artifact="deck", skill_id="deck.isolated")
            plan_dir = root / "plan"
            repair_dir = root / "repair"
            plan_dir.mkdir()
            repair_dir.mkdir()

            _stage_runtime_skills(ctx, plan_dir, stage="plan")
            _stage_runtime_skills(ctx, repair_dir, stage="repair")

            plan_root = plan_dir / "runtime_skills/packs/deck.isolated/references"
            repair_root = repair_dir / "runtime_skills/packs/deck.isolated/references"
            self.assertTrue((plan_root / "plan.txt").is_file())
            self.assertFalse((plan_root / "repair.txt").exists())
            self.assertTrue((repair_root / "repair.txt").is_file())
            self.assertFalse((repair_root / "plan.txt").exists())

    def test_runtime_skill_snapshot_is_required_for_v2_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "run"
            run_dir.mkdir()
            attempt = root / "attempt"
            attempt.mkdir()
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id="missing-skills",
            )

            with self.assertRaisesRegex(ValueError, "runtime skill snapshot is required"):
                _stage_runtime_skills(ctx, attempt, stage="plan")

    def setUp(self) -> None:
        self.browser_audit_patcher = patch(
            "autodesign.agents.external_slides_author.audit_slides_html",
            return_value={
                "kind": "artifact_browser_audit",
                "version": 1,
                "artifact_type": "slides",
                "backend": "test-playwright",
                "status": "ok",
                "accepted": True,
                "findings": [],
                "metrics": {"snapshot_count": 2},
                "warnings": [],
            },
        )
        self.browser_audit_patcher.start()

    def tearDown(self) -> None:
        self.browser_audit_patcher.stop()

    def test_validation_uses_visual_plan_role_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            layers = root / "layers"
            layers.mkdir()
            for name in ("method.png", "results.png"):
                (layers / name).write_bytes(b"image")
            slides = []
            for index in range(12):
                image = ""
                if index == 1:
                    image = (
                        '<figure><img data-source-id="method" src="layers/method.png" '
                        'alt="Method diagram"><figcaption>The method visual isolates the staged '
                        "pipeline and its information flow.</figcaption></figure>"
                    )
                elif index == 2:
                    image = (
                        '<figure><img data-source-id="results" src="layers/results.png" '
                        'alt="Result evidence"><figcaption>The result visual supports this local '
                        "comparison and its measured consequence.</figcaption></figure>"
                    )
                slides.append(
                    f'<section class="deck-slide" id="slide-{index + 1}">'
                    f'<h2>Slide {index + 1}</h2><p>{_substantive_copy(index)}</p>{image}</section>'
                )
            html = (
                "<!doctype html><html><head><style>"
                ".deck-slide{width:1920px;height:1080px;aspect-ratio:16 / 9}"
                "</style></head><body><main id='deck' data-slide-count='12'>"
                + "".join(slides)
                + "</main>"
                + "<script>document.addEventListener('keydown',e=>{"
                "if(e.key==='ArrowLeft'||e.key==='ArrowRight')document.body.dataset.key=e.key"
                "})</script></body></html>"
            )
            html_path = root / "slides.html"
            html_path.write_text(html, encoding="utf-8")
            catalog = {
                "assets": [
                    {
                        "asset_id": "method",
                        "visual_role": "method",
                        "caption": "Architecture overview",
                        "staged_path": "layers/method.png",
                        "eligibility": {"eligible": True, "reserve": False},
                    },
                    {
                        "asset_id": "results",
                        "visual_role": "evidence",
                        "caption": "Denoised interpolation",
                        "staged_path": "layers/results.png",
                        "eligibility": {"eligible": True, "reserve": False},
                    },
                ]
            }
            plan = {
                "targets": {
                    "minimum_visual_slide_count": 2,
                    "minimum_visual_placement_count": 2,
                },
                "optional_reserve_asset_ids": [],
                "evidence_coverage": {
                    "method": True,
                    "results": True,
                    "method_asset_ids": ["method"],
                    "results_asset_ids": ["results"],
                },
            }

            report = _validate_slides(
                html_path,
                attempt_dir=root,
                expected_slide_count=12,
                visual_plan=plan,
                catalog=catalog,
            )

        self.assertEqual(report["status"], "ok", report["issues"])
        self.assertEqual(report["source_visual_roles"], ["method", "results"])

    def test_validation_rejects_body_direct_slides_without_a_narrow_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            slides = [
                (
                    f'<section class="deck-slide" id="slide-{index}" '
                    f'data-slide-role="content" data-section="Evidence">'
                    f"<h2>Slide {index}</h2><p>{_substantive_copy(index)}</p></section>"
                )
                for index in range(1, 3)
            ]
            html_path = _write_slides_html(root, slides, wrap_in_root=False)

            report = _validate_slides(
                html_path,
                attempt_dir=root,
                expected_slide_count=2,
                visual_plan={
                    "targets": {
                        "minimum_unique_source_visual_count": 0,
                        "minimum_source_visual_placement_count": 0,
                        "minimum_visual_unit_slide_count": 0,
                        "source_visual_reuse_cap": 1,
                    },
                    "optional_reserve_asset_ids": [],
                    "evidence_coverage": {},
                    "color_system": {},
                },
                catalog={"assets": []},
            )

        self.assertEqual(report["status"], "error")
        self.assertIn(
            "missing_deck_artifact_root",
            {item["id"] for item in report["issues"]},
        )

    def test_validation_rejects_body_even_when_it_claims_the_deck_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            slides = [
                (
                    f'<section class="deck-slide" id="slide-{index}" '
                    f'data-slide-role="content" data-section="Evidence">'
                    f"<h2>Slide {index}</h2><p>{_substantive_copy(index)}</p></section>"
                )
                for index in range(1, 3)
            ]
            html_path = _write_slides_html(root, slides, wrap_in_root=False)
            html_path.write_text(
                html_path.read_text(encoding="utf-8").replace(
                    "<body>",
                    "<body data-autodesign-artifact-root='deck'>",
                ),
                encoding="utf-8",
            )

            report = _validate_slides(
                html_path,
                attempt_dir=root,
                expected_slide_count=2,
                visual_plan={"targets": {}, "color_system": {}},
                catalog={"assets": []},
            )

        self.assertIn(
            "missing_deck_artifact_root",
            {item["id"] for item in report["issues"]},
        )

    def test_promotion_rejects_a_deck_that_normalizes_to_zero_editable_layers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=2, slide_count=2)
            attempt_dir = root / "slides_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            slides = [
                (
                    f'<section class="deck-slide" id="slide-{index}" '
                    f'data-slide-role="content" data-section="Evidence">'
                    f"<h2>Slide {index}</h2><p>{_substantive_copy(index)}</p></section>"
                )
                for index in range(1, 3)
            ]
            _write_slides_html(attempt_dir, slides, wrap_in_root=False)
            for name, payload in {
                "designer_author_done.json": {"status": "done"},
                "slides_visual_plan.json": {},
                "slides_asset_catalog.json": {"assets": []},
                "slides_validation.json": {"status": "ok", "issues": []},
            }.items():
                (attempt_dir / name).write_text(json.dumps(payload), encoding="utf-8")

            with (
                patch(
                    "autodesign.agents.external_slides_author.screenshot_deck_slides",
                    side_effect=_fake_slide_capture,
                ),
                self.assertRaisesRegex(ValueError, "editable Deck root"),
            ):
                ExternalSlidesAuthor(settings, "system")._promote(
                    ctx,
                    attempt_dir=attempt_dir,
                    expected_slide_count=2,
                    validation={"status": "ok", "issues": []},
                    _normal_lease_owned=True,
                )

            self.assertFalse((root / "final").exists())

    def test_twelve_slide_plan_reuses_four_source_assets_for_eight_placements(self) -> None:
        provenance, rendered = _synthetic_visual_evidence(Path("/tmp/layers"), count=4)
        color_system = {
            "palette_id": "academic_light_current",
            "roles": {
                "background": "#F7F8FA",
                "surface": "#FFFFFF",
                "text": "#17202A",
                "accent": "#A8323E",
            },
        }

        plan = build_slides_visual_plan(
            provenance,
            rendered_layers=rendered,
            expected_slide_count=12,
            color_system=color_system,
        )

        self.assertEqual(plan["targets"]["unique_source_visual_count"], 4)
        self.assertEqual(plan["targets"]["minimum_unique_source_visual_count"], 4)
        self.assertEqual(plan["targets"]["source_visual_placement_count"], 8)
        self.assertEqual(plan["targets"]["minimum_source_visual_placement_count"], 8)
        self.assertEqual(plan["targets"]["visual_unit_slide_count"], 8)
        self.assertEqual(plan["targets"]["minimum_visual_unit_slide_count"], 8)
        self.assertEqual(plan["targets"]["source_visual_reuse_cap"], 2)
        self.assertEqual(plan["color_system"], color_system)
        placement_ids = [item["asset_id"] for item in plan["placement_recommendations"]]
        self.assertEqual(len(placement_ids), 8)
        self.assertEqual({source_id: placement_ids.count(source_id) for source_id in set(placement_ids)}, {
            "ingest_fig_01": 2,
            "ingest_fig_02": 2,
            "ingest_fig_03": 2,
            "ingest_fig_04": 2,
        })
        self.assertEqual(
            len({item["suggested_slide"] for item in plan["placement_recommendations"]}),
            8,
        )

    def test_full_formal_visual_plan_scales_with_deck_length_and_storyboard(self) -> None:
        provenance, rendered = _synthetic_visual_evidence(
            Path("/tmp/layers"),
            count=24,
        )
        outline = [
            {
                "slide_index": index,
                "title": f"Slide {index}",
                "role": "cover" if index == 1 else "closing" if index == 24 else "content",
                "chapter": "Opening" if index <= 2 else "Evidence",
                "communication_job": "Establish the paper claim.",
                "assertion_title": f"Claim {index}",
                "scope": "paper",
                "layout_family": "evidence_split",
                "visual_refs": [],
                "evidence_refs": [],
                "speaker_note_intent": "Explain the source-backed point.",
            }
            for index in range(1, 25)
        ]

        plan = build_slides_visual_plan(
            provenance,
            rendered_layers=rendered,
            expected_slide_count=24,
            deck_plan={
                "talk_profile": "full_formal",
                "slide_count": 24,
                "outline": outline,
            },
        )

        self.assertGreater(plan["targets"]["unique_source_visual_count"], 10)
        self.assertGreater(plan["targets"]["source_visual_placement_count"], 10)
        self.assertGreater(plan["targets"]["visual_unit_slide_count"], 8)
        self.assertTrue(plan["targets"]["require_speaker_notes"])
        self.assertEqual(len(plan["storyboard"]), 24)
        self.assertEqual(plan["storyboard"][7]["chapter"], "Evidence")
        self.assertEqual(plan["storyboard"][7]["layout_family"], "evidence_split")
        self.assertIn("role_word_ranges", plan["narrative_contract"])

    def test_soft_deck_plan_count_is_authoritative_for_external_author(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx, _settings = _context(
                Path(temp_dir),
                visual_count=14,
                slide_count=20,
            )

            self.assertEqual(
                _expected_slide_count("Create an academic paper deck.", ctx),
                20,
            )

    def test_full_formal_validation_requires_source_anchored_speaker_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = {
                "targets": {
                    "minimum_substantive_word_count": 30,
                    "minimum_unique_source_visual_count": 0,
                    "minimum_source_visual_placement_count": 0,
                    "minimum_visual_unit_slide_count": 0,
                    "source_visual_reuse_cap": 1,
                    "require_speaker_notes": True,
                },
                "optional_reserve_asset_ids": [],
                "evidence_coverage": {},
                "color_system": {},
            }
            slides_without_notes = [
                (
                    f'<section class="deck-slide" id="slide-{index}" '
                    f'data-slide-role="{"cover" if index == 1 else "closing" if index == 4 else "content"}">'
                    f"<h2>Slide {index}</h2><p>{_substantive_copy(index)}</p></section>"
                )
                for index in range(1, 5)
            ]
            html_path = _write_slides_html(root, slides_without_notes)

            missing = _validate_slides(
                html_path,
                attempt_dir=root,
                expected_slide_count=4,
                visual_plan=plan,
                catalog={"assets": []},
            )

            slides_with_notes = [
                slide.replace(
                    'data-slide-role="',
                    (
                        'data-speaker-notes="[Sources] Paper p.1. '
                        '[Talk] Explain this source-backed point." data-slide-role="'
                    ),
                )
                for slide in slides_without_notes
            ]
            html_path = _write_slides_html(root, slides_with_notes)
            complete = _validate_slides(
                html_path,
                attempt_dir=root,
                expected_slide_count=4,
                visual_plan=plan,
                catalog={"assets": []},
            )

        missing_ids = {item["id"] for item in missing["issues"]}
        complete_ids = {item["id"] for item in complete["issues"]}
        self.assertIn("missing_speaker_notes", missing_ids)
        self.assertNotIn("missing_speaker_notes", complete_ids)
        self.assertNotIn("invalid_speaker_note_format", complete_ids)
        self.assertEqual(complete["speaker_note_count"], 4)

    def test_visual_plan_deduplicates_source_assets_by_content_hash(self) -> None:
        provenance, rendered = _synthetic_visual_evidence(Path("/tmp/layers"), count=4)
        for asset in provenance["assets"]:
            asset["output_sha256"] = "same-pixels"

        plan = build_slides_visual_plan(provenance, rendered_layers=rendered)

        self.assertEqual(plan["targets"]["unique_source_visual_count"], 1)
        self.assertEqual(plan["recommended_asset_ids"], ["ingest_fig_01"])

    def test_two_slide_plan_has_no_out_of_range_substantive_placements(self) -> None:
        provenance, rendered = _synthetic_visual_evidence(Path("/tmp/layers"), count=4)

        plan = build_slides_visual_plan(
            provenance,
            rendered_layers=rendered,
            expected_slide_count=2,
        )

        self.assertEqual(plan["targets"]["visual_unit_slide_count"], 0)
        self.assertEqual(plan["targets"]["source_visual_placement_count"], 0)
        self.assertEqual(plan["placement_recommendations"], [])

    def test_static_validator_rejects_sparse_twelve_slide_deck(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            catalog, plan = _four_asset_catalog_and_plan(root)
            slides = [
                f'<section class="deck-slide" id="slide-{index + 1}">'
                f'<h2>Slide {index + 1}</h2><p>Brief note only.</p></section>'
                for index in range(12)
            ]
            html_path = _write_slides_html(root, slides)

            report = _validate_slides(
                html_path,
                attempt_dir=root,
                expected_slide_count=12,
                visual_plan=plan,
                catalog=catalog,
            )

        issue_ids = {item["id"] for item in report["issues"]}
        self.assertEqual(report["status"], "error")
        self.assertIn("insufficient_substantive_slide_words", issue_ids)
        self.assertIn("insufficient_unique_source_visuals", issue_ids)
        self.assertIn("insufficient_source_visual_placements", issue_ids)
        self.assertIn("insufficient_visual_unit_slides", issue_ids)
        self.assertEqual(len(report["per_slide_metrics"]), 12)
        self.assertTrue(all("word_count" in item for item in report["per_slide_metrics"]))

    def test_static_validator_accepts_dense_editable_deck_with_reuse_and_native_units(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            catalog, plan = _four_asset_catalog_and_plan(root)
            slides = []
            for index in range(12):
                visual = ""
                if 1 <= index <= 8:
                    asset_index = ((index - 1) % 4) + 1
                    visual = (
                        f'<figure data-source-id="ingest_fig_{asset_index:02d}">'
                        f'<img '
                        f'src="layers/source_{asset_index:02d}.png" alt="Paper evidence">'
                        f"<figcaption>Local reading {index}: this placement isolates a different "
                        "mechanism, comparison, or implication from the source figure.</figcaption>"
                        "</figure>"
                    )
                if index == 9:
                    visual = (
                        '<table data-visual-unit="table" data-evidence-ref="paper_memory:table-1">'
                        '<caption>Native result comparison</caption>'
                        "<tr><th>Method</th><th>Score</th></tr><tr><td>Ours</td><td>91.2</td></tr></table>"
                    )
                elif index == 10:
                    visual = (
                        '<div class="equation" data-visual-unit="equation" '
                        'data-evidence-ref="paper_memory:objective">L = L_task + lambda L_align</div>'
                        '<div class="mechanism-diagram" data-visual-unit="diagram" '
                        'data-evidence-ref="paper_memory:method"><span>Input</span><span>Encoder</span>'
                        "<span>Output</span></div>"
                    )
                role = "cover" if index == 0 else "closing" if index == 11 else "content"
                slides.append(
                    f'<section class="deck-slide" id="slide-{index + 1}" data-slide-role="{role}">'
                    f'<h2>Slide {index + 1}</h2><p>{_substantive_copy(index)}</p>{visual}</section>'
                )
            html_path = _write_slides_html(root, slides)

            report = _validate_slides(
                html_path,
                attempt_dir=root,
                expected_slide_count=12,
                visual_plan=plan,
                catalog=catalog,
            )

        self.assertEqual(report["status"], "ok", report["issues"])
        self.assertEqual(report["unique_source_visual_count"], 4)
        self.assertEqual(report["source_visual_placement_count"], 8)
        self.assertGreaterEqual(report["visual_unit_slide_count"], 8)
        self.assertEqual(report["native_table_count"], 1)
        self.assertEqual(report["native_equation_count"], 1)
        self.assertEqual(report["native_diagram_count"], 1)

    def test_static_validator_counts_paginated_deck_content_across_active_states(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            catalog, plan = _four_asset_catalog_and_plan(root)
            slides = []
            for index in range(12):
                visual = ""
                if 1 <= index <= 8:
                    asset_index = ((index - 1) % 4) + 1
                    visual = (
                        f'<figure data-source-id="ingest_fig_{asset_index:02d}">'
                        f'<img src="layers/source_{asset_index:02d}.png" alt="Paper evidence">'
                        f"<figcaption>Local reading {index}: this source figure explains the "
                        "mechanism, result, and consequence used on the current slide.</figcaption>"
                        "</figure>"
                    )
                elif index == 9:
                    visual = (
                        '<table data-visual-unit="table" data-evidence-ref="paper_memory:table-1">'
                        '<caption>Native result comparison</caption>'
                        "<tr><th>Method</th><th>Score</th></tr>"
                        "<tr><td>Ours</td><td>91.2</td></tr></table>"
                    )
                role = "cover" if index == 0 else "closing" if index == 11 else "content"
                active = " active" if index == 0 else ""
                slides.append(
                    f'<section class="deck-slide{active}" id="slide-{index + 1}" '
                    f'data-slide-role="{role}"><h2>Slide {index + 1}</h2>'
                    f"<p>{_substantive_copy(index)}</p>{visual}</section>"
                )
            html_path = _write_slides_html(root, slides)
            html = html_path.read_text(encoding="utf-8").replace(
                ".deck-slide{width:1920px;height:1080px;aspect-ratio:16 / 9}",
                (
                    ".deck-slide{display:none;width:1920px;height:1080px;"
                    "aspect-ratio:16 / 9}.deck-slide.active{display:block}"
                ),
            )
            html_path.write_text(html, encoding="utf-8")

            report = _validate_slides(
                html_path,
                attempt_dir=root,
                expected_slide_count=12,
                visual_plan=plan,
                catalog=catalog,
            )

        issue_ids = {item["id"] for item in report["issues"]}
        self.assertNotIn("source_visual_not_visible", issue_ids)
        self.assertEqual(report["unique_source_visual_count"], 4)
        self.assertEqual(report["source_visual_placement_count"], 8)
        self.assertEqual(report["native_table_count"], 1)

    def test_static_validator_rejects_invalid_source_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            catalog, plan = _four_asset_catalog_and_plan(root)
            repeated = (
                '<img data-source-id="ingest_fig_01" src="layers/source_01.png" '
                'alt="Repeated evidence">'
            )
            slides = []
            for index in range(12):
                visual = repeated * 2 if index == 1 else repeated if index == 2 else ""
                slides.append(
                    f'<section class="deck-slide" id="slide-{index + 1}">'
                    f'<h2>Slide {index + 1}</h2><p>{_substantive_copy(index)}</p>{visual}</section>'
                )
            html_path = _write_slides_html(root, slides)

            report = _validate_slides(
                html_path,
                attempt_dir=root,
                expected_slide_count=12,
                visual_plan=plan,
                catalog=catalog,
            )

        issue_ids = {item["id"] for item in report["issues"]}
        self.assertIn("source_visual_repeated_on_same_slide", issue_ids)
        self.assertIn("source_visual_reuse_cap_exceeded", issue_ids)
        self.assertIn("source_visual_missing_local_interpretation", issue_ids)

    def test_static_validator_does_not_count_off_slide_images_or_empty_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            catalog, plan = _four_asset_catalog_and_plan(root)
            slides = [
                f'<section class="deck-slide" id="slide-{index + 1}">'
                f'<h2>Slide {index + 1}</h2><p>{_substantive_copy(index)}</p>'
                '<table data-visual-unit="table" data-evidence-ref="paper_memory:none"></table>'
                '</section>'
                for index in range(12)
            ]
            off_slide = (
                '<img data-source-id="ingest_fig_01" src="layers/source_01.png" '
                'alt="Off-slide evidence">'
            )
            html_path = _write_slides_html(root, slides, extra_body=off_slide)

            report = _validate_slides(
                html_path,
                attempt_dir=root,
                expected_slide_count=12,
                visual_plan=plan,
                catalog=catalog,
            )

        issue_ids = {item["id"] for item in report["issues"]}
        self.assertIn("source_visual_outside_slide", issue_ids)
        self.assertIn("insufficient_source_visual_placements", issue_ids)
        self.assertIn("insufficient_visual_unit_slides", issue_ids)
        self.assertEqual(report["native_table_count"], 0)

    def test_visual_plan_uses_full_provenance_not_poster_selection(self) -> None:
        provenance, rendered = _synthetic_visual_evidence(Path("/tmp/layers"), count=14)

        catalog = build_slides_asset_catalog(provenance, rendered_layers=rendered)
        plan = build_slides_visual_plan(provenance, rendered_layers=rendered)

        self.assertEqual(len(catalog["assets"]), 14)
        self.assertEqual(catalog["metrics"]["eligible_asset_count"], 14)
        self.assertGreater(plan["targets"]["visual_slide_count"], 10)
        self.assertLessEqual(plan["targets"]["visual_slide_count"], 16)
        self.assertGreater(plan["targets"]["visual_placement_count"], 10)
        self.assertLessEqual(plan["targets"]["visual_placement_count"], 18)
        self.assertIn("ingest_fig_01", plan["recommended_asset_ids"])
        self.assertGreater(len(plan["recommended_asset_ids"]), 1)
        self.assertTrue(plan["evidence_coverage"]["method"])
        self.assertTrue(plan["evidence_coverage"]["results"])

    def test_unmatched_reserve_is_optional_shortfall_only(self) -> None:
        provenance, rendered = _synthetic_visual_evidence(Path("/tmp/layers"), count=1)
        for index in range(2, 5):
            asset_id = f"ingest_fig_{index:02d}"
            provenance["assets"].append({
                "asset_id": asset_id,
                "kind": "image",
                "source_page": index,
                "caption_association_method": "unmatched",
                "extract_strategy": "raster",
                "output_file": f"layers/source_{index:02d}.png",
                "output_width_px": 320,
                "output_height_px": 180,
                "visual_role": "method" if index == 2 else "results",
            })

        catalog = build_slides_asset_catalog(provenance, rendered_layers=rendered)
        plan = build_slides_visual_plan(provenance, rendered_layers=rendered)

        self.assertEqual(catalog["metrics"]["eligible_asset_count"], 1)
        self.assertEqual(catalog["metrics"]["reserve_asset_count"], 3)
        self.assertEqual(plan["recommended_asset_ids"], ["ingest_fig_01"])
        self.assertEqual(plan["optional_reserve_asset_ids"], ["ingest_fig_02", "ingest_fig_03"])
        self.assertTrue(
            all(item["story_role"] == "supporting" for item in plan["optional_reserve_assets"])
        )
        self.assertNotIn("ingest_fig_02", plan["evidence_coverage"]["method_asset_ids"])
        self.assertNotIn("ingest_fig_03", plan["evidence_coverage"]["results_asset_ids"])

    def test_run_stages_full_ingest_and_promotes_valid_fake_agent_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=14, slide_count=12)
            registry = SkillRegistry.load(Path(__file__).resolve().parents[1] / "skills")
            bundle = registry.select(
                brief="Create academic paper slides.",
                attachments=[],
                artifact_hint="deck",
            )
            _write_runtime_skill_snapshot(
                root,
                skill_bundle=bundle,
                skill_contexts=bundle.render_all(),
            )
            fake_agent = _write_fake_agent(root, emitted_slide_count=None)
            settings.designer_author_cmd = f"{sys.executable} {fake_agent}"
            author = ExternalSlidesAuthor(
                settings,
                "INTERNAL DESIGNER PROMPT MUST NOT LEAK INTO SLIDES AUTHORING",
            )

            with patch(
                "autodesign.agents.external_slides_author.screenshot_deck_slides",
                side_effect=_fake_slide_capture,
            ), patch(
                "autodesign.agents.external_slides_author.audit_slides_html",
                return_value={
                    "kind": "artifact_browser_audit",
                    "version": 1,
                    "artifact_type": "slides",
                    "backend": "test-playwright",
                    "status": "ok",
                    "accepted": True,
                    "findings": [],
                    "metrics": {"snapshot_count": 2},
                    "warnings": [],
                },
            ), patch(
                "autodesign.agents.external_slides_author.log",
            ) as log_event:
                author.run("Create an academic paper deck.", ctx)

            attempt = root / "slides_author" / "attempt_01"
            manifest = json.loads((attempt / "author_input_manifest.json").read_text(encoding="utf-8"))
            catalog = json.loads((attempt / "slides_asset_catalog.json").read_text(encoding="utf-8"))
            visual_plan = json.loads((attempt / "slides_visual_plan.json").read_text(encoding="utf-8"))
            observed = json.loads((attempt / "staged_observation.json").read_text(encoding="utf-8"))
            browser_qa = json.loads((attempt / "slides_browser_qa.json").read_text(encoding="utf-8"))
            prompt = (attempt / "slides_author_prompt.md").read_text(encoding="utf-8")

            self.assertEqual(manifest["expected_slide_count"], 12)
            self.assertIn("deck_plan.json", manifest["must_read_first"])
            self.assertIn("deck_plan.json", manifest["staged_inputs"])
            self.assertTrue((attempt / "deck_plan.json").is_file())
            self.assertIn("runtime_skills/index.md", manifest["runtime_skills"])
            self.assertNotIn("runtime_skills/snapshot.json", manifest["runtime_skills"])
            self.assertEqual(manifest["progressive_disclosure"]["full_asset_catalog"], "slides_asset_catalog.json")
            self.assertNotIn("assets", manifest)
            self.assertEqual(len(catalog["assets"]), 14)
            self.assertEqual(
                visual_plan["color_system"]["palette_id"],
                "current_academic_light",
            )
            self.assertEqual(observed["provenance_asset_count"], 14)
            self.assertEqual(observed["catalog_asset_count"], 14)
            self.assertTrue(browser_qa["accepted"])
            self.assertTrue((attempt / "attempt_candidate.json").is_file())
            self.assertTrue((attempt / "candidate" / "slides.html").is_file())
            self.assertEqual(ctx.state["poster_plan_contract"]["selected_visuals"], [{"layer_id": "ingest_fig_01"}])
            self.assertIn("one 16:9 viewport per slide", prompt)
            self.assertIn("keyboard navigation", prompt)
            self.assertIn("no visible playback or slide-navigation controls", prompt)
            self.assertIn("native HTML text and tables", prompt)
            self.assertIn("role-specific word ranges", prompt)
            self.assertIn("verifiable equation", prompt)
            self.assertIn("experiment setup/evaluation protocol", prompt)
            self.assertIn("white or near-white canvas", prompt)
            self.assertIn("one restrained accent", prompt)
            self.assertIn("flat editorial composition", prompt)
            self.assertIn("data-speaker-notes", prompt)
            self.assertIn("deck_plan.json", prompt)
            self.assertIn("no remote assets", prompt)
            self.assertNotIn("For a 12-slide academic deck", prompt)
            self.assertNotIn("same visual direction as the Landing artifact", prompt)
            self.assertNotIn("INTERNAL DESIGNER PROMPT", prompt)
            self.assertIn("Read runtime_skills/index.md first", prompt)
            self.assertNotIn("runtime_skills/snapshot.json", prompt)
            self.assertFalse((attempt / "designer_author_prompt.md").exists())
            staged_skill = attempt / "runtime_skills" / "packs" / "deck.paper2deck_provenance" / "SKILL.md"
            self.assertTrue(staged_skill.is_file())
            self.assertEqual(staged_skill.stat().st_mode & 0o222, 0)
            staged_skill_text = staged_skill.read_text(encoding="utf-8")
            self.assertIn("default 18-slide academic deck", staged_skill_text)
            self.assertNotIn("default 12-slide academic deck", staged_skill_text)
            self.assertTrue((attempt / "paper_memory.json").is_file())
            self.assertTrue((attempt / "paper_memory_dossier.json").is_file())
            self.assertTrue((attempt / "paper_visual_provenance.json").is_file())
            self.assertEqual(len(list((attempt / "layers").glob("*.png"))), 14)
            self.assertTrue((root / "final" / "deck.html").is_file())
            log_event.assert_any_call(
                "slides_author.attempt_start",
                mode="external",
                attempt=1,
                max_attempts=settings.designer_author_max_attempts,
            )

    def test_promotion_copies_linked_local_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=14, slide_count=12)
            fake_agent = _write_fake_agent(root, emitted_slide_count=None)
            body = fake_agent.read_text(encoding="utf-8")
            original_html_line = (
                "html = \"<!doctype html><html><head><style>.deck-slide{width:1920px;"
                "height:1080px;aspect-ratio:16 / 9} img{max-width:45%}</style></head>"
                "<body><main class='od-deck'>\" + ''.join(slides) + \"</main><script>"
                "document.addEventListener('keydown',e=>{if(e.key==='ArrowRight'||"
                "e.key==='ArrowLeft')document.body.dataset.key=e.key})</script></body></html>\""
            )
            linked_html_lines = (
                "(cwd / 'theme.css').write_text('img{max-width:45%}')\n"
                "html = \"<!doctype html><html><head><link rel='stylesheet' href='theme.css'>"
                "<style>.deck-slide{width:1920px;height:1080px;aspect-ratio:16 / 9}</style>"
                "</head><body><main class='od-deck'>\" + ''.join(slides) + \"</main><script>"
                "document.addEventListener('keydown',e=>{if(e.key==='ArrowRight'||"
                "e.key==='ArrowLeft')document.body.dataset.key=e.key})</script></body></html>\""
            )
            self.assertIn(original_html_line, body)
            body = body.replace(
                original_html_line,
                linked_html_lines,
            )
            fake_agent.write_text(body, encoding="utf-8")
            settings.designer_author_cmd = f"{sys.executable} {fake_agent}"
            author = ExternalSlidesAuthor(settings, "system")

            with patch(
                "autodesign.agents.external_slides_author.screenshot_deck_slides",
                side_effect=_fake_slide_capture,
            ):
                author.run("Create an academic paper deck.", ctx)

            self.assertTrue((root / "final" / "theme.css").is_file())
            self.assertTrue((root / "final" / "slides.html").is_file())
            self.assertTrue((root / "final" / "preview.png").is_file())
            self.assertTrue(ctx.state["finalized"])
            self.assertEqual(ctx.state["artifact_type"], "deck")
            self.assertEqual(
                Path(ctx.state["composition"].deck_html_path).resolve(),
                (root / "final" / "deck.html").resolve(),
            )
            self.assertEqual(
                ctx.state["last_composite_payload"]["html_relative_path"],
                "final/deck.html",
            )
            self.assertEqual(
                ctx.state["designer_author_direct_final"],
                {
                    "source": "external_slides_author",
                    "artifact_type": "deck",
                    "acceptance_path": "deterministic_validation_pass",
                },
            )
            self.assertEqual(ctx.state["slides_author_result"]["status"], "ok")
            self.assertEqual(author.token_totals, (0, 0))
            self.assertEqual(author.cache_totals, (0, 0))

    def test_linked_css_import_is_rejected_before_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=14, slide_count=12)
            fake_agent = _write_fake_agent(root, emitted_slide_count=None)
            body = fake_agent.read_text(encoding="utf-8")
            original = "<style>.deck-slide{width:1920px;height:1080px;aspect-ratio:16 / 9} img{max-width:45%}</style>"
            replacement = (
                "<style>.deck-slide{width:1920px;height:1080px;aspect-ratio:16 / 9}</style>"
                "<link rel='stylesheet' href='theme.css'>"
            )
            self.assertIn(original, body)
            body = body.replace(original, replacement)
            body = body.replace(
                "(cwd / 'slides.html').write_text(html)",
                "(cwd / 'theme.css').write_text(\"@import 'base.css'; "
                ".deck-slide{width:1920px;height:1080px;aspect-ratio:16 / 9}\")\n"
                "(cwd / 'base.css').write_text('img{max-width:45%}')\n"
                "(cwd / 'slides.html').write_text(html)",
            )
            fake_agent.write_text(body, encoding="utf-8")
            settings.designer_author_cmd = f"{sys.executable} {fake_agent}"
            settings.designer_author_max_attempts = 1

            ExternalSlidesAuthor(settings, "system").run(
                "Create an academic paper deck.",
                ctx,
            )

            validation = json.loads(
                (root / "slides_author" / "attempt_01" / "slides_validation.json").read_text()
            )
            self.assertIn("css_import_forbidden", {item["id"] for item in validation["issues"]})
            self.assertFalse((root / "final" / "deck.html").exists())

    def test_process_log_omits_command_and_redacts_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            attempt = Path(temp_dir)
            secret = "slides-harness-secret-value"
            token = "slides-command-token-value"
            cmd = ["agent", "--token", token, f"--api-key={secret}"]

            _write_process_log(
                attempt,
                cmd,
                1,
                f"token={token} bearer {secret}",
                f"api_key: {secret}",
                harness="custom",
                model="fake-model",
                elapsed_s=1.25,
                sensitive_values=[secret, token],
            )

            payload = json.loads(
                (attempt / "designer_author_log.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("command", payload)
            self.assertIn("command_sha256", payload)
            self.assertEqual(payload["harness"], "custom")
            self.assertEqual(payload["model"], "fake-model")
            rendered = json.dumps(payload)
            self.assertNotIn(secret, rendered)
            self.assertNotIn(token, rendered)
            self.assertIn("[REDACTED]", rendered)

    def test_process_log_redacts_inherited_environment_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            attempt = root / "attempt"
            attempt.mkdir()
            script = root / "print_inherited_secret.py"
            script.write_text(
                "import os\nprint(os.environ.get('OPENAI_API_KEY', ''))\n",
                encoding="utf-8",
            )
            secret = "slides-inherited-provider-secret"
            settings = SimpleNamespace(
                designer_author_cmd=f"{sys.executable} {script}",
                designer_author_harness="custom",
                designer_author_model="fake-model",
                designer_author_timeout_s=10,
                harness_api_key=None,
            )

            with patch.dict(os.environ, {"OPENAI_API_KEY": secret}):
                ExternalSlidesAuthor(settings, "system")._invoke(
                    settings.designer_author_cmd,
                    prompt="prompt",
                    attempt_dir=attempt,
                )

            rendered = (attempt / "designer_author_log.json").read_text(encoding="utf-8")
            self.assertNotIn(secret, rendered)
            self.assertIn("[REDACTED]", rendered)

    def test_staged_source_visual_bytes_must_match_trusted_source_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=10, slide_count=12)
            settings.designer_author_max_attempts = 1
            fake_agent = _write_fake_agent(root, emitted_slide_count=None)
            body = fake_agent.read_text(encoding="utf-8").replace(
                "(cwd / 'slides.html').write_text(html)",
                "(cwd / 'layers/source_01.png').write_bytes(b'coding-agent-replacement')\n"
                "(cwd / 'slides.html').write_text(html)",
            )
            fake_agent.write_text(body, encoding="utf-8")
            settings.designer_author_cmd = f"{sys.executable} {fake_agent}"

            ExternalSlidesAuthor(settings, "system").run("Create a paper deck.", ctx)

            validation = json.loads(
                (root / "slides_author/attempt_01/slides_validation.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn(
                "source_visual_hash_mismatch",
                {issue["id"] for issue in validation["issues"]},
            )
            self.assertFalse((root / "final").exists())

    def test_interrupted_promotion_never_promotes_unvalidated_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            final_dir = root / "final"
            backup_dir = root / ".slides-final-backup-interrupted"
            staging_dir = root / ".slides-final-staging-interrupted"
            backup_dir.mkdir()
            staging_dir.mkdir()
            final_dir.mkdir()
            (final_dir / "deck.html").write_text("untrusted", encoding="utf-8")
            (backup_dir / "deck.html").write_text("old", encoding="utf-8")
            (staging_dir / "deck.html").write_text("new", encoding="utf-8")
            (root / ".slides-final-promotion.json").write_text(
                json.dumps({
                    "version": 1,
                    "phase": "backup_created",
                    "final_name": "final",
                    "backup_name": backup_dir.name,
                    "staging_name": staging_dir.name,
                }),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "backup retained"):
                _recover_interrupted_promotion(final_dir)

            self.assertEqual(
                (final_dir / "deck.html").read_text(encoding="utf-8"),
                "untrusted",
            )
            self.assertTrue(backup_dir.is_dir())
            self.assertEqual(
                (backup_dir / "deck.html").read_text(encoding="utf-8"),
                "old",
            )
            self.assertFalse(staging_dir.exists())
            self.assertTrue((root / ".slides-final-promotion.json").is_file())

    def test_promotion_recovery_rejects_backup_namespace_as_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            final_dir = root / "final"
            backup_dir = root / ".slides-final-backup-alias"
            final_dir.mkdir()
            backup_dir.mkdir()
            (final_dir / "deck.html").write_text("current", encoding="utf-8")
            (backup_dir / "deck.html").write_text("old", encoding="utf-8")
            (root / ".slides-final-promotion.json").write_text(
                json.dumps({
                    "version": 1,
                    "phase": "backup_created",
                    "final_name": "final",
                    "backup_name": "",
                    "staging_name": backup_dir.name,
                }),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "invalid promotion journal"):
                _recover_interrupted_promotion(final_dir)

            self.assertTrue(backup_dir.is_dir())
            self.assertEqual(
                (backup_dir / "deck.html").read_text(encoding="utf-8"),
                "old",
            )
            self.assertEqual(
                (final_dir / "deck.html").read_text(encoding="utf-8"),
                "current",
            )
            self.assertTrue((root / ".slides-final-promotion.json").is_file())

    def test_promotion_recovery_unlinks_staging_symlink_without_touching_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            final_dir = root / "final"
            backup_dir = root / ".slides-final-backup-protected"
            staging_link = root / ".slides-final-staging-link"
            final_dir.mkdir()
            backup_dir.mkdir()
            (backup_dir / "deck.html").write_text("old", encoding="utf-8")
            staging_link.symlink_to(backup_dir, target_is_directory=True)
            (root / ".slides-final-promotion.json").write_text(
                json.dumps({
                    "version": 1,
                    "phase": "prepared",
                    "final_name": "final",
                    "backup_name": "",
                    "staging_name": staging_link.name,
                }),
                encoding="utf-8",
            )

            _recover_interrupted_promotion(final_dir)

            self.assertFalse(staging_link.exists())
            self.assertTrue(backup_dir.is_dir())
            self.assertEqual(
                (backup_dir / "deck.html").read_text(encoding="utf-8"),
                "old",
            )

    def test_resume_uses_original_source_hash_anchor_after_layer_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=10, slide_count=12)
            catalog = build_slides_asset_catalog(
                ctx.state["paper_visual_provenance"],
                rendered_layers=ctx.state["rendered_layers"],
            )
            original_hashes = _trusted_slides_source_hashes(ctx, catalog)
            source_id = next(iter(original_hashes))
            source_record = next(
                record for record in catalog["assets"] if record["asset_id"] == source_id
            )
            Path(source_record["rendered_layer"]["src_path"]).write_bytes(
                b"mutated-after-run"
            )
            previous = root / "slides_author" / "attempt_01"
            previous.mkdir(parents=True)
            (previous / "slides.html").write_text(
                "<html><body>prior</body></html>", encoding="utf-8"
            )
            ctx.state["slides_author_attempts"] = 1
            ctx.state["external_author_resume"] = {
                "prior_attempts": 1,
                "previous_attempt_dir": str(previous),
                "repair_feedback": {"status": "error", "issues": []},
            }
            settings.designer_author_max_attempts = 1
            settings.designer_author_cmd = (
                f"{sys.executable} {_write_fake_agent(root, emitted_slide_count=None)}"
            )

            ExternalSlidesAuthor(settings, "system").run("Create a paper deck.", ctx)

            anchored = json.loads(
                (root / "slides_trusted_source_hashes.json").read_text(encoding="utf-8")
            )["hashes"]
            validation = json.loads(
                (root / "slides_author/attempt_02/slides_validation.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(anchored, original_hashes)
            self.assertIn(
                "source_visual_hash_mismatch",
                {issue["id"] for issue in validation["issues"]},
            )
            self.assertFalse((root / "final").exists())

    def test_resume_rejects_anchor_replaced_to_match_mutated_layer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, _settings = _context(root, visual_count=10, slide_count=12)
            catalog = build_slides_asset_catalog(
                ctx.state["paper_visual_provenance"],
                rendered_layers=ctx.state["rendered_layers"],
            )
            source_record = catalog["assets"][0]
            source_path = Path(source_record["rendered_layer"]["src_path"])
            source_record["provenance"]["output_sha256"] = hashlib.sha256(
                source_path.read_bytes()
            ).hexdigest()
            _trusted_slides_source_hashes(ctx, catalog)
            source_path.write_bytes(b"mutated-and-rebaselined")
            forged_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
            anchor_path = root / "slides_trusted_source_hashes.json"
            anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
            anchor["hashes"][source_record["asset_id"]] = forged_hash
            anchor_path.chmod(0o644)
            anchor_path.write_text(json.dumps(anchor), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "disagrees with ingest provenance"):
                _trusted_slides_source_hashes(
                    ctx,
                    catalog,
                    require_existing=True,
                )

    def test_resume_rejects_catalog_that_drops_trusted_source_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, _settings = _context(root, visual_count=10, slide_count=12)
            catalog = build_slides_asset_catalog(
                ctx.state["paper_visual_provenance"],
                rendered_layers=ctx.state["rendered_layers"],
            )
            trusted = _trusted_slides_source_hashes(ctx, catalog)
            self.assertGreater(len(trusted), 1)
            reduced_catalog = {
                **catalog,
                "assets": [
                    record
                    for record in catalog["assets"]
                    if record.get("asset_id") != next(iter(trusted))
                ],
            }

            with self.assertRaisesRegex(ValueError, "catalog"):
                _trusted_slides_source_hashes(
                    ctx,
                    reduced_catalog,
                    require_existing=True,
                )

    def test_browser_layout_failure_retries_before_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=14, slide_count=12)
            settings.designer_author_cmd = f"{sys.executable} {_write_fake_agent(root, emitted_slide_count=None)}"
            failed_audit = {
                "kind": "artifact_browser_audit",
                "version": 1,
                "artifact_type": "slides",
                "backend": "test-playwright",
                "status": "error",
                "accepted": False,
                "findings": [{
                    "id": "slides_required_source_clipped",
                    "severity": "error",
                    "message": "required source evidence is clipped",
                    "evidence": {"slide_id": "slide-2", "source_id": "ingest_fig_01"},
                }],
                "metrics": {"snapshot_count": 2},
                "warnings": [],
            }
            passed_audit = {
                **failed_audit,
                "status": "ok",
                "accepted": True,
                "findings": [],
            }

            with patch(
                "autodesign.agents.external_slides_author.audit_slides_html",
                side_effect=[failed_audit, passed_audit],
            ), patch(
                "autodesign.agents.external_slides_author.screenshot_deck_slides",
                side_effect=_fake_slide_capture,
            ):
                ExternalSlidesAuthor(settings, "system").run("Create an academic paper deck.", ctx)

            first_validation = json.loads(
                (root / "slides_author" / "attempt_01" / "slides_validation.json").read_text()
            )
            self.assertIn(
                "slides_required_source_clipped",
                {issue["id"] for issue in first_validation["issues"]},
            )
            self.assertTrue((root / "slides_author" / "attempt_02" / "slides_browser_qa.json").is_file())
            self.assertTrue((root / "final" / "deck.html").is_file())
            self.assertEqual(ctx.state["slides_author_attempts"], 2)

    def test_run_delivers_wrong_default_slide_count_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=14, slide_count=12)
            fake_agent = _write_fake_agent(root, emitted_slide_count=11)
            settings.designer_author_cmd = f"{sys.executable} {fake_agent}"
            settings.designer_author_max_attempts = 1

            with patch(
                "autodesign.agents.external_slides_author.screenshot_deck_slides",
                side_effect=_fake_slide_capture,
            ):
                ExternalSlidesAuthor(settings, "system").run("Create a paper deck.", ctx)

            self.assertFalse(ctx.state.get("designer_contract_abort", False))
            self.assertTrue((root / "final" / "slides.html").is_file())
            validation = json.loads(
                (root / "slides_author" / "attempt_01" / "slides_validation.json").read_text(encoding="utf-8")
            )
            self.assertIn("slide_count_mismatch", {item["id"] for item in validation["issues"]})
            manifest = json.loads(
                (root / "final" / "slides_author_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["slide_count"], 11)
            self.assertEqual(manifest["expected_slide_count"], 12)
            self.assertEqual(manifest["quality_status"], "ready_with_warnings")
            self.assertIn("slide_count_mismatch", manifest["quality_diagnostics"])

    def test_run_fails_open_when_paper_memory_dossier_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=10, slide_count=12)
            ctx.state.pop("paper_memory_dossier")
            fake_agent = _write_fake_agent(root, emitted_slide_count=None)
            settings.designer_author_cmd = f"{sys.executable} {fake_agent}"

            with patch(
                "autodesign.agents.external_slides_author.screenshot_deck_slides",
                side_effect=_fake_slide_capture,
            ):
                ExternalSlidesAuthor(settings, "system").run("Create a paper deck.", ctx)

            attempt = root / "slides_author" / "attempt_01"
            prompt = (attempt / "slides_author_prompt.md").read_text(encoding="utf-8")
            self.assertFalse((attempt / "paper_memory_dossier.json").exists())
            self.assertIn(
                "paper_memory.json and paper_evidence_packs/",
                prompt,
            )
            self.assertTrue((root / "final" / "deck.html").exists())

    def test_deterministic_validation_failure_stages_patch_first_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=14, slide_count=12)
            settings.designer_author_max_attempts = 2
            fake_agent = _write_repairing_fake_agent(root)
            settings.designer_author_cmd = f"{sys.executable} {fake_agent}"

            with patch(
                "autodesign.agents.external_slides_author.screenshot_deck_slides",
                side_effect=_fake_slide_capture,
            ):
                ExternalSlidesAuthor(settings, "ignored internal prompt").run(
                    "Create a paper deck.",
                    ctx,
                )

            second = root / "slides_author" / "attempt_02"
            findings = json.loads(
                (second / "previous_validation_findings.json").read_text(encoding="utf-8")
            )
            observation = json.loads(
                (second / "repair_observation.json").read_text(encoding="utf-8")
            )
            repair_prompt = (second / "slides_author_prompt.md").read_text(encoding="utf-8")

            self.assertEqual(ctx.state["slides_author_attempts"], 2)
            self.assertTrue((second / "previous_slides.html").is_file())
            self.assertIn("slide_count_mismatch", {item["id"] for item in findings["issues"]})
            self.assertTrue(observation["previous_slides_exists"])
            self.assertIn("slide_count_mismatch", observation["finding_ids"])
            self.assertIn("Patch previous_slides.html first", repair_prompt)
            self.assertTrue((root / "final" / "deck.html").is_file())
            self.assertFalse(ctx.state.get("designer_contract_abort", False))

    def test_process_failure_consumes_attempt_budget_then_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=10, slide_count=12)
            settings.designer_author_max_attempts = 2
            fake_agent = root / "fake_no_output_slides.py"
            fake_agent.write_text("import sys\nsys.stdin.read()\n", encoding="utf-8")
            settings.designer_author_cmd = f"{sys.executable} {fake_agent}"

            ExternalSlidesAuthor(settings, "system").run("Create a paper deck.", ctx)

            self.assertEqual(ctx.state["slides_author_attempts"], 2)
            self.assertTrue((root / "slides_author" / "attempt_02" / "previous_validation_findings.json").is_file())
            prompt = (root / "slides_author" / "attempt_02" / "slides_author_prompt.md").read_text(encoding="utf-8")
            self.assertIn("No usable previous_slides.html baseline", prompt)
            self.assertTrue(ctx.state["designer_contract_abort"])
            self.assertFalse((root / "final" / "deck.html").exists())

    def test_resume_starts_patch_first_from_external_author_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=10, slide_count=12)
            _trusted_slides_source_hashes(
                ctx,
                build_slides_asset_catalog(
                    ctx.state["paper_visual_provenance"],
                    rendered_layers=ctx.state["rendered_layers"],
                ),
            )
            settings.designer_author_max_attempts = 1
            previous = root / "slides_author" / "attempt_02"
            previous.mkdir(parents=True)
            (previous / "slides.html").write_text("<html><body>prior</body></html>", encoding="utf-8")
            ctx.state["slides_author_attempts"] = 2
            ctx.state["external_author_resume"] = {
                "prior_attempts": 2,
                "previous_attempt_dir": str(previous),
                "repair_feedback": {
                    "status": "error",
                    "issues": [{"id": "slide_resume_gap", "message": "repair prior gap"}],
                },
                "source_run_dir": str(root),
            }
            fake_agent = _write_fake_agent(root, emitted_slide_count=None)
            settings.designer_author_cmd = f"{sys.executable} {fake_agent}"

            with patch(
                "autodesign.agents.external_slides_author.screenshot_deck_slides",
                side_effect=_fake_slide_capture,
            ):
                ExternalSlidesAuthor(settings, "system").run("Create a paper deck.", ctx)

            resumed = root / "slides_author" / "attempt_03"
            self.assertTrue((resumed / "previous_slides.html").is_file())
            self.assertIn(
                "Patch previous_slides.html first",
                (resumed / "slides_author_prompt.md").read_text(encoding="utf-8"),
            )
            self.assertNotIn("external_author_resume", ctx.state)

    def test_malformed_done_marker_retries_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=10, slide_count=12)
            settings.designer_author_max_attempts = 2
            fake_agent = _write_malformed_marker_then_valid_agent(root)
            settings.designer_author_cmd = f"{sys.executable} {fake_agent}"

            with patch(
                "autodesign.agents.external_slides_author.screenshot_deck_slides",
                side_effect=_fake_slide_capture,
            ):
                ExternalSlidesAuthor(settings, "system").run("Create a paper deck.", ctx)

            self.assertEqual(ctx.state["slides_author_attempts"], 2)
            findings = json.loads(
                (root / "slides_author" / "attempt_02" / "previous_validation_findings.json").read_text(encoding="utf-8")
            )
            self.assertIn("slides_author_invalid_done_marker", {item["id"] for item in findings["issues"]})
            self.assertTrue((root / "final" / "deck.html").is_file())

    def test_hidden_source_visuals_do_not_satisfy_visual_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=10, slide_count=12)
            settings.designer_author_max_attempts = 1
            fake_agent = _write_fake_agent(root, emitted_slide_count=None)
            body = fake_agent.read_text(encoding="utf-8").replace(
                "<figure><img data-source-id=",
                "<figure style='display:none'><img data-source-id=",
            )
            fake_agent.write_text(body, encoding="utf-8")
            settings.designer_author_cmd = f"{sys.executable} {fake_agent}"

            ExternalSlidesAuthor(settings, "system").run("Create a paper deck.", ctx)

            validation = json.loads(
                (root / "slides_author" / "attempt_01" / "slides_validation.json").read_text(encoding="utf-8")
            )
            issue_ids = {item["id"] for item in validation["issues"]}
            self.assertIn("source_visual_not_visible", issue_ids)
            self.assertIn("insufficient_visual_placements", issue_ids)
            self.assertEqual(validation["visual_placement_count"], 0)

    def test_preview_failure_does_not_leave_partial_final(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=10, slide_count=12)
            settings.designer_author_max_attempts = 1
            fake_agent = _write_fake_agent(root, emitted_slide_count=None)
            settings.designer_author_cmd = f"{sys.executable} {fake_agent}"

            with patch(
                "autodesign.agents.external_slides_author.screenshot_deck_slides",
                side_effect=RuntimeError("browser unavailable"),
            ):
                ExternalSlidesAuthor(settings, "system").run("Create a paper deck.", ctx)

            self.assertTrue(ctx.state["designer_contract_abort"])
            self.assertEqual(ctx.state["slides_author_result"]["reason"], "slides_author_promotion_failed")
            self.assertFalse((root / "final").exists())

    def test_fresh_pdf_run_invokes_shared_deck_ingest_before_authoring(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            layers_dir = root / "layers"
            layers_dir.mkdir()
            paper_path = root / "paper.pdf"
            paper_path.write_bytes(b"%PDF synthetic")
            settings = SimpleNamespace(
                designer_author_cmd="",
                designer_author_harness="custom",
                designer_author_model="fake-agent",
                designer_author_timeout_s=10,
                designer_author_max_attempts=1,
                harness_api_key=None,
                repo_root=root,
            )
            ctx = ToolContext(
                settings=settings,
                run_dir=root,
                layers_dir=layers_dir,
                run_id="fresh-slides-test",
            )
            ctx.state["attachments"] = [str(paper_path)]
            registry = SkillRegistry.load(Path(__file__).resolve().parents[1] / "skills")
            bundle = registry.select(
                brief="Create academic paper slides.",
                attachments=[paper_path],
                artifact_hint="deck",
            )
            _write_runtime_skill_snapshot(
                root,
                skill_bundle=bundle,
                skill_contexts=bundle.render_all(),
            )
            fake_agent = _write_fake_agent(root, emitted_slide_count=None)
            settings.designer_author_cmd = f"{sys.executable} {fake_agent}"
            calls: list[tuple[str, dict]] = []

            def fake_invoke(tool_name: str, args: dict, tool_ctx: ToolContext) -> SimpleNamespace:
                calls.append((tool_name, args))
                if tool_name == "ingest_document":
                    provenance, rendered = _synthetic_visual_evidence(layers_dir, count=14)
                    for index in range(1, 15):
                        Image.new("RGB", (320, 180), (40 + index, 80, 120)).save(
                            layers_dir / f"source_{index:02d}.png"
                        )
                    tool_ctx.state.update({
                        "paper_memory": {"title": "Fresh Paper", "chunks": [{"text": "Evidence"}]},
                        "paper_visual_provenance": provenance,
                        "rendered_layers": rendered,
                    })
                return SimpleNamespace(status="ok", payload={}, error_message=None)

            with (
                patch(
                    "autodesign.agents.external_slides_author.invoke_designer_tool",
                    side_effect=fake_invoke,
                ),
                patch(
                    "autodesign.agents.external_slides_author.screenshot_deck_slides",
                    side_effect=_fake_slide_capture,
                ),
            ):
                ExternalSlidesAuthor(settings, "ignored").run("Create a paper deck.", ctx)

            self.assertEqual(
                calls,
                [
                    ("switch_artifact_type", {"type": "deck"}),
                    ("ingest_document", {"file_paths": [str(paper_path)]}),
                ],
            )
            self.assertTrue((root / "final" / "deck.html").is_file())

    def test_hard_deck_lock_overrides_eighteen_slide_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx, settings = _context(root, visual_count=10, slide_count=8, hard_lock=True)
            fake_agent = _write_fake_agent(root, emitted_slide_count=None)
            settings.designer_author_cmd = f"{sys.executable} {fake_agent}"

            with patch(
                "autodesign.agents.external_slides_author.screenshot_deck_slides",
                side_effect=_fake_slide_capture,
            ):
                ExternalSlidesAuthor(settings, "system").run("Create exactly 8 slides.", ctx)

            validation = json.loads(
                (root / "slides_author" / "attempt_01" / "slides_validation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(validation["expected_slide_count"], 8)
            self.assertEqual(validation["actual_slide_count"], 8)
            self.assertEqual(validation["unique_source_visual_count"], 6)
            self.assertTrue((root / "final" / "deck.html").exists())
            self.assertTrue((root / "final" / "slides.html").exists())


def _context(
    root: Path,
    *,
    visual_count: int,
    slide_count: int,
    hard_lock: bool = False,
) -> tuple[ToolContext, SimpleNamespace]:
    layers_dir = root / "layers"
    layers_dir.mkdir(parents=True)
    provenance, rendered = _synthetic_visual_evidence(layers_dir, count=visual_count)
    for index in range(1, visual_count + 1):
        Image.new("RGB", (320, 180), (40 + index, 80, 120)).save(
            layers_dir / f"source_{index:02d}.png"
        )
    settings = SimpleNamespace(
        designer_author_cmd="",
        designer_author_harness="custom",
        designer_author_model="fake-agent",
        designer_author_timeout_s=10,
        designer_author_max_attempts=2,
        harness_api_key=None,
        repo_root=root,
    )
    ctx = ToolContext(
        settings=settings,
        run_dir=root,
        layers_dir=layers_dir,
        run_id="slides-test",
    )
    ctx.state.update(
        {
            "artifact_type": "deck",
            "paper_memory": {"title": "Synthetic Paper", "chunks": [{"text": "Full paper evidence."}]},
            "paper_memory_dossier": {"method": ["Method evidence"], "results": ["Result evidence"]},
            "paper_visual_provenance": provenance,
            "rendered_layers": rendered,
            "poster_plan_contract": {"selected_visuals": [{"layer_id": "ingest_fig_01"}]},
            "required_color_system": {
                "palette_id": "current_academic_light",
                "roles": {
                    "background": "#F7F8FA",
                    "surface": "#FFFFFF",
                    "text": "#17202A",
                    "accent": "#A8323E",
                },
            },
            "deck_plan": {
                "slide_count": slide_count,
                "lock_level": "hard" if hard_lock else "soft",
                "status": "explicit" if hard_lock else "fallback",
            },
        }
    )
    registry = SkillRegistry.load(Path(__file__).resolve().parents[1] / "skills")
    bundle = registry.select(
        brief="Create academic paper slides.",
        attachments=[],
        artifact_hint="deck",
    )
    _write_runtime_skill_snapshot(
        root,
        skill_bundle=bundle,
        skill_contexts=bundle.render_all(),
    )
    return ctx, settings


def _synthetic_visual_evidence(
    layers_dir: Path,
    *,
    count: int,
) -> tuple[dict, dict[str, dict]]:
    assets = []
    rendered: dict[str, dict] = {}
    for index in range(1, count + 1):
        asset_id = f"ingest_fig_{index:02d}"
        role = "method" if index <= 4 else "results" if index <= 10 else "analysis"
        output_file = f"layers/source_{index:02d}.png"
        record = {
            "asset_id": asset_id,
            "kind": "image",
            "source_page": index,
            "caption_full": f"Figure {index}: {role} evidence.",
            "caption_short": f"Figure {index}",
            "caption_association_method": "captioned_group",
            "captioned_source_group": True,
            "extract_strategy": "raster",
            "output_file": output_file,
            "output_width_px": 320,
            "output_height_px": 180,
            "output_sha256": f"sha-{index:02d}",
            "visual_role": role,
            "visual_score": 100 - index,
        }
        assets.append(record)
        rendered[asset_id] = {
            "layer_id": asset_id,
            "kind": "image",
            "source": "ingested_pdf",
            "src_path": str(layers_dir / f"source_{index:02d}.png"),
            "caption": record["caption_full"],
            "caption_association_method": "captioned_group",
            "captioned_source_group": True,
            "extract_strategy": "raster",
            "source_page": index,
            "image_size": "320x180",
            "visual_role": role,
            "visual_score": record["visual_score"],
        }
    return {"kind": "paper_visual_provenance", "version": 1, "assets": assets}, rendered


def _substantive_copy(index: int) -> str:
    return (
        f"This slide {index + 1} explains one distinct paper claim with enough local context "
        "to connect the research question, observed evidence, interpretation, and consequence. "
        "The wording stays source grounded, avoids repeating the thesis, and tells the audience "
        "why this specific result or mechanism matters for the overall argument."
    )


def _four_asset_catalog_and_plan(root: Path) -> tuple[dict, dict]:
    layers = root / "layers"
    layers.mkdir()
    provenance, rendered = _synthetic_visual_evidence(layers, count=4)
    for index in range(1, 5):
        (layers / f"source_{index:02d}.png").write_bytes(b"image")
    catalog = build_slides_asset_catalog(provenance, rendered_layers=rendered)
    plan = build_slides_visual_plan(
        provenance,
        rendered_layers=rendered,
        expected_slide_count=12,
    )
    return catalog, plan


def _write_slides_html(
    root: Path,
    slides: list[str],
    *,
    extra_body: str = "",
    wrap_in_root: bool = True,
) -> Path:
    slide_markup = "".join(slides)
    if wrap_in_root:
        slide_markup = (
            f"<main id='deck' data-slide-count='{len(slides)}'>"
            f"{slide_markup}</main>"
        )
    html = (
        "<!doctype html><html><head><style>"
        ".deck-slide{width:1920px;height:1080px;aspect-ratio:16 / 9}"
        "</style></head><body>"
        + slide_markup
        + extra_body
        + "<script>document.addEventListener('keydown',e=>{"
        "if(e.key==='ArrowLeft'||e.key==='ArrowRight')document.body.dataset.key=e.key"
        "})</script></body></html>"
    )
    html_path = root / "slides.html"
    html_path.write_text(html, encoding="utf-8")
    return html_path


def _write_fake_agent(root: Path, *, emitted_slide_count: int | None) -> Path:
    script = root / f"fake_slides_agent_{emitted_slide_count or 'manifest'}.py"
    script.write_text(
        "\n".join(
            [
                "import json, pathlib, sys",
                "cwd = pathlib.Path.cwd()",
                "prompt = sys.stdin.read()",
                "manifest = json.loads((cwd / 'author_input_manifest.json').read_text())",
                "catalog = json.loads((cwd / 'slides_asset_catalog.json').read_text())",
                "visual_plan = json.loads((cwd / 'slides_visual_plan.json').read_text())",
                "provenance = json.loads((cwd / 'paper_visual_provenance.json').read_text())",
                f"count = {emitted_slide_count!r} or int(manifest['expected_slide_count'])",
                "eligible = [a for a in catalog['assets'] if a['eligibility']['eligible']]",
                "by_id = {a['asset_id']: a for a in eligible}",
                "placements = [by_id[item['asset_id']] for item in visual_plan['placement_recommendations'] if item['asset_id'] in by_id]",
                "visual_slide_count = min(count, int(visual_plan['targets']['visual_unit_slide_count']))",
                "copy = 'This slide develops one distinct source grounded claim with enough context to connect the research question, observed evidence, interpretation, and consequence. The explanation avoids repeating the thesis or mechanism and tells the audience why this specific finding matters to the complete academic argument and evaluation.'",
                "slides = []",
                "for index in range(count):",
                "    images = []",
                "    for placement_index, asset in enumerate(placements):",
                "        if index >= visual_slide_count or placement_index % visual_slide_count != index:",
                "            continue",
                "        images.append(f\"<figure><img data-source-id='{asset['asset_id']}' src='{asset['staged_path']}' alt='{asset['asset_id']}'><figcaption>{asset['caption']} This local reading explains why the evidence matters on this slide.</figcaption></figure>\")",
                "    native = '' if images or index >= visual_slide_count else \"<div data-visual-unit='diagram' data-evidence-ref='paper_memory:method'>Input to method to evidence.</div>\"",
                "    note = '[Sources] Synthetic paper evidence. [Talk] Explain this source-backed point.'",
                "    slides.append(f\"<section class='deck-slide' id='slide-{index + 1}' data-slide-index='{index + 1}' data-slide-role='content' data-section='Evidence' data-speaker-notes='{note}'><h2>Slide {index + 1}</h2><p>{copy}</p>{''.join(images)}{native}</section>\")",
                "html = \"<!doctype html><html><head><style>.deck-slide{width:1920px;height:1080px;aspect-ratio:16 / 9} img{max-width:45%}</style></head><body><main class='od-deck'>\" + ''.join(slides) + \"</main><script>document.addEventListener('keydown',e=>{if(e.key==='ArrowRight'||e.key==='ArrowLeft')document.body.dataset.key=e.key})</script></body></html>\"",
                "(cwd / 'slides.html').write_text(html)",
                "(cwd / 'designer_author_done.json').write_text(json.dumps({'status': 'done'}))",
                "(cwd / 'staged_observation.json').write_text(json.dumps({'catalog_asset_count': len(catalog['assets']), 'provenance_asset_count': len(provenance['assets']), 'prompt': prompt}))",
            ]
        ),
        encoding="utf-8",
    )
    return script


def _write_repairing_fake_agent(root: Path) -> Path:
    script = root / "fake_repairing_slides_agent.py"
    script.write_text(
        "\n".join(
            [
                "import json, pathlib, sys",
                "cwd = pathlib.Path.cwd()",
                "prompt = sys.stdin.read()",
                "manifest = json.loads((cwd / 'author_input_manifest.json').read_text())",
                "catalog = json.loads((cwd / 'slides_asset_catalog.json').read_text())",
                "visual_plan = json.loads((cwd / 'slides_visual_plan.json').read_text())",
                "attempt = int(cwd.name.rsplit('_', 1)[1])",
                "count = 11 if attempt == 1 else int(manifest['expected_slide_count'])",
                "eligible = [a for a in catalog['assets'] if a['eligibility']['eligible']]",
                "by_id = {a['asset_id']: a for a in eligible}",
                "placements = [by_id[item['asset_id']] for item in visual_plan['placement_recommendations'] if item['asset_id'] in by_id]",
                "visual_slide_count = min(count, int(visual_plan['targets']['visual_unit_slide_count']))",
                "copy = 'This slide develops one distinct source grounded claim with enough context to connect the research question, observed evidence, interpretation, and consequence. The explanation avoids repeating the thesis or mechanism and tells the audience why this specific finding matters to the complete academic argument and evaluation.'",
                "slides = []",
                "for index in range(count):",
                "    images = []",
                "    for placement_index, asset in enumerate(placements):",
                "        if index < visual_slide_count and placement_index % visual_slide_count == index:",
                "            images.append(f\"<figure><img data-source-id='{asset['asset_id']}' src='{asset['staged_path']}' alt='{asset['asset_id']}'><figcaption>This local readout explains the source evidence on this slide.</figcaption></figure>\")",
                "    native = '' if images or index >= visual_slide_count else \"<div data-visual-unit='diagram' data-evidence-ref='paper_memory:method'>Input to method to evidence.</div>\"",
                "    note = '[Sources] Synthetic paper evidence. [Talk] Explain this source-backed point.'",
                "    slides.append(f\"<section class='deck-slide' id='slide-{index + 1}' data-slide-role='content' data-section='Evidence' data-speaker-notes='{note}'><h2>Slide {index + 1}</h2><p>{copy}</p>{''.join(images)}{native}</section>\")",
                "html = f\"<!doctype html><html><head><style>.deck-slide{{width:1920px;height:1080px;aspect-ratio:16 / 9}}</style></head><body><main id='deck' data-slide-count='{count}'>\" + ''.join(slides) + \"</main><script>document.addEventListener('keydown',e=>{if(e.key==='ArrowRight'||e.key==='ArrowLeft')document.body.dataset.key=e.key})</script></body></html>\"",
                "(cwd / 'slides.html').write_text(html)",
                "(cwd / 'designer_author_done.json').write_text('{}')",
                "if attempt > 1:",
                "    findings = json.loads((cwd / 'previous_validation_findings.json').read_text())",
                "    observation = {'previous_slides_exists': (cwd / 'previous_slides.html').is_file(), 'finding_ids': [item['id'] for item in findings['issues']], 'prompt': prompt}",
                "    (cwd / 'repair_observation.json').write_text(json.dumps(observation))",
            ]
        ),
        encoding="utf-8",
    )
    return script


def _write_malformed_marker_then_valid_agent(root: Path) -> Path:
    script = _write_fake_agent(root, emitted_slide_count=None)
    body = script.read_text(encoding="utf-8")
    body = body.replace(
        "(cwd / 'designer_author_done.json').write_text(json.dumps({'status': 'done'}))",
        "(cwd / 'designer_author_done.json').write_text('{invalid' if cwd.name == 'attempt_01' else json.dumps({'status': 'done'}))",
    )
    script.write_text(body, encoding="utf-8")
    return script


def _fake_slide_capture(html_path: Path, slides_dir: Path, **_: object) -> SimpleNamespace:
    slides_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    document = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    slide_count = len(document.select(".deck-slide"))
    for index in range(slide_count):
        path = slides_dir / f"slide_{index:02d}.png"
        Image.new("RGB", (320, 180), (245, 245, 245)).save(path)
        paths.append(path)
    return SimpleNamespace(backend="fake", paths=paths, warnings=[])


def _isolated_skill_context(root: Path, *, artifact: str, skill_id: str) -> ToolContext:
    skills_root = root / "skills"
    pack_root = skills_root / artifact / "isolated"
    references = pack_root / "references"
    references.mkdir(parents=True)
    (pack_root / "SKILL.md").write_text(
        "# Isolated\n\n## Stage: plan\nPlan only.\n\n## Stage: repair\nRepair only.\n",
        encoding="utf-8",
    )
    (references / "plan.txt").write_text("plan resource", encoding="utf-8")
    (references / "repair.txt").write_text("repair resource", encoding="utf-8")
    (pack_root / "skill.json").write_text(
        json.dumps({
            "manifest_version": 2,
            "id": skill_id,
            "version": "1.0.0",
            "description": "Stage isolation fixture.",
            "applies_to": [artifact],
            "stages": ["plan", "repair"],
            "triggers": [],
            "priority": 100,
            "enabled_by_default": True,
            "source": {"kind": "test"},
            "assets": [],
            "outputs": [],
            "resources": [
                {"id": "plan", "path": "references/plan.txt", "description": "Plan.",
                 "stages": ["plan"], "when_to_read": "During plan.", "media_type": "text/plain"},
                {"id": "repair", "path": "references/repair.txt", "description": "Repair.",
                 "stages": ["repair"], "when_to_read": "During repair.", "media_type": "text/plain"},
            ],
        }),
        encoding="utf-8",
    )
    pack = SkillRegistry.load(skills_root).get(skill_id)
    assert pack is not None
    bundle = SkillBundle([pack])
    run_dir = root / "run"
    _write_runtime_skill_snapshot(
        run_dir,
        skill_bundle=bundle,
        skill_contexts=bundle.render_all(),
    )
    return ToolContext(
        settings=SimpleNamespace(),
        run_dir=run_dir,
        layers_dir=run_dir / "layers",
        run_id="isolated",
    )


if __name__ == "__main__":
    unittest.main()
