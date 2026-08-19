from __future__ import annotations

from pathlib import Path
import unittest

from autodesign.config import resolve_template
from autodesign.runner import _select_effective_template
from autodesign.util.canvas_planner import (
    CanvasIntentError,
    parse_canvas_intent,
    plan_canvas,
    refine_canvas_plan_from_ingest,
)


def _reference_metadata(width: int, height: int) -> dict[str, object]:
    return {
        "default_canvas": {
            "w_px": width,
            "h_px": height,
            "dpi": 96,
            "aspect_ratio": f"{width}:{height}",
            "color_mode": "RGB",
        }
    }


class CanvasIntentTest(unittest.TestCase):
    def test_prompt_ratio_uses_short_edge_and_long_edge_cap(self) -> None:
        cases = [
            ("Academic poster, aspect ratio 1.4:1", 2150, 1536, "1.4:1"),
            ("Academic poster in 5/3", 2560, 1536, "5:3"),
            ("Academic poster with a 10x1 ratio", 4096, 410, "10:1"),
        ]
        for brief, width, height, aspect in cases:
            with self.subTest(brief=brief):
                plan = plan_canvas(brief, [Path("paper.pdf")])

                self.assertEqual(plan["source"], "explicit_ratio")
                self.assertEqual(plan["lock_level"], "hard")
                self.assertEqual(plan["canvas"], {
                    "w_px": width,
                    "h_px": height,
                    "dpi": 150,
                    "aspect_ratio": aspect,
                    "color_mode": "RGB",
                })


    def test_compatible_pixels_ratio_and_orientation_choose_exact_pixels(self) -> None:
        plan = plan_canvas(
            "Academic poster, exact canvas 2800x2000 px, aspect ratio 1.4:1, landscape",
            [],
            requested_template="neurips-portrait",
        )

        self.assertEqual(plan["source"], "explicit_pixels")
        self.assertEqual(plan["preset_id"], "custom-2800x2000")
        self.assertEqual(plan["canvas"]["w_px"], 2800)
        self.assertEqual(plan["canvas"]["h_px"], 2000)


    def test_incompatible_prompt_canvas_directives_raise_stable_error(self) -> None:
        cases = [
            "Academic poster, exact canvas 2400x1350 px and aspect ratio 4:3",
            "Academic poster in 4:3 and 3:4",
            "Academic poster, template: cvpr-landscape, portrait",
        ]
        for brief in cases:
            with self.subTest(brief=brief):
                with self.assertRaises(CanvasIntentError) as raised:
                    parse_canvas_intent(brief)

                self.assertEqual(raised.exception.code, "conflicting_canvas_directives")


    def test_named_prompt_template_wins_over_ui_template(self) -> None:
        plan = plan_canvas(
            "Academic poster. Template: academic-landscape-5x3",
            [],
            requested_template="neurips-portrait",
        )

        self.assertEqual(plan["source"], "explicit_template")
        self.assertEqual(plan["preset_id"], "academic-landscape-5x3")
        self.assertEqual(plan["canvas"]["w_px"], 2560)
        self.assertEqual(plan["canvas"]["h_px"], 1536)


    def test_unknown_explicit_prompt_template_raises_stable_error(self) -> None:
        with self.assertRaises(CanvasIntentError) as raised:
            parse_canvas_intent("Academic poster. Template: glossy-mega-board")

        self.assertEqual(raised.exception.code, "unknown_canvas_template")

    def test_nonpositive_explicit_ratio_raises_stable_error(self) -> None:
        for brief in (
            "Academic poster with aspect ratio 1:0",
            "Academic poster with aspect ratio -1:2",
        ):
            with self.subTest(brief=brief):
                with self.assertRaises(CanvasIntentError) as raised:
                    parse_canvas_intent(brief)

                self.assertEqual(raised.exception.code, "invalid_canvas_ratio")

    def test_named_poster_template_conflicts_with_explicit_deck_type(self) -> None:
        for brief in (
            "Type: deck\nTemplate: cvpr-landscape",
            "Create a slide deck. Template: cvpr-landscape",
        ):
            with self.subTest(brief=brief):
                with self.assertRaises(CanvasIntentError) as raised:
                    plan_canvas(brief, [])

                self.assertEqual(
                    raised.exception.code,
                    "conflicting_canvas_directives",
                )


    def test_generic_template_word_is_not_treated_as_an_explicit_template(self) -> None:
        self.assertIsNone(parse_canvas_intent(
            "Use the paper's template as visual inspiration for an academic poster."
        ))


    def test_prior_canvas_directives_do_not_enter_current_request_intent(self) -> None:
        brief = (
            "[Conversation context — your prior turns in this thread:]\n"
            "  • User: Use template: neurips-portrait and 3:4.\n"
            "[User's current request:]\n"
            "Type: poster (single-page). Use the attached paper."
        )

        self.assertIsNone(parse_canvas_intent(brief))
        plan = plan_canvas(brief, [Path("paper.pdf")])
        self.assertEqual(plan["preset_id"], "cvpr-landscape")


    def test_prompt_ratio_overrides_ui_preset_and_reference(self) -> None:
        plan = plan_canvas(
            "Academic poster in a 1.4:1 ratio",
            [],
            requested_template="neurips-portrait",
            reference_metadata=_reference_metadata(1200, 1800),
        )

        self.assertEqual(plan["source"], "explicit_ratio")
        self.assertEqual(plan["canvas"]["w_px"], 2150)
        self.assertEqual(plan["canvas"]["h_px"], 1536)


    def test_ui_preset_overrides_reference_and_is_hard_locked(self) -> None:
        plan = plan_canvas(
            "Academic paper poster",
            [],
            requested_template="poster-classic-4x3",
            reference_metadata=_reference_metadata(1200, 1800),
        )

        self.assertEqual(plan["source"], "template")
        self.assertEqual(plan["preset_id"], "poster-classic-4x3")
        self.assertEqual(plan["lock_level"], "hard")


    def test_prompt_ratio_survives_ingest_refinement(self) -> None:
        plan = plan_canvas("Academic paper poster in 5:3", [Path("paper.pdf")])

        refined = refine_canvas_plan_from_ingest(
            plan,
            [{"type": "pdf", "registered_figure_ids": ["figure_1"]}],
            {"figure_1": {"image_size": "200x100"}},
        )

        self.assertEqual(refined, plan)


    def test_legacy_geometry_named_templates_no_longer_resolve_to_cvpr(self) -> None:
        self.assertEqual(resolve_template("academic-landscape-1.414"), {
            "w_px": 2172,
            "h_px": 1536,
            "dpi": 150,
            "aspect_ratio": "1.414:1",
            "color_mode": "RGB",
        })
        self.assertEqual(resolve_template("academic-wide-3280x1860"), {
            "w_px": 3280,
            "h_px": 1860,
            "dpi": 150,
            "aspect_ratio": "164:93",
            "color_mode": "RGB",
        })

    def test_runner_preserves_registered_template_geometry_identity(self) -> None:
        self.assertEqual(
            _select_effective_template(
                "Academic poster",
                [Path("paper.pdf")],
                "academic-landscape-1.414",
            ),
            "academic-landscape-1.414",
        )


if __name__ == "__main__":
    unittest.main()
