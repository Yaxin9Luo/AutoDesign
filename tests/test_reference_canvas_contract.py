from __future__ import annotations

from pathlib import Path

from PIL import Image

from autodesign.util.canvas_planner import plan_canvas
from autodesign.util import reference_poster


def test_normalized_image_metadata_uses_exif_corrected_intrinsic_canvas(tmp_path: Path) -> None:
    source = tmp_path / "rotated.jpg"
    image = Image.new("RGB", (4000, 2000), "white")
    exif = Image.Exif()
    exif[274] = 6
    image.save(source, exif=exif)

    metadata = reference_poster.normalize_reference_poster(source, tmp_path / "normalized")

    assert metadata["version"] == 2
    assert metadata["intrinsic_width"] == 2000
    assert metadata["intrinsic_height"] == 4000
    assert metadata["intrinsic_unit"] == "px"
    assert metadata["intrinsic_aspect_ratio"] == 0.5
    assert metadata["preview_width_px"] == 1536
    assert metadata["preview_height_px"] == 3072
    assert metadata["default_canvas"] == {
        "w_px": 2000,
        "h_px": 4000,
        "dpi": 96,
        "aspect_ratio": "1:2",
        "color_mode": "RGB",
    }
    assert metadata["canvas_scale_factor"] == 1.0
    assert metadata["canvas_scale_policy"] == "aspect_preserving_4k_tiers"


def test_reference_canvas_from_pdf_metadata_uses_deterministic_preview_pixels() -> None:
    metadata = {
        "source_suffix": ".pdf",
        "source_page_width_pt": 792.0,
        "source_page_height_pt": 612.0,
        "preview_width_px": 3072,
        "preview_height_px": 2374,
    }

    assert _reference_canvas_from_metadata(metadata) == {
        "w_px": 4096,
        "h_px": 3165,
        "dpi": 150,
        "aspect_ratio": "4096:3165",
        "color_mode": "RGB",
    }


def test_reference_canvas_from_pptx_metadata_uses_preview_not_emu_as_pixels() -> None:
    metadata = {
        "source_suffix": ".pptx",
        "slide_width_emu": 12_192_000,
        "slide_height_emu": 6_858_000,
        "preview_width_px": 3072,
        "preview_height_px": 1728,
    }

    assert _reference_canvas_from_metadata(metadata) == {
        "w_px": 4096,
        "h_px": 2304,
        "dpi": 150,
        "aspect_ratio": "16:9",
        "color_mode": "RGB",
    }


def test_reference_canvas_from_html_metadata_uses_normalized_root_preview() -> None:
    metadata = {
        "source_suffix": ".html",
        "computed_root_style": {"width_px": 4000, "height_px": 2000},
        "preview_width_px": 3072,
        "preview_height_px": 1536,
    }

    assert _reference_canvas_from_metadata(metadata) == {
        "w_px": 4000,
        "h_px": 2000,
        "dpi": 96,
        "aspect_ratio": "2:1",
        "color_mode": "RGB",
    }


def test_plan_canvas_exact_brief_pixels_beat_template_and_reference() -> None:
    reference = _reference_metadata(1200, 1800)

    plan = plan_canvas(
        "Academic poster, exact canvas 2400x1350 px",
        [],
        requested_template="neurips-portrait",
        reference_metadata=reference,
    )

    assert plan["source"] == "explicit_pixels"
    assert plan["preset_id"] == "custom-2400x1350"
    assert plan["lock_level"] == "hard"
    assert plan["canvas"] == {
        "w_px": 2400,
        "h_px": 1350,
        "dpi": 150,
        "aspect_ratio": "16:9",
        "color_mode": "RGB",
    }


def test_explicit_poster_template_overrides_negative_nonposter_keywords() -> None:
    plan = plan_canvas(
        "Generate a 3072 x 1536 px / 2:1 landscape academic conference poster, "
        "not a landing page, dashboard, marketing graphic, or slide collage.",
        [],
        requested_template="cvpr-landscape",
    )

    assert plan["artifact_type"] == "poster"
    assert plan["source"] == "explicit_pixels"
    assert plan["preset_id"] == "custom-3072x1536"
    assert plan["canvas"]["w_px"] == 3072
    assert plan["canvas"]["h_px"] == 1536


def test_plan_canvas_does_not_treat_source_figure_dimensions_as_canvas() -> None:
    reference = _reference_metadata(1200, 1800)

    plan = plan_canvas(
        "Academic poster using the source figure at 1200x800",
        [],
        reference_metadata=reference,
    )

    assert plan["source"] == "reference_poster"
    assert plan["canvas"] == reference["default_canvas"]


