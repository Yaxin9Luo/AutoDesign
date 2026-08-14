from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
from types import SimpleNamespace
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

try:
    from autodesign.agents.external_landing_author import (
        ExternalLandingAuthor,
        _recover_interrupted_promotion,
        _stage_runtime_skills,
        _trusted_landing_source_hashes,
        _validate_landing_output,
        capture_landing_attempt_candidate,
    )
    from autodesign.util.landing_visual_plan import (
        build_landing_asset_catalog,
        build_landing_visual_plan,
    )
except ModuleNotFoundError:
    ExternalLandingAuthor = None
    _validate_landing_output = None
    build_landing_asset_catalog = None
    build_landing_visual_plan = None

from autodesign.tools import ToolContext
from autodesign.runner import _write_runtime_skill_snapshot
from autodesign.skills.registry import SkillBundle, SkillRegistry


_ROLES = ("hero", "method", "results", "data", "qualitative", "analysis")


class ExternalLandingAuthorTests(unittest.TestCase):
    def test_reduced_motion_candidate_is_captured_with_quality_warnings(self) -> None:
        attempt_dir = self._write_validation_candidate(
            style=".hero{transition:opacity 1s}",
        )
        run_dir = self.root / "candidate-run"
        target_attempt = run_dir / "landing_author" / "attempt_01"
        target_attempt.parent.mkdir(parents=True)
        shutil.copytree(attempt_dir, target_attempt)
        diagnostics = {
            "kind": "external_landing_validation",
            "version": 1,
            "accepted": False,
            "findings": [{
                "issue_id": "landing_motion_without_reduced_motion",
                "message": "reduced-motion polish remains",
            }],
            "metrics": {},
        }
        (target_attempt / "landing_validation.json").write_text(
            json.dumps(diagnostics),
            encoding="utf-8",
        )
        ctx = ToolContext(
            settings=SimpleNamespace(),
            run_dir=run_dir,
            layers_dir=run_dir / "layers",
            run_id="candidate-run",
        )

        with patch(
            "autodesign.agents.external_landing_author.screenshot_html",
            side_effect=self._fake_screenshot,
        ):
            candidate = capture_landing_attempt_candidate(
                ctx=ctx,
                attempt_dir=target_attempt,
                attempt=1,
                max_attempts=3,
                diagnostics=diagnostics,
            )

        self.assertEqual(candidate.safety_state, "ready_with_warnings")
        self.assertEqual(candidate.hard_blockers, [])
        self.assertEqual(
            [issue.issue_id for issue in candidate.warnings],
            ["landing_motion_without_reduced_motion"],
        )
        dependency_names = {
            Path(path).name for path in candidate.dependency_relative_paths
        }
        self.assertIn("landing_asset_catalog.json", dependency_names)
        self.assertIn("landing_visual_plan.json", dependency_names)

    def test_exhaustion_promotes_best_safe_landing_with_quality_warnings(self) -> None:
        ctx = self._make_ctx(self._valid_fake_author(), max_attempts=2)
        quality = {
            "kind": "external_landing_validation",
            "version": 1,
            "accepted": False,
            "findings": [
                {
                    "issue_id": "landing_motion_without_reduced_motion",
                    "message": "reduced-motion polish remains",
                },
                {
                    "issue_id": "landing_content_clipped",
                    "message": "desktop layout still clips a heading",
                    "evidence": {
                        "elements": [{"content_role": "heading"}],
                    },
                },
            ],
            "metrics": {"browser_audit_backend": "test-playwright"},
        }

        with (
            patch(
                "autodesign.agents.external_landing_author._validate_landing_output",
                side_effect=[quality, quality, quality],
            ),
            patch(
                "autodesign.agents.external_landing_author.screenshot_html",
                side_effect=self._fake_screenshot,
            ),
        ):
            ExternalLandingAuthor(ctx.settings, "system").run(
                "Create the paper project page.",
                ctx,
            )

        manifest_path = ctx.run_dir / "final" / "landing_author_manifest.json"
        self.assertTrue(
            manifest_path.is_file(),
            "safe Landing candidate was not promoted at exhaustion",
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
                "landing_motion_without_reduced_motion",
                "landing_content_clipped",
            ],
        )
        self.assertTrue((ctx.run_dir / "final" / "landing_asset_catalog.json").is_file())
        self.assertTrue((ctx.run_dir / "final" / "landing_visual_plan.json").is_file())
        self.assertTrue((ctx.run_dir / "final" / "landing_validation.json").is_file())
        self.assertTrue({
            "landing_asset_catalog.json",
            "landing_visual_plan.json",
            "landing_validation.json",
        }.issubset(manifest["sidecar_sha256"]))
        self.assertIn("attempt_02", manifest["attempt_dir"])

    def test_fallback_revalidation_persists_rejected_candidate_evidence(self) -> None:
        ctx = self._make_ctx(self._valid_fake_author(), max_attempts=1)
        quality = {
            "kind": "external_landing_validation",
            "version": 1,
            "accepted": False,
            "findings": [{
                "issue_id": "landing_motion_without_reduced_motion",
                "message": "reduced-motion polish remains",
            }],
            "metrics": {},
        }
        hard = {
            **quality,
            "findings": [{
                "issue_id": "landing_remote_reference",
                "message": "remote source appeared before fallback promotion",
            }],
        }

        with (
            patch(
                "autodesign.agents.external_landing_author._validate_landing_output",
                side_effect=[quality, hard],
            ),
            patch(
                "autodesign.agents.external_landing_author.audit_landing_html",
                return_value={
                    "status": "ok",
                    "accepted": True,
                    "backend": "playwright",
                    "findings": [],
                    "warnings": [],
                    "metrics": {},
                },
            ),
            patch(
                "autodesign.agents.external_landing_author.screenshot_html",
                side_effect=self._fake_screenshot,
            ),
        ):
            ExternalLandingAuthor(ctx.settings, "system").run(
                "Create the paper project page.",
                ctx,
            )

        evidence = json.loads(
            (ctx.run_dir / "landing_best_available_rejected.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(
            evidence["candidate_id"].startswith("landing-attempt-01-")
        )
        self.assertEqual(
            [item["issue_id"] for item in evidence["hard_blockers"]],
            ["landing_remote_reference"],
        )
        self.assertFalse((ctx.run_dir / "final" / "index.html").exists())

    def test_fallback_stages_fresh_validation_and_browser_sidecars(self) -> None:
        ctx = self._make_ctx(self._valid_fake_author(), max_attempts=1)
        static_ok = {
            "kind": "external_landing_validation",
            "version": 1,
            "accepted": True,
            "findings": [],
            "metrics": {"used_source_visual_ids": []},
        }
        fresh_quality = {
            **static_ok,
            "accepted": False,
            "findings": [{
                "issue_id": "landing_missing_results_section",
                "message": "results section remains incomplete",
            }],
        }
        stale_browser = {
            "accepted": False,
            "backend": "playwright",
            "findings": [{
                "id": "landing_motion_without_reduced_motion",
                "message": "stale browser warning",
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
                "autodesign.agents.external_landing_author._validate_landing_output",
                side_effect=[static_ok, fresh_quality],
            ),
            patch(
                "autodesign.agents.external_landing_author.audit_landing_html",
                side_effect=[stale_browser, fresh_browser],
            ),
            patch(
                "autodesign.agents.external_landing_author.screenshot_html",
                side_effect=self._fake_screenshot,
            ),
        ):
            ExternalLandingAuthor(ctx.settings, "system").run(
                "Create the paper project page.",
                ctx,
            )

        final_dir = ctx.run_dir / "final"
        manifest = json.loads(
            (final_dir / "landing_author_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        validation_sidecar = json.loads(
            (final_dir / "landing_validation.json").read_text(encoding="utf-8")
        )
        browser_sidecar = json.loads(
            (final_dir / "landing_browser_qa.json").read_text(encoding="utf-8")
        )
        self.assertEqual(validation_sidecar, manifest["validation"])
        self.assertEqual(browser_sidecar, fresh_browser)
        self.assertEqual(
            manifest["sidecar_sha256"]["landing_validation.json"],
            hashlib.sha256(
                (final_dir / "landing_validation.json").read_bytes()
            ).hexdigest(),
        )
        self.assertEqual(
            manifest["sidecar_sha256"]["landing_browser_qa.json"],
            hashlib.sha256(
                (final_dir / "landing_browser_qa.json").read_bytes()
            ).hexdigest(),
        )

    def test_fallback_skips_fresh_blocked_top_and_promotes_next_safe_landing(
        self,
    ) -> None:
        ctx = self._make_ctx(self._valid_fake_author(), max_attempts=2)
        quality = {
            "kind": "external_landing_validation",
            "version": 1,
            "accepted": False,
            "findings": [{
                "issue_id": "landing_motion_without_reduced_motion",
                "message": "reduced-motion polish remains",
            }],
            "metrics": {},
        }
        hard = {
            **quality,
            "findings": [{
                "issue_id": "landing_remote_reference",
                "message": "remote source appeared before fallback promotion",
            }],
        }

        with (
            patch(
                "autodesign.agents.external_landing_author._validate_landing_output",
                side_effect=[quality, quality, hard, quality],
            ),
            patch(
                "autodesign.agents.external_landing_author.audit_landing_html",
                return_value={
                    "status": "ok",
                    "accepted": True,
                    "backend": "playwright",
                    "findings": [],
                    "warnings": [],
                    "metrics": {},
                },
            ),
            patch(
                "autodesign.agents.external_landing_author.screenshot_html",
                side_effect=self._fake_screenshot,
            ),
        ):
            ExternalLandingAuthor(ctx.settings, "system").run(
                "Create the paper project page.",
                ctx,
            )

        manifest = json.loads(
            (ctx.run_dir / "final" / "landing_author_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("attempt_01", manifest["attempt_dir"])
        rejected = json.loads(
            (ctx.run_dir / "landing_best_available_rejected.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("attempt-02", rejected["candidate_id"])

    def test_runtime_skill_snapshot_integrity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx = _isolated_skill_context(root, artifact="landing", skill_id="landing.isolated")
            staged_skill = root / "run/runtime_skills/packs/landing.isolated/SKILL.md"
            staged_skill.write_text("tampered", encoding="utf-8")
            attempt = root / "attempt"
            attempt.mkdir()

            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                _stage_runtime_skills(ctx, attempt, stage="plan")

    def test_runtime_skill_plan_and_repair_resources_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ctx = _isolated_skill_context(root, artifact="landing", skill_id="landing.isolated")
            plan_dir = root / "plan"
            repair_dir = root / "repair"
            plan_dir.mkdir()
            repair_dir.mkdir()

            _stage_runtime_skills(ctx, plan_dir, stage="plan")
            _stage_runtime_skills(ctx, repair_dir, stage="repair")

            plan_root = plan_dir / "runtime_skills/packs/landing.isolated/references"
            repair_root = repair_dir / "runtime_skills/packs/landing.isolated/references"
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

    def test_runtime_skill_snapshot_missing_allows_explicit_legacy_compat(self) -> None:
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
                run_id="legacy-skills",
            )
            ctx.state["legacy_runtime_skills_compat"] = True

            staged = _stage_runtime_skills(ctx, attempt, stage="plan")

            self.assertFalse(staged["catalog"]["available"])
            self.assertTrue(staged["catalog"]["legacy_compat"])

    def test_source_id_on_figure_wrapper_counts_the_nested_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            attempt = Path(temp_dir)
            layers = attempt / "layers"
            layers.mkdir()
            (layers / "figure.png").write_bytes(b"image")
            (attempt / "designer_author_done.json").write_text("{}", encoding="utf-8")
            (attempt / "landing_asset_catalog.json").write_text(
                json.dumps({
                    "assets": [{
                        "asset_id": "figure_01",
                        "output_file": "layers/figure.png",
                        "output_sha256": "figure-sha",
                        "visual_selection_tier": "eligible",
                    }]
                }),
                encoding="utf-8",
            )
            (attempt / "landing_visual_plan.json").write_text(
                json.dumps({
                    "optional_reserve_assets": [],
                    "validation_targets": {"required_unique_source_visuals": 1},
                }),
                encoding="utf-8",
            )
            (attempt / "index.html").write_text(
                """<!doctype html><html><body><main>
                <section data-section-role="hero"><h1>Paper</h1>
                <figure data-source-id="figure_01">
                  <img src="layers/figure.png" alt="Source evidence">
                </figure></section>
                <section data-section-role="method"><h2>Method</h2></section>
                <section data-section-role="results"><h2>Results</h2></section>
                <div style="display:none"><img src="layers/figure.png" alt=""></div>
                </main></body></html>""",
                encoding="utf-8",
            )

            diagnostics = _validate_landing_output(attempt)

        issue_ids = {finding["issue_id"] for finding in diagnostics["findings"]}
        self.assertNotIn("landing_source_visual_missing_id", issue_ids)
        self.assertNotIn("landing_source_visual_id_path_mismatch", issue_ids)
        self.assertEqual(diagnostics["metrics"]["used_source_visual_count"], 1)

    def test_hidden_interaction_copy_is_allowed_when_source_is_visibly_placed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            attempt = Path(temp_dir)
            layers = attempt / "layers"
            layers.mkdir()
            (layers / "figure.png").write_bytes(b"image")
            (attempt / "designer_author_done.json").write_text("{}", encoding="utf-8")
            (attempt / "landing_asset_catalog.json").write_text(
                json.dumps({
                    "assets": [{
                        "asset_id": "figure_01",
                        "output_file": "layers/figure.png",
                        "output_sha256": "figure-sha",
                        "visual_selection_tier": "eligible",
                    }]
                }),
                encoding="utf-8",
            )
            (attempt / "landing_visual_plan.json").write_text(
                json.dumps({
                    "optional_reserve_assets": [],
                    "validation_targets": {"required_unique_source_visuals": 1},
                }),
                encoding="utf-8",
            )
            (attempt / "index.html").write_text(
                """<!doctype html><html><head><style>
                .dialog { display:none }
                </style></head><body><main>
                <section data-section-role="hero"><h1>Paper</h1>
                <img src="layers/figure.png" data-source-id="figure_01"></section>
                <section data-section-role="method"><h2>Method</h2></section>
                <section data-section-role="results"><h2>Results</h2></section>
                <div class="dialog"><img src="layers/figure.png" data-source-id="figure_01"></div>
                </main></body></html>""",
                encoding="utf-8",
            )

            diagnostics = _validate_landing_output(attempt)

        issue_ids = {finding["issue_id"] for finding in diagnostics["findings"]}
        self.assertNotIn("landing_source_visual_not_visible", issue_ids)
        self.assertEqual(diagnostics["metrics"]["used_source_visual_count"], 1)

    def setUp(self) -> None:
        self.assertIsNotNone(
            ExternalLandingAuthor,
            "ExternalLandingAuthor has not been implemented",
        )
        self.assertIsNotNone(
            build_landing_visual_plan,
            "landing visual planning has not been implemented",
        )
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.browser_audit_patcher = patch(
            "autodesign.agents.external_landing_author.audit_landing_html",
            return_value={
                "kind": "artifact_browser_audit",
                "version": 1,
                "artifact_type": "landing",
                "backend": "test-playwright",
                "status": "ok",
                "accepted": True,
                "findings": [],
                "metrics": {"snapshot_count": 4},
                "warnings": [],
            },
        )
        self.browser_audit_patcher.start()

    def tearDown(self) -> None:
        self.browser_audit_patcher.stop()
        self.temp_dir.cleanup()

    def test_visual_plan_uses_full_provenance_and_balances_roles(self) -> None:
        provenance = self._provenance()
        poster_selected_ids = {"rejected_asset"}

        plan = build_landing_visual_plan(provenance)

        recommended = plan["recommended_assets"]
        recommended_ids = {item["asset_id"] for item in recommended}
        self.assertEqual(plan["source"], "paper_visual_provenance")
        self.assertEqual(len(recommended), 16)
        self.assertEqual(
            len({item["output_sha256"] for item in recommended}),
            len(recommended),
        )
        self.assertTrue(recommended_ids.isdisjoint(poster_selected_ids))
        self.assertEqual(
            {item["landing_role"] for item in recommended},
            set(_ROLES),
        )
        self.assertEqual(plan["eligible_asset_count"], 18)

    def test_visual_plan_adds_structured_experience_contract_and_current_color(self) -> None:
        required_color_system = {
            "palette_id": "plum_sage",
            "roles": {"primary_accent": "#6f2c63", "surface": "#ffffff"},
        }

        plan = build_landing_visual_plan(
            self._provenance(count=3),
            current_color_system=required_color_system,
        )

        contract = plan["visual_experience_contract"]
        self.assertEqual(contract["surface"]["mode"], "academic_light_editorial")
        self.assertEqual(contract["color"]["primary_accent_count"], 1)
        self.assertEqual(contract["color"]["current_color_system"], required_color_system)
        self.assertEqual(contract["icons"]["format"], "inline_svg")
        self.assertEqual(contract["icons"]["count"], {"min": 3, "max": 8})
        self.assertTrue(contract["interaction"]["source_grounded_required"])
        self.assertTrue(contract["motion"]["reduced_motion_required_when_used"])
        self.assertEqual(contract["motion"]["content_visibility"], "visible_without_javascript")
        self.assertFalse(contract["three_d"]["enabled"])
        self.assertEqual(contract["three_d"]["opt_in_source"], "none")

    def test_visual_plan_only_enables_3d_for_explicit_brief_or_source(self) -> None:
        default_plan = build_landing_visual_plan(self._provenance(count=1))
        brief_plan = build_landing_visual_plan(
            self._provenance(count=1),
            brief="Use the paper's interactive 3D reconstruction as evidence.",
        )
        source_plan = build_landing_visual_plan({
            "assets": [
                self._asset(1),
                {
                    "asset_id": "scene_model",
                    "kind": "model_3d",
                    "output_file": "layers/scene.glb",
                    "caption_short": "Source 3D reconstruction",
                },
            ]
        })
        incidental_mention_plan = build_landing_visual_plan({
            "assets": [self._asset(1, caption_short="Results shown as a 3D bar chart")]
        })

        self.assertFalse(default_plan["visual_experience_contract"]["three_d"]["enabled"])
        self.assertEqual(
            brief_plan["visual_experience_contract"]["three_d"]["opt_in_source"],
            "brief",
        )
        self.assertEqual(
            source_plan["visual_experience_contract"]["three_d"]["opt_in_source"],
            "source",
        )
        self.assertFalse(
            incidental_mention_plan["visual_experience_contract"]["three_d"]["enabled"]
        )

    def test_stages_full_ingest_catalog_and_promotes_valid_output(self) -> None:
        ctx = self._make_ctx(self._valid_fake_author())
        required_color_system = {
            "palette_id": "plum_sage",
            "roles": {"primary_accent": "#6f2c63", "surface": "#ffffff"},
        }
        ctx.state["poster_content_brief"] = {
            "color_system": required_color_system,
        }
        author = ExternalLandingAuthor(ctx.settings, "INTERNAL_DESIGNER_LOOP_PROMPT")

        screenshot_calls: list[dict[str, object]] = []

        def fake_screenshot(_html_path, out_path, **kwargs):
            screenshot_calls.append(dict(kwargs))
            out_path.write_bytes(b"preview")
            return SimpleNamespace(
                backend="test-renderer",
                warnings=[],
                paths=[out_path],
                width_px=1440,
                height_px=2400,
            )

        with (
            patch(
                "autodesign.agents.external_landing_author.screenshot_html",
                side_effect=fake_screenshot,
            ),
            patch("autodesign.agents.external_landing_author.log") as log_event,
        ):
            author.run("Create the paper project page.", ctx)

        attempt_dir = ctx.run_dir / "landing_author" / "attempt_01"
        catalog = json.loads(
            (attempt_dir / "landing_asset_catalog.json").read_text(encoding="utf-8")
        )
        plan = json.loads(
            (attempt_dir / "landing_visual_plan.json").read_text(encoding="utf-8")
        )
        manifest = json.loads(
            (attempt_dir / "author_input_manifest.json").read_text(encoding="utf-8")
        )
        browser_qa = json.loads(
            (attempt_dir / "landing_browser_qa.json").read_text(encoding="utf-8")
        )

        self.assertEqual(len(catalog["assets"]), 18)
        self.assertNotIn(
            "rejected_asset",
            {item["asset_id"] for item in catalog["assets"]},
        )
        self.assertEqual(len(plan["recommended_assets"]), 16)
        self.assertEqual(
            plan["visual_experience_contract"]["color"]["current_color_system"],
            required_color_system,
        )
        self.assertEqual(manifest["required_color_system"], required_color_system)
        self.assertTrue(browser_qa["accepted"])
        self.assertTrue((attempt_dir / "attempt_candidate.json").is_file())
        self.assertTrue((attempt_dir / "candidate" / "index.html").is_file())
        self.assertTrue(all(call["prime_local_media"] for call in screenshot_calls))
        self.assertTrue(
            any(
                item["asset_id"] not in {"rejected_asset"}
                for item in plan["recommended_assets"]
            )
        )
        self.assertEqual(
            manifest["output_contract"],
            {
                "html": "index.html",
                "done_marker": "designer_author_done.json",
            },
        )
        self.assertEqual(manifest["runtime_skills"]["catalog"]["stage"], "plan")
        self.assertIn("runtime_skills/index.md", manifest["runtime_skills"]["files"])
        self.assertNotIn("runtime_skills/snapshot.json", manifest["runtime_skills"]["files"])
        for relative_path in (
            "paper_memory.json",
            "paper_memory.md",
            "paper_memory_dossier.json",
            "paper_memory_dossier.md",
            "paper_visual_provenance.json",
            "paper_evidence_packs/method.md",
            "layers/asset_01.png",
            "runtime_skills/index.md",
            "runtime_skills/packs/landing.visual_recipe/SKILL.md",
        ):
            self.assertTrue((attempt_dir / relative_path).exists(), relative_path)
        staged_skill = attempt_dir / "runtime_skills/packs/landing.visual_recipe/SKILL.md"
        self.assertEqual(staged_skill.stat().st_mode & stat.S_IWUSR, 0)
        prompt = (attempt_dir / "prompt_seen.txt").read_text(encoding="utf-8")
        canonical_path = attempt_dir / "landing_author_prompt.md"
        self.assertTrue(canonical_path.exists())
        canonical_prompt = canonical_path.read_text(encoding="utf-8")
        self.assertEqual(prompt, canonical_prompt)
        self.assertNotIn("INTERNAL_DESIGNER_LOOP_PROMPT", canonical_prompt)
        self.assertIn("self-contained dynamic academic project page for desktop browsers", prompt)
        self.assertNotIn("responsive CSS", prompt)
        self.assertIn("real paper figures and tables", prompt)
        self.assertIn("native editable text", prompt)
        self.assertIn("no remote assets or scripts", prompt)
        self.assertIn("not a SaaS card wall", prompt)
        self.assertIn("academic-light editorial", prompt)
        self.assertIn("one primary accent", prompt)
        self.assertIn("3 to 8 restrained inline SVG icons", prompt)
        self.assertIn("purposeful source-grounded interaction", prompt)
        self.assertIn("prefers-reduced-motion", prompt)
        self.assertIn("must not depend on JavaScript reveal", prompt)
        self.assertIn("data-source-id equal to its catalog asset_id", prompt)
        self.assertIn("3D is disabled", prompt)
        self.assertIn('"palette_id": "plum_sage"', prompt)
        self.assertIn("Read runtime_skills/index.md first", prompt)
        self.assertNotIn("RUNTIME_SKILL_BODY_MUST_NOT_BE_INLINED", prompt)
        self.assertTrue((attempt_dir / "index.html").exists())
        self.assertTrue((attempt_dir / "designer_author_done.json").exists())
        self.assertTrue((ctx.run_dir / "final" / "index.html").exists())
        self.assertTrue((ctx.run_dir / "final" / "layers" / "asset_01.png").exists())
        self.assertTrue((ctx.run_dir / "final" / "preview.png").exists())
        self.assertTrue((ctx.run_dir / "final" / "card_preview.png").exists())
        self.assertEqual(
            [call.get("full_page") for call in screenshot_calls[-2:]],
            [True, False],
        )
        final_manifest = json.loads(
            (ctx.run_dir / "final" / "landing_author_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            final_manifest["card_preview_relative_path"],
            "final/card_preview.png",
        )
        self.assertEqual(
            final_manifest["card_preview_sha256"],
            hashlib.sha256(b"preview").hexdigest(),
        )
        self.assertTrue(ctx.state["finalized"])
        log_event.assert_any_call(
            "landing_author.attempt_start",
            mode="external",
            attempt=1,
            max_attempts=ctx.settings.designer_author_max_attempts,
        )

    def test_promotion_is_atomic_when_preview_render_fails(self) -> None:
        ctx = self._make_ctx(self._valid_fake_author())
        final_dir = ctx.run_dir / "final"
        final_dir.mkdir()
        (final_dir / "index.html").write_text("old final", encoding="utf-8")
        (final_dir / "preview.png").write_bytes(b"old preview")
        author = ExternalLandingAuthor(ctx.settings, "system")

        with patch(
            "autodesign.agents.external_landing_author.screenshot_html",
            side_effect=RuntimeError("renderer unavailable"),
        ):
            author.run("Create the paper project page.", ctx)

        self.assertEqual(
            (final_dir / "index.html").read_text(encoding="utf-8"),
            "old final",
        )
        self.assertEqual((final_dir / "preview.png").read_bytes(), b"old preview")
        self.assertFalse(ctx.state["finalized"])
        self.assertEqual(
            ctx.state["designer_api_error"]["issue_id"],
            "external_landing_promotion_failed",
        )

    def test_process_logs_redact_harness_key_and_command_tokens(self) -> None:
        secret = "landing-harness-secret-value"
        token = "landing-command-token-value"
        inherited_secret = "landing-inherited-provider-secret"
        script = self._write_fake_author(
            """
            import os
            print('key=' + os.environ.get('ANTHROPIC_AUTH_TOKEN', ''))
            print(os.environ.get('OPENAI_API_KEY', ''))
            print('argv=' + ' '.join(sys.argv))
            open('index.html', 'w', encoding='utf-8').write('<html></html>')
            json.dump({'status': 'complete'}, open('designer_author_done.json', 'w', encoding='utf-8'))
            """,
            imports="import json, os, sys\n",
        )
        ctx = self._make_ctx(script)
        ctx.settings.designer_author_cmd = (
            f"{sys.executable} {script} --token {token} --api-key={secret}"
        )
        ctx.settings.harness_api_key = secret
        author = ExternalLandingAuthor(ctx.settings, "system")
        attempt = ctx.run_dir / "landing_author" / "attempt_01"
        attempt.mkdir(parents=True)

        with patch.dict(os.environ, {"OPENAI_API_KEY": inherited_secret}):
            author._invoke_author_command(
                ctx.settings.designer_author_cmd,
                prompt="prompt",
                attempt_dir=attempt,
                timeout_s=10,
            )

        persisted = (
            (attempt / "designer_author_stdout.log").read_text(encoding="utf-8")
            + (attempt / "designer_author_stderr.log").read_text(encoding="utf-8")
        )
        self.assertNotIn(secret, persisted)
        self.assertNotIn(token, persisted)
        self.assertNotIn(inherited_secret, persisted)
        self.assertIn("[REDACTED]", persisted)

    def test_staged_source_visual_bytes_must_match_trusted_source_hash(self) -> None:
        attempt = self._write_validation_candidate(source_asset=True)
        source = attempt / "layers" / "asset_01.png"
        trusted_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        source.write_bytes(b"coding-agent-replacement")

        diagnostics = _validate_landing_output(
            attempt,
            trusted_source_hashes={"asset_01": trusted_hash},
        )

        self.assertFalse(diagnostics["accepted"])
        self.assertIn(
            "landing_source_visual_hash_mismatch",
            {finding["issue_id"] for finding in diagnostics["findings"]},
        )

    def test_interrupted_promotion_never_promotes_unvalidated_staging(self) -> None:
        root = self.root / "landing_promotion_recovery"
        root.mkdir()
        final_dir = root / "final"
        backup_dir = root / ".landing-final-backup-interrupted"
        staging_dir = root / ".landing-final-staging-interrupted"
        backup_dir.mkdir()
        staging_dir.mkdir()
        final_dir.mkdir()
        (final_dir / "index.html").write_text("untrusted", encoding="utf-8")
        (backup_dir / "index.html").write_text("old", encoding="utf-8")
        (staging_dir / "index.html").write_text("new", encoding="utf-8")
        (root / ".landing-final-promotion.json").write_text(
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
            (final_dir / "index.html").read_text(encoding="utf-8"),
            "untrusted",
        )
        self.assertTrue(backup_dir.is_dir())
        self.assertEqual(
            (backup_dir / "index.html").read_text(encoding="utf-8"),
            "old",
        )
        self.assertFalse(staging_dir.exists())
        self.assertTrue((root / ".landing-final-promotion.json").is_file())

    def test_promotion_recovery_rejects_backup_namespace_as_staging(self) -> None:
        root = self.root / "landing_promotion_alias"
        root.mkdir()
        final_dir = root / "final"
        backup_dir = root / ".landing-final-backup-alias"
        final_dir.mkdir()
        backup_dir.mkdir()
        (final_dir / "index.html").write_text("current", encoding="utf-8")
        (backup_dir / "index.html").write_text("old", encoding="utf-8")
        (root / ".landing-final-promotion.json").write_text(
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
            (backup_dir / "index.html").read_text(encoding="utf-8"),
            "old",
        )
        self.assertEqual(
            (final_dir / "index.html").read_text(encoding="utf-8"),
            "current",
        )
        self.assertTrue((root / ".landing-final-promotion.json").is_file())

    def test_promotion_recovery_unlinks_staging_symlink_without_touching_backup(self) -> None:
        root = self.root / "landing_promotion_symlink"
        root.mkdir()
        final_dir = root / "final"
        backup_dir = root / ".landing-final-backup-protected"
        staging_link = root / ".landing-final-staging-link"
        final_dir.mkdir()
        backup_dir.mkdir()
        (backup_dir / "index.html").write_text("old", encoding="utf-8")
        staging_link.symlink_to(backup_dir, target_is_directory=True)
        (root / ".landing-final-promotion.json").write_text(
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
            (backup_dir / "index.html").read_text(encoding="utf-8"),
            "old",
        )

    def test_resume_uses_original_source_hash_anchor_after_layer_mutation(self) -> None:
        ctx = self._make_ctx(self._valid_fake_author(), max_attempts=1)
        catalog = build_landing_asset_catalog(ctx.state["paper_visual_provenance"])
        original_hashes = _trusted_landing_source_hashes(ctx.run_dir, catalog)
        source_id = next(iter(original_hashes))
        source_record = next(
            asset
            for asset in catalog["assets"]
            if asset["asset_id"] == source_id
        )
        (ctx.run_dir / source_record["output_file"]).write_bytes(b"mutated-after-run")
        previous = ctx.run_dir / "landing_author" / "attempt_01"
        previous.mkdir(parents=True)
        (previous / "index.html").write_text("<html><body>prior</body></html>", encoding="utf-8")
        ctx.state["landing_author_attempts"] = 1
        ctx.state["external_author_resume"] = {
            "prior_attempts": 1,
            "previous_attempt_dir": str(previous),
            "repair_feedback": {"accepted": False, "findings": []},
        }

        ExternalLandingAuthor(ctx.settings, "system").run(
            "Create the paper project page.",
            ctx,
        )

        anchored = json.loads(
            (ctx.run_dir / "landing_trusted_source_hashes.json").read_text(encoding="utf-8")
        )["hashes"]
        validation = json.loads(
            (ctx.run_dir / "landing_author/attempt_02/landing_validation.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(anchored, original_hashes)
        self.assertIn(
            "landing_source_visual_hash_mismatch",
            {finding["issue_id"] for finding in validation["findings"]},
        )
        self.assertFalse((ctx.run_dir / "final").exists())

    def test_resume_rejects_anchor_replaced_to_match_mutated_layer(self) -> None:
        ctx = self._make_ctx(self._valid_fake_author(), max_attempts=1)
        catalog = build_landing_asset_catalog(ctx.state["paper_visual_provenance"])
        source_record = catalog["assets"][0]
        source_path = ctx.run_dir / source_record["output_file"]
        source_record["output_sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
        _trusted_landing_source_hashes(ctx.run_dir, catalog)
        source_path.write_bytes(b"mutated-and-rebaselined")
        forged_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        anchor_path = ctx.run_dir / "landing_trusted_source_hashes.json"
        anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
        anchor["hashes"][source_record["asset_id"]] = forged_hash
        anchor_path.chmod(0o644)
        anchor_path.write_text(json.dumps(anchor), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "disagrees with ingest provenance"):
            _trusted_landing_source_hashes(
                ctx.run_dir,
                catalog,
                require_existing=True,
            )

    def test_resume_rejects_catalog_that_drops_trusted_source_ids(self) -> None:
        ctx = self._make_ctx(self._valid_fake_author(), max_attempts=1)
        catalog = build_landing_asset_catalog(ctx.state["paper_visual_provenance"])
        trusted = _trusted_landing_source_hashes(ctx.run_dir, catalog)
        self.assertGreater(len(trusted), 1)
        reduced_catalog = {
            **catalog,
            "assets": [
                asset
                for asset in catalog["assets"]
                if asset.get("asset_id") != next(iter(trusted))
            ],
        }

        with self.assertRaisesRegex(ValueError, "catalog"):
            _trusted_landing_source_hashes(
                ctx.run_dir,
                reduced_catalog,
                require_existing=True,
            )

    def test_browser_layout_failure_retries_before_promotion(self) -> None:
        ctx = self._make_ctx(self._valid_fake_author(), max_attempts=2)
        author = ExternalLandingAuthor(ctx.settings, "system")
        failed_audit = {
            "kind": "artifact_browser_audit",
            "version": 1,
            "artifact_type": "landing",
            "backend": "test-playwright",
            "status": "error",
            "accepted": False,
            "findings": [{
                "id": "landing_required_source_not_visible",
                "severity": "error",
                "message": "required evidence is not visibly painted",
                "evidence": {"source_id": "asset_01"},
            }],
            "metrics": {"snapshot_count": 4},
            "warnings": [],
        }
        passed_audit = {
            **failed_audit,
            "status": "ok",
            "accepted": True,
            "findings": [],
        }

        def fake_screenshot(_html_path, out_path, **_kwargs):
            out_path.write_bytes(b"preview")
            return SimpleNamespace(
                backend="test-renderer",
                warnings=[],
                paths=[out_path],
                width_px=1440,
                height_px=2400,
            )

        with patch(
            "autodesign.agents.external_landing_author.audit_landing_html",
            side_effect=[failed_audit, passed_audit],
        ), patch(
            "autodesign.agents.external_landing_author.screenshot_html",
            side_effect=fake_screenshot,
        ):
            author.run("Create the paper project page.", ctx)

        first_validation = json.loads(
            (ctx.run_dir / "landing_author" / "attempt_01" / "landing_validation.json").read_text()
        )
        self.assertIn(
            "landing_required_source_not_visible",
            {finding["issue_id"] for finding in first_validation["findings"]},
        )
        self.assertTrue((ctx.run_dir / "landing_author" / "attempt_02" / "landing_browser_qa.json").is_file())
        self.assertTrue((ctx.run_dir / "final" / "index.html").is_file())
        self.assertEqual(ctx.state["landing_author_attempts"], 2)
        self.assertEqual(ctx.state["landing_author_direct_final"]["source"], "external_landing_author")
        self.assertEqual(author.token_totals, (0, 0))
        self.assertEqual(author.cache_totals, (0, 0))

    def test_validation_failure_retries_with_patch_first_context(self) -> None:
        ctx = self._make_ctx(self._repairing_fake_author(), max_attempts=2)
        author = ExternalLandingAuthor(ctx.settings, "internal prompt must stay hidden")

        with patch("autodesign.agents.external_landing_author.screenshot_html") as render:
            render.side_effect = self._fake_screenshot
            author.run("Create the paper project page.", ctx)

        second = ctx.run_dir / "landing_author" / "attempt_02"
        self.assertTrue((ctx.run_dir / "final" / "index.html").exists())
        self.assertTrue((second / "previous_index.html").exists())
        self.assertTrue((second / "index.html").exists())
        compact_findings = json.loads(
            (second / "previous_landing_validation.json").read_text(encoding="utf-8")
        )
        self.assertIn("findings", compact_findings)
        prompt = (second / "landing_author_prompt.md").read_text(encoding="utf-8")
        self.assertIn("PATCH-FIRST REPAIR ATTEMPT", prompt)
        self.assertIn("landing_insufficient_source_visual_density", prompt)
        self.assertFalse(ctx.state.get("designer_contract_abort", False))

    def test_process_no_output_retries_within_attempt_budget(self) -> None:
        ctx = self._make_ctx(self._no_output_then_valid_author(), max_attempts=2)
        author = ExternalLandingAuthor(ctx.settings, "hidden internal prompt")

        with patch("autodesign.agents.external_landing_author.screenshot_html") as render:
            render.side_effect = self._fake_screenshot
            author.run("Create the paper project page.", ctx)

        self.assertTrue((ctx.run_dir / "landing_author" / "attempt_02").exists())
        self.assertTrue((ctx.run_dir / "final" / "index.html").exists())
        self.assertFalse(ctx.state.get("designer_contract_abort", False))

    def test_retry_does_not_trust_run_layer_poisoned_by_prior_attempt(self) -> None:
        script = self._write_fake_author(
            """
            cwd = pathlib.Path.cwd()
            if cwd.name == 'attempt_01':
                (cwd.parents[1] / 'layers' / 'asset_01.png').write_bytes(b'poisoned-source')
                raise SystemExit(0)
            catalog = json.load(open('landing_asset_catalog.json', encoding='utf-8'))
            sources = catalog['assets'][:8]
            images = ''.join(
                f'<img src="{source["output_file"]}" data-source-id="{source["asset_id"]}">'
                for source in sources
            )
            words = ' '.join(['grounded'] * 100)
            interaction = f'<button data-source-id="{sources[0]["asset_id"]}">Inspect evidence</button>'
            html = f'''<!doctype html><html><head>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>@media (max-width:700px){{section{{display:block}}}}</style></head><body><main>
            <section id="hero"><h1>Synthetic Paper</h1><p>{words}</p>{images}{interaction}</section>
            <section id="method"><h2>Method</h2><p>{words}</p></section>
            <section id="results"><h2>Results</h2><p>{words}</p></section>
            </main></body></html>'''
            open('index.html', 'w', encoding='utf-8').write(html)
            json.dump({'status': 'complete'}, open('designer_author_done.json', 'w', encoding='utf-8'))
            """,
            imports="import json, pathlib, sys\n",
        )
        ctx = self._make_ctx(script, max_attempts=2)

        ExternalLandingAuthor(ctx.settings, "system").run(
            "Create the paper project page.",
            ctx,
        )

        validation = json.loads(
            (ctx.run_dir / "landing_author/attempt_02/landing_validation.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn(
            "landing_source_visual_hash_mismatch",
            {finding["issue_id"] for finding in validation["findings"]},
        )
        self.assertFalse((ctx.run_dir / "final").exists())

    def test_resume_starts_patch_first_from_external_author_baseline(self) -> None:
        ctx = self._make_ctx(self._valid_fake_author(), max_attempts=1)
        _trusted_landing_source_hashes(
            ctx.run_dir,
            build_landing_asset_catalog(ctx.state["paper_visual_provenance"]),
        )
        previous = ctx.run_dir / "landing_author" / "attempt_03"
        previous.mkdir(parents=True)
        (previous / "index.html").write_text("<html><body>prior</body></html>", encoding="utf-8")
        feedback = {
            "accepted": False,
            "findings": [{"issue_id": "landing_resume_gap", "message": "repair prior gap"}],
        }
        ctx.state["landing_author_attempts"] = 3
        ctx.state["external_author_resume"] = {
            "prior_attempts": 3,
            "previous_attempt_dir": str(previous),
            "repair_feedback": feedback,
            "source_run_dir": str(ctx.run_dir),
        }

        with patch("autodesign.agents.external_landing_author.screenshot_html") as render:
            render.side_effect = self._fake_screenshot
            ExternalLandingAuthor(ctx.settings, "system").run("Create the paper project page.", ctx)

        resumed = ctx.run_dir / "landing_author" / "attempt_04"
        self.assertTrue((resumed / "previous_index.html").is_file())
        self.assertIn(
            "PATCH-FIRST REPAIR ATTEMPT",
            (resumed / "landing_author_prompt.md").read_text(encoding="utf-8"),
        )
        self.assertNotIn("external_author_resume", ctx.state)

    def test_missing_dossier_is_fail_open(self) -> None:
        ctx = self._make_ctx(self._valid_fake_author(), include_dossier=False)
        author = ExternalLandingAuthor(ctx.settings, "hidden internal prompt")

        with patch("autodesign.agents.external_landing_author.screenshot_html") as render:
            render.side_effect = self._fake_screenshot
            author.run("Create the paper project page.", ctx)

        attempt = ctx.run_dir / "landing_author" / "attempt_01"
        self.assertTrue((ctx.run_dir / "final" / "index.html").exists())
        self.assertFalse((attempt / "paper_memory_dossier.json").exists())
        prompt = (attempt / "landing_author_prompt.md").read_text(encoding="utf-8")
        self.assertIn("dossier is absent", prompt)
        self.assertIn("fall back to paper_memory", prompt)

    def test_visual_plan_recomputes_stale_eligibility_with_rendered_layers(self) -> None:
        provenance = {
            "assets": [
                self._asset(1, designer_eligible=False),
                self._asset(2, designer_eligible=False),
            ]
        }
        rendered_layers = {
            "asset_02": {"curation_flags": ["body_text_leak"]},
        }

        try:
            plan = build_landing_visual_plan(
                provenance,
                rendered_layers=rendered_layers,
            )
        except TypeError as exc:
            self.fail(f"visual plan must accept rendered_layers: {exc}")

        self.assertEqual(
            [item["asset_id"] for item in plan["recommended_assets"]],
            ["asset_01"],
        )

    def test_unmatched_reserve_is_optional_shortfall_only(self) -> None:
        eligible = self._asset(1)
        reserves = [
            self._asset(
                index,
                asset_id=f"ingest_fig_{index:02d}",
                caption_short="",
                caption_association_method="unmatched",
                caption_confidence=0.0,
                captioned_source_group=False,
                visual_role="method" if index == 2 else "results",
                extract_strategy="raster",
                source_page=index,
            )
            for index in range(2, 5)
        ]
        provenance = {"assets": [eligible, *reserves]}

        catalog = build_landing_asset_catalog(provenance)
        plan = build_landing_visual_plan(provenance)

        self.assertEqual(catalog["eligible_asset_count"], 1)
        self.assertEqual(catalog["reserve_asset_count"], 3)
        self.assertEqual(
            [item["asset_id"] for item in plan["recommended_assets"]],
            ["asset_01"],
        )
        self.assertEqual(
            [item["asset_id"] for item in plan["optional_reserve_assets"]],
            ["ingest_fig_02", "ingest_fig_03"],
        )
        self.assertTrue(
            all(item["story_role"] == "supporting" for item in plan["optional_reserve_assets"])
        )
        self.assertEqual(plan["validation_targets"]["required_unique_source_visuals"], 1)

    def test_hidden_source_visuals_do_not_satisfy_density(self) -> None:
        ctx = self._make_ctx(self._hidden_visual_fake_author())
        author = ExternalLandingAuthor(ctx.settings, "hidden internal prompt")

        author.run("Create the paper project page.", ctx)

        diagnostics = ctx.state["landing_author_validation"]
        finding_ids = {finding["issue_id"] for finding in diagnostics["findings"]}
        self.assertIn("landing_source_visual_not_visible", finding_ids)
        self.assertIn("landing_insufficient_source_visual_density", finding_ids)
        self.assertEqual(diagnostics["metrics"]["used_source_visual_count"], 0)

    def test_visual_density_requires_eight_unique_source_visuals(self) -> None:
        ctx = self._make_ctx(self._sparse_fake_author())
        author = ExternalLandingAuthor(ctx.settings, "hidden internal prompt")

        with patch(
            "autodesign.agents.external_landing_author.screenshot_html",
            side_effect=self._fake_screenshot,
        ):
            author.run("Create the paper project page.", ctx)

        diagnostics = json.loads(
            (
                ctx.run_dir / "landing_author" / "attempt_01" / "landing_validation.json"
            ).read_text(encoding="utf-8")
        )
        density = next(
            (
                finding
                for finding in diagnostics["findings"]
                if finding["issue_id"] == "landing_insufficient_source_visual_density"
            ),
            None,
        )
        self.assertIsNotNone(density)
        self.assertEqual(density["required"], 8)
        self.assertEqual(density["actual"], 1)
        self.assertTrue((ctx.run_dir / "final" / "index.html").is_file())
        manifest = json.loads(
            (ctx.run_dir / "final" / "landing_author_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["quality_status"], "ready_with_warnings")
        self.assertIn(
            "landing_insufficient_source_visual_density",
            manifest["quality_diagnostics"],
        )

    def test_visual_density_scales_to_corpus_capacity(self) -> None:
        provenance = self._provenance(count=3)
        ctx = self._make_ctx(self._valid_fake_author(), provenance=provenance)
        author = ExternalLandingAuthor(ctx.settings, "hidden internal prompt")

        with patch("autodesign.agents.external_landing_author.screenshot_html") as render:
            render.side_effect = self._fake_screenshot
            author.run("Create the paper project page.", ctx)

        diagnostics = ctx.state["landing_author_validation"]
        self.assertTrue(diagnostics["accepted"])
        self.assertEqual(diagnostics["metrics"].get("required_source_visual_count"), 3)

    def test_source_id_must_match_the_local_asset_path(self) -> None:
        ctx = self._make_ctx(self._mismatched_source_id_author())
        author = ExternalLandingAuthor(ctx.settings, "hidden internal prompt")

        author.run("Create the paper project page.", ctx)

        diagnostics = ctx.state["landing_author_validation"]
        self.assertIn(
            "landing_source_visual_id_path_mismatch",
            {finding["issue_id"] for finding in diagnostics["findings"]},
        )
        mismatch_messages = [
            finding["message"]
            for finding in diagnostics["findings"]
            if finding["issue_id"] == "landing_source_visual_id_path_mismatch"
        ]
        self.assertTrue(
            all("expected data-source-id" in message for message in mismatch_messages),
            mismatch_messages,
        )

    def test_rejects_remote_asset_and_leaves_validation_diagnostics(self) -> None:
        ctx = self._make_ctx(self._unsafe_fake_author())
        author = ExternalLandingAuthor(ctx.settings, "system prompt")

        author.run("Create the paper project page.", ctx)

        attempt_dir = ctx.run_dir / "landing_author" / "attempt_01"
        diagnostics = json.loads(
            (attempt_dir / "landing_validation.json").read_text(encoding="utf-8")
        )
        self.assertFalse(diagnostics["accepted"])
        self.assertIn(
            "landing_remote_reference",
            {finding["issue_id"] for finding in diagnostics["findings"]},
        )
        self.assertFalse((ctx.run_dir / "final" / "index.html").exists())
        self.assertFalse(ctx.state["finalized"])
        self.assertEqual(
            ctx.state["designer_api_error"]["issue_id"],
            "external_landing_validation_failed",
        )

    def test_rejects_malformed_done_marker(self) -> None:
        ctx = self._make_ctx(self._invalid_done_fake_author())
        author = ExternalLandingAuthor(ctx.settings, "system prompt")

        author.run("Create the paper project page.", ctx)

        diagnostics = json.loads(
            (
                ctx.run_dir
                / "landing_author"
                / "attempt_01"
                / "landing_validation.json"
            ).read_text(encoding="utf-8")
        )
        self.assertIn(
            "landing_invalid_done_marker",
            {finding["issue_id"] for finding in diagnostics["findings"]},
        )
        self.assertFalse((ctx.run_dir / "final" / "index.html").exists())

    def test_motion_without_reduced_motion_hard_fails(self) -> None:
        attempt = self._write_validation_candidate(
            style=".result { transition: opacity 180ms ease; }",
        )

        diagnostics = _validate_landing_output(attempt)

        self.assertFalse(diagnostics["accepted"])
        self.assertIn(
            "landing_motion_without_reduced_motion",
            {finding["issue_id"] for finding in diagnostics["findings"]},
        )
        self.assertGreater(diagnostics["metrics"]["motion_declaration_count"], 0)
        self.assertFalse(diagnostics["metrics"]["has_prefers_reduced_motion"])

    def test_desktop_landing_does_not_require_mobile_viewport_metadata(self) -> None:
        attempt = self._write_validation_candidate()
        html_path = attempt / "index.html"
        html_path.write_text(
            html_path.read_text(encoding="utf-8").replace(
                '<meta name="viewport" content="width=device-width, initial-scale=1">',
                "",
            ),
            encoding="utf-8",
        )

        diagnostics = _validate_landing_output(attempt)

        self.assertNotIn(
            "landing_missing_viewport",
            {finding["issue_id"] for finding in diagnostics["findings"]},
        )

    def test_empty_reduced_motion_rule_does_not_satisfy_contract(self) -> None:
        attempt = self._write_validation_candidate(
            style="""
            .result { transition: opacity 180ms ease; }
            @media (prefers-reduced-motion: reduce) { .result { color: inherit; } }
            """,
        )

        diagnostics = _validate_landing_output(attempt)

        self.assertFalse(diagnostics["accepted"])
        self.assertTrue(diagnostics["metrics"]["has_prefers_reduced_motion"])
        self.assertFalse(diagnostics["metrics"]["has_effective_reduced_motion"])
        self.assertIn(
            "landing_motion_without_reduced_motion",
            {finding["issue_id"] for finding in diagnostics["findings"]},
        )

    def test_linked_stylesheet_motion_is_validated(self) -> None:
        attempt = self._write_validation_candidate()
        (attempt / "site.css").write_text(
            ".result { animation: pulse 1s ease; }",
            encoding="utf-8",
        )
        html_path = attempt / "index.html"
        html_path.write_text(
            html_path.read_text(encoding="utf-8").replace(
                "</head>", '<link rel="stylesheet" href="site.css"></head>'
            ),
            encoding="utf-8",
        )

        diagnostics = _validate_landing_output(attempt)

        self.assertFalse(diagnostics["accepted"])
        self.assertIn(
            "landing_motion_without_reduced_motion",
            {finding["issue_id"] for finding in diagnostics["findings"]},
        )

    def test_linked_stylesheet_urls_resolve_relative_to_stylesheet(self) -> None:
        attempt = self._write_validation_candidate()
        styles = attempt / "styles"
        styles.mkdir()
        assets = attempt / "assets"
        assets.mkdir()
        (assets / "background.png").write_bytes(b"image")
        (styles / "site.css").write_text(
            ".result { background-image: url('../assets/background.png'); }",
            encoding="utf-8",
        )
        html_path = attempt / "index.html"
        html_path.write_text(
            html_path.read_text(encoding="utf-8").replace(
                "</head>", '<link rel="stylesheet" href="styles/site.css"></head>'
            ),
            encoding="utf-8",
        )

        diagnostics = _validate_landing_output(attempt)

        self.assertNotIn(
            "landing_invalid_local_reference",
            {finding["issue_id"] for finding in diagnostics["findings"]},
        )

    def test_plain_no_motion_page_still_passes(self) -> None:
        attempt = self._write_validation_candidate(
            style="* { transition: none; animation-duration: 0s; scroll-behavior: auto; }",
        )

        diagnostics = _validate_landing_output(attempt)

        self.assertTrue(diagnostics["accepted"], diagnostics["findings"])
        self.assertEqual(diagnostics["metrics"]["motion_declaration_count"], 0)
        self.assertFalse(diagnostics["metrics"]["has_prefers_reduced_motion"])

    def test_reports_icons_interaction_and_reduced_motion_metrics(self) -> None:
        icons = "".join(
            '<svg class="icon" aria-hidden="true" viewBox="0 0 16 16"></svg>'
            for _ in range(3)
        )
        attempt = self._write_validation_candidate(
            style="""
            .result { animation: settle 180ms ease; }
            @keyframes settle { from { opacity: .9; } to { opacity: 1; } }
            @media (prefers-reduced-motion: reduce) {
              .result { animation: none; transition: none; }
            }
            """,
            extra_body=(
                f'<button data-source-id="asset_01" aria-label="Inspect source figure">{icons}</button>'
            ),
            source_asset=True,
        )

        diagnostics = _validate_landing_output(attempt)

        self.assertTrue(diagnostics["accepted"], diagnostics["findings"])
        self.assertEqual(diagnostics["metrics"]["inline_svg_icon_count"], 3)
        self.assertEqual(diagnostics["metrics"]["interactive_control_count"], 1)
        self.assertEqual(diagnostics["metrics"]["source_grounded_interaction_count"], 1)
        self.assertTrue(diagnostics["metrics"]["has_prefers_reduced_motion"])
        self.assertTrue(diagnostics["metrics"]["has_effective_reduced_motion"])

    def test_required_source_grounded_interaction_is_enforced(self) -> None:
        attempt = self._write_validation_candidate(source_asset=True)
        plan_path = attempt / "landing_visual_plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["visual_experience_contract"] = {
            "interaction": {"source_grounded_required": True},
        }
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        diagnostics = _validate_landing_output(attempt)

        self.assertFalse(diagnostics["accepted"])
        self.assertIn(
            "landing_missing_source_grounded_interaction",
            {finding["issue_id"] for finding in diagnostics["findings"]},
        )

    def test_explicit_zero_source_target_does_not_promote_reserves_to_required(self) -> None:
        attempt = self._write_validation_candidate()
        (attempt / "layers").mkdir()
        (attempt / "layers" / "reserve.png").write_bytes(b"reserve")
        (attempt / "landing_asset_catalog.json").write_text(
            json.dumps({
                "assets": [{
                    "asset_id": "reserve_01",
                    "output_file": "layers/reserve.png",
                    "output_sha256": "reserve-sha",
                    "visual_selection_tier": "reserve_unmatched",
                }],
            }),
            encoding="utf-8",
        )

        diagnostics = _validate_landing_output(attempt)

        self.assertTrue(diagnostics["accepted"], diagnostics["findings"])
        self.assertEqual(diagnostics["metrics"]["required_source_visual_count"], 0)

    def test_icon_only_control_without_accessible_name_hard_fails(self) -> None:
        attempt = self._write_validation_candidate(
            extra_body="""
            <button><svg aria-hidden="true" viewBox="0 0 16 16"></svg></button>
            <button aria-label="Open figure"><svg aria-hidden="true" viewBox="0 0 16 16"></svg></button>
            <button><svg aria-label="Next figure" viewBox="0 0 16 16"></svg></button>
            """,
        )

        diagnostics = _validate_landing_output(attempt)

        self.assertFalse(diagnostics["accepted"])
        self.assertIn(
            "landing_icon_control_missing_accessible_name",
            {finding["issue_id"] for finding in diagnostics["findings"]},
        )
        self.assertEqual(diagnostics["metrics"]["icon_only_control_count"], 3)
        self.assertEqual(
            diagnostics["metrics"]["inaccessible_icon_only_control_count"],
            1,
        )

    def test_javascript_reveal_cannot_gate_content_visibility(self) -> None:
        attempt = self._write_validation_candidate(
            style=".reveal { opacity: 0; } .reveal.is-visible { opacity: 1; }",
            extra_body='<p class="reveal">Evidence gated by JavaScript.</p>',
            script="""
            new IntersectionObserver(entries => {
              entries.forEach(entry => entry.target.classList.add('is-visible'));
            });
            """,
        )

        diagnostics = _validate_landing_output(attempt)

        self.assertFalse(diagnostics["accepted"])
        self.assertIn(
            "landing_javascript_reveal_dependency",
            {finding["issue_id"] for finding in diagnostics["findings"]},
        )
        self.assertEqual(diagnostics["metrics"]["javascript_reveal_dependency_count"], 1)

    def _make_ctx(
        self,
        fake_author: Path,
        *,
        provenance: dict | None = None,
        include_dossier: bool = True,
        max_attempts: int = 1,
    ) -> ToolContext:
        run_dir = self.root / f"run_{fake_author.stem}"
        layers_dir = run_dir / "layers"
        layers_dir.mkdir(parents=True)
        provenance = provenance or self._provenance()
        for asset in provenance["assets"]:
            output_file = str(asset.get("output_file") or "")
            if output_file:
                path = run_dir / output_file
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"source-image")

        paper_memory = {
            "kind": "paper_memory",
            "metadata": {"title": "Synthetic Paper", "authors": ["A. Researcher"]},
            "chunks": [{"section": "method", "text": "Grounded paper evidence."}],
        }
        dossier = {
            "kind": "paper_memory_dossier",
            "summary": "A source-grounded synthetic paper dossier.",
        }
        artifacts: dict[str, dict] = {
            "paper_memory.json": paper_memory,
            "paper_visual_provenance.json": provenance,
        }
        if include_dossier:
            artifacts["paper_memory_dossier.json"] = dossier
        for name, payload in artifacts.items():
            (run_dir / name).write_text(json.dumps(payload), encoding="utf-8")
        (run_dir / "paper_memory.md").write_text("# Paper memory", encoding="utf-8")
        if include_dossier:
            (run_dir / "paper_memory_dossier.md").write_text("# Dossier", encoding="utf-8")
        evidence_dir = run_dir / "paper_evidence_packs"
        evidence_dir.mkdir()
        (evidence_dir / "method.md").write_text("# Method evidence", encoding="utf-8")
        registry = SkillRegistry.load(Path(__file__).resolve().parents[1] / "skills")
        bundle = registry.select(
            brief="Create a paper project landing page.",
            attachments=[],
            artifact_hint="landing",
        )
        _write_runtime_skill_snapshot(
            run_dir,
            skill_bundle=bundle,
            skill_contexts=bundle.render_all(),
        )

        settings = SimpleNamespace(
            designer_author_cmd=f"{sys.executable} {fake_author}",
            designer_author_harness="custom",
            designer_author_model="fake-model",
            designer_author_timeout_s=10,
            designer_author_max_attempts=max_attempts,
            harness_api_key=None,
        )
        ctx = ToolContext(
            settings=settings,
            run_dir=run_dir,
            layers_dir=layers_dir,
            run_id=run_dir.name,
        )
        ctx.state.update(
            {
                "artifact_type": "landing",
                "attachments": [],
                "paper_memory": paper_memory,
                "paper_visual_provenance": provenance,
                "poster_plan_contract": {"selected_visual_ids": ["rejected_asset"]},
                "paper_visual_storyboard": {"selected_asset_ids": ["rejected_asset"]},
            }
        )
        if include_dossier:
            ctx.state["paper_memory_dossier"] = dossier
        return ctx

    def _write_validation_candidate(
        self,
        *,
        style: str = "",
        extra_body: str = "",
        script: str = "",
        source_asset: bool = False,
    ) -> Path:
        attempt = self.root / f"validation_{len(list(self.root.glob('validation_*')))}"
        attempt.mkdir()
        words = " ".join(["grounded"] * 100)
        assets = []
        source_image = ""
        if source_asset:
            layers = attempt / "layers"
            layers.mkdir()
            (layers / "asset_01.png").write_bytes(b"source-image")
            assets.append({
                "asset_id": "asset_01",
                "output_file": "layers/asset_01.png",
                "output_sha256": "sha-01",
                "visual_selection_tier": "eligible",
            })
            source_image = (
                '<img src="layers/asset_01.png" data-source-id="asset_01" alt="Source figure">'
            )
        (attempt / "landing_asset_catalog.json").write_text(
            json.dumps({"assets": assets}),
            encoding="utf-8",
        )
        (attempt / "landing_visual_plan.json").write_text(
            json.dumps({
                "optional_reserve_assets": [],
                "validation_targets": {"required_unique_source_visuals": 0},
            }),
            encoding="utf-8",
        )
        (attempt / "designer_author_done.json").write_text("{}", encoding="utf-8")
        (attempt / "index.html").write_text(
            f"""<!doctype html><html><head>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>{style}</style></head><body><main>
            <section id="hero"><h1>Synthetic Paper</h1><p>{words}</p>{source_image}{extra_body}</section>
            <section id="method"><h2>Method</h2><p>{words}</p></section>
            <section id="results" class="result"><h2>Results</h2><p>{words}</p></section>
            </main><script>{script}</script></body></html>""",
            encoding="utf-8",
        )
        return attempt

    def _provenance(self, *, count: int = 18) -> dict:
        assets = [self._asset(index) for index in range(1, count + 1)]
        assets.append(
            {
                "asset_id": "rejected_asset",
                "kind": "image",
                "output_file": "layers/rejected.png",
                "caption_short": "Contaminated visual",
                "caption_association_method": "captioned_group",
                "caption_confidence": 0.9,
                "captioned_source_group": True,
                "designer_eligible": True,
                "curation_flags": ["body_text_leak"],
            }
        )
        return {
            "kind": "paper_visual_provenance",
            "version": 1,
            "assets": assets,
        }

    def _asset(self, index: int, **overrides) -> dict:
        role = _ROLES[(index - 1) % len(_ROLES)]
        asset = {
            "asset_id": f"asset_{index:02d}",
            "kind": "table" if role == "data" else "image",
            "output_file": f"layers/asset_{index:02d}.png",
            "output_sha256": "sha-17" if index == 18 else f"sha-{index:02d}",
            "output_width_px": 1200,
            "output_height_px": 700,
            "caption_short": f"{role.title()} evidence {index}",
            "caption_association_method": "captioned_group",
            "caption_confidence": 0.9,
            "captioned_source_group": True,
            "visual_role": role,
            "visual_score": 100 - index,
            "designer_eligible": True,
        }
        asset.update(overrides)
        return asset

    def _valid_fake_author(self) -> Path:
        return self._write_fake_author(
            """
            catalog = json.load(open('landing_asset_catalog.json', encoding='utf-8'))
            sources = catalog['assets'][:min(8, len(catalog['assets']))]
            images = ''.join(
                f'<img src="{source["output_file"]}" data-source-id="{source["asset_id"]}">'
                for source in sources
            )
            words = ' '.join(['grounded'] * 100)
            interaction = f'<button data-source-id="{sources[0]["asset_id"]}">Inspect evidence</button>'
            html = f'''<!doctype html><html><head>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>body{{margin:0}} img{{max-width:100%;height:auto}}
            @media (max-width:700px){{section{{display:block}}}}</style></head><body><main>
            <section id="hero" data-section-role="hero"><h1>Synthetic Paper</h1><p>{words}</p>
            {images}{interaction}</section>
            <section id="method" data-section-role="method"><h2>Method</h2><p>{words}</p></section>
            <section id="results" data-section-role="results"><h2>Results</h2><p>{words}</p></section>
            <section id="analysis" data-section-role="analysis"><h2>Analysis</h2><p>{words}</p></section>
            </main><script>document.documentElement.classList.add('interactive')</script></body></html>'''
            open('index.html', 'w', encoding='utf-8').write(html)
            json.dump({'status': 'complete'}, open('designer_author_done.json', 'w', encoding='utf-8'))
            """
        )

    def _sparse_fake_author(self) -> Path:
        return self._write_fake_author(
            """
            catalog = json.load(open('landing_asset_catalog.json', encoding='utf-8'))
            source = catalog['assets'][0]
            words = ' '.join(['grounded'] * 100)
            html = f'''<!doctype html><html><head>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>@media (max-width:700px){{section{{display:block}}}}</style></head><body><main>
            <section id="hero"><h1>Synthetic Paper</h1><p>{words}</p>
            <img src="{source['output_file']}" data-source-id="{source['asset_id']}"></section>
            <section id="method"><h2>Method</h2><p>{words}</p></section>
            <section id="results"><h2>Results</h2><p>{words}</p></section>
            </main></body></html>'''
            open('index.html', 'w', encoding='utf-8').write(html)
            json.dump({'status': 'complete'}, open('designer_author_done.json', 'w', encoding='utf-8'))
            """
        )

    def _repairing_fake_author(self) -> Path:
        return self._write_fake_author(
            """
            catalog = json.load(open('landing_asset_catalog.json', encoding='utf-8'))
            repair = pathlib.Path.cwd().name == 'attempt_02'
            sources = catalog['assets'][:8] if repair else catalog['assets'][:1]
            images = ''.join(
                f'<img src="{source["output_file"]}" data-source-id="{source["asset_id"]}">'
                for source in sources
            )
            words = ' '.join(['grounded'] * 100)
            interaction = f'<button data-source-id="{sources[0]["asset_id"]}">Inspect evidence</button>'
            html = f'''<!doctype html><html><head>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>@media (max-width:700px){{section{{display:block}}}}</style></head><body><main>
            <section id="hero"><h1>Synthetic Paper</h1><p>{words}</p>{images}{interaction}</section>
            <section id="method"><h2>Method</h2><p>{words}</p></section>
            <section id="results"><h2>Results</h2><p>{words}</p></section>
            </main></body></html>'''
            open('index.html', 'w', encoding='utf-8').write(html)
            json.dump({'status': 'complete'}, open('designer_author_done.json', 'w', encoding='utf-8'))
            """,
            imports="import json, pathlib, sys\n",
        )

    def _no_output_then_valid_author(self) -> Path:
        return self._write_fake_author(
            """
            if pathlib.Path.cwd().name == 'attempt_01':
                raise SystemExit(0)
            catalog = json.load(open('landing_asset_catalog.json', encoding='utf-8'))
            sources = catalog['assets'][:8]
            images = ''.join(
                f'<img src="{source["output_file"]}" data-source-id="{source["asset_id"]}">'
                for source in sources
            )
            words = ' '.join(['grounded'] * 100)
            interaction = f'<button data-source-id="{sources[0]["asset_id"]}">Inspect evidence</button>'
            html = f'''<!doctype html><html><head>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>@media (max-width:700px){{section{{display:block}}}}</style></head><body><main>
            <section id="hero"><h1>Synthetic Paper</h1><p>{words}</p>{images}{interaction}</section>
            <section id="method"><h2>Method</h2><p>{words}</p></section>
            <section id="results"><h2>Results</h2><p>{words}</p></section>
            </main></body></html>'''
            open('index.html', 'w', encoding='utf-8').write(html)
            json.dump({'status': 'complete'}, open('designer_author_done.json', 'w', encoding='utf-8'))
            """,
            imports="import json, pathlib, sys\n",
        )

    def _mismatched_source_id_author(self) -> Path:
        return self._write_fake_author(
            """
            catalog = json.load(open('landing_asset_catalog.json', encoding='utf-8'))
            sources = catalog['assets'][:8]
            shifted = sources[1:] + sources[:1]
            images = ''.join(
                f'<img src="{source["output_file"]}" data-source-id="{other["asset_id"]}">'
                for source, other in zip(sources, shifted)
            )
            words = ' '.join(['grounded'] * 100)
            html = f'''<!doctype html><html><head>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>@media (max-width:700px){{section{{display:block}}}}</style></head><body><main>
            <section id="hero"><h1>Synthetic Paper</h1><p>{words}</p>{images}</section>
            <section id="method"><h2>Method</h2><p>{words}</p></section>
            <section id="results"><h2>Results</h2><p>{words}</p></section>
            </main></body></html>'''
            open('index.html', 'w', encoding='utf-8').write(html)
            json.dump({'status': 'complete'}, open('designer_author_done.json', 'w', encoding='utf-8'))
            """
        )

    def _hidden_visual_fake_author(self) -> Path:
        return self._write_fake_author(
            """
            catalog = json.load(open('landing_asset_catalog.json', encoding='utf-8'))
            sources = catalog['assets'][:8]
            images = ''.join(
                f'<img style="opacity:0;width:0" src="{source["output_file"]}" data-source-id="{source["asset_id"]}">'
                for source in sources
            )
            words = ' '.join(['grounded'] * 100)
            html = f'''<!doctype html><html><head>
            <meta name="viewport" content="width=device-width, initial-scale=1"></head><body><main>
            <section id="hero"><h1>Synthetic Paper</h1><p>{words}</p>{images}</section>
            <section id="method"><h2>Method</h2><p>{words}</p></section>
            <section id="results"><h2>Results</h2><p>{words}</p></section>
            </main></body></html>'''
            open('index.html', 'w', encoding='utf-8').write(html)
            json.dump({'status': 'complete'}, open('designer_author_done.json', 'w', encoding='utf-8'))
            """
        )

    def _unsafe_fake_author(self) -> Path:
        return self._write_fake_author(
            """
            catalog = json.load(open('landing_asset_catalog.json', encoding='utf-8'))
            source = catalog['assets'][0]
            words = ' '.join(['grounded'] * 100)
            html = f'''<!doctype html><html><head>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <link rel="stylesheet" href="https://example.com/site.css"></head><body><main>
            <section id="hero"><h1>Synthetic Paper</h1><p>{words}</p>
            <img src="{source['output_file']}" data-source-id="{source['asset_id']}"></section>
            <section id="method"><h2>Method</h2><p>{words}</p></section>
            <section id="results"><h2>Results</h2><p>{words}</p></section>
            </main></body></html>'''
            open('index.html', 'w', encoding='utf-8').write(html)
            json.dump({'status': 'complete'}, open('designer_author_done.json', 'w', encoding='utf-8'))
            """
        )

    def _invalid_done_fake_author(self) -> Path:
        body = self._valid_fake_author().read_text(encoding="utf-8")
        body = body.replace(
            "json.dump({'status': 'complete'}, open('designer_author_done.json', 'w', encoding='utf-8'))",
            "open('designer_author_done.json', 'w', encoding='utf-8').write('{invalid')",
        )
        path = self.root / "fake_author_invalid_done.py"
        path.write_text(body, encoding="utf-8")
        return path

    def _write_fake_author(
        self,
        body: str,
        *,
        imports: str = "import json, sys\n",
    ) -> Path:
        path = self.root / f"fake_author_{len(list(self.root.glob('fake_author_*.py')))}.py"
        script = imports
        script += "open('prompt_seen.txt', 'w', encoding='utf-8').write(sys.stdin.read())\n"
        script += textwrap.dedent(body)
        path.write_text(script, encoding="utf-8")
        return path

    @staticmethod
    def _fake_screenshot(_html_path, out_path, **_kwargs):
        out_path.write_bytes(b"preview")
        return SimpleNamespace(backend="test-renderer", warnings=[])


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
