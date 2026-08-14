from __future__ import annotations

import unittest

from autodesign.evaluator.poster_rubric import DIMENSIONS
from autodesign.evaluator.quality_rubric import aggregate_final


def _deterministic(
    *,
    density: float,
    basic: float,
    layout: float,
    findings: list[dict[str, object]] | None = None,
    gate: dict[str, object] | None = None,
) -> dict[str, object]:
    components = {
        dimension.id: {"score_0_10": 8.0, "status": "ok", "metrics": {}}
        for dimension in DIMENSIONS
    }
    components["information_density_and_synthesis"]["score_0_10"] = density
    components["basic_layout_integrity"]["score_0_10"] = basic
    components["layout_readability"]["score_0_10"] = layout
    return {
        "dimension_components": components,
        "gate": gate or {"triggered": False, "ceiling": 40.0, "p0_finding_ids": []},
        "findings": findings or [],
    }


def _judge(*, serious_dimensions: set[str]) -> dict[str, object]:
    return {
        "dimension_scores": {
            dimension.id: {
                "score_0_10": 8.0,
                "defects_found": ([{
                    "defect": "confirmed serious visual defect",
                    "where": "main poster body",
                    "severity": "serious",
                }] if dimension.id in serious_dimensions else []),
            }
            for dimension in DIMENSIONS
        }
    }


class PosterQualityAggregationTest(unittest.TestCase):
    def test_serious_visual_defect_plus_two_weak_objective_signals_caps_at_fail(self) -> None:
        report = aggregate_final(
            _deterministic(density=3.5, basic=3.0, layout=4.0),
            _judge(serious_dimensions={"visual_evidence_use"}),
            mode="benchmark",
            candidate_name="confirmed-major-failure",
            artifact="poster.png",
            paper="paper.pdf",
        )

        self.assertEqual(report.overall_score_0_100, 49.0)
        self.assertEqual(report.verdict, "fail")
        self.assertIn("judge-confirmed-major-visual-failure", [finding.id for finding in report.findings])

    def test_single_visual_defect_with_healthy_objective_scores_does_not_cap(self) -> None:
        report = aggregate_final(
            _deterministic(density=8.0, basic=8.0, layout=8.0),
            _judge(serious_dimensions={"visual_evidence_use"}),
            mode="benchmark",
            candidate_name="localized-defect",
            artifact="poster.png",
            paper="paper.pdf",
        )

        self.assertEqual(report.overall_score_0_100, 80.0)
        self.assertEqual(report.verdict, "pass")
        self.assertNotIn("judge-confirmed-major-visual-failure", [finding.id for finding in report.findings])
        self.assertNotIn("judge-confirmed-serious-visual-defect", [finding.id for finding in report.findings])

    def test_two_serious_visual_dimensions_only_block_a_clean_pass(self) -> None:
        report = aggregate_final(
            _deterministic(density=9.0, basic=8.0, layout=8.0),
            _judge(serious_dimensions={"visual_evidence_use", "layout_readability"}),
            mode="benchmark",
            candidate_name="multiple-serious-defects",
            artifact="poster.png",
            paper="paper.pdf",
        )

        self.assertEqual(report.overall_score_0_100, 69.0)
        self.assertEqual(report.verdict, "revise")

    def test_three_serious_visual_dimensions_cap_at_revise(self) -> None:
        report = aggregate_final(
            _deterministic(density=9.0, basic=8.0, layout=8.0),
            _judge(serious_dimensions={
                "visual_evidence_use",
                "layout_readability",
                "professional_aesthetics",
            }),
            mode="benchmark",
            candidate_name="cross-dimension-failure",
            artifact="poster.png",
            paper="paper.pdf",
        )

        self.assertEqual(report.overall_score_0_100, 55.0)
        self.assertEqual(report.verdict, "revise")

    def test_two_serious_dimensions_and_collapsed_layout_cap_at_revise(self) -> None:
        report = aggregate_final(
            _deterministic(density=9.0, basic=1.0, layout=8.0),
            _judge(serious_dimensions={"visual_evidence_use", "layout_readability"}),
            mode="benchmark",
            candidate_name="structural-collapse",
            artifact="poster.png",
            paper="paper.pdf",
        )

        self.assertEqual(report.overall_score_0_100, 55.0)
        self.assertEqual(report.verdict, "revise")

    def test_empty_visual_placeholder_caps_at_fifty(self) -> None:
        report = aggregate_final(
            _deterministic(
                density=9.0,
                basic=8.0,
                layout=9.0,
                findings=[{
                    "id": "basic-layout-empty-visual-placeholder",
                    "severity": "P1",
                    "message": "A framed visual slot is empty.",
                    "dimension": "basic_layout_integrity",
                }],
            ),
            _judge(serious_dimensions=set()),
            mode="benchmark",
            candidate_name="empty-visual-slot",
            artifact="poster.png",
            paper="paper.pdf",
        )

        self.assertEqual(report.overall_score_0_100, 50.0)
        self.assertEqual(report.verdict, "revise")
        self.assertIn("deterministic-major-visual-failure", [finding.id for finding in report.findings])

    def test_two_empty_visual_placeholders_trigger_ten_point_gate(self) -> None:
        report = aggregate_final(
            _deterministic(
                density=9.0,
                basic=7.0,
                layout=9.0,
                findings=[{
                    "id": "basic-layout-empty-visual-placeholder-severe",
                    "severity": "P0",
                    "message": "Two framed visual slots are empty.",
                    "dimension": "basic_layout_integrity",
                }],
                gate={
                    "triggered": True,
                    "ceiling": 10.0,
                    "p0_finding_ids": ["basic-layout-empty-visual-placeholder-severe"],
                },
            ),
            _judge(serious_dimensions=set()),
            mode="benchmark",
            candidate_name="multiple-empty-visual-slots",
            artifact="poster.png",
            paper="paper.pdf",
        )

        self.assertEqual(report.overall_score_0_100, 10.0)
        self.assertTrue(report.gate_triggered)
        self.assertEqual(report.gate_ceiling, 10.0)
        self.assertEqual(report.verdict, "fail")

    def test_multi_panel_crop_failure_caps_at_fifty(self) -> None:
        report = aggregate_final(
            _deterministic(
                density=9.0,
                basic=4.0,
                layout=9.0,
                findings=[{
                    "id": "basic-layout-multi-panel-crop-failure",
                    "severity": "P1",
                    "message": "Several visuals are clipped.",
                    "dimension": "basic_layout_integrity",
                }],
            ),
            _judge(serious_dimensions=set()),
            mode="benchmark",
            candidate_name="multi-panel-crop-failure",
            artifact="poster.png",
            paper="paper.pdf",
        )

        self.assertEqual(report.overall_score_0_100, 50.0)
        self.assertEqual(report.verdict, "revise")


if __name__ == "__main__":
    unittest.main()
