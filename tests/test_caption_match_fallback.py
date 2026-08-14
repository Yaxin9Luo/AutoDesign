from __future__ import annotations

import importlib
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from autodesign.util.pdf import CaptionedVisualGroup, PdfFigureCandidate


ingest_document = importlib.import_module("autodesign.tools.ingest_document")


def _candidate(
    root: Path,
    name: str,
    *,
    page: int = 1,
    bbox: tuple[float, float, float, float] = (40.0, 80.0, 260.0, 220.0),
    source_group_label: str = "",
    source_group_caption: str = "",
) -> PdfFigureCandidate:
    path = root / f"{name}.png"
    Image.new("RGB", (440, 280), "white").save(path)
    return PdfFigureCandidate(
        page=page,
        bbox_pt=bbox,
        path=path,
        width_px=440,
        height_px=280,
        strategy="raster",
        xref=1,
        protected_anchor=bool(source_group_label),
        anchor_kind="figure" if source_group_label else "",
        anchor_label=source_group_label.replace("Figure ", "") if source_group_label else "",
        anchor_reason="captioned_source_group" if source_group_label else "",
        captioned_source_group=bool(source_group_label),
        source_group_id=f"p001:figure:{name}" if source_group_label else "",
        source_group_kind="figure" if source_group_label else "",
        source_group_label=source_group_label,
        source_group_caption=source_group_caption,
        source_group_source="pdf_caption_block" if source_group_label else "",
    )


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(settings=SimpleNamespace(ingest_model="test-model"))


