from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.build_small_subset_eval_report import (
    _compatible_vlm_provenance,
    _load_records,
    _render_html,
    _summarize,
    _write_combined_csv,
)


DIMENSION_IDS = (
    "source_faithfulness",
    "paper_coverage",
    "information_density_and_synthesis",
    "visual_evidence_use",
    "basic_layout_integrity",
    "layout_readability",
    "professional_aesthetics",
)


class SmallSubsetEvalReportTest(unittest.TestCase):
    def test_report_size_is_derived_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            suite = Path(tmp)
            systems = [
                {"key": "autodesign-test", "label": "AutoDesign Test", "items": 1},
                {"key": "direct-test", "label": "Direct Test", "items": 1},
            ]
            items = []
            for index, system in enumerate(systems):
                artifact = suite / "inputs" / system["key"] / "discipline" / "paper" / "poster.png"
                artifact.parent.mkdir(parents=True)
                artifact.write_bytes(b"poster")
                report = suite / "systems" / system["key"] / "case" / "poster_quality_report.json"
                report.parent.mkdir(parents=True)
                report.write_text(json.dumps({
                    "eval_protocol": "posterbench-final",
                    "evaluator_fingerprint": "sha256:" + "a" * 64,
                    "source_evaluator_fingerprint": "sha256:" + "c" * 64,
                    "vlm_prompt_fingerprint": "sha256:" + "b" * 64,
                    "judge_model": "gemini-3.5-flash",
                    "artifact_sha256": str(index),
                    "overall_score_0_100": 60.0 + index,
                    "verdict": "revise",
                    "dimensions": [
                        {"id": dimension, "score_0_10": 6.0 + index / 10.0}
                        for dimension in DIMENSION_IDS
                    ],
                    "findings": [],
                    "gate_triggered": False,
                    "gate_ceiling": None,
                }))
                summary = suite / "systems" / system["key"] / "benchmark_summary.json"
                summary.write_text(json.dumps({
                    "eval_protocol": "posterbench-final",
                    "evaluator_fingerprint": "sha256:" + "a" * 64,
                    "vlm_prompt_fingerprint": "sha256:" + "b" * 64,
                    "vlm_prompt_fingerprints": ["sha256:" + "b" * 64],
                    "source_evaluator_fingerprints": ["sha256:" + "c" * 64],
                    "batch_style_fingerprint": "sha256:" + "d" * 64,
                    "judge_models": ["gemini-3.5-flash"],
                    "batch_style_homogeneity": {"test-system": {
                        "status": "not_applicable",
                        "batch_style_fingerprint": "sha256:" + "d" * 64,
                        "judge_model": "gemini-3.5-flash",
                        "adjustment_points": 0.0,
                    }},
                    "records": [{
                    "status": "scored",
                    "officially_eligible": True,
                    "system": "test-system",
                    "case": "paper",
                    "discipline": "discipline",
                    "discipline_label": "Discipline",
                    "overall": 60.0 + index,
                    "verdict": "revise",
                    "dimensions": {
                        dimension: 6.0 + index / 10.0
                        for dimension in DIMENSION_IDS
                    },
                    "batch_style_status": "not_applicable",
                    "batch_style_fingerprint": "sha256:" + "d" * 64,
                    "batch_style_judge_model": "gemini-3.5-flash",
                    "homogeneity_adjustment": 0.0,
                    "report_path": str(report),
                }]}))
                items.append({
                    "system_key": system["key"],
                    "system_label": system["label"],
                    "discipline": "discipline",
                    "case": "paper",
                    "source": str(artifact),
                    "staged": str(artifact),
                    "sha256": str(index),
                })

            manifest = {"systems": systems, "items": items}
            records = _load_records(suite, manifest)
            rendered = _render_html(manifest, records, _summarize(records, systems), None)
            csv_path = suite / "combined.csv"
            _write_combined_csv(csv_path, records)
            csv_header = csv_path.read_text(encoding="utf-8-sig").splitlines()[0]

            stale_summary_path = suite / "systems" / systems[0]["key"] / "benchmark_summary.json"
            stale_summary = json.loads(stale_summary_path.read_text(encoding="utf-8"))
            stale_summary["records"][0]["overall"] = 99.0
            stale_summary_path.write_text(json.dumps(stale_summary), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "score disagrees"):
                _load_records(suite, manifest)

            stale_summary["records"][0]["overall"] = 60.0
            stale_summary["batch_style_homogeneity"]["test-system"]["status"] = "degraded"
            stale_summary_path.write_text(json.dumps(stale_summary), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "batch-style result"):
                _load_records(suite, manifest)

            stale_summary["batch_style_homogeneity"]["test-system"]["status"] = "not_applicable"
            stale_summary["records"][0]["verdict"] = "pass"
            stale_summary_path.write_text(json.dumps(stale_summary), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "verdict disagrees"):
                _load_records(suite, manifest)

        self.assertEqual(len(records), 2)
        self.assertIn("Small Subset 2 系统海报评测", rendered)
        self.assertIn("2 张海报", rendered)
        self.assertIn("显示 ${count} / 2", rendered)
        self.assertIn("PosterBench Final Eval", rendered)
        self.assertIn("judge gemini-3.5-flash", rendered)
        self.assertNotIn("rubric", rendered.lower())
        self.assertNotIn("sha256:", rendered)
        for field in (
            "source_evaluator_fingerprint",
            "vlm_prompt_fingerprint",
            "batch_style_fingerprint",
            "reaggregation_status",
            "officially_eligible",
        ):
            self.assertIn(field, csv_header)

    def test_reaggregated_is_publishable_but_degraded_reaggregation_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            suite = Path(tmp)
            key = "system"
            artifact = suite / "inputs" / key / "discipline" / "paper" / "poster.png"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"poster")
            report_path = suite / "systems" / key / "case" / "poster_quality_report.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(json.dumps({
                "eval_protocol": "posterbench-final",
                "evaluator_fingerprint": "sha256:" + "a" * 64,
                "source_evaluator_fingerprint": "sha256:" + "c" * 64,
                "vlm_prompt_fingerprint": "sha256:" + "b" * 64,
                "judge_model": "gemini-3.5-flash",
                "artifact_sha256": "poster-hash",
                "reaggregation_status": "ok",
                "overall_score_0_100": 60.0,
                "verdict": "revise",
                "dimensions": [
                    {"id": dimension, "score_0_10": 6.0}
                    for dimension in DIMENSION_IDS
                ],
                "findings": [],
            }), encoding="utf-8")
            summary_path = suite / "systems" / key / "benchmark_summary.json"
            summary = {
                "eval_protocol": "posterbench-final",
                "evaluator_fingerprint": "sha256:" + "a" * 64,
                "vlm_prompt_fingerprint": "sha256:" + "b" * 64,
                "vlm_prompt_fingerprints": ["sha256:" + "b" * 64],
                "source_evaluator_fingerprints": ["sha256:" + "c" * 64],
                "batch_style_fingerprint": "sha256:" + "d" * 64,
                "judge_models": ["gemini-3.5-flash"],
                "batch_style_homogeneity": {"test-system": {
                    "status": "not_applicable",
                    "batch_style_fingerprint": "sha256:" + "d" * 64,
                    "judge_model": "gemini-3.5-flash",
                    "adjustment_points": 0.0,
                }},
                "records": [{
                    "status": "reaggregated",
                    "officially_eligible": True,
                    "system": "test-system",
                    "case": "paper",
                    "discipline": "discipline",
                    "overall": 60.0,
                    "verdict": "revise",
                    "dimensions": {dimension: 6.0 for dimension in DIMENSION_IDS},
                    "batch_style_status": "not_applicable",
                    "batch_style_fingerprint": "sha256:" + "d" * 64,
                    "batch_style_judge_model": "gemini-3.5-flash",
                    "homogeneity_adjustment": 0.0,
                    "report_path": str(report_path),
                }],
            }
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            manifest = {
                "systems": [{"key": key, "label": "System", "items": 1}],
                "items": [{
                    "system_key": key,
                    "case": "paper",
                    "discipline": "discipline",
                    "staged": str(artifact),
                    "sha256": "poster-hash",
                }],
            }

            records = _load_records(suite, manifest)
            self.assertEqual(len(records), 1)

            summary["records"][0]["status"] = "reaggregated_degraded"
            summary["records"][0]["officially_eligible"] = False
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "publishable posters"):
                _load_records(suite, manifest)

    def test_current_and_declared_equivalent_legacy_vlm_provenance_can_mix(self) -> None:
        self.assertTrue(_compatible_vlm_provenance({
            "sha256:" + "a" * 64,
            "legacy-rubric:0.1.20",
        }))
        self.assertFalse(_compatible_vlm_provenance({
            "sha256:" + "a" * 64,
            "legacy-rubric:0.1.14",
        }))
        self.assertFalse(_compatible_vlm_provenance({"legacy-rubric:0.1.14"}))
        self.assertTrue(_compatible_vlm_provenance({
            "legacy-rubric:0.1.19",
            "legacy-rubric:0.1.20",
        }))


if __name__ == "__main__":
    unittest.main()
