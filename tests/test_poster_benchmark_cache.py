from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
import fitz

from autodesign.evaluator import tools
from autodesign.evaluator.poster_rubric import DIMENSIONS
from autodesign.evaluator.protocol import (
    EVAL_PROTOCOL,
    EVALUATOR_FINGERPRINT,
    VLM_PROMPT_FINGERPRINT,
)
from autodesign.evaluator.quality_rubric import aggregate_final
from autodesign.evaluator.tools import DEFAULT_BENCHMARK_JUDGE_MODEL
from autodesign.evaluator.vlm_benchmark import PosterCandidate, run_poster_benchmark
from scripts import run_poster_benchmark_main_table as benchmark
from scripts.run_poster_benchmark_main_table import _file_sha256, _judge_image


CURRENT_FINAL_METADATA = {
    "eval_protocol": EVAL_PROTOCOL,
    "evaluator_fingerprint": benchmark.BENCHMARK_EVALUATOR_FINGERPRINT,
    "source_evaluator_fingerprint": EVALUATOR_FINGERPRINT,
    "vlm_prompt_fingerprint": VLM_PROMPT_FINGERPRINT,
}
CURRENT_VLM_METADATA = {
    "eval_protocol": EVAL_PROTOCOL,
    "vlm_prompt_fingerprint": VLM_PROMPT_FINGERPRINT,
}
PRE_RENAME_EVALUATOR_FINGERPRINT = (
    "sha256:138f5cedc0ef5361ef0f8cb550c602d4c0e78f6f56a1d645816a1dcb311117f2"
)
PRE_RENAME_BENCHMARK_EVALUATOR_FINGERPRINT = (
    "sha256:67aa0c9ba0096c5082e0b176bbfe1cf70ba2372a832bcca1ad55b6e6a028620e"
)


