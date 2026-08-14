from __future__ import annotations

import importlib
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from PIL import Image

from autodesign.util.paper_visual_storyboard import build_paper_visual_storyboard
from autodesign.util.poster_plan_contract import build_poster_plan_contract
from autodesign.util.source_visual_eligibility import classify_source_visual


ingest_document = importlib.import_module("autodesign.tools.ingest_document")


def _captioned_figure_provenance() -> dict:
    return {
        "assets": [{
            "asset_id": "ingest_fig_01",
            "kind": "image",
            "output_file": "layers/img_ingest_fig_01.png",
            "output_sha256": "fixture-sha256",
            "output_width_px": 320,
            "output_height_px": 96,
            "caption_short": "Rate-distortion over time",
            "caption_full": "Figure 5: Rate-distortion over diffusion timesteps.",
            "visual_role": "evidence",
            "visual_score": 80,
            "curation_flags": ["low_caption_confidence"],
            "crop_quality_flags": [],
            "material_quality": {"material_score": 0.98, "warnings": []},
            "designer_eligible": True,
            "planner_eligible": True,
            "planner_visible": True,
            "designer_reject_reasons": [],
            "planner_reject_reasons": [],
            "severe_crop_flags": [],
            "extract_strategy": "captioned_group",
            "protected_anchor": True,
            "anchor_kind": "figure",
            "anchor_label": "5",
            "anchor_reason": "captioned_source_group",
            "captioned_source_group": True,
            "source_group_id": "p007:figure:5",
            "source_group_kind": "figure",
            "source_group_label": "Figure 5",
            "source_group_caption": "Figure 5: Rate-distortion over diffusion timesteps.",
            "source_group_source": "pdf_caption_block",
        }],
    }


def _selected_storyboard() -> dict:
    return {
        "selected_assets": [{"asset_id": "ingest_fig_01"}],
        "primary_assets": [{"asset_id": "ingest_fig_01"}],
        "secondary_assets": [],
        "reserve_assets": [],
        "rejected_assets": [],
        "metrics": {"selected_asset_count": 1, "primary_asset_count": 1},
    }


def test_reused_provenance_preserves_selected_captioned_figure(tmp_path) -> None:
    layers_dir = tmp_path / "layers"
    layers_dir.mkdir()
    Image.new("RGB", (320, 96), (244, 246, 248)).save(layers_dir / "img_ingest_fig_01.png")
    provenance = _captioned_figure_provenance()

    rendered = ingest_document._rendered_layers_from_reused_provenance(
        provenance,
        run_dir=tmp_path,
        layers_dir=layers_dir,
    )
    reused = rendered["ingest_fig_01"]
    for key in (
        "captioned_source_group",
        "source_group_id",
        "source_group_kind",
        "source_group_label",
        "source_group_caption",
        "source_group_source",
        "protected_anchor",
        "anchor_kind",
        "anchor_label",
        "anchor_reason",
        "material_quality",
    ):
        assert reused[key] == provenance["assets"][0][key]

    sanitized = ingest_document._sanitize_paper_visual_storyboard_for_rendered(
        _selected_storyboard(),
        rendered,
    )
    assert [item["asset_id"] for item in sanitized["selected_assets"]] == ["ingest_fig_01"]
    assert sanitized["rejected_assets"] == []


def test_reused_provenance_recomputes_stale_derived_false(tmp_path) -> None:
    layers_dir = tmp_path / "layers"
    layers_dir.mkdir()
    Image.new("RGB", (320, 96), (244, 246, 248)).save(layers_dir / "img_ingest_fig_01.png")
    provenance = _captioned_figure_provenance()
    stale = provenance["assets"][0]
    stale["designer_eligible"] = False
    stale["planner_eligible"] = False
    stale["planner_visible"] = False
    stale["designer_reject_reasons"] = ["designer_eligible=false"]
    stale["planner_reject_reasons"] = ["planner_visible=false"]

    rendered = ingest_document._rendered_layers_from_reused_provenance(
        provenance,
        run_dir=tmp_path,
        layers_dir=layers_dir,
    )

    assert rendered["ingest_fig_01"]["designer_eligible"] is True
    assert rendered["ingest_fig_01"]["planner_visible"] is True
    assert rendered["ingest_fig_01"]["visual_selection_tier"] == "eligible"


