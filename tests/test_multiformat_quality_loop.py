from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autodesign.designer import _should_stop_after_dogfood_blocking_composite
from autodesign.runner import _mark_dogfood_report_only_partial
from autodesign.schema import DesignSpec, ToolResultRecord
from autodesign.tools import ToolContext
from autodesign.tools.composite import _composite_deck_html_first
from autodesign.tools.deck_html_renderer import DeckHtmlRenderResult
from autodesign.tools.deck_html_renderer import _hydrate_deck_blocks
from autodesign.tools.deck_html_renderer import _layout_slide
from autodesign.tools.propose_design_spec import propose_design_spec
from autodesign.util.browser_render import BrowserRenderResult
from autodesign.util.html_artifact import audit_frame_layout_plan, html_artifact_to_deck_html
from autodesign.util.visual_reference_contract import (
    only_visual_reference_progression_findings,
)


class MultiFormatQualityLoopTest(unittest.TestCase):
    def test_source_id_image_hydrates_when_semantic_layer_id_does_not_match(self) -> None:
        spec = DesignSpec.model_validate(_deck_spec(block_count=1, with_source_image=True))
        deck = html_artifact_to_deck_html(spec.html_artifact).model_dump(mode="json")
        ctx = SimpleNamespace(state={
            "rendered_layers": {
                "ingest_fig_03": {
                    "src_path": "/tmp/source-figure.png",
                    "headers": ["Model", "Score"],
                    "rows": [["Ours", "91.2"]],
                },
            },
        })

        _hydrate_deck_blocks(deck, ctx)

        block = deck["slides"][0]["blocks"][0]
        self.assertEqual(block["src_path"], "/tmp/source-figure.png")
        self.assertEqual(block["block_id"], "method_visual")
        self.assertEqual(block["layer_id"], "method_visual")
        self.assertEqual(block["source_id"], "ingest_fig_03")

    def test_source_id_table_hydrates_none_rows_and_headers(self) -> None:
        spec_payload = _deck_spec(block_count=0)
        spec_payload["html_artifact"]["frames"][0]["blocks"] = [{
            "block_id": "results_table",
            "kind": "table",
            "role": "results_table",
            "source_id": "ingest_table_01",
        }]
        spec = DesignSpec.model_validate(spec_payload)
        deck = html_artifact_to_deck_html(spec.html_artifact).model_dump(mode="json")
        ctx = SimpleNamespace(state={
            "rendered_layers": {
                "ingest_table_01": {
                    "headers": ["Model", "Score"],
                    "rows": [["Ours", "91.2"]],
                },
            },
        })

        _hydrate_deck_blocks(deck, ctx)

        block = deck["slides"][0]["blocks"][0]
        self.assertEqual(block["headers"], ["Model", "Score"])
        self.assertEqual(block["rows"], [["Ours", "91.2"]])

    def test_empty_layer_record_does_not_mask_source_hydration(self) -> None:
        spec_payload = _deck_spec(block_count=0)
        spec_payload["html_artifact"]["frames"][0]["blocks"] = [{
            "block_id": "method_visual",
            "layer_id": "semantic_visual",
            "source_id": "ingest_fig_03",
            "kind": "image",
        }]
        spec = DesignSpec.model_validate(spec_payload)
        deck = html_artifact_to_deck_html(spec.html_artifact).model_dump(mode="json")
        ctx = SimpleNamespace(state={"rendered_layers": {
            "semantic_visual": {},
            "ingest_fig_03": {"src_path": "/tmp/source-figure.png"},
        }})

        _hydrate_deck_blocks(deck, ctx)

        self.assertEqual(deck["slides"][0]["blocks"][0]["src_path"], "/tmp/source-figure.png")

    def test_deck_html_to_html_artifact_preserves_layout_plan(self) -> None:
        spec_payload = _deck_spec(block_count=0)
        spec_payload.pop("html_artifact")
        spec_payload["deck_html"] = {
            "slides": [{
                "slide_id": "slide_01",
                "layout": "editorial_split",
                "layout_plan": {
                    "archetype": "editorial_split",
                    "slots": [{
                        "slot_id": "main",
                        "role": "content",
                        "bbox": {"x": 100, "y": 100, "w": 1720, "h": 880},
                    }],
                },
                "blocks": [],
            }],
        }
        spec = DesignSpec.model_validate(spec_payload)
        from autodesign.util.html_artifact import canonicalize_design_spec

        canonicalize_design_spec(spec)

        self.assertEqual(
            spec.html_artifact.frames[0].layout_plan.slots[0].slot_id,
            "main",
        )

    def test_deck_conversion_preserves_layout_plan_and_renderer_uses_slots(self) -> None:
        spec_payload = _deck_spec(block_count=0)
        frame = spec_payload["html_artifact"]["frames"][0]
        frame["layout_plan"] = {
            "archetype": "editorial_split",
            "slots": [
                {
                    "slot_id": "title",
                    "role": "title",
                    "bbox": {"x": 100, "y": 80, "w": 1720, "h": 120},
                },
                {
                    "slot_id": "visual",
                    "role": "figure",
                    "bbox": {"x": 100, "y": 240, "w": 1720, "h": 720},
                },
            ],
        }
        frame["blocks"] = [
            {
                "block_id": "title_block",
                "kind": "text",
                "role": "title",
                "slot_id": "title",
                "text": "A source-grounded result",
            },
            {
                "block_id": "visual_block",
                "kind": "image",
                "role": "result_figure",
                "slot_id": "visual",
                "src_path": "/tmp/result.png",
            },
        ]
        spec = DesignSpec.model_validate(spec_payload)
        deck = html_artifact_to_deck_html(spec.html_artifact).model_dump(mode="json")

        placements = _layout_slide(
            deck["slides"][0],
            slide_id="slide_01",
            layout="editorial_split",
            slide_w=1920,
            slide_h=1080,
            theme={"display_font": "Inter", "body_font": "Inter", "ink": "#111111"},
        )

        self.assertEqual(deck["slides"][0]["layout_plan"]["slots"][1]["slot_id"], "visual")
        by_id = {placement.block_id: placement for placement in placements}
        self.assertEqual(by_id["title_block"].bbox, {"x": 100, "y": 80, "w": 1720, "h": 120})
        self.assertEqual(by_id["visual_block"].bbox, {"x": 100, "y": 240, "w": 1720, "h": 720})

    def test_multiple_blocks_in_one_layout_slot_do_not_overlap(self) -> None:
        slide = {
            "slide_id": "slide_01",
            "layout": "editorial_split",
            "layout_plan": {
                "archetype": "editorial_split",
                "slots": [{
                    "slot_id": "evidence",
                    "role": "evidence",
                    "bbox": {"x": 100, "y": 160, "w": 1720, "h": 760},
                }],
            },
            "blocks": [
                {
                    "block_id": "evidence_figure",
                    "kind": "image",
                    "role": "result_figure",
                    "slot_id": "evidence",
                    "src_path": "/tmp/result.png",
                },
                {
                    "block_id": "evidence_readout",
                    "kind": "text",
                    "role": "evidence_readout",
                    "slot_id": "evidence",
                    "text": "The figure reports the central result across benchmark conditions.",
                },
            ],
        }

        placements = _layout_slide(
            slide,
            slide_id="slide_01",
            layout="editorial_split",
            slide_w=1920,
            slide_h=1080,
            theme={"display_font": "Inter", "body_font": "Inter", "ink": "#111111"},
        )

        self.assertEqual(len(placements), 2)
        figure, readout = placements
        self.assertLessEqual(figure.bbox["y"] + figure.bbox["h"], readout.bbox["y"])
        self.assertLessEqual(readout.bbox["y"] + readout.bbox["h"], 920)

    def test_declared_slot_bbox_overrides_block_bbox(self) -> None:
        slide = {
            "slide_id": "slide_01",
            "layout": "editorial_split",
            "layout_plan": {
                "archetype": "editorial_split",
                "slots": [{
                    "slot_id": "main",
                    "role": "content",
                    "bbox": {"x": 100, "y": 160, "w": 800, "h": 700},
                }],
            },
            "blocks": [{
                "block_id": "body",
                "kind": "text",
                "role": "body",
                "slot_id": "main",
                "bbox": {"x": 2500, "y": 2500, "w": 100, "h": 100},
                "text": "The slot plan is authoritative.",
            }],
        }

        placements = _layout_slide(
            slide,
            slide_id="slide_01",
            layout="editorial_split",
            slide_w=1920,
            slide_h=1080,
            theme={"display_font": "Inter", "body_font": "Inter", "ink": "#111111"},
        )

        self.assertEqual(placements[0].bbox, {"x": 100, "y": 160, "w": 800, "h": 700})

    def test_complex_deck_frame_missing_layout_plan_is_rejected_at_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx = ToolContext(
                settings=SimpleNamespace(poster_harness_mode="cheap"),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id="deck-layout-plan",
            )
            ctx.layers_dir.mkdir()
            result = propose_design_spec({
                "design_spec": _deck_spec(block_count=6),
            }, ctx=ctx)

        self.assertEqual(result.status, "error")
        self.assertEqual(result.payload.get("issue_id"), "frame_layout_missing_plan")
        self.assertEqual(
            (result.payload.get("frame_layout_findings") or [{}])[0].get("id"),
            "frame_layout_missing_plan",
        )
        self.assertIsNone(ctx.state.get("design_spec"))
        self.assertNotIn("spec_revision_count", ctx.state)

    def test_small_deck_frame_without_layout_plan_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx = ToolContext(
                settings=SimpleNamespace(poster_harness_mode="cheap"),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id="small-deck",
            )
            ctx.layers_dir.mkdir()
            result = propose_design_spec({
                "design_spec": _deck_spec(block_count=5),
            }, ctx=ctx)

        self.assertEqual(result.status, "ok", result.error_message)

    def test_legacy_deck_frame_without_layout_plan_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx = _tool_context(run_dir, run_id="legacy-deck")
            spec = _deck_spec(block_count=6)
            spec["html_artifact"]["theme"] = {"_autodesign_legacy_source": "deck_html"}
            result = propose_design_spec({"design_spec": spec}, ctx=ctx)

        self.assertEqual(result.status, "ok", result.error_message)

    def test_complex_deck_frame_with_layout_plan_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx = _tool_context(run_dir, run_id="planned-deck")
            spec = _deck_spec(block_count=6)
            spec["html_artifact"]["frames"][0]["layout_plan"] = {
                "archetype": "editorial_split",
                "slots": [
                    {
                        "slot_id": "main",
                        "role": "content",
                        "bbox": {"x": 96, "y": 120, "w": 1728, "h": 840},
                    },
                ],
            }
            for block in spec["html_artifact"]["frames"][0]["blocks"]:
                block["slot_id"] = "main"
            result = propose_design_spec({"design_spec": spec}, ctx=ctx)

        self.assertEqual(result.status, "ok", result.error_message)

    def test_empty_layout_slots_are_rejected_before_state_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx = _tool_context(run_dir, run_id="empty-layout-slots")
            spec = _deck_spec(block_count=6)
            spec["html_artifact"]["frames"][0]["layout_plan"] = {
                "archetype": "editorial_split",
                "slots": [],
            }

            result = propose_design_spec({"design_spec": spec}, ctx=ctx)

        self.assertEqual(result.status, "error")
        self.assertEqual(result.payload.get("issue_id"), "frame_layout_missing_plan")
        self.assertIsNone(ctx.state.get("design_spec"))
        self.assertNotIn("spec_revision_count", ctx.state)

    def test_direct_blocks_with_declared_slot_ids_are_not_free_floating(self) -> None:
        spec = _deck_spec(block_count=6)
        frame = spec["html_artifact"]["frames"][0]
        frame["layout_plan"] = {
            "archetype": "editorial_split",
            "slots": [{
                "slot_id": "main",
                "role": "content",
                "bbox": {"x": 96, "y": 120, "w": 1728, "h": 840},
            }],
        }
        for block in frame["blocks"]:
            block["slot_id"] = "main"

        findings = audit_frame_layout_plan(spec["html_artifact"], artifact_type="deck")

        self.assertNotIn("frame_layout_free_floating_blocks", {f.get("id") for f in findings})

    def test_any_unwired_substantive_deck_block_is_rejected(self) -> None:
        spec = _deck_spec(block_count=6)
        frame = spec["html_artifact"]["frames"][0]
        frame["layout_plan"] = {
            "archetype": "editorial_split",
            "slots": [{
                "slot_id": "main",
                "role": "content",
                "bbox": {"x": 96, "y": 120, "w": 1728, "h": 840},
            }],
        }
        for block in frame["blocks"][:-1]:
            block["slot_id"] = "main"

        findings = audit_frame_layout_plan(spec["html_artifact"], artifact_type="deck")

        self.assertIn("frame_layout_free_floating_blocks", {f.get("id") for f in findings})

    def test_visual_readout_in_same_declared_slot_satisfies_caption_contract(self) -> None:
        spec = _deck_spec(block_count=0)
        frame = spec["html_artifact"]["frames"][0]
        frame["layout_plan"] = {
            "archetype": "editorial_split",
            "slots": [{
                "slot_id": "evidence",
                "role": "evidence",
                "bbox": {"x": 96, "y": 120, "w": 1728, "h": 840},
            }],
        }
        frame["blocks"] = [
            {
                "block_id": "result_figure",
                "kind": "image",
                "role": "result_figure",
                "slot_id": "evidence",
            },
            {
                "block_id": "result_readout",
                "kind": "text",
                "role": "body",
                "slot_id": "evidence",
                "text": "The source figure shows the reported performance gain across all benchmarks.",
            },
        ]

        findings = audit_frame_layout_plan(spec["html_artifact"], artifact_type="deck")

        self.assertNotIn("frame_layout_missing_caption", {f.get("id") for f in findings})

    def test_normalized_deck_slot_bbox_is_rejected_at_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx = _tool_context(run_dir, run_id="normalized-layout-slots")
            spec = _deck_spec(block_count=6)
            frame = spec["html_artifact"]["frames"][0]
            frame["layout_plan"] = {
                "archetype": "editorial_split",
                "slots": [{
                    "slot_id": "main",
                    "role": "content",
                    "bbox": {"x": 0, "y": 0, "w": 1, "h": 1},
                }],
            }
            for block in frame["blocks"]:
                block["slot_id"] = "main"

            result = propose_design_spec({"design_spec": spec}, ctx=ctx)

        self.assertEqual(result.status, "error")
        self.assertEqual(result.payload.get("issue_id"), "frame_layout_slot_bbox_too_small")
        self.assertIsNone(ctx.state.get("design_spec"))
        self.assertNotIn("spec_revision_count", ctx.state)

    def test_duplicate_and_off_canvas_deck_slots_are_rejected(self) -> None:
        duplicate = _planned_deck_spec()
        duplicate["html_artifact"]["frames"][0]["layout_plan"]["slots"].append({
            "slot_id": "main",
            "role": "duplicate",
            "bbox": {"x": 100, "y": 100, "w": 500, "h": 500},
        })
        off_canvas = _planned_deck_spec()
        off_canvas["html_artifact"]["frames"][0]["layout_plan"]["slots"][0]["bbox"] = {
            "x": 1800,
            "y": 120,
            "w": 400,
            "h": 840,
        }

        duplicate_ids = {
            item.get("id")
            for item in audit_frame_layout_plan(duplicate["html_artifact"], artifact_type="deck")
        }
        off_canvas_ids = {
            item.get("id")
            for item in audit_frame_layout_plan(off_canvas["html_artifact"], artifact_type="deck")
        }

        self.assertIn("frame_layout_duplicate_slot_id", duplicate_ids)
        self.assertIn("frame_layout_slot_out_of_canvas", off_canvas_ids)

    def test_nested_duplicate_and_off_canvas_deck_slots_are_rejected(self) -> None:
        spec = _planned_deck_spec()
        slots = spec["html_artifact"]["frames"][0]["layout_plan"]["slots"]
        slots.extend([{
            "slot_id": "main",
            "role": "nested_duplicate",
            "parent_slot_id": "visual",
            "bbox": {"x": 100, "y": 100, "w": 300, "h": 300},
        }, {
            "slot_id": "nested_off_canvas",
            "role": "nested_visual",
            "parent_slot_id": "visual",
            "bbox": {"x": 1900, "y": 100, "w": 200, "h": 300},
        }])

        finding_ids = {
            item.get("id")
            for item in audit_frame_layout_plan(spec["html_artifact"], artifact_type="deck")
        }

        self.assertIn("frame_layout_duplicate_slot_id", finding_ids)
        self.assertIn("frame_layout_slot_out_of_canvas", finding_ids)

    def test_visible_authoring_intent_is_rejected_at_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx = _tool_context(run_dir, run_id="visible-authoring-intent")
            spec = _planned_deck_spec()
            spec["html_artifact"]["frames"][0]["blocks"].append({
                "block_id": "visible_intent",
                "kind": "text",
                "role": "speaker_note_intent",
                "slot_id": "main",
                "text": "Intent: tell the audience what this slide should accomplish.",
            })

            result = propose_design_spec({"design_spec": spec}, ctx=ctx)

        self.assertEqual(result.status, "error")
        self.assertEqual(result.payload.get("issue_id"), "frame_layout_visible_authoring_note")
        self.assertIsNone(ctx.state.get("design_spec"))

    def test_short_deck_frame_with_visible_intent_prefix_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx = _tool_context(run_dir, run_id="short-visible-intent")
            spec = _deck_spec(block_count=1)
            spec["html_artifact"]["frames"][0]["blocks"][0].update({
                "role": "body",
                "text": "Intent: explain why this slide exists.",
            })

            result = propose_design_spec({"design_spec": spec}, ctx=ctx)

        self.assertEqual(result.status, "error")
        self.assertEqual(result.payload.get("issue_id"), "frame_layout_visible_authoring_note")

    def test_visible_authoring_intent_in_title_or_items_is_rejected(self) -> None:
        for field, value in (("title", "Intent: explain the evidence."), ("items", ["Design note: use a chart."])):
            with self.subTest(field=field):
                spec = _deck_spec(block_count=1)
                block = spec["html_artifact"]["frames"][0]["blocks"][0]
                block["text"] = "Grounded content"
                block[field] = value
                finding_ids = {
                    item.get("id")
                    for item in audit_frame_layout_plan(spec["html_artifact"], artifact_type="deck")
                }
                self.assertIn("frame_layout_visible_authoring_note", finding_ids)

    def test_ordinal_only_metric_card_is_rejected_at_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx = _tool_context(run_dir, run_id="empty-metric-card")
            spec = _planned_deck_spec()
            spec["html_artifact"]["frames"][0]["blocks"].append({
                "block_id": "takeaway_1",
                "kind": "metric",
                "role": "takeaway_card",
                "slot_id": "main",
                "text": "1",
            })

            result = propose_design_spec({"design_spec": spec}, ctx=ctx)

        self.assertEqual(result.status, "error")
        self.assertEqual(result.payload.get("issue_id"), "frame_layout_empty_metric_card")
        self.assertIsNone(ctx.state.get("design_spec"))

    def test_text_takeaway_card_with_only_ordinal_is_rejected(self) -> None:
        spec = _deck_spec(block_count=1)
        spec["html_artifact"]["frames"][0]["blocks"][0].update({
            "kind": "text",
            "role": "takeaway_card",
            "text": "2",
        })

        finding_ids = {
            item.get("id")
            for item in audit_frame_layout_plan(spec["html_artifact"], artifact_type="deck")
        }

        self.assertIn("frame_layout_empty_metric_card", finding_ids)

    def test_takeaway_card_with_ordinal_only_title_or_items_is_rejected(self) -> None:
        for field, value in (("title", "1"), ("items", ["2"])):
            with self.subTest(field=field):
                spec = _deck_spec(block_count=1)
                block = spec["html_artifact"]["frames"][0]["blocks"][0]
                block.update({"kind": "text", "role": "takeaway_card", "text": None, field: value})
                finding_ids = {
                    item.get("id")
                    for item in audit_frame_layout_plan(spec["html_artifact"], artifact_type="deck")
                }
                self.assertIn("frame_layout_empty_metric_card", finding_ids)

    def test_only_visual_reference_progression_is_actionable(self) -> None:
        progression = [{
            "id": "visual_reference:visual-reference-revision-required",
            "source": "visual_reference",
        }]
        mixed = [*progression, {"id": "deck_layout:deck_low_visual_area", "source": "deck_layout"}]

        self.assertTrue(only_visual_reference_progression_findings(progression))
        self.assertFalse(only_visual_reference_progression_findings(mixed))

    def test_visual_reference_attempt_failure_is_not_progression(self) -> None:
        self.assertFalse(only_visual_reference_progression_findings([{
            "id": "visual_reference:visual-reference-attempt-failed",
            "source": "visual_reference",
        }]))

    def test_designer_continues_for_visual_reference_progression_and_clears_stale_marker(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            ctx = _tool_context(Path(raw_tmp), run_id="designer-progression")
            ctx.state["dogfood_blocking_composite_report_only"] = {"stale": True}
            result = ToolResultRecord.model_validate({
                "status": "ok",
                "payload": {"design_feedback": _progression_feedback()},
            })

            should_stop = _should_stop_after_dogfood_blocking_composite("composite", result, ctx)

        self.assertFalse(should_stop)
        self.assertNotIn("dogfood_blocking_composite_report_only", ctx.state)
        self.assertTrue(ctx.state["designer_blocking_composite_feedback"]["continue_for_repair"])
        self.assertTrue(ctx.state["designer_blocking_composite_feedback"]["visual_reference_progression"])

    def test_designer_continues_for_repairable_deck_layout_with_bounded_budget(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            ctx = _tool_context(Path(raw_tmp), run_id="deck-layout-repair")
            ctx.state["artifact_type"] = "deck"
            result = ToolResultRecord.model_validate({
                "status": "ok",
                "payload": {"design_feedback": _deck_layout_feedback()},
            })

            first_stop = _should_stop_after_dogfood_blocking_composite("composite", result, ctx)
            second_stop = _should_stop_after_dogfood_blocking_composite("composite", result, ctx)
            third_stop = _should_stop_after_dogfood_blocking_composite("composite", result, ctx)

        self.assertFalse(first_stop)
        self.assertFalse(second_stop)
        self.assertTrue(third_stop)
        self.assertEqual(ctx.state["deck_layout_designer_repair_attempts"], 2)
        self.assertIn("dogfood_blocking_composite_report_only", ctx.state)

    def test_mixed_deck_and_system_blockers_remain_report_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            ctx = _tool_context(Path(raw_tmp), run_id="mixed-deck-layout-repair")
            ctx.state["artifact_type"] = "deck"
            feedback = _deck_layout_feedback()
            feedback["findings"].append({
                "id": "quality_lint:unsafe-output",
                "source": "quality_lint",
                "severity": "blocker",
                "artifact_type": "deck",
                "message": "Unsafe output must be handled by the system repair path.",
                "suggested_action": "Stop and repair the system output.",
                "repairable": False,
            })
            result = ToolResultRecord.model_validate({
                "status": "ok",
                "payload": {"design_feedback": feedback},
            })

            should_stop = _should_stop_after_dogfood_blocking_composite("composite", result, ctx)

        self.assertTrue(should_stop)
        self.assertNotIn("deck_layout_designer_repair_attempts", ctx.state)

    def test_runner_does_not_mark_progression_only_feedback_report_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            ctx = _tool_context(Path(raw_tmp), run_id="runner-progression")
            ctx.state["dogfood_blocking_composite_report_only"] = {"stale": True}
            ctx.state["last_composite_payload"] = {
                "preview_sha256": "abc",
                "design_feedback": _progression_feedback(),
            }

            _mark_dogfood_report_only_partial(ctx)

        self.assertNotIn("dogfood_blocking_composite_report_only", ctx.state)

    def test_deck_composite_marks_visual_reference_revision_before_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp)
            ctx = _tool_context(run_dir, run_id="composited-revision")
            spec_payload = _deck_spec(block_count=1, with_source_image=True)
            frame = spec_payload["html_artifact"]["frames"][0]
            spec_payload["html_artifact"]["frames"] = [
                {**frame, "frame_id": f"slide_{idx:02d}"}
                for idx in range(1, 13)
            ]
            spec = DesignSpec.model_validate(spec_payload)
            spec.deck_html = html_artifact_to_deck_html(spec.html_artifact)
            ctx.state.update({
                "artifact_type": "deck",
                "design_spec": spec,
                "run_brief": "Create a substantial 12-slide academic paper deck.",
                "visual_reference_attempted": True,
                "visual_reference_status": "success",
                "visual_reference_revision_required": False,
                "visual_reference_revision_spec_revision": 1,
                "visual_reference_revision_composited": False,
                "spec_revision_count": 1,
            })

            def fake_write(_spec, out_path, _ctx):
                out_path.write_text("<html><body>deck</body></html>", encoding="utf-8")
                return DeckHtmlRenderResult(
                    slide_count=12,
                    slide_ids=[f"slide_{idx:02d}" for idx in range(1, 13)],
                    stats={},
                )

            def fake_screenshots(_html_path, slides_dir, **_kwargs):
                paths = []
                for idx in range(12):
                    path = slides_dir / f"slide_{idx:02d}.png"
                    _write_test_png(path)
                    paths.append(path)
                return BrowserRenderResult(backend="test", paths=paths)

            def fake_preview(_paths, preview_path):
                _write_test_png(preview_path)

            def fake_pdf(_html_path, pdf_path, **_kwargs):
                pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
                return BrowserRenderResult(backend="test")

            with (
                patch("autodesign.tools.composite.write_html_first_deck", side_effect=fake_write),
                patch("autodesign.tools.composite.screenshot_deck_slides", side_effect=fake_screenshots),
                patch("autodesign.tools.composite.build_deck_preview_grid", side_effect=fake_preview),
                patch("autodesign.tools.composite.export_deck_pdf", side_effect=fake_pdf),
                patch("autodesign.tools.composite.audit_deck_html_layout", return_value=[]),
                patch("autodesign.tools.composite._lint_composite_html", return_value={
                    "quality_lint_findings": [],
                    "quality_lint_p0_count": 0,
                }),
            ):
                result = _composite_deck_html_first(spec, ctx)

        self.assertEqual(result.status, "ok", result.error_message)
        self.assertTrue(ctx.state["visual_reference_revision_composited"])
        finding_ids = {
            finding.get("id")
            for finding in result.payload["design_feedback"]["findings"]
        }
        self.assertFalse(any(
            str(finding_id).endswith(":visual-reference-revision-not-composited")
            for finding_id in finding_ids
        ))


def _deck_spec(*, block_count: int, with_source_image: bool = False) -> dict:
    if with_source_image:
        blocks = [{
            "block_id": "method_visual",
            "layer_id": "method_visual",
            "source_id": "ingest_fig_03",
            "kind": "image",
            "role": "method_figure",
        }]
    else:
        blocks = [{
            "block_id": f"block_{idx:02d}",
            "kind": "text",
            "text": f"Source-grounded point {idx}",
        } for idx in range(block_count)]
    return {
        "brief": "Create a 12-slide academic deck.",
        "artifact_type": "deck",
        "canvas": {"w_px": 1920, "h_px": 1080},
        "html_artifact": {
            "target": "deck",
            "theme": {},
            "frames": [{
                "frame_id": "slide_01",
                "kind": "slide",
                "role": "method",
                "blocks": blocks,
            }],
        },
    }


def _planned_deck_spec() -> dict:
    spec = _deck_spec(block_count=6)
    frame = spec["html_artifact"]["frames"][0]
    frame["layout_plan"] = {
        "archetype": "editorial_split",
        "slots": [{
            "slot_id": "main",
            "role": "content",
            "bbox": {"x": 96, "y": 120, "w": 1728, "h": 840},
        }],
    }
    for block in frame["blocks"]:
        block["slot_id"] = "main"
    return spec


def _tool_context(run_dir: Path, *, run_id: str) -> ToolContext:
    ctx = ToolContext(
        settings=SimpleNamespace(poster_harness_mode="dogfood"),
        run_dir=run_dir,
        layers_dir=run_dir / "layers",
        run_id=run_id,
    )
    ctx.layers_dir.mkdir(exist_ok=True)
    return ctx


def _progression_feedback() -> dict:
    return {
        "artifact_type": "deck",
        "iteration": 1,
        "findings": [{
            "id": "visual_reference:visual-reference-revision-required",
            "source": "visual_reference",
            "severity": "blocker",
            "artifact_type": "deck",
            "message": "Revise the deck after visual-reference review.",
            "suggested_action": "Revise and composite again.",
        }],
        "has_blocking_findings": True,
    }


def _deck_layout_feedback() -> dict:
    return {
        "artifact_type": "deck",
        "iteration": 1,
        "findings": [{
            "id": "deck_layout:deck_repeated_layout",
            "source": "deck_layout",
            "severity": "blocker",
            "artifact_type": "deck",
            "message": "Three or more consecutive slides use the same layout.",
            "suggested_action": "Revise the deck rhythm with varied slide layouts.",
            "repairable": True,
        }],
        "has_blocking_findings": True,
    }


def _write_test_png(path: Path) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 9), "white").save(path)


if __name__ == "__main__":
    unittest.main()
