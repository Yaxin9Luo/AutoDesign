from __future__ import annotations

import unittest

from autodesign.evaluator.metrics import visual_evidence_metrics
from autodesign.evaluator import tools


def _rect(x0: float, y0: float, x1: float, y1: float) -> dict[str, float]:
    w = x1 - x0
    h = y1 - y0
    return {
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "w": w,
        "h": h,
        "area_ratio": round((w * h) / (1000 * 500), 4),
        "canvas_width": 1000,
        "canvas_height": 500,
    }


class EvaluatorVisualEvidenceTest(unittest.TestCase):
    def test_visual_evidence_groups_adjacent_overlapping_and_contained_regions(self) -> None:
        rects = [
            _rect(100, 100, 300, 300),
            _rect(120, 120, 180, 180),  # contained in the first evidence block
            _rect(304, 100, 450, 300),  # adjacent subfigure in the same evidence block
            _rect(600, 50, 850, 180),
            _rect(900, 400, 930, 430),
        ]

        bundle, findings = visual_evidence_metrics(rects, 0.32)

        self.assertEqual(findings, [])
        self.assertEqual(bundle.metrics["figure_region_count"], 5)
        self.assertEqual(bundle.metrics["evidence_group_count"], 3)
        self.assertAlmostEqual(bundle.metrics["evidence_group_area_ratio"], 0.2068, places=4)
        self.assertAlmostEqual(bundle.metrics["largest_group_area_ratio"], 0.14, places=4)
        self.assertAlmostEqual(bundle.metrics["median_group_short_edge_ratio"], 0.26, places=4)
        self.assertEqual(bundle.metrics["thumbnail_group_count"], 1)

    def test_nearby_independent_figures_remain_separate_groups(self) -> None:
        rects = [
            _rect(100, 100, 250, 250),
            _rect(262, 100, 412, 250),
        ]

        bundle, findings = visual_evidence_metrics(rects, 0.2)

        self.assertEqual(findings, [])
        self.assertEqual(bundle.metrics["figure_region_count"], 2)
        self.assertEqual(bundle.metrics["evidence_group_count"], 2)

    def test_visual_evidence_grounding_uses_groups_not_raw_region_count_as_anchor(self) -> None:
        grounding = {
            "available": True,
            "figure_region_count": 7,
            "evidence_group_count": 2,
            "evidence_group_area_ratio": 0.18,
            "largest_group_area_ratio": 0.11,
            "median_group_short_edge_ratio": 0.19,
            "thumbnail_group_count": 1,
            "text_coverage": 0.31,
            "no_figures_detected": False,
            "possible_screenshot_wall": False,
            "figure_cramming": False,
        }

        prompt = tools._format_grounding("visual_evidence_use", grounding)
        notes = tools._DIMENSION_SCORING_NOTES["visual_evidence_use"]

        self.assertIn("evidence groups=2", prompt)
        self.assertIn("raw detector boxes=7 (debug only)", prompt)
        self.assertIn("thumbnail-sized groups=1", prompt)
        self.assertNotIn("detected figure-shaped regions=7", prompt)
        self.assertIn("Do not award high scores based only on count, captions, or neat arrangement", notes)
        self.assertIn("9-10 requires multiple readable locally explained evidence groups", notes)
        self.assertIn("native tables or sparse line charts can be missed", notes)

    def test_professional_aesthetics_prompt_penalizes_template_rhythm_and_default_palettes(self) -> None:
        checklist = tools._DEFECT_CHECKLISTS["professional_aesthetics"]
        notes = tools._DIMENSION_SCORING_NOTES["professional_aesthetics"]

        self.assertIn("mechanical numbering", checklist)
        self.assertIn("template rhythm", notes)
        self.assertIn("6-7", notes)
        self.assertIn("8+", notes)
        self.assertIn("controlled non-default palette", notes)
        self.assertIn("scale variation", notes)
        self.assertIn("excessive colors", notes)
        self.assertIn("one-note default theme", notes)


if __name__ == "__main__":
    unittest.main()
