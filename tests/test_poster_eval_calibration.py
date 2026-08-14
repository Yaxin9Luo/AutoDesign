from __future__ import annotations

import unittest

from scripts.build_poster_eval_calibration import (
    KNOWN_SEVERE_CASES,
    KNOWN_SEVERE_SCORES,
    _analyze_labels,
    _build_manifest,
    _known_anchor_labels,
)


def _item(item_id: str, cohort: str, system_key: str, case: str) -> dict[str, object]:
    return {
        "id": item_id,
        "cohort": cohort,
        "system_key": system_key,
        "system_label": system_key,
        "discipline": "physics_astronomy",
        "discipline_label": "Physics",
        "case": case,
        "algorithm_score": 60.0,
        "algorithm_verdict": "revise",
        "artifact": f"/tmp/{item_id}.png",
        "artifact_uri": f"file:///tmp/{item_id}.png",
        "paper": "/tmp/paper.pdf",
        "source_dataset": "fixture.csv",
    }


class PosterEvalCalibrationTest(unittest.TestCase):
    def test_known_anchor_labels_are_kept_out_of_blind_manifest(self) -> None:
        severe_case = sorted(KNOWN_SEVERE_CASES)[0]
        base_items = [
            _item("base-a", "base_sample", "system_a", severe_case),
            _item("base-b", "base_sample", "system_b", severe_case),
        ]
        direct_item = _item(
            "direct-a",
            "targeted",
            "direct_cc_deepseek_v4_pro",
            severe_case,
        )
        manifest = _build_manifest(
            base_items=base_items,
            extra_items=[direct_item],
            selected_cases={("physics_astronomy", severe_case)},
            quality={},
            seed=1,
            source_paths={},
        )

        self.assertNotIn("known_anchor_labels", manifest)
        self.assertEqual(manifest["coverage"]["known_severe_anchors"], 1)
        self.assertEqual({item["split"] for item in manifest["items"]}, {"diagnostic"})
        anchors = _known_anchor_labels(manifest["items"])
        self.assertEqual(list(anchors), ["direct-a"])
        self.assertEqual(anchors["direct-a"]["human_score"], KNOWN_SEVERE_SCORES[severe_case])

    def test_holdout_split_is_paper_level_and_excludes_diagnostics(self) -> None:
        base_items = []
        selected_cases = set()
        for case_index in range(6):
            case = f"case-{case_index}"
            selected_cases.add(("physics_astronomy", case))
            base_items.extend([
                _item(f"{case}-a", "base_sample", "system_a", case),
                _item(f"{case}-b", "base_sample", "system_b", case),
            ])
        targeted = [_item("targeted", "targeted", "system_a", "case-targeted")]
        manifest = _build_manifest(
            base_items=base_items,
            extra_items=targeted,
            selected_cases=selected_cases,
            quality={},
            seed=7,
            source_paths={},
        )

        self.assertEqual(manifest["coverage"]["development_groups"], 4)
        self.assertEqual(manifest["coverage"]["holdout_groups"], 2)
        self.assertEqual(manifest["coverage"]["diagnostic_groups"], 1)
        for group in manifest["groups"]:
            item_splits = {
                item["split"]
                for item in manifest["items"]
                if item["id"] in group["item_ids"]
            }
            self.assertEqual(item_splits, {group["split"]})
        targeted_item = next(item for item in manifest["items"] if item["id"] == "targeted")
        self.assertEqual(targeted_item["split"], "diagnostic")

    def test_zero_algorithm_score_counts_as_severe_cap_hit(self) -> None:
        item = _item("zero-score", "base_sample", "system_a", "case-zero")
        item["algorithm_score"] = 0.0
        manifest = _build_manifest(
            base_items=[item],
            extra_items=[],
            selected_cases={("physics_astronomy", "case-zero")},
            quality={},
            seed=3,
            source_paths={},
        )
        analysis = _analyze_labels(manifest, {
            "manifest_id": manifest["manifest_id"],
            "labels": {
                "zero-score": {
                    "tier": "severe",
                    "human_score": 10,
                    "failures": ["broken_layout"],
                }
            },
        })

        self.assertEqual(analysis["severe_cap_recall"], 1.0)
        self.assertEqual(analysis["bad_false_pass_rate"], 0.0)
        self.assertEqual(analysis["per_split"][0]["severe_cap_recall"], 1.0)

    def test_analysis_derives_tier_from_score_when_saved_tier_is_stale(self) -> None:
        item = _item("bad-label", "base_sample", "system_a", "case-label")
        manifest = _build_manifest(
            base_items=[item],
            extra_items=[],
            selected_cases={("physics_astronomy", "case-label")},
            quality={},
            seed=5,
            source_paths={},
        )
        labels = {
            "manifest_id": manifest["manifest_id"],
            "labels": {"bad-label": {"tier": "severe", "human_score": 80}},
        }

        analysis = _analyze_labels(manifest, labels)

        self.assertEqual(analysis["rows"][0]["human_tier"], "pass")
        self.assertEqual(analysis["rows"][0]["saved_human_tier"], "severe")
        self.assertEqual(analysis["label_quality"]["tier_score_mismatches"], 1)

    def test_analysis_accepts_numeric_score_without_saved_tier(self) -> None:
        item = _item("score-only", "base_sample", "system_a", "case-score-only")
        manifest = _build_manifest(
            base_items=[item],
            extra_items=[],
            selected_cases={("physics_astronomy", "case-score-only")},
            quality={},
            seed=6,
            source_paths={},
        )
        analysis = _analyze_labels(manifest, {
            "manifest_id": manifest["manifest_id"],
            "labels": {"score-only": {"tier": "", "human_score": 55}},
        })

        self.assertEqual(analysis["labeled_items"], 1)
        self.assertEqual(analysis["rows"][0]["human_tier"], "revise")
        self.assertEqual(analysis["label_quality"]["missing_saved_tiers"], 1)


if __name__ == "__main__":
    unittest.main()