def test_captioned_weak_flags_do_not_hard_reject_but_page_crop_contamination_does() -> None:
    clean_captioned = _captioned_figure_provenance()["assets"][0]
    clean_captioned["curation_flags"] = [
        "low_caption_confidence",
        "high_edge_whitespace",
        "mostly_white_visual",
        "low_detail_visual_content",
    ]
    clean_captioned["visual_score"] = 40
    clean_result = classify_source_visual("ingest_fig_01", clean_captioned)
    assert clean_result["visual_selection_tier"] == "eligible"
    assert clean_result["designer_eligible"] is True

    contaminated = deepcopy(clean_captioned)
    contaminated["extract_strategy"] = "captioned_group"
    contaminated["crop_quality_flags"] = ["body_text_leak", "neighbor_asset_leak"]
    contaminated_result = classify_source_visual("ingest_fig_02", contaminated)
    assert contaminated_result["visual_selection_tier"] == "rejected"
    assert contaminated_result["designer_eligible"] is False


def test_captioned_low_value_flag_only_downranks_clean_complete_figure() -> None:
    captioned = _captioned_figure_provenance()["assets"][0]
    captioned["curation_flags"] = ["low_value_example_crop"]
    captioned["output_width_px"] = 1000
    captioned["output_height_px"] = 700
    result = classify_source_visual("ingest_fig_01", captioned)
    assert result["visual_selection_tier"] == "eligible"

    unmatched = _storyboard_asset("ingest_fig_02", captioned=False)
    unmatched["curation_flags"] = ["low_value_example_crop"]
    assert classify_source_visual("ingest_fig_02", unmatched)["visual_selection_tier"] == "rejected"

    assets = [
        captioned,
        _storyboard_asset("ingest_fig_02", captioned=True),
        _storyboard_asset("ingest_fig_03", captioned=True),
        _storyboard_asset("ingest_fig_04", captioned=True),
    ]
    storyboard = build_paper_visual_storyboard(
        manifest={"title": "Fixture"},
        recommended_text_units={},
        recommended_figures={"evidence": [asset["asset_id"] for asset in assets]},
        visual_candidate_scores=[],
        paper_visual_provenance={"assets": assets},
        canvas_plan={
            "preset_id": "cvpr-landscape",
            "canvas": {"w_px": 3072, "h_px": 1536},
        },
    )
    selected_ids = {item["asset_id"] for item in storyboard["selected_assets"]}
    assert "ingest_fig_01" in selected_ids


def test_embedded_raster_placement_flags_are_not_payload_contamination() -> None:
    raster = {
        "asset_id": "ingest_fig_01",
        "kind": "image",
        "extract_strategy": "raster",
        "source_page": 2,
        "output_width_px": 900,
        "output_height_px": 600,
        "caption_full": "Figure 1: Overview.",
        "caption_confidence": 0.8,
        "crop_quality_flags": ["body_text_leak", "neighbor_asset_leak"],
    }
    result = classify_source_visual("ingest_fig_01", raster)
    assert result["visual_selection_tier"] == "eligible"
    assert result["placement_quality_flags"] == ["body_text_leak", "neighbor_asset_leak"]
    assert result["severe_crop_flags"] == []


def test_unmatched_confidence_without_caption_cannot_become_eligible() -> None:
    unmatched = {
        "asset_id": "ingest_fig_01",
        "kind": "image",
        "extract_strategy": "raster",
        "source_page": 2,
        "output_width_px": 900,
        "output_height_px": 600,
        "caption_confidence": 0.8,
        "caption_association_method": "unmatched",
    }
    result = classify_source_visual("ingest_fig_01", unmatched)
    assert result["visual_selection_tier"] == "reserve_unmatched"

    unmatched["caption_full"] = "Stale caption from an older ingest policy."
    stale_result = classify_source_visual("ingest_fig_01", unmatched)
    assert stale_result["visual_selection_tier"] == "reserve_unmatched"


def test_clean_unmatched_vector_crop_can_be_shortfall_reserve() -> None:
    unmatched = {
        "asset_id": "ingest_fig_01",
        "kind": "image",
        "extract_strategy": "vector",
        "source_page": 4,
        "output_width_px": 900,
        "output_height_px": 600,
        "caption_association_method": "unmatched",
        "crop_quality_flags": [],
    }

    result = classify_source_visual("ingest_fig_01", unmatched)

    assert result["visual_selection_tier"] == "reserve_unmatched"
    assert result["designer_eligible"] is True


