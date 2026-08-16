from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "agent_skills" / "autodesign-poster"
HARNESS_PATH = SKILL_ROOT / "scripts" / "poster_harness.py"


def _load_harness():
    if not HARNESS_PATH.is_file():
        raise AssertionError("standalone poster harness is missing")
    module_name = "autodesign_portable_poster_harness_test"
    spec = importlib.util.spec_from_file_location(module_name, HARNESS_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("poster harness could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _plan(*, preset: str = "cvpr-landscape", max_attempts: int = 4) -> dict[str, object]:
    if preset == "a0-landscape":
        canvas = {"width_px": 3366, "height_px": 2378}
        print_size = {"width_mm": 1189.0, "height_mm": 841.0}
    else:
        canvas = {"width_px": 3072, "height_px": 1536}
        print_size = {"width_mm": 2133.6, "height_mm": 1066.8}
    return {
        "format_version": 1,
        "artifact_type": "poster",
        "preset": preset,
        "canvas": canvas,
        "print": print_size,
        "narrative": [
            {"role": "problem", "purpose": "Frame the research problem."},
            {"role": "method", "purpose": "Explain the method."},
            {"role": "evidence", "purpose": "Show measured evidence."},
            {"role": "takeaway", "purpose": "State the bounded conclusion."},
        ],
        "visual_allocations": [],
        "max_attempts": max_attempts,
    }


def _claims() -> list[dict[str, object]]:
    return [
        {
            "id": "c-problem",
            "text": "The grounded poster source reports 85% accuracy.",
            "source_ids": ["ev-001"],
        },
        {
            "id": "c-method",
            "text": "The source uses two-stage routing.",
            "source_ids": ["ev-001"],
        },
        {
            "id": "c-evidence",
            "text": "Accuracy reaches 85%.",
            "source_ids": ["ev-001"],
        },
        {
            "id": "c-takeaway",
            "text": "The grounded poster retains accuracy.",
            "source_ids": ["ev-001"],
        },
    ]


def _poster_html(*, image: bool = False) -> str:
    visual = (
        '<img src="assets/vis-001.png" data-source-id="vis-001" '
        'alt="Source method diagram">'
        if image
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
@page {{ size: 2133.6mm 1066.8mm; margin: 0; }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; width: 3072px; height: 1536px; }}
body {{ font-family: Arial, Helvetica, sans-serif; color: #18202a; background: #fff; }}
.paper-poster {{ width: 3072px; height: 1536px; padding: 48px; overflow: hidden; }}
[data-role="identity-header"] {{ height: 250px; border-top: 18px solid #174a7e; }}
[data-identity="title"] {{ margin: 18px 0 4px; font-size: 56px; line-height: 1.04; }}
[data-identity="authors"], [data-identity="institutions"] {{ margin: 4px 0; font-size: 28px; }}
.body {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 24px; height: 1190px; }}
section {{ min-width: 0; padding: 24px; border-top: 6px solid #174a7e; }}
h2 {{ margin: 0 0 18px; font-size: 36px; line-height: 1.05; }}
p, li, th, td {{ font-size: 24px; line-height: 1.22; }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ padding: 8px; text-align: left; border-bottom: 2px solid #ccd4dc; }}
img {{ display: block; width: 100%; max-height: 390px; object-fit: contain; }}
@media print {{
  html, body {{ width: 2133.6mm; height: 1066.8mm; }}
  .paper-poster {{ width: 2133.6mm; height: 1066.8mm; }}
}}
</style>
</head>
<body>
<main class="paper-poster" data-autodesign-artifact="poster"
      data-canvas-width="3072" data-canvas-height="1536"
      data-print-width-mm="2133.6" data-print-height-mm="1066.8">
  <header data-role="identity-header">
    <h1 data-identity="title" data-source-ids="ev-001">Grounded Poster Study</h1>
    <p data-identity="authors" data-source-ids="ev-001">A. Researcher · B. Scientist</p>
    <p data-identity="institutions" data-source-ids="ev-001">Example University</p>
  </header>
  <div class="body">
    <section data-section-role="problem" data-claim-id="c-problem" data-source-ids="ev-001">
      <h2>Problem</h2>
      <p>The grounded poster source reports 85% accuracy.</p>
      <p>Research posters must preserve the paper's actual question and scope.</p>
    </section>
    <section data-section-role="method" data-claim-id="c-method" data-source-ids="ev-001">
      <h2>Method</h2>
      <p>The source uses two-stage routing.</p>
      {visual}
      <ul><li>Stage one selects evidence.</li><li>Stage two composes the result.</li></ul>
    </section>
    <section data-section-role="evidence" data-claim-id="c-evidence" data-source-ids="ev-001">
      <h2>Evidence</h2>
      <p>Accuracy reaches 85%.</p>
      <table><thead><tr><th>Measure</th><th>Value</th></tr></thead>
      <tbody><tr><td>Accuracy</td><td>85%</td></tr></tbody></table>
    </section>
    <section data-section-role="takeaway" data-claim-id="c-takeaway" data-source-ids="ev-001">
      <h2>Takeaway</h2>
      <p>The grounded poster retains accuracy.</p>
      <p>Keep the conclusion bounded by the supplied evidence.</p>
    </section>
  </div>
</main>
</body>
</html>
"""


class AutoDesignPosterSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_fixture(self, *, image: bool = False) -> tuple[Path, Path]:
        artifact = self.root / "artifact"
        artifact.mkdir()
        html = artifact / "poster.html"
        html.write_text(_poster_html(image=image), encoding="utf-8")
        if image:
            (artifact / "assets").mkdir()
            (artifact / "assets" / "vis-001.png").write_bytes(b"fixture-image")
        return artifact, html

    def test_cli_help_exposes_complete_portable_lifecycle(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(HARNESS_PATH), "--help"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        for command in (
            "doctor", "init", "evidence", "bind-visuals", "plan",
            "begin-attempt", "validate", "review-context", "record-review",
            "finalize", "resume",
        ):
            self.assertIn(command, completed.stdout)

    def test_plan_defaults_to_fixed_cvpr_landscape_contract(self) -> None:
        harness = _load_harness()
        normalized = harness.normalize_plan(
            {
                "format_version": 1,
                "artifact_type": "poster",
                "narrative": _plan()["narrative"],
                "visual_allocations": [],
            }
        )
        self.assertEqual(normalized["preset"], "cvpr-landscape")
        self.assertEqual(normalized["canvas"], {"width_px": 3072, "height_px": 1536})
        self.assertEqual(normalized["print"], {"width_mm": 2133.6, "height_mm": 1066.8})
        self.assertEqual(normalized["max_attempts"], 4)

    def test_plan_honors_supported_user_size_and_rejects_ratio_mismatch(self) -> None:
        harness = _load_harness()
        normalized = harness.normalize_plan(_plan(preset="a0-landscape"))
        self.assertEqual(normalized["canvas"], {"width_px": 3366, "height_px": 2378})
        self.assertEqual(normalized["print"], {"width_mm": 1189.0, "height_mm": 841.0})

        broken = _plan()
        broken["preset"] = "custom"
        broken["print"] = {"width_mm": 841.0, "height_mm": 1189.0}
        with self.assertRaisesRegex(harness.PosterContractError, "aspect ratio"):
            harness.normalize_plan(broken)

    def test_static_lint_accepts_editable_grounded_poster_contract(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture(image=True)
        result = harness.lint_poster_html(
            html,
            artifact_root=artifact,
            plan=_plan(),
            claims=_claims(),
        )
        self.assertTrue(result["passed"], result)
        self.assertEqual(
            {check["id"] for check in result["checks"]},
            {
                "document_contract", "fixed_canvas", "print_page_size",
                "identity_header", "native_editability", "source_bindings",
                "local_assets", "narrative_arc", "typography_contract",
            },
        )

    def test_static_lint_accepts_grounded_identity_ids_outside_claim_map(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        html.write_text(
            html.read_text(encoding="utf-8").replace(
                'data-source-ids="ev-001">Grounded Poster Study',
                'data-source-ids="ev-identity">Grounded Poster Study',
            ).replace(
                'data-source-ids="ev-001">A. Researcher',
                'data-source-ids="ev-identity">A. Researcher',
            ).replace(
                'data-source-ids="ev-001">Example University',
                'data-source-ids="ev-identity">Example University',
            ),
            encoding="utf-8",
        )
        result = harness.lint_poster_html(
            html,
            artifact_root=artifact,
            plan=_plan(),
            claims=_claims(),
            evidence_ids=["ev-identity"],
        )
        binding = next(check for check in result["checks"] if check["id"] == "source_bindings")
        self.assertTrue(binding["passed"], binding)

    def test_static_lint_rejects_non_identity_header_content_and_logo(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        text = html.read_text(encoding="utf-8").replace(
            '<p data-identity="institutions" data-source-ids="ev-001">Example University</p>',
            '<p data-identity="institutions" data-source-ids="ev-001">Example University</p>'
            '<p data-identity="venue">CVPR 2026</p><img src="logo.png" alt="logo">',
        )
        html.write_text(text, encoding="utf-8")
        result = harness.lint_poster_html(html, artifact_root=artifact, plan=_plan(), claims=_claims())
        finding = next(check for check in result["checks"] if check["id"] == "identity_header")
        self.assertFalse(finding["passed"])
        self.assertIn("exactly title, authors, and institutions", finding["detail"])

    def test_static_lint_rejects_remote_traversal_data_and_missing_assets(self) -> None:
        harness = _load_harness()
        for source in (
            "https://example.com/figure.png",
            "data:image/png;base64,AAAA",
            "../outside.png",
            "assets/missing.png",
        ):
            with self.subTest(source=source):
                artifact, html = self._write_fixture()
                html.write_text(
                    html.read_text(encoding="utf-8").replace(
                        "<h2>Method</h2>",
                        f'<h2>Method</h2><img src="{source}" data-source-id="vis-001" alt="method">',
                    ),
                    encoding="utf-8",
                )
                result = harness.lint_poster_html(
                    html, artifact_root=artifact, plan=_plan(), claims=_claims()
                )
                local = next(check for check in result["checks"] if check["id"] == "local_assets")
                self.assertFalse(local["passed"])
                for child in sorted(artifact.iterdir(), reverse=True):
                    if child.is_dir():
                        for nested in child.rglob("*"):
                            if nested.is_file():
                                nested.unlink()
                        child.rmdir()
                    else:
                        child.unlink()
                artifact.rmdir()

    def test_static_lint_rejects_remote_inline_style_asset(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        html.write_text(
            html.read_text(encoding="utf-8").replace(
                '<section data-section-role="method"',
                '<section style="background-image:url(https://example.com/paper.png)" '
                'data-section-role="method"',
            ),
            encoding="utf-8",
        )
        result = harness.lint_poster_html(
            html, artifact_root=artifact, plan=_plan(), claims=_claims()
        )
        local = next(check for check in result["checks"] if check["id"] == "local_assets")
        self.assertFalse(local["passed"])

    def test_static_lint_rejects_unreferenced_artifact_files(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        (artifact / "unused-reference.png").write_bytes(b"must not ship")
        result = harness.lint_poster_html(
            html, artifact_root=artifact, plan=_plan(), claims=_claims()
        )
        local = next(check for check in result["checks"] if check["id"] == "local_assets")
        self.assertFalse(local["passed"])
        self.assertIn("unreferenced", local["detail"])

    def test_plan_rejects_content_assets_the_poster_cannot_render(self) -> None:
        harness = _load_harness()
        run = self.root / "unsupported-asset-run"
        source = self.root / "paper.txt"
        source.write_text("Grounded poster source reports 85% accuracy.", encoding="utf-8")
        unsupported = self.root / "raw-data.csv"
        unsupported.write_text("measure,value\naccuracy,85\n", encoding="utf-8")
        harness.initialize_poster_run(run, source, extra_assets=[unsupported])
        plan = _plan()
        plan["visual_allocations"] = [{"visual_id": "vis-001", "role": "evidence"}]
        with self.assertRaisesRegex(harness.PosterContractError, "unsupported poster visual"):
            harness.save_poster_plan(run, plan)

    def test_static_lint_rejects_raster_only_or_non_native_table_content(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        html.write_text(
            """<!doctype html><html><head><style>@page{size:2133.6mm 1066.8mm;margin:0}</style></head>
            <body><main class="paper-poster" data-autodesign-artifact="poster"
            data-canvas-width="3072" data-canvas-height="1536"
            data-print-width-mm="2133.6" data-print-height-mm="1066.8">
            <img src="poster.png" alt="flattened poster"></main></body></html>""",
            encoding="utf-8",
        )
        (artifact / "poster.png").write_bytes(b"flattened")
        result = harness.lint_poster_html(html, artifact_root=artifact, plan=_plan(), claims=_claims())
        native = next(check for check in result["checks"] if check["id"] == "native_editability")
        self.assertFalse(native["passed"])
        self.assertIn("native", native["detail"].lower())

    def test_static_lint_rejects_wrong_canvas_print_size_and_typography(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        text = html.read_text(encoding="utf-8")
        text = text.replace('data-canvas-width="3072"', 'data-canvas-width="1920"')
        text = text.replace("size: 2133.6mm 1066.8mm", "size: 297mm 210mm")
        text = text.replace("font-size: 24px", "font-size: 10px")
        html.write_text(text, encoding="utf-8")
        result = harness.lint_poster_html(html, artifact_root=artifact, plan=_plan(), claims=_claims())
        failed = {check["id"] for check in result["checks"] if not check["passed"]}
        self.assertTrue({"fixed_canvas", "print_page_size", "typography_contract"}.issubset(failed))

    def test_static_lint_rejects_missing_or_mismatched_claim_bindings(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        text = html.read_text(encoding="utf-8")
        text = text.replace('data-claim-id="c-method"', 'data-claim-id="unknown-claim"')
        text = text.replace("The grounded poster source reports 85% accuracy.", "An unsupported claim.", 1)
        html.write_text(text, encoding="utf-8")
        result = harness.lint_poster_html(html, artifact_root=artifact, plan=_plan(), claims=_claims())
        binding = next(check for check in result["checks"] if check["id"] == "source_bindings")
        self.assertFalse(binding["passed"])
        self.assertIn("unknown-claim", binding["detail"])

    def test_static_lint_rejects_scripts_handlers_and_incomplete_research_arc(self) -> None:
        harness = _load_harness()
        artifact, html = self._write_fixture()
        text = html.read_text(encoding="utf-8")
        text = text.replace("<body>", '<body onload="fetch(\'https://example.com\')"><script>alert(1)</script>')
        text = text.replace('data-section-role="takeaway"', 'data-section-role="evidence"')
        html.write_text(text, encoding="utf-8")
        result = harness.lint_poster_html(html, artifact_root=artifact, plan=_plan(), claims=_claims())
        failed = {check["id"] for check in result["checks"] if not check["passed"]}
        self.assertTrue({"document_contract", "narrative_arc"}.issubset(failed))

    def test_pdfinfo_gate_requires_one_page_and_exact_physical_size(self) -> None:
        harness = _load_harness()
        passed = harness.parse_pdfinfo(
            "Pages:          1\nPage size:      6048 x 3024 pts (custom)\n",
            expected_width_mm=2133.6,
            expected_height_mm=1066.8,
        )
        self.assertTrue(passed["passed"], passed)

        two_pages = harness.parse_pdfinfo(
            "Pages: 2\nPage size: 6048 x 3024 pts\n",
            expected_width_mm=2133.6,
            expected_height_mm=1066.8,
        )
        wrong_size = harness.parse_pdfinfo(
            "Pages: 1\nPage size: 792 x 612 pts (letter)\n",
            expected_width_mm=2133.6,
            expected_height_mm=1066.8,
        )
        self.assertFalse(two_pages["passed"])
        self.assertFalse(wrong_size["passed"])

    def test_begin_attempt_stages_only_allocated_eligible_content_visuals(self) -> None:
        harness = _load_harness()
        run = self.root / "run"
        source = self.root / "paper.txt"
        source.write_text(
            "Grounded poster source reports 85% accuracy and uses two-stage routing.",
            encoding="utf-8",
        )
        content_asset = self.root / "method.png"
        content_asset.write_bytes(b"content")
        style_reference = self.root / "reference.png"
        style_reference.write_bytes(b"style-only")
        harness.initialize_poster_run(
            run,
            source,
            extra_assets=[content_asset],
            reference_images=[style_reference],
        )
        plan = _plan()
        plan["visual_allocations"] = [{"visual_id": "vis-001", "role": "method"}]
        harness.save_poster_plan(run, plan)
        attempt = harness.begin_poster_attempt(run)
        staged = run / "attempts" / attempt["attempt_id"] / "artifact" / "assets"
        self.assertEqual([path.name for path in staged.iterdir()], ["vis-001.png"])
        self.assertEqual((staged / "vis-001.png").read_bytes(), b"content")
        self.assertNotIn(b"style-only", (staged / "vis-001.png").read_bytes())

    def test_bounded_repair_stops_after_plan_attempt_budget(self) -> None:
        harness = _load_harness()
        run = self.root / "bounded-run"
        source = self.root / "paper.txt"
        source.write_text("Grounded poster source reports 85% accuracy.", encoding="utf-8")
        harness.initialize_poster_run(run, source)
        harness.save_poster_plan(run, _plan(max_attempts=2))
        first = harness.begin_poster_attempt(run)["attempt_id"]
        harness.core.mark_side_state(run, "failed", reason="first repair")
        second = harness.begin_poster_attempt(run)["attempt_id"]
        self.assertEqual((first, second), ("01", "02"))
        harness.core.mark_side_state(run, "failed", reason="second repair")
        with self.assertRaisesRegex(harness.PosterContractError, "attempt budget"):
            harness.begin_poster_attempt(run)

    def test_full_lifecycle_hash_binds_preview_pdf_review_and_final_delivery(self) -> None:
        harness = _load_harness()
        run = self.root / "lifecycle-run"
        source = self.root / "paper.txt"
        source.write_text(
            "Grounded poster source reports 85% accuracy and uses two-stage routing. "
            "The grounded poster retains accuracy.",
            encoding="utf-8",
        )
        harness.initialize_poster_run(run, source)
        harness.save_poster_plan(run, _plan())
        attempt_info = harness.begin_poster_attempt(run)
        attempt_id = attempt_info["attempt_id"]
        poster_path = Path(attempt_info["poster_path"])
        poster_path.write_text(_poster_html(), encoding="utf-8")
        source_map = self.root / "source-map-input.json"
        source_map.write_text(json.dumps({"claims": _claims()}), encoding="utf-8")

        def fake_render(*, attempt_root: Path, **_kwargs: object) -> dict[str, object]:
            preview = attempt_root / "qa" / "previews" / "poster.png"
            preview.write_bytes(b"png-preview")
            (attempt_root / "artifact" / "preview.png").write_bytes(b"png-preview")
            (attempt_root / "artifact" / "poster.pdf").write_bytes(b"%PDF-fixture")
            return {
                "passed": True,
                "checks": [
                    {"id": "browser_geometry", "passed": True, "detail": "passed"},
                    {"id": "single_page_pdf", "passed": True, "detail": "passed"},
                ],
                "preview_path": "qa/previews/poster.png",
            }

        with mock.patch.object(harness, "_render_poster_outputs", side_effect=fake_render):
            deterministic = harness.validate_poster_attempt(
                run,
                attempt_id,
                source_map_path=source_map,
                allow_browser_install=False,
            )
        self.assertTrue(deterministic["passed"], deterministic)
        context = harness.create_poster_review_context(run, attempt_id)
        review = {
            "format_version": 1,
            "attempt_id": attempt_id,
            "review_context_sha256": context["context_sha256"],
            "artifact_hashes": context["artifact_hashes"],
            "preview_hashes": context["preview_hashes"],
            "reviewed_frame_ids": sorted(context["preview_hashes"]),
            "source_manifest_sha256": context["source_manifest_sha256"],
            "source_map_sha256": context["source_map_sha256"],
            "rubric_sha256": context["rubric_sha256"],
            "reviewer_mode": "fresh_host_vlm",
            "dimension_scores": {name: 4 for name in harness.REVIEW_RUBRIC["dimensions"]},
            "blockers": [],
            "localized_repairs": [],
            "verdict": "pass",
            "complete": True,
        }
        review_path = self.root / "review.json"
        review_path.write_text(json.dumps(review), encoding="utf-8")
        harness.record_poster_review(run, attempt_id, review_path)
        manifest = harness.finalize_poster_attempt(run, attempt_id)
        self.assertEqual(manifest["verification_status"], "verified")
        self.assertEqual(
            set(manifest["files"]),
            {"poster.html", "poster.pdf", "preview.png", "provenance/source-map.json"},
        )
        self.assertEqual(
            harness.resume_poster_run(run)["next_action"],
            "complete",
        )

    @unittest.skipUnless(
        __import__("os").environ.get("AUTODESIGN_SKILL_REAL_BROWSER") == "1",
        "set AUTODESIGN_SKILL_REAL_BROWSER=1 for pinned Chromium/PDF integration",
    )
    def test_real_browser_renders_preview_and_exact_one_page_pdf(self) -> None:
        harness = _load_harness()
        run = self.root / "real-browser-run"
        source = self.root / "paper.txt"
        source.write_text(
            "Grounded poster source reports 85% accuracy and uses two-stage routing. "
            "The grounded poster retains accuracy.",
            encoding="utf-8",
        )
        harness.initialize_poster_run(run, source)
        harness.save_poster_plan(run, _plan())
        attempt = harness.begin_poster_attempt(run)
        Path(attempt["poster_path"]).write_text(_poster_html(), encoding="utf-8")
        source_map = self.root / "real-source-map.json"
        source_map.write_text(json.dumps({"claims": _claims()}), encoding="utf-8")
        result = harness.validate_poster_attempt(
            run,
            attempt["attempt_id"],
            source_map_path=source_map,
            cache_root=Path(__import__("os").environ["AUTODESIGN_SKILL_BROWSER_CACHE"]),
            allow_browser_install=False,
        )
        self.assertTrue(result["passed"], result)
        attempt_root = run / "attempts" / attempt["attempt_id"]
        self.assertGreater((attempt_root / "artifact" / "poster.pdf").stat().st_size, 1000)
        self.assertGreater((attempt_root / "artifact" / "preview.png").stat().st_size, 1000)
        pdf_check = next(check for check in result["checks"] if check["id"] == "single_page_pdf")
        self.assertEqual(pdf_check["pages"], 1)


if __name__ == "__main__":
    unittest.main()
