from __future__ import annotations

import unittest

from PIL import Image, ImageDraw

from autodesign.evaluator.spatial import (
    _audit_empty_visual_placeholders,
    _audit_multi_panel_crop_failure,
)


def _panel(panel_id: str, x0: int, y0: int, width: int, height: int) -> dict[str, object]:
    return {
        "id": panel_id,
        "rect": {
            "x0": float(x0),
            "y0": float(y0),
            "x1": float(x0 + width),
            "y1": float(y0 + height),
            "w": float(width),
            "h": float(height),
        },
    }


class PosterSpatialFailureTest(unittest.TestCase):
    def test_two_blank_framed_visual_slots_are_a_p0_failure(self) -> None:
        image = Image.new("RGB", (1000, 500), "white")
        panels = [_panel("frame-a", 120, 120, 160, 100), _panel("frame-b", 520, 120, 160, 100)]

        result = _audit_empty_visual_placeholders(
            image=image,
            panel_regions=panels,
            heading_y=60.0,
        )

        self.assertEqual(result["blank_placeholder_count"], 2)
        self.assertEqual(result["findings"][0]["id"], "basic-layout-empty-visual-placeholder-severe")
        self.assertEqual(result["findings"][0]["severity"], "P0")

    def test_token_label_in_standalone_frame_is_near_empty(self) -> None:
        image = Image.new("RGB", (1000, 500), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((190, 150, 210, 160), fill=(160, 160, 160))
        panels = [_panel("token-frame", 120, 120, 160, 100)]

        result = _audit_empty_visual_placeholders(
            image=image,
            panel_regions=panels,
            heading_y=60.0,
        )

        self.assertEqual(result["blank_placeholder_count"], 0)
        self.assertEqual(result["near_empty_slot_count"], 1)
        self.assertEqual(result["findings"][0]["id"], "basic-layout-near-empty-visual-slot")

    def test_light_nested_chart_is_not_a_near_empty_placeholder(self) -> None:
        image = Image.new("RGB", (1000, 500), "white")
        draw = ImageDraw.Draw(image)
        draw.line((140, 190, 200, 145, 260, 180), fill=(80, 100, 130), width=2)
        child = _panel("chart", 120, 120, 160, 100)
        parent = _panel("figure-section", 70, 80, 500, 250)

        result = _audit_empty_visual_placeholders(
            image=image,
            panel_regions=[child, parent],
            heading_y=60.0,
        )

        self.assertEqual(result["near_empty_slot_count"], 0)
        self.assertEqual(result["findings"], [])

    def test_multi_panel_crop_requires_all_three_signals(self) -> None:
        result = _audit_multi_panel_crop_failure(
            canvas_metrics={"max_true_edge_count": 2},
            bottom_metrics={"true_bottom_touch_count": 3},
            crop_metrics={"crop_damage_count": 2},
        )
        self.assertTrue(result["detected"])
        self.assertEqual(result["findings"][0]["id"], "basic-layout-multi-panel-crop-failure")

        incomplete = _audit_multi_panel_crop_failure(
            canvas_metrics={"max_true_edge_count": 1},
            bottom_metrics={"true_bottom_touch_count": 3},
            crop_metrics={"crop_damage_count": 2},
        )
        self.assertFalse(incomplete["detected"])


if __name__ == "__main__":
    unittest.main()