def test_raster_payload_severe_evidence_is_not_hidden_by_placement_flag() -> None:
    raster = {
        "asset_id": "ingest_fig_01",
        "kind": "image",
        "extract_strategy": "raster",
        "source_page": 2,
        "output_width_px": 900,
        "output_height_px": 600,
        "caption_full": "Figure 1: Overview.",
        "caption_confidence": 0.8,
        "placement_quality_flags": ["body_text_leak"],
        "curation_flags": ["body_text_leak"],
    }
    result = classify_source_visual("ingest_fig_01", raster)
    assert result["visual_selection_tier"] == "rejected"
    assert result["severe_crop_flags"] == ["body_text_leak"]


def test_storyboard_does_not_treat_raster_placement_flag_as_payload_contamination() -> None:
    raster = _storyboard_asset("ingest_fig_01", captioned=True)
    raster["crop_quality_flags"] = ["body_text_leak", "neighbor_asset_leak"]
    storyboard = build_paper_visual_storyboard(
        manifest={"title": "Fixture"},
        recommended_text_units={},
        recommended_figures={"evidence": ["ingest_fig_01"]},
        visual_candidate_scores=[],
        paper_visual_provenance={"assets": [raster]},
        canvas_plan={
            "preset_id": "cvpr-landscape",
            "canvas": {"w_px": 3072, "h_px": 1536},
        },
    )
    assert [item["asset_id"] for item in storyboard["selected_assets"]] == ["ingest_fig_01"]


def test_page_region_severe_flags_remain_hard_rejections() -> None:
    for flag in ("partial_visual_crop", "running_header_leak", "page_like_figure_crop"):
        page_region = {
            "asset_id": "ingest_fig_01",
            "kind": "image",
            "extract_strategy": "captioned_group",
            "source_page": 2,
            "output_width_px": 900,
            "output_height_px": 600,
            "caption_full": "Figure 1: Overview.",
            "caption_confidence": 0.8,
            "crop_quality_flags": [flag],
        }
        result = classify_source_visual("ingest_fig_01", page_region)
        assert result["visual_selection_tier"] == "rejected", flag
        assert result["severe_crop_flags"] == [flag]


def _storyboard_asset(asset_id: str, *, captioned: bool) -> dict:
    asset = {
        "asset_id": asset_id,
        "kind": "image",
        "output_file": f"layers/img_{asset_id}.png",
        "output_sha256": asset_id,
        "output_width_px": 1000,
        "output_height_px": 700,
        "source_page": 2,
        "extract_strategy": "raster",
        "visual_role": "evidence",
        "visual_score": 80,
        "curation_flags": [],
        "crop_quality_flags": [],
    }
    if captioned:
        asset.update({
            "caption_full": f"Figure {asset_id[-1]}: Evidence.",
            "captioned_source_group": True,
            "source_group_id": f"p002:figure:{asset_id[-1]}",
            "source_group_label": f"Figure {asset_id[-1]}",
            "source_group_caption": f"Figure {asset_id[-1]}: Evidence.",
        })
    return asset


