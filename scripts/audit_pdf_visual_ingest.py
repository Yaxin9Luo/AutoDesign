#!/usr/bin/env python3
"""Run PDF visual ingest audits without launching poster authoring.

This is for regression-checking source figure/table recall on real papers.
It runs only ingest_document and the deterministic provenance/storyboard/
contract handoff that follows ingest.

Examples:
    uv run python scripts/audit_pdf_visual_ingest.py \\
        --paper vit=./data/vit/paper.pdf

    uv run python scripts/audit_pdf_visual_ingest.py \\
        --all-papers --paper-root ./data
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from autodesign.agents.external_designer_author import _write_author_quick_brief
from autodesign.config import load_settings
from autodesign.tools import ToolContext
from autodesign.tools.ingest_document import ingest_document
from autodesign.util.logging import run_context


REPO_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_ANCHORS: dict[str, list[tuple[str, str]]] = {
    "vit": [("figure", "1"), ("table", "2")],
    "nerf": [("figure", "2")],
    "mask-rcnn": [("figure", "1")],
    "transformer": [("figure", "1"), ("table", "1")],
}

LEAK_CURATION_FLAGS = {
    "algorithm_caption_leak",
    "body_text_leak",
    "caption_in_crop",
    "caption_strip_leak",
    "edge_visual_remnant",
    "figure_caption_leak",
    "header_band_leak",
    "multi_caption_leak",
    "neighbor_asset_leak",
    "other_caption_in_crop",
    "page_like_figure_crop",
    "page_like_table_crop",
    "page_furniture_leak",
    "running_header_leak",
    "section_heading_leak",
    "table_body_text_leak",
    "table_fragment_crop",
}

DESIGNER_INELIGIBLE_FLAGS = LEAK_CURATION_FLAGS | {
    "image_payload_unavailable",
    "partial_visual_crop",
    "unlocated_raster_component",
}

EDGE_TEXT_RESIDUE_FLAGS = LEAK_CURATION_FLAGS | {
    "page_furniture_leak",
}

WEAK_QUALITY_FLAGS = DESIGNER_INELIGIBLE_FLAGS | {
    "low_caption_confidence",
    "low_information_visual",
    "low_resolution",
    "low_value_example_crop",
    "no_caption",
    "source_page_unknown",
    "table_too_dense",
    "table_without_structure",
}

LOW_DESIGNER_ELIGIBLE_ASSET_THRESHOLD = 6

SEED_MANUAL_REVIEW_NOTES: list[dict[str, Any]] = [
    {
        "paper_match": "ai/vit/",
        "paper": "ViT",
        "asset_id": "ingest_fig_08",
        "severity": "crop-contamination",
        "note": "Figure 1 crop includes the page furniture text 'Published as Conference Paper at ICLR 2021' at the top.",
        "general_fix_hint": "Captioned figure localization should suppress running headers/page furniture before accepting a crop.",
    },
    {
        "paper_match": "ai/2020-nerf-representing-scenes-as-neural-radiance-fields-for-view-synthesis/",
        "paper": "NeRF",
        "asset_id": "ingest_fig_42",
        "severity": "crop-contamination",
        "note": "Top edge includes unrelated body/header text residue.",
        "general_fix_hint": "Figure crop post-processing should trim text strips that are outside the captioned visual group.",
    },
    {
        "paper_match": "ai/2020-nerf-representing-scenes-as-neural-radiance-fields-for-view-synthesis/",
        "paper": "NeRF",
        "asset_id": "ingest_fig_43",
        "severity": "crop-contamination",
        "note": "Top edge includes unrelated body/header text residue.",
        "general_fix_hint": "Figure crop post-processing should trim text strips that are outside the captioned visual group.",
    },
    {
        "paper_match": "ai/2020-nerf-representing-scenes-as-neural-radiance-fields-for-view-synthesis/",
        "paper": "NeRF",
        "asset_id": "ingest_fig_44",
        "severity": "crop-contamination",
        "note": "Top edge includes unrelated body/header text residue.",
        "general_fix_hint": "Figure crop post-processing should trim text strips that are outside the captioned visual group.",
    },
    {
        "paper_match": "ai/2017-mask-r-cnn/",
        "paper": "Mask R-CNN",
        "asset_id": "ingest_fig_02",
        "severity": "partial-crop",
        "note": "Method figure crop loses part of the left side and includes a small text residue at the bottom.",
        "general_fix_hint": "A recovered captioned group should beat an overlapping object-level vector crop when it covers the full figure better.",
    },
    {
        "paper_match": "ai/2017-mask-r-cnn/",
        "paper": "Mask R-CNN",
        "asset_id": "ingest_fig_27",
        "severity": "crop-contamination",
        "note": "Bottom edge includes text residue.",
        "general_fix_hint": "Designer-eligible selection should demote crops with body/caption text leakage even if they are captioned.",
    },
    {
        "paper_match": "ai/2017-mask-r-cnn/",
        "paper": "Mask R-CNN",
        "asset_id": "ingest_table_02",
        "severity": "designer-eligibility",
        "note": "The crop is a dense composite of multiple tables; keeping it in provenance is fine, but it should not be promoted for poster use.",
        "general_fix_hint": "Table poster-fitness should separate provenance registration from designer-eligible/high-priority table assets.",
    },
    {
        "paper_match": "ai/2017-mask-r-cnn/",
        "paper": "Mask R-CNN",
        "asset_id": "ingest_fig_12",
        "severity": "partial-crop",
        "note": "Left and bottom sides are partial, with residue from another nearby visual region.",
        "general_fix_hint": "Object-level crops without a captioned full-group match should be demoted or moved to debug/reserve.",
    },
    {
        "paper_match": "ai/2017-attention-is-all-you-need/",
        "paper": "Attention Is All You Need",
        "asset_id": "",
        "severity": "control-good",
        "note": "Manual review found this paper's current figure/table crops to be clean; use it as a regression control.",
        "general_fix_hint": "Future crop-quality changes should not regress this clean booktabs/architecture case.",
    },
]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "out" / "ingest_audits" / datetime.now().strftime("%Y%m%d_%H%M%S"),
        help="Audit output directory.",
    )
    parser.add_argument(
        "--paper",
        action="append",
        default=[],
        metavar="NAME=PDF",
        help="Paper to audit; repeatable, e.g. vit=./data/vit/paper.pdf.",
    )
    parser.add_argument(
        "--paper-root",
        type=Path,
        help="Root to scan when --all-papers is set.",
    )
    parser.add_argument(
        "--all-papers",
        action="store_true",
        help="Audit every paper.pdf found under --paper-root.",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="NAME",
        help="Run only named papers from the selected set.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional limit after discovery/filtering; useful for smoke checks.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run papers even when an audit_summary.json already exists.",
    )
    return parser.parse_args(argv)


def _resolve_papers(args: argparse.Namespace) -> dict[str, Path]:
    papers: dict[str, Path] = {}
    if args.all_papers:
        if args.paper_root is None:
            raise SystemExit("--all-papers requires --paper-root DIR.")
        papers = _discover_posterbench_papers(args.paper_root)

    for item in args.paper:
        if "=" not in item:
            raise SystemExit(f"--paper must be NAME=PDF, got: {item}")
        name, path = item.split("=", 1)
        papers[name.strip()] = Path(path).expanduser().resolve()

    if not papers:
        raise SystemExit("Provide --paper NAME=PDF or --all-papers --paper-root DIR.")

    return papers


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    os.environ["AUTODESIGN_INGEST_PDF_CACHE"] = "0"

    papers = _resolve_papers(args)
    if args.only:
        wanted = {_slug(item) for item in args.only}
        papers = {
            name: path for name, path in papers.items()
            if _slug(name) in wanted
        }
        if not papers:
            raise SystemExit(f"--only matched no papers: {', '.join(args.only)}")
    if args.limit and args.limit > 0:
        papers = dict(list(papers.items())[:args.limit])
    if not papers:
        raise SystemExit("No papers to audit.")

    settings = replace(load_settings(), enable_paper_memory_agent=False)
    out_dir = args.out.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    index: dict[str, Any] = {
        "kind": "pdf_visual_ingest_audit",
        "out_dir": str(out_dir),
        "paper_root": str(args.paper_root.expanduser().resolve()) if args.paper_root else "",
        "mode": "all_papers" if args.all_papers else "selected_papers",
        "cache_disabled": os.getenv("AUTODESIGN_INGEST_PDF_CACHE") == "0",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "planned_count": len(papers),
        "seed_manual_review_notes": SEED_MANUAL_REVIEW_NOTES,
        "papers": [],
    }
    _refresh_index(out_dir, index)

    for idx, (name, pdf_path) in enumerate(papers.items(), start=1):
        summary_path = out_dir / _slug(name) / "audit_summary.json"
        if summary_path.exists() and not args.force:
            summary = _read_json(summary_path)
            if summary:
                summary["resumed_from_existing_summary"] = True
            else:
                summary = _failure_summary(out_dir, name, pdf_path, "existing audit_summary.json was unreadable")
        else:
            try:
                summary = _run_one(settings, out_dir, name, pdf_path)
            except Exception as exc:
                summary = _failure_summary(out_dir, name, pdf_path, repr(exc))
        summary["index_position"] = idx
        index["papers"].append(summary)
        _refresh_index(out_dir, index)
        print(
            f"[{idx}/{len(papers)}] {name}: {summary.get('status')} "
            f"assets={summary.get('asset_count', 0)} "
            f"tables={summary.get('registered_source_table_count', 0)} "
            f"visible={summary.get('planner_visible_registered_asset_count', 0)}",
            flush=True,
        )

    index["finished_at"] = datetime.now().isoformat(timespec="seconds")
    _refresh_index(out_dir, index)
    print(json.dumps(index, indent=2, ensure_ascii=False))
    return 0


def _discover_posterbench_papers(root: Path) -> dict[str, Path]:
    root = root.expanduser().resolve()
    papers: dict[str, Path] = {}
    for pdf_path in sorted(root.rglob("paper.pdf")):
        try:
            rel_parent = pdf_path.parent.relative_to(root)
        except ValueError:
            rel_parent = pdf_path.parent
        name = "__".join(rel_parent.parts)
        slug = _slug(name)
        if slug in papers:
            slug = _slug(str(rel_parent))
        papers[slug] = pdf_path
    return papers


def _refresh_index(out_dir: Path, index: dict[str, Any]) -> None:
    papers = [item for item in index.get("papers", []) if isinstance(item, dict)]
    index["completed_count"] = len(papers)
    index["ok_count"] = sum(1 for item in papers if item.get("status") == "ok")
    index["failed_count"] = sum(1 for item in papers if item.get("status") != "ok")
    index.update(_index_review_rollup(papers))
    index["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_json(out_dir / "index.json", index)
    _write_html_index(out_dir, index)


def _index_review_rollup(papers: list[dict[str, Any]]) -> dict[str, Any]:
    zero_asset_papers = [
        _paper_asset_rollup_row(paper)
        for paper in papers
        if bool(paper.get("zero_asset_paper"))
    ]
    low_asset_papers = [
        _paper_asset_rollup_row(paper)
        for paper in papers
        if bool(paper.get("low_asset_paper"))
    ]
    contract_bad_assets = [
        _paper_issue_row(paper, asset)
        for paper in papers
        for asset in list(paper.get("contract_bad_assets") or [])
        if isinstance(asset, dict)
    ]
    designer_ineligible_assets = [
        _paper_issue_row(paper, asset)
        for paper in papers
        for asset in list(paper.get("designer_ineligible_assets") or [])
        if isinstance(asset, dict)
    ]
    table_leak_assets = [
        _paper_issue_row(paper, asset)
        for paper in papers
        for asset in list(paper.get("table_leak_assets") or [])
        if isinstance(asset, dict)
    ]
    figure_leak_assets = [
        _paper_issue_row(paper, asset)
        for paper in papers
        for asset in list(paper.get("figure_leak_assets") or [])
        if isinstance(asset, dict)
    ]
    selected_issue_assets = [
        _paper_issue_row(paper, asset)
        for paper in papers
        for asset in list(((paper.get("selected_asset_review") or {}).get("issue_assets") or []))
        if isinstance(asset, dict)
    ]
    source_shortfall_papers = [
        _paper_selected_source_shortfall_row(paper)
        for paper in papers
        if _paper_metric(paper, "source_visual_shortfall") > 0
    ]
    unbacked_shortfall_papers = [
        _paper_selected_source_shortfall_row(paper)
        for paper in papers
        if _paper_metric(paper, "unbacked_source_visual_shortfall") > 0
    ]
    golden_check_summaries = [
        {"paper": paper.get("name"), **(paper.get("golden_check_summary") or {})}
        for paper in papers
        if isinstance(paper.get("golden_check_summary"), dict)
    ]
    review_summary = {
        "low_asset_threshold": LOW_DESIGNER_ELIGIBLE_ASSET_THRESHOLD,
        "zero_asset_paper_count": len(zero_asset_papers),
        "low_asset_paper_count": len(low_asset_papers),
        "zero_asset_papers": zero_asset_papers,
        "low_asset_papers": low_asset_papers,
        "contract_bad_asset_count": len(contract_bad_assets),
        "contract_bad_selected_asset_count": sum(
            _paper_metric(paper, "contract_bad_selected_asset_count")
            for paper in papers
        ),
        "contract_bad_planner_visible_asset_count": sum(
            _paper_metric(paper, "contract_bad_planner_visible_asset_count")
            for paper in papers
        ),
        "contract_bad_assets": contract_bad_assets,
        "designer_ineligible_asset_count": len(designer_ineligible_assets),
        "designer_ineligible_planner_visible_asset_count": sum(
            _paper_metric(paper, "designer_ineligible_planner_visible_asset_count")
            for paper in papers
        ),
        "designer_ineligible_assets": designer_ineligible_assets,
        "table_leak_count": len(table_leak_assets),
        "table_leak_assets": table_leak_assets,
        "figure_leak_count": len(figure_leak_assets),
        "figure_leak_assets": figure_leak_assets,
        "selected_duplicate_count": sum(_paper_metric(paper, "selected_duplicate_count") for paper in papers),
        "selected_weak_quality_count": sum(_paper_metric(paper, "selected_weak_quality_count") for paper in papers),
        "selected_edge_text_residue_count": sum(
            _paper_metric(paper, "selected_edge_text_residue_count")
            for paper in papers
        ),
        "selected_forbidden_intersection_count": sum(
            _paper_metric(paper, "selected_forbidden_intersection_count")
            for paper in papers
        ),
        "selected_issue_asset_count": len(selected_issue_assets),
        "selected_issue_assets": selected_issue_assets,
        "selected_source_visual_count": sum(_paper_metric(paper, "selected_source_visual_count") for paper in papers),
        "source_visual_shortfall": sum(_paper_metric(paper, "source_visual_shortfall") for paper in papers),
        "source_visual_shortfall_paper_count": len(source_shortfall_papers),
        "source_visual_shortfall_papers": source_shortfall_papers,
        "unbacked_source_visual_shortfall": sum(
            _paper_metric(paper, "unbacked_source_visual_shortfall")
            for paper in papers
        ),
        "unbacked_source_visual_shortfall_paper_count": len(unbacked_shortfall_papers),
        "unbacked_source_visual_shortfall_papers": unbacked_shortfall_papers,
        "supplemental_native_visual_task_count": sum(
            _paper_metric(paper, "supplemental_native_visual_task_count")
            for paper in papers
        ),
        "golden_check_summaries": golden_check_summaries,
    }
    return {
        **review_summary,
        "review_summary": review_summary,
    }


def _paper_metric(paper: dict[str, Any], key: str) -> int:
    metrics = paper.get("review_metrics") if isinstance(paper.get("review_metrics"), dict) else {}
    return _safe_int(paper.get(key, metrics.get(key)), 0)


def _paper_asset_rollup_row(paper: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": paper.get("name"),
        "status": paper.get("status"),
        "registered_asset_count": paper.get("registered_asset_count", 0),
        "planner_visible_registered_asset_count": paper.get("planner_visible_registered_asset_count", 0),
        "designer_eligible_asset_count": paper.get("designer_eligible_asset_count", 0),
        "designer_ineligible_asset_count": paper.get("designer_ineligible_asset_count", 0),
        "contract_bad_asset_count": paper.get("contract_bad_asset_count", 0),
        "selected_source_visual_count": paper.get("selected_source_visual_count", 0),
        "source_visual_shortfall": paper.get("source_visual_shortfall", 0),
        "poster_visual_unit_target": paper.get("poster_visual_unit_target", 0),
        "supplemental_native_visual_task_count": paper.get("supplemental_native_visual_task_count", 0),
        "table_leak_count": paper.get("table_leak_count", 0),
        "figure_leak_count": paper.get("figure_leak_count", 0),
        "run_dir": paper.get("run_dir"),
    }


def _paper_issue_row(paper: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "paper": paper.get("name"),
        "asset_id": asset.get("asset_id"),
        "asset_class": asset.get("asset_class"),
        "kind": asset.get("kind"),
        "planner_visible": bool(asset.get("planner_visible")),
        "debug_only": bool(asset.get("debug_only")),
        "source_page": asset.get("source_page"),
        "visual_role": asset.get("visual_role"),
        "visual_score": asset.get("visual_score"),
        "curation_flags": list(asset.get("curation_flags") or []),
        "crop_quality_flags": list(asset.get("crop_quality_flags") or []),
        "severe_crop_flags": list(asset.get("severe_crop_flags") or []),
        "designer_eligible": asset.get("designer_eligible"),
        "issue_reasons": list(asset.get("issue_reasons") or asset.get("contract_sources") or []),
        "selected_review_reasons": list(asset.get("selected_review_reasons") or []),
        "selected_duplicate": bool(asset.get("selected_duplicate")),
        "selected_weak_quality": bool(asset.get("selected_weak_quality")),
        "selected_edge_text_residue": bool(asset.get("selected_edge_text_residue")),
        "selected_forbidden_intersection": bool(asset.get("selected_forbidden_intersection")),
        "contract_reason": asset.get("contract_reason"),
        "caption_short": asset.get("caption_short"),
        "run_dir": paper.get("run_dir"),
    }


def _paper_selected_source_shortfall_row(paper: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": paper.get("name"),
        "status": paper.get("status"),
        "selected_source_visual_count": paper.get("selected_source_visual_count", 0),
        "poster_visual_unit_target": paper.get("poster_visual_unit_target", 0),
        "source_visual_shortfall": paper.get("source_visual_shortfall", 0),
        "supplemental_native_visual_task_count": paper.get("supplemental_native_visual_task_count", 0),
        "unbacked_source_visual_shortfall": paper.get("unbacked_source_visual_shortfall", 0),
        "selected_issue_asset_count": len((paper.get("selected_asset_review") or {}).get("issue_assets") or []),
        "run_dir": paper.get("run_dir"),
    }


def _failure_summary(out_dir: Path, name: str, pdf_path: Path, error: str) -> dict[str, Any]:
    try:
        resolved = pdf_path.expanduser().resolve()
    except OSError:
        resolved = pdf_path.expanduser()
    run_dir = out_dir / _slug(name)
    run_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "name": name,
        "pdf": str(resolved),
        "run_dir": str(run_dir),
        "status": "error",
        "error": error,
        "contact_sheet": "",
        "asset_count": 0,
        "registered_asset_count": 0,
        "planner_visible_registered_asset_count": 0,
        "captioned_visual_group_count": 0,
        "registered_source_table_count": 0,
        "unparsed_source_table_count": 0,
        "manual_review_notes": _manual_review_notes_for_pdf(resolved),
        "passed": False,
    }
    summary.update(_empty_review_fields(status="error"))
    _write_json(run_dir / "audit_summary.json", summary)
    return summary


def _empty_review_fields(*, status: str) -> dict[str, Any]:
    fields = {
        "contract_bad_asset_count": 0,
        "contract_bad_selected_asset_count": 0,
        "contract_bad_planner_visible_asset_count": 0,
        "contract_bad_assets": [],
        "designer_eligible_asset_count": 0,
        "designer_ineligible_asset_count": 0,
        "designer_ineligible_planner_visible_asset_count": 0,
        "designer_ineligible_assets": [],
        "table_leak_count": 0,
        "table_leak_assets": [],
        "figure_leak_count": 0,
        "figure_leak_assets": [],
        "selected_asset_count": 0,
        "selected_source_visual_count": 0,
        "poster_visual_unit_target": 0,
        "source_visual_shortfall": 0,
        "supplemental_native_visual_task_count": 0,
        "unbacked_source_visual_shortfall": 0,
        "supplemental_native_visual_tasks": [],
        "selected_duplicate_count": 0,
        "selected_weak_quality_count": 0,
        "selected_edge_text_residue_count": 0,
        "selected_forbidden_intersection_count": 0,
        "selected_asset_review": {
            "status": status,
            "metrics": {
                "selected_asset_count": 0,
                "selected_source_visual_count": 0,
                "poster_visual_unit_target": 0,
                "source_visual_shortfall": 0,
                "supplemental_native_visual_task_count": 0,
                "unbacked_source_visual_shortfall": 0,
                "selected_duplicate_count": 0,
                "selected_weak_quality_count": 0,
                "selected_edge_text_residue_count": 0,
                "selected_forbidden_intersection_count": 0,
            },
            "assets": [],
            "issue_assets": [],
        },
        "zero_asset_paper": True,
        "low_asset_paper": False,
        "low_asset_threshold": LOW_DESIGNER_ELIGIBLE_ASSET_THRESHOLD,
    }
    fields["review_metrics"] = {
        "status": status,
        **{key: value for key, value in fields.items() if key.endswith("_count") or key.endswith("_paper")},
        "low_asset_threshold": LOW_DESIGNER_ELIGIBLE_ASSET_THRESHOLD,
    }
    fields["golden_check_summary"] = dict(fields["review_metrics"])
    fields["golden_check_summary"].update({
        "expected_anchor_count": 0,
        "expected_anchor_provenance_pass_count": 0,
        "expected_anchor_storyboard_primary_pass_count": 0,
        "expected_anchor_contract_selected_pass_count": 0,
        "expected_anchor_contract_high_priority_pass_count": 0,
        "expected_anchor_all_provenance": True,
        "expected_anchor_all_storyboard_primary": True,
        "expected_anchor_all_contract_selected": True,
        "expected_anchor_all_contract_high_priority": True,
        "quick_brief_has_high_priority_section": False,
        "ingest_audit_candidate_pass": False,
    })
    return fields


def _manual_review_notes_for_pdf(pdf_path: Path) -> list[dict[str, Any]]:
    norm = str(pdf_path).replace("\\", "/").lower()
    notes: list[dict[str, Any]] = []
    for item in SEED_MANUAL_REVIEW_NOTES:
        marker = str(item.get("paper_match") or "").lower()
        if marker and marker in norm:
            note = {key: value for key, value in item.items() if key != "paper_match"}
            notes.append(note)
    return notes


def _run_one(settings: Any, out_dir: Path, name: str, pdf_path: Path) -> dict[str, Any]:
    run_dir = out_dir / _slug(name)
    layers_dir = run_dir / "layers"
    run_dir.mkdir(parents=True, exist_ok=True)
    layers_dir.mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(
        settings=settings,
        run_dir=run_dir,
        layers_dir=layers_dir,
        run_id=f"ingest-audit-{_slug(name)}",
    )
    ctx.state["artifact_type"] = "poster"
    ctx.state["raw_user_brief"] = _audit_prompt()
    ctx.state["run_brief"] = _audit_prompt()
    ctx.state["canvas_plan"] = _audit_canvas_plan()

    pdf_path = pdf_path.expanduser().resolve()
    manual_review_notes = _manual_review_notes_for_pdf(pdf_path)
    with run_context(ctx.run_id, run_dir):
        result = ingest_document({"file_paths": [str(pdf_path)]}, ctx=ctx)
    status = getattr(result, "status", "")
    error = getattr(result, "error_message", "") if status != "ok" else ""

    contact_sheet = _find_contact_sheet(layers_dir)
    if contact_sheet is not None:
        _copy_if_exists(contact_sheet, run_dir / "contact_sheet.png")
    _write_author_quick_brief(ctx, run_dir, _audit_prompt())

    provenance = _read_json(run_dir / "paper_visual_provenance.json")
    storyboard = _read_json(run_dir / "paper_visual_storyboard.json")
    contract = _read_json(run_dir / "poster_plan_contract.json")
    content = _read_json(run_dir / "poster_content_brief.json")
    rendered = ctx.state.get("rendered_layers")
    rendered_layers = rendered if isinstance(rendered, dict) else {}
    pdf_summary = _pdf_summary_for_path(ctx.state.get("ingested"), pdf_path)
    manifest = pdf_summary.get("manifest") if isinstance(pdf_summary.get("manifest"), dict) else {}
    events = _read_run_events(run_dir / "run_events.jsonl")
    quick_brief_path = run_dir / "author_quick_brief.md"
    quick_brief_text = quick_brief_path.read_text(encoding="utf-8") if quick_brief_path.exists() else ""

    assets = [asset for asset in provenance.get("assets", []) if isinstance(asset, dict)]
    planner_visible_ids = _planner_visible_asset_ids(storyboard, contract, content)
    registered_assets = [
        _compact_registered_asset(
            asset,
            rendered_layers.get(str(asset.get("asset_id") or "")) or {},
            planner_visible=str(asset.get("asset_id") or "") in planner_visible_ids,
        )
        for asset in assets
    ]
    registered_source_tables = [
        item for item in registered_assets
        if item.get("asset_class") == "source_table"
    ]
    flagged_planner_visible_assets = [
        item for item in registered_assets
        if item.get("planner_visible") and item.get("curation_flags")
    ]
    unparsed_source_tables = [
        item for item in registered_assets
        if item.get("asset_class") in {"unparsed_source_table", "source_table_crop_candidate"}
    ]
    captioned_groups = _captioned_visual_groups(
        manifest=manifest,
        assets=assets,
        rendered=rendered_layers,
        planner_visible_ids=planner_visible_ids,
    )
    candidate_diagnostics = _candidate_diagnostics(
        assets=assets,
        rendered=rendered_layers,
        storyboard=storyboard,
        events=events,
        planner_visible_ids=planner_visible_ids,
    )
    protected = [asset for asset in assets if asset.get("protected_anchor")]
    protected_table_candidates = [
        asset for asset in assets
        if (
            str(asset.get("kind") or "") == "source_table_crop_candidate"
            or (
                bool(asset.get("protected_anchor"))
                and str(asset.get("anchor_kind") or "").lower() == "table"
            )
        )
    ]
    protected_table_sheet = _write_protected_table_candidates_contact_sheet(
        run_dir,
        protected_table_candidates,
    )
    expected = EXPECTED_ANCHORS.get(name, [])
    anchor_checks = [
        _anchor_check(kind, label, assets, storyboard, contract)
        for kind, label in expected
    ]
    selected_ids = [
        str(item.get("layer_id") or item.get("asset_id") or "")
        for item in contract.get("selected_visuals", [])
        if isinstance(item, dict)
    ]
    primary_ids = [
        str(item.get("asset_id") or item.get("layer_id") or "")
        for item in storyboard.get("primary_assets", [])
        if isinstance(item, dict)
    ]
    contract_bad_assets = _contract_bad_assets(
        contract=contract,
        storyboard=storyboard,
        content=content,
        registered_assets=registered_assets,
        planner_visible_ids=planner_visible_ids,
        selected_ids=selected_ids,
    )
    contract_bad_ids = {
        str(item.get("asset_id") or "")
        for item in contract_bad_assets
        if str(item.get("asset_id") or "")
    }
    designer_ineligible_assets = _designer_ineligible_assets(
        registered_assets,
        contract_bad_ids=contract_bad_ids,
    )
    designer_ineligible_ids = {
        str(item.get("asset_id") or "")
        for item in designer_ineligible_assets
        if str(item.get("asset_id") or "")
    }
    selected_asset_review = _selected_asset_review(
        selected_ids=selected_ids,
        contract=contract,
        storyboard=storyboard,
        content=content,
        registered_assets=registered_assets,
        contract_bad_assets=contract_bad_assets,
        designer_ineligible_assets=designer_ineligible_assets,
        candidate_diagnostics=candidate_diagnostics,
    )
    source_visual_metrics = _selected_source_visual_metrics(
        selected_ids=selected_ids,
        contract=contract,
        content=content,
    )
    selected_asset_review["metrics"].update({
        key: value for key, value in source_visual_metrics.items()
        if key != "supplemental_native_visual_tasks"
    })
    selected_asset_review["supplemental_native_visual_tasks"] = source_visual_metrics.get(
        "supplemental_native_visual_tasks",
        [],
    )
    designer_eligible_asset_count = sum(
        1 for item in registered_assets
        if str(item.get("asset_id") or "") not in designer_ineligible_ids
    )
    table_leak_assets = _leak_assets(registered_assets, asset_family="table")
    figure_leak_assets = _leak_assets(registered_assets, asset_family="figure")
    zero_asset_paper = designer_eligible_asset_count == 0
    low_asset_paper = (
        0 < designer_eligible_asset_count < LOW_DESIGNER_ELIGIBLE_ASSET_THRESHOLD
    )
    review_metrics = _paper_review_metrics(
        status=status,
        registered_asset_count=len(registered_assets),
        planner_visible_registered_asset_count=sum(
            1 for item in registered_assets if item.get("planner_visible")
        ),
        designer_eligible_asset_count=designer_eligible_asset_count,
        designer_ineligible_assets=designer_ineligible_assets,
        contract_bad_assets=contract_bad_assets,
        selected_asset_review=selected_asset_review,
        source_visual_metrics=source_visual_metrics,
        table_leak_assets=table_leak_assets,
        figure_leak_assets=figure_leak_assets,
        zero_asset_paper=zero_asset_paper,
        low_asset_paper=low_asset_paper,
    )
    golden_check_summary = _golden_check_summary(
        review_metrics=review_metrics,
        anchor_checks=anchor_checks,
        quick_brief_has_high_priority_section="## High-Priority Source Visuals" in quick_brief_text,
    )
    summary: dict[str, Any] = {
        "name": name,
        "pdf": str(pdf_path),
        "run_dir": str(run_dir),
        "status": status,
        "error": error,
        "contact_sheet": str(contact_sheet) if contact_sheet is not None else "",
        "table_candidate_debug_contact_sheet": (
            str(protected_table_sheet) if protected_table_sheet is not None else ""
        ),
        "protected_table_candidates_contact_sheet": (
            str(protected_table_sheet) if protected_table_sheet is not None else ""
        ),
        "asset_count": len(assets),
        "registered_asset_count": len(registered_assets),
        "registered_assets": registered_assets,
        "planner_visible_registered_asset_count": sum(
            1 for item in registered_assets if item.get("planner_visible")
        ),
        "captioned_visual_group_count": len(captioned_groups),
        "captioned_visual_groups": captioned_groups,
        "registered_source_table_count": len(registered_source_tables),
        "registered_source_tables": registered_source_tables,
        "flagged_planner_visible_asset_count": len(flagged_planner_visible_assets),
        "flagged_planner_visible_assets": flagged_planner_visible_assets,
        "contract_bad_asset_count": len(contract_bad_assets),
        "contract_bad_selected_asset_count": sum(
            1 for item in contract_bad_assets if item.get("contract_selected")
        ),
        "contract_bad_planner_visible_asset_count": sum(
            1 for item in contract_bad_assets if item.get("planner_visible")
        ),
        "contract_bad_assets": contract_bad_assets,
        "designer_eligible_asset_count": designer_eligible_asset_count,
        "designer_ineligible_asset_count": len(designer_ineligible_assets),
        "designer_ineligible_planner_visible_asset_count": sum(
            1 for item in designer_ineligible_assets if item.get("planner_visible")
        ),
        "designer_ineligible_assets": designer_ineligible_assets,
        "table_leak_count": len(table_leak_assets),
        "table_leak_assets": table_leak_assets,
        "figure_leak_count": len(figure_leak_assets),
        "figure_leak_assets": figure_leak_assets,
        "selected_asset_count": selected_asset_review["metrics"].get("selected_asset_count", 0),
        "selected_source_visual_count": source_visual_metrics.get("selected_source_visual_count", 0),
        "poster_visual_unit_target": source_visual_metrics.get("poster_visual_unit_target", 0),
        "source_visual_shortfall": source_visual_metrics.get("source_visual_shortfall", 0),
        "supplemental_native_visual_task_count": source_visual_metrics.get("supplemental_native_visual_task_count", 0),
        "unbacked_source_visual_shortfall": review_metrics.get("unbacked_source_visual_shortfall", 0),
        "supplemental_native_visual_tasks": source_visual_metrics.get("supplemental_native_visual_tasks", []),
        "selected_duplicate_count": selected_asset_review["metrics"].get("selected_duplicate_count", 0),
        "selected_weak_quality_count": selected_asset_review["metrics"].get("selected_weak_quality_count", 0),
        "selected_edge_text_residue_count": selected_asset_review["metrics"].get("selected_edge_text_residue_count", 0),
        "selected_forbidden_intersection_count": selected_asset_review["metrics"].get("selected_forbidden_intersection_count", 0),
        "selected_asset_review": selected_asset_review,
        "selected_assets": selected_asset_review.get("assets", []),
        "selected_issue_assets": selected_asset_review.get("issue_assets", []),
        "zero_asset_paper": zero_asset_paper,
        "low_asset_paper": low_asset_paper,
        "low_asset_threshold": LOW_DESIGNER_ELIGIBLE_ASSET_THRESHOLD,
        "review_metrics": review_metrics,
        "golden_check_summary": golden_check_summary,
        "unparsed_source_table_count": len(unparsed_source_tables),
        "unparsed_source_tables": unparsed_source_tables,
        "unparsed_captioned_table_group_count": sum(
            1 for item in captioned_groups
            if item.get("kind") == "table" and not item.get("formal_table_asset_ids")
        ),
        "candidate_diagnostics": candidate_diagnostics,
        "dropped_debug_candidate_count": candidate_diagnostics.get("dropped_or_debug_candidate_count", 0),
        "dropped_or_duplicate_candidates": candidate_diagnostics.get("dropped_or_duplicate_candidates", []),
        "manual_review_notes": manual_review_notes,
        "protected_anchor_count": len(protected),
        "protected_table_candidate_count": len(protected_table_candidates),
        "protected_table_candidates": [
            _compact_anchor_asset(asset)
            for asset in protected_table_candidates[:16]
        ],
        "protected_anchors": [
            _compact_anchor_asset(asset)
            for asset in protected[:16]
        ],
        "storyboard_primary_ids": primary_ids,
        "contract_selected_ids": selected_ids,
        "high_priority_visual_ids": (content.get("visual_selection") or {}).get("high_priority_visual_ids") or [],
        "contract_high_priority_ids": [
            str(item.get("layer_id") or item.get("asset_id") or "")
            for item in ((contract.get("source_asset_tiers") or {}).get("high_priority_assets") or [])
            if isinstance(item, dict)
        ],
        "quick_brief_has_high_priority_section": "## High-Priority Source Visuals" in quick_brief_text,
        "expected_anchor_checks": anchor_checks,
        "passed": (
            status == "ok"
            and all(item.get("provenance") for item in anchor_checks)
            and _review_metrics_pass(review_metrics)
        ),
    }
    _write_json(run_dir / "audit_summary.json", summary)
    return summary


def _anchor_check(
    kind: str,
    label: str,
    assets: list[dict[str, Any]],
    storyboard: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    matched = [_asset_matches_anchor(asset, kind, label) for asset in assets]
    matched_ids = [
        str(asset.get("asset_id") or "")
        for asset, ok in zip(assets, matched, strict=False)
        if ok and str(asset.get("asset_id") or "")
    ]
    primary_ids = {
        str(item.get("asset_id") or item.get("layer_id") or "")
        for item in storyboard.get("primary_assets", [])
        if isinstance(item, dict)
    }
    selected_ids = {
        str(item.get("layer_id") or item.get("asset_id") or "")
        for item in contract.get("selected_visuals", [])
        if isinstance(item, dict)
    }
    high_priority_ids = {
        str(item.get("layer_id") or item.get("asset_id") or "")
        for item in ((contract.get("source_asset_tiers") or {}).get("high_priority_assets") or [])
        if isinstance(item, dict)
    }
    return {
        "kind": kind,
        "label": label,
        "asset_ids": matched_ids,
        "provenance": bool(matched_ids),
        "storyboard_primary": any(asset_id in primary_ids for asset_id in matched_ids),
        "contract_selected": any(asset_id in selected_ids for asset_id in matched_ids),
        "contract_high_priority": any(asset_id in high_priority_ids for asset_id in matched_ids),
    }


def _asset_matches_anchor(asset: dict[str, Any], kind: str, label: str) -> bool:
    anchor_kind = str(asset.get("anchor_kind") or "").lower()
    anchor_label = str(asset.get("anchor_label") or "").lower()
    if anchor_kind == kind and anchor_label == label.lower():
        return True
    caption = " ".join(
        str(asset.get(key) or "")
        for key in ("caption_short", "caption_full")
    )
    pattern = r"\b" + (r"table" if kind == "table" else r"fig(?:ure)?\.?") + r"\s*" + re.escape(label) + r"\b"
    return bool(re.search(pattern, caption, flags=re.IGNORECASE))


def _contract_bad_assets(
    *,
    contract: dict[str, Any],
    storyboard: dict[str, Any],
    content: dict[str, Any],
    registered_assets: list[dict[str, Any]],
    planner_visible_ids: set[str],
    selected_ids: list[str],
) -> list[dict[str, Any]]:
    by_id = {
        str(item.get("asset_id") or ""): item
        for item in registered_assets
        if str(item.get("asset_id") or "")
    }
    selected = {str(item or "") for item in selected_ids if str(item or "")}
    bad: dict[str, dict[str, Any]] = {}

    def add(asset_id: Any, source: str, reason: Any = "") -> None:
        asset_id_s = str(asset_id or "").strip()
        if not asset_id_s:
            return
        base = dict(by_id.get(asset_id_s) or {"asset_id": asset_id_s})
        if asset_id_s not in by_id:
            base["missing_registered_record"] = True
        item = bad.setdefault(asset_id_s, base)
        sources = list(item.get("contract_sources") or [])
        _append_unique(sources, source)
        item["contract_sources"] = sources
        reasons = list(item.get("contract_reasons") or [])
        reason_s = str(reason or "").strip()
        if reason_s:
            _append_unique(reasons, reason_s)
        item["contract_reasons"] = reasons
        item["contract_reason"] = "; ".join(reasons)
        item["contract_selected"] = asset_id_s in selected
        item["planner_visible"] = bool(item.get("planner_visible")) or asset_id_s in planner_visible_ids

    tiers = contract.get("source_asset_tiers") if isinstance(contract.get("source_asset_tiers"), dict) else {}
    for item in list(tiers.get("rejected_assets") or []):
        if not isinstance(item, dict):
            continue
        add(
            item.get("asset_id") or item.get("layer_id"),
            "contract.source_asset_tiers.rejected_assets",
            item.get("reason") or "contract rejected source asset",
        )
    for asset_id in list(tiers.get("forbidden_source_ids") or []):
        add(asset_id, "contract.source_asset_tiers.forbidden_source_ids", "contract forbidden source asset")

    policy = contract.get("source_asset_policy") if isinstance(contract.get("source_asset_policy"), dict) else {}
    for asset_id in list(policy.get("forbidden_source_ids") or []):
        add(asset_id, "contract.source_asset_policy.forbidden_source_ids", "contract policy forbidden source asset")

    visual_selection = content.get("visual_selection") if isinstance(content.get("visual_selection"), dict) else {}
    for key in ("forbidden_visual_ids", "storyboard_rejected_asset_ids"):
        for asset_id in list(visual_selection.get(key) or []):
            add(asset_id, f"content.visual_selection.{key}", "visual selection rejected source asset")

    for item in list(storyboard.get("rejected_assets") or []):
        if not isinstance(item, dict):
            continue
        add(
            item.get("asset_id") or item.get("layer_id"),
            "storyboard.rejected_assets",
            item.get("reason") or "storyboard rejected source asset",
        )

    return sorted(
        bad.values(),
        key=lambda item: (
            0 if item.get("contract_selected") else 1,
            0 if item.get("planner_visible") else 1,
            str(item.get("asset_id") or ""),
        ),
    )


def _designer_ineligible_assets(
    registered_assets: list[dict[str, Any]],
    *,
    contract_bad_ids: set[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for asset in registered_assets:
        asset_id = str(asset.get("asset_id") or "")
        flags = _asset_flags(asset)
        reasons: list[str] = []
        if asset.get("designer_eligible") is False:
            reasons.append("designer_eligible=false")
        if asset.get("planner_visible") is False:
            reasons.append("planner_visible=false")
        if asset_id in contract_bad_ids:
            reasons.append("contract_bad_asset")
        if asset.get("debug_only"):
            reasons.append("debug_only_asset")
        for flag in sorted(flags & DESIGNER_INELIGIBLE_FLAGS):
            reasons.append(f"curation_flag:{flag}")
        if not reasons:
            continue
        item = dict(asset)
        item["designer_eligible"] = False
        item["issue_reasons"] = reasons
        out.append(item)
    return sorted(
        out,
        key=lambda item: (
            0 if item.get("planner_visible") else 1,
            str(item.get("asset_id") or ""),
        ),
    )


def _selected_source_visual_metrics(
    *,
    selected_ids: list[str],
    contract: dict[str, Any],
    content: dict[str, Any],
) -> dict[str, Any]:
    sources = _visual_metric_sources(contract, content)
    selected_count = _first_present_int(sources, "selected_source_visual_count")
    if selected_count is None:
        selected_count = sum(1 for asset_id in selected_ids if _is_audit_source_visual_id(asset_id))

    target = _first_present_int(sources, "poster_visual_unit_target")
    if target is None:
        target = _first_present_int(sources, "target_source_visual_count")
    if target is None:
        target = _first_present_int(sources, "min_selected_visual_count")

    shortfall = _first_present_int(sources, "source_visual_shortfall")
    if shortfall is None:
        shortfall = max(0, target - selected_count) if target is not None else 0

    tasks = _supplemental_native_visual_tasks(contract, content)
    task_count = _first_present_int(sources, "supplemental_native_visual_task_count")
    if task_count is None:
        task_count = len(tasks)

    return {
        "selected_source_visual_count": selected_count,
        "poster_visual_unit_target": target if target is not None else 0,
        "source_visual_shortfall": max(0, shortfall),
        "supplemental_native_visual_task_count": task_count,
        "supplemental_native_visual_tasks": tasks,
    }


def _visual_metric_sources(contract: dict[str, Any], content: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []

    def add(value: Any) -> None:
        if isinstance(value, dict):
            sources.append(value)

    add(contract)
    add(contract.get("metrics"))
    add(contract.get("review_metrics"))
    add(contract.get("density_targets"))
    add(contract.get("visual_selection"))
    add(contract.get("visual_storyboard"))
    add(content)
    add(content.get("metrics"))
    add(content.get("review_metrics"))
    add(content.get("density_targets"))
    add(content.get("visual_selection"))
    add(content.get("visual_storyboard"))
    return sources


def _first_present_int(sources: list[dict[str, Any]], key: str) -> int | None:
    for source in sources:
        if key not in source:
            continue
        return _safe_int(source.get(key), 0)
    return None


def _supplemental_native_visual_tasks(contract: dict[str, Any], content: dict[str, Any]) -> list[dict[str, Any]]:
    for source in (
        contract,
        contract.get("visual_selection") if isinstance(contract.get("visual_selection"), dict) else {},
        content,
        content.get("visual_selection") if isinstance(content.get("visual_selection"), dict) else {},
    ):
        tasks = source.get("supplemental_native_visual_tasks") if isinstance(source, dict) else None
        if isinstance(tasks, list) and tasks:
            return [item for item in tasks if isinstance(item, dict)]
    return []


def _is_audit_source_visual_id(value: Any) -> bool:
    asset_id = str(value or "").strip()
    return bool(asset_id) and (
        asset_id.startswith("ingest_fig_")
        or asset_id.startswith("ingest_table_")
        or asset_id.startswith("source_table_crop_candidate_")
    )


def _selected_asset_review(
    *,
    selected_ids: list[str],
    contract: dict[str, Any],
    storyboard: dict[str, Any],
    content: dict[str, Any],
    registered_assets: list[dict[str, Any]],
    contract_bad_assets: list[dict[str, Any]],
    designer_ineligible_assets: list[dict[str, Any]],
    candidate_diagnostics: dict[str, Any],
) -> dict[str, Any]:
    by_id = {
        str(item.get("asset_id") or ""): item
        for item in registered_assets
        if str(item.get("asset_id") or "")
    }
    bad_by_id = {
        str(item.get("asset_id") or ""): item
        for item in contract_bad_assets
        if str(item.get("asset_id") or "")
    }
    ineligible_by_id = {
        str(item.get("asset_id") or ""): item
        for item in designer_ineligible_assets
        if str(item.get("asset_id") or "")
    }
    forbidden_ids = _forbidden_review_source_ids(contract, storyboard, content)
    duplicate_candidate_ids = _duplicate_candidate_source_ids(storyboard, candidate_diagnostics)

    seen: set[str] = set()
    seen_assets: list[tuple[str, dict[str, Any]]] = []
    assets: list[dict[str, Any]] = []
    for idx, asset_id in enumerate([str(item or "").strip() for item in selected_ids], start=1):
        if not asset_id:
            continue
        base = dict(by_id.get(asset_id) or {"asset_id": asset_id})
        flags = sorted(_asset_flags(base))
        reasons: list[str] = []
        duplicate_match = next(
            (
                prev_id
                for prev_id, prev_asset in seen_assets
                if _review_assets_are_duplicates(prev_asset, base)
            ),
            "",
        )
        duplicate = asset_id in seen or asset_id in duplicate_candidate_ids or bool(duplicate_match)
        forbidden = asset_id in forbidden_ids
        if asset_id in seen:
            _append_unique(reasons, "duplicate_selected_id")
        if duplicate_match:
            _append_unique(reasons, f"duplicate_selected_visual:{duplicate_match}")
        if asset_id in duplicate_candidate_ids:
            _append_unique(reasons, "duplicate_candidate")
        if forbidden:
            _append_unique(reasons, "selected_id_intersects_forbidden_or_rejected_sources")
        if asset_id not in by_id:
            _append_unique(reasons, "missing_registered_asset")

        bad_asset = bad_by_id.get(asset_id) or {}
        for reason in list(bad_asset.get("contract_reasons") or []):
            _append_unique(reasons, f"contract:{reason}")
        for source in list(bad_asset.get("contract_sources") or []):
            _append_unique(reasons, f"contract_source:{source}")

        ineligible_asset = ineligible_by_id.get(asset_id) or {}
        ineligible_reasons = [
            str(reason)
            for reason in list(ineligible_asset.get("issue_reasons") or [])
            if str(reason or "").strip()
        ]
        for reason in ineligible_reasons:
            _append_unique(reasons, f"ineligible:{reason}")

        if base.get("debug_only"):
            _append_unique(reasons, "debug_only_asset")
        if base.get("designer_eligible") is False:
            _append_unique(reasons, "designer_eligible=false")
        if base.get("planner_eligible") is False:
            _append_unique(reasons, "planner_eligible=false")
        if base.get("planner_visible") is False:
            _append_unique(reasons, "planner_visible=false")

        edge_flags = sorted(set(flags) & EDGE_TEXT_RESIDUE_FLAGS)
        weak_flag_set = set(flags) & WEAK_QUALITY_FLAGS
        if _review_asset_high_confidence_captioned(base):
            weak_flag_set.discard("low_caption_confidence")
        weak_flags = sorted(weak_flag_set)
        for flag in edge_flags:
            _append_unique(reasons, f"edge_text_residue:{flag}")
        for flag in weak_flags:
            _append_unique(reasons, f"weak_quality:{flag}")

        duplicate = duplicate or _contains_issue_token(reasons + flags, "duplicate")
        weak_quality = (
            asset_id not in by_id
            or bool(base.get("debug_only"))
            or base.get("designer_eligible") is False
            or base.get("planner_eligible") is False
            or base.get("planner_visible") is False
            or bool(weak_flags)
            or any(reason != "contract_bad_asset" for reason in ineligible_reasons)
        )
        edge_text_residue = bool(edge_flags)

        item = dict(base)
        item.update({
            "selected_index": idx,
            "asset_id": asset_id,
            "thumbnail_file": base.get("output_file"),
            "selected_duplicate": duplicate,
            "selected_weak_quality": weak_quality,
            "selected_edge_text_residue": edge_text_residue,
            "selected_forbidden_intersection": forbidden,
            "selected_review_reasons": reasons,
        })
        assets.append(item)
        seen.add(asset_id)
        seen_assets.append((asset_id, base))

    issue_assets = [
        item for item in assets
        if (
            item.get("selected_duplicate")
            or item.get("selected_weak_quality")
            or item.get("selected_edge_text_residue")
            or item.get("selected_forbidden_intersection")
        )
    ]
    metrics = {
        "selected_asset_count": len(assets),
        "selected_duplicate_count": sum(1 for item in assets if item.get("selected_duplicate")),
        "selected_weak_quality_count": sum(1 for item in assets if item.get("selected_weak_quality")),
        "selected_edge_text_residue_count": sum(
            1 for item in assets if item.get("selected_edge_text_residue")
        ),
        "selected_forbidden_intersection_count": sum(
            1 for item in assets if item.get("selected_forbidden_intersection")
        ),
    }
    return {
        "status": "fail" if any(
            metrics[key]
            for key in (
                "selected_duplicate_count",
                "selected_weak_quality_count",
                "selected_edge_text_residue_count",
                "selected_forbidden_intersection_count",
            )
        ) else "ok",
        "metrics": metrics,
        "assets": assets,
        "issue_assets": issue_assets,
        "selected_forbidden_intersection_ids": [
            item.get("asset_id") for item in assets
            if item.get("selected_forbidden_intersection")
        ],
    }


def _forbidden_review_source_ids(
    contract: dict[str, Any],
    storyboard: dict[str, Any],
    content: dict[str, Any],
) -> set[str]:
    ids: set[str] = set()

    def add(value: Any) -> None:
        asset_id = str(value or "").strip()
        if asset_id:
            ids.add(asset_id)

    tiers = contract.get("source_asset_tiers") if isinstance(contract.get("source_asset_tiers"), dict) else {}
    for asset_id in list(tiers.get("forbidden_source_ids") or []):
        add(asset_id)
    for item in list(tiers.get("rejected_assets") or []):
        if isinstance(item, dict):
            add(item.get("asset_id") or item.get("layer_id"))

    policy = contract.get("source_asset_policy") if isinstance(contract.get("source_asset_policy"), dict) else {}
    for asset_id in list(policy.get("forbidden_source_ids") or []):
        add(asset_id)

    visual_selection = content.get("visual_selection") if isinstance(content.get("visual_selection"), dict) else {}
    for key in ("forbidden_visual_ids", "storyboard_rejected_asset_ids"):
        for asset_id in list(visual_selection.get(key) or []):
            add(asset_id)

    for item in list(storyboard.get("rejected_assets") or []):
        if isinstance(item, dict):
            add(item.get("asset_id") or item.get("layer_id"))
    return ids


def _duplicate_candidate_source_ids(
    storyboard: dict[str, Any],
    candidate_diagnostics: dict[str, Any],
) -> set[str]:
    ids: set[str] = set()

    def add_if_duplicate(item: dict[str, Any]) -> None:
        reason = " ".join(
            str(item.get(key) or "")
            for key in ("category", "reason", "contract_reason")
        )
        if "duplicate" not in reason.lower():
            return
        asset_id = str(item.get("asset_id") or item.get("layer_id") or "").strip()
        if asset_id:
            ids.add(asset_id)

    for item in list(storyboard.get("rejected_assets") or []):
        if isinstance(item, dict):
            add_if_duplicate(item)
    for item in list(candidate_diagnostics.get("storyboard_rejected_candidates") or []):
        if isinstance(item, dict):
            add_if_duplicate(item)
    return ids


def _contains_issue_token(values: list[Any], token: str) -> bool:
    token = str(token or "").lower()
    return any(token in str(value or "").lower() for value in values)


def _leak_assets(registered_assets: list[dict[str, Any]], *, asset_family: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for asset in registered_assets:
        if _registered_asset_family(asset) != asset_family:
            continue
        leak_flags = sorted(_asset_flags(asset) & LEAK_CURATION_FLAGS)
        if not leak_flags:
            continue
        item = dict(asset)
        item["issue_reasons"] = [f"curation_flag:{flag}" for flag in leak_flags]
        out.append(item)
    return sorted(out, key=lambda item: str(item.get("asset_id") or ""))


def _registered_asset_family(asset: dict[str, Any]) -> str:
    asset_class = str(asset.get("asset_class") or "")
    kind = str(asset.get("kind") or "")
    asset_id = str(asset.get("asset_id") or "")
    if (
        asset_class in {"source_table", "unparsed_source_table", "source_table_crop_candidate"}
        or kind in {"table", "source_table_crop_candidate"}
        or asset_id.startswith("ingest_table_")
    ):
        return "table"
    if asset_class == "source_figure" or kind in {"image", "figure"} or asset_id.startswith("ingest_fig_"):
        return "figure"
    return "other"


def _asset_flags(asset: dict[str, Any]) -> set[str]:
    return {
        str(flag or "").strip()
        for key in ("curation_flags", "crop_quality_flags", "severe_crop_flags")
        for flag in list(asset.get(key) or [])
        if str(flag or "").strip()
    }


def _review_assets_are_duplicates(a: dict[str, Any], b: dict[str, Any]) -> bool:
    a_id = str(a.get("asset_id") or "")
    b_id = str(b.get("asset_id") or "")
    if a_id and b_id and a_id == b_id:
        return True
    for key in ("source_group_id", "output_sha256", "sha256", "image_sha256", "source_sha256"):
        a_value = str(a.get(key) or "").strip()
        b_value = str(b.get(key) or "").strip()
        if a_value and b_value and a_value == b_value:
            return True
    a_path = _review_asset_output_identity(a)
    b_path = _review_asset_output_identity(b)
    if a_path and b_path and a_path == b_path:
        return True
    a_page = _safe_int(a.get("source_page") or a.get("page"), -1)
    b_page = _safe_int(b.get("source_page") or b.get("page"), -2)
    if a_page < 0 or a_page != b_page:
        return False
    a_xref = str(a.get("source_image_xref") or a.get("image_xref") or "").strip()
    b_xref = str(b.get("source_image_xref") or b.get("image_xref") or "").strip()
    if a_xref and b_xref and a_xref == b_xref:
        return True
    a_bbox = _review_asset_bbox_tuple(a)
    b_bbox = _review_asset_bbox_tuple(b)
    if not a_bbox or not b_bbox:
        return False
    iou, coverage = _review_bbox_overlap_stats(a_bbox, b_bbox)
    return iou >= 0.72 or coverage >= 0.88


def _review_asset_output_identity(asset: dict[str, Any]) -> str:
    for key in ("output_file", "thumbnail_file", "src_path"):
        value = str(asset.get(key) or "").strip()
        if value:
            return re.sub(r"/+", "/", value).lower()
    return ""


def _review_asset_high_confidence_captioned(asset: dict[str, Any]) -> bool:
    score = _safe_int(asset.get("visual_score"), 0)
    if score < 72:
        return False
    if not (
        asset.get("captioned_source_group")
        or asset.get("source_group_id")
        or asset.get("source_group_label")
        or asset.get("source_group_caption")
    ):
        return False
    caption = " ".join(
        str(asset.get(key) or "")
        for key in ("caption", "caption_text", "caption_full", "caption_short", "source_group_caption", "title")
    ).strip()
    return bool(caption)


def _review_asset_bbox_tuple(asset: dict[str, Any]) -> tuple[float, float, float, float] | None:
    for key in ("source_bbox_pdf_points", "source_bbox", "pdf_bbox", "bbox"):
        value = asset.get(key)
        if isinstance(value, (list, tuple)) and len(value) >= 4:
            try:
                x0, y0, x1, y1 = [float(v) for v in value[:4]]
            except (TypeError, ValueError):
                continue
            if x1 > x0 and y1 > y0:
                return x0, y0, x1, y1
        if isinstance(value, dict):
            try:
                x = float(value.get("x"))
                y = float(value.get("y"))
                w = float(value.get("w"))
                h = float(value.get("h"))
            except (TypeError, ValueError):
                continue
            if w > 0 and h > 0:
                return x, y, x + w, y + h
    return None


def _review_bbox_overlap_stats(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> tuple[float, float]:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0, 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    a_area = max(0.0, (ax1 - ax0) * (ay1 - ay0))
    b_area = max(0.0, (bx1 - bx0) * (by1 - by0))
    union = a_area + b_area - inter
    iou = inter / union if union > 0 else 0.0
    coverage = inter / min(a_area, b_area) if min(a_area, b_area) > 0 else 0.0
    return iou, coverage


def _paper_review_metrics(
    *,
    status: str,
    registered_asset_count: int,
    planner_visible_registered_asset_count: int,
    designer_eligible_asset_count: int,
    designer_ineligible_assets: list[dict[str, Any]],
    contract_bad_assets: list[dict[str, Any]],
    selected_asset_review: dict[str, Any],
    source_visual_metrics: dict[str, Any],
    table_leak_assets: list[dict[str, Any]],
    figure_leak_assets: list[dict[str, Any]],
    zero_asset_paper: bool,
    low_asset_paper: bool,
) -> dict[str, Any]:
    selected_metrics = (
        selected_asset_review.get("metrics")
        if isinstance(selected_asset_review.get("metrics"), dict)
        else {}
    )
    source_shortfall = _safe_int(source_visual_metrics.get("source_visual_shortfall"), 0)
    supplemental_task_count = _safe_int(
        source_visual_metrics.get("supplemental_native_visual_task_count"),
        0,
    )
    unbacked_shortfall = max(0, source_shortfall - supplemental_task_count)
    return {
        "status": status,
        "registered_asset_count": registered_asset_count,
        "planner_visible_registered_asset_count": planner_visible_registered_asset_count,
        "designer_eligible_asset_count": designer_eligible_asset_count,
        "designer_ineligible_asset_count": len(designer_ineligible_assets),
        "designer_ineligible_planner_visible_asset_count": sum(
            1 for item in designer_ineligible_assets if item.get("planner_visible")
        ),
        "contract_bad_asset_count": len(contract_bad_assets),
        "contract_bad_selected_asset_count": sum(
            1 for item in contract_bad_assets if item.get("contract_selected")
        ),
        "contract_bad_planner_visible_asset_count": sum(
            1 for item in contract_bad_assets if item.get("planner_visible")
        ),
        "selected_asset_count": _safe_int(selected_metrics.get("selected_asset_count"), 0),
        "selected_duplicate_count": _safe_int(selected_metrics.get("selected_duplicate_count"), 0),
        "selected_weak_quality_count": _safe_int(selected_metrics.get("selected_weak_quality_count"), 0),
        "selected_edge_text_residue_count": _safe_int(
            selected_metrics.get("selected_edge_text_residue_count"),
            0,
        ),
        "selected_forbidden_intersection_count": _safe_int(
            selected_metrics.get("selected_forbidden_intersection_count"),
            0,
        ),
        "selected_source_visual_count": _safe_int(
            source_visual_metrics.get("selected_source_visual_count"),
            0,
        ),
        "poster_visual_unit_target": _safe_int(source_visual_metrics.get("poster_visual_unit_target"), 0),
        "source_visual_shortfall": source_shortfall,
        "supplemental_native_visual_task_count": supplemental_task_count,
        "unbacked_source_visual_shortfall": unbacked_shortfall,
        "table_leak_count": len(table_leak_assets),
        "table_leak_planner_visible_count": sum(
            1 for item in table_leak_assets if item.get("planner_visible")
        ),
        "figure_leak_count": len(figure_leak_assets),
        "figure_leak_planner_visible_count": sum(
            1 for item in figure_leak_assets if item.get("planner_visible")
        ),
        "zero_asset_paper": zero_asset_paper,
        "low_asset_paper": low_asset_paper,
        "low_asset_threshold": LOW_DESIGNER_ELIGIBLE_ASSET_THRESHOLD,
    }


def _golden_check_summary(
    *,
    review_metrics: dict[str, Any],
    anchor_checks: list[dict[str, Any]],
    quick_brief_has_high_priority_section: bool,
) -> dict[str, Any]:
    expected_anchor_count = len(anchor_checks)
    provenance_pass_count = sum(1 for item in anchor_checks if item.get("provenance"))
    primary_pass_count = sum(1 for item in anchor_checks if item.get("storyboard_primary"))
    contract_selected_pass_count = sum(1 for item in anchor_checks if item.get("contract_selected"))
    high_priority_pass_count = sum(1 for item in anchor_checks if item.get("contract_high_priority"))
    return {
        **review_metrics,
        "expected_anchor_count": expected_anchor_count,
        "expected_anchor_provenance_pass_count": provenance_pass_count,
        "expected_anchor_storyboard_primary_pass_count": primary_pass_count,
        "expected_anchor_contract_selected_pass_count": contract_selected_pass_count,
        "expected_anchor_contract_high_priority_pass_count": high_priority_pass_count,
        "expected_anchor_all_provenance": expected_anchor_count == provenance_pass_count,
        "expected_anchor_all_storyboard_primary": expected_anchor_count == primary_pass_count,
        "expected_anchor_all_contract_selected": expected_anchor_count == contract_selected_pass_count,
        "expected_anchor_all_contract_high_priority": expected_anchor_count == high_priority_pass_count,
        "quick_brief_has_high_priority_section": quick_brief_has_high_priority_section,
        "ingest_audit_candidate_pass": (
            review_metrics.get("status") == "ok"
            and not review_metrics.get("zero_asset_paper")
            and review_metrics.get("contract_bad_selected_asset_count") == 0
            and _review_metrics_pass(review_metrics)
        ),
    }


def _review_metrics_pass(review_metrics: dict[str, Any]) -> bool:
    source_shortfall = _safe_int(review_metrics.get("source_visual_shortfall"), 0)
    supplemental_task_count = _safe_int(
        review_metrics.get("supplemental_native_visual_task_count"),
        0,
    )
    unbacked_shortfall = _safe_int(
        review_metrics.get("unbacked_source_visual_shortfall"),
        max(0, source_shortfall - supplemental_task_count),
    )
    return (
        review_metrics.get("status") == "ok"
        and review_metrics.get("contract_bad_selected_asset_count") == 0
        and review_metrics.get("contract_bad_planner_visible_asset_count") == 0
        and review_metrics.get("designer_ineligible_planner_visible_asset_count") == 0
        and review_metrics.get("selected_duplicate_count") == 0
        and review_metrics.get("selected_weak_quality_count") == 0
        and review_metrics.get("selected_edge_text_residue_count") == 0
        and review_metrics.get("selected_forbidden_intersection_count") == 0
        and unbacked_shortfall == 0
        and review_metrics.get("table_leak_planner_visible_count") == 0
        and review_metrics.get("figure_leak_planner_visible_count") == 0
    )


def _compact_anchor_asset(asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "asset_id": asset.get("asset_id"),
        "kind": asset.get("kind"),
        "extract_strategy": asset.get("extract_strategy"),
        "anchor_kind": asset.get("anchor_kind"),
        "anchor_label": asset.get("anchor_label"),
        "anchor_reason": asset.get("anchor_reason"),
        "source_page": asset.get("source_page"),
        "source_bbox_pdf_points": asset.get("source_bbox_pdf_points"),
        "visual_role": asset.get("visual_role"),
        "visual_score": asset.get("visual_score"),
        "curation_flags": list(asset.get("curation_flags") or []),
        "crop_quality_flags": list(asset.get("crop_quality_flags") or []),
        "severe_crop_flags": list(asset.get("severe_crop_flags") or []),
        "designer_eligible": asset.get("designer_eligible"),
        "planner_eligible": asset.get("planner_eligible"),
        "planner_visible": asset.get("planner_visible"),
        "designer_reject_reasons": list(asset.get("designer_reject_reasons") or []),
        "planner_reject_reasons": list(asset.get("planner_reject_reasons") or []),
        "curation_reason": asset.get("curation_reason"),
        "caption_short": asset.get("caption_short"),
        "output_file": asset.get("output_file"),
        "size": [asset.get("output_width_px"), asset.get("output_height_px")],
    }


def _pdf_summary_for_path(raw_summaries: Any, pdf_path: Path) -> dict[str, Any]:
    if not isinstance(raw_summaries, list):
        return {}
    target = str(pdf_path)
    try:
        target_resolved = str(pdf_path.resolve())
    except OSError:
        target_resolved = target
    for item in raw_summaries:
        if not isinstance(item, dict) or item.get("type") != "pdf":
            continue
        raw_file = str(item.get("file") or "")
        try:
            raw_resolved = str(Path(raw_file).expanduser().resolve())
        except OSError:
            raw_resolved = raw_file
        if raw_file == target or raw_resolved == target_resolved:
            return item
    return {}


def _planner_visible_asset_ids(
    storyboard: dict[str, Any],
    contract: dict[str, Any],
    content: dict[str, Any],
) -> set[str]:
    ids: set[str] = set()

    def add(value: Any) -> None:
        asset_id = str(value or "").strip()
        if asset_id:
            ids.add(asset_id)

    for key in ("selected_assets", "primary_assets", "secondary_assets", "reserve_assets"):
        for item in list(storyboard.get(key) or []):
            if isinstance(item, dict):
                add(item.get("asset_id") or item.get("layer_id"))
    for item in list(contract.get("selected_visuals") or []):
        if isinstance(item, dict):
            add(item.get("layer_id") or item.get("asset_id"))
    for key in ("high_priority_assets", "selected_assets", "reserve_assets"):
        for item in list(((contract.get("source_asset_tiers") or {}).get(key)) or []):
            if isinstance(item, dict):
                add(item.get("layer_id") or item.get("asset_id"))
    visual_selection = content.get("visual_selection") if isinstance(content.get("visual_selection"), dict) else {}
    for key in (
        "primary_visual_ids",
        "high_priority_visual_ids",
        "storyboard_selected_asset_ids",
        "storyboard_primary_asset_ids",
        "storyboard_secondary_asset_ids",
        "storyboard_reserve_asset_ids",
    ):
        for item in list(visual_selection.get(key) or []):
            add(item)
    return ids


def _compact_registered_asset(
    asset: dict[str, Any],
    rec: dict[str, Any],
    *,
    planner_visible: bool,
) -> dict[str, Any]:
    asset_id = str(asset.get("asset_id") or "")
    rec = rec if isinstance(rec, dict) else {}
    debug_only = _debug_only_registered_asset(asset, rec)
    headers = rec.get("headers") if isinstance(rec.get("headers"), list) else []
    rows = rec.get("rows") if isinstance(rec.get("rows"), list) else []
    out = _compact_anchor_asset(asset)
    out.update({
        "asset_class": _registered_asset_class(asset, rec),
        "planner_visible": bool(planner_visible) and not debug_only,
        "debug_only": debug_only,
        "table_parse_status": rec.get("table_parse_status") or "",
        "table_parse_error": rec.get("table_parse_error") or "",
        "table_visual_source": rec.get("table_visual_source") or "",
        "headers_count": len(headers),
        "rows_count": len(rows),
        "source_group_id": rec.get("source_group_id") or asset.get("source_group_id") or "",
        "source_group_label": rec.get("source_group_label") or asset.get("source_group_label") or "",
        "source_group_caption": rec.get("source_group_caption") or asset.get("source_group_caption") or "",
    })
    if not out.get("output_file") and rec.get("src_path"):
        out["output_file"] = rec.get("src_path")
    if not out.get("asset_id"):
        out["asset_id"] = asset_id
    return out


def _registered_asset_class(asset: dict[str, Any], rec: dict[str, Any]) -> str:
    if _is_unparsed_source_table(asset, rec):
        if str(asset.get("kind") or rec.get("kind") or "") == "table":
            return "unparsed_source_table"
        return "source_table_crop_candidate"
    kind = str(asset.get("kind") or rec.get("kind") or "")
    if kind == "table":
        return "source_table"
    if kind in {"image", "figure"} or str(asset.get("asset_id") or "").startswith("ingest_fig_"):
        return "source_figure"
    return kind or "source_asset"


def _debug_only_registered_asset(asset: dict[str, Any], rec: dict[str, Any]) -> bool:
    return _is_source_table_crop_candidate(asset, rec)


def _is_unparsed_source_table(asset: dict[str, Any], rec: dict[str, Any]) -> bool:
    if str(rec.get("table_parse_status") or asset.get("table_parse_status") or "") == "unparsed_source_crop":
        return True
    return _is_source_table_crop_candidate(asset, rec)


def _is_source_table_crop_candidate(asset: dict[str, Any], rec: dict[str, Any]) -> bool:
    kind = str(asset.get("kind") or rec.get("kind") or "")
    strategy = str(asset.get("extract_strategy") or rec.get("extract_strategy") or "")
    return kind == "source_table_crop_candidate" or strategy == "source_table_crop_candidate"


def _captioned_visual_groups(
    *,
    manifest: dict[str, Any],
    assets: list[dict[str, Any]],
    rendered: dict[str, Any],
    planner_visible_ids: set[str],
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}

    def add_group(kind: str, label: str, page: Any, caption: str, source: str) -> None:
        kind = kind if kind in {"figure", "table"} else "figure"
        label = str(label or "").lower().strip()
        caption = " ".join(str(caption or "").split())
        key = _caption_group_key(kind, label, page, caption)
        group = groups.setdefault(key, {
            "group_id": key,
            "kind": kind,
            "label": label,
            "pages": [],
            "caption": caption,
            "sources": [],
            "registered_asset_ids": [],
            "formal_table_asset_ids": [],
            "unparsed_table_candidate_ids": [],
            "planner_visible_asset_ids": [],
            "debug_only_asset_ids": [],
        })
        if page not in group["pages"] and page not in (None, ""):
            group["pages"].append(page)
        if caption and not group.get("caption"):
            group["caption"] = caption
        if source and source not in group["sources"]:
            group["sources"].append(source)

    for kind, key in (("figure", "figures"), ("table", "tables")):
        for item in list(manifest.get(key) or []):
            if not isinstance(item, dict):
                continue
            caption = str(item.get("caption") or item.get("title") or "")
            label_kind, label = _caption_kind_label_from_text(caption)
            if label_kind and label_kind != kind:
                continue
            add_group(kind, label, item.get("page"), caption, f"manifest.{key}")

    for asset in assets:
        kind, label = _asset_caption_kind_label(asset)
        caption = str(asset.get("caption_full") or asset.get("caption_short") or "")
        if not label and not caption:
            continue
        add_group(kind, label, asset.get("source_page"), caption, "registered_asset")

    for asset in assets:
        asset_id = str(asset.get("asset_id") or "")
        if not asset_id:
            continue
        rec = rendered.get(asset_id) if isinstance(rendered.get(asset_id), dict) else {}
        for group in groups.values():
            if not _asset_matches_caption_group(asset, group):
                continue
            _append_unique(group["registered_asset_ids"], asset_id)
            if asset_id in planner_visible_ids and not _debug_only_registered_asset(asset, rec):
                _append_unique(group["planner_visible_asset_ids"], asset_id)
            if _is_unparsed_source_table(asset, rec):
                _append_unique(group["unparsed_table_candidate_ids"], asset_id)
                if _debug_only_registered_asset(asset, rec):
                    _append_unique(group["debug_only_asset_ids"], asset_id)
            elif str(asset.get("kind") or rec.get("kind") or "") == "table":
                _append_unique(group["formal_table_asset_ids"], asset_id)

    out = []
    for group in groups.values():
        if group["formal_table_asset_ids"]:
            status = "formal_table_registered"
        elif group["unparsed_table_candidate_ids"]:
            status = "unparsed_source_table_crop"
        elif group["registered_asset_ids"]:
            status = "registered_source_visual"
        else:
            status = "caption_only_no_registered_asset"
        item = dict(group)
        item["status"] = status
        item["registered_count"] = len(group["registered_asset_ids"])
        item["planner_visible_count"] = len(group["planner_visible_asset_ids"])
        out.append(item)
    return sorted(
        out,
        key=lambda item: (
            _safe_int((item.get("pages") or [999])[0], 999),
            0 if item.get("kind") == "figure" else 1,
            _caption_label_sort_key(item.get("label")),
            str(item.get("caption") or ""),
        ),
    )


def _candidate_diagnostics(
    *,
    assets: list[dict[str, Any]],
    rendered: dict[str, Any],
    storyboard: dict[str, Any],
    events: list[dict[str, Any]],
    planner_visible_ids: set[str],
) -> dict[str, Any]:
    debug_only = [
        _compact_registered_asset(
            asset,
            rendered.get(str(asset.get("asset_id") or "")) or {},
            planner_visible=str(asset.get("asset_id") or "") in planner_visible_ids,
        )
        for asset in assets
        if _debug_only_registered_asset(asset, rendered.get(str(asset.get("asset_id") or "")) or {})
    ]
    storyboard_rejected: list[dict[str, Any]] = []
    for item in list(storyboard.get("rejected_assets") or []):
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "")
        storyboard_rejected.append({
            "asset_id": item.get("asset_id"),
            "category": "duplicate" if "duplicate" in reason.lower() else "storyboard_rejected",
            "reason": reason,
        })

    event_counts = {
        "figure_candidate_dropped_count": 0,
        "table_candidate_dropped_count": 0,
        "table_candidates_dropped_by_figure_overlap_count": 0,
        "registered_table_fallback_candidate_count": 0,
    }
    reject_events: list[dict[str, Any]] = []
    for event in events:
        name = str(event.get("event") or "")
        if name == "ingest.pdf.register":
            event_counts["figure_candidate_dropped_count"] += _safe_int(event.get("dropped"), 0)
        elif name == "ingest.pdf.register_tables":
            event_counts["table_candidate_dropped_count"] += _safe_int(event.get("dropped"), 0)
            event_counts["registered_table_fallback_candidate_count"] += _safe_int(event.get("fallback_candidates"), 0)
            event_counts["registered_table_fallback_candidate_count"] += _safe_int(event.get("unparsed_source_tables"), 0)
        elif name == "ingest.pdf.table_candidates":
            event_counts["table_candidates_dropped_by_figure_overlap_count"] += _safe_int(
                event.get("dropped_by_figure_overlap"), 0
            )
        if name in {"ingest.pdf.reject_fake", "ingest.pdf.reject_table"}:
            reject_events.append({
                "category": "ingest_rejected_table" if name.endswith("reject_table") else "ingest_rejected_figure",
                "page": event.get("page"),
                "path": event.get("path"),
                "reason": event.get("reason") or "",
            })

    dropped_or_duplicate = (
        [{"category": "debug_only_registered_asset", **item} for item in debug_only]
        + storyboard_rejected
        + reject_events
    )
    return {
        "debug_only_candidate_count": len(debug_only),
        "debug_only_candidates": debug_only,
        "storyboard_rejected_count": len(storyboard_rejected),
        "storyboard_duplicate_rejected_count": sum(
            1 for item in storyboard_rejected if item.get("category") == "duplicate"
        ),
        "storyboard_rejected_candidates": storyboard_rejected,
        "reject_event_count": len(reject_events),
        "reject_events": reject_events,
        "event_counts": event_counts,
        "dropped_or_debug_candidate_count": len(dropped_or_duplicate),
        "dropped_or_duplicate_candidates": dropped_or_duplicate,
    }


def _read_run_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def _caption_group_key(kind: str, label: str, page: Any, caption: str) -> str:
    if label:
        return f"{kind}:{label}"
    norm = re.sub(r"[^a-z0-9]+", "-", caption.lower()).strip("-")
    if norm:
        return f"{kind}:caption:{norm[:96]}"
    return f"{kind}:page:{page or '?'}"


def _asset_caption_kind_label(asset: dict[str, Any]) -> tuple[str, str]:
    anchor_kind = str(asset.get("anchor_kind") or "").lower()
    anchor_label = str(asset.get("anchor_label") or "").lower().strip()
    if anchor_kind in {"figure", "table"} and anchor_label:
        return anchor_kind, anchor_label
    caption = " ".join(str(asset.get(key) or "") for key in ("caption_full", "caption_short"))
    label_kind, label = _caption_kind_label_from_text(caption)
    if label_kind and label:
        return label_kind, label
    kind = str(asset.get("kind") or "")
    if kind in {"table", "source_table_crop_candidate"}:
        return "table", ""
    return "figure", ""


def _caption_kind_label_from_text(text: str) -> tuple[str, str]:
    match = re.search(r"\b(?P<kind>fig(?:ure)?\.?|table)\s*(?P<label>[0-9]+[a-z]?)\b", text, flags=re.IGNORECASE)
    if not match:
        return "", ""
    raw_kind = match.group("kind").lower()
    return ("table" if raw_kind.startswith("table") else "figure", match.group("label").lower())


def _asset_matches_caption_group(asset: dict[str, Any], group: dict[str, Any]) -> bool:
    kind, label = _asset_caption_kind_label(asset)
    group_kind = str(group.get("kind") or "")
    group_label = str(group.get("label") or "")
    if group_label:
        return kind == group_kind and label == group_label
    caption = str(asset.get("caption_full") or asset.get("caption_short") or "")
    return _caption_group_key(kind, label, asset.get("source_page"), caption) == group.get("group_id")


def _caption_label_sort_key(label: Any) -> tuple[int, str]:
    text = str(label or "")
    match = re.match(r"([0-9]+)([a-z]?)", text)
    if not match:
        return (999, text)
    return (_safe_int(match.group(1), 999), match.group(2) or "")


def _append_unique(values: list[Any], value: Any) -> None:
    if value not in values:
        values.append(value)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _audit_canvas_plan() -> dict[str, Any]:
    return {
        "preset_id": "cvpr-landscape",
        "canvas": {"w_px": 3072, "h_px": 1536, "dpi": 96},
        "density_budget": {
            "target_visuals_min": 6,
            "target_visuals_max": 10,
            "max_visuals": 10,
            "visual_area_min": 0.34,
        },
        "body_grid": {"layout_mode": "editorial_flow", "cols": 3},
    }


def _audit_prompt() -> str:
    return (
        "Generate a dense academic paper poster from the attached paper. "
        "Use the paper title, authors, and institution names in the header; "
        "use local source figures/tables as evidence in the body."
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-") or "paper"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _copy_if_exists(src: Path, dst: Path) -> None:
    if src.exists():
        shutil.copy2(src, dst)


def _write_protected_table_candidates_contact_sheet(
    run_dir: Path,
    assets: list[dict[str, Any]],
) -> Path | None:
    if not assets:
        return None
    try:
        cols = 2
        cell_w, cell_h = 760, 460
        thumb_w, thumb_h = 710, 320
        rows = (len(assets) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), "#f8f8f4")
        draw = ImageDraw.Draw(sheet)
        font = ImageFont.load_default()
        small_font = ImageFont.load_default()
        for idx, asset in enumerate(assets):
            col = idx % cols
            row = idx // cols
            x = col * cell_w
            y = row * cell_h
            draw.rectangle((x + 10, y + 10, x + cell_w - 10, y + cell_h - 10),
                           outline="#d6d3cc", width=2)
            src = _asset_output_path(run_dir, asset)
            if src is not None and src.exists():
                with Image.open(src) as img:
                    img = img.convert("RGB")
                    img.thumbnail((thumb_w, thumb_h), Image.Resampling.LANCZOS)
                    px = x + 25 + (thumb_w - img.width) // 2
                    py = y + 20 + (thumb_h - img.height) // 2
                    sheet.paste(img, (px, py))
            else:
                draw.rectangle((x + 25, y + 20, x + 25 + thumb_w, y + 20 + thumb_h),
                               outline="#b8b5ad", fill="#ece9e2")
                draw.text((x + 40, y + 165), "table candidate crop unavailable",
                          fill="#5f5a50", font=font)

            label = (
                f"{asset.get('asset_id', '')} · {asset.get('kind', '')} · "
                f"p.{asset.get('source_page', '?')} · "
                f"Table {asset.get('anchor_label', '?')}"
            )
            draw.text((x + 25, y + 360), label[:90], fill="#1f2933", font=font)
            caption = str(asset.get("caption_short") or asset.get("caption_full") or "")
            for line_i, line in enumerate(_wrap_text(caption, 96)[:3]):
                draw.text((x + 25, y + 382 + line_i * 18),
                          line, fill="#55514a", font=small_font)
        out_path = run_dir / "protected_table_candidates_contact_sheet.png"
        sheet.save(out_path)
        return out_path
    except Exception:
        return None


def _asset_output_path(run_dir: Path, asset: dict[str, Any]) -> Path | None:
    raw = str(asset.get("output_file") or asset.get("thumbnail_file") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute():
        return path
    return run_dir / path


def _wrap_text(text: str, width: int) -> list[str]:
    text = " ".join(str(text or "").replace("\n", " ").split())
    if not text:
        return []
    words = text.split(" ")
    lines: list[str] = []
    cur = ""
    for word in words:
        nxt = word if not cur else f"{cur} {word}"
        if len(nxt) <= width:
            cur = nxt
            continue
        if cur:
            lines.append(cur)
        cur = word[:width]
    if cur:
        lines.append(cur)
    return lines


def _find_contact_sheet(layers_dir: Path) -> Path | None:
    candidates = [
        layers_dir / "contact_sheet.png",
        layers_dir / "ingest_contact_sheet_paper.png",
    ]
    candidates.extend(sorted(layers_dir.glob("ingest_contact_sheet_*.png")))
    for path in candidates:
        if path.exists():
            return path
    return None


def _selected_review_html(out_dir: Path, paper: dict[str, Any]) -> str:
    review = paper.get("selected_asset_review") if isinstance(paper.get("selected_asset_review"), dict) else {}
    assets = [item for item in list(review.get("assets") or []) if isinstance(item, dict)]
    if not assets:
        return ""
    metrics = review.get("metrics") if isinstance(review.get("metrics"), dict) else {}
    selected_source_count = paper.get("selected_source_visual_count", metrics.get("selected_source_visual_count", 0))
    poster_visual_target = paper.get("poster_visual_unit_target", metrics.get("poster_visual_unit_target", 0))
    source_shortfall = paper.get("source_visual_shortfall", metrics.get("source_visual_shortfall", 0))
    supplemental_task_count = paper.get(
        "supplemental_native_visual_task_count",
        metrics.get("supplemental_native_visual_task_count", 0),
    )
    unbacked_shortfall = paper.get(
        "unbacked_source_visual_shortfall",
        metrics.get("unbacked_source_visual_shortfall", 0),
    )
    shortfall_badge = "unbacked source visual shortfall" if _safe_int(unbacked_shortfall, 0) > 0 else "source visual shortfall"
    shortfall_html = (
        f"<p><span class='badge issue'>{shortfall_badge}</span> "
        f"selectedSource={selected_source_count} target={poster_visual_target} "
        f"shortfall={source_shortfall} supplementalNativeTasks={supplemental_task_count} "
        f"unbacked={unbacked_shortfall}</p>"
        if _safe_int(source_shortfall, 0) > 0 else ""
    )
    cards = []
    for asset in assets:
        asset_id = str(asset.get("asset_id") or "")
        img = _asset_image_tag(out_dir, paper, asset, alt=f"{asset_id} selected source visual")
        flags = sorted(_asset_flags(asset))
        reasons = [
            str(reason)
            for reason in list(asset.get("selected_review_reasons") or [])
            if str(reason or "").strip()
        ]
        status_badges = "".join(
            f"<span class='badge issue'>{_escape(label)}</span>"
            for label, key in (
                ("duplicate", "selected_duplicate"),
                ("weak", "selected_weak_quality"),
                ("edge text", "selected_edge_text_residue"),
                ("forbidden", "selected_forbidden_intersection"),
            )
            if asset.get(key)
        )
        flags_html = _inline_list_html(flags) or "<span class='muted'>none</span>"
        reasons_html = _inline_list_html(reasons) or "<span class='muted'>none</span>"
        selected_status = status_badges or "<span class='badge ok'>ok</span>"
        cards.append(
            "<article class='asset-card'>"
            f"{img}"
            f"<h4>{_escape(asset_id)}</h4>"
            f"<p>{selected_status}</p>"
            f"<p><strong>Flags:</strong> {flags_html}</p>"
            f"<p><strong>Reasons:</strong> {reasons_html}</p>"
            "</article>"
        )
    return (
        "<h3>Selected Source Assets</h3>"
        f"<p>selected={metrics.get('selected_asset_count', len(assets))} "
        f"selectedSource={selected_source_count} "
        f"target={poster_visual_target} "
        f"shortfall={source_shortfall} "
        f"duplicates={metrics.get('selected_duplicate_count', 0)} "
        f"weakQuality={metrics.get('selected_weak_quality_count', 0)} "
        f"edgeTextResidue={metrics.get('selected_edge_text_residue_count', 0)} "
        f"forbiddenIntersection={metrics.get('selected_forbidden_intersection_count', 0)}</p>"
        + shortfall_html
        + "<div class='asset-grid'>"
        + "".join(cards)
        + "</div>"
    )


def _supplemental_native_visual_tasks_html(paper: dict[str, Any]) -> str:
    tasks = [
        item for item in list(paper.get("supplemental_native_visual_tasks") or [])
        if isinstance(item, dict)
    ]
    task_count = _safe_int(paper.get("supplemental_native_visual_task_count"), len(tasks))
    if not tasks and task_count <= 0:
        return ""
    if not tasks:
        return (
            "<h3>Supplemental Native Visual Tasks</h3>"
            f"<p>taskCount={task_count}; task details were not included in the contract/brief output.</p>"
        )
    rows = []
    for task in tasks:
        task_id = str(task.get("task_id") or task.get("id") or task.get("kind") or "")
        title = str(task.get("title") or task.get("role") or "")
        instruction = str(task.get("instruction") or task.get("description") or task.get("purpose") or "")
        sources = task.get("source_text_roles") or task.get("source_ids") or []
        rows.append(
            "<tr>"
            f"<td>{_escape(task_id)}</td>"
            f"<td>{_escape(title)}</td>"
            f"<td>{_escape(instruction)}</td>"
            f"<td>{_inline_list_html(list(sources) if isinstance(sources, list) else [])}</td>"
            "</tr>"
        )
    return (
        "<h3>Supplemental Native Visual Tasks</h3>"
        f"<p>taskCount={task_count}</p>"
        "<table class='candidate-table'><thead><tr>"
        "<th>ID</th><th>Title</th><th>Instruction</th><th>Sources</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _debug_only_candidates_html(paper: dict[str, Any]) -> str:
    diagnostics = (
        paper.get("candidate_diagnostics")
        if isinstance(paper.get("candidate_diagnostics"), dict)
        else {}
    )
    candidates = [
        item for item in list(diagnostics.get("debug_only_candidates") or [])
        if isinstance(item, dict)
    ]
    if not candidates:
        return ""
    rows = []
    for item in candidates:
        flags = sorted(_asset_flags(item))
        reasons = list(item.get("designer_reject_reasons") or item.get("planner_reject_reasons") or [])
        rows.append(
            "<tr>"
            f"<td>{_escape(str(item.get('asset_id') or ''))}</td>"
            f"<td>{_escape(str(item.get('asset_class') or item.get('kind') or ''))}</td>"
            f"<td>{_escape(str(item.get('source_page') or ''))}</td>"
            f"<td>{_inline_list_html(flags)}</td>"
            f"<td>{_inline_list_html(reasons)}</td>"
            "</tr>"
        )
    return (
        "<h3>Debug-only table candidates</h3>"
        "<table class='candidate-table'><thead><tr>"
        "<th>ID</th><th>Class</th><th>Page</th><th>Flags</th><th>Reasons</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _asset_image_tag(out_dir: Path, paper: dict[str, Any], asset: dict[str, Any], *, alt: str) -> str:
    run_dir_raw = str(paper.get("run_dir") or "").strip()
    run_dir = Path(run_dir_raw) if run_dir_raw else out_dir
    src_path = _asset_output_path(run_dir, asset)
    if src_path is None:
        return "<div class='missing-thumb'>thumbnail unavailable</div>"
    try:
        src = str(src_path.resolve().relative_to(out_dir))
    except (OSError, ValueError):
        src = str(src_path)
    return f'<img class="asset-thumb" src="{_escape(src)}" alt="{_escape(alt)}">'


def _inline_list_html(values: list[Any]) -> str:
    return ", ".join(
        f"<code>{_escape(str(value))}</code>"
        for value in values
        if str(value or "").strip()
    )


def _write_html_index(out_dir: Path, index: dict[str, Any]) -> None:
    cards = []
    selected_cards = []
    for paper in index.get("papers", []):
        if not isinstance(paper, dict):
            continue
        rel = ""
        contact = paper.get("contact_sheet")
        if contact:
            try:
                rel = str(Path(contact).resolve().relative_to(out_dir))
            except ValueError:
                rel = str(contact)
        table_rel = ""
        protected_table_contact = paper.get("protected_table_candidates_contact_sheet")
        if protected_table_contact:
            try:
                table_rel = str(Path(protected_table_contact).resolve().relative_to(out_dir))
            except ValueError:
                table_rel = str(protected_table_contact)
        checks = paper.get("expected_anchor_checks") or []
        check_html = "".join(
            f"<li>{_escape(str(item.get('kind')))} {_escape(str(item.get('label')))}: "
            f"provenance={bool(item.get('provenance'))}, primary={bool(item.get('storyboard_primary'))}, "
            f"contract={bool(item.get('contract_selected'))}</li>"
            for item in checks if isinstance(item, dict)
        )
        img = (
            f'<h3>Main contact sheet</h3><img src="{_escape(rel)}" '
            f'alt="{_escape(str(paper.get("name")))} contact sheet">'
            if rel else ""
        )
        table_img = (
            f'<h3>Audit-only table candidate crops</h3><img src="{_escape(table_rel)}" '
            f'alt="{_escape(str(paper.get("name")))} audit-only table candidates">'
            if table_rel else ""
        )
        selected_review_html = _selected_review_html(out_dir, paper)
        supplemental_tasks_html = _supplemental_native_visual_tasks_html(paper)
        debug_candidates_html = _debug_only_candidates_html(paper)
        manual_notes_html = _manual_notes_html(paper.get("manual_review_notes"))
        details = "".join([
            _details_json("Golden check summary", paper.get("golden_check_summary")),
            _details_json("Selected-only review", paper.get("selected_asset_review")),
            _details_json("Supplemental native visual tasks", paper.get("supplemental_native_visual_tasks")),
            _details_json("Contract bad assets", paper.get("contract_bad_assets")),
            _details_json("Designer-ineligible assets", paper.get("designer_ineligible_assets")),
            _details_json("Table leak assets", paper.get("table_leak_assets")),
            _details_json("Figure leak assets", paper.get("figure_leak_assets")),
            _details_json("Flagged planner-visible assets", paper.get("flagged_planner_visible_assets")),
            _details_json("Captioned visual groups", paper.get("captioned_visual_groups")),
            _details_json("Registered assets", paper.get("registered_assets")),
            _details_json("Registered source tables", paper.get("registered_source_tables")),
            _details_json("Unparsed source tables", paper.get("unparsed_source_tables")),
            _details_json("Dropped / duplicate / debug-only candidates", paper.get("dropped_or_duplicate_candidates")),
        ])
        cards.append(
            "<section>"
            f"<h2>{_escape(str(paper.get('name')))}: {_escape(str(paper.get('status')))}</h2>"
            f"<p>assets={paper.get('asset_count')} plannerVisible={paper.get('planner_visible_registered_asset_count')} "
            f"captionedGroups={paper.get('captioned_visual_group_count')} "
            f"sourceTables={paper.get('registered_source_table_count')} "
            f"unparsedSourceTables={paper.get('unparsed_source_table_count')} "
            f"eligible={paper.get('designer_eligible_asset_count')} "
            f"designerIneligible={paper.get('designer_ineligible_asset_count')} "
            f"contractBad={paper.get('contract_bad_asset_count')} "
            f"contractBadSelected={paper.get('contract_bad_selected_asset_count')} "
            f"selectedSource={paper.get('selected_source_visual_count')} "
            f"sourceShortfall={paper.get('source_visual_shortfall')} "
            f"unbackedShortfall={paper.get('unbacked_source_visual_shortfall')} "
            f"visualTarget={paper.get('poster_visual_unit_target')} "
            f"supplementalNativeTasks={paper.get('supplemental_native_visual_task_count')} "
            f"selectedDuplicates={paper.get('selected_duplicate_count')} "
            f"selectedWeak={paper.get('selected_weak_quality_count')} "
            f"selectedEdgeText={paper.get('selected_edge_text_residue_count')} "
            f"selectedForbidden={paper.get('selected_forbidden_intersection_count')} "
            f"tableLeaks={paper.get('table_leak_count')} "
            f"figureLeaks={paper.get('figure_leak_count')} "
            f"zeroAsset={paper.get('zero_asset_paper')} "
            f"lowAsset={paper.get('low_asset_paper')} "
            f"flaggedVisible={paper.get('flagged_planner_visible_asset_count')} "
            f"droppedDebugCandidates={paper.get('dropped_debug_candidate_count')} "
            f"protected={paper.get('protected_anchor_count')} "
            f"auditOnlyTableCrops={paper.get('protected_table_candidate_count')} "
            f"quickBriefHighPriority={paper.get('quick_brief_has_high_priority_section')}</p>"
            f"{manual_notes_html}<ul>{check_html}</ul>"
            f"{selected_review_html}{supplemental_tasks_html}{debug_candidates_html}{details}{img}{table_img}</section>"
        )
        selected_cards.append(
            "<section>"
            f"<h2>{_escape(str(paper.get('name')))}: {_escape(str(paper.get('status')))}</h2>"
            f"<p>{_escape(str(paper.get('pdf') or ''))}</p>"
            f"<p>selected={paper.get('selected_asset_count')} "
            f"selectedSource={paper.get('selected_source_visual_count')} "
            f"target={paper.get('poster_visual_unit_target')} "
            f"shortfall={paper.get('source_visual_shortfall')} "
            f"unbackedShortfall={paper.get('unbacked_source_visual_shortfall')} "
            f"supplementalNativeTasks={paper.get('supplemental_native_visual_task_count')} "
            f"duplicates={paper.get('selected_duplicate_count')} "
            f"weak={paper.get('selected_weak_quality_count')} "
            f"edgeText={paper.get('selected_edge_text_residue_count')} "
            f"forbidden={paper.get('selected_forbidden_intersection_count')}</p>"
            f"{selected_review_html}{supplemental_tasks_html}{debug_candidates_html}</section>"
        )
    summary = (
        f"<p class='summary'>mode={_escape(str(index.get('mode') or ''))} "
        f"planned={index.get('planned_count', 0)} completed={index.get('completed_count', 0)} "
        f"ok={index.get('ok_count', 0)} failed={index.get('failed_count', 0)} "
        f"contractBad={index.get('contract_bad_asset_count', 0)} "
        f"selectedSource={index.get('selected_source_visual_count', 0)} "
        f"sourceShortfall={index.get('source_visual_shortfall', 0)} "
        f"sourceShortfallPapers={index.get('source_visual_shortfall_paper_count', 0)} "
        f"unbackedShortfall={index.get('unbacked_source_visual_shortfall', 0)} "
        f"unbackedShortfallPapers={index.get('unbacked_source_visual_shortfall_paper_count', 0)} "
        f"supplementalNativeTasks={index.get('supplemental_native_visual_task_count', 0)} "
        f"selectedDuplicates={index.get('selected_duplicate_count', 0)} "
        f"selectedWeak={index.get('selected_weak_quality_count', 0)} "
        f"selectedEdgeText={index.get('selected_edge_text_residue_count', 0)} "
        f"selectedForbidden={index.get('selected_forbidden_intersection_count', 0)} "
        f"designerIneligible={index.get('designer_ineligible_asset_count', 0)} "
        f"tableLeaks={index.get('table_leak_count', 0)} "
        f"figureLeaks={index.get('figure_leak_count', 0)} "
        f"zeroAssetPapers={index.get('zero_asset_paper_count', 0)} "
        f"lowAssetPapers={index.get('low_asset_paper_count', 0)} "
        f"lowAssetThreshold={index.get('low_asset_threshold', LOW_DESIGNER_ELIGIBLE_ASSET_THRESHOLD)} "
        f"cacheDisabled={bool(index.get('cache_disabled'))} "
        f"updated={_escape(str(index.get('updated_at') or ''))}</p>"
    )
    seed_notes = _manual_notes_html(index.get("seed_manual_review_notes"), title="Seed Manual Review Notes")
    review_details = "".join([
        _details_json("Zero-asset papers", index.get("zero_asset_papers")),
        _details_json("Low-asset papers", index.get("low_asset_papers")),
        _details_json("Contract bad assets", index.get("contract_bad_assets")),
        _details_json("Designer-ineligible assets", index.get("designer_ineligible_assets")),
        _details_json("Selected issue assets", index.get("selected_issue_assets")),
        _details_json("Source visual shortfall papers", index.get("source_visual_shortfall_papers")),
        _details_json("Unbacked source visual shortfall papers", index.get("unbacked_source_visual_shortfall_papers")),
        _details_json("Table leak assets", index.get("table_leak_assets")),
        _details_json("Figure leak assets", index.get("figure_leak_assets")),
    ])
    style = (
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;margin:24px;background:#f7f7f4;color:#222}"
        "section{margin:0 0 32px;padding:16px;background:white;border:1px solid #ddd}"
        ".summary{padding:12px 14px;background:#fff;border:1px solid #ddd}"
        ".notes{margin:14px 0;padding:12px 14px;background:#fff8e5;border:1px solid #e5c970}"
        ".notes h2,.notes h3{margin-top:0}.note{margin:8px 0;padding:8px;border-top:1px solid #eadb9d}"
        ".badge{display:inline-block;margin-right:6px;padding:2px 6px;border:1px solid #c7b35d;background:#fffdf2;font-size:12px}"
        ".badge.issue{border-color:#c2410c;background:#fff7ed;color:#7c2d12}.badge.ok{border-color:#16a34a;background:#f0fdf4;color:#14532d}"
        ".asset-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin:12px 0}"
        ".asset-card{border:1px solid #ddd;background:#fbfbf8;padding:10px}.asset-card h4{margin:8px 0 4px;font-size:13px}"
        ".asset-card p{margin:5px 0;font-size:12px;line-height:1.35}.asset-thumb{max-height:180px;object-fit:contain}"
        ".missing-thumb{height:160px;display:flex;align-items:center;justify-content:center;background:#eee;color:#777;border:1px solid #ddd}"
        ".candidate-table{border-collapse:collapse;width:100%;font-size:12px}.candidate-table th,.candidate-table td{border:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}"
        ".muted{color:#777}"
        "img{display:block;width:100%;height:auto;border:1px solid #ddd;background:#fafafa}"
        "details{margin:10px 0}summary{cursor:pointer;font-weight:600}"
        "pre{white-space:pre-wrap;background:#f5f5f2;border:1px solid #e2e0d8;padding:10px;overflow:auto}</style>"
    )
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>PDF Visual Ingest Audit</title>"
        + style
        + "</head><body><h1>PDF Visual Ingest Audit</h1>"
        + "<p><a href='selected_review.html'>Open selected source review only</a></p>"
        + summary
        + review_details
        + seed_notes
        + "".join(cards)
        + "</body></html>"
    )
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    selected_html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Selected Source Visual Review</title>"
        + style
        + "</head><body><h1>Selected Source Visual Review</h1>"
        + "<p><a href='index.html'>Back to full audit</a></p>"
        + summary
        + "".join(selected_cards)
        + "</body></html>"
    )
    (out_dir / "selected_review.html").write_text(selected_html, encoding="utf-8")


def _manual_notes_html(payload: Any, *, title: str = "Manual Review Notes") -> str:
    notes = [item for item in (payload or []) if isinstance(item, dict)]
    if not notes:
        return ""
    body = []
    for item in notes:
        asset_id = str(item.get("asset_id") or "")
        asset_badge = f"<span class='badge'>{_escape(asset_id)}</span>" if asset_id else ""
        body.append(
            "<div class='note'>"
            f"<div><span class='badge'>{_escape(str(item.get('paper') or ''))}</span>"
            f"{asset_badge}<span class='badge'>{_escape(str(item.get('severity') or ''))}</span></div>"
            f"<p>{_escape(str(item.get('note') or ''))}</p>"
            f"<p><strong>General fix hint:</strong> {_escape(str(item.get('general_fix_hint') or ''))}</p>"
            "</div>"
        )
    heading = "h2" if title.startswith("Seed") else "h3"
    return (
        f"<div class='notes'><{heading}>{_escape(title)}</{heading}>"
        + "".join(body)
        + "</div>"
    )


def _details_json(title: str, payload: Any) -> str:
    if not payload:
        return ""
    count = len(payload) if isinstance(payload, list) else ""
    suffix = f" ({count})" if count != "" else ""
    body = json.dumps(payload, indent=2, ensure_ascii=False)
    return (
        f"<details><summary>{_escape(title)}{suffix}</summary>"
        f"<pre>{_escape(body)}</pre></details>"
    )


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


if __name__ == "__main__":
    raise SystemExit(main())