class CaptionMatchFallbackTests(unittest.TestCase):
    def test_captioned_source_group_binds_without_vlm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            candidate = _candidate(
                Path(tmp),
                "fig3",
                source_group_label="Figure 3",
                source_group_caption="Figure 3: Denoising trajectories.",
            )
            manifest = {
                "figures": [{
                    "page": 1,
                    "caption": "Figure 3: Denoising trajectories.",
                }],
            }
            with patch.object(ingest_document, "vlm_call_json") as vlm:
                matches = ingest_document._match_captions_parallel([candidate], manifest, _ctx())

            vlm.assert_not_called()
            self.assertEqual(matches[0]["matched_idx"], 0)
            self.assertEqual(matches[0]["caption_association_method"], "captioned_group")
            self.assertEqual(matches[0]["confidence"], 0.95)

    def test_geometry_assignment_is_global_for_side_by_side_figures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidates = [
                _candidate(root, "left", bbox=(20.0, 80.0, 260.0, 220.0)),
                _candidate(root, "right", bbox=(330.0, 80.0, 570.0, 220.0)),
            ]
            manifest = {
                "figures": [
                    {
                        "page": 1,
                        "caption": "Figure 3: Left-hand samples.",
                        "caption_bbox_pdf_points": [20.0, 225.0, 260.0, 250.0],
                    },
                    {
                        "page": 1,
                        "caption": "Figure 4: Right-hand samples.",
                        "caption_bbox_pdf_points": [330.0, 225.0, 570.0, 250.0],
                    },
                ],
            }
            with patch.object(ingest_document, "vlm_call_json") as vlm:
                matches = ingest_document._match_captions_parallel(candidates, manifest, _ctx())

            vlm.assert_not_called()
            self.assertEqual([matches[0]["matched_idx"], matches[1]["matched_idx"]], [0, 1])
            self.assertEqual(matches[0]["caption_association_method"], "geometry")
            self.assertEqual(matches[1]["caption_association_method"], "geometry")

    def test_weak_single_geometry_candidate_still_uses_vlm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            candidate = _candidate(Path(tmp), "weak", bbox=(20.0, 80.0, 260.0, 220.0))
            manifest = {
                "figures": [{
                    "page": 1,
                    "caption": "Figure 8: A distant, weakly aligned caption.",
                    "caption_bbox_pdf_points": [212.0, 355.0, 452.0, 380.0],
                }],
            }
            vlm_result = {
                "matched_idx": 0,
                "confidence": 0.8,
                "is_real_figure": True,
                "short_caption": "Distant caption",
                "sub_panels": [],
                "reason": "visual evidence confirms the match",
            }

            with patch.object(ingest_document, "vlm_call_json", return_value=vlm_result) as vlm:
                matches = ingest_document._match_captions_parallel([candidate], manifest, _ctx())

            vlm.assert_called_once()
            self.assertEqual(matches[0]["caption_association_method"], "vlm")
            self.assertEqual(matches[0]["matched_idx"], 0)

    def test_pdf_discovery_geometry_is_persisted_into_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            candidate = _candidate(Path(tmp), "fig5", bbox=(40.0, 80.0, 260.0, 220.0))
            manifest = {"figures": []}
            ingest_document._enrich_manifest_with_pdf_caption_groups(
                manifest,
                [CaptionedVisualGroup(
                    kind="figure",
                    label="5",
                    page=1,
                    caption_rect=(40.0, 225.0, 260.0, 250.0),
                    caption_text="Figure 5: Samples and quantitative comparison.",
                )],
            )

            with patch.object(ingest_document, "vlm_call_json") as vlm:
                matches = ingest_document._match_captions_parallel([candidate], manifest, _ctx())

            vlm.assert_not_called()
            self.assertEqual(manifest["figures"][0]["caption_bbox_pdf_points"], [40.0, 225.0, 260.0, 250.0])
            self.assertEqual(matches[0]["caption_association_method"], "geometry")

    def test_vlm_failure_uses_unique_same_page_geometry_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidates = [
                _candidate(root, "wide", bbox=(20.0, 80.0, 570.0, 220.0)),
                _candidate(root, "left", bbox=(20.0, 80.0, 260.0, 220.0)),
            ]
            manifest = {
                "figures": [
                    {
                        "page": 1,
                        "caption": "Figure 5: Left-hand samples.",
                        "caption_bbox_pdf_points": [20.0, 225.0, 260.0, 250.0],
                    },
                    {
                        "page": 1,
                        "caption": "Figure 6: Right-hand quantitative comparison.",
                        "caption_bbox_pdf_points": [330.0, 225.0, 570.0, 250.0],
                    },
                ],
            }
            events: list[tuple[str, dict[str, object]]] = []

            def capture(event: str, **payload: object) -> None:
                events.append((event, payload))

            with patch.object(
                ingest_document,
                "vlm_call_json",
                side_effect=RuntimeError("400 Unable to download the image"),
            ), patch.object(ingest_document, "log", side_effect=capture):
                matches = ingest_document._match_captions_parallel(candidates, manifest, _ctx())

            self.assertEqual(matches[0]["matched_idx"], 1)
            self.assertEqual(matches[0]["caption_association_method"], "geometry_fallback")
            self.assertEqual(matches[1]["matched_idx"], 0)
            self.assertEqual(matches[1]["caption_association_method"], "geometry")
            self.assertGreaterEqual(matches[0]["confidence"], 0.5)
            self.assertIn("400", matches[0]["reason"])
            summary = next(payload for event, payload in events if event == "ingest.pdf.caption_match.summary")
            self.assertEqual(summary["methods"]["geometry_fallback"], 1)
            self.assertTrue(any("400" in reason for reason in summary["failure_reasons"]))

    def test_vlm_duplicate_caption_assignment_is_completion_order_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidates = [_candidate(root, "slow"), _candidate(root, "fast")]
            manifest = {"figures": [{"page": 1, "caption": "Figure 1: Overview."}]}

            def fake_match(index, *_args):
                if index == 0:
                    time.sleep(0.03)
                    confidence = 0.9
                else:
                    confidence = 0.7
                return {
                    "matched_idx": 0,
                    "confidence": confidence,
                    "is_real_figure": True,
                    "reason": "fixture",
                    "caption_text": "Figure 1: Overview.",
                    "short_caption": "Overview",
                    "sub_panels": [],
                }

            with patch.object(ingest_document, "_match_one_caption", side_effect=fake_match):
                matches = ingest_document._match_captions_parallel(candidates, manifest, _ctx())

            self.assertEqual(matches[0]["caption_association_method"], "vlm")
            self.assertEqual(matches[0]["matched_idx"], 0)
            self.assertEqual(matches[1]["caption_association_method"], "unmatched")


if __name__ == "__main__":
    unittest.main()