def test_unmatched_reserve_only_fills_shortfall_and_never_becomes_primary() -> None:
    assets = [
        _storyboard_asset("ingest_fig_01", captioned=True),
        *[
            _storyboard_asset(f"ingest_fig_0{idx}", captioned=False)
            for idx in range(2, 7)
        ],
    ]
    storyboard = build_paper_visual_storyboard(
        manifest={"title": "Fixture"},
        recommended_text_units={},
        recommended_figures={"evidence": ["ingest_fig_01"]},
        visual_candidate_scores=[],
        paper_visual_provenance={"assets": assets},
        canvas_plan={
            "preset_id": "cvpr-landscape",
            "canvas": {"w_px": 3072, "h_px": 1536},
        },
    )

    primary_ids = {item["asset_id"] for item in storyboard["primary_assets"]}
    secondary_reserve = [
        item for item in storyboard["secondary_assets"]
        if item.get("visual_selection_tier") == "reserve_unmatched"
    ]
    assert primary_ids == {"ingest_fig_01"}
    assert len(secondary_reserve) == 2
    assert all(item.get("unmatched_caption") is True for item in secondary_reserve)
    assert all(item.get("replacement_only") is True for item in secondary_reserve)
    assert all(item.get("shortfall_only") is True for item in secondary_reserve)
    assert all(item.get("story_role") not in {"hero_method", "main_evidence"} for item in secondary_reserve)
    rejected_ids = {item["asset_id"] for item in storyboard["rejected_assets"]}
    assert not ({item["asset_id"] for item in secondary_reserve} & rejected_ids)

    rendered = {
        str(asset["asset_id"]): {
            **asset,
            "layer_id": asset["asset_id"],
            "caption": asset.get("caption_full") or "",
            "image_size": f"{asset['output_width_px']}x{asset['output_height_px']}",
        }
        for asset in assets
    }
    contract = build_poster_plan_contract(
        {
            "kind": "paper_poster_content_brief",
            "title": "Fixture",
            "sections": [],
            "visual_selection": {
                "primary_visual_ids": ["ingest_fig_01"],
                "secondary_visual_ids": [item["asset_id"] for item in secondary_reserve],
                "source_asset_records": assets,
                "role_buckets": {"evidence": ["ingest_fig_01"]},
            },
            "visual_storyboard": storyboard,
        },
        canvas_plan={
            "preset_id": "cvpr-landscape",
            "canvas": {"w_px": 3072, "h_px": 1536},
        },
        rendered_layers=rendered,
    )
    reserve_ids = {item["layer_id"] for item in contract["source_asset_tiers"]["secondary_assets"]}
    required_ids = set(contract["required_source_visual_ids"])
    forbidden_ids = set(contract["source_asset_tiers"]["forbidden_source_ids"])
    assert reserve_ids == {item["asset_id"] for item in secondary_reserve}
    assert required_ids == {"ingest_fig_01"}
    assert not (reserve_ids & forbidden_ids)
    for item in contract["source_asset_tiers"]["secondary_assets"]:
        assert item["visual_selection_tier"] == "reserve_unmatched"
        assert item["replacement_only"] is True
        assert item["shortfall_only"] is True
    required_role_ids = {
        visual_id
        for role in contract["required_visual_roles"]
        for visual_id in role.get("visual_ids") or []
    }
    assert not (reserve_ids & required_role_ids)


def test_reuse_sanitizers_keep_unmatched_only_as_optional_secondary() -> None:
    eligible = _storyboard_asset("ingest_fig_01", captioned=True)
    reserve = _storyboard_asset("ingest_fig_02", captioned=False)
    rendered = {
        asset["asset_id"]: {
            **asset,
            "layer_id": asset["asset_id"],
            "caption": asset.get("caption_full") or "",
            "image_size": f"{asset['output_width_px']}x{asset['output_height_px']}",
        }
        for asset in (eligible, reserve)
    }
    storyboard = {
        "selected_assets": [{"asset_id": "ingest_fig_01"}, {"asset_id": "ingest_fig_02"}],
        "primary_assets": [{"asset_id": "ingest_fig_01"}, {"asset_id": "ingest_fig_02"}],
        "secondary_assets": [{"asset_id": "ingest_fig_02"}],
        "reserve_assets": [],
        "rejected_assets": [],
        "metrics": {"capacity": {"minimum_count": 5}},
    }
    sanitized_storyboard = ingest_document._sanitize_paper_visual_storyboard_for_rendered(
        storyboard,
        rendered,
    )
    assert [item["asset_id"] for item in sanitized_storyboard["primary_assets"]] == ["ingest_fig_01"]
    assert [item["asset_id"] for item in sanitized_storyboard["secondary_assets"]] == ["ingest_fig_02"]

    brief = {
        "kind": "paper_poster_content_brief",
        "visual_selection": {
            "primary_visual_ids": ["ingest_fig_01", "ingest_fig_02"],
            "high_priority_visual_ids": ["ingest_fig_02"],
            "secondary_visual_ids": ["ingest_fig_02"],
            "role_buckets": {"method": ["ingest_fig_02"]},
        },
        "source_asset_policy": {
            "primary_assets_mandatory": ["ingest_fig_01", "ingest_fig_02"],
            "secondary_assets_optional": ["ingest_fig_02"],
        },
        "visual_storyboard": storyboard,
    }
    sanitized_brief = ingest_document._sanitize_poster_content_brief_visual_eligibility(
        brief,
        rendered,
    )
    selection = sanitized_brief["visual_selection"]
    assert selection["primary_visual_ids"] == ["ingest_fig_01"]
    assert selection["high_priority_visual_ids"] == []
    assert selection["secondary_visual_ids"] == ["ingest_fig_02"]
    assert selection["role_buckets"]["method"] == []
    assert sanitized_brief["source_asset_policy"]["primary_assets_mandatory"] == ["ingest_fig_01"]
    assert sanitized_brief["source_asset_policy"]["secondary_assets_optional"] == ["ingest_fig_02"]


