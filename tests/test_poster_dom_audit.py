from __future__ import annotations

import importlib.util
import inspect
import json
import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from tests import test_autodesign_poster_skill as poster_skill_fixtures


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "agent_skills" / "autodesign-poster"
DOM_AUDIT_PATH = SKILL_ROOT / "scripts" / "poster_dom_audit.py"


def _load_dom_audit(test_case: unittest.TestCase):
    test_case.assertTrue(
        DOM_AUDIT_PATH.is_file(), "standalone Poster DOM audit module is missing"
    )
    module_name = "autodesign_portable_poster_dom_audit_test"
    spec = importlib.util.spec_from_file_location(module_name, DOM_AUDIT_PATH)
    test_case.assertIsNotNone(spec)
    test_case.assertIsNotNone(spec.loader if spec else None)
    module = importlib.util.module_from_spec(spec)
    previous = __import__("sys").dont_write_bytecode
    __import__("sys").dont_write_bytecode = True
    try:
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        __import__("sys").dont_write_bytecode = previous
    return module


def _rect(x: float, y: float, w: float, h: float) -> dict[str, float]:
    return {"x": x, "y": y, "w": w, "h": h, "right": x + w, "bottom": y + h}


def _snapshot(*, media: str = "screen") -> dict[str, object]:
    return {
        "media": media,
        "viewport": {
            "width": 1000,
            "height": 600,
            "document_width": 1000,
            "document_height": 600,
        },
        "root": {
            "block_id": "paper-poster-root",
            "rect": _rect(0, 0, 1000, 600),
            "background_color": "rgb(255, 255, 255)",
            "background_rgba": [255, 255, 255, 255],
            "background_image": "none",
            "effective_opacity": 1,
            "paint_effects": [],
            "scrollWidth": 1000,
            "scrollHeight": 600,
            "clientWidth": 1000,
            "clientHeight": 600,
            "overflowX": "hidden",
            "overflowY": "hidden",
        },
        "text_nodes": [],
        "elements": [],
        "images": [],
        "tables": [],
        "lists": [],
        "panels": [],
        "source_flows": [],
    }


def _dense_snapshot(*, media: str = "screen") -> dict[str, object]:
    snapshot = _snapshot(media=media)
    snapshot["text_nodes"] = [
        {
            "block_id": f"dense-{row}-{column}",
            "text": "Grounded editable poster content fills this scientific region.",
            "rect": _rect(15 + column * 245, 10 + row * 95, 225, 80),
            "visible_rect": _rect(15 + column * 245, 10 + row * 95, 225, 80),
            "clipped_by": "",
        }
        for row in range(6)
        for column in range(4)
    ]
    return snapshot


class PosterDomPureEvaluatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audit = _load_dom_audit(self)
        self.canvas = {"width_px": 1000, "height_px": 600}
        self.print_size = {"width_mm": 264.5833, "height_mm": 158.75}

    def _evaluate(
        self,
        screen: dict[str, object],
        print_snapshot: dict[str, object] | None = None,
    ) -> dict[str, object]:
        evaluations = [
            self.audit.evaluate_dom_snapshot(
                screen,
                canvas=self.canvas,
                print_size=self.print_size,
            )
        ]
        if print_snapshot is not None:
            evaluations.append(
                self.audit.evaluate_dom_snapshot(
                    print_snapshot,
                    canvas=self.canvas,
                    print_size=self.print_size,
                )
            )
        return {
            "passed": all(item["passed"] for item in evaluations),
            "findings": [
                finding
                for evaluation in evaluations
                for finding in evaluation["findings"]
            ],
            "metrics": {
                key: value
                for evaluation in evaluations
                for key, value in evaluation["metrics"].items()
            },
        }

    def _codes(self, result: dict[str, object]) -> list[str]:
        return [str(item["code"]) for item in result["findings"]]

    def test_public_interface_uses_one_snapshot_and_one_probe_script(self) -> None:
        parameters = inspect.signature(
            self.audit.evaluate_dom_snapshot
        ).parameters
        self.assertEqual(list(parameters), ["snapshot", "canvas", "print_size"])
        self.assertEqual(
            parameters["snapshot"].kind,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        self.assertTrue(
            all(
                parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
                for name in ("canvas", "print_size")
            )
        )
        self.assertEqual(
            list(inspect.signature(self.audit.browser_probe_script).parameters), []
        )
        script = self.audit.browser_probe_script()
        self.assertIsInstance(script, str)
        self.assertIn("document.createTreeWalker", script)

        result = self.audit.evaluate_dom_snapshot(
            _dense_snapshot(),
            canvas=self.canvas,
            print_size=self.print_size,
        )

        self.assertTrue(result["passed"], result)

    def test_each_media_capture_waits_for_fonts_and_uses_the_public_probe_script(self) -> None:
        probe_script = "(media) => ({media, sentinel: 'single-probe-source'})"

        class FakePage:
            def __init__(self) -> None:
                self.calls: list[tuple[object, ...]] = []

            def emulate_media(self, *, media: str) -> None:
                self.calls.append(("emulate_media", media))

            def evaluate(self, script: str, argument: str | None = None) -> object:
                if script == probe_script:
                    self.calls.append(("evaluate_probe", argument))
                    return {"media": argument, "sentinel": "single-probe-source"}
                self.assert_font_script(script)
                self.calls.append(("wait_fonts",))
                return True

            def screenshot(self, *, full_page: bool) -> bytes:
                self.calls.append(("screenshot", full_page))
                return b"png-bytes"

            @staticmethod
            def assert_font_script(script: str) -> None:
                if "document.fonts.ready" not in script:
                    raise AssertionError(f"unexpected page evaluation: {script}")

        page = FakePage()
        with mock.patch.object(
            self.audit, "browser_probe_script", return_value=probe_script
        ):
            screen = self.audit._capture_media_snapshot(page, "screen")
            printed = self.audit._capture_media_snapshot(page, "print")

        self.assertEqual(screen, ({"media": "screen", "sentinel": "single-probe-source"}, b"png-bytes"))
        self.assertEqual(printed, ({"media": "print", "sentinel": "single-probe-source"}, b"png-bytes"))
        self.assertEqual(
            page.calls,
            [
                ("emulate_media", "screen"),
                ("wait_fonts",),
                ("evaluate_probe", "screen"),
                ("screenshot", True),
                ("emulate_media", "print"),
                ("wait_fonts",),
                ("evaluate_probe", "print"),
                ("screenshot", True),
            ],
        )

    def test_valid_dense_snapshot_is_deterministic_and_has_no_findings(self) -> None:
        screen = _dense_snapshot()
        first = self._evaluate(screen)
        second = self._evaluate(json.loads(json.dumps(screen)))
        self.assertEqual(first, second)
        self.assertEqual((first["passed"], first["findings"]), (True, []))

    def test_primary_canvas_requires_opaque_white_without_a_background_image(self) -> None:
        cases = {
            "transparent": ("rgba(0, 0, 0, 0)", [0, 0, 0, 0], "none"),
            "tinted": ("rgb(248, 250, 252)", [248, 250, 252, 255], "none"),
            "dark": ("rgb(18, 24, 32)", [18, 24, 32, 255], "none"),
            "gradient": (
                "rgb(255, 255, 255)",
                [255, 255, 255, 255],
                "linear-gradient(rgb(255, 255, 255), rgb(238, 242, 247))",
            ),
        }
        for media in ("screen", "print"):
            for label, (color, rgba, image) in cases.items():
                with self.subTest(media=media, label=label):
                    snapshot = _dense_snapshot(media=media)
                    snapshot["root"]["background_color"] = color
                    snapshot["root"]["background_rgba"] = rgba
                    snapshot["root"]["background_image"] = image
                    result = self.audit.evaluate_dom_snapshot(
                        snapshot,
                        canvas=self.canvas,
                        print_size=self.print_size,
                    )
                    self.assertIn("poster-dom-canvas-background", self._codes(result))

        white = self.audit.evaluate_dom_snapshot(
            _dense_snapshot(),
            canvas=self.canvas,
            print_size=self.print_size,
        )
        self.assertNotIn("poster-dom-canvas-background", self._codes(white))

    def test_primary_canvas_rejects_paint_effects_that_change_rendered_white(self) -> None:
        cases = {
            "ancestor-opacity": {"effective_opacity": 0.5},
            "root-filter": {"paint_effects": ["filter:opacity(0.5)"]},
            "root-blend": {"paint_effects": ["mix-blend-mode:multiply"]},
            "root-mask": {"paint_effects": ["mask-image:linear-gradient(#fff,transparent)"]},
            "root-clip": {"paint_effects": ["clip:rect(0px,500px,600px,0px)"]},
        }
        for label, changes in cases.items():
            with self.subTest(label=label):
                snapshot = _dense_snapshot()
                snapshot["root"].update(changes)
                result = self.audit.evaluate_dom_snapshot(
                    snapshot,
                    canvas=self.canvas,
                    print_size=self.print_size,
                )
                self.assertIn("poster-dom-canvas-background", self._codes(result))

    def test_canvas_fill_detects_blank_lower_canvas_without_panels(self) -> None:
        snapshot = _snapshot()
        snapshot["text_nodes"] = [
            {
                "block_id": f"top-copy-{column}",
                "text": "Grounded content occupies only the top of this sparse poster.",
                "rect": _rect(20 + column * 245, 20, 220, 90),
                "visible_rect": _rect(20 + column * 245, 20, 220, 90),
                "clipped_by": "",
            }
            for column in range(4)
        ]

        result = self.audit.evaluate_dom_snapshot(
            snapshot,
            canvas=self.canvas,
            print_size=self.print_size,
        )

        self.assertEqual(snapshot["panels"], [])
        self.assertIn("poster-dom-blank-band", self._codes(result))
        self.assertIn("poster-dom-sparse-oversized-panel", self._codes(result))
        self.assertGreater(result["metrics"]["screen_canvas_lower_blank_ratio"], 0.75)

    def test_canvas_fill_detects_underfilled_columns_even_when_content_reaches_bottom(self) -> None:
        snapshot = _snapshot()
        snapshot["text_nodes"] = [
            {
                "block_id": f"left-column-{row}",
                "text": "Grounded content fills one column but not the poster width.",
                "rect": _rect(20, 10 + row * 95, 200, 80),
                "visible_rect": _rect(20, 10 + row * 95, 200, 80),
                "clipped_by": "",
            }
            for row in range(6)
        ]

        result = self.audit.evaluate_dom_snapshot(
            snapshot,
            canvas=self.canvas,
            print_size=self.print_size,
        )

        self.assertNotIn("poster-dom-blank-band", self._codes(result))
        self.assertIn("poster-dom-sparse-oversized-panel", self._codes(result))
        finding = next(
            item
            for item in result["findings"]
            if item["code"] == "poster-dom-sparse-oversized-panel"
        )
        self.assertIn("lower_half_sparse", finding["geometry"]["reasons"])

    def test_root_position_escape_is_detected_in_all_four_directions(self) -> None:
        cases = {
            "left": _rect(-12, 0, 1000, 600),
            "top": _rect(0, -12, 1000, 600),
            "right": _rect(12, 0, 1000, 600),
            "bottom": _rect(0, 12, 1000, 600),
        }

        for direction, root_rect in cases.items():
            with self.subTest(direction=direction):
                snapshot = _snapshot()
                snapshot["root"]["rect"] = root_rect
                result = self.audit.evaluate_dom_snapshot(
                    snapshot,
                    canvas=self.canvas,
                    print_size=self.print_size,
                )
                finding = next(
                    item
                    for item in result["findings"]
                    if item["code"] == "poster-dom-viewport-escape"
                )
                self.assertGreater(finding["geometry"][f"{direction}_gap_px"], 4)
                self.assertEqual(finding["geometry"]["media"], "screen")

    def test_document_scroll_escape_and_body_margin_offset_are_both_measured(self) -> None:
        snapshot = _snapshot()
        snapshot["root"]["rect"] = _rect(12, 0, 1000, 600)
        snapshot["viewport"]["document_width"] = 1012

        result = self.audit.evaluate_dom_snapshot(
            snapshot,
            canvas=self.canvas,
            print_size=self.print_size,
        )

        findings = {item["code"]: item for item in result["findings"]}
        self.assertIn("poster-dom-root-overflow", findings)
        self.assertIn("poster-dom-viewport-escape", findings)
        self.assertEqual(
            findings["poster-dom-root-overflow"]["geometry"]["document_width_gap_px"],
            12,
        )
        self.assertEqual(
            findings["poster-dom-viewport-escape"]["geometry"]["right_gap_px"],
            12,
        )

        vertical = _snapshot()
        vertical["viewport"]["document_height"] = 612
        vertical_result = self.audit.evaluate_dom_snapshot(
            vertical,
            canvas=self.canvas,
            print_size=self.print_size,
        )
        vertical_finding = next(
            item
            for item in vertical_result["findings"]
            if item["code"] == "poster-dom-root-overflow"
        )
        self.assertEqual(vertical_finding["geometry"]["document_height_gap_px"], 12)

    def test_intended_root_and_document_bounds_do_not_emit_fit_findings(self) -> None:
        result = self.audit.evaluate_dom_snapshot(
            _snapshot(),
            canvas=self.canvas,
            print_size=self.print_size,
        )

        self.assertFalse(
            {
                "poster-dom-root-overflow",
                "poster-dom-viewport-escape",
                "poster-dom-screen-print-mismatch",
            }
            & set(self._codes(result)),
            result,
        )

    def test_physical_print_canvas_sets_the_allowed_document_bounds(self) -> None:
        snapshot = _snapshot(media="print")
        snapshot["viewport"] = {
            "width": 3072,
            "height": 1536,
            "document_width": 8064,
            "document_height": 4032,
        }
        snapshot["root"] = {
            **snapshot["root"],
            "rect": _rect(0, 0, 8064, 4032),
            "scrollWidth": 8064,
            "scrollHeight": 4032,
            "clientWidth": 8064,
            "clientHeight": 4032,
        }

        result = self.audit.evaluate_dom_snapshot(
            snapshot,
            canvas={"width_px": 3072, "height_px": 1536},
            print_size={"width_mm": 2133.6, "height_mm": 1066.8},
        )

        self.assertNotIn("poster-dom-root-overflow", self._codes(result), result)
        self.assertNotIn(
            "poster-dom-screen-print-mismatch", self._codes(result), result
        )

    def test_all_stable_finding_codes_have_the_exact_contract(self) -> None:
        cases: dict[str, tuple[dict[str, object], dict[str, object]]] = {}

        root_overflow = _snapshot()
        root_overflow["root"]["scrollHeight"] = 690
        cases["poster-dom-root-overflow"] = (root_overflow, _snapshot(media="print"))

        clipping = _snapshot()
        clipping["text_nodes"] = [
            {
                "block_id": "clipped-copy",
                "text": "This grounded text is visibly clipped by its panel.",
                "rect": _rect(40, 60, 360, 80),
                "visible_rect": _rect(40, 60, 360, 42),
                "clipped_by": "method-panel",
            }
        ]
        cases["poster-dom-text-clipping"] = (clipping, _snapshot(media="print"))

        overlap = _snapshot()
        overlap["text_nodes"] = [
            {
                "block_id": "left-copy",
                "text": "First editable text block with grounded content.",
                "rect": _rect(50, 100, 300, 90),
                "visible_rect": _rect(50, 100, 300, 90),
                "clipped_by": "",
            },
            {
                "block_id": "right-copy",
                "text": "Second editable text block collides with the first.",
                "rect": _rect(250, 120, 300, 90),
                "visible_rect": _rect(250, 120, 300, 90),
                "clipped_by": "",
            },
        ]
        cases["poster-dom-text-overlap"] = (overlap, _snapshot(media="print"))

        escaped = _snapshot()
        escaped["elements"] = [
            {
                "block_id": "escaped-result",
                "tag": "section",
                "role": "result",
                "class_name": "poster-section",
                "text": "Grounded result",
                "rect": _rect(940, 120, 120, 180),
            }
        ]
        cases["poster-dom-viewport-escape"] = (escaped, _snapshot(media="print"))

        blank_band = _snapshot()
        blank_band["panels"] = [
            {
                "block_id": "blank-panel",
                "rect": _rect(20, 20, 440, 520),
                "word_count": 150,
                "content_rects": [_rect(40, 40, 400, 120), _rect(40, 180, 400, 80)],
            }
        ]
        cases["poster-dom-blank-band"] = (blank_band, _snapshot(media="print"))

        sparse = _snapshot()
        sparse["panels"] = [
            {
                "block_id": "sparse-panel",
                "rect": _rect(20, 20, 440, 520),
                "word_count": 9,
                "content_rects": [_rect(40, 40, 120, 50)],
            }
        ]
        cases["poster-dom-sparse-oversized-panel"] = (sparse, _snapshot(media="print"))

        low_resolution = _snapshot()
        low_resolution["images"] = [
            {
                "block_id": "method-figure",
                "source_id": "vis-0001",
                "rect": _rect(80, 100, 400, 300),
                "complete": True,
                "naturalWidth": 420,
                "naturalHeight": 315,
            }
        ]
        cases["poster-dom-image-low-effective-resolution"] = (
            low_resolution,
            _snapshot(media="print"),
        )

        table_overflow = _snapshot()
        table_overflow["tables"] = [
            {
                "block_id": "results-table",
                "rect": _rect(80, 160, 420, 180),
                "scrollWidth": 560,
                "scrollHeight": 180,
                "clientWidth": 420,
                "clientHeight": 180,
                "overflowX": "hidden",
                "overflowY": "visible",
                "font_px": 24,
            }
        ]
        cases["poster-dom-table-overflow"] = (table_overflow, _snapshot(media="print"))

        small_table_text = _snapshot()
        small_table_text["tables"] = [
            {
                "block_id": "tiny-table",
                "rect": _rect(80, 160, 420, 180),
                "scrollWidth": 420,
                "scrollHeight": 180,
                "clientWidth": 420,
                "clientHeight": 180,
                "overflowX": "visible",
                "overflowY": "visible",
                "font_px": 23.5,
            }
        ]
        cases["poster-dom-table-text-small"] = (
            small_table_text,
            _snapshot(media="print"),
        )

        bad_gutter = _snapshot()
        bad_gutter["lists"] = [
            {
                "block_id": "method-readout",
                "rect": _rect(260, 120, 220, 200),
                "item_count": 3,
                "has_source_flow_ancestor": True,
                "is_direct_source_flow_child": True,
                "has_floated_source_sibling": True,
                "paddingInlineStartPx": 8,
                "paddingLeftPx": 8,
                "textIndentPx": -3,
            }
        ]
        cases["poster-dom-source-flow-gutter"] = (bad_gutter, _snapshot(media="print"))

        bad_sibling = _snapshot()
        bad_sibling["source_flows"] = [
            {
                "block_id": "method-flow",
                "rect": _rect(40, 80, 440, 360),
                "source_child_count": 1,
                "readout_child_count": 1,
                "direct_sibling": False,
            }
        ]
        cases["poster-dom-source-flow-sibling"] = (
            bad_sibling,
            _snapshot(media="print"),
        )

        print_mismatch = _snapshot(media="print")
        print_mismatch["root"]["rect"] = _rect(0, 0, 900, 600)
        print_mismatch["root"]["clientWidth"] = 900
        print_mismatch["root"]["scrollWidth"] = 900
        cases["poster-dom-screen-print-mismatch"] = (_snapshot(), print_mismatch)

        canvas_background = _snapshot()
        canvas_background["root"]["background_color"] = "rgb(245, 247, 250)"
        canvas_background["root"]["background_rgba"] = [245, 247, 250, 255]
        cases["poster-dom-canvas-background"] = (
            canvas_background,
            _snapshot(media="print"),
        )

        boxy = _snapshot()
        boxy["elements"] = [
            {
                "block_id": f"metric-card-{index:02d}",
                "tag": "div",
                "role": "metric",
                "class_name": "metric-card",
                "text": "Grounded metric result",
                "word_count": 3,
                "rect": _rect(20 + (index % 5) * 180, 30 + (index // 5) * 110, 150, 80),
                "border_width_px": 1,
                "background_distinct": True,
                "has_shadow": False,
            }
            for index in range(10)
        ]
        cases["poster-dom-template-boxiness"] = (boxy, _snapshot(media="print"))

        expected_codes = {
            "poster-dom-root-overflow",
            "poster-dom-text-clipping",
            "poster-dom-text-overlap",
            "poster-dom-viewport-escape",
            "poster-dom-blank-band",
            "poster-dom-sparse-oversized-panel",
            "poster-dom-image-low-effective-resolution",
            "poster-dom-table-overflow",
            "poster-dom-table-text-small",
            "poster-dom-source-flow-gutter",
            "poster-dom-source-flow-sibling",
            "poster-dom-screen-print-mismatch",
            "poster-dom-canvas-background",
            "poster-dom-template-boxiness",
        }
        self.assertEqual(set(cases), expected_codes)
        for expected_code, (screen, print_snapshot) in cases.items():
            with self.subTest(code=expected_code):
                result = self._evaluate(screen, print_snapshot)
                self.assertIn(expected_code, self._codes(result), result)
                finding = next(
                    item for item in result["findings"] if item["code"] == expected_code
                )
                self.assertEqual(
                    set(finding),
                    {
                        "code",
                        "block_id",
                        "severity",
                        "geometry",
                        "message",
                        "suggested_repair_route",
                    },
                )
                self.assertEqual(finding["suggested_repair_route"], "layout_repair")
                self.assertIsInstance(finding["geometry"], dict)
                self.assertIn(finding["geometry"]["media"], {"screen", "print"})
                self.assertIn(
                    finding["geometry"]["media"].capitalize(), finding["message"]
                )

                print_only = json.loads(
                    json.dumps(
                        print_snapshot
                        if expected_code == "poster-dom-screen-print-mismatch"
                        else screen
                    )
                )
                print_only["media"] = "print"
                print_result = self.audit.evaluate_dom_snapshot(
                    print_only,
                    canvas=self.canvas,
                    print_size=self.print_size,
                )
                self.assertIn(expected_code, self._codes(print_result), print_result)
                print_finding = next(
                    item
                    for item in print_result["findings"]
                    if item["code"] == expected_code
                )
                self.assertEqual(print_finding["geometry"]["media"], "print")

    def test_table_container_escape_uses_the_table_overflow_code(self) -> None:
        screen = _snapshot()
        screen["tables"] = [
            {
                "block_id": "escaped-table",
                "rect": _rect(80, 160, 460, 180),
                "container_rect": _rect(80, 160, 400, 180),
                "scrollWidth": 460,
                "scrollHeight": 180,
                "clientWidth": 460,
                "clientHeight": 180,
                "overflowX": "visible",
                "overflowY": "visible",
                "font_px": 24,
            }
        ]

        result = self._evaluate(screen)

        finding = next(
            item
            for item in result["findings"]
            if item["code"] == "poster-dom-table-overflow"
        )
        self.assertGreater(finding["geometry"]["container_escape_px"], 8)

    def test_scroll_gaps_compare_scroll_size_to_the_client_box(self) -> None:
        root_screen = _snapshot()
        root_screen["root"]["rect"] = _rect(0, 0, 1006, 600)
        root_screen["root"]["scrollWidth"] = 1005
        root_screen["root"]["clientWidth"] = 1000
        self.assertIn(
            "poster-dom-root-overflow", self._codes(self._evaluate(root_screen))
        )

        table_screen = _snapshot()
        table_screen["tables"] = [
            {
                "block_id": "bordered-overflow-table",
                "rect": _rect(80, 160, 426, 180),
                "container_rect": _rect(60, 140, 500, 220),
                "scrollWidth": 425,
                "scrollHeight": 176,
                "clientWidth": 420,
                "clientHeight": 176,
                "font_px": 24,
            }
        ]
        self.assertIn(
            "poster-dom-table-overflow", self._codes(self._evaluate(table_screen))
        )

    def test_sparse_large_panel_uses_the_rendered_content_area_threshold(self) -> None:
        screen = _snapshot()
        screen["panels"] = [
            {
                "block_id": "underfilled-panel",
                "rect": _rect(20, 20, 440, 520),
                "word_count": 80,
                "content_rects": [_rect(40, 40, 120, 50)],
            }
        ]

        result = self._evaluate(screen)

        self.assertIn("poster-dom-sparse-oversized-panel", self._codes(result))

    def test_source_flow_requires_direct_source_readout_and_evidence_binding(self) -> None:
        cases = (
            (1, 1, True, False),
            (1, 1, True, None),
            (0, 1, False, False),
        )
        for source_count, readout_count, direct_sibling, evidence_value in cases:
            with self.subTest(
                source_count=source_count,
                readout_count=readout_count,
                direct_sibling=direct_sibling,
                evidence_ids_intersect=evidence_value,
            ):
                screen = _snapshot()
                flow = {
                    "block_id": "method-flow",
                    "rect": _rect(40, 80, 440, 360),
                    "source_child_count": source_count,
                    "readout_child_count": readout_count,
                    "direct_sibling": direct_sibling,
                }
                if evidence_value is not None:
                    flow["evidence_ids_intersect"] = evidence_value
                screen["source_flows"] = [flow]

                result = self._evaluate(screen)

                self.assertIn("poster-dom-source-flow-sibling", self._codes(result))

    def test_screen_print_parity_also_requires_the_screen_canvas(self) -> None:
        screen = _snapshot()
        screen["root"]["rect"] = _rect(0, 0, 900, 600)
        screen["root"]["clientWidth"] = 900
        screen["root"]["scrollWidth"] = 900

        result = self._evaluate(screen)

        self.assertIn("poster-dom-screen-print-mismatch", self._codes(result))


class PosterDomRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.audit = _load_dom_audit(self)
        self.harness = poster_skill_fixtures._load_harness()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, name: str) -> tuple[Path, str, Path]:
        run = self.root / name
        source = self.root / f"{name}.txt"
        source.write_text(
            "Grounded poster source reports 85% accuracy and uses two-stage routing. "
            "The grounded poster retains accuracy.",
            encoding="utf-8",
        )
        self.harness.initialize_poster_run(run, source)
        evidence_id = str(self.harness.core.load_evidence(run)[0]["id"])
        selection = {
            "run_format_version": 2,
            "assets": [],
            "source_story": {
                key: {
                    "status": "not_applicable",
                    "asset_ids": [],
                    "evidence_ids": [evidence_id],
                    "rationale": f"The reviewed source has no distinct {key} visual region.",
                }
                for key in ("central_method", "primary_result")
            },
        }
        context = self.harness.create_poster_source_review_context(run, selection)
        review = {
            "run_format_version": 2,
            "source_review_context_sha256": context["context_sha256"],
            "reviewer_kind": "fresh_subagent",
            "dimension_scores": {
                "importance": 4,
                "crop_completeness": 4,
                "caption_claim_match": 4,
                "label_axis_legend_readability": 4,
                "duplicate_or_ornamental_content": 4,
                "method_result_coverage": 4,
                "poster_area_fit": 4,
            },
            "asset_findings": [],
            "coverage_findings": [],
            "blockers": [],
            "localized_repairs": [],
            "verdict": "pass",
            "complete": True,
        }
        self.harness.record_poster_source_review(
            run, context["context_path"], review
        )
        self.harness.save_poster_plan(run, poster_skill_fixtures._plan())
        attempt = self.harness.begin_poster_attempt(run)["attempt_id"]
        artifact = run / "attempts" / attempt / "artifact"
        (artifact / "poster.html").write_text(
            poster_skill_fixtures._poster_html(), encoding="utf-8"
        )
        return run, attempt, artifact

    def _probe_payload(self) -> dict[str, object]:
        screen = _dense_snapshot()
        printed = _dense_snapshot(media="print")
        for snapshot in (screen, printed):
            snapshot["viewport"] = {
                "width": 3072,
                "height": 1536,
                "document_width": 3072,
                "document_height": 1536,
            }
            snapshot["root"] = {
                **snapshot["root"],
                "rect": _rect(0, 0, 3072, 1536),
                "scrollWidth": 3072,
                "scrollHeight": 1536,
                "clientWidth": 3072,
                "clientHeight": 1536,
            }
            snapshot["text_nodes"] = [
                {
                    "block_id": f"dense-{row}-{column}",
                    "text": "Grounded editable poster content fills this scientific region.",
                    "rect": _rect(
                        45 + column * 755,
                        25 + row * 245,
                        700,
                        215,
                    ),
                    "visible_rect": _rect(
                        45 + column * 755,
                        25 + row * 245,
                        700,
                        215,
                    ),
                    "clipped_by": "",
                }
                for row in range(6)
                for column in range(4)
            ]
        return {
            "screen_snapshot": screen,
            "print_snapshot": printed,
            "screenshots": {
                "screen": poster_skill_fixtures.AutoDesignPosterSkillTests._png(
                    8, 6, 30
                ),
                "print": poster_skill_fixtures.AutoDesignPosterSkillTests._png(
                    8, 6, 60
                ),
            },
            "diagnostics": {
                "blocked_requests": [],
                "blocked_popups": [],
                "blocked_workers": [],
                "console_errors": [],
                "request_errors": [],
                "page_errors": [],
            },
        }

    def _artifact_bytes(self, artifact: Path) -> dict[str, bytes]:
        return {
            path.relative_to(artifact).as_posix(): path.read_bytes()
            for path in artifact.rglob("*")
            if path.is_file()
        }

    def test_run_audit_writes_only_dom_qa_and_preserves_artifact_bytes(self) -> None:
        run, attempt, artifact = self._run("read-only-runner")
        before = self._artifact_bytes(artifact)
        with mock.patch.object(
            self.audit, "_invoke_browser_worker", return_value=self._probe_payload()
        ):
            report = self.audit.run_poster_dom_audit(
                run, attempt, cache_root=self.root / "browser", allow_browser_install=False
            )

        self.assertTrue(report["passed"], report)
        self.assertTrue(report["artifact_unchanged"])
        self.assertEqual(
            report["artifact_tree_sha256_before"],
            report["artifact_tree_sha256_after"],
        )
        self.assertEqual(self._artifact_bytes(artifact), before)
        qa = run / "attempts" / attempt / "qa"
        self.assertTrue((qa / "dom-audit.json").is_file())
        self.assertTrue((qa / "previews" / "dom-screen.png").is_file())
        self.assertTrue((qa / "previews" / "dom-print.png").is_file())
        self.assertEqual(
            {
                path.relative_to(qa).as_posix()
                for path in qa.rglob("*")
                if path.is_file()
            },
            {
                "dom-audit.json",
                "previews/dom-screen.png",
                "previews/dom-print.png",
            },
        )
        self.assertEqual(
            self.audit.load_verified_poster_dom_audit(run, attempt), report
        )

    def test_run_audit_blocks_print_only_defects_and_exposes_both_media_metrics(self) -> None:
        run, attempt, _artifact = self._run("print-only-defects")
        payload = self._probe_payload()
        printed = payload["print_snapshot"]
        printed["text_nodes"] = [
            {
                "block_id": "print-clipped-copy",
                "text": "Print-only editable text is clipped by its authored panel.",
                "rect": _rect(80, 80, 600, 100),
                "visible_rect": _rect(80, 80, 600, 40),
                "clipped_by": "print-panel",
            }
        ]
        printed["tables"] = [
            {
                "block_id": "print-wide-table",
                "rect": _rect(80, 220, 600, 240),
                "container_rect": _rect(80, 220, 600, 240),
                "scrollWidth": 720,
                "scrollHeight": 240,
                "clientWidth": 600,
                "clientHeight": 240,
                "font_px": 24,
            }
        ]

        with mock.patch.object(
            self.audit, "_invoke_browser_worker", return_value=payload
        ):
            report = self.audit.run_poster_dom_audit(
                run,
                attempt,
                cache_root=self.root / "browser",
                allow_browser_install=False,
            )

        self.assertFalse(report["passed"], report)
        findings = {
            item["code"]: item
            for item in report["findings"]
            if item["code"]
            in {"poster-dom-text-clipping", "poster-dom-table-overflow"}
        }
        self.assertEqual(
            set(findings),
            {"poster-dom-text-clipping", "poster-dom-table-overflow"},
        )
        self.assertTrue(
            all(item["geometry"]["media"] == "print" for item in findings.values())
        )
        self.assertIn("screen_text_node_count", report["metrics"])
        self.assertIn("print_text_node_count", report["metrics"])

    def test_run_audit_rejects_symlinked_qa_without_outside_mutation(self) -> None:
        run, attempt, _artifact = self._run("symlink-qa")
        attempt_root = run / "attempts" / attempt
        outside = self.root / "outside-qa"
        outside.mkdir()
        marker = outside / "KEEP.txt"
        marker.write_bytes(b"keep")
        shutil.rmtree(attempt_root / "qa")
        (attempt_root / "qa").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(self.harness.core.PathSafetyError):
            self.audit.run_poster_dom_audit(
                run, attempt, cache_root=self.root / "browser", allow_browser_install=False
            )

        self.assertEqual(marker.read_bytes(), b"keep")
        self.assertEqual({path.name for path in outside.iterdir()}, {"KEEP.txt"})

    def test_run_audit_rejects_hardlinked_artifact_without_outside_mutation(self) -> None:
        run, attempt, artifact = self._run("hardlink-artifact")
        outside = self.root / "outside-poster.html"
        outside.write_bytes((artifact / "poster.html").read_bytes())
        (artifact / "poster.html").unlink()
        os.link(outside, artifact / "poster.html")
        before = outside.read_bytes()

        with self.assertRaises(self.harness.core.PathSafetyError):
            self.audit.run_poster_dom_audit(
                run, attempt, cache_root=self.root / "browser", allow_browser_install=False
            )

        self.assertEqual(outside.read_bytes(), before)
        self.assertFalse(
            (run / "attempts" / attempt / "qa" / "dom-audit.json").exists()
        )

    def test_run_audit_detects_artifact_drift_before_persisting_results(self) -> None:
        run, attempt, artifact = self._run("artifact-drift")
        payload = self._probe_payload()

        def drift(**_kwargs: object) -> dict[str, object]:
            (artifact / "poster.html").write_text("changed", encoding="utf-8")
            return payload

        with mock.patch.object(self.audit, "_invoke_browser_worker", side_effect=drift):
            with self.assertRaises(self.harness.core.IntegrityError):
                self.audit.run_poster_dom_audit(
                    run,
                    attempt,
                    cache_root=self.root / "browser",
                    allow_browser_install=False,
                )

        self.assertFalse(
            (run / "attempts" / attempt / "qa" / "dom-audit.json").exists()
        )
        self.assertFalse(
            (run / "attempts" / attempt / "qa" / "previews" / "dom-screen.png").exists()
        )

    def test_verified_report_rejects_later_artifact_drift(self) -> None:
        run, attempt, artifact = self._run("verified-drift")
        with mock.patch.object(
            self.audit, "_invoke_browser_worker", return_value=self._probe_payload()
        ):
            self.audit.run_poster_dom_audit(
                run, attempt, cache_root=self.root / "browser", allow_browser_install=False
            )
        (artifact / "poster.html").write_text("changed", encoding="utf-8")

        with self.assertRaises(self.harness.core.IntegrityError):
            self.audit.load_verified_poster_dom_audit(run, attempt)

    def test_loading_a_missing_report_does_not_create_qa_directories(self) -> None:
        run, attempt, _artifact = self._run("missing-report")
        qa = run / "attempts" / attempt / "qa"
        shutil.rmtree(qa)

        with self.assertRaises(self.harness.core.PathSafetyError):
            self.audit.load_verified_poster_dom_audit(run, attempt)

        self.assertFalse(qa.exists())


@unittest.skipUnless(
    os.environ.get("AUTODESIGN_SKILL_REAL_BROWSER") == "1",
    "set AUTODESIGN_SKILL_REAL_BROWSER=1 to run pinned Chromium DOM tests",
)
class PosterDomRealBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.audit = _load_dom_audit(self)
        self.harness = poster_skill_fixtures._load_harness()
        cache = os.environ.get("AUTODESIGN_SKILL_BROWSER_CACHE", "").strip()
        self.cache = Path(cache) if cache else Path.home() / ".cache/autodesign-skills/browser"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _html(body: str, *, extra_style: str = "", script: str = "") -> str:
        return textwrap.dedent(
            f"""\
            <!doctype html>
            <html><head><meta charset="utf-8"><style>
            @page {{ size: 2133.6mm 1066.8mm; margin: 0; }}
            * {{ box-sizing: border-box; }}
            html, body {{ margin: 0; width: 3072px; height: 1536px; overflow: hidden; }}
            body {{ font-family: Arial, sans-serif; background: white; color: #18202a; }}
            .paper-poster {{ position: relative; width: 3072px; height: 1536px; overflow: hidden; background: #fff; }}
            p, li, td, th {{ font-size: 24px; line-height: 1.25; }}
            @media print {{
              html, body, .paper-poster {{ width: 3072px; height: 1536px; }}
            }}
            {extra_style}
            </style></head><body>
            <main class="paper-poster" data-autodesign-artifact="poster"
                  data-block-id="paper-poster-root" data-canvas-width="3072"
                  data-canvas-height="1536" data-print-width-mm="2133.6"
                  data-print-height-mm="1066.8">{body}</main>
            {script}
            </body></html>
            """
        )

    def _run(self, name: str, html: str) -> tuple[dict[str, object], bytes]:
        runner = PosterDomRunnerTests(methodName="runTest")
        runner.root = self.root
        runner.audit = self.audit
        runner.harness = self.harness
        run, attempt, artifact = runner._run(name)
        poster = artifact / "poster.html"
        poster.write_text(html, encoding="utf-8")
        before = poster.read_bytes()
        report = self.audit.run_poster_dom_audit(
            run,
            attempt,
            cache_root=self.cache,
            allow_browser_install=False,
        )
        self.assertEqual(poster.read_bytes(), before)
        self.assertEqual(poster.stat().st_nlink, 1)
        self.last_run = run
        self.last_attempt = attempt
        return report, before

    @staticmethod
    def _codes(report: dict[str, object]) -> set[str]:
        return {str(item["code"]) for item in report["findings"]}

    def test_valid_dense_poster_passes_and_binds_screen_and_print(self) -> None:
        paragraphs = "".join(
            f'<p data-block-id="copy-{index}" style="position:absolute;left:{80 + (index % 3) * 980}px;'
            f'top:{80 + (index // 3) * 170}px;width:850px;height:120px">'
            "Grounded methods, measured results, and bounded conclusions remain editable. "
            "Each source-backed block carries enough detail for a dense conference poster.</p>"
            for index in range(24)
        )
        report, _before = self._run("real-valid", self._html(paragraphs))

        self.assertTrue(report["passed"], report)
        self.assertEqual(report["findings"], [])
        self.assertTrue(report["artifact_unchanged"])
        self.assertEqual(
            {
                label: item["path"]
                for label, item in report["screenshots"].items()
            },
            {
                "screen": "qa/previews/dom-screen.png",
                "print": "qa/previews/dom-print.png",
            },
        )
        for item in report["screenshots"].values():
            self.assertRegex(str(item["sha256"]), r"^[0-9a-f]{64}$")
        attempt_root = self.last_run / "attempts" / self.last_attempt
        self.harness.core.write_source_map(
            self.last_run,
            self.last_attempt,
            poster_skill_fixtures._claims(),
        )
        (attempt_root / "artifact" / "poster.pdf").write_bytes(b"%PDF-fixture")
        (attempt_root / "artifact" / "preview.png").write_bytes(b"print-preview")
        (attempt_root / "qa" / "previews" / "poster-print.png").write_bytes(
            b"print-preview"
        )
        report = self.audit.run_poster_dom_audit(
            self.last_run,
            self.last_attempt,
            cache_root=self.cache,
            allow_browser_install=False,
        )
        self.assertEqual(
            self.audit.load_verified_poster_dom_audit(
                self.last_run, self.last_attempt
            ),
            report,
        )
        self.harness.core.record_deterministic_result(
            self.last_run,
            self.last_attempt,
            passed=True,
            checks=[{"id": "poster_dom_audit", "passed": True}],
            artifact_paths=[
                "artifact/poster.html",
                "artifact/poster.pdf",
                "artifact/preview.png",
            ],
            preview_paths={
                "poster_screen": "qa/previews/dom-screen.png",
                "poster_pdf": "qa/previews/poster-print.png",
            },
        )
        context = self.harness.create_poster_review_context(
            self.last_run, self.last_attempt
        )
        self.assertEqual(set(context["preview_hashes"]), {"poster_pdf", "poster_screen"})
        self.assertEqual(
            context["preview_hashes"]["poster_screen"],
            self.harness.core.sha256_file(
                attempt_root / "qa" / "previews" / "dom-screen.png"
            ),
        )
        self.assertEqual(
            context["preview_hashes"]["poster_pdf"],
            self.harness.core.sha256_file(
                attempt_root / "qa" / "previews" / "poster-print.png"
            ),
        )

    def test_primary_canvas_background_is_checked_in_screen_and_print(self) -> None:
        body = "".join(
            f'<p data-block-id="copy-{index}" style="position:absolute;left:{80 + (index % 3) * 980}px;'
            f'top:{80 + (index // 3) * 170}px;width:850px;height:120px">'
            "Grounded methods and evidence fill this conference poster.</p>"
            for index in range(24)
        )
        tinted, _before = self._run(
            "real-tinted-canvas",
            self._html(body, extra_style=".paper-poster { background: #f4f7fb; }"),
        )
        print_gradient, _before = self._run(
            "real-print-gradient-canvas",
            self._html(
                body,
                extra_style=(
                    "@media print { .paper-poster { background: #fff; "
                    "background-image: linear-gradient(#fff, #eef2f7); } }"
                ),
            ),
        )
        filtered, _before = self._run(
            "real-filtered-canvas",
            self._html(
                body,
                extra_style=(
                    "body { background: #000; } "
                    ".paper-poster { filter: opacity(.5); }"
                ),
            ),
        )
        translucent, _before = self._run(
            "real-translucent-canvas",
            self._html(
                body,
                extra_style=(
                    "body { background: #000; } "
                    ".paper-poster { opacity: .5; }"
                ),
            ),
        )

        tinted_findings = [
            item
            for item in tinted["findings"]
            if item["code"] == "poster-dom-canvas-background"
        ]
        gradient_findings = [
            item
            for item in print_gradient["findings"]
            if item["code"] == "poster-dom-canvas-background"
        ]
        filtered_findings = [
            item
            for item in filtered["findings"]
            if item["code"] == "poster-dom-canvas-background"
        ]
        translucent_findings = [
            item
            for item in translucent["findings"]
            if item["code"] == "poster-dom-canvas-background"
        ]
        self.assertEqual(
            {item["geometry"]["media"] for item in tinted_findings},
            {"screen", "print"},
        )
        self.assertEqual(
            {item["geometry"]["media"] for item in gradient_findings},
            {"print"},
        )
        self.assertEqual(
            {item["geometry"]["media"] for item in filtered_findings},
            {"screen", "print"},
        )
        self.assertEqual(
            {item["geometry"]["media"] for item in translucent_findings},
            {"screen", "print"},
        )
        self.assertTrue(tinted["artifact_unchanged"])
        self.assertTrue(print_gradient["artifact_unchanged"])
        self.assertTrue(filtered["artifact_unchanged"])
        self.assertTrue(translucent["artifact_unchanged"])

    def test_sparse_top_canvas_without_recognized_panels_fails_fill_checks(self) -> None:
        body = "".join(
            f'<p data-block-id="top-copy-{column}" style="position:absolute;left:{80 + column * 740}px;'
            'top:60px;width:660px;height:100px">Grounded content occupies only the top '
            "of this deliberately sparse conference poster.</p>"
            for column in range(4)
        )

        report, _before = self._run("real-sparse-top", self._html(body))

        self.assertEqual(report["metrics"]["screen_panel_count"], 0)
        self.assertEqual(report["metrics"]["print_panel_count"], 0)
        self.assertIn("poster-dom-blank-band", self._codes(report), report)
        self.assertIn("poster-dom-sparse-oversized-panel", self._codes(report), report)
        self.assertGreater(
            report["metrics"]["screen_canvas_lower_blank_ratio"], 0.75
        )

    def test_body_margin_offset_escapes_the_real_browser_canvas(self) -> None:
        report, _before = self._run(
            "real-body-margin",
            self._html(
                '<p data-block-id="copy">Grounded content remains editable.</p>',
                extra_style="body { margin-left: 12px; }",
            ),
        )

        escapes = [
            finding
            for finding in report["findings"]
            if finding["code"] == "poster-dom-viewport-escape"
            and finding["block_id"] == "paper-poster-root"
        ]
        self.assertTrue(escapes, report)
        self.assertTrue(
            all(finding["geometry"]["right_gap_px"] >= 12 for finding in escapes),
            escapes,
        )

    def test_print_only_clipping_is_labeled_and_blocks(self) -> None:
        report, _before = self._run(
            "real-print-only-clipping",
            self._html(
                '<div id="print-clip" data-block-id="print-clip" '
                'style="position:absolute;left:80px;top:80px;width:650px">'
                '<p style="margin:0">Grounded print-only text continues across several rendered '
                "lines and is clipped only after the print media style is applied to this "
                "editable authored container.</p></div>",
                extra_style="@media print { #print-clip { height: 38px; overflow: hidden; } }",
            ),
        )

        clipping = [
            finding
            for finding in report["findings"]
            if finding["code"] == "poster-dom-text-clipping"
        ]
        self.assertTrue(clipping, report)
        self.assertEqual(
            {finding["geometry"]["media"] for finding in clipping}, {"print"}
        )
        self.assertFalse(report["passed"])

    def test_required_rendered_defects_are_detected(self) -> None:
        transparent_png = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAF"
            "gAJ/l20ZKAAAAABJRU5ErkJggg=="
        )
        cases = {
            "poster-dom-text-clipping": self._html(
                '<div data-block-id="clip" style="position:absolute;left:80px;top:80px;'
                'width:700px;height:38px;overflow:hidden"><p style="margin:0">'
                "Grounded text continues across several rendered lines and must be visibly clipped "
                "inside this deliberately short authored container.</p></div>"
            ),
            "poster-dom-text-overlap": self._html(
                '<p data-block-id="left" style="position:absolute;left:80px;top:80px;width:700px">'
                "First grounded result overlaps another editable text block.</p>"
                '<p data-block-id="right" style="position:absolute;left:180px;top:90px;width:700px">'
                "Second grounded result overlaps the first editable text block.</p>"
            ),
            "poster-dom-blank-band": self._html(
                '<section data-block-id="blank-panel" style="position:absolute;left:80px;top:80px;'
                'width:900px;height:1000px"><p>Only one grounded line occupies this large panel.</p>'
                "</section>"
            ),
            "poster-dom-image-low-effective-resolution": self._html(
                f'<img data-block-id="low-res" src="{transparent_png}" alt="source result" '
                'style="position:absolute;left:80px;top:80px;width:600px;height:400px">'
            ),
            "poster-dom-table-overflow": self._html(
                '<div data-block-id="table-container" style="position:absolute;left:80px;top:80px;'
                'width:420px;overflow:hidden"><table data-block-id="wide-table" '
                'style="width:1000px"><tr><th>Grounded measure</th><th>Value</th></tr>'
                "<tr><td>Accuracy</td><td>85%</td></tr></table></div>"
            ),
            "poster-dom-source-flow-gutter": self._html(
                '<div class="source-flow-unit" data-block-id="method-flow" '
                'data-source-ids="ev-001"><figure data-source-id="vis-001" '
                'data-source-ids="ev-001" '
                'style="float:left;width:240px;height:180px"></figure>'
                '<ul data-block-id="method-readout" data-source-ids="ev-001" '
                'style="padding-inline-start:4px;text-indent:-4px"><li>Grounded method readout.</li>'
                "</ul></div>"
            ),
            "poster-dom-source-flow-sibling": self._html(
                '<div class="source-flow-unit" data-block-id="mismatched-flow">'
                '<figure data-source-id="vis-001" data-source-ids="ev-method" '
                'style="float:left;width:240px;height:180px"></figure>'
                '<div data-role="readout" data-source-id="vis-001" '
                'data-source-ids="ev-result">'
                "Grounded explanatory readout with mismatched evidence.</div></div>"
            ),
            "poster-dom-screen-print-mismatch": self._html(
                '<p data-block-id="copy">Grounded screen and print parity must hold.</p>',
                extra_style="@media print { .paper-poster { width: 2800px; height: 1536px; } }",
            ),
        }
        for expected, html in cases.items():
            with self.subTest(code=expected):
                report, _before = self._run(expected, html)
                self.assertIn(expected, self._codes(report), report)

    def test_network_popups_and_workers_are_blocked_with_sanitized_diagnostics(self) -> None:
        script = """
        <script>
        fetch('https://blocked.example/private?token=super-secret').catch(() => {});
        window.open('https://popup.example/private?token=super-secret');
        try { new Worker('https://worker.example/private.js?token=super-secret'); } catch (_) {}
        try {
          new Worker(URL.createObjectURL(new Blob(['self.postMessage("ready")'],
            {type: 'text/javascript'})));
        } catch (_) {}
        console.error('failed https://console.example/private?token=super-secret');
        </script>
        """
        report, _before = self._run(
            "blocked-browser-effects",
            self._html(
                '<p data-block-id="copy">Grounded browser diagnostics stay local.</p>',
                script=script,
            ),
        )

        diagnostics = report["browser_diagnostics"]
        self.assertTrue(diagnostics["blocked_requests"])
        self.assertTrue(diagnostics["blocked_popups"])
        self.assertTrue(diagnostics["blocked_workers"])
        serialized = json.dumps(diagnostics, sort_keys=True)
        self.assertNotIn("super-secret", serialized)
        self.assertNotIn(str(self.root), serialized)

    def test_boxiness_distinguishes_plain_white_flow_from_dark_filled_boxes(self) -> None:
        def boxes(background: str, color: str) -> str:
            return "".join(
                f'<div data-block-id="box-{index}" class="metric-unit" '
                f'style="position:absolute;left:{80 + (index % 5) * 560}px;'
                f'top:{80 + (index // 5) * 260}px;width:440px;height:180px;'
                f'background:{background};color:{color}">Grounded metric result</div>'
                for index in range(10)
            )

        white_report, _before = self._run(
            "plain-white-flow", self._html(boxes("rgb(255,255,255)", "#18202a"))
        )
        dark_report, _before = self._run(
            "dark-filled-boxes", self._html(boxes("rgb(0,0,0)", "#ffffff"))
        )

        self.assertNotIn("poster-dom-template-boxiness", self._codes(white_report))
        self.assertIn("poster-dom-template-boxiness", self._codes(dark_report))

    def test_measurement_script_contains_no_authored_dom_write_api(self) -> None:
        source = DOM_AUDIT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("import autodesign", source)
        self.assertNotIn("from autodesign", source)
        for forbidden in (
            "setAttribute(",
            "removeAttribute(",
            "appendChild(",
            "insertBefore(",
            "replaceChildren(",
            ".style =",
            ".style=",
            ".style.setProperty(",
            ".classList.add(",
            ".classList.remove(",
            "insertRule(",
            "auto_fit",
            "autofit",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
