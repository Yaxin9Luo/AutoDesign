from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from autodesign.harness_matrix import classify_run_dir
from autodesign.util.io import sha256_file


class HarnessMatrixClassificationTests(unittest.TestCase):
    def test_fallback_manifest_must_match_final_poster_bytes(self) -> None:
        cases = ("missing_final", "missing_hash", "mismatched_hash")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw:
                run_dir = Path(raw) / case
                final_dir = run_dir / "final"
                final_dir.mkdir(parents=True)
                html_path = final_dir / "poster.html"
                if case != "missing_final":
                    html_path.write_text(
                        "<!doctype html><main class='paper-poster'>Paper</main>",
                        encoding="utf-8",
                    )
                manifest = {
                    "quality_status": "ready_with_warnings",
                    "quality_diagnostics": [
                        "paper_poster_html_typography_contract_failed"
                    ],
                    "remaining_hard_issue_ids": [],
                }
                if case == "missing_final":
                    manifest["html_sha256"] = "a" * 64
                elif case == "mismatched_hash":
                    manifest["html_sha256"] = "b" * 64
                (final_dir / "designer_author_best_available_artifact_fallback.json").write_text(
                    json.dumps(manifest),
                    encoding="utf-8",
                )

                result = classify_run_dir(run_dir, returncode=0)

                self.assertEqual(result["outcome_class"], "stale_fallback_manifest")
                self.assertEqual(
                    result["hard_issue_ids"],
                    ["stale_fallback_manifest"],
                )
                self.assertIn("stale", result["primary_blocker"])

    def test_matching_fallback_manifest_remains_usable(self) -> None:
        for manifest_name, expected_outcome in (
            (
                "designer_author_best_available_artifact_fallback.json",
                "best_available_with_warnings",
            ),
            (
                "designer_author_best_candidate_fallback.json",
                "best_candidate_fallback",
            ),
        ):
            with (
                self.subTest(manifest=manifest_name),
                tempfile.TemporaryDirectory() as raw,
            ):
                run_dir = Path(raw)
                final_dir = run_dir / "final"
                final_dir.mkdir()
                html_path = final_dir / "poster.html"
                html_path.write_text(
                    "<!doctype html><main class='paper-poster'>Paper</main>",
                    encoding="utf-8",
                )
                (final_dir / manifest_name).write_text(
                    json.dumps({
                        "html_sha256": sha256_file(html_path),
                        "quality_status": "ready_with_warnings",
                        "quality_diagnostics": [
                            "paper_poster_html_typography_contract_failed"
                        ],
                        "remaining_hard_issue_ids": [],
                    }),
                    encoding="utf-8",
                )

                result = classify_run_dir(run_dir, returncode=0)

                self.assertEqual(result["outcome_class"], expected_outcome)
                self.assertEqual(result["hard_issue_ids"], [])


if __name__ == "__main__":
    unittest.main()