def test_fresh_brief_propagates_at_most_two_shortfall_reserves() -> None:
    assets = [
        _storyboard_asset("ingest_fig_01", captioned=True),
        *[_storyboard_asset(f"ingest_fig_0{idx}", captioned=False) for idx in range(2, 7)],
    ]
    rendered = {
        asset["asset_id"]: {
            **asset,
            "layer_id": asset["asset_id"],
            "caption": asset.get("caption_full") or "",
            "image_size": f"{asset['output_width_px']}x{asset['output_height_px']}",
        }
        for asset in assets
    }
    scores = [
        {"layer_id": asset["asset_id"], "visual_role": "evidence", "visual_score": 80}
        for asset in assets
    ]
    canvas_plan = {
        "preset_id": "cvpr-landscape",
        "canvas": {"w_px": 3072, "h_px": 1536},
        "density_budget": {"target_visuals_min": 5, "target_visuals_max": 8, "max_visuals": 10},
    }
    storyboard = build_paper_visual_storyboard(
        manifest={"title": "Fixture"},
        recommended_text_units={},
        recommended_figures={"evidence": ["ingest_fig_01"]},
        visual_candidate_scores=scores,
        paper_visual_provenance={"assets": assets},
        canvas_plan=canvas_plan,
    )
    brief = ingest_document._build_poster_content_brief(
        summaries=[{"type": "pdf", "manifest": {"title": "Fixture", "abstract": "A result."}}],
        rendered=rendered,
        recommended_figures={"evidence": ["ingest_fig_01"]},
        recommended_text_units={"takeaways": [{"text": "A grounded result.", "source": "abstract"}]},
        visual_candidate_scores=scores,
        canvas_plan=canvas_plan,
        paper_visual_provenance={"assets": assets},
        paper_visual_storyboard=storyboard,
    )

    secondary_ids = brief["visual_selection"]["secondary_visual_ids"]
    reserve_secondary = [
        asset_id for asset_id in secondary_ids
        if classify_source_visual(asset_id, rendered[asset_id])["visual_selection_tier"] == "reserve_unmatched"
    ]
    assert len(reserve_secondary) == 2
    assert not (set(reserve_secondary) & set(brief["visual_selection"]["primary_visual_ids"]))
    contract = build_poster_plan_contract(brief, canvas_plan=canvas_plan, rendered_layers=rendered)
    contract_reserves = [
        item for item in contract["source_asset_tiers"]["secondary_assets"]
        if item.get("visual_selection_tier") == "reserve_unmatched"
    ]
    assert len(contract_reserves) == 2
    assert not ({item["layer_id"] for item in contract_reserves} & set(contract["required_source_visual_ids"]))


