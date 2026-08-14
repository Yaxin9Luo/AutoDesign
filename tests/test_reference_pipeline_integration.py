from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from autodesign.agents.external_designer_author import (
    ExternalDesignerAuthor,
    _attempt_critic_design_spec,
    _direct_canvas,
    _write_author_quick_brief,
)
from autodesign.agents.reference_style_agent import (
    _REFERENCE_STYLE_MAX_ATTEMPTS,
    _reference_style_prompt,
)
from autodesign.util.io import sha256_file
from autodesign.util.reference_style_audit import audit_reference_style_artifacts


def test_reference_prompt_uses_active_canvas(tmp_path: Path) -> None:
    prompt = _reference_style_prompt(
        tmp_path,
        {
            "canvas_contract": {
                "w_px": 1440,
                "h_px": 960,
                "aspect_ratio": "3:2",
            }
        },
        model_hint="",
        runtime_skill={
            "resources": [{
                "id": "output_contract_v4",
                "path": "references/output_contract_v4.md",
            }],
        },
    )

    assert "1440x960" in prompt
    assert "3:2" in prompt
    assert "3072x1536" not in prompt


def test_quick_brief_reads_nested_canvas_plan(tmp_path: Path) -> None:
    attempt_dir = tmp_path / "attempt_01"
    attempt_dir.mkdir()
    ctx = SimpleNamespace(
        run_dir=tmp_path,
        state={
            "canvas_plan": {
                "preset_id": "reference-poster",
                "canvas": {
                    "w_px": 1440,
                    "h_px": 960,
                    "dpi": 96,
                    "aspect_ratio": "3:2",
                },
            },
            "poster_content_brief": {"title": "Paper"},
            "poster_plan_contract": {
                "required_source_visual_ids": ["ingest_fig_01", "ingest_fig_02"],
                "selected_visuals": [
                    {"layer_id": "ingest_fig_01", "caption_short": "Method", "output_file": "layers/a.png"},
                    {"layer_id": "ingest_fig_02", "caption_short": "Results", "output_file": "layers/b.png"},
                ],
            },
            "paper_visual_storyboard": {},
        },
    )

    assert _write_author_quick_brief(ctx, attempt_dir, "Generate a poster")
    quick_brief = (attempt_dir / "author_quick_brief.md").read_text(encoding="utf-8")
    assert '"w_px": 1440' in quick_brief
    assert '"h_px": 960' in quick_brief
    assert '"aspect_ratio": "3:2"' in quick_brief
    assert "## Required Source Visuals for Attempt 1" in quick_brief
    assert "ingest_fig_01 | Method | layers/a.png" in quick_brief
    assert "ingest_fig_02 | Results | layers/b.png" in quick_brief
    assert "Visual-first composition" in quick_brief

    prompt = ExternalDesignerAuthor(SimpleNamespace(designer_author_model=""), "")._build_prompt(
        ctx,
        brief="Generate a poster",
        attempt_dir=attempt_dir,
        attempt_index=1,
        max_attempts=1,
    )
    assert "ingest_fig_01, ingest_fig_02" in prompt
    assert "hard evidence requirements" in prompt
    assert "visual-first composition" in prompt.lower()


def test_attempt_critic_fallback_preserves_active_canvas_contract(tmp_path: Path) -> None:
    ctx = SimpleNamespace(
        run_dir=tmp_path,
        state={
            "brief": "Academic paper poster",
            "canvas_plan": {
                "preset_id": "reference-poster",
                "canvas": {
                    "w_px": 1440,
                    "h_px": 960,
                    "dpi": 96,
                    "aspect_ratio": "3:2",
                    "color_mode": "RGB",
                },
            },
        },
    )

    spec = _attempt_critic_design_spec(ctx, _direct_canvas(ctx))

    assert spec.canvas == {
        "w_px": 1440,
        "h_px": 960,
        "dpi": 96,
        "aspect_ratio": "3:2",
        "color_mode": "RGB",
    }


def test_reference_style_budget_is_initial_attempt_plus_three_repairs() -> None:
    assert _REFERENCE_STYLE_MAX_ATTEMPTS == 4


def test_reference_audit_uses_contract_canvas(tmp_path: Path) -> None:
    reference_dir = tmp_path / "reference_poster"
    reference_dir.mkdir()
    source = reference_dir / "reference_source.png"
    source.write_bytes(b"source")
    metadata = {
        "source_sha256": sha256_file(source),
        "page_index": 0,
        "canvas_contract": {"w_px": 1440, "h_px": 960, "aspect_ratio": "3:2"},
    }
    (reference_dir / "reference_source_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    blueprint = tmp_path / "reference_style_blueprint.html"
    blueprint.write_text(
        '<div class="reference-style-blueprint"><header data-style-role="identity-header">'
        '{{PAPER_TITLE}}{{AUTHORS}}{{INSTITUTIONS}}</header><main data-style-role="body-regions">'
        '<section data-style-role="body-region" data-region-id="region_1" data-region-role="column">'
        '<section data-style-role="section"><h2 data-style-role="section-heading">{{SECTION_TITLE}}</h2>'
        '<p>{{TARGET_PAPER_CONTENT}}</p></section></section></main></div>',
        encoding="utf-8",
    )
    preview = tmp_path / "reference_style_blueprint_preview.png"
    raw_preview = tmp_path / "reference_style_raw_blueprint_preview.png"
    Image.new("RGB", (1440, 960), "white").save(preview)
    Image.new("RGB", (1440, 960), "white").save(raw_preview)
    contract = {
        "version": 4,
        "sanitizer_version": 4,
        "source_sha256": metadata["source_sha256"],
        "source_page_index": 0,
        "extraction_skill_sha256": "a" * 64,
        "extraction_prompt_schema_sha256": "b" * 64,
        "extraction_runtime_fingerprint": "c" * 64,
        "style_reference_id": "reference_test",
        "canvas_contract": metadata["canvas_contract"],
        "style_tokens": {
            "body_region_structure": {
                "regions": [{"region_id": "region_1", "region_role": "column"}],
                "major_sections_per_region": [1],
            },
            "layout_rhythm": {
                "region_boxes": [
                    {"region_id": "region_1", "x_pct": 0, "y_pct": 20, "w_pct": 100, "h_pct": 80}
                ]
            },
            "chrome_treatment": {"present": False},
        },
        "blueprint": {
            "sha256": sha256_file(blueprint),
            "preview_sha256": sha256_file(preview),
            "raw_preview_sha256": sha256_file(raw_preview),
        },
    }
    (tmp_path / "reference_style_contract.json").write_text(json.dumps(contract), encoding="utf-8")
    (reference_dir / "reference_style_agent_review.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "rendered_blueprint_inspected": True,
                "header_matches_reference": True,
                "body_region_geometry_matches_reference": True,
                "chrome_avoids_content": True,
                "blueprint_sha256": sha256_file(blueprint),
            }
        ),
        encoding="utf-8",
    )

    (tmp_path / "designer_author").mkdir()
    (tmp_path / "run_events.jsonl").write_text("{}\n", encoding="utf-8")

    report = audit_reference_style_artifacts(tmp_path)
    assert report["checks"]["preview_canvas"] is True
    assert report["checks"]["forbidden_pipeline_artifacts_absent"] is True

    extraction_only_report = audit_reference_style_artifacts(
        tmp_path,
        enforce_extraction_only_artifacts=True,
    )
    assert extraction_only_report["checks"]["forbidden_pipeline_artifacts_absent"] is False
