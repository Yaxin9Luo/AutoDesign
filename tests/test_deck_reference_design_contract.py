from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from autodesign.skills.registry import SkillRegistry
from autodesign.tools._contract import ToolContext
from autodesign.tools import deck_html_renderer


REPO_ROOT = Path(__file__).resolve().parents[1]


class DeckReferenceDesignContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = SkillRegistry.load(REPO_ROOT / "skills")

    def test_general_deck_skill_indexes_versioned_academic_taste_resource(self) -> None:
        bundle = self.registry.select(
            brief="Create a 12-slide academic paper presentation.",
            attachments=[Path("paper.pdf")],
            artifact_hint="deck",
        )
        pack = bundle.get("deck.html_ppt_general")

        self.assertIsNotNone(pack)
        assert pack is not None
        resource = pack.resource("academic_deck_taste_v1")
        self.assertIsNotNone(resource)
        self.assertEqual(resource.path, "references/academic_deck_taste_v1.json")
        self.assertEqual(resource.stages, ["plan", "repair"])

        rendered_plan = pack.render("plan")
        self.assertIn("academic_deck_taste_v1", rendered_plan)
        self.assertIn("Read `academic_deck_taste_v1`", rendered_plan)

        content = pack.read_resource("academic_deck_taste_v1", "plan")
        self.assertIsNotNone(content)
        assert content is not None
        lowered = content.lower()
        for required in (
            '"version": 1',
            '"default_slide_count": 18',
            "explicit_user_override",
            "html-only",
            "white",
            "off-white",
            "serif main hierarchy",
            "near-black",
            "one restrained accent",
            "invisible keyboard",
            "source figures",
            "source tables",
            "visual explanation",
            "black or dark default",
            "visible playback controls",
            "dashboard card grids",
            "text walls",
            "irrelevant sparse imagery",
            "generic framework demo",
        ):
            self.assertIn(required, lowered)
        self.assertNotIn(".png", lowered)
        self.assertNotIn("screenshot", lowered)

    def test_unresolved_image_is_identifiable_blocking_and_not_visual_area(self) -> None:
        theme = deck_html_renderer._theme_tokens(
            SimpleNamespace(palette=[], typography={}, visual_profile=None),
            {"theme": {"profile": "technical_light"}},
        )
        placement = deck_html_renderer._placement(
            "slide_01",
            {
                "block_id": "ingest_fig_01",
                "layer_id": "ingest_fig_01",
                "kind": "image",
                "role": "source_figure",
                "src_path": "/definitely/missing/source-figure.png",
            },
            {"x": 0, "y": 0, "w": 960, "h": 1080},
            theme,
        )
        markup = deck_html_renderer._placement_html(placement, {}, theme)
        result = deck_html_renderer.DeckHtmlRenderResult(
            slide_count=1,
            placements=[placement],
            layout_counts={"editorial_split": 1},
            layout_sequence=["editorial_split"],
        )
        result.stats = deck_html_renderer._layout_stats(
            result,
            slide_w=1920,
            slide_h=1080,
        )
        findings = deck_html_renderer.audit_deck_html_layout(
            result,
            slide_w=1920,
            slide_h=1080,
        )

        self.assertTrue(placement.missing_source_visual)
        self.assertIn('data-od-missing-source-visual="true"', markup)
        self.assertIn('data-missing-reason="unresolved-local-image"', markup)
        self.assertEqual(result.stats["avg_visual_area_ratio"], 0.0)
        self.assertEqual(result.stats["missing_source_visual_count"], 1)
        self.assertIn("deck_missing_source_visual", {item["id"] for item in findings})

    def test_rendered_html_preserves_paper_provenance_data_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_path = root / "figure.png"
            image_path.write_bytes(
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
            )
            ctx = ToolContext(
                settings=SimpleNamespace(
                    fonts={},
                    fonts_dir=root,
                    default_text_font="NotoSansSC",
                ),
                run_dir=root,
                layers_dir=root / "layers",
                run_id="deck-provenance-test",
            )
            spec = SimpleNamespace(
                canvas={"w_px": 1920, "h_px": 1080},
                html_artifact=None,
                deck_html={
                    "title": "Paper deck",
                    "slides": [{
                        "slide_id": "slide_01",
                        "layout": "editorial_split",
                        "blocks": [{
                            "block_id": "claim_figure",
                            "kind": "image",
                            "role": "source_figure",
                            "src_path": str(image_path),
                            "source": "paper.pdf",
                            "source_id": "ingest_fig_03",
                            "evidence_quote": 'Accuracy improves by 4.2%.',
                            "evidence_source": "page 7",
                            "covers": ["evidence_02", "mechanism_01"],
                        }],
                    }],
                },
                palette=[],
                typography={},
                visual_profile=None,
                brief="Paper deck",
                layer_graph=[],
            )
            out_path = root / "deck.html"

            result = deck_html_renderer.write_html_first_deck(spec, out_path, ctx)
            output = out_path.read_text(encoding="utf-8")

        placement = result.placements[0]
        self.assertEqual(placement.source, "paper.pdf")
        self.assertEqual(placement.source_id, "ingest_fig_03")
        self.assertEqual(placement.evidence_quote, "Accuracy improves by 4.2%.")
        self.assertEqual(placement.evidence_source, "page 7")
        self.assertEqual(placement.covers, ["evidence_02", "mechanism_01"])
        self.assertIn('data-source="paper.pdf"', output)
        self.assertIn('data-source-id="ingest_fig_03"', output)
        self.assertIn('data-evidence-quote="Accuracy improves by 4.2%."', output)
        self.assertIn('data-evidence-source="page 7"', output)
        self.assertIn(
            'data-covers="[&quot;evidence_02&quot;,&quot;mechanism_01&quot;]"',
            output,
        )

    def test_empty_table_and_blank_slide_are_p0_and_not_visual_coverage(self) -> None:
        theme = deck_html_renderer._theme_tokens(
            SimpleNamespace(palette=[], typography={}, visual_profile=None),
            {"theme": {"profile": "technical_light"}},
        )
        empty_table = deck_html_renderer._placement(
            "slide_01",
            {
                "block_id": "empty_results_table",
                "kind": "table",
                "role": "results_table",
                "headers": [],
                "rows": [],
            },
            {"x": 0, "y": 0, "w": 1920, "h": 1080},
            theme,
        )
        markup = deck_html_renderer._placement_html(
            empty_table,
            {"headers": [], "rows": []},
            theme,
        )
        result = deck_html_renderer.DeckHtmlRenderResult(
            slide_count=2,
            slide_ids=["slide_01", "slide_02"],
            placements=[empty_table],
            layout_counts={"editorial_split": 2},
            layout_sequence=["editorial_split", "editorial_split"],
        )
        result.stats = deck_html_renderer._layout_stats(
            result,
            slide_w=1920,
            slide_h=1080,
        )
        findings = deck_html_renderer.audit_deck_html_layout(
            result,
            slide_w=1920,
            slide_h=1080,
        )

        self.assertTrue(empty_table.content_placeholder)
        self.assertIn('data-od-content-placeholder="true"', markup)
        self.assertEqual(result.stats["slide_count"], 2)
        self.assertEqual(result.stats["empty_slide_ids"], ["slide_02"])
        self.assertEqual(result.stats["content_placeholder_count"], 1)
        self.assertEqual(result.stats["avg_visual_area_ratio"], 0.0)
        finding_ids = {item["id"] for item in findings}
        self.assertIn("deck_empty_slide", finding_ids)
        self.assertIn("deck_content_placeholder", finding_ids)

    def test_header_only_tables_are_placeholders_not_visual_evidence(self) -> None:
        theme = deck_html_renderer._theme_tokens(
            SimpleNamespace(palette=[], typography={}, visual_profile=None),
            {"theme": {"profile": "technical_light"}},
        )
        for block in (
            {
                "block_id": "explicit_header_only",
                "kind": "table",
                "headers": ["Method", "Score"],
                "rows": [],
            },
            {
                "block_id": "promoted_header_only",
                "kind": "table",
                "headers": [],
                "rows": [["Method", "Score"]],
            },
        ):
            with self.subTest(block_id=block["block_id"]):
                placement = deck_html_renderer._placement(
                    "slide_01",
                    block,
                    {"x": 0, "y": 0, "w": 960, "h": 540},
                    theme,
                )
                self.assertTrue(placement.content_placeholder)

    def test_structural_border_meets_non_text_contrast_without_darkening_decorative_rule(self) -> None:
        for profile_id in ("modern_serif", "metropolis_light", "technical_light"):
            theme = deck_html_renderer._theme_tokens(
                SimpleNamespace(palette=[], typography={}, visual_profile=None),
                {"theme": {"profile": profile_id}},
            )
            with self.subTest(profile_id=profile_id):
                self.assertGreaterEqual(
                    deck_html_renderer._contrast_ratio(
                        theme["structural_border"],
                        theme["surface"],
                    ),
                    3.0,
                )
                self.assertGreaterEqual(
                    deck_html_renderer._contrast_ratio(
                        theme["structural_border"],
                        theme["bg"],
                    ),
                    3.0,
                )
                self.assertNotEqual(theme["border"], theme["structural_border"])

        custom = deck_html_renderer._theme_tokens(
            SimpleNamespace(palette=[], typography={}, visual_profile=None),
            {
                "theme": {
                    "bg": "#888888",
                    "surface": "#FFFFFF",
                    "structural_border": "#777777",
                }
            },
        )
        self.assertGreaterEqual(
            deck_html_renderer._contrast_ratio(custom["structural_border"], custom["bg"]),
            3.0,
        )
        self.assertGreaterEqual(
            deck_html_renderer._contrast_ratio(custom["structural_border"], custom["surface"]),
            3.0,
        )


if __name__ == "__main__":
    unittest.main()