def test_fresh_brief_never_resurrects_storyboard_rejected_visuals() -> None:
    assets = [
        _storyboard_asset(f"ingest_fig_0{idx}", captioned=True)
        for idx in range(1, 19)
    ]
    rendered = {
        asset["asset_id"]: {
            **asset,
            "layer_id": asset["asset_id"],
            "caption": asset.get("caption_full") or "",
            "image_size": f"{asset['output_width_px']}x{asset['output_height_px']}",
        }
        for asset in assets
    }
    scores = [
        {"layer_id": asset["asset_id"], "visual_role": "evidence", "visual_score": 90}
        for asset in assets
    ]
    storyboard = {
        "target_visual_count": 4,
        "selected_assets": [{"asset_id": asset["asset_id"]} for asset in assets[:4]],
        "primary_assets": [{"asset_id": asset["asset_id"]} for asset in assets[:4]],
        "secondary_assets": [],
        "reserve_assets": [],
        "rejected_assets": [
            {"asset_id": asset["asset_id"], "reason": "canvas capacity"}
            for asset in assets[4:]
        ],
        "metrics": {"capacity": {"minimum_count": 4}},
    }
    canvas_plan = {
        "preset_id": "cvpr-landscape",
        "canvas": {"w_px": 3072, "h_px": 1536},
        "density_budget": {"target_visuals_min": 4, "target_visuals_max": 6, "max_visuals": 8},
    }

    brief = ingest_document._build_poster_content_brief(
        summaries=[{"type": "pdf", "manifest": {"title": "Fixture", "abstract": "A result."}}],
        rendered=rendered,
        recommended_figures={"evidence": [asset["asset_id"] for asset in assets]},
        recommended_text_units={"takeaways": [{"text": "A grounded result.", "source": "abstract"}]},
        visual_candidate_scores=scores,
        canvas_plan=canvas_plan,
        paper_visual_provenance={"assets": assets},
        paper_visual_storyboard=storyboard,
    )

    rejected_ids = {asset["asset_id"] for asset in assets[4:]}
    selection = brief["visual_selection"]
    assert not (rejected_ids & set(selection["high_priority_visual_ids"]))
    assert not (rejected_ids & set(selection["primary_visual_ids"]))
    assert rejected_ids <= set(selection["forbidden_visual_ids"])


def test_reuse_removes_stale_rejection_for_reclassified_reserve() -> None:
    eligible = _storyboard_asset("ingest_fig_01", captioned=True)
    reserve = _storyboard_asset("ingest_fig_02", captioned=False)
    rendered = {
        asset["asset_id"]: {
            **asset,
            "layer_id": asset["asset_id"],
            "caption": asset.get("caption_full") or "",
            "image_size": f"{asset['output_width_px']}x{asset['output_height_px']}",
        }
        for asset in (eligible, reserve)
    }
    storyboard = {
        "selected_assets": [{"asset_id": "ingest_fig_01"}, {"asset_id": "ingest_fig_02"}],
        "primary_assets": [{"asset_id": "ingest_fig_01"}],
        "secondary_assets": [{"asset_id": "ingest_fig_02"}],
        "reserve_assets": [],
        "rejected_assets": [{"asset_id": "ingest_fig_02", "reason": "planner_visible=false"}],
        "metrics": {"capacity": {"minimum_count": 5}},
    }
    sanitized_storyboard = ingest_document._sanitize_paper_visual_storyboard_for_rendered(storyboard, rendered)
    assert [item["asset_id"] for item in sanitized_storyboard["secondary_assets"]] == ["ingest_fig_02"]
    assert all(item.get("asset_id") != "ingest_fig_02" for item in sanitized_storyboard["rejected_assets"])

    brief = {
        "visual_selection": {
            "primary_visual_ids": ["ingest_fig_01"],
            "secondary_visual_ids": ["ingest_fig_02"],
            "forbidden_visual_ids": ["ingest_fig_02"],
            "storyboard_rejected_asset_ids": ["ingest_fig_02"],
        },
        "source_asset_policy": {
            "primary_assets_mandatory": ["ingest_fig_01"],
            "secondary_assets_optional": ["ingest_fig_02"],
            "forbidden_source_ids": ["ingest_fig_02"],
        },
        "visual_storyboard": sanitized_storyboard,
    }
    sanitized_brief = ingest_document._sanitize_poster_content_brief_visual_eligibility(brief, rendered)
    assert sanitized_brief["visual_selection"]["secondary_visual_ids"] == ["ingest_fig_02"]
    assert "ingest_fig_02" not in sanitized_brief["visual_selection"]["forbidden_visual_ids"]
    assert "ingest_fig_02" not in sanitized_brief["source_asset_policy"]["forbidden_source_ids"]


def test_reuse_caps_many_reserves_and_compact_payload_never_promotes_them() -> None:
    reserves = [_storyboard_asset(f"ingest_fig_0{idx}", captioned=False) for idx in range(1, 6)]
    rendered = {
        asset["asset_id"]: {
            **asset,
            "layer_id": asset["asset_id"],
            "image_size": f"{asset['output_width_px']}x{asset['output_height_px']}",
        }
        for asset in reserves
    }
    storyboard = {
        "selected_assets": [{"asset_id": asset["asset_id"], **asset} for asset in reserves],
        "primary_assets": [],
        "secondary_assets": [{"asset_id": asset["asset_id"], **asset} for asset in reserves],
        "reserve_assets": [],
        "rejected_assets": [],
        "metrics": {"capacity": {"minimum_count": 5}},
    }
    sanitized = ingest_document._sanitize_paper_visual_storyboard_for_rendered(storyboard, rendered)
    assert len(sanitized["secondary_assets"]) == 2
    compact = ingest_document._compact_paper_visual_storyboard_for_planner(sanitized)
    assert compact["primary_assets"] == []
    assert len(compact["secondary_assets"]) == 2


