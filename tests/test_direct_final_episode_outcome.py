from __future__ import annotations

import unittest
from types import SimpleNamespace

from autodesign.agents.external_designer_author import (
    _soft_finalizable_direct_validation_feedback,
)
from autodesign.runner import _derive_episode_outcome
from autodesign.schema import CritiqueReport


class DirectFinalEpisodeOutcomeTests(unittest.TestCase):
    def test_near_miss_soft_accept_waits_for_attempt_budget(self) -> None:
        feedback = {
            "summary": {
                "issue_id": "paper_poster_html_local_flow_overflow",
                "severity": "near_miss",
                "soft_finalizable": True,
                "visible_overflow": False,
                "hard_issue_count": 0,
                "issues": [{
                    "severity": "near_miss",
                    "soft_finalizable": True,
                    "visible_overflow": False,
                    "bottom_overflow_px": 4,
                    "scroll_overflow_px": {"bottom": 4},
                }],
            },
            "payload": {
                "issue_id": "paper_poster_html_local_flow_overflow",
                "severity": "near_miss",
                "soft_finalizable": True,
                "visible_overflow": False,
                "hard_issue_count": 0,
                "blank_fill_required": False,
                "issues": [{
                    "severity": "near_miss",
                    "soft_finalizable": True,
                    "visible_overflow": False,
                    "bottom_overflow_px": 4,
                    "scroll_overflow_px": {"bottom": 4},
                }],
            },
        }

        self.assertIsNone(
            _soft_finalizable_direct_validation_feedback(
                feedback,
                11,
                max_attempts=12,
            )
        )
        accepted = _soft_finalizable_direct_validation_feedback(
            feedback,
            12,
            max_attempts=12,
        )
        self.assertEqual(accepted["attempt"], 12)

    def test_soft_accepted_poster_ignores_stale_critic_failure(self) -> None:
        ctx = SimpleNamespace(
            state={
                "designer_author_direct_final": {
                    "source": "external_designer_author",
                    "acceptance_path": "soft_accept",
                },
                "critique_results": [
                    CritiqueReport(score=0.0, verdict="fail", summary="prior candidate")
                ],
            }
        )

        status, score = _derive_episode_outcome(
            ctx,
            finalized=True,
            spec_present=True,
            composition_present=True,
        )

        self.assertEqual(status, "pass")
        self.assertIsNone(score)

    def test_existing_poster_direct_final_does_not_change_stale_critic_semantics(self) -> None:
        ctx = SimpleNamespace(
            state={
                "designer_author_direct_final": {
                    "source": "external_designer_author",
                    "acceptance_path": "critic_skipped_final_attempt",
                },
                "critique_results": [
                    CritiqueReport(score=0.42, verdict="fail", summary="prior candidate")
                ],
            }
        )

        status, score = _derive_episode_outcome(
            ctx,
            finalized=True,
            spec_present=True,
            composition_present=True,
        )

        self.assertEqual(status, "fail")
        self.assertEqual(score, 0.42)

    def test_all_validated_external_artifact_authors_are_direct_final(self) -> None:
        for source in (
            "external_landing_author",
            "external_slides_author",
            "external_video_author",
        ):
            with self.subTest(source=source):
                ctx = SimpleNamespace(
                    state={
                        "designer_author_direct_final": {
                            "source": source,
                            "acceptance_path": "deterministic_validation_pass",
                        },
                        "critique_results": [
                            CritiqueReport(
                                score=0.42,
                                verdict="fail",
                                summary="stale pre-promotion critique",
                            )
                        ],
                    }
                )

                status, score = _derive_episode_outcome(
                    ctx,
                    finalized=True,
                    spec_present=True,
                    composition_present=True,
                )

                self.assertEqual(status, "pass")
                self.assertIsNone(score)

    def test_normal_failed_critique_remains_failure(self) -> None:
        ctx = SimpleNamespace(
            state={
                "critique_results": [
                    CritiqueReport(score=0.42, verdict="fail", summary="current candidate")
                ]
            }
        )

        status, score = _derive_episode_outcome(
            ctx,
            finalized=True,
            spec_present=True,
            composition_present=True,
        )

        self.assertEqual(status, "fail")
        self.assertEqual(score, 0.42)


if __name__ == "__main__":
    unittest.main()
