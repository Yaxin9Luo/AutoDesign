from __future__ import annotations

import unittest

from autodesign.util.poster_plan_contract import (
    _is_allowed_preflight_archetype,
    preflight_poster_plan_contract,
)


class ReferenceContractPreflightTest(unittest.TestCase):
    def test_reference_canvas_is_allowed_without_joining_normal_archetypes(self) -> None:
        reference_canvas = {
            "source": "reference_poster",
            "preset_id": "reference-poster",
        }

        self.assertTrue(
            _is_allowed_preflight_archetype("reference-poster", reference_canvas)
        )
        self.assertFalse(
            _is_allowed_preflight_archetype(
                "reference-poster",
                {"source": "brief_scene", "preset_id": "reference-poster"},
            )
        )
        self.assertTrue(
            _is_allowed_preflight_archetype(
                "cvpr-landscape",
                {"source": "brief_scene", "preset_id": "cvpr-landscape"},
            )
        )

    def test_reference_contract_preflight_does_not_request_cvpr_pivot(self) -> None:
        contract = {
            "kind": "paper_poster_plan_contract",
            "layout_archetype": "reference-poster",
            "selected_visuals": [],
            "required_sections": [],
            "density_targets": {"min_visual_area_ratio": 0.4},
        }
        canvas = {
            "source": "reference_poster",
            "preset_id": "reference-poster",
            "canvas": {"w_px": 4096, "h_px": 2048},
            "density_budget": {"visual_area_min": 0.4},
        }

        report = preflight_poster_plan_contract(
            contract,
            {"sections": []},
            rendered_layers={},
            canvas_plan=canvas,
        )

        self.assertNotIn(
            "poster_contract_preflight_layout_archetype_invalid",
            {finding["id"] for finding in report["findings"]},
        )


if __name__ == "__main__":
    unittest.main()
