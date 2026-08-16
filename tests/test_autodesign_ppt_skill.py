from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "agent_skills" / "autodesign-ppt"
HARNESS_PATH = SKILL_ROOT / "scripts" / "ppt_harness.py"
EXPORTER_PATH = SKILL_ROOT / "scripts" / "export_pptx.py"
SETUP_PATH = SKILL_ROOT / "scripts" / "setup_ppt.py"
SAFE_NAVIGATION_SCRIPT = "(()=>{const s=[...document.querySelectorAll('.deck-slide')];const i=()=>Math.max(0,s.findIndex(x=>'#'+x.id===location.hash));const g=n=>{const x=s[Math.min(s.length-1,Math.max(0,n))];if(x){location.hash=x.id;x.scrollIntoView({block:'start'})}};addEventListener('keydown',e=>{if(e.key==='ArrowLeft'){e.preventDefault();g(i()-1)}else if(e.key==='ArrowRight'){e.preventDefault();g(i()+1)}});addEventListener('hashchange',()=>{const x=s[i()];if(x)x.scrollIntoView({block:'start'})})})();"


def _load_script(name: str, path: Path):
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _png_bytes() -> bytes:
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFElEQVR4nGP4z8DA"
        "wMDAxMDAwAIAAwEBAAHiIw0AAAAASUVORK5CYII="
    )


def _deck_html(slide_count: int = 18, *, remote_asset: bool = False) -> str:
    slides: list[str] = []
    for index in range(1, slide_count + 1):
        slide_id = f"slide-{index:02d}"
        image_src = "https://example.com/figure.png" if remote_asset and index == 2 else "assets/pixel.png"
        content = [
            (
                f'<h1 data-pptx-kind="text" data-pptx-x="120" data-pptx-y="90" '
                f'data-pptx-w="1680" data-pptx-h="120" data-font-size="54" '
                f'data-claim-id="claim-{index:02d}" data-source-ids="ev-001">'
                "The paper presents a grounded research finding."
                "</h1>"
            ),
            (
                '<p data-pptx-kind="text" data-pptx-x="120" data-pptx-y="260" '
                'data-pptx-w="760" data-pptx-h="180" data-font-size="28" '
                'data-source-ids="ev-001">A concise source-backed explanation.</p>'
            ),
            (
                '<div data-pptx-kind="shape" data-shape="rect" data-pptx-x="120" '
                'data-pptx-y="500" data-pptx-w="760" data-pptx-h="12" '
                'data-fill="#6B3FA0"></div>'
            ),
        ]
        if index == 2:
            content.append(
                f'<img data-pptx-kind="image" data-pptx-x="1000" data-pptx-y="260" '
                f'data-pptx-w="700" data-pptx-h="500" data-source-ids="visual-explicit-001" '
                f'src="{image_src}" alt="Source figure">'
            )
        if index == 3:
            content.append(
                '<table data-pptx-kind="table" data-pptx-x="980" data-pptx-y="250" '
                'data-pptx-w="740" data-pptx-h="420" data-source-ids="ev-001">'
                '<tr><th>Method</th><th>Result</th></tr>'
                '<tr><td>AutoDesign</td><td>Grounded</td></tr>'
                '</table>'
            )
        slides.append(
            f'<section class="deck-slide" id="{slide_id}" data-slide-id="{slide_id}" '
            f'data-slide-index="{index}" data-slide-role="evidence" data-section="paper-talk" '
            f'data-assertion-title="The paper presents a grounded research finding." '
            f'data-source-ids="ev-001" data-speaker-notes="[Sources] ev-001 [Talk] Explain slide {index}." '
            'data-width="1920" data-height="1080" data-background="#F7F7F3">'
            + "".join(content)
            + "</section>"
        )
    return "".join(
        [
            "<!doctype html><html><head><meta charset=\"utf-8\"><style>",
            "html,body{margin:0;background:#efefec}.deck-slide{position:relative;width:1920px;height:1080px;overflow:hidden;background:#F7F7F3}",
            "@media print{.deck-slide{display:block!important;break-after:page;page-break-after:always}}",
            "</style></head><body>",
            f'<main id="deck" data-autodesign-artifact-root="deck" data-slide-count="{slide_count}" data-width="1920" data-height="1080">',
            *slides,
            "</main>",
            f"<script data-autodesign-navigation>{SAFE_NAVIGATION_SCRIPT}</script>",
            "</body></html>",
        ]
    )


class AutoDesignPptSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.harness = _load_script("portable_ppt_harness", HARNESS_PATH)
        self.exporter = _load_script("portable_ppt_exporter", EXPORTER_PATH)
        self.setup = _load_script("portable_ppt_setup", SETUP_PATH)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _require(self, module: object | None, path: Path) -> object:
        self.assertIsNotNone(module, f"missing standalone PPT implementation: {path}")
        return module

    def _write_fixture(self, slide_count: int = 18, *, remote_asset: bool = False) -> Path:
        artifact = self.root / "artifact"
        (artifact / "assets").mkdir(parents=True)
        (artifact / "assets" / "pixel.png").write_bytes(_png_bytes())
        html = artifact / "deck.html"
        html.write_text(_deck_html(slide_count, remote_asset=remote_asset), encoding="utf-8")
        return html

    def test_planner_defaults_paper_decks_to_exactly_18_slides(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        plan = harness.build_deck_plan("Create a conference deck from this paper.", ["ev-001"])
        self.assertEqual(plan["slide_count"], 18)
        self.assertEqual(plan["count_source"], "academic_default")
        self.assertEqual(len(plan["slides"]), 18)
        self.assertEqual([item["slide_id"] for item in plan["slides"]], [f"slide-{i:02d}" for i in range(1, 19)])

    def test_explicit_user_slide_count_overrides_the_18_slide_default(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        for brief, expected in (("Make 12 slides.", 12), ("请生成 22 页 PPT。", 22), ("Create a 15-page deck", 15)):
            with self.subTest(brief=brief):
                plan = harness.build_deck_plan(brief, ["ev-001"])
                self.assertEqual(plan["slide_count"], expected)
                self.assertEqual(plan["count_source"], "explicit_user")
                self.assertEqual(len(plan["slides"]), expected)

    def test_html_contract_accepts_exact_ids_assertions_sources_notes_canvas_and_navigation(self) -> None:
        exporter = self._require(self.exporter, EXPORTER_PATH)
        html = self._write_fixture()
        report = exporter.validate_deck_html(html, expected_slide_count=18)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["slide_ids"], [f"slide-{i:02d}" for i in range(1, 19)])
        self.assertEqual(report["assertion_title_count"], 18)
        self.assertEqual(report["speaker_note_count"], 18)
        self.assertEqual(report["canvas"], {"width": 1920, "height": 1080})
        self.assertTrue(report["keyboard_navigation"])

    def test_html_contract_rejects_remote_assets_and_wrong_slide_count(self) -> None:
        exporter = self._require(self.exporter, EXPORTER_PATH)
        html = self._write_fixture(17, remote_asset=True)
        report = exporter.validate_deck_html(html, expected_slide_count=18)
        self.assertFalse(report["passed"])
        codes = {item["code"] for item in report["issues"]}
        self.assertIn("slide_count_mismatch", codes)
        self.assertIn("remote_asset", codes)

    def test_html_contract_rejects_visible_text_that_would_be_rasterized_in_pptx(self) -> None:
        exporter = self._require(self.exporter, EXPORTER_PATH)
        html = self._write_fixture()
        text = html.read_text(encoding="utf-8")
        text = text.replace(
            "</section>",
            "<aside>This important result was never tagged as editable.</aside></section>",
            1,
        )
        html.write_text(text, encoding="utf-8")
        report = exporter.validate_deck_html(html, expected_slide_count=18)
        self.assertFalse(report["passed"])
        self.assertIn("untagged_visible_text", {item["code"] for item in report["issues"]})

    def test_html_contract_rejects_arbitrary_executable_script(self) -> None:
        exporter = self._require(self.exporter, EXPORTER_PATH)
        html = self._write_fixture()
        html.write_text(
            html.read_text(encoding="utf-8").replace(
                "</body>", "<script>fetch('https://example.com')</script></body>"
            ),
            encoding="utf-8",
        )
        report = exporter.validate_deck_html(html, expected_slide_count=18)
        self.assertFalse(report["passed"])
        self.assertIn("unsafe_script", {item["code"] for item in report["issues"]})

    def test_slide_audit_variants_isolate_every_slide_and_preserve_local_assets(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        html = self._write_fixture()
        (html.parent / "theme.css").write_text(".deck-slide{color:#171717}\n", encoding="utf-8")
        html.write_text(
            html.read_text(encoding="utf-8").replace(
                "</head>", '<link rel="stylesheet" href="theme.css"></head>'
            ),
            encoding="utf-8",
        )
        variant_dir = self.root / "variants"
        variants = harness.create_slide_audit_variants(html, variant_dir, 18)
        self.assertEqual(len(variants), 18)
        for index, path in enumerate(variants, start=1):
            text = path.read_text(encoding="utf-8")
            self.assertIn(f"#slide-{index:02d}", text)
            self.assertTrue((path.parent / "assets" / "pixel.png").is_file())
            self.assertTrue((path.parent / "theme.css").is_file())
            self.assertNotIn("http://", text)
            self.assertNotIn("https://", text)

    def test_pptx_export_reopens_with_editable_text_table_image_shape_and_notes(self) -> None:
        exporter = self._require(self.exporter, EXPORTER_PATH)
        html = self._write_fixture()
        output = self.root / "deck.pptx"
        report = exporter.export_deck_to_pptx(html, output)
        self.assertTrue(output.is_file())
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["slide_count"], 18)
        self.assertEqual(report["notes_count"], 18)
        self.assertGreaterEqual(report["editable_text_shapes"], 36)
        self.assertGreaterEqual(report["table_shapes"], 1)
        self.assertGreaterEqual(report["picture_shapes"], 1)
        self.assertGreaterEqual(report["editable_shape_count"], 18)
        self.assertEqual(report["slide_size_inches"], [13.333333, 7.5])
        with zipfile.ZipFile(output) as archive:
            self.assertIn("ppt/slides/slide18.xml", archive.namelist())
            self.assertIn("ppt/notesSlides/notesSlide18.xml", archive.namelist())
            slide_xml = archive.read("ppt/slides/slide3.xml")
            self.assertIn(b"<a:tbl>", slide_xml)
            self.assertIn(b"The paper presents a grounded research finding.", slide_xml)

    def test_pptx_validation_rejects_whole_slide_rasterization_without_editable_overlays(self) -> None:
        exporter = self._require(self.exporter, EXPORTER_PATH)
        html = self._write_fixture()
        text = html.read_text(encoding="utf-8")
        text = text.replace('data-pptx-kind="text"', 'data-ignored-kind="text"')
        text = text.replace(
            '<img data-pptx-kind="image" data-pptx-x="1000" data-pptx-y="260" data-pptx-w="700" data-pptx-h="500"',
            '<img data-pptx-kind="image" data-pptx-x="0" data-pptx-y="0" data-pptx-w="1920" data-pptx-h="1080"',
        )
        html.write_text(text, encoding="utf-8")
        report = exporter.validate_deck_html(html, expected_slide_count=18)
        self.assertFalse(report["passed"])
        self.assertIn("missing_editable_text", {item["code"] for item in report["issues"]})

    def test_contact_sheet_html_references_every_slide_preview(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        previews = []
        for index in range(1, 19):
            path = self.root / "previews" / f"slide-{index:02d}.png"
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(_png_bytes())
            previews.append(path)
        sheet = harness.write_contact_sheet_html(previews, self.root / "contact" / "index.html")
        text = sheet.read_text(encoding="utf-8")
        self.assertEqual(text.count("<img "), 18)
        self.assertIn("Slide 18", text)

    def test_interrupted_validation_discards_only_unverified_qa_scratch(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        attempt = self.root / "attempt"
        scratch = attempt / "qa" / "deck"
        scratch.mkdir(parents=True)
        (scratch / "partial.json").write_text("{}\n", encoding="utf-8")
        cleaned = harness.prepare_qa_directory(attempt)
        self.assertEqual(cleaned, scratch.resolve())
        self.assertTrue(cleaned.is_dir())
        self.assertEqual(list(cleaned.iterdir()), [])

        file_attempt = self.root / "file-attempt"
        file_scratch = file_attempt / "qa" / "deck"
        file_scratch.parent.mkdir(parents=True)
        file_scratch.write_text("interrupted\n", encoding="utf-8")
        cleaned_file = harness.prepare_qa_directory(file_attempt)
        self.assertTrue(cleaned_file.is_dir())
        self.assertEqual(list(cleaned_file.iterdir()), [])

    def test_setup_runtime_is_versioned_outside_the_installed_skill(self) -> None:
        setup = self._require(self.setup, SETUP_PATH)
        spec = setup.runtime_spec(cache_root=self.root / "cache")
        self.assertIn("python-pptx-1.0.2", spec.cache_key)
        self.assertTrue(str(spec.cache_dir).startswith(str(self.root / "cache")))
        self.assertFalse(str(spec.cache_dir).startswith(str(SKILL_ROOT)))
        self.assertEqual(setup.PINNED_PACKAGES["python-pptx"], "1.0.2")
        with self.assertRaisesRegex(setup.PptRuntimeError, "outside the installed Skill"):
            setup.runtime_spec(cache_root=SKILL_ROOT / "generated-cache")

    def test_passing_review_cannot_bypass_the_bound_minimum_scores(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        scores = {dimension: 4 for dimension in harness.REVIEW_RUBRIC["dimensions"]}
        scores["source_fidelity"] = 3
        review_path = self.root / "review.json"
        review_path.write_text(
            json.dumps({"verdict": "pass", "dimension_scores": scores}),
            encoding="utf-8",
        )
        args = SimpleNamespace(run_dir=self.root, attempt="01", review=review_path)
        with (
            mock.patch.object(harness, "_resume", return_value={}),
            mock.patch.object(harness.portable, "record_semantic_review") as record,
            self.assertRaisesRegex(harness.PptHarnessError, "at least 4"),
        ):
            harness._command_record_review(args)
        record.assert_not_called()

    @unittest.skipUnless(
        os.environ.get("AUTODESIGN_SKILL_REAL_PPT") == "1",
        "set AUTODESIGN_SKILL_REAL_PPT=1 for the full browser/PDF/PPTX smoke",
    )
    def test_real_validate_pipeline_produces_18_previews_pdf_contact_sheet_and_reopen_report(self) -> None:
        harness = self._require(self.harness, HARNESS_PATH)
        html = self._write_fixture()
        result = harness.render_and_validate_deck(
            html,
            expected_slide_count=18,
            qa_dir=self.root / "qa",
            browser_cache=Path(os.environ["AUTODESIGN_SKILL_BROWSER_CACHE"]),
            ppt_cache=self.root / "ppt-cache",
            offline_browser=True,
        )
        self.assertTrue(result["passed"], result)
        self.assertEqual(len(result["preview_paths"]), 19)
        self.assertEqual(result["pdf_page_count"], 18)
        self.assertTrue(Path(result["contact_sheet"]).is_file())
        self.assertTrue(Path(result["pptx_path"]).is_file())
        self.assertEqual(result["pptx_validation"]["slide_count"], 18)
        if shutil.which("soffice"):
            self.assertTrue(result["rendered_pptx_comparison"]["performed"])
            self.assertEqual(result["rendered_pptx_comparison"]["page_count"], 18)


if __name__ == "__main__":
    unittest.main()
