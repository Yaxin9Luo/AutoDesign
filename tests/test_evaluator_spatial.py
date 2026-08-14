import unittest
from typing import Any
from unittest.mock import patch

from PIL import Image
from autodesign.evaluator import spatial


def _rect(x0: float, y0: float, x1: float, y1: float) -> dict[str, float]:
    return {
        "x0": float(x0),
        "y0": float(y0),
        "x1": float(x1),
        "y1": float(y1),
        "w": float(x1 - x0),
        "h": float(y1 - y0),
    }


def _region(region_id: str, kind: str, x0: float, y0: float, x1: float, y1: float, **extra: Any) -> dict[str, Any]:
    rect = _rect(x0, y0, x1, y1)
    return {
        "id": region_id,
        "kind": kind,
        "rect": rect,
        "area": rect["w"] * rect["h"],
        **extra,
    }


class BasicLayoutIntegrityRecalibrationTests(unittest.TestCase):
    def test_flat_header_bands_and_rules_are_not_visual_crop_damage(self) -> None:
        section = _region("section-1", "panel", 100, 120, 900, 560, confidence=0.95)
        visual_regions = [
            _region("section-header-band", "visual", 0, 120, 1000, 142),
            _region("flat-rule-line", "visual", 40, 548, 960, 558),
        ]

        metrics = spatial._audit_visual_crop_damage(
            visual_regions=visual_regions,
            section_regions=[section],
            width=1000,
            height=700,
            long_edge=1000,
        )

        self.assertEqual(0, metrics["crop_damage_count"])
        self.assertEqual([], metrics["findings"])

    def test_bottom_occupancy_without_true_edge_regions_is_untrusted_p2(self) -> None:
        occupancy = {
            "occ": [[False] * 20 for _ in range(19)] + [[True] * 20],
            "heading_rows": 2,
        }

        metrics = spatial._audit_bottom_truncation(
            text_regions=[],
            visual_regions=[],
            occupancy=occupancy,
            width=1000,
            height=700,
            inset=12,
            heading_y=90,
        )

        self.assertTrue(metrics["finding"])
        self.assertEqual("P2", metrics["severity"])
        self.assertIs(metrics["trusted_p1"], False)

    def test_low_confidence_panel_overflow_cannot_be_trusted_p1(self) -> None:
        panel = _region("panel-1", "panel", 100, 100, 500, 500, confidence=0.25)
        visuals = [
            _region(f"visual-{index}", "visual", 420, 140 + index * 90, 610, 210 + index * 90)
            for index in range(3)
        ]

        metrics = spatial._audit_panel_bounds(
            text_regions=[],
            visual_regions=visuals,
            panel_regions=[panel],
            canvas_width=1000,
            canvas_height=700,
            long_edge=1000,
        )
        finding = next(f for f in metrics["findings"] if f["id"] == "basic-layout-panel-visual-overflow")

        self.assertEqual("P2", finding["severity"])
        self.assertIs(finding["evidence"]["trusted_p1"], False)

    def test_low_confidence_panels_are_not_closed_section_boundaries(self) -> None:
        sections = [
            _region("panel-1", "panel", 100, 100, 300, 300, confidence=0.25),
            _region("panel-2", "panel", 340, 100, 540, 300, confidence=0.25),
        ]
        visuals = [
            _region("visual-1", "visual", 180, 140, 390, 220),
            _region("visual-2", "visual", 420, 140, 620, 220),
            _region("visual-3", "visual", 180, 230, 390, 290),
        ]

        metrics = spatial._audit_section_bounds(
            text_regions=[],
            visual_regions=visuals,
            section_regions=sections,
            panel_region_count=2,
            long_edge=1000,
            canvas_width=1000,
            canvas_height=700,
            heading_y=90,
        )

        self.assertEqual("inferred", metrics["source"])
        self.assertTrue(all(f["severity"] != "P1" for f in metrics["findings"]))

    def test_panel_border_pixels_alone_do_not_prove_visual_crop(self) -> None:
        import numpy as np

        section = _region("section-1", "panel", 100, 100, 500, 500, confidence=0.95)
        visuals = [
            _region("visual-1", "visual", 420, 140, 610, 250),
            _region("visual-2", "visual", 420, 290, 610, 400),
        ]
        border_only_mask = np.zeros((700, 1000), dtype=np.uint8)
        border_only_mask[100:501, 499:502] = 255

        metrics = spatial._audit_visual_crop_damage(
            visual_regions=visuals,
            section_regions=[section],
            width=1000,
            height=700,
            long_edge=1000,
            visual_mask=border_only_mask,
        )

        self.assertEqual(0, metrics["crop_damage_count"])
        self.assertEqual([], metrics["findings"])

    def test_active_pixels_crossing_credible_boundary_remain_trusted_p1(self) -> None:
        import numpy as np

        section = _region("section-1", "panel", 100, 100, 500, 500, confidence=0.95)
        visuals = [
            _region("visual-1", "visual", 420, 140, 610, 250),
            _region("visual-2", "visual", 420, 290, 610, 400),
        ]
        visual_mask = np.zeros((700, 1000), dtype=np.uint8)
        visual_mask[140:250, 420:610] = 255
        visual_mask[290:400, 420:610] = 255

        metrics = spatial._audit_visual_crop_damage(
            visual_regions=visuals,
            section_regions=[section],
            width=1000,
            height=700,
            long_edge=1000,
            visual_mask=visual_mask,
        )
        finding = next(f for f in metrics["findings"] if f["id"] == "basic-layout-visual-crop-damage")

        self.assertEqual("P1", finding["severity"])
        self.assertIs(finding["evidence"]["trusted_p1"], True)

    def test_visual_internal_frame_is_not_a_panel_overflow_boundary(self) -> None:
        panel = _region("inner-figure-frame", "panel", 100, 100, 500, 300, confidence=0.95)
        visual = _region("wide-source-figure", "visual", 110, 100, 580, 290)
        caption = _region("figure-caption", "text", 350, 275, 575, 295, body=True)

        metrics = spatial._audit_panel_bounds(
            text_regions=[caption],
            visual_regions=[visual],
            panel_regions=[panel],
            canvas_width=1000,
            canvas_height=700,
            long_edge=1000,
        )

        self.assertEqual(0, metrics["panel_count"])
        self.assertEqual(1, metrics["visual_internal_panel_count"])
        self.assertNotIn(
            "basic-layout-panel-visual-overflow",
            {finding["id"] for finding in metrics["findings"]},
        )
        self.assertNotIn(
            "basic-layout-panel-text-overflow",
            {finding["id"] for finding in metrics["findings"]},
        )

    def test_visual_internal_frame_is_not_active_boundary_crop_damage(self) -> None:
        import numpy as np

        panel = _region("inner-figure-frame", "panel", 100, 100, 500, 300, confidence=0.95)
        visual = _region("wide-source-figure", "visual", 110, 100, 580, 290)
        visual_mask = np.zeros((700, 1000), dtype=np.uint8)
        visual_mask[100:290, 110:580] = 255

        metrics = spatial._audit_visual_crop_damage(
            visual_regions=[visual],
            section_regions=[panel],
            width=1000,
            height=700,
            long_edge=1000,
            visual_mask=visual_mask,
        )

        self.assertEqual(0, metrics["crop_damage_count"])
        self.assertEqual([], metrics["findings"])

    def test_heading_text_touching_true_top_edge_is_canvas_clipping(self) -> None:
        heading = _region("title", "text", 200, 0, 800, 42, body=False)

        metrics = spatial._audit_canvas_overflow(
            text_regions=[heading],
            visual_regions=[],
            width=1000,
            height=700,
            inset=12,
            heading_y=90,
        )

        self.assertIs(metrics["finding"], True)
        self.assertIs(metrics["trusted_p1"], True)
        self.assertEqual(1, metrics["true_edge_counts"]["top"])

    def test_panel_overflow_without_active_boundary_evidence_is_p2(self) -> None:
        panel = _region("section-panel", "panel", 100, 100, 500, 500, confidence=0.95)
        visual = _region("overflowing-visual", "visual", 420, 260, 610, 390)
        text_regions = [
            _region("body-1", "text", 130, 150, 390, 175, body=True),
            _region("body-2", "text", 130, 190, 410, 215, body=True),
        ]

        metrics = spatial._audit_panel_bounds(
            text_regions=text_regions,
            visual_regions=[visual],
            panel_regions=[panel],
            canvas_width=1000,
            canvas_height=700,
            long_edge=1000,
        )
        finding = next(
            finding for finding in metrics["findings"]
            if finding["id"] == "basic-layout-panel-visual-overflow"
        )

        self.assertEqual("P2", finding["severity"])
        self.assertIs(finding["evidence"]["trusted_p1"], False)

    def test_closed_panel_overflow_is_not_duplicated_as_section_overflow(self) -> None:
        sections = [
            _region("panel-1", "panel", 100, 100, 300, 300, confidence=0.95),
            _region("panel-2", "panel", 340, 100, 540, 300, confidence=0.95),
        ]
        visuals = [
            _region("visual-1", "visual", 180, 140, 390, 220),
            _region("visual-2", "visual", 420, 140, 620, 220),
            _region("visual-3", "visual", 180, 230, 390, 290),
        ]

        metrics = spatial._audit_section_bounds(
            text_regions=[],
            visual_regions=visuals,
            section_regions=sections,
            panel_region_count=2,
            long_edge=1000,
            canvas_width=1000,
            canvas_height=700,
            heading_y=90,
        )

        self.assertEqual("panel", metrics["source"])
        self.assertNotIn(
            "basic-layout-section-content-overflow",
            {finding["id"] for finding in metrics["findings"]},
        )
        self.assertNotIn(
            "basic-layout-inter-section-collision",
            {finding["id"] for finding in metrics["findings"]},
        )

    def test_intentional_side_full_bleed_visuals_are_not_crop_damage(self) -> None:
        visuals = [
            _region("left-full-bleed", "visual", 0, 120, 230, 620),
            _region("right-full-bleed", "visual", 770, 120, 1000, 620),
        ]

        metrics = spatial._audit_visual_crop_damage(
            visual_regions=visuals,
            section_regions=[],
            width=1000,
            height=700,
            long_edge=1000,
        )

        self.assertEqual(0, metrics["crop_damage_count"])
        self.assertEqual([], metrics["findings"])

    def test_intentional_full_width_visual_bands_are_not_canvas_clipping(self) -> None:
        visuals = [
            _region("full-width-visual-1", "visual", 0, 120, 1000, 320),
            _region("full-width-visual-2", "visual", 0, 380, 1000, 580),
        ]

        canvas = spatial._audit_canvas_overflow(
            text_regions=[],
            visual_regions=visuals,
            width=1000,
            height=700,
            inset=12,
            heading_y=90,
        )
        crop = spatial._audit_visual_crop_damage(
            visual_regions=visuals,
            section_regions=[],
            width=1000,
            height=700,
            long_edge=1000,
        )

        self.assertIs(canvas["finding"], False)
        self.assertIs(canvas["trusted_p1"], False)
        self.assertEqual(0, crop["crop_damage_count"])
        self.assertEqual([], crop["findings"])

    def test_public_findings_preserve_applied_penalty(self) -> None:
        result = spatial.basic_layout_integrity(
            Image.new("RGB", (500, 250), "white"),
            segments=[],
        )

        self.assertTrue(result["findings"])
        self.assertTrue(all("penalty" in finding for finding in result["findings"]))

    def test_nested_parent_and_child_sections_do_not_create_crossing_finding(self) -> None:
        parent_column = _region("column-1", "panel", 80, 120, 500, 660, confidence=0.95)
        child_section = _region("section-1", "panel", 100, 200, 460, 350, confidence=0.95)
        text = _region("text-1", "text", 120, 230, 440, 270, body=True)

        metrics = spatial._audit_section_bounds(
            text_regions=[text],
            visual_regions=[],
            section_regions=[parent_column, child_section],
            panel_region_count=2,
            long_edge=1000,
            canvas_width=1000,
            canvas_height=700,
            heading_y=90,
        )

        self.assertEqual(0, metrics["inter_section_collision_count"])
        self.assertNotIn(
            "basic-layout-inter-section-collision",
            {finding["id"] for finding in metrics["findings"]},
        )

    def test_inferred_section_overflow_is_p2_and_not_trusted_p1(self) -> None:
        sections = [
            _region("section-1", "inferred-section", 100, 150, 300, 250, confidence=0.55),
            _region("section-2", "inferred-section", 100, 290, 300, 390, confidence=0.55),
            _region("section-3", "inferred-section", 100, 430, 300, 530, confidence=0.55),
        ]
        visual_regions = [
            _region("visual-1", "visual", 120, 180, 350, 220),
            _region("visual-2", "visual", 120, 320, 350, 360),
            _region("visual-3", "visual", 120, 460, 350, 500),
        ]

        metrics = spatial._audit_section_bounds(
            text_regions=[],
            visual_regions=visual_regions,
            section_regions=sections,
            panel_region_count=0,
            long_edge=1000,
            canvas_width=1000,
            canvas_height=700,
            heading_y=90,
        )
        overflow = next(f for f in metrics["findings"] if f["id"] == "basic-layout-section-content-overflow")

        self.assertEqual("P2", overflow["severity"])
        self.assertEqual("inferred", overflow["evidence"]["source"])
        self.assertIs(overflow["evidence"]["trusted_p1"], False)

    def test_touching_two_section_edges_without_overflow_is_not_visual_crop_damage(self) -> None:
        section = _region("section-1", "panel", 100, 100, 500, 400, confidence=0.95)
        visual = _region("contained-wide-visual", "visual", 100, 150, 500, 260)

        metrics = spatial._audit_visual_crop_damage(
            visual_regions=[visual],
            section_regions=[section],
            width=1000,
            height=700,
            long_edge=1000,
        )

        self.assertEqual(0, metrics["crop_damage_count"])
        self.assertEqual([], metrics["findings"])

    def test_canvas_clipping_remains_trusted_p1_with_source_evidence(self) -> None:
        visual_regions = [
            _region("bottom-clipped-visual-1", "visual", 120, 520, 320, 700),
            _region("bottom-clipped-visual-2", "visual", 420, 540, 620, 700),
        ]

        metrics = spatial._audit_visual_crop_damage(
            visual_regions=visual_regions,
            section_regions=[],
            width=1000,
            height=700,
            long_edge=1000,
        )
        finding = next(f for f in metrics["findings"] if f["id"] == "basic-layout-visual-crop-damage")

        self.assertEqual("P1", finding["severity"])
        self.assertIn("trusted_p1", finding["evidence"])
        self.assertIs(finding["evidence"]["trusted_p1"], True)
        self.assertIn("trusted_sources", finding["evidence"])
        self.assertIn("canvas", finding["evidence"]["trusted_sources"])

    def test_figure_region_rects_include_canvas_dimensions_for_grouping_ratios(self) -> None:
        visual = _region("visual-1", "visual", 100, 150, 500, 350)
        cv_payload = {
            "panels": [],
            "visuals": [visual],
            "heading_visuals": [],
            "heading_dividers": [],
            "visual_mask": None,
            "available": True,
        }

        with patch.object(spatial, "_cv_layout_regions", return_value=cv_payload):
            result = spatial.basic_layout_integrity(Image.new("RGB", (1000, 500), "white"), segments=[])

        self.assertEqual(1, len(result["figure_region_rects"]))
        figure = result["figure_region_rects"][0]
        self.assertIn("canvas_width", figure)
        self.assertIn("canvas_height", figure)
        self.assertEqual(1000, figure["canvas_width"])
        self.assertEqual(500, figure["canvas_height"])


if __name__ == "__main__":
    unittest.main()
