from __future__ import annotations

import unittest

from autodesign.evaluator.poster_rubric import DIMENSIONS
from autodesign.evaluator.quality_rubric import aggregate_final


def _deterministic(
    *,
    density: float | None,
    readability: float | None,
) -> dict[str, object]:
    components = {
        dimension.id: {"score_0_10": 8.0, "status": "ok", "metrics": {}}
        for dimension in DIMENSIONS
    }
    components["information_density_and_synthesis"]["score_0_10"] = density
    components["layout_readability"]["score_0_10"] = readability
    return {
        "dimension_components": components,
        "gate": {"triggered": False, "ceiling": 40.0, "p0_finding_ids": []},
        "findings": [],
    }


def _judge(
    *,
    readability: float | None,
    aesthetics: float | None,
    serious_dimensions: set[str] | None = None,
) -> dict[str, object]:
    serious_dimensions = serious_dimensions or set()
    scores: dict[str, dict[str, object]] = {}
    for dimension in DIMENSIONS:
        score: float | None = 8.0
        if dimension.id == "layout_readability":
            score = readability
        elif dimension.id == "professional_aesthetics":
            score = aesthetics
        entry: dict[str, object] = {
            "defects_found": ([{
                "defect": "confirmed serious visual defect",
                "where": "poster body",
                "severity": "serious",
            }] if dimension.id in serious_dimensions else []),
        }
        if score is not None:
            entry["score_0_10"] = score
        scores[dimension.id] = entry
    return {"dimension_scores": scores}


def _aggregate(
    *,
    density: float | None,
    deterministic_readability: float | None,
    judge_readability: float | None,
    aesthetics: float | None,
    faithfulness: float | None = None,
    serious_dimensions: set[str] | None = None,
):
    deterministic = _deterministic(
        density=density,
        readability=deterministic_readability,
    )
    judge = _judge(
        readability=judge_readability,
        aesthetics=aesthetics,
        serious_dimensions=serious_dimensions,
    )
    if faithfulness is not None:
        deterministic["dimension_components"]["source_faithfulness"]["score_0_10"] = faithfulness
        judge["dimension_scores"]["source_faithfulness"]["score_0_10"] = faithfulness
    return aggregate_final(
        deterministic,
        judge,
        mode="benchmark",
        candidate_name="presentation-viability",
        artifact="poster.png",
        paper="paper.pdf",
    )


class PresentationViabilityCapTest(unittest.TestCase):
    def test_viability_ceiling_does_not_raise_lower_weighted_score(self) -> None:
        report = _aggregate(
            density=5.5,
            deterministic_readability=6.0,
            judge_readability=6.0,
            aesthetics=8.0,
            faithfulness=6.0,
        )

        self.assertEqual(report.overall_score_0_100, 67.25)
        finding = next(
            finding
            for finding in report.findings
            if finding.id == "presentation-viability-score-cap"
        )
        self.assertEqual(finding.evidence, {
            "information_density_and_synthesis": 5.5,
            "layout_readability": 6.0,
            "professional_aesthetics": 6.0,
            "presentation_viability": 5.75,
            "minimum_input_score": 5.5,
            "minimum_input_below_6": True,
            "presentation_viability_below_6_25": True,
            "weak_dimensions": ["information_density_and_synthesis"],
            "input_score_stage": "post_dimension_caps",
            "minimum_input_threshold_0_10": 6.0,
            "presentation_viability_threshold_0_10": 6.25,
            "score_ceiling": 67.5,
        })

    def test_does_not_trigger_at_either_strict_threshold_boundary(self) -> None:
        minimum_boundary = _aggregate(
            density=6.0,
            deterministic_readability=6.0,
            judge_readability=6.0,
            aesthetics=6.0,
        )
        viability_boundary = _aggregate(
            density=5.5,
            deterministic_readability=8.0,
            judge_readability=8.0,
            aesthetics=8.0,
        )

        for report in (minimum_boundary, viability_boundary):
            self.assertNotIn(
                "presentation-viability-score-cap",
                [finding.id for finding in report.findings],
            )

    def test_missing_any_viability_input_fails_open(self) -> None:
        cases = (
            {"density": None, "deterministic_readability": 5.0, "judge_readability": 5.0, "aesthetics": 5.0},
            {"density": 5.0, "deterministic_readability": None, "judge_readability": None, "aesthetics": 5.0},
            {"density": 5.0, "deterministic_readability": 5.0, "judge_readability": 5.0, "aesthetics": None},
        )

        for case in cases:
            with self.subTest(case=case):
                report = _aggregate(**case)
                self.assertNotIn(
                    "presentation-viability-score-cap",
                    [finding.id for finding in report.findings],
                )

    def test_single_judge_dimension_defect_no_longer_caps_overall_at_69(self) -> None:
        report = _aggregate(
            density=8.0,
            deterministic_readability=8.0,
            judge_readability=8.0,
            aesthetics=8.0,
            serious_dimensions={"visual_evidence_use"},
        )

        self.assertEqual(report.overall_score_0_100, 80.0)
        self.assertNotIn(
            "judge-confirmed-serious-visual-defect",
            [finding.id for finding in report.findings],
        )

    def test_multi_signal_major_visual_failure_ceiling_is_retained(self) -> None:
        report = _aggregate(
            density=8.0,
            deterministic_readability=8.0,
            judge_readability=8.0,
            aesthetics=8.0,
            serious_dimensions={
                "visual_evidence_use",
                "layout_readability",
                "professional_aesthetics",
            },
        )

        self.assertEqual(report.overall_score_0_100, 55.0)
        self.assertIn(
            "judge-confirmed-major-visual-failure",
            [finding.id for finding in report.findings],
        )


if __name__ == "__main__":
    unittest.main()
