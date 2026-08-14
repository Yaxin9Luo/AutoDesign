from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from bs4 import BeautifulSoup

from autodesign.agents.external_designer_author import (
    _apply_reference_layout_to_contract,
)
from autodesign.tools.propose_paper_poster_html import (
    _required_source_ids,
    _source_coverage_error,
)
from autodesign.util.poster_plan_contract import build_poster_plan_contract


def _brief(primary_ids: list[str]) -> dict:
    return {
        "kind": "paper_poster_content_brief",
        "title": "Required visual contract fixture",
        "sections": [],
        "visual_selection": {
            "target_visual_count": max(1, len(primary_ids)),
            "primary_visual_ids": primary_ids,
        },
    }


def _source_record(*, eligible: bool = True) -> dict:
    return {
        "kind": "image",
        "extract_strategy": "captioned_group",
        "visual_role": "evidence",
        "visual_score": 90,
        "caption_short": "Source evidence",
        "caption_confidence": 0.9,
        "caption_association_method": "captioned_group",
        "captioned_source_group": True,
        "curation_flags": [] if eligible else ["low_information_visual"],
        "designer_eligible": eligible,
        "planner_eligible": eligible,
        "planner_visible": eligible,
    }


def _contract(primary_ids: list[str], rendered: dict[str, dict]) -> dict:
    return build_poster_plan_contract(
        _brief(primary_ids),
        canvas_plan={
            "preset_id": "cvpr-landscape",
            "canvas": {"w_px": 3072, "h_px": 1536},
            "density_budget": {
                "target_visuals_min": 2,
                "target_visuals_max": 6,
                "max_visuals": 6,
                "visual_area_min": 0.34,
            },
        },
        rendered_layers=rendered,
    )


def test_eligible_selected_visuals_become_canonical_required_ids() -> None:
    contract = _contract(
        ["ingest_fig_01", "ingest_fig_02"],
        {
            "ingest_fig_01": _source_record(),
            "ingest_fig_02": _source_record(),
            "ingest_fig_inventory_only": _source_record(),
        },
    )

    selected_ids = [item["layer_id"] for item in contract["selected_visuals"]]
    assert contract["required_source_visual_ids"] == selected_ids
    assert selected_ids[:2] == ["ingest_fig_01", "ingest_fig_02"]


def test_all_ineligible_selected_visuals_produce_empty_required_ids() -> None:
    contract = _contract(
        ["ingest_fig_01"],
        {"ingest_fig_01": _source_record(eligible=False)},
    )

    assert contract["required_source_visual_ids"] == []


def test_missing_rendered_layer_records_do_not_become_required_visuals() -> None:
    contract = _contract(["ingest_fig_01"], {})

    assert contract["selected_visuals"] == []
    assert contract["required_source_visual_ids"] == []


def test_missing_canonical_required_source_is_a_hard_validation_error() -> None:
    ctx = SimpleNamespace(state={
        "poster_plan_contract": {
            "required_source_visual_ids": ["ingest_fig_01"],
            "selected_visuals": [],
        },
    })

    error = _source_coverage_error(BeautifulSoup("<main></main>", "html.parser"), ctx)

    assert error is not None
    assert error.status == "error"
    assert error.payload["issue_id"] == "paper_poster_html_source_coverage_low"
    assert error.payload["required_source_ids"] == ["ingest_fig_01"]
    assert error.payload["missing_source_ids"] == ["ingest_fig_01"]


def test_hidden_source_id_marker_does_not_satisfy_visual_coverage() -> None:
    ctx = SimpleNamespace(state={
        "poster_plan_contract": {
            "required_source_visual_ids": ["ingest_fig_01"],
            "selected_visuals": [],
        },
    })
    soup = BeautifulSoup(
        '<main><div hidden data-source-id="ingest_fig_01"></div></main>',
        "html.parser",
    )

    error = _source_coverage_error(soup, ctx)

    assert error is not None
    assert error.payload["missing_source_ids"] == ["ingest_fig_01"]


def test_hidden_source_image_descendant_does_not_satisfy_visual_coverage() -> None:
    ctx = SimpleNamespace(state={
        "poster_plan_contract": {"required_source_visual_ids": ["ingest_fig_01"]},
        "rendered_layers": {"ingest_fig_01": _source_record()},
    })
    soup = BeautifulSoup(
        '<figure data-source-id="ingest_fig_01"><img hidden src="{{layer:ingest_fig_01}}"></figure>',
        "html.parser",
    )

    error = _source_coverage_error(soup, ctx)

    assert error is not None
    assert error.payload["missing_source_ids"] == ["ingest_fig_01"]


def test_translucent_rendered_source_image_counts_but_zero_size_does_not() -> None:
    ctx = SimpleNamespace(state={
        "poster_plan_contract": {"required_source_visual_ids": ["ingest_fig_01"]},
        "rendered_layers": {"ingest_fig_01": _source_record()},
    })
    soup = BeautifulSoup(
        '<img data-block-id="fig" data-source-id="ingest_fig_01" '
        'style="opacity:0.5" src="{{layer:ingest_fig_01}}">',
        "html.parser",
    )

    assert _source_coverage_error(soup, ctx) is None
    error = _source_coverage_error(
        soup,
        ctx,
        bboxes={"fig": {"x": 10, "y": 10, "w": 0, "h": 100}},
        canvas={"w_px": 1000, "h_px": 500},
    )

    assert error is not None
    assert error.payload["missing_source_ids"] == ["ingest_fig_01"]


def test_legacy_contract_without_canonical_field_keeps_selected_visual_fallback() -> None:
    legacy_ctx = SimpleNamespace(state={
        "poster_plan_contract": {
            "selected_visuals": [{"layer_id": "ingest_fig_legacy"}],
            "density_targets": {"min_visual_count": 1},
        },
    })

    assert _required_source_ids(legacy_ctx) == ["ingest_fig_legacy"]


def test_reference_contract_copy_preserves_normal_required_source_ids() -> None:
    normal_contract = _contract(
        ["ingest_fig_01", "ingest_fig_02"],
        {
            "ingest_fig_01": _source_record(),
            "ingest_fig_02": _source_record(),
        },
    )
    reference_contract = _apply_reference_layout_to_contract(
        deepcopy(normal_contract),
        {
            "source": "reference_poster",
            "layout_mode": "freeform_regions",
            "region_count": 4,
            "regions": [],
        },
    )

    normal_ids = _required_source_ids(SimpleNamespace(state={
        "poster_plan_contract": normal_contract,
    }))
    reference_ids = _required_source_ids(SimpleNamespace(state={
        "poster_plan_contract": reference_contract,
        "reference_style_contract": {
            "version": 3,
            "transfer_mode": "reference_first_reconstruction",
            "style_reference_id": "fixture-reference",
            "style_tokens": {},
        },
    }))

    assert reference_contract["required_source_visual_ids"] == normal_contract[
        "required_source_visual_ids"
    ]
    assert reference_ids == normal_ids == ["ingest_fig_01", "ingest_fig_02"]
