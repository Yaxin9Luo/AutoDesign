from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from autodesign.evaluator.metrics import paper_body_screenshot_metrics
from autodesign.evaluator.poster_rubric import (
    BODY_SCREENSHOT_CATASTROPHIC_GATE_CEILING_SCORE,
    DIMENSIONS,
)
from autodesign.evaluator.quality_rubric import aggregate_final


def _box(x: float, y: float, width: float, height: float = 8.0) -> list[list[float]]:
    return [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]


def _page_like_ocr(*, copied_width: float) -> tuple[dict[str, object], str]:
    paper_tokens = [f"paper{i}" for i in range(400)]
    poster_tokens = paper_tokens[:160] + [f"poster{i}" for i in range(1140)]
    segments: list[dict[str, object]] = []
    for index in range(12):
        start = index * 12
        segments.append({
            "box": _box(20.0, 12.0 + index * 10.0, copied_width),
            "text": " ".join(paper_tokens[start:start + 12]),
            "score": 0.99,
        })
    for index in range(179):
        x = 800.0 if index == 178 else 500.0
        y = 492.0 if index == 178 else 140.0 + (index % 35) * 10.0
        segments.append({
            "box": _box(x, y, 200.0),
            "text": " ".join(f"other{index}_{token}" for token in range(6)),
            "score": 0.99,
        })
    return ({
        "available": True,
        "image_size": [1000, 500],
        "text": " ".join(poster_tokens),
        "word_count": len(poster_tokens),
        "text_coverage_ratio": 0.36,
        "segments": segments,
    }, " ".join(paper_tokens))


def _regional_copy_ocr(
    *,
    total_words: int,
    copied_words: int,
    copied_segments: int,
    copied_width: float,
    other_segments: int,
    other_width: float,
    text_coverage: float,
    segment_height: float = 8.0,
) -> tuple[dict[str, object], str]:
    paper_tokens = [f"paperword{index:04d}" for index in range(900)]
    poster_tokens = paper_tokens[:copied_words] + [
        f"posterword{index:04d}"
        for index in range(total_words - copied_words)
    ]
    segments: list[dict[str, object]] = []
    cursor = 0
    for index in range(copied_segments):
        remaining_segments = copied_segments - index
        segment_words = (copied_words - cursor) // remaining_segments
        segments.append({
            "box": _box(20.0, 12.0 + index * 9.0, copied_width, segment_height),
            "text": " ".join(paper_tokens[cursor:cursor + segment_words]),
            "score": 0.99,
        })
        cursor += segment_words
    for index in range(other_segments):
        segments.append({
            "box": _box(520.0, 12.0 + (index % 48) * 9.0, other_width, segment_height),
            "text": " ".join(f"otherword{index:03d}{token}" for token in range(6)),
            "score": 0.99,
        })
    return ({
        "available": True,
        "image_size": [1000, 500],
        "text": " ".join(poster_tokens),
        "word_count": total_words,
        "text_coverage_ratio": text_coverage,
        "segments": segments,
    }, " ".join(paper_tokens))


