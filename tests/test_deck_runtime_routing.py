from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from autodesign.skills.registry import SkillRegistry
from autodesign.tools._contract import ToolContext
from autodesign.tools import deck_html_renderer
from autodesign.util.deck_planner import plan_deck
from scripts.web_server import _apply_type_prologue


REPO_ROOT = Path(__file__).resolve().parents[1]


class DeckRuntimeRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = SkillRegistry.load(REPO_ROOT / "skills")

    def _deck_skill_ids(
        self,
        brief: str,
        attachments: list[Path],
    ) -> list[str]:
        bundle = self.registry.select(
            brief=brief,
            attachments=attachments,
            artifact_hint="deck",
        )
        return [skill_id for skill_id in bundle.ids if skill_id.startswith("deck.")]

    def _selected_ids(self, brief: str, attachments: list[Path]) -> list[str]:
        return self.registry.select(
            brief=brief,
            attachments=attachments,
            artifact_hint="deck",
        ).ids

    def test_paper_deck_composes_provenance_and_general_visual_skill_once(self) -> None:
        ids = self._deck_skill_ids(
            "Create an HTML-first 12-slide academic paper deck.",
            [Path("paper.pdf")],
        )

        self.assertEqual(
            ids,
            ["deck.paper2deck_provenance", "deck.html_ppt_general"],
        )
        self.assertEqual(len(ids), len(set(ids)))
        selected_ids = self._selected_ids(
            "Create an HTML-first 12-slide academic paper deck.",
            [Path("paper.pdf")],
        )
        self.assertFalse(any(skill_id.startswith("poster.") for skill_id in selected_ids))

    def test_report_deck_composes_report_and_general_visual_skill_once(self) -> None:
        ids = self._deck_skill_ids(
            "Turn this annual report into a presentation.",
            [Path("annual-report.pdf")],
        )

        self.assertEqual(
            ids,
            ["deck.report2deck_general", "deck.html_ppt_general"],
        )
        self.assertEqual(len(ids), len(set(ids)))

    def test_generic_pdf_deck_defaults_to_paper_provenance(self) -> None:
        ids = self._deck_skill_ids(
            "Create a deck from the attached PDF.",
            [Path("2401.12345.pdf")],
        )

        self.assertEqual(
            ids,
            ["deck.paper2deck_provenance", "deck.html_ppt_general"],
        )

    def test_business_pdf_cues_keep_report_routing(self) -> None:
        ids = self._deck_skill_ids(
            "Create a deck from this quarterly business review.",
            [Path("2401.12345.pdf")],
        )

        self.assertEqual(
            ids,
            ["deck.report2deck_general", "deck.html_ppt_general"],
        )

    def test_beautify_deck_keeps_the_beautify_skill_isolated(self) -> None:
        ids = self._deck_skill_ids(
            "Beautify this existing presentation without changing its content.",
            [Path("existing.pptx")],
        )

        self.assertEqual(ids, ["deck.ppt_beautify"])

    def test_renderer_owns_three_deterministic_academic_light_profiles(self) -> None:
        profiles = getattr(deck_html_renderer, "_ACADEMIC_LIGHT_THEME_PROFILES", {})

        self.assertEqual(
            set(profiles),
            {"modern_serif", "metropolis_light", "technical_light"},
        )
        for profile_id, profile in profiles.items():
            with self.subTest(profile_id=profile_id):
                self.assertIn(profile["display_font"], {
                    "PlayfairDisplay", "NotoSerifSC", "IBMPlexSans", "Inter",
                })
                self.assertIn(profile["body_font"], {
                    "NotoSansSC", "IBMPlexSans", "Inter",
                })
                self.assertNotEqual(profile["bg"].lower(), "#000000")
                self.assertNotEqual(profile["surface"].lower(), "#000000")
                for token in ("ink", "muted", "accent"):
                    self.assertGreaterEqual(
                        deck_html_renderer._contrast_ratio(
                            profile[token],
                            profile["bg"],
                        ),
                        4.5,
                    )

        spec = SimpleNamespace(palette=[], typography={}, visual_profile=None)
        authored = {"theme": {"profile": "modern_serif", "accent": "#245B78"}}
        first = deck_html_renderer._theme_tokens(spec, authored)
        second = deck_html_renderer._theme_tokens(spec, authored)

        self.assertEqual(first, second)
        self.assertEqual(first["profile"], "modern_serif")
        self.assertEqual(first["display_font"], "PlayfairDisplay")
        self.assertEqual(first["accent"], "#245B78")
        self.assertEqual(
            deck_html_renderer._theme_tokens(spec, {"theme": {}})["profile"],
            "custom",
        )

        unsafe = deck_html_renderer._theme_tokens(
            spec,
            {
                "theme": {
                    "profile": "technical_light",
                    "ink": "not-a-color",
                    "muted": "#F4F7F9",
                    "accent": "#FFFFFF",
                }
            },
        )
        for token in ("ink", "muted", "accent"):
            self.assertGreaterEqual(
                deck_html_renderer._contrast_ratio(unsafe[token], unsafe["bg"]),
                4.5,
            )

    def test_academic_deck_defaults_to_exactly_eighteen_unless_overridden(self) -> None:
        default_plan = plan_deck(
            "Create a conference deck from the attached PDF.",
            [Path("2401.12345.pdf")],
        )
        overridden_plan = plan_deck(
            "Create an 8-slide conference deck from the attached PDF.",
            [Path("2401.12345.pdf")],
        )
        opted_out_plan = plan_deck(
            "Create a deck from this paper; do not default to a fixed 18-slide outline.",
            [Path("2401.12345.pdf")],
        )

        self.assertEqual(default_plan["slide_count"], 18)
        self.assertEqual(default_plan["count_range"], [18, 18])
        self.assertEqual(len(default_plan["outline"]), 18)
        self.assertEqual(
            [slide["role"] for slide in default_plan["outline"]],
            [
                "cover", "outline", "problem-scope", "motivation",
                "prior-work", "contributions", "method-overview", "mechanism",
                "algorithm", "experiment-setup", "primary-results",
                "secondary-results", "ablation-analysis", "qualitative-analysis",
                "limitations", "implications", "takeaways", "closing",
            ],
        )
        self.assertEqual(
            default_plan["density_budget"]["target_words_per_substantive_slide"],
            [45, 110],
        )
        self.assertEqual(default_plan["density_budget"]["max_words_per_slide"], 140)
        self.assertEqual(overridden_plan["slide_count"], 8)
        self.assertEqual(overridden_plan["lock_level"], "hard")
        self.assertIsNone(opted_out_plan["slide_count"])
        self.assertEqual(opted_out_plan["status"], "pending")

    def test_web_deck_entry_keeps_the_academic_default_soft(self) -> None:
        plan = plan_deck(
            _apply_type_prologue(
                "Create a conference deck from the attached PDF.",
                "deck",
            ),
            [Path("2401.12345.pdf")],
        )

        self.assertEqual(plan["slide_count"], 18)
        self.assertEqual(plan["lock_level"], "soft")
        self.assertEqual(plan["source"], "academic_default")

    def test_full_formal_academic_profile_expands_storyboard_without_changing_default(self) -> None:
        formal_plan = plan_deck(
            "Create a full formal academic conference talk from the attached paper.",
            [Path("2401.12345.pdf")],
        )

        self.assertEqual(formal_plan["talk_profile"], "full_formal")
        self.assertGreaterEqual(formal_plan["slide_count"], 20)
        self.assertLessEqual(formal_plan["slide_count"], 26)
        self.assertEqual(len(formal_plan["outline"]), formal_plan["slide_count"])
        for item in formal_plan["outline"]:
            self.assertIn("chapter", item)
            self.assertIn("communication_job", item)
            self.assertIn("assertion_title", item)
            self.assertIn("scope", item)
            self.assertIn("layout_family", item)
            self.assertIn("evidence_refs", item)
            self.assertIn("speaker_note_intent", item)

    def test_eighteen_slide_default_does_not_leak_to_business_or_generic_sources(self) -> None:
        for cue in (
            "quarterly business review",
            "company profile",
            "case study",
            "industry research",
            "财报",
            "行业调研",
            "白皮书",
            "企业介绍",
        ):
            with self.subTest(cue=cue):
                business_plan = plan_deck(
                    f"Create a {cue} deck.",
                    [Path("source.pdf")],
                )
                self.assertEqual(business_plan["deck_subtype"], "report")
                self.assertNotEqual(business_plan["slide_count"], 12)
        generic_doc_plan = plan_deck(
            "Create a deck from the attached document.",
            [Path("notes.docx")],
        )
        image_plan = plan_deck(
            "Create a deck from the attached images.",
            [Path("diagram.png")],
        )

        self.assertEqual(generic_doc_plan["deck_subtype"], "general")
        self.assertNotEqual(generic_doc_plan["slide_count"], 12)
        self.assertEqual(image_plan["deck_subtype"], "general")
        self.assertNotEqual(image_plan["slide_count"], 12)

    def test_deck_html_is_script_free_and_exposes_declarative_slide_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = SimpleNamespace(
                fonts={},
                fonts_dir=root,
                default_text_font="NotoSansSC",
            )
            ctx = ToolContext(
                settings=settings,
                run_dir=root,
                layers_dir=root / "layers",
                run_id="deck-test",
            )
            slides = [
                {
                    "slide_id": f"slide_{index:02d}",
                    "title": f"Slide {index}",
                    "layout": "editorial_split",
                    "blocks": [{
                        "block_id": f"body_{index:02d}",
                        "kind": "text",
                        "role": "body",
                        "text": f"Editable body {index}",
                    }],
                }
                for index in range(1, 4)
            ]
            spec = SimpleNamespace(
                canvas={"w_px": 1920, "h_px": 1080},
                html_artifact=None,
                deck_html={
                    "title": "Academic deck",
                    "theme": {"profile": "technical_light"},
                    "slides": slides,
                },
                palette=[],
                typography={},
                visual_profile=None,
                brief="Academic deck",
                layer_graph=[],
            )
            out_path = root / "deck.html"

            deck_html_renderer.write_html_first_deck(spec, out_path, ctx)
            output = out_path.read_text(encoding="utf-8")

        self.assertNotIn("<script", output.lower())
        self.assertIn('data-current-slide', output)
        self.assertIn('prefers-reduced-motion: reduce', output)
        self.assertIn("@media print", output)
        self.assertIn("--od-slide-w: 1920px", output)
        self.assertIn("--od-slide-h: 1080px", output)
        self.assertIn('data-od-theme-profile="technical_light"', output)
        self.assertNotIn("background: #111827", output)


if __name__ == "__main__":
    unittest.main()
