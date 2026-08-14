from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

from autodesign.util.pdf import (
    discover_captioned_visual_groups,
    recover_captioned_visual_groups,
)


def _draw_figure(page: fitz.Page, rect: fitz.Rect) -> None:
    page.draw_rect(rect, color=(0.1, 0.2, 0.5), fill=(0.82, 0.88, 0.96), width=1.2)
    page.draw_line(rect.top_left, rect.bottom_right, color=(0.1, 0.2, 0.5), width=1.0)
    page.draw_line(rect.bottom_left, rect.top_right, color=(0.1, 0.2, 0.5), width=1.0)
    page.draw_circle(rect.tl + (rect.br - rect.tl) * 0.5, 18, color=(0.7, 0.2, 0.1), width=1.2)


def _insert_caption(page: fitz.Page, rect: fitz.Rect, text: str) -> None:
    remaining = page.insert_textbox(rect, text, fontsize=10, fontname="helv")
    if remaining < 0:
        raise AssertionError(f"caption did not fit fixture box: {text}")


class PdfCaptionDiscoveryTests(unittest.TestCase):
    def test_splits_two_captions_in_one_block_with_distinct_anchors(self) -> None:
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text(fitz.Point(48, 430), "Figure 3: Left-side samples.", fontsize=10)
        page.insert_text(fitz.Point(330, 430), "Figure 4 - Right-side ablation.", fontsize=10)

        groups = discover_captioned_visual_groups(doc)

        self.assertEqual([group.label for group in groups], ["3", "4"])
        left, right = groups
        self.assertLess(left.caption_rect[2], right.caption_rect[2])
        self.assertLess((left.caption_rect[0] + left.caption_rect[2]) / 2, page.rect.width / 2)
        self.assertGreater((right.caption_rect[0] + right.caption_rect[2]) / 2, page.rect.width / 2)
        self.assertIn("Left-side samples", left.caption_text)
        self.assertIn("Right-side ablation", right.caption_text)
        doc.close()

    def test_cross_reference_does_not_create_or_drop_caption(self) -> None:
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        _insert_caption(
            page,
            fitz.Rect(72, 420, 540, 458),
            "Figure 5: Main architecture and training process; see Table 4 for numeric details.",
        )

        groups = discover_captioned_visual_groups(doc)

        self.assertEqual([(group.kind, group.label) for group in groups], [("figure", "5")])
        self.assertIn("Table 4", groups[0].caption_text)
        doc.close()