class PaperBodyScreenshotMetricsTest(unittest.TestCase):
    def test_small_copied_microtext_area_does_not_trigger_page_crop(self) -> None:
        ocr, paper_text = _page_like_ocr(copied_width=200.0)
        with patch("autodesign.evaluator.metrics._extract_paper_text", return_value=paper_text):
            bundle, findings = paper_body_screenshot_metrics(Path("paper.txt"), ocr)

        self.assertEqual(bundle.metrics["severity_level"], "none")
        self.assertEqual(findings, [])
        self.assertLess(bundle.metrics["copied_body_segment_area_ratio"], 0.07)

    def test_copied_area_uses_full_image_canvas(self) -> None:
        ocr, paper_text = _page_like_ocr(copied_width=400.0)
        ocr["image_size"] = [2000, 1000]
        with patch("autodesign.evaluator.metrics._extract_paper_text", return_value=paper_text):
            bundle, findings = paper_body_screenshot_metrics(Path("paper.txt"), ocr)

        self.assertEqual(bundle.metrics["severity_level"], "none")
        self.assertEqual(findings, [])
        self.assertLess(bundle.metrics["copied_body_segment_area_ratio"], 0.02)

    def test_large_copied_microtext_area_triggers_page_crop(self) -> None:
        ocr, paper_text = _page_like_ocr(copied_width=400.0)
        with patch("autodesign.evaluator.metrics._extract_paper_text", return_value=paper_text):
            bundle, findings = paper_body_screenshot_metrics(Path("paper.txt"), ocr)

        self.assertEqual(bundle.metrics["severity_level"], "severe")
        self.assertEqual(bundle.metrics["severe_reason"], "paper_page_microtext_crop")
        self.assertEqual([finding.id for finding in findings], ["paper-body-screenshot-severe"])
        self.assertGreaterEqual(bundle.metrics["copied_body_segment_area_ratio"], 0.07)

    def test_regional_copy_triggers_severe_below_dense_crop_word_floor(self) -> None:
        ocr, paper_text = _regional_copy_ocr(
            total_words=967,
            copied_words=264,
            copied_segments=22,
            copied_width=380.0,
            other_segments=66,
            other_width=127.0,
            text_coverage=0.3117,
        )
        bundle, findings = paper_body_screenshot_metrics(
            Path("paper.txt"),
            ocr,
            paper_text=paper_text,
        )

        self.assertEqual(bundle.metrics["severity_level"], "severe")
        self.assertEqual(bundle.metrics["severe_reason"], "regional_paper_crop")
        self.assertEqual([finding.id for finding in findings], ["paper-body-screenshot-severe"])

    def test_regional_copy_allows_readable_crop_text_height(self) -> None:
        ocr, paper_text = _regional_copy_ocr(
            total_words=967,
            copied_words=264,
            copied_segments=22,
            copied_width=380.0,
            other_segments=66,
            other_width=127.0,
            text_coverage=0.3117,
            segment_height=9.6,
        )
        bundle, findings = paper_body_screenshot_metrics(
            Path("paper.txt"),
            ocr,
            paper_text=paper_text,
        )

        self.assertGreater(bundle.metrics["page_crop_layout"]["median_body_segment_height_ref_px"], 18.5)
        self.assertEqual(bundle.metrics["severity_level"], "severe")
        self.assertEqual(bundle.metrics["severe_reason"], "regional_paper_crop")
        self.assertEqual([finding.id for finding in findings], ["paper-body-screenshot-severe"])

    def test_distributed_microtext_page_crop_survives_fragmented_ocr_matches(self) -> None:
        ocr, paper_text = _regional_copy_ocr(
            total_words=1600,
            copied_words=250,
            copied_segments=20,
            copied_width=130.0,
            other_segments=200,
            other_width=80.0,
            text_coverage=0.35,
        )
        bundle, findings = paper_body_screenshot_metrics(
            Path("paper.txt"),
            ocr,
            paper_text=paper_text,
        )

        self.assertLess(bundle.metrics["copied_body_segment_area_ratio"], 0.07)
        self.assertGreaterEqual(bundle.metrics["page_crop_layout"]["microtext_segment_ratio"], 0.75)
        self.assertEqual(bundle.metrics["severity_level"], "severe")
        self.assertEqual(bundle.metrics["severe_reason"], "distributed_paper_page_crop")
        self.assertEqual([finding.id for finding in findings], ["paper-body-screenshot-severe"])

    def test_sparse_regional_copy_triggers_severe_below_old_scan_floor(self) -> None:
        ocr, paper_text = _regional_copy_ocr(
            total_words=734,
            copied_words=197,
            copied_segments=13,
            copied_width=368.0,
            other_segments=42,
            other_width=183.0,
            text_coverage=0.2186,
        )
        bundle, findings = paper_body_screenshot_metrics(
            Path("paper.txt"),
            ocr,
            paper_text=paper_text,
        )

        self.assertEqual(bundle.metrics["severity_level"], "severe")
        self.assertEqual(bundle.metrics["severe_reason"], "sparse_paper_crop")
        self.assertEqual([finding.id for finding in findings], ["paper-body-screenshot-severe"])

    def test_catastrophic_copy_wall_is_distinct_from_severe_crop(self) -> None:
        ocr, paper_text = _regional_copy_ocr(
            total_words=1600,
            copied_words=520,
            copied_segments=50,
            copied_width=350.0,
            other_segments=70,
            other_width=120.0,
            text_coverage=0.50,
        )
        bundle, findings = paper_body_screenshot_metrics(
            Path("paper.txt"),
            ocr,
            paper_text=paper_text,
        )

        self.assertEqual(bundle.metrics["severity_level"], "catastrophic")
        self.assertEqual(bundle.metrics["catastrophic_reason"], "copied_body_canvas_wall")
        self.assertEqual([finding.id for finding in findings], ["paper-body-screenshot-catastrophic"])

    def test_severe_page_crop_caps_final_score_at_30(self) -> None:
        deterministic = {
            "dimension_components": {
                dimension.id: {"score_0_10": 10.0, "status": "ok", "metrics": {}}
                for dimension in DIMENSIONS
            },
            "gate": {
                "triggered": True,
                "ceiling": 30.0,
                "p0_finding_ids": ["paper-body-screenshot-severe"],
            },
            "findings": [{
                "id": "paper-body-screenshot-severe",
                "severity": "P0",
                "message": "Large paper-body screenshot detected.",
                "dimension": "render_integrity",
                "evidence": {},
            }],
        }
        judge_report = {
            "dimension_scores": {
                dimension.id: {"score_0_10": 10.0}
                for dimension in DIMENSIONS
            }
        }

        report = aggregate_final(
            deterministic,
            judge_report,
            mode="benchmark",
            candidate_name="known-bad",
            artifact="poster.png",
            paper="paper.pdf",
        )

        self.assertTrue(report.gate_triggered)
        self.assertEqual(report.gate_ceiling, 30.0)
        self.assertEqual(report.overall_score_0_100, 30.0)
        self.assertEqual(report.verdict, "fail")

    def test_catastrophic_page_crop_can_cap_final_score_at_zero(self) -> None:
        deterministic = {
            "dimension_components": {
                dimension.id: {"score_0_10": 10.0, "status": "ok", "metrics": {}}
                for dimension in DIMENSIONS
            },
            "gate": {
                "triggered": True,
                "ceiling": BODY_SCREENSHOT_CATASTROPHIC_GATE_CEILING_SCORE,
                "p0_finding_ids": ["paper-body-screenshot-catastrophic"],
            },
            "findings": [{
                "id": "paper-body-screenshot-catastrophic",
                "severity": "P0",
                "message": "Poster content is a paper-body screenshot wall.",
                "dimension": "render_integrity",
                "evidence": {},
            }],
        }
        judge_report = {
            "dimension_scores": {
                dimension.id: {"score_0_10": 10.0}
                for dimension in DIMENSIONS
            }
        }

        report = aggregate_final(
            deterministic,
            judge_report,
            mode="benchmark",
            candidate_name="catastrophic",
            artifact="poster.png",
            paper="paper.pdf",
        )

        self.assertTrue(report.gate_triggered)
        self.assertEqual(report.gate_ceiling, 0.0)
        self.assertEqual(report.overall_score_0_100, 0.0)
        self.assertEqual(report.verdict, "fail")


if __name__ == "__main__":
    unittest.main()