class PosterBenchmarkCacheTest(unittest.TestCase):
    def test_pre_rename_evaluator_fingerprint_is_read_compatible(self) -> None:
        report = {
            "eval_protocol": EVAL_PROTOCOL,
            "evaluator_fingerprint": PRE_RENAME_EVALUATOR_FINGERPRINT,
        }

        self.assertTrue(benchmark._matches_evaluator_fingerprint(report))

    def test_pre_rename_benchmark_report_is_read_compatible(self) -> None:
        report = {
            **CURRENT_FINAL_METADATA,
            "evaluator_fingerprint": PRE_RENAME_BENCHMARK_EVALUATOR_FINGERPRINT,
            "source_evaluator_fingerprint": PRE_RENAME_EVALUATOR_FINGERPRINT,
            "overall_score_0_100": 80.0,
            "dimensions": [
                {"id": dim, "score_0_10": 8.0}
                for dim in benchmark.ALL_DIMS
            ],
        }
        self.assertTrue(benchmark._complete_final_report(report))

    def test_score_job_normalizes_pre_rename_benchmark_report_before_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "poster.png"
            paper = root / "paper.pdf"
            Image.new("RGB", (20, 10), "white").save(artifact)
            paper.write_bytes(b"paper")
            job = benchmark.CandidateJob(
                system="system",
                discipline="discipline",
                case="paper",
                paper=paper,
                artifact=artifact,
                source_name="poster.png",
            )
            final_path = (
                root
                / "candidates"
                / job.system
                / job.discipline
                / benchmark._safe_name(job.case)
                / "poster_quality_report.json"
            )
            final_path.parent.mkdir(parents=True)
            final_path.write_text(json.dumps({
                **CURRENT_FINAL_METADATA,
                "evaluator_fingerprint": PRE_RENAME_BENCHMARK_EVALUATOR_FINGERPRINT,
                "source_evaluator_fingerprint": PRE_RENAME_EVALUATOR_FINGERPRINT,
                "artifact_sha256": benchmark._file_sha256(artifact),
                "paper_sha256": benchmark._file_sha256(paper),
                "judge_model": tools.DEFAULT_BENCHMARK_JUDGE_MODEL,
                "overall_score_0_100": 80.0,
                "dimensions": [
                    {"id": dim, "score_0_10": 8.0, "metrics": {}}
                    for dim in benchmark.ALL_DIMS
                ],
                "findings": [],
            }), encoding="utf-8")

            record = benchmark._score_job(
                job,
                out_dir=root,
                model=None,
                force=False,
                force_vlm=False,
                retries=1,
            )
            normalized = json.loads(final_path.read_text(encoding="utf-8"))

        self.assertEqual(record["status"], "reaggregated")
        self.assertTrue(record["officially_eligible"])
        self.assertEqual(
            normalized["evaluator_fingerprint"],
            benchmark.BENCHMARK_EVALUATOR_FINGERPRINT,
        )

    def test_reaggregation_rewrites_pre_rename_fingerprint_to_current(self) -> None:
        report = {
            **CURRENT_FINAL_METADATA,
            "evaluator_fingerprint": PRE_RENAME_BENCHMARK_EVALUATOR_FINGERPRINT,
            "source_evaluator_fingerprint": PRE_RENAME_EVALUATOR_FINGERPRINT,
            "overall_score_0_100": 80.0,
            "dimensions": [
                {"id": dim, "score_0_10": 8.0, "metrics": {}}
                for dim in benchmark.ALL_DIMS
            ],
            "findings": [],
        }

        reaggregated = benchmark._reaggregate_final_report(report)

        self.assertEqual(
            reaggregated["evaluator_fingerprint"],
            benchmark.BENCHMARK_EVALUATOR_FINGERPRINT,
        )
        self.assertEqual(reaggregated["reaggregation_status"], "ok")

    def test_paper_text_cache_invalidates_when_paper_hash_changes(self) -> None:
        job = benchmark.CandidateJob(
            system="system",
            discipline="discipline",
            case="paper",
            paper=Path("paper.pdf"),
            artifact=Path("poster.png"),
            source_name="poster.png",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                benchmark,
                "_extract_paper_text",
                side_effect=["first paper", "revised paper"],
            ) as extract:
                first = benchmark._paper_text(job, Path(tmp), paper_sha256="hash-a")
                cached = benchmark._paper_text(job, Path(tmp), paper_sha256="hash-a")
                revised = benchmark._paper_text(job, Path(tmp), paper_sha256="hash-b")

        self.assertEqual(first, cached)
        self.assertEqual(revised, "revised paper")
        self.assertEqual(extract.call_count, 2)

    def test_paper_brief_excludes_absolute_path_from_vlm_input(self) -> None:
        job = benchmark.CandidateJob(
            system="system",
            discipline="discipline",
            case="paper",
            paper=Path("/private/location/paper.pdf"),
            artifact=Path("poster.png"),
            source_name="poster.png",
        )
        with tempfile.TemporaryDirectory() as tmp:
            brief = benchmark._paper_brief(
                job,
                "paper text",
                Path(tmp),
                paper_sha256="paper-hash",
            )

        self.assertNotIn("paper_path", brief)
        self.assertEqual(brief["paper_sha256"], "paper-hash")

    def test_vlm_benchmark_dry_run_uses_benchmark_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = root / "paper.pdf"
            document = fitz.open()
            document.new_page().insert_text((72, 72), "Paper text")
            document.save(paper)
            document.close()
            poster = root / "poster.png"
            Image.new("RGB", (200, 100), "white").save(poster)

            report = run_poster_benchmark(
                paper=paper,
                candidates=[PosterCandidate(name="candidate", artifact=poster)],
                out_dir=root / "out",
                pairwise=False,
                dry_run=True,
            )

        self.assertEqual(report["model"], DEFAULT_BENCHMARK_JUDGE_MODEL)

    def test_vlm_judge_defaults_to_benchmark_model_not_generation_critic(self) -> None:
        calls: list[tuple[object, str, str]] = []

        class FakeBackend:
            def vision_user_message(self, **kwargs: object) -> dict[str, object]:
                return kwargs

            def create_turn(self, **kwargs: object) -> SimpleNamespace:
                return SimpleNamespace(text='{"score_0_10": 7.5}')

        def fake_backend(settings: object, model: str, *, role: str) -> FakeBackend:
            calls.append((settings, model, role))
            return FakeBackend()

        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "poster.png"
            Image.new("RGB", (16, 8), "white").save(image)
            with (
                patch("autodesign.config.load_settings", return_value=SimpleNamespace(critic_model="gpt-5.4")),
                patch("autodesign.llm_backend.make_backend", side_effect=fake_backend),
            ):
                result = tools.tool_vlm_judge(
                    dimension="paper_coverage",
                    image=image,
                    paper_brief={},
                )

        self.assertEqual(calls[0][1:], (DEFAULT_BENCHMARK_JUDGE_MODEL, "critic"))
        self.assertEqual(result["model"], DEFAULT_BENCHMARK_JUDGE_MODEL)

    def test_vlm_call_limiter_spaces_calls_without_delaying_first_call(self) -> None:
        now = [10.0]
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        with (
            patch.object(benchmark.time, "monotonic", side_effect=lambda: now[0]),
            patch.object(benchmark.time, "sleep", side_effect=fake_sleep),
        ):
            limiter = benchmark._VLMCallLimiter(2.0)
            limiter.wait()
            limiter.wait()

        self.assertEqual(sleeps, [2.0])

    def test_vlm_dim_does_not_sleep_after_final_failed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "poster.png"
            Image.new("RGB", (20, 10), "white").save(image)
            with (
                patch.object(benchmark, "tool_vlm_judge", return_value={"status": "error"}),
                patch.object(benchmark.time, "sleep") as sleep,
            ):
                result = benchmark._run_vlm_dim(
                    dim="paper_coverage",
                    image=image,
                    brief={},
                    deterministic={},
                    cdir=Path(tmp),
                    model=None,
                    force=True,
                    retries=1,
                )

        self.assertEqual(result["status"], "error")
        sleep.assert_not_called()

    def test_judge_image_is_regenerated_when_source_pixels_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "poster.png"
            candidate_dir = root / "candidate"
            Image.new("RGB", (200, 100), "red").save(source)

            judge = _judge_image(source, candidate_dir)
            first_judge_hash = _file_sha256(judge)

            Image.new("RGB", (200, 100), "blue").save(source)
            judge = _judge_image(source, candidate_dir)
            second_judge_hash = _file_sha256(judge)
            metadata = json.loads((candidate_dir / "judge_input.meta.json").read_text())

            self.assertNotEqual(first_judge_hash, second_judge_hash)
            self.assertEqual(metadata["source_sha256"], _file_sha256(source))

    def test_vlm_cache_requires_current_prompt_fingerprint(self) -> None:
        self.assertFalse(benchmark._valid_vlm_result({
            "eval_protocol": EVAL_PROTOCOL,
            "vlm_prompt_fingerprint": "sha256:" + "0" * 64,
            "status": "ok",
            "score_0_10": 8.0,
        }))
        self.assertTrue(benchmark._valid_vlm_result({
            **CURRENT_VLM_METADATA,
            "status": "ok",
            "score_0_10": 8.0,
        }))

    def test_vlm_cache_requires_matching_judge_model(self) -> None:
        record = {
            **CURRENT_VLM_METADATA,
            "status": "ok",
            "score_0_10": 8.0,
            "model": "gpt-5.4",
        }
        self.assertFalse(benchmark._valid_vlm_result(record, judge_model=DEFAULT_BENCHMARK_JUDGE_MODEL))
        record["model"] = DEFAULT_BENCHMARK_JUDGE_MODEL
        self.assertTrue(benchmark._valid_vlm_result(record, judge_model=DEFAULT_BENCHMARK_JUDGE_MODEL))

    def test_vlm_cache_requires_matching_dimension_and_input_fingerprint(self) -> None:
        record = {
            **CURRENT_VLM_METADATA,
            "vlm_input_fingerprint": "sha256:" + "1" * 64,
            "dimension": "paper_coverage",
            "model": DEFAULT_BENCHMARK_JUDGE_MODEL,
            "status": "ok",
            "score_0_10": 7.0,
        }

        self.assertTrue(benchmark._valid_vlm_result(
            record,
            judge_model=DEFAULT_BENCHMARK_JUDGE_MODEL,
            dimension="paper_coverage",
            expected_input_fingerprint="sha256:" + "1" * 64,
        ))
        self.assertFalse(benchmark._valid_vlm_result(
            record,
            judge_model=DEFAULT_BENCHMARK_JUDGE_MODEL,
            dimension="source_faithfulness",
            expected_input_fingerprint="sha256:" + "1" * 64,
        ))
        self.assertFalse(benchmark._valid_vlm_result(
            record,
            judge_model=DEFAULT_BENCHMARK_JUDGE_MODEL,
            dimension="paper_coverage",
            expected_input_fingerprint="sha256:" + "2" * 64,
        ))

    def test_explicit_wrong_vlm_fingerprint_cannot_fall_back_to_legacy_rubric(self) -> None:
        record = {
            "rubric_version": "0.1.20",
            "vlm_prompt_fingerprint": "sha256:" + "0" * 64,
            "status": "ok",
            "score_0_10": 7.0,
            "model": DEFAULT_BENCHMARK_JUDGE_MODEL,
        }

        self.assertFalse(benchmark._valid_vlm_result(
            record,
            judge_model=DEFAULT_BENCHMARK_JUDGE_MODEL,
            legacy_rubric_versions={"0.1.20"},
        ))

    def test_vlm_cache_rejects_error_status_even_with_numeric_score(self) -> None:
        self.assertFalse(benchmark._valid_vlm_result({
            **CURRENT_VLM_METADATA,
            "status": "error",
            "score_0_10": 8.0,
        }))

    def test_complete_final_report_requires_bounded_scores_and_overall(self) -> None:
        base = {
            **CURRENT_FINAL_METADATA,
            "artifact_sha256": "artifact",
            "judge_model": DEFAULT_BENCHMARK_JUDGE_MODEL,
            "overall_score_0_100": 80.0,
            "dimensions": [
                {"id": dim, "score_0_10": 8.0}
                for dim in benchmark.ALL_DIMS
            ],
        }
        self.assertTrue(benchmark._complete_final_report(
            base,
            artifact_sha256="artifact",
            judge_model=DEFAULT_BENCHMARK_JUDGE_MODEL,
        ))
        invalid_score = {**base, "dimensions": [*base["dimensions"][:-1], {"id": benchmark.ALL_DIMS[-1], "score_0_10": 100.0}]}
        self.assertFalse(benchmark._complete_final_report(invalid_score))
        missing_overall = dict(base)
        missing_overall.pop("overall_score_0_100")
        self.assertFalse(benchmark._complete_final_report(missing_overall))
        inconsistent = {
            **base,
            "overall_score_0_100": 100.0,
            "dimensions": [
                {"id": dim, "score_0_10": 0.0}
                for dim in benchmark.ALL_DIMS
            ],
        }
        self.assertFalse(benchmark._complete_final_report(inconsistent))

        unsupported_low_overall = {**base, "overall_score_0_100": 0.0}
        self.assertFalse(benchmark._complete_final_report(unsupported_low_overall))

        supported_low_overall = {
            **base,
            "overall_score_0_100": 60.0,
            "findings": [{
                "id": "layout-coupled-score-cap",
                "evidence": {"overall_ceiling": 60.0},
            }],
        }
        self.assertTrue(benchmark._complete_final_report(supported_low_overall))

        unrelated_ceiling = {
            **base,
            "overall_score_0_100": 60.0,
            "findings": [{
                "id": "unrelated-advisory",
                "evidence": {"score_ceiling": 60.0},
            }],
        }
        self.assertFalse(benchmark._complete_final_report(unrelated_ceiling))

        removed_single_dimension_cap = {
            **base,
            "overall_score_0_100": 65.0,
            "dimensions": [
                {"id": dim, "score_0_10": 6.5}
                for dim in benchmark.ALL_DIMS
            ],
            "findings": [{
                "id": "judge-confirmed-serious-visual-defect",
                "evidence": {
                    "score_ceiling": 69.0,
                    "serious_dimensions": ["visual_evidence_use"],
                },
            }],
        }
        self.assertFalse(benchmark._complete_final_report(removed_single_dimension_cap))

        supported_multi_dimension_cap = {
            **base,
            "overall_score_0_100": 69.0,
            "findings": [{
                "id": "judge-confirmed-serious-visual-defect",
                "evidence": {
                    "score_ceiling": 69.0,
                    "serious_dimensions": [
                        "visual_evidence_use",
                        "layout_readability",
                    ],
                },
            }],
        }
        self.assertTrue(benchmark._complete_final_report(supported_multi_dimension_cap))

    def test_complete_final_report_binds_paper_content(self) -> None:
        report = {
            **CURRENT_FINAL_METADATA,
            "paper_sha256": "paper-a",
            "overall_score_0_100": 80.0,
            "dimensions": [
                {"id": dim, "score_0_10": 8.0}
                for dim in benchmark.ALL_DIMS
            ],
        }

        self.assertTrue(benchmark._complete_final_report(report, paper_sha256="paper-a"))
        self.assertFalse(benchmark._complete_final_report(report, paper_sha256="paper-b"))

    def test_complete_final_report_requires_compatible_vlm_prompt(self) -> None:
        report = {
            **CURRENT_FINAL_METADATA,
            "vlm_prompt_fingerprint": "sha256:" + "f" * 64,
            "overall_score_0_100": 80.0,
            "dimensions": [
                {"id": dim, "score_0_10": 8.0}
                for dim in benchmark.ALL_DIMS
            ],
        }

        self.assertFalse(benchmark._complete_final_report(report))

    def test_complete_final_report_requires_source_provenance(self) -> None:
        report = {
            **CURRENT_FINAL_METADATA,
            "overall_score_0_100": 80.0,
            "dimensions": [
                {"id": dim, "score_0_10": 8.0}
                for dim in benchmark.ALL_DIMS
            ],
        }
        report.pop("source_evaluator_fingerprint")

        self.assertFalse(benchmark._complete_final_report(report))

    def test_explicit_source_fingerprint_takes_precedence_over_legacy_marker(self) -> None:
        report = {
            **CURRENT_FINAL_METADATA,
            "legacy_source_rubric_version": "incompatible-old-marker",
            "overall_score_0_100": 80.0,
            "dimensions": [
                {"id": dim, "score_0_10": 8.0}
                for dim in benchmark.ALL_DIMS
            ],
        }

        self.assertTrue(benchmark._complete_final_report(report))

    def test_complete_final_report_rejects_degraded_legacy_reaggregation(self) -> None:
        report = {
            **CURRENT_FINAL_METADATA,
            "reaggregation_status": "degraded",
            "overall_score_0_100": 80.0,
            "dimensions": [
                {"id": dim, "score_0_10": 8.0}
                for dim in benchmark.ALL_DIMS
            ],
        }

        self.assertFalse(benchmark._complete_final_report(report))

    def test_noncompatible_legacy_report_reaggregates_as_degraded(self) -> None:
        report = {
            "rubric_version": "0.1.17",
            "overall_score_0_100": 80.0,
            "gate_triggered": False,
            "dimensions": [
                {"id": dim, "score_0_10": 8.0, "status": "ok", "metrics": {}}
                for dim in benchmark.ALL_DIMS
            ],
            "findings": [],
        }

        reaggregated = benchmark._reaggregate_final_report(report)

        self.assertEqual(reaggregated["legacy_source_rubric_version"], "0.1.17")
        self.assertEqual(reaggregated["reaggregation_status"], "degraded")
        self.assertFalse(benchmark._complete_final_report(reaggregated))

    def test_reaggregate_job_rejects_stale_artifact_and_paper_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "poster.png"
            paper = root / "paper.pdf"
            Image.new("RGB", (20, 10), "white").save(artifact)
            paper.write_bytes(b"current paper")
            job = benchmark.CandidateJob(
                system="anonymous-system",
                discipline="ai_ml_existing_20",
                case="paper-1",
                paper=paper,
                artifact=artifact,
                source_name="poster.png",
            )
            cdir = root / "candidates" / job.system / job.discipline / benchmark._safe_name(job.case)
            cdir.mkdir(parents=True)
            (cdir / "poster_quality_report.json").write_text(json.dumps({
                **CURRENT_FINAL_METADATA,
                "artifact_sha256": "stale-artifact",
                "paper_sha256": "stale-paper",
                "overall_score_0_100": 80.0,
                "dimensions": [
                    {"id": dim, "score_0_10": 8.0, "metrics": {}}
                    for dim in benchmark.ALL_DIMS
                ],
                "findings": [],
            }), encoding="utf-8")

            record = benchmark._reaggregate_job(job, out_dir=root)

        self.assertEqual(record["status"], "reaggregated_degraded")
        self.assertFalse(record["officially_eligible"])
        self.assertIsNone(record["overall"])

    def test_reaggregate_job_rejects_out_of_range_cached_scores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "poster.png"
            paper = root / "paper.pdf"
            Image.new("RGB", (20, 10), "white").save(artifact)
            paper.write_bytes(b"paper")
            job = benchmark.CandidateJob(
                system="anonymous-system",
                discipline="ai_ml_existing_20",
                case="paper-1",
                paper=paper,
                artifact=artifact,
                source_name="poster.png",
            )
            cdir = root / "candidates" / job.system / job.discipline / benchmark._safe_name(job.case)
            cdir.mkdir(parents=True)
            (cdir / "poster_quality_report.json").write_text(json.dumps({
                **CURRENT_FINAL_METADATA,
                "artifact_sha256": benchmark._file_sha256(artifact),
                "paper_sha256": benchmark._file_sha256(paper),
                "overall_score_0_100": 100.0,
                "gate_triggered": False,
                "dimensions": [
                    {"id": dim, "score_0_10": 100.0, "status": "ok", "metrics": {}}
                    for dim in benchmark.ALL_DIMS
                ],
                "findings": [],
            }), encoding="utf-8")

            record = benchmark._reaggregate_job(job, out_dir=root)

        self.assertEqual(record["status"], "reaggregated_degraded")
        self.assertIsNone(record["overall"])
        self.assertFalse(record["officially_eligible"])

    def test_explicit_incompatible_source_fingerprint_overrides_legacy_version(self) -> None:
        report = {
            "eval_protocol": EVAL_PROTOCOL,
            "evaluator_fingerprint": "sha256:" + "0" * 64,
            "rubric_version": "0.1.20",
            "dimensions": [
                {"id": dim, "score_0_10": 7.0, "metrics": {}}
                for dim in benchmark.ALL_DIMS
            ],
            "findings": [],
            "overall_score_0_100": 70.0,
        }

        reaggregated = benchmark._reaggregate_final_report(report)

        self.assertEqual(reaggregated["reaggregation_status"], "degraded")
        self.assertEqual(
            reaggregated["source_evaluator_fingerprint"],
            "sha256:" + "0" * 64,
        )

    def test_legacy_prompt_cache_compatibility_is_read_only(self) -> None:
        for dimension in benchmark.VLM_DIMS:
            with self.subTest(dimension=dimension):
                expected = {"0.1.19", "0.1.20"}
                if dimension in {"source_faithfulness", "paper_coverage"}:
                    expected.update({"0.1.14", "0.1.15", "0.1.16", "0.1.17", "0.1.18"})
                self.assertEqual(
                    benchmark._legacy_compatible_vlm_rubric_versions(dimension),
                    expected,
                )

    def test_complete_final_report_requires_current_presentation_viability_cap(self) -> None:
        dimensions = [
            {
                "id": dim,
                "score_0_10": {
                    "information_density_and_synthesis": 5.5,
                    "layout_readability": 6.0,
                    "professional_aesthetics": 6.0,
                }.get(dim, 8.0),
            }
            for dim in benchmark.ALL_DIMS
        ]
        stale = {
            **CURRENT_FINAL_METADATA,
            "overall_score_0_100": 69.25,
            "dimensions": dimensions,
            "findings": [],
        }
        current = {
            **stale,
            "overall_score_0_100": 67.5,
            "findings": [{
                "id": "presentation-viability-score-cap",
                "evidence": {"score_ceiling": 67.5},
            }],
        }

        self.assertFalse(benchmark._complete_final_report(stale))
        self.assertTrue(benchmark._complete_final_report(current))

    def test_all_vlm_dimensions_reuse_prior_arithmetic_only_rubric_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cdir = Path(tmp)
            Image.new("RGB", (20, 10), "white").save(cdir / "poster.png")
            path = cdir / "vlm" / "paper_coverage.json"
            path.parent.mkdir(parents=True)
            cached = {
                "rubric_version": "0.1.17",
                "status": "ok",
                "score_0_10": 7.5,
                "model": DEFAULT_BENCHMARK_JUDGE_MODEL,
            }
            path.write_text(json.dumps(cached), encoding="utf-8")
            with patch.object(benchmark, "tool_vlm_judge") as judge:
                result = benchmark._run_vlm_dim(
                    dim="paper_coverage",
                    image=cdir / "poster.png",
                    brief={},
                    deterministic={},
                    cdir=cdir,
                    model=None,
                    force=False,
                    retries=1,
                )

        self.assertEqual(result, cached)
        judge.assert_not_called()

    def test_recalibrated_vlm_dimension_rejects_legacy_prompt_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cdir = Path(tmp)
            Image.new("RGB", (20, 10), "white").save(cdir / "poster.png")
            path = cdir / "vlm" / "professional_aesthetics.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "rubric_version": "0.1.15",
                "status": "ok",
                "score_0_10": 8.5,
                "model": DEFAULT_BENCHMARK_JUDGE_MODEL,
            }), encoding="utf-8")
            with patch.object(benchmark, "tool_vlm_judge", return_value={
                "status": "ok",
                "score_0_10": 6.5,
                "model": DEFAULT_BENCHMARK_JUDGE_MODEL,
            }) as judge:
                result = benchmark._run_vlm_dim(
                    dim="professional_aesthetics",
                    image=cdir / "poster.png",
                    brief={},
                    deterministic={},
                    cdir=cdir,
                    model=None,
                    force=False,
                    retries=1,
                )

        self.assertEqual(result["score_0_10"], 6.5)
        self.assertEqual(result["eval_protocol"], EVAL_PROTOCOL)
        self.assertEqual(result["vlm_prompt_fingerprint"], VLM_PROMPT_FINGERPRINT)
        self.assertNotIn("rubric_version", result)
        judge.assert_called_once()

    def test_vlm_dispatch_forwards_all_metric_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "deterministic_report.json"
            metric_bundles = {
                "visual_evidence": {"available": True},
                "paper_body_screenshot": {"severity_level": "severe"},
                "basic_layout_integrity": {"available": True},
            }
            report.write_text(json.dumps({"metric_bundles": metric_bundles}), encoding="utf-8")
            args = argparse.Namespace(
                tool="vlm_judge",
                paper_brief=None,
                report=report,
                dimension="visual_evidence_use",
                image=Path(tmp) / "poster.png",
                profile=None,
                model=None,
                dry_run=True,
            )
            with patch.object(tools, "tool_vlm_judge", return_value={"status": "ok"}) as judge:
                tools._dispatch(args)

            self.assertEqual(judge.call_args.kwargs["grounding"], metric_bundles)

    def test_vlm_dim_forwards_density_component_to_aesthetics_grounding(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_judge(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {
                "status": "ok",
                "score_0_10": 8.5,
                "model": DEFAULT_BENCHMARK_JUDGE_MODEL,
            }

        deterministic = {
            "metric_bundles": {
                "basic_layout_integrity": {"available": True, "score_0_10": 9.0},
            },
            "dimension_components": {
                "information_density_and_synthesis": {"score_0_10": 5.9, "status": "ok"},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "poster.png"
            Image.new("RGB", (20, 10), "white").save(image)
            with patch.object(benchmark, "tool_vlm_judge", side_effect=fake_judge):
                benchmark._run_vlm_dim(
                    dim="professional_aesthetics",
                    image=image,
                    brief={},
                    deterministic=deterministic,
                    cdir=Path(tmp),
                    model=None,
                    force=True,
                    retries=1,
                )

        grounding = calls[0]["grounding"]
        self.assertIsInstance(grounding, dict)
        self.assertEqual(
            grounding["dimension_components"]["information_density_and_synthesis"]["score_0_10"],
            5.9,
        )

    def test_official_rubric_weights_are_canonical(self) -> None:
        weights = {dim.id: dim.weight for dim in DIMENSIONS}

        self.assertEqual(
            weights,
            {
                "source_faithfulness": 10.0,
                "paper_coverage": 10.0,
                "information_density_and_synthesis": 15.0,
                "visual_evidence_use": 10.0,
                "basic_layout_integrity": 20.0,
                "layout_readability": 25.0,
                "professional_aesthetics": 10.0,
            },
        )
        self.assertEqual(sum(weights.values()), 100.0)

    def test_final_protocol_reuses_compatible_legacy_vlm_results(self) -> None:
        self.assertEqual(benchmark.EVAL_PROTOCOL, "posterbench-final")
        for dimension in benchmark.VLM_DIMS:
            with self.subTest(dimension=dimension):
                self.assertIn(
                    "0.1.19",
                    benchmark._legacy_compatible_vlm_rubric_versions(dimension),
                )

    def test_layout_p1_failure_caps_high_judge_scores(self) -> None:
        deterministic = {
            "dimension_components": {
                "information_density_and_synthesis": {"score_0_10": 8.96, "status": "ok", "metrics": {}},
                "basic_layout_integrity": {"score_0_10": 4.4, "status": "warning", "metrics": {}},
            },
            "findings": [
                {
                    "id": "basic-layout-bottom-truncation",
                    "severity": "P1",
                    "dimension": "basic_layout_integrity",
                    "message": "Bottom content appears truncated.",
                    "evidence": {
                        "confidence": "high",
                        "boundary_source": "canvas",
                        "trusted_p1": True,
                    },
                },
                {
                    "id": "basic-layout-visual-crop-damage",
                    "severity": "P2",
                    "dimension": "basic_layout_integrity",
                    "message": "A visual asset is visibly cropped.",
                    "evidence": {},
                },
            ],
            "gate": {"triggered": False, "ceiling": 40.0},
        }
        judge_report = {
            "dimension_scores": {
                "source_faithfulness": {"score_0_10": 10.0},
                "paper_coverage": {"score_0_10": 10.0},
                "visual_evidence_use": {"score_0_10": 10.0},
                "layout_readability": {"score_0_10": 8.45},
                "professional_aesthetics": {"score_0_10": 8.5},
            }
        }

        final = aggregate_final(
            deterministic,
            judge_report,
            mode="benchmark",
            candidate_name="trusted-layout-damage-control",
            artifact="poster.png",
            paper="paper.pdf",
        ).to_dict()
        dimensions = {dim["id"]: dim for dim in final["dimensions"]}

        self.assertEqual(final["overall_score_0_100"], 60.0)
        self.assertEqual(dimensions["visual_evidence_use"]["score_0_10"], 5.0)
        self.assertEqual(dimensions["layout_readability"]["score_0_10"], 6.0)
        self.assertEqual(dimensions["professional_aesthetics"]["score_0_10"], 6.0)
        self.assertTrue(any(finding["id"] == "layout-coupled-score-cap" for finding in final["findings"]))

    def test_low_confidence_inferred_section_p1_does_not_cap_other_dimensions(self) -> None:
        deterministic = {
            "dimension_components": {
                "information_density_and_synthesis": {"score_0_10": 9.0, "status": "ok", "metrics": {}},
                "basic_layout_integrity": {"score_0_10": 4.4, "status": "warning", "metrics": {}},
            },
            "findings": [
                {
                    "id": "basic-layout-section-content-overflow",
                    "severity": "P1",
                    "dimension": "basic_layout_integrity",
                    "message": "Inferred section crossing.",
                    "evidence": {
                        "confidence": "low",
                        "source": "inferred",
                        "trusted_p1": False,
                    },
                },
            ],
            "gate": {"triggered": False, "ceiling": 40.0},
        }
        judge_report = {
            "dimension_scores": {
                "source_faithfulness": {"score_0_10": 9.0},
                "paper_coverage": {"score_0_10": 9.0},
                "visual_evidence_use": {"score_0_10": 9.0},
                "layout_readability": {"score_0_10": 9.0},
                "professional_aesthetics": {"score_0_10": 9.0},
            }
        }

        final = aggregate_final(
            deterministic,
            judge_report,
            mode="benchmark",
            candidate_name="open-grid-control",
            artifact="poster.png",
            paper="paper.pdf",
        ).to_dict()
        dimensions = {dim["id"]: dim for dim in final["dimensions"]}

        self.assertEqual(dimensions["visual_evidence_use"]["score_0_10"], 9.0)
        self.assertEqual(dimensions["layout_readability"]["score_0_10"], 9.0)
        self.assertEqual(dimensions["professional_aesthetics"]["score_0_10"], 9.0)
        self.assertGreater(final["overall_score_0_100"], 68.0)
        self.assertFalse(any(finding["id"] == "layout-coupled-score-cap" for finding in final["findings"]))

    def test_inferred_penalty_cannot_escalate_a_trusted_p1_overall_ceiling(self) -> None:
        deterministic = {
            "dimension_components": {
                "information_density_and_synthesis": {"score_0_10": 9.0, "status": "ok", "metrics": {}},
                "basic_layout_integrity": {"score_0_10": 4.4, "status": "warning", "metrics": {}},
            },
            "findings": [
                {
                    "id": "basic-layout-canvas-overflow",
                    "severity": "P1",
                    "dimension": "basic_layout_integrity",
                    "penalty": 1.0,
                    "evidence": {"trusted_p1": True, "boundary_source": "canvas", "confidence": "high"},
                },
                {
                    "id": "basic-layout-section-content-overflow",
                    "severity": "P2",
                    "dimension": "basic_layout_integrity",
                    "penalty": 2.0,
                    "evidence": {"trusted_p1": False, "boundary_source": "inferred_open_grid", "confidence": "medium"},
                },
            ],
            "gate": {"triggered": False, "ceiling": 40.0},
        }
        judge_report = {
            "dimension_scores": {
                "source_faithfulness": {"score_0_10": 9.0},
                "paper_coverage": {"score_0_10": 9.0},
                "visual_evidence_use": {"score_0_10": 9.0},
                "layout_readability": {"score_0_10": 9.0},
                "professional_aesthetics": {"score_0_10": 9.0},
            }
        }

        final = aggregate_final(
            deterministic,
            judge_report,
            mode="benchmark",
            candidate_name="trusted-plus-inferred",
            artifact="poster.png",
            paper="paper.pdf",
        ).to_dict()

        self.assertGreater(final["overall_score_0_100"], 68.0)
        cap = next(f for f in final["findings"] if f["id"] == "layout-coupled-score-cap")
        self.assertIsNone(cap["evidence"]["overall_ceiling"])

    def test_poster_scale_legibility_caps_only_layout_readability(self) -> None:
        deterministic = {
            "dimension_components": {
                "information_density_and_synthesis": {"score_0_10": 9.0, "status": "ok", "metrics": {}},
                "basic_layout_integrity": {
                    "score_0_10": 9.0,
                    "status": "ok",
                    "metrics": {"median_body_text_height_ref_px": 18.5},
                },
            },
            "findings": [],
            "gate": {"triggered": False, "ceiling": 40.0},
        }
        judge_report = {
            "dimension_scores": {
                "source_faithfulness": {"score_0_10": 9.0},
                "paper_coverage": {"score_0_10": 9.0},
                "visual_evidence_use": {"score_0_10": 9.0},
                "layout_readability": {"score_0_10": 9.0},
                "professional_aesthetics": {"score_0_10": 9.0},
            }
        }

        final = aggregate_final(
            deterministic,
            judge_report,
            mode="benchmark",
            candidate_name="small-body-type",
            artifact="poster.png",
            paper="paper.pdf",
        ).to_dict()
        dimensions = {dim["id"]: dim for dim in final["dimensions"]}

        self.assertEqual(dimensions["layout_readability"]["score_0_10"], 7.0)
        self.assertEqual(dimensions["visual_evidence_use"]["score_0_10"], 9.0)
        self.assertEqual(dimensions["professional_aesthetics"]["score_0_10"], 9.0)
        self.assertEqual(
            dimensions["layout_readability"]["metrics"]["poster_scale_legibility_cap"]["score_ceiling"],
            7.0,
        )

    def test_low_density_caps_generic_clean_aesthetics(self) -> None:
        deterministic = {
            "dimension_components": {
                "information_density_and_synthesis": {"score_0_10": 5.91, "status": "ok", "metrics": {}},
                "basic_layout_integrity": {"score_0_10": 9.1, "status": "ok", "metrics": {}},
            },
            "findings": [],
            "gate": {"triggered": False, "ceiling": 40.0},
        }
        judge_report = {
            "dimension_scores": {
                "source_faithfulness": {"score_0_10": 10.0},
                "paper_coverage": {"score_0_10": 10.0},
                "visual_evidence_use": {"score_0_10": 9.0},
                "layout_readability": {"score_0_10": 7.36},
                "professional_aesthetics": {"score_0_10": 8.5},
            }
        }

        final = aggregate_final(
            deterministic,
            judge_report,
            mode="benchmark",
            candidate_name="low-density-clean-layout-control",
            artifact="poster.png",
            paper="paper.pdf",
        ).to_dict()
        dimensions = {dim["id"]: dim for dim in final["dimensions"]}

        self.assertEqual(dimensions["professional_aesthetics"]["score_0_10"], 6.0)
        self.assertTrue(any(finding["id"] == "academic-poster-aesthetics-density-cap" for finding in final["findings"]))

    def test_reaggregate_final_report_applies_layout_cap(self) -> None:
        final = {
            **CURRENT_FINAL_METADATA,
            "mode": "benchmark",
            "candidate_name": "cached-trusted-layout-damage-control",
            "artifact": "poster.png",
            "paper": "paper.pdf",
            "gate_triggered": False,
            "gate_ceiling": None,
            "dimensions": [
                {"id": "source_faithfulness", "score_0_10": 10.0, "status": "ok"},
                {"id": "paper_coverage", "score_0_10": 10.0, "status": "ok"},
                {"id": "information_density_and_synthesis", "score_0_10": 8.96, "status": "ok"},
                {"id": "visual_evidence_use", "score_0_10": 10.0, "status": "ok"},
                {"id": "basic_layout_integrity", "score_0_10": 4.4, "status": "warning"},
                {"id": "layout_readability", "score_0_10": 8.45, "status": "ok"},
                {"id": "professional_aesthetics", "score_0_10": 8.5, "status": "ok"},
            ],
            "findings": [
                {
                    "id": "basic-layout-bottom-truncation",
                    "severity": "P1",
                    "dimension": "basic_layout_integrity",
                    "message": "Bottom content appears truncated.",
                    "evidence": {
                        "confidence": "high",
                        "boundary_source": "canvas",
                        "trusted_p1": True,
                    },
                },
                {
                    "id": "basic-layout-visual-crop-damage",
                    "severity": "P2",
                    "dimension": "basic_layout_integrity",
                    "message": "A visual asset is visibly cropped.",
                    "evidence": {},
                },
            ],
        }

        reaggregated = benchmark._reaggregate_final_report(final)
        dimensions = {dim["id"]: dim for dim in reaggregated["dimensions"]}

        self.assertEqual(reaggregated["overall_score_0_100"], 60.0)
        self.assertEqual(dimensions["visual_evidence_use"]["score_0_10"], 5.0)
        self.assertEqual(dimensions["layout_readability"]["score_0_10"], 6.0)
        self.assertEqual(dimensions["professional_aesthetics"]["score_0_10"], 6.0)
        self.assertEqual(reaggregated["finding_counts"]["P1"], 2)

        second = benchmark._reaggregate_final_report(reaggregated)
        self.assertEqual(second["overall_score_0_100"], reaggregated["overall_score_0_100"])
        self.assertEqual(second["finding_counts"], reaggregated["finding_counts"])
        self.assertTrue(any(f["id"] == "layout-coupled-score-cap" for f in second["findings"]))

    def test_reaggregate_recomputes_viability_and_preserves_stricter_major_ceiling(self) -> None:
        final = {
            **CURRENT_FINAL_METADATA,
            "mode": "benchmark",
            "gate_triggered": False,
            "dimensions": [
                {
                    "id": dim,
                    "score_0_10": {
                        "information_density_and_synthesis": 5.5,
                        "basic_layout_integrity": 4.4,
                        "layout_readability": 6.0,
                        "professional_aesthetics": 6.0,
                    }.get(dim, 8.0),
                    "status": "warning" if dim in {"layout_readability", "professional_aesthetics"} else "ok",
                    "metrics": {},
                }
                for dim in benchmark.ALL_DIMS
            ],
            "findings": [
                {
                    "id": "basic-layout-bottom-truncation",
                    "severity": "P1",
                    "dimension": "basic_layout_integrity",
                    "evidence": {"trusted_p1": True, "boundary_source": "canvas", "confidence": "high"},
                },
                {
                    "id": "layout-coupled-score-cap",
                    "severity": "P1",
                    "dimension": "basic_layout_integrity",
                    "evidence": {"overall_ceiling": 60.0, "capped_dimensions": [
                        {"dimension": "layout_readability", "original_score_0_10": 8.0},
                        {"dimension": "professional_aesthetics", "original_score_0_10": 8.0},
                    ]},
                },
                {
                    "id": "presentation-viability-score-cap",
                    "severity": "P1",
                    "dimension": "layout_readability",
                    "evidence": {"presentation_viability": 5.0, "score_ceiling": 60.0},
                },
                {
                    "id": "judge-confirmed-serious-visual-defect",
                    "severity": "P1",
                    "dimension": "visual_evidence_use",
                    "evidence": {
                        "score_ceiling": 69.0,
                        "serious_dimensions": ["visual_evidence_use"],
                    },
                },
                {
                    "id": "deterministic-major-visual-failure",
                    "severity": "P1",
                    "dimension": "basic_layout_integrity",
                    "evidence": {"score_ceiling": 55.0},
                },
                {
                    "id": "judge-confirmed-major-visual-failure",
                    "severity": "P1",
                    "dimension": "layout_readability",
                    "evidence": {"score_ceiling": 58.0},
                },
            ],
        }

        reaggregated = benchmark._reaggregate_final_report(final)
        finding_ids = [finding["id"] for finding in reaggregated["findings"]]
        dimensions = {dim["id"]: dim for dim in reaggregated["dimensions"]}

        self.assertEqual(reaggregated["overall_score_0_100"], 55.0)
        self.assertEqual(dimensions["layout_readability"]["score_0_10"], 6.0)
        self.assertEqual(dimensions["professional_aesthetics"]["score_0_10"], 6.0)
        self.assertEqual(finding_ids.count("presentation-viability-score-cap"), 1)
        self.assertNotIn("judge-confirmed-serious-visual-defect", finding_ids)
        viability = next(
            finding for finding in reaggregated["findings"]
            if finding["id"] == "presentation-viability-score-cap"
        )
        self.assertEqual(viability["evidence"]["presentation_viability"], 5.75)
        self.assertEqual(viability["evidence"]["score_ceiling"], 67.5)

        second = benchmark._reaggregate_final_report(reaggregated)
        self.assertEqual(second["overall_score_0_100"], reaggregated["overall_score_0_100"])
        self.assertEqual(second["dimensions"], reaggregated["dimensions"])
        self.assertEqual(second["findings"], reaggregated["findings"])

    def test_reaggregate_preserves_correlated_multi_dimension_pass_ceiling(self) -> None:
        final = {
            **CURRENT_FINAL_METADATA,
            "gate_triggered": False,
            "dimensions": [
                {"id": dim, "score_0_10": 8.0, "status": "ok", "metrics": {}}
                for dim in benchmark.ALL_DIMS
            ],
            "findings": [{
                "id": "judge-confirmed-serious-visual-defect",
                "severity": "P1",
                "dimension": "visual_evidence_use",
                "evidence": {
                    "score_ceiling": 69.0,
                    "serious_dimensions": [
                        "visual_evidence_use",
                        "layout_readability",
                    ],
                },
            }],
        }

        reaggregated = benchmark._reaggregate_final_report(final)

        self.assertEqual(reaggregated["overall_score_0_100"], 69.0)
        self.assertEqual(
            sum(
                finding["id"] == "judge-confirmed-serious-visual-defect"
                for finding in reaggregated["findings"]
            ),
            1,
        )
        self.assertTrue(benchmark._complete_final_report(reaggregated))

    def test_reaggregate_removes_obsolete_dimension_cap_metadata_and_restores_score(self) -> None:
        final = {
            **CURRENT_FINAL_METADATA,
            "gate_triggered": False,
            "dimensions": [
                {
                    "id": dim,
                    "score_0_10": 6.0 if dim == "layout_readability" else 8.0,
                    "status": "warning" if dim == "layout_readability" else "ok",
                    "metrics": ({"layout_coupled_cap": {"score_ceiling": 6.0}}
                                if dim == "layout_readability" else {}),
                }
                for dim in benchmark.ALL_DIMS
            ],
            "findings": [{
                "id": "layout-coupled-score-cap",
                "severity": "P2",
                "dimension": "basic_layout_integrity",
                "evidence": {"capped_dimensions": [{
                    "dimension": "layout_readability",
                    "original_score_0_10": 8.0,
                    "capped_score_0_10": 6.0,
                }]},
            }],
        }

        reaggregated = benchmark._reaggregate_final_report(final)
        readability = next(
            dim for dim in reaggregated["dimensions"]
            if dim["id"] == "layout_readability"
        )

        self.assertEqual(readability["score_0_10"], 8.0)
        self.assertNotIn("layout_coupled_cap", readability["metrics"])
        self.assertNotIn(
            "layout-coupled-score-cap",
            [finding["id"] for finding in reaggregated["findings"]],
        )

    def test_reaggregate_preserves_zero_gate_ceiling(self) -> None:
        final = {
            **CURRENT_FINAL_METADATA,
            "mode": "benchmark",
            "candidate_name": "catastrophic-gate",
            "artifact": "poster.png",
            "paper": "paper.pdf",
            "gate_triggered": True,
            "gate_ceiling": 0.0,
            "dimensions": [
                {"id": dim, "score_0_10": 8.0, "status": "ok"}
                for dim in benchmark.ALL_DIMS
            ],
            "findings": [],
        }

        reaggregated = benchmark._reaggregate_final_report(final)

        self.assertEqual(reaggregated["gate_ceiling"], 0.0)
        self.assertEqual(reaggregated["overall_score_0_100"], 0.0)

    def test_legacy_reaggregate_is_explicitly_degraded_without_inventing_trust(self) -> None:
        final = {
            "rubric_version": "0.1.15",
            "mode": "benchmark",
            "gate_triggered": False,
            "dimensions": [
                {
                    "id": dim,
                    "score_0_10": 4.4 if dim == "basic_layout_integrity" else 8.0,
                    "status": "ok",
                }
                for dim in benchmark.ALL_DIMS
            ],
            "findings": [{
                "id": "basic-layout-bottom-truncation",
                "severity": "P1",
                "dimension": "basic_layout_integrity",
                "evidence": {"true_bottom_touch_count": 2},
            }],
        }

        reaggregated = benchmark._reaggregate_final_report(final)

        self.assertEqual(reaggregated["legacy_source_rubric_version"], "0.1.15")
        self.assertEqual(reaggregated["reaggregation_status"], "degraded")
        self.assertGreater(reaggregated["overall_score_0_100"], 68.0)
        self.assertFalse(any(f["id"] == "layout-coupled-score-cap" for f in reaggregated["findings"]))

    def test_force_selected_aesthetics_dimension_does_not_force_batch_judge(self) -> None:
        args = SimpleNamespace(
            force_vlm=False,
            force_vlm_dims={"professional_aesthetics"},
        )

        self.assertFalse(benchmark._force_batch_style_judge(args))
        args.force_vlm = True
        self.assertTrue(benchmark._force_batch_style_judge(args))

    def test_reaggregate_final_report_applies_low_density_aesthetics_cap(self) -> None:
        final = {
            "rubric_version": "old",
            "mode": "benchmark",
            "candidate_name": "cached-native-clean-digest-like",
            "artifact": "poster.png",
            "paper": "paper.pdf",
            "gate_triggered": False,
            "gate_ceiling": None,
            "dimensions": [
                {"id": "source_faithfulness", "score_0_10": 10.0, "status": "ok"},
                {"id": "paper_coverage", "score_0_10": 10.0, "status": "ok"},
                {"id": "information_density_and_synthesis", "score_0_10": 6.17, "status": "ok"},
                {"id": "visual_evidence_use", "score_0_10": 9.0, "status": "ok"},
                {"id": "basic_layout_integrity", "score_0_10": 9.1, "status": "ok"},
                {"id": "layout_readability", "score_0_10": 7.36, "status": "ok"},
                {"id": "professional_aesthetics", "score_0_10": 8.5, "status": "ok"},
            ],
            "findings": [],
        }

        reaggregated = benchmark._reaggregate_final_report(final)
        dimensions = {dim["id"]: dim for dim in reaggregated["dimensions"]}

        self.assertEqual(dimensions["professional_aesthetics"]["score_0_10"], 6.5)
        self.assertEqual(reaggregated["finding_counts"]["P2"], 1)
        self.assertTrue(any(finding["id"] == "academic-poster-aesthetics-density-cap" for finding in reaggregated["findings"]))

    def test_professional_aesthetics_prompt_penalizes_excessive_palette(self) -> None:
        checklist = tools._DEFECT_CHECKLISTS["professional_aesthetics"]
        notes = tools._DIMENSION_SCORING_NOTES["professional_aesthetics"]

        self.assertIn("too many colors", checklist)
        self.assertIn("human-made conference poster", notes)
        self.assertIn("overly many accent colors", notes)

    def test_force_vlm_dims_accepts_only_supported_subjective_dimensions(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "run_poster_benchmark_main_table.py",
                "--force-vlm-dims",
                "visual_evidence_use,layout_readability,professional_aesthetics",
            ],
        ):
            args = benchmark.parse_args()

        self.assertEqual(
            args.force_vlm_dims,
            {"visual_evidence_use", "layout_readability", "professional_aesthetics"},
        )

    def test_batch_style_adjustment_changes_only_benchmark_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "poster_quality_report.json"
            original_report = {"professional_aesthetics": 8.0, "overall": 80.0}
            report_path.write_text(json.dumps(original_report), encoding="utf-8")
            record = {
                "system": "anonymous-system",
                "system_label": "Anonymous system",
                "case": "paper-1",
                "overall": 70.5,
                "verdict": "pass",
                "gate_triggered": False,
                "dimensions": {
                    **{dim: 7.0 for dim in benchmark.ALL_DIMS},
                    "professional_aesthetics": 7.5,
                },
                "report_path": str(report_path),
            }
            batch_result = {
                "status": "ok",
                "style_adaptability_score_0_10": 4.5,
                "adjustment_points": -1.0,
                "explanation": "Repeated skeleton.",
            }

            adjusted = benchmark._apply_batch_style_result(record, batch_result)

            self.assertEqual(adjusted["raw_professional_aesthetics"], 7.5)
            self.assertEqual(adjusted["dimensions"]["professional_aesthetics"], 6.5)
            self.assertEqual(adjusted["adjusted_professional_aesthetics"], 6.5)
            self.assertEqual(adjusted["style_adaptability"], 4.5)
            self.assertEqual(adjusted["homogeneity_adjustment"], -1.0)
            self.assertEqual(adjusted["overall"], 69.5)
            self.assertEqual(adjusted["verdict"], "revise")
            self.assertEqual(record["dimensions"]["professional_aesthetics"], 7.5)
            self.assertEqual(record["verdict"], "pass")
            self.assertEqual(json.loads(report_path.read_text()), original_report)

    def test_degraded_batch_result_does_not_upgrade_existing_verdict(self) -> None:
        record = {
            "overall": 75.0,
            "verdict": "revise",
            "gate_triggered": False,
            "dimensions": {dim: 7.5 for dim in benchmark.ALL_DIMS},
        }

        adjusted = benchmark._apply_batch_style_result(record, {
            "status": "degraded",
            "adjustment_points": 0.0,
        })

        self.assertIsNone(adjusted["overall"])
        self.assertEqual(adjusted["diagnostic_overall"], 75.0)
        self.assertFalse(adjusted["officially_eligible"])
        self.assertEqual(adjusted["status"], "batch_style_degraded")
        self.assertEqual(adjusted["verdict"], "revise")

    def test_batch_postprocessing_does_not_resurrect_incomplete_record(self) -> None:
        record = {
            "overall": None,
            "diagnostic_overall": 70.0,
            "officially_eligible": False,
            "status": "incomplete",
            "dimensions": {dim: 8.0 for dim in benchmark.ALL_DIMS[:-1]},
        }

        adjusted = benchmark._apply_batch_style_result(record, {
            "status": "ok",
            "adjustment_points": -0.5,
        })

        self.assertIsNone(adjusted["overall"])
        self.assertFalse(adjusted["officially_eligible"])

    def test_batch_style_adjustment_does_not_create_a_new_single_poster_viability_cap(self) -> None:
        dimensions = {dim: 8.0 for dim in benchmark.ALL_DIMS}
        dimensions.update({
            "information_density_and_synthesis": 5.9,
            "layout_readability": 6.2,
            "professional_aesthetics": 7.5,
        })
        viability = benchmark._presentation_viability_record_fields(dimensions)
        self.assertFalse(viability["presentation_viability_triggered"])
        record = {
            "overall": 75.0,
            "verdict": "pass",
            "gate_triggered": False,
            "dimensions": dimensions,
            **viability,
        }

        adjusted = benchmark._apply_batch_style_result(record, {
            "status": "ok",
            "style_adaptability_score_0_10": 2.0,
            "adjustment_points": -1.5,
        })

        self.assertEqual(adjusted["presentation_viability"], viability["presentation_viability"])
        self.assertFalse(adjusted["presentation_viability_triggered"])
        self.assertIsNone(adjusted["presentation_viability_ceiling"])
        self.assertEqual(adjusted["overall"], 70.35)

    def test_aggregate_tolerates_non_numeric_trusted_confidence(self) -> None:
        records = [{
            "system": "anonymous-system",
            "overall": 70.0,
            "dimensions": {dim: 7.0 for dim in benchmark.ALL_DIMS},
            "trusted_layout_p1_confidence": "medium",
        }]

        row = benchmark._aggregate_one("anonymous-system", "all", records)

        self.assertEqual(row["trusted_layout_p1_count"], 0)
        self.assertEqual(row["trusted_layout_p1_rate"], 0.0)

    def test_official_aggregate_excludes_degraded_and_incomplete_records(self) -> None:
        base = {
            "system": "anonymous-system",
            "discipline": "all",
            "dimensions": {dim: 8.0 for dim in benchmark.ALL_DIMS},
            "verdict": "pass",
            "gate_triggered": False,
        }
        records = [
            {**base, "status": "scored", "overall": 80.0, "officially_eligible": True},
            {**base, "status": "reaggregated_degraded", "overall": 99.0, "officially_eligible": False},
            {**base, "status": "incomplete", "overall": 95.0, "officially_eligible": False},
        ]

        row = benchmark._aggregate_one("anonymous-system", "all", records)

        self.assertEqual(row["n"], 1)
        self.assertEqual(row["overall"], 80.0)

    def test_presentation_viability_explainability_flows_to_record_aggregate_and_csv(self) -> None:
        final = {
            **CURRENT_FINAL_METADATA,
            "overall_score_0_100": 67.5,
            "verdict": "revise",
            "gate_triggered": False,
            "dimensions": [
                {
                    "id": dim,
                    "score_0_10": {
                        "information_density_and_synthesis": 5.5,
                        "layout_readability": 6.0,
                        "professional_aesthetics": 6.0,
                    }.get(dim, 8.0),
                }
                for dim in benchmark.ALL_DIMS
            ],
            "findings": [{
                "id": "presentation-viability-score-cap",
                "severity": "P1",
                "dimension": "layout_readability",
                "evidence": {"presentation_viability": 5.75, "score_ceiling": 67.5},
            }],
        }
        job = benchmark.CandidateJob(
            system="anonymous-system",
            discipline="ai_ml_existing_20",
            case="paper-1",
            paper=None,
            artifact=None,
            source_name="poster.png",
            status="ready",
        )

        record = benchmark._record_from_final(job, final, "cached")
        row = benchmark._aggregate_one("anonymous-system", "all", [record])

        self.assertEqual(record["presentation_viability"], 5.75)
        self.assertTrue(record["presentation_viability_triggered"])
        self.assertEqual(record["presentation_viability_ceiling"], 67.5)
        self.assertEqual(record["presentation_viability_weak_dimensions"], ["information_density_and_synthesis"])
        self.assertEqual(row["presentation_viability_mean"], 5.75)
        self.assertEqual(row["presentation_viability_trigger_count"], 1)
        self.assertEqual(row["presentation_viability_trigger_rate"], 1.0)
        self.assertEqual(row["presentation_viability_ceiling"], 67.5)
        self.assertEqual(row["presentation_viability_weak_dimensions"], ["information_density_and_synthesis"])

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scores.csv"
            benchmark._write_scores_csv(csv_path, [record])
            header = csv_path.read_text(encoding="utf-8").splitlines()[0]
        self.assertIn("presentation_viability", header)
        self.assertIn("presentation_viability_triggered", header)
        self.assertIn("presentation_viability_ceiling", header)
        self.assertIn("presentation_viability_weak_dimensions", header)
        self.assertIn("batch_style_fingerprint", header)
        self.assertIn("batch_style_judge_model", header)
        self.assertIn("batch_style_cache_status", header)
        self.assertIn("batch_style_source", header)

    def test_batch_style_cache_does_not_pass_manual_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = []
            for index in range(20):
                artifact = root / f"poster-{index:02d}.png"
                Image.new("RGB", (32, 16), "white").save(artifact)
                records.append({
                    "system": "anonymous-system",
                    "artifact": str(artifact),
                    "overall": 80.0,
                    "dimensions": {dim: 8.0 for dim in benchmark.ALL_DIMS},
                })

            with patch.object(
                benchmark,
                "evaluate_batch_style_homogeneity",
                return_value={"status": "ok", "adjustment_points": 0.0},
            ) as evaluate:
                benchmark._apply_batch_style_homogeneity(
                    records,
                    out_dir=root / "out",
                    judge_model="judge-v1",
                    reaggregate_only=True,
                    force_judge=False,
                )

        self.assertNotIn("rubric_version", evaluate.call_args.kwargs)
        self.assertNotIn("module_version", evaluate.call_args.kwargs)

    def test_missing_batch_artifact_degrades_entire_publishable_system(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = []
            for index in range(20):
                artifact = root / f"poster-{index:02d}.png"
                if index:
                    Image.new("RGB", (32, 16), "white").save(artifact)
                records.append({
                    "system": "anonymous-system",
                    "artifact": str(artifact),
                    "overall": 80.0,
                    "dimensions": {dim: 8.0 for dim in benchmark.ALL_DIMS},
                })

            with patch.object(benchmark, "evaluate_batch_style_homogeneity") as evaluate:
                adjusted, results = benchmark._apply_batch_style_homogeneity(
                    records,
                    out_dir=root / "out",
                    judge_model="judge-v1",
                    reaggregate_only=True,
                    force_judge=False,
                )

        self.assertEqual(results["anonymous-system"]["status"], "degraded")
        self.assertTrue(all(record["overall"] is None for record in adjusted))
        self.assertTrue(all(record["officially_eligible"] is False for record in adjusted))
        evaluate.assert_not_called()

    def test_small_batch_is_not_applicable_and_remains_publishable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = []
            for index in range(10):
                artifact = root / f"poster-{index:02d}.png"
                Image.new("RGB", (32, 16), "white").save(artifact)
                records.append({
                    "system": "anonymous-system",
                    "artifact": str(artifact),
                    "overall": 80.0,
                    "dimensions": {dim: 8.0 for dim in benchmark.ALL_DIMS},
                })

            with patch.object(benchmark, "evaluate_batch_style_homogeneity") as evaluate:
                adjusted, results = benchmark._apply_batch_style_homogeneity(
                    records,
                    out_dir=root / "out",
                    judge_model="judge-v1",
                    reaggregate_only=True,
                    force_judge=False,
                )

        self.assertEqual(results["anonymous-system"]["status"], "not_applicable")
        self.assertTrue(all(record["overall"] == 80.0 for record in adjusted))
        self.assertTrue(all(record["batch_style_status"] == "not_applicable" for record in adjusted))
        evaluate.assert_not_called()

    def test_overall_table_includes_all_dimensions_and_sorts_high_to_low(self) -> None:
        def row(system: str, overall: float | None) -> dict[str, object]:
            return {
                "system": system,
                "system_label": system.title(),
                "discipline": "all",
                "n": 100,
                "overall": overall,
                **{dim: 7.5 for dim in benchmark.ALL_DIMS},
            }

        html = benchmark._overall_only_table([
            row("middle", 70.0),
            row("highest", 82.0),
            row("missing", None),
            row("lowest", 55.0),
        ])

        for dim in benchmark.ALL_DIMS:
            self.assertIn(benchmark.DIM_LABELS[dim], html)
        self.assertLess(html.index("Highest"), html.index("Middle"))
        self.assertLess(html.index("Middle"), html.index("Lowest"))
        self.assertLess(html.index("Lowest"), html.index("Missing"))

    def test_reaggregate_only_without_batch_cache_is_explicitly_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = []
            for index in range(20):
                artifact = root / f"poster-{index:02d}.png"
                Image.new("RGB", (32, 16), "white").save(artifact)
                records.append({
                    "system": "anonymous-system",
                    "system_label": "Anonymous system",
                    "case": f"paper-{index:02d}",
                    "artifact": str(artifact),
                    "overall": 80.0,
                    "dimensions": {dim: 8.0 for dim in benchmark.ALL_DIMS},
                })

            with patch.object(
                benchmark,
                "evaluate_batch_style_homogeneity",
                return_value={
                    "status": "skipped",
                    "cache_status": "miss",
                    "adjustment_points": 0.0,
                    "explanation": "No valid cache.",
                },
            ) as evaluate:
                adjusted, results = benchmark._apply_batch_style_homogeneity(
                    records,
                    out_dir=root / "out",
                    judge_model="judge-v1",
                    reaggregate_only=True,
                    force_judge=False,
                )

        self.assertEqual(results["anonymous-system"]["status"], "degraded")
        self.assertEqual(results["anonymous-system"]["adjustment_points"], 0.0)
        self.assertTrue(all(record["overall"] is None for record in adjusted))
        self.assertTrue(all(record["officially_eligible"] is False for record in adjusted))
        self.assertTrue(all(record["diagnostic_overall"] == 80.0 for record in adjusted))
        self.assertIsNone(evaluate.call_args.kwargs["judge_backend"])


if __name__ == "__main__":
    unittest.main()