class PdfCaptionRecoveryTests(unittest.TestCase):
    def _recover(self, doc: fitz.Document):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        figures, tables = recover_captioned_visual_groups(doc, Path(temp_dir.name))
        self.assertEqual(tables, [])
        return figures

    def test_recovers_caption_below_and_above_figure(self) -> None:
        for caption_above in (False, True):
            with self.subTest(caption_above=caption_above):
                doc = fitz.open()
                page = doc.new_page(width=612, height=792)
                visual_rect = fitz.Rect(120, 170, 492, 330)
                caption_rect = (
                    fitz.Rect(120, 120, 492, 150)
                    if caption_above
                    else fitz.Rect(120, 350, 492, 380)
                )
                _draw_figure(page, visual_rect)
                _insert_caption(page, caption_rect, "Figure 1: Complete model overview and results.")

                figures = self._recover(doc)

                self.assertEqual(len(figures), 1)
                recovered = fitz.Rect(figures[0].bbox_pt)
                self.assertGreaterEqual(recovered.width, visual_rect.width - 2)
                self.assertGreaterEqual(recovered.height, visual_rect.height - 2)
                self.assertTrue(figures[0].captioned_source_group)
                doc.close()

    def test_padding_stops_at_adjacent_prose_and_foreign_caption(self) -> None:
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_textbox(
            fitz.Rect(92, 72, 520, 112),
            "This paragraph contains enough surrounding prose to be a body-text boundary. "
            "It must not leak into the recovered scientific figure crop.",
            fontsize=9,
        )
        visual_rect = fitz.Rect(110, 120, 500, 278)
        _draw_figure(page, visual_rect)
        _insert_caption(page, fitz.Rect(110, 288, 500, 316), "Figure 6: Main qualitative comparison.")
        _insert_caption(page, fitz.Rect(110, 322, 500, 350), "Figure 7: Neighboring analysis figure.")

        figures = self._recover(doc)

        figure6 = next(item for item in figures if item.anchor_label == "6")
        recovered = fitz.Rect(figure6.bbox_pt)
        prose_rect = fitz.Rect(page.get_text("blocks")[0][:4])
        self.assertGreater(recovered.y0, prose_rect.y1)
        self.assertLessEqual(recovered.y1, 286.0)
        doc.close()

    def test_multi_component_figure_is_one_group_crop(self) -> None:
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        left = fitz.Rect(80, 170, 270, 315)
        right = fitz.Rect(330, 170, 520, 315)
        _draw_figure(page, left)
        _draw_figure(page, right)
        _insert_caption(
            page,
            fitz.Rect(80, 340, 540, 370),
            "Figure 2: Two-panel comparison across experimental conditions and representative datasets.",
        )

        figures = self._recover(doc)

        self.assertEqual(len(figures), 1)
        recovered = fitz.Rect(figures[0].bbox_pt)
        self.assertLessEqual(recovered.x0, left.x0 + 2)
        self.assertGreaterEqual(recovered.x1, right.x1 - 2)
        doc.close()

    def test_recovers_multi_component_figure_left_of_side_caption(self) -> None:
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        top_left = fitz.Rect(55, 120, 245, 240)
        top_right = fitz.Rect(275, 120, 420, 240)
        bottom = fitz.Rect(55, 265, 420, 355)
        for rect in (top_left, top_right, bottom):
            _draw_figure(page, rect)
        _insert_caption(
            page,
            fitz.Rect(445, 120, 570, 310),
            "Figure 4: Multi-faceted characterization with three coordinated panels "
            "shown to the left of this side caption.",
        )
        page.insert_textbox(
            fitz.Rect(55, 390, 420, 455),
            "This paragraph contains surrounding body prose below the figure and must not "
            "be included in the recovered crop.",
            fontsize=9,
        )

        figures = self._recover(doc)

        self.assertEqual(len(figures), 1)
        recovered = fitz.Rect(figures[0].bbox_pt)
        self.assertLessEqual(recovered.x0, top_left.x0 + 2)
        self.assertGreaterEqual(recovered.x1, top_right.x1 - 2)
        self.assertLessEqual(recovered.y0, top_left.y0 + 2)
        self.assertGreaterEqual(recovered.y1, bottom.y1 - 2)
        self.assertLess(recovered.x1, 443)
        self.assertLess(recovered.y1, 390)
        doc.close()

    def test_side_by_side_captions_recover_distinct_non_overlapping_figures(self) -> None:
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        left = fitz.Rect(55, 170, 285, 330)
        right = fitz.Rect(327, 170, 557, 330)
        _draw_figure(page, left)
        _draw_figure(page, right)
        page.insert_text(fitz.Point(55, 352), "Figure 3: Left-side samples.", fontsize=10)
        page.insert_text(fitz.Point(327, 352), "Figure 4: Right-side ablation.", fontsize=10)

        figures = self._recover(doc)

        self.assertEqual({item.anchor_label for item in figures}, {"3", "4"})
        recovered = {item.anchor_label: fitz.Rect(item.bbox_pt) for item in figures}
        self.assertLessEqual(recovered["3"].x1, page.rect.width / 2 + 26)
        self.assertGreaterEqual(recovered["4"].x0, page.rect.width / 2 - 26)
        self.assertFalse(recovered["3"].intersects(recovered["4"]))
        doc.close()

    def test_recovery_logs_structured_skip_reason(self) -> None:
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        _insert_caption(page, fitz.Rect(90, 300, 522, 330), "Figure 9: Missing visual payload.")

        events: list[tuple[str, dict[str, object]]] = []

        def capture(event: str, **payload: object) -> None:
            events.append((event, payload))

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "autodesign.util.pdf.log", side_effect=capture
        ):
            figures, _ = recover_captioned_visual_groups(doc, Path(temp_dir))

        self.assertEqual(figures, [])
        skipped = [payload for event, payload in events if event == "ingest.pdf.captioned_group.recovery_skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].get("reason"), "no_visual_components")
        doc.close()

    def test_recovery_distinguishes_boundary_collision_from_hard_contamination(self) -> None:
        cases: list[tuple[str, fitz.Document]] = []

        boundary_doc = fitz.open()
        boundary_page = boundary_doc.new_page(width=612, height=792)
        boundary_page.insert_textbox(
            fitz.Rect(110, 128, 500, 160),
            "This surrounding paragraph is a real prose boundary with enough words to be classified. "
            "It must stop recovery padding.",
            fontsize=8,
        )
        _draw_figure(boundary_page, fitz.Rect(110, 162, 500, 180))
        _insert_caption(
            boundary_page,
            fitz.Rect(110, 185, 500, 213),
            "Figure 10: Narrow diagnostic strip.",
        )
        cases.append(("text_boundary_collision", boundary_doc))

        contaminated_doc = fitz.open()
        contaminated_page = contaminated_doc.new_page(width=612, height=792)
        _draw_figure(contaminated_page, fitz.Rect(110, 150, 500, 300))
        contaminated_page.insert_text(fitz.Point(140, 205), "4 Experimental Results", fontsize=12)
        _insert_caption(
            contaminated_page,
            fitz.Rect(110, 320, 500, 348),
            "Figure 11: Crop containing foreign section text.",
        )
        cases.append(("hard_crop_contamination", contaminated_doc))

        for expected_reason, doc in cases:
            with self.subTest(expected_reason=expected_reason), tempfile.TemporaryDirectory() as temp_dir:
                events: list[tuple[str, dict[str, object]]] = []

                def capture(event: str, **payload: object) -> None:
                    events.append((event, payload))

                with patch("autodesign.util.pdf.log", side_effect=capture):
                    figures, _ = recover_captioned_visual_groups(doc, Path(temp_dir))
                self.assertEqual(figures, [])
                skipped = [
                    payload
                    for event, payload in events
                    if event == "ingest.pdf.captioned_group.recovery_skipped"
                ]
                self.assertEqual(len(skipped), 1)
                self.assertEqual(skipped[0].get("reason"), expected_reason)
                doc.close()


if __name__ == "__main__":
    unittest.main()