def test_plan_canvas_template_beats_explicit_ratio_and_reference() -> None:
    plan = plan_canvas(
        "Academic poster in a 3:4 ratio",
        [],
        requested_template="cvpr-landscape",
        reference_metadata=_reference_metadata(1200, 1800),
    )

    assert plan["source"] == "template"
    assert plan["preset_id"] == "cvpr-landscape"


def test_plan_canvas_explicit_ratio_beats_reference() -> None:
    plan = plan_canvas(
        "Academic poster in a 3:4 ratio",
        [],
        reference_metadata=_reference_metadata(2400, 1200),
    )

    assert plan["source"] == "explicit_ratio"
    assert plan["preset_id"] == "neurips-portrait"


def test_plan_canvas_reference_is_hard_and_has_no_editorial_grid() -> None:
    reference = _reference_metadata(2400, 1200)

    plan = plan_canvas(
        "Academic paper poster",
        [Path("paper.pdf")],
        reference_metadata=reference,
    )

    assert plan["source"] == "reference_poster"
    assert plan["preset_id"] == "reference-poster"
    assert plan["lock_level"] == "hard"
    assert plan["canvas"] == reference["default_canvas"]
    assert "body_grid" not in plan
    assert "grid_family" not in plan


def test_plan_canvas_defaults_are_unchanged_without_reference() -> None:
    plan = plan_canvas("Academic paper poster", [Path("paper.pdf")])

    assert plan["source"] == "brief_scene"
    assert plan["preset_id"] == "cvpr-landscape"
    assert plan["body_grid"]["family"] == "editorial_3col"


def test_exact_pixels_preserve_non_poster_artifact_type() -> None:
    plan = plan_canvas("Create a 1920x1080 px video", [])

    assert plan["artifact_type"] == "video"
    assert plan["canvas"]["w_px"] == 1920
    assert plan["canvas"]["h_px"] == 1080
    assert plan["density_budget"]["max_text_layers"] == 12


def test_video_prologue_wins_over_landing_wording() -> None:
    plan = plan_canvas(
        "Type: video (1920x1080 H.264 MP4). First produce a landing page.\nCreate a 1280x720 px video.",
        [],
    )

    assert plan["artifact_type"] == "video"


def test_prior_conversation_pixels_do_not_override_current_reference() -> None:
    brief = (
        "[Conversation context — your prior turns in this thread:]\n"
        "  • User: Make it 1000x1400 px.\n"
        "[User's current request:]\n"
        "Type: poster (single-page, print-ready, fixed-size canvas).\n"
        "Use the attached paper and reference poster."
    )

    plan = plan_canvas(
        brief,
        [Path("paper.pdf")],
        reference_metadata=_reference_metadata(1440, 960),
    )

    assert plan["source"] == "reference_poster"
    assert plan["canvas"]["w_px"] == 4320


def test_reference_poster_keeps_academic_visual_density_budget() -> None:
    plan = plan_canvas(
        "Academic paper poster",
        [Path("paper.pdf")],
        reference_metadata=_reference_metadata(1440, 960),
    )

    assert plan["density_budget"]["target_visuals_min"] >= 6


def test_reference_style_canvas_helper_accepts_normalized_default_canvas() -> None:
    from autodesign.agents.reference_style_agent import _reference_canvas_contract

    canvas = _reference_canvas_contract(
        {"default_canvas": _reference_metadata(1440, 960)["default_canvas"]}
    )

    assert canvas["w_px"] == 4320
    assert canvas["h_px"] == 2880


def test_low_resolution_reference_scales_by_four_without_changing_ratio() -> None:
    metadata = {
        "source_suffix": ".png",
        "original_width_px": 720,
        "original_height_px": 962,
    }

    assert _reference_canvas_from_metadata(metadata) == {
        "w_px": 2880,
        "h_px": 3848,
        "dpi": 96,
        "aspect_ratio": "360:481",
        "color_mode": "RGB",
    }


def test_two_k_reference_scales_by_two() -> None:
    metadata = {
        "source_suffix": ".png",
        "original_width_px": 2278,
        "original_height_px": 1574,
    }

    canvas = _reference_canvas_from_metadata(metadata)
    assert canvas["w_px"] == 4556
    assert canvas["h_px"] == 3148


def _reference_metadata(width: int, height: int) -> dict[str, object]:
    metadata: dict[str, object] = {
        "source_suffix": ".png",
        "original_width_px": width,
        "original_height_px": height,
        "preview_width_px": width,
        "preview_height_px": height,
    }
    metadata["default_canvas"] = reference_poster.reference_canvas_from_metadata(metadata)
    return metadata


def _reference_canvas_from_metadata(metadata: dict[str, object]) -> dict[str, object]:
    assert hasattr(reference_poster, "reference_canvas_from_metadata")
    return reference_poster.reference_canvas_from_metadata(metadata)
