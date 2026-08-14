from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from PIL import Image

from autodesign.evaluator.benchmark_calibration_report import (
    build_anonymous_system_contact_sheet,
    build_same_paper_comparison_section,
    render_system_explainability_fields,
)


def _image(path: Path, color: str) -> Path:
    Image.new("RGB", (220, 110), color).save(path)
    return path


class BenchmarkCalibrationReportTest(unittest.TestCase):
    def test_anonymous_system_contact_sheet_writes_pixels_without_system_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _image(root / "first.png", "red")
            second = _image(root / "second.png", "blue")
            out = root / "system_a_sheet.jpg"

            result = build_anonymous_system_contact_sheet(
                [
                    {"system_label": "System A", "artifact": str(first), "case": "paper-1"},
                    {"system_label": "System A", "artifact": str(second), "case": "paper-2"},
                    {"system_label": "System A", "artifact": str(root / "missing.png"), "case": "paper-3"},
                ],
                out,
                max_items=100,
                columns=2,
                thumb_size=(120, 60),
            )

            self.assertEqual(result["image_path"], str(out))
            self.assertEqual(result["items_total"], 3)
            self.assertEqual(result["items_rendered"], 2)
            self.assertEqual(result["missing_artifacts"], 1)
            self.assertTrue(out.exists())
            self.assertIn("missing artifact", " ".join(result["degraded_notes"]).lower())
            self.assertFalse(result["batch_style_judge_input"]["labels_in_pixels"])
            self.assertNotIn("System A", str(result["batch_style_judge_input"]))

    def test_same_paper_comparison_limits_to_30_papers_and_keeps_labels_out_of_judge_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = []
            for paper_idx in range(31):
                for system_idx, system_label in enumerate(("System A", "System B")):
                    artifact = _image(
                        root / f"paper_{paper_idx:02d}_{system_idx}.png",
                        "red" if system_idx == 0 else "blue",
                    )
                    records.append({
                        "discipline": "ai_ml_existing_20",
                        "discipline_label": "AI/ML",
                        "case": f"paper-{paper_idx:02d}",
                        "system": f"system-{system_idx}",
                        "system_label": system_label,
                        "artifact": str(artifact),
                    })

            result = build_same_paper_comparison_section(
                records,
                root / "comparison",
                max_papers=30,
                thumb_size=(80, 40),
            )

            self.assertEqual(result["papers_selected"], 30)
            self.assertEqual(result["items_total"], 60)
            self.assertTrue(Path(result["contact_sheet_path"]).exists())
            self.assertFalse(result["batch_style_judge_input"]["labels_in_pixels"])
            self.assertNotIn("System A", str(result["batch_style_judge_input"]))
            self.assertNotIn("System B", str(result["batch_style_judge_input"]))
            self.assertIn("System A", result["html_section"])
            self.assertIn("System B", result["html_section"])
            self.assertIn("same-paper-comparison", result["html_section"])

    def test_same_paper_comparison_balances_30_papers_across_five_disciplines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = []
            for discipline_idx in range(5):
                discipline = f"discipline-{discipline_idx}"
                for paper_idx in range(10):
                    for system_idx in range(2):
                        artifact = _image(
                            root / f"{discipline}_{paper_idx}_{system_idx}.png",
                            "red" if system_idx == 0 else "blue",
                        )
                        records.append({
                            "discipline": discipline,
                            "case": f"paper-{paper_idx:02d}",
                            "system": f"system-{system_idx}",
                            "artifact": str(artifact),
                        })

            result = build_same_paper_comparison_section(
                records,
                root / "comparison",
                max_papers=30,
                thumb_size=(40, 20),
            )

        self.assertEqual(result["papers_selected"], 30)
        self.assertEqual(result["selected_discipline_counts"], {
            f"discipline-{index}": 6 for index in range(5)
        })

    def test_explainability_fields_render_audit_values_without_recomputing_scores(self) -> None:
        result = render_system_explainability_fields([
            {
                "system": "system-a",
                "system_label": "System A",
                "raw_professional_aesthetics": 8.8,
                "adjusted_professional_aesthetics": 6.0,
                "style_adaptability": 7.1,
                "homogeneity_adjustment": -0.4,
                "evidence_group_count": 5,
                "evidence_area_ratio": 0.32,
                "legibility_cap": 6.5,
                "trusted_layout_p1_source": "deterministic-basic-layout",
                "trusted_layout_p1_count": 9,
                "trusted_layout_p1_rate": 0.09,
                "presentation_viability_mean": 7.25,
                "presentation_viability_trigger_count": 4,
                "presentation_viability_trigger_rate": 0.20,
                "presentation_viability_ceiling": 69,
                "presentation_viability_weak_dimensions": [
                    "basic_layout_integrity",
                    "layout_readability",
                ],
            }
        ])

        html = result["html"]
        self.assertEqual(result["rows_rendered"], 1)
        self.assertEqual(result["degraded_notes"], [])
        self.assertIn("Raw professional aesthetics", html)
        self.assertIn("Adjusted professional aesthetics", html)
        self.assertIn("8.80", html)
        self.assertIn("6.00", html)
        self.assertIn("-0.40", html)
        self.assertIn("deterministic-basic-layout", html)
        self.assertIn("9", html)
        self.assertIn("0.09", html)
        self.assertIn("Presentation viability mean", html)
        self.assertIn("Presentation viability trigger count", html)
        self.assertIn("Presentation viability trigger rate", html)
        self.assertIn("Presentation viability ceiling", html)
        self.assertIn("Weak dimensions", html)
        self.assertIn("7.25", html)
        self.assertIn("4.00", html)
        self.assertIn("0.20", html)
        self.assertIn("69.00", html)
        self.assertIn("basic_layout_integrity, layout_readability", html)
        self.assertIn(
            "method-agnostic non-compensability/pass-eligibility rule, not a method penalty",
            html,
        )

    def test_raw_figure_metrics_do_not_masquerade_as_evidence_groups(self) -> None:
        result = render_system_explainability_fields([{
            "system": "legacy-system",
            "system_label": "Legacy system",
            "visual_evidence": {"figure_region_count": 12, "figure_area_ratio": 0.6},
        }])

        self.assertIn("missing explainability fields", " ".join(result["degraded_notes"]).lower())
        self.assertNotIn(">12.00<", result["html"])
        self.assertNotIn(">0.60<", result["html"])

    def test_explainability_fields_degrade_explicitly_when_fields_are_missing(self) -> None:
        result = render_system_explainability_fields([
            {"system": "system-a", "system_label": "System A"}
        ])

        self.assertEqual(result["rows_rendered"], 1)
        self.assertGreaterEqual(len(result["degraded_notes"]), 1)
        self.assertIn("missing explainability fields", " ".join(result["degraded_notes"]).lower())
        self.assertIn("System A", result["html"])
        self.assertIn("—", result["html"])


if __name__ == "__main__":
    unittest.main()