class ReusedIngestVisualEligibilityTests(unittest.TestCase):
    def test_preserves_selected_captioned_figure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            test_reused_provenance_preserves_selected_captioned_figure(Path(tmp))

    def test_recomputes_stale_derived_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            test_reused_provenance_recomputes_stale_derived_false(Path(tmp))

    def test_still_rejects_genuinely_weak_figure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            test_reused_provenance_still_rejects_genuinely_weak_figure(Path(tmp))

    def test_weak_and_severe_flags(self) -> None:
        test_captioned_weak_flags_do_not_hard_reject_but_page_crop_contamination_does()

    def test_captioned_low_value_flag_is_ranking_only(self) -> None:
        test_captioned_low_value_flag_only_downranks_clean_complete_figure()

    def test_raster_placement_flags(self) -> None:
        test_embedded_raster_placement_flags_are_not_payload_contamination()

    def test_unmatched_confidence_requires_caption(self) -> None:
        test_unmatched_confidence_without_caption_cannot_become_eligible()

    def test_clean_unmatched_vector_is_reserve(self) -> None:
        test_clean_unmatched_vector_crop_can_be_shortfall_reserve()

    def test_raster_payload_severe_evidence(self) -> None:
        test_raster_payload_severe_evidence_is_not_hidden_by_placement_flag()

    def test_storyboard_uses_raster_placement_semantics(self) -> None:
        test_storyboard_does_not_treat_raster_placement_flag_as_payload_contamination()

    def test_page_region_severe_flags(self) -> None:
        test_page_region_severe_flags_remain_hard_rejections()

    def test_unmatched_reserve_shortfall(self) -> None:
        test_unmatched_reserve_only_fills_shortfall_and_never_becomes_primary()

    def test_reuse_sanitizers_keep_reserve_optional(self) -> None:
        test_reuse_sanitizers_keep_unmatched_only_as_optional_secondary()

    def test_fresh_brief_propagates_shortfall_reserves(self) -> None:
        test_fresh_brief_propagates_at_most_two_shortfall_reserves()

    def test_fresh_brief_excludes_storyboard_rejected(self) -> None:
        test_fresh_brief_never_resurrects_storyboard_rejected_visuals()

    def test_reuse_removes_stale_rejection(self) -> None:
        test_reuse_removes_stale_rejection_for_reclassified_reserve()

    def test_reuse_caps_reserves_and_compact_never_promotes(self) -> None:
        test_reuse_caps_many_reserves_and_compact_payload_never_promotes_them()


def test_reused_provenance_still_rejects_genuinely_weak_figure(tmp_path) -> None:
    layers_dir = tmp_path / "layers"
    layers_dir.mkdir()
    Image.new("RGB", (320, 96), (244, 246, 248)).save(layers_dir / "img_ingest_fig_01.png")
    provenance = _captioned_figure_provenance()
    rendered = ingest_document._rendered_layers_from_reused_provenance(
        provenance,
        run_dir=tmp_path,
        layers_dir=layers_dir,
    )
    weak = deepcopy(rendered["ingest_fig_01"])
    for key in (
        "captioned_source_group",
        "source_group_id",
        "source_group_kind",
        "source_group_label",
        "source_group_caption",
        "source_group_source",
        "protected_anchor",
        "anchor_kind",
        "anchor_label",
        "anchor_reason",
    ):
        weak.pop(key, None)
    weak["visual_score"] = 60

    sanitized = ingest_document._sanitize_paper_visual_storyboard_for_rendered(
        _selected_storyboard(),
        {"ingest_fig_01": weak},
    )
    assert sanitized["selected_assets"] == []
    assert sanitized["rejected_assets"] == [{
        "asset_id": "ingest_fig_01",
        "reason": "designer-selected-ineligible source asset: unmatched_caption_not_reservable",
    }]


if __name__ == "__main__":
    unittest.main()
