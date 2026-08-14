from __future__ import annotations

import unittest
from types import SimpleNamespace

from autodesign.tools.propose_design_spec import _dense_authored_visible_word_floor


class ReferenceDenseWordFloorTests(unittest.TestCase):
    def test_reference_poster_honors_explicit_visible_word_target(self) -> None:
        ctx = SimpleNamespace(
            state={
                "poster_plan_contract": {
                    "kind": "paper_poster_plan_contract",
                    "layout_archetype": "reference-poster",
                    "reference_profile": "conference_editorial_flow",
                    "reference_layout_contract": {"source": "reference_poster"},
                    "content_fill_targets": {"target_panel_fill_ratio": 0.68},
                    "native_information_targets": {
                        "min_visible_words": 360,
                        "target_visible_words": 560,
                    },
                }
            }
        )

        self.assertEqual(_dense_authored_visible_word_floor(ctx), 560)

    def test_non_reference_dense_poster_keeps_default_floor(self) -> None:
        ctx = SimpleNamespace(
            state={
                "poster_plan_contract": {
                    "kind": "paper_poster_plan_contract",
                    "layout_archetype": "three-column-editorial",
                    "reference_profile": "conference_editorial_flow",
                    "content_fill_targets": {"target_panel_fill_ratio": 0.68},
                    "native_information_targets": {
                        "min_visible_words": 360,
                        "target_visible_words": 560,
                    },
                }
            }
        )

        self.assertEqual(_dense_authored_visible_word_floor(ctx), 650)


if __name__ == "__main__":
    unittest.main()
