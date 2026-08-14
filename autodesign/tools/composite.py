"""composite — bundle layers into PSD + SVG + HTML + flattened preview.

PSD = psd-tools 1.11+ PixelLayers (text layers cropped to bbox for size).
SVG = svgwrite with embedded background + real <text> vector elements.
      Fonts subsetted (only used glyphs) and embedded as base64 WOFF2 in @font-face.
HTML = tools.html_renderer — absolute-positioned poster with contenteditable
       text layers, inlined fonts + images (v1.0 #6).
Preview = PIL alpha_composite chain over an RGB white base.
"""

from __future__ import annotations

import base64
import copy
import math
from pathlib import Path
from typing import Any

import svgwrite
from PIL import Image, ImageDraw
from psd_tools import PSDImage
from psd_tools.constants import BlendMode, Compression

from ..util.io import atomic_write_json, sha256_file
from ._contract import ToolContext, obs_error, obs_ok
from ._deck_preview import build_deck_preview_grid
from ._font_embed import build_font_face_css
from .deck_html_renderer import (
    audit_deck_html_layout,
    write_html_first_deck,
)
from .frame_renderer import write_deck_html
from .html_renderer import write_html, write_landing_html
from .paper_poster_renderer import (
    AUTHORED_PAPER_POSTER_REQUIRED_STATE_KEY,
    find_authored_paper_poster_frame,
    is_academic_paper_poster_context,
    render_authored_paper_poster,
    should_use_authored_paper_poster,
)
from .pptx_renderer import (
    _legacy_slot_bbox,
    render_slide_preview_png,
    write_pptx,
    write_pptx_hybrid,
)
from .render_text_layer import measure_text_height, render_text_png
from ..quality_assets import (
    audit_paper_poster_density,
    audit_paper_poster_information,
    count_p0_findings,
    lint_html_quality,
)
from ..config import effective_poster_harness_mode
from ..util.browser_render import (
    downsample_image_to_max_edge,
    export_deck_pdf,
    screenshot_deck_slides,
    screenshot_html,
)
from ..util.layout_grounder import LayoutGroundingResult, ground_html_layout
from ..schema import ArtifactType, CompositionArtifacts, ToolResultRecord
from ..util.design_feedback import build_design_feedback
from ..util.html_artifact import (
    audit_html_artifact_contract,
    canonicalize_design_spec,
    has_legacy_source_marker,
)
from ..util.logging import log
from ..util.poster_plan_contract import audit_poster_plan_contract
from ..util.table_png import render_table_png
from ..util.visual_reference_contract import build_visual_reference_contract


# v2.2 versioning helpers — every composite call writes into its own
# `composites/iter_<N>/` subdirectory so revise loops + critique iters
# don't lose intermediate state. `final/` symlinks track the latest
# iteration for product consumers (cli display, apply-edits source).


def _open_iter_dir(ctx: ToolContext) -> tuple[Path, int]:
    """Allocate the next composite iteration directory.

    Returns `(iter_dir, iter_num)` where iter_dir = `<run_dir>/composites/iter_NN`.
    Caller writes ALL composite outputs (psd / svg / html / preview / slides)
    into this dir. Use `_refresh_final_links` after a successful write so
    consumers can keep using stable paths via `<run_dir>/final/<name>`.
    """
    iter_num = ctx.next_composite_iter()
    iter_dir = ctx.run_dir / "composites" / f"iter_{iter_num:02d}"
    iter_dir.mkdir(parents=True, exist_ok=True)
    return iter_dir, iter_num


def _prior_preview_sha(ctx: ToolContext) -> str | None:
    """Return the sha256 of the *previous* iteration's preview.png if any
    (lets the new composite's payload encode `supersedes_preview_sha256` so
    DPO training can pair pre/post snapshots).

    Call AFTER `_open_iter_dir` — current composite_iter is N (new dir),
    we want N-1 (the prior dir).
    """
    iter_num = int(ctx.state.get("composite_iter") or 0)
    if iter_num <= 1:
        return None
    prior = ctx.run_dir / "composites" / f"iter_{iter_num - 1:02d}" / "preview.png"
    if not prior.exists():
        return None
    try:
        return sha256_file(prior)
    except OSError:
        return None


def _refresh_final_links(iter_dir: Path, ctx: ToolContext, files: list[str]) -> None:
    """Update `<run_dir>/final/<name>` symlinks to point at this iter's files.

    Existing symlinks (or files) at the target paths are removed first so the
    operation is atomic-ish. Uses RELATIVE symlinks so the run_dir is portable
    if copied to another machine. Skips files that don't exist in iter_dir.
    """
    final_dir = ctx.run_dir / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    for fname in files:
        src = iter_dir / fname
        if not src.exists():
            continue
        link = final_dir / fname
        if link.is_symlink() or link.exists():
            link.unlink()
        # Relative path from final/ to composites/iter_NN/<fname>:
        rel = Path("..") / "composites" / iter_dir.name / fname
        link.symlink_to(rel)
    # Also relink subdirectories (deck/slides/) when present.
    for subdir_name in ("slides",):
        src_sub = iter_dir / subdir_name
        if not src_sub.is_dir():
            continue
        link_sub = final_dir / subdir_name
        if link_sub.is_symlink() or link_sub.exists():
            if link_sub.is_symlink():
                link_sub.unlink()
            else:
                # safety: if it's a real dir, leave it (don't blow away product data)
                continue
        link_sub.symlink_to(Path("..") / "composites" / iter_dir.name / subdir_name)


def _maybe_retain_dogfood_best_authored_paper_poster(
    *,
    iter_dir: Path,
    ctx: ToolContext,
    payload: dict[str, Any],
    final_files: list[str],
) -> dict[str, Any]:
    """Keep dogfood final/ links pointed at the best authored poster composite.

    Planner repair turns can make an already-renderable paper poster much worse
    by pushing blocks outside the canvas. In dogfood mode, final artifacts are
    evaluation inputs, so the final symlink should not move from a low-P0
    composite to a later regression.
    """
    if effective_poster_harness_mode(ctx.settings) != "dogfood":
        return payload
    if str(payload.get("artifact_type") or "") != "poster":
        return payload
    if str(payload.get("render_mode") or "") != "authored_html":
        return payload

    rank = _dogfood_authored_poster_rank(payload)
    best = ctx.state.get("dogfood_best_authored_paper_poster")
    density_regression: dict[str, Any] | None = None
    if isinstance(best, dict):
        best_payload = best.get("payload") if isinstance(best.get("payload"), dict) else {}
        density_regression = _dogfood_density_preservation_regression(payload, best_payload)
    if not isinstance(best, dict) or (density_regression is None and rank < tuple(best.get("rank") or ())):
        ctx.state["dogfood_best_authored_paper_poster"] = {
            "iteration": int(payload.get("iteration") or 0),
            "iter_dir": str(iter_dir),
            "rank": rank,
            "payload": copy.deepcopy(payload),
            "composition": _composition_state_record(ctx.state.get("composition")),
            "design_spec": _copy_state_value(ctx.state.get("design_spec")),
            "spec_revision_count": ctx.state.get("spec_revision_count"),
            "last_design_feedback": _copy_state_value(ctx.state.get("last_design_feedback")),
        }
        log(
            "composite.dogfood_best_candidate.updated",
            iteration=payload.get("iteration"),
            rank=list(rank),
            dom_p0=payload.get("paper_poster_dom_p0_count"),
            blocker_count=((payload.get("design_feedback") or {}).get("counts") or {}).get("blocker"),
            density_preservation="pass",
        )
        return payload

    best_rank = tuple(best.get("rank") or ())
    best_iter_dir = Path(str(best.get("iter_dir") or ""))
    if best_iter_dir.exists():
        _refresh_final_links(best_iter_dir, ctx, final_files)
    composition_record = best.get("composition")
    if isinstance(composition_record, dict):
        ctx.state["composition"] = CompositionArtifacts(**composition_record)
    if best.get("design_spec") is not None:
        ctx.state["design_spec"] = _copy_state_value(best.get("design_spec"))
    if best.get("spec_revision_count") is not None:
        ctx.state["spec_revision_count"] = best.get("spec_revision_count")
    if best.get("last_design_feedback") is not None:
        ctx.state["last_design_feedback"] = _copy_state_value(best.get("last_design_feedback"))

    restored = copy.deepcopy(best.get("payload") or payload)
    restored["dogfood_best_candidate_retained"] = True
    restored["dogfood_rejected_iteration"] = int(payload.get("iteration") or 0)
    restored["dogfood_rejected_rank"] = list(rank)
    restored["dogfood_best_rank"] = list(best_rank)
    if density_regression:
        restored["dogfood_density_preservation_rejected"] = True
        restored["dogfood_density_preservation_regression"] = density_regression
    log(
        "composite.dogfood_candidate_regression_reverted",
        rejected_iteration=payload.get("iteration"),
        best_iteration=best.get("iteration"),
        rejected_rank=list(rank),
        best_rank=list(best_rank),
        rejected_dom_p0=payload.get("paper_poster_dom_p0_count"),
        best_dom_p0=restored.get("paper_poster_dom_p0_count"),
        density_preservation_regression=density_regression,
    )
    return restored


def _dogfood_authored_poster_rank(payload: dict[str, Any]) -> tuple[float, ...]:
    feedback = payload.get("design_feedback") if isinstance(payload.get("design_feedback"), dict) else {}
    counts = feedback.get("counts") if isinstance(feedback.get("counts"), dict) else {}
    findings = payload.get("paper_poster_dom_findings") if isinstance(payload.get("paper_poster_dom_findings"), list) else []
    id_counts: dict[str, int] = {}
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        finding_id = str(finding.get("id") or "")
        if finding_id:
            id_counts[finding_id] = id_counts.get(finding_id, 0) + 1
    visible_words = _safe_float(payload.get("visible_text_word_count"), 0.0)
    info_units = _safe_float(payload.get("paper_info_unit_count"), 0.0)
    visual_area = _safe_float(payload.get("visual_area_ratio"), 0.0)
    density_regression = _dogfood_dense_reference_regression_score(
        payload,
        visible_words=visible_words,
        info_units=info_units,
        visual_area=visual_area,
    )
    return (
        _safe_float(counts.get("blocker"), 0.0),
        _safe_float(payload.get("authored_generation_contract_p0_count"), 0.0),
        density_regression,
        _safe_float(payload.get("paper_poster_dom_p0_count"), 0.0),
        _safe_float(payload.get("poster_contract_p0_count"), 0.0),
        _safe_float(payload.get("paper_information_p0_count"), 0.0),
        _safe_float(payload.get("paper_density_p0_count"), 0.0),
        float(id_counts.get("paper-poster-block-out-of-bounds", 0)),
        float(id_counts.get("paper-poster-overflow", 0)),
        float(id_counts.get("paper-poster-text-overlap", 0)),
        float(id_counts.get("paper-poster-panel-underfilled", 0)),
        float(id_counts.get("paper-poster-footer-overlap", 0)),
        _safe_float(counts.get("high"), 0.0),
        -visible_words,
        -info_units,
        -visual_area,
    )


def _dogfood_dense_reference_regression_score(
    payload: dict[str, Any],
    *,
    visible_words: float,
    info_units: float,
    visual_area: float,
) -> float:
    """Penalize sparse dogfood candidates before local DOM polish.

    Dense-but-locally-imperfect posters should remain repair candidates; sparse
    posters with fewer local overlap findings should not replace them as the
    final dogfood artifact.
    """
    contract = payload.get("poster_plan_contract") if isinstance(payload.get("poster_plan_contract"), dict) else {}
    profile = str(contract.get("reference_profile") or payload.get("reference_profile") or "")
    if profile not in {"research_synthesis_dense", "visual_evidence_wall"}:
        return 0.0
    metrics = payload.get("poster_contract_metrics") if isinstance(payload.get("poster_contract_metrics"), dict) else {}
    native_targets = contract.get("native_information_targets") if isinstance(contract.get("native_information_targets"), dict) else {}
    layout_contract = contract.get("layout_slot_contract") if isinstance(contract.get("layout_slot_contract"), dict) else {}
    density_targets = contract.get("density_targets") if isinstance(contract.get("density_targets"), dict) else {}
    min_words = max(
        1.0,
        _safe_float(native_targets.get("min_visible_words"), 0.0),
        _safe_float(layout_contract.get("min_visible_words"), 0.0),
        1100.0 if profile == "visual_evidence_wall" else 1300.0,
    )
    min_units = max(
        1.0,
        _safe_float(native_targets.get("min_native_information_units"), 0.0),
        _safe_float(layout_contract.get("min_native_information_units"), 0.0),
        22.0 if profile == "visual_evidence_wall" else 28.0,
    )
    min_visual_area = max(
        0.01,
        _safe_float(density_targets.get("min_visual_area_ratio"), 0.0),
        0.10 if profile == "research_synthesis_dense" else 0.14,
    )
    words = max(visible_words, _safe_float(metrics.get("dense_visible_word_count"), 0.0))
    units = max(info_units, _safe_float(metrics.get("dense_native_information_unit_count"), 0.0))
    word_ratio = words / min_words
    unit_ratio = units / min_units
    visual_ratio = visual_area / min_visual_area
    hard_fail = 0.0
    if word_ratio < 0.82:
        hard_fail += 10.0
    if unit_ratio < 0.78:
        hard_fail += 10.0
    if visual_ratio < 0.65:
        hard_fail += 4.0
    soft_deficit = max(0.0, 1.0 - word_ratio) + max(0.0, 1.0 - unit_ratio) + max(0.0, 1.0 - visual_ratio) * 0.3
    return round(hard_fail + soft_deficit, 4)


def _dogfood_density_preservation_regression(
    candidate: dict[str, Any],
    best: dict[str, Any],
) -> dict[str, Any] | None:
    """Reject later dogfood candidates that get through gates by deleting substance."""
    metrics = _dogfood_density_preservation_metrics(candidate, best)
    regressed = [
        item for item in metrics
        if item["best"] > item["floor"] and item["candidate"] < item["floor"]
    ]
    if not regressed:
        return None
    return {
        "reason": "density_preservation_regression",
        "metrics": regressed,
        "repair": (
            "Repair layout gates by moving/resizing lanes, reducing font size or line-height, "
            "splitting dense content into columns/tables, or rebalancing panels. Do not make "
            "a later candidate acceptable by deleting source-backed content from the best draft."
        ),
    }


def _dogfood_density_preservation_metrics(
    candidate: dict[str, Any],
    best: dict[str, Any],
) -> list[dict[str, float | str]]:
    specs = [
        ("visible_text_word_count", 0.86, 80.0),
        ("paper_info_unit_count", 0.82, 3.0),
        ("placed_table_count", 0.75, 0.5),
        ("visual_area_ratio", 0.72, 0.035),
    ]
    out: list[dict[str, float | str]] = []
    for key, ratio, absolute_drop in specs:
        best_value = _safe_float(best.get(key), 0.0)
        candidate_value = _safe_float(candidate.get(key), 0.0)
        if best_value <= 0:
            continue
        floor = min(best_value * ratio, best_value - absolute_drop)
        floor = max(0.0, floor)
        out.append({
            "metric": key,
            "candidate": round(candidate_value, 4),
            "best": round(best_value, 4),
            "floor": round(floor, 4),
        })
    return out


def _composition_state_record(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, CompositionArtifacts):
        return None
    return {
        "html_path": value.html_path,
        "html_artifact_path": value.html_artifact_path,
        "psd_path": value.psd_path,
        "svg_path": value.svg_path,
        "pdf_path": value.pdf_path,
        "pptx_path": value.pptx_path,
        "preview_path": value.preview_path,
        "layer_manifest": copy.deepcopy(value.layer_manifest),
    }


def _copy_state_value(value: Any) -> Any:
    if hasattr(value, "model_copy"):
        return value.model_copy(deep=True)
    return copy.deepcopy(value)


def _remove_final_links(ctx: ToolContext, files: list[str]) -> None:
    """Remove stale final artifacts that do not belong to the latest export mode."""
    final_dir = ctx.run_dir / "final"
    if not final_dir.exists():
        return
    for fname in files:
        path = final_dir / fname
        if path.is_symlink() or path.is_file():
            path.unlink()


def _attach_design_feedback(
    payload: dict[str, Any],
    *,
    iter_dir: Path,
    iter_num: int,
    ctx: ToolContext,
) -> dict[str, Any]:
    """Attach normalized environment feedback to a composite payload."""
    visual_reference_payload = build_visual_reference_contract(payload, ctx=ctx)
    payload.update(visual_reference_payload)
    atomic_write_json(iter_dir / "visual_reference_contract.json", {
        "artifact_type": payload.get("artifact_type"),
        "iteration": iter_num,
        **visual_reference_payload,
    })
    payload["visual_reference_contract_relative_path"] = (
        f"composites/iter_{iter_num:02d}/visual_reference_contract.json"
    )
    feedback = build_design_feedback(
        payload,
        artifact_type=str(payload.get("artifact_type") or ctx.state.get("artifact_type") or "unknown"),
        iteration=iter_num,
    )
    feedback_payload = feedback.model_dump(mode="json")
    atomic_write_json(iter_dir / "design_feedback.json", feedback_payload)
    payload["design_feedback"] = feedback_payload
    payload["design_feedback_relative_path"] = (
        f"composites/iter_{iter_num:02d}/design_feedback.json"
    )
    ctx.state["last_design_feedback"] = feedback
    return payload


def _attach_html_artifact_contract(
    payload: dict[str, Any],
    *,
    spec: Any,
    iter_dir: Path,
    iter_num: int,
) -> dict[str, Any]:
    """Persist canonical scene graph and attach unified contract findings."""
    artifact = getattr(spec, "html_artifact", None)
    if artifact is not None:
        try:
            atomic_write_json(iter_dir / "html_artifact.json", {
                "artifact_type": payload.get("artifact_type"),
                "iteration": iter_num,
                "html_artifact": artifact.model_dump(mode="json")
                if hasattr(artifact, "model_dump") else artifact,
            })
            payload["html_artifact_relative_path"] = (
                f"composites/iter_{iter_num:02d}/html_artifact.json"
            )
        except OSError:
            pass
    contract_payload = audit_html_artifact_contract(spec, payload)
    try:
        atomic_write_json(iter_dir / "html_artifact_contract.json", {
            "artifact_type": payload.get("artifact_type"),
            "iteration": iter_num,
            **contract_payload,
        })
        payload["html_artifact_contract_relative_path"] = (
            f"composites/iter_{iter_num:02d}/html_artifact_contract.json"
        )
    except OSError:
        pass
    payload.update(contract_payload)
    return payload


# Warn when the planner's bbox aspect is this many times off from the
# layer's source-content aspect. Above the threshold we letterbox for
# images / re-render for tables so text/figures stay legible; below,
# we keep the old "stretch to fit" behavior (imperceptible squeeze).
_ASPECT_MISMATCH_WARN_RATIO = 2.0

# Descender-clearance multiplier for text layers. A rasterized Latin glyph
# including descenders occupies ~1.10–1.20 × font_size_px vertically. If a
# planner declares `bbox.h = font_size_px` the descender spills ~20 % below
# the bbox bottom, crashing into any layer directly beneath. Effective
# vertical footprint = max(bbox.h, font_size_px × this multiplier).
_TEXT_DESCENDER_MULTIPLIER = 1.20
_POSTER_TEXT_GAP_PX = 18
_POSTER_TEXT_REPAIR_PASSES = 4
_POSTER_GROUNDING_REPAIR_PASSES = 3
_POSTER_GROUNDING_MIN_FONT_SCALE = 0.82
_POSTER_GROUNDING_FONT_STEP = 0.92
_POSTER_GROUNDING_EDGE_GAP_PX = 12
_POSTER_BROWSER_TIMEOUT_MS = 12_000


def _lint_composite_html(
    html_path: Path,
    *,
    artifact_type: str,
    iter_dir: Path,
) -> dict[str, Any]:
    """Run deterministic quality lint against generated HTML.

    Lint findings are advisory payload signals; they never fail composite.
    The designer prompt treats P0 findings as a reason for one revision pass.
    """
    try:
        html = html_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        findings = [{
            "severity": "P1",
            "id": "quality-lint-read-error",
            "message": f"Could not read HTML for quality lint: {e}",
            "fix": "Inspect the composite HTML path and re-run composite.",
            "snippet": str(html_path),
        }]
    else:
        findings = lint_html_quality(html)

    p0_count = count_p0_findings(findings)
    payload = {
        "quality_lint_findings": findings,
        "quality_lint_p0_count": p0_count,
    }
    try:
        atomic_write_json(iter_dir / "quality_lint.json", {
            "artifact_type": artifact_type,
            **payload,
        })
    except OSError:
        pass
    log(
        "quality.lint.done",
        artifact_type=artifact_type,
        html=str(html_path),
        findings=len(findings),
        p0=p0_count,
    )
    return payload


def _effective_text_extent(
    layer: Any,
    *,
    role: str | None = None,
    ctx: ToolContext | None = None,
) -> tuple[int, int, int, int] | None:
    """Return (x, y, w, h_effective) — the glyph-inclusive vertical footprint
    of a `kind: "text"` layer, or None if bbox/font_size missing.

    Accepts either a dict (poster path: `rendered_layers` records) or a
    Pydantic ``LayerNode`` (deck path: spec-tree children). Persisted legacy
    deck specs can omit a bbox and retain a deprecated ``template_slot``;
    their compatibility geometry is resolved by ``_legacy_slot_bbox``.

    `bbox.h` is the planner's intent; real rasterized height floors at
    `font_size_px × _TEXT_DESCENDER_MULTIPLIER` so descender collisions
    between stacked text layers surface as real overlaps."""
    kind = layer.get("kind") if isinstance(layer, dict) else getattr(layer, "kind", None)
    if kind != "text":
        return None

    if isinstance(layer, dict):
        bbox = layer.get("bbox")
        slot = layer.get("template_slot")
        fs = layer.get("font_size_px") or 0
    else:
        bbox = getattr(layer, "bbox", None)
        slot = getattr(layer, "template_slot", None)
        fs = getattr(layer, "font_size_px", None) or 0

    bx = by = bw = bh = None
    if bbox is not None:
        try:
            if isinstance(bbox, dict):
                bx = int(bbox.get("x", 0))
                by = int(bbox.get("y", 0))
                bw = int(bbox.get("w", 0))
                bh = int(bbox.get("h", 0))
            else:
                bx = int(getattr(bbox, "x", 0) or 0)
                by = int(getattr(bbox, "y", 0) or 0)
                bw = int(getattr(bbox, "w", 0) or 0)
                bh = int(getattr(bbox, "h", 0) or 0)
        except (TypeError, ValueError):
            bx = by = bw = bh = None

    if bw is None or bw <= 0 or bh is None or bh <= 0:
        slot_bbox = _legacy_slot_bbox(role, slot)
        if slot_bbox is None:
            return None
        bx, by, bw, bh = slot_bbox

    try:
        fs = int(fs)
    except (TypeError, ValueError):
        fs = 0
    descender_h = int(fs * _TEXT_DESCENDER_MULTIPLIER) if fs > 0 else 0
    measured_h = 0
    if ctx is not None and isinstance(layer, dict) and fs > 0 and bw > 0:
        try:
            measured_h = measure_text_height(
                layer.get("text") or "",
                layer.get("font_family"),
                fs,
                bw,
                ctx,
                line_height=layer.get("line_height"),
                letter_spacing=layer.get("letter_spacing"),
                text_transform=layer.get("text_transform"),
                font_weight=layer.get("font_weight"),
            )
        except Exception:
            measured_h = 0
    return bx, by, bw, max(bh, descender_h, measured_h)


def _rects_overlap(a: tuple[int, int, int, int],
                   b: tuple[int, int, int, int]) -> tuple[int, int] | None:
    """Return (x_overlap_px, y_overlap_px) if rects a and b intersect, else None."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x_ov = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    y_ov = max(0, min(ay + ah, by + bh) - max(ay, by))
    if x_ov > 0 and y_ov > 0:
        return x_ov, y_ov
    return None


def _placed_ingest_display_map(
    layers: list[dict[str, Any]],
) -> dict[str, tuple[str, int]]:
    """Map placed ingest figure / table layer_ids to (`"fig"` | `"table"`, N).

    Display numbers follow the order each `ingest_fig_NN` / `ingest_table_NN`
    appears in the sorted layer list (same order the poster reads
    top-to-bottom once z_index is respected). The 01/02/… suffix from the
    ingest step is intentionally NOT reused as the display number — the
    paper's Fig. 7 might be poster Fig. 2 if that's the order the planner
    chose to lay them out.
    """
    out: dict[str, tuple[str, int]] = {}
    fig_n = 0
    tbl_n = 0
    for L in layers:
        lid = L.get("layer_id") or ""
        kind = L.get("kind")
        if kind == "image" and lid.startswith("ingest_fig_"):
            fig_n += 1
            out[lid] = ("fig", fig_n)
        elif kind == "table" and lid.startswith("ingest_table_"):
            tbl_n += 1
            out[lid] = ("table", tbl_n)
    return out


def _detect_missing_figure_xrefs(
    layers: list[dict[str, Any]],
    spec: Any,
) -> list[str]:
    """Return layer_ids of placed `ingest_fig_NN` / `ingest_table_NN` that no
    text layer cross-references via `(Fig. N)` / `(Table N)` literal.

    Skips entirely for non-paper posters (no placed ingest layers). A layer
    counts as cross-referenced when ANY text layer's `.text` contains the
    literal `Fig. N` / `Figure N` / `Fig. N-M` / `Table N` pattern
    (case-insensitive, period-optional) for its display number, or when the
    visual already has a nearby editable caption/reference. This keeps the
    signal focused on genuinely orphaned paper visuals instead of penalizing
    dense poster panels that label figure strips with semantic captions.
    """
    display_map = _placed_ingest_display_map(layers)
    if not display_map:
        return []

    import re

    haystack_parts: list[str] = []
    text_layers: list[dict[str, Any]] = []
    layer_by_id = {
        str(L.get("layer_id") or ""): L for L in layers
        if L.get("layer_id")
    }
    for L in layers:
        if L.get("kind") != "text":
            continue
        text_layers.append(L)
        t = L.get("text") or ""
        if t:
            haystack_parts.append(t)
    # Pull from the authoritative DesignSpec too — covers cases where
    # render_text_layer hasn't yet populated `text` onto rendered_layers
    # but the planner's layer_graph has it.
    for node in list(getattr(spec, "layer_graph", None) or []):
        if getattr(node, "kind", None) != "text":
            continue
        t = getattr(node, "text", None) or ""
        if t:
            haystack_parts.append(t)
    haystack = "\n".join(haystack_parts)

    misses: list[str] = []
    for layer_id, (kind, n) in display_map.items():
        layer = layer_by_id.get(layer_id) or {}
        if (
            not _haystack_has_display_xref(haystack, kind, n, re)
            and not _visual_has_nearby_editable_xref(layer, text_layers, kind)
        ):
            misses.append(layer_id)
    return misses


def _haystack_has_display_xref(haystack: str, kind: str, n: int, re_mod: Any) -> bool:
    if kind == "fig":
        direct = rf"\b(?:fig(?:ure)?\.?)\s*{n}\b"
        range_pattern = r"\bfig(?:ure)?s?\.?\s*(\d+)\s*[-–—]\s*(\d+)\b"
    else:
        direct = rf"\btables?\.?\s*{n}\b"
        range_pattern = r"\btables?\.?\s*(\d+)\s*[-–—]\s*(\d+)\b"
    if re_mod.search(direct, haystack, re_mod.IGNORECASE):
        return True
    for match in re_mod.finditer(range_pattern, haystack, re_mod.IGNORECASE):
        try:
            start = int(match.group(1))
            end = int(match.group(2))
        except (TypeError, ValueError):
            continue
        lo, hi = sorted((start, end))
        if lo <= n <= hi:
            return True
    return False


def _visual_has_nearby_editable_xref(
    visual_layer: dict[str, Any],
    text_layers: list[dict[str, Any]],
    kind: str,
) -> bool:
    caption = str(visual_layer.get("caption") or visual_layer.get("title") or "").strip()
    if caption and _caption_text_matches_visual_kind(caption, kind):
        return True
    visual_bbox = visual_layer.get("bbox") or {}
    if not isinstance(visual_bbox, dict):
        return False
    for text_layer in text_layers:
        if not _caption_text_matches_visual_kind(
            " ".join([
                str(text_layer.get("layer_id") or ""),
                str(text_layer.get("name") or ""),
                str(text_layer.get("role") or ""),
                str(text_layer.get("text") or ""),
            ]),
            kind,
        ):
            continue
        if _is_near_visual_caption(visual_bbox, text_layer.get("bbox") or {}):
            return True
    return False


def _caption_text_matches_visual_kind(text: str, kind: str) -> bool:
    haystack = text.lower()
    if kind == "table":
        return "table" in haystack or "caption" in haystack or "cap_" in haystack
    return (
        "fig" in haystack
        or "figure" in haystack
        or "caption" in haystack
        or "cap_" in haystack
    )


def _is_near_visual_caption(visual_bbox: dict[str, Any], text_bbox: dict[str, Any]) -> bool:
    if not isinstance(text_bbox, dict):
        return False
    vx = _safe_int(visual_bbox.get("x"), 0)
    vy = _safe_int(visual_bbox.get("y"), 0)
    vw = _safe_int(visual_bbox.get("w"), 0)
    vh = _safe_int(visual_bbox.get("h"), 0)
    tx = _safe_int(text_bbox.get("x"), 0)
    ty = _safe_int(text_bbox.get("y"), 0)
    tw = _safe_int(text_bbox.get("w"), 0)
    th = _safe_int(text_bbox.get("h"), 0)
    if vw <= 0 or vh <= 0 or tw <= 0 or th <= 0:
        return False
    vx2, vy2 = vx + vw, vy + vh
    tx2, ty2 = tx + tw, ty + th
    horizontal_overlap = max(0, min(vx2, tx2) - max(vx, tx))
    vertical_overlap = max(0, min(vy2, ty2) - max(vy, ty))
    min_width = max(1, min(vw, tw))
    horizontally_related = horizontal_overlap / min_width >= 0.35
    below_or_above = (
        0 <= ty - vy2 <= max(90, int(vh * 0.35))
        or 0 <= vy - ty2 <= max(90, int(vh * 0.35))
    )
    side_by_side = (
        vertical_overlap / max(1, min(vh, th)) >= 0.45
        and (
            0 <= tx - vx2 <= max(80, int(vw * 0.08))
            or 0 <= vx - tx2 <= max(80, int(vw * 0.08))
        )
    )
    return (horizontally_related and below_or_above) or side_by_side


def _detect_text_overlaps(
    layers: list[dict[str, Any]],
    *,
    ctx: ToolContext | None = None,
) -> list[dict[str, Any]]:
    """Detect glyph-inclusive bbox collisions between poster text layers.

    Emits one `composite.text_overlap_warning` log event per colliding pair
    and returns the list for inclusion in the `obs_ok` summary — which the
    planner reads on the next turn, so the collision feeds back without
    waiting for a full critique pass.
    """
    text_layers = [
        (L, _effective_text_extent(L, ctx=ctx))
        for L in layers
        if L.get("kind") == "text"
    ]
    text_layers = [(L, ext) for L, ext in text_layers if ext is not None]
    warnings: list[dict[str, Any]] = []
    for i in range(len(text_layers)):
        la, ea = text_layers[i]
        for j in range(i + 1, len(text_layers)):
            lb, eb = text_layers[j]
            ov = _rects_overlap(ea, eb)
            if ov is None:
                continue
            _x_ov, y_ov = ov
            entry = {
                "layer_a": la.get("layer_id"),
                "layer_b": lb.get("layer_id"),
                "y_overlap_px": int(y_ov),
                "font_size_a": int(la.get("font_size_px") or 0),
                "font_size_b": int(lb.get("font_size_px") or 0),
            }
            warnings.append(entry)
            log("composite.text_overlap_warning", **entry)
    return warnings


def _repair_poster_text_layout(
    layers: list[dict[str, Any]],
    cw: int,
    ch: int,
    ctx: ToolContext,
) -> list[dict[str, Any]]:
    """Best-effort poster text collision repair before export.

    The planner supplies absolute bboxes, but dense paper posters routinely
    under-budget real wrapped text height. Repairing here keeps PSD/SVG/HTML
    outputs aligned: we update the bbox, then re-render the text PNG so the
    raster fallback matches the browser frame.
    """
    text_layers = [
        L for L in layers
        if L.get("kind") == "text" and L.get("bbox") and L.get("text")
    ]
    if len(text_layers) < 2:
        return []

    repairs: list[dict[str, Any]] = []
    changed: set[str] = set()

    for L in text_layers:
        bbox = L.get("bbox") or {}
        old = dict(bbox)
        fs = _safe_int(L.get("font_size_px"), 0)
        bw = _safe_int(bbox.get("w"), 0)
        if fs <= 0 or bw <= 0:
            continue
        try:
            measured_h = measure_text_height(
                L.get("text") or "",
                L.get("font_family"),
                fs,
                bw,
                ctx,
                line_height=L.get("line_height"),
                letter_spacing=L.get("letter_spacing"),
                text_transform=L.get("text_transform"),
                font_weight=L.get("font_weight"),
            )
        except Exception:
            measured_h = 0
        min_h = max(
            _safe_int(bbox.get("h"), 0),
            int(math.ceil(fs * _TEXT_DESCENDER_MULTIPLIER)),
            int(math.ceil(measured_h * 1.08)),
        )
        max_h = max(1, ch - _safe_int(bbox.get("y"), 0) - 8)
        min_h = min(min_h, max_h)
        if min_h > _safe_int(bbox.get("h"), 0):
            bbox["h"] = min_h
            changed.add(str(L.get("layer_id")))
            repairs.append({
                "layer_id": L.get("layer_id"),
                "reason": "increase_text_bbox_height",
                "old_bbox": old,
                "new_bbox": dict(bbox),
            })

    for _pass in range(_POSTER_TEXT_REPAIR_PASSES):
        moved = False
        placed: list[dict[str, Any]] = []
        for L in sorted(
            text_layers,
            key=lambda x: (
                _safe_int((x.get("bbox") or {}).get("y"), 0),
                _safe_int((x.get("bbox") or {}).get("x"), 0),
                _safe_int(x.get("z_index"), 0),
            ),
        ):
            bbox = L.get("bbox") or {}
            old = dict(bbox)
            target_y = _safe_int(bbox.get("y"), 0)
            for prev in placed:
                prev_ext = _effective_text_extent(prev, ctx=ctx)
                cur_ext = _effective_text_extent(L, ctx=ctx)
                if prev_ext is None or cur_ext is None:
                    continue
                x_ov = _horizontal_overlap(prev_ext, cur_ext)
                min_x_ov = max(32, int(min(prev_ext[2], cur_ext[2]) * 0.16))
                if x_ov < min_x_ov:
                    continue
                required_y = prev_ext[1] + prev_ext[3] + _POSTER_TEXT_GAP_PX
                if cur_ext[1] < required_y:
                    target_y = max(target_y, required_y)
            max_y = max(0, ch - _safe_int(bbox.get("h"), 1) - 8)
            target_y = min(target_y, max_y)
            if target_y != _safe_int(bbox.get("y"), 0):
                bbox["y"] = int(target_y)
                changed.add(str(L.get("layer_id")))
                repairs.append({
                    "layer_id": L.get("layer_id"),
                    "reason": "push_down_to_clear_text_overlap",
                    "old_bbox": old,
                    "new_bbox": dict(bbox),
                    "pass": _pass + 1,
                })
                moved = True
            placed.append(L)
        if not moved:
            break

    if changed:
        for L in text_layers:
            if str(L.get("layer_id")) not in changed:
                continue
            try:
                _rerender_poster_text_layer(L, cw, ch, ctx)
            except Exception as e:
                log(
                    "composite.poster_text_repair_rerender_failed",
                    layer_id=L.get("layer_id"),
                    error=str(e),
                )
        for repair in repairs:
            log("composite.poster_text_layout_repair", **repair)
    return repairs


def _rerender_poster_text_layer(
    layer: dict[str, Any],
    cw: int,
    ch: int,
    ctx: ToolContext,
) -> None:
    layer_id = str(layer.get("layer_id") or "text")
    out_path = ctx.layers_dir / f"text_{layer_id}.layout.png"
    resolved_family, _fallback = render_text_png(
        text=layer.get("text") or "",
        font_family=layer.get("font_family"),
        font_size=_safe_int(layer.get("font_size_px"), 24),
        fill=layer.get("fill") or "#000000",
        bbox=layer.get("bbox") or {"x": 0, "y": 0, "w": cw, "h": ch},
        align=layer.get("align") or "left",
        effects=layer.get("effects") or {},
        font_weight=layer.get("font_weight"),
        font_style=layer.get("font_style"),
        line_height=layer.get("line_height"),
        letter_spacing=layer.get("letter_spacing"),
        text_transform=layer.get("text_transform"),
        canvas_w=cw,
        canvas_h=ch,
        out_path=out_path,
        ctx=ctx,
    )
    layer["src_path"] = str(out_path)
    layer["font_family"] = resolved_family
    layer["sha256"] = sha256_file(out_path)
    layer["layout_repaired"] = True


def _horizontal_overlap(
    a: tuple[int, int, int, int],
    b: tuple[int, int, int, int],
) -> int:
    ax, _ay, aw, _ah = a
    bx, _by, bw, _bh = b
    return max(0, min(ax + aw, bx + bw) - max(ax, bx))


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _poster_layers_from_html_artifact(
    spec: Any,
    ctx: ToolContext,
    *,
    cw: int,
    ch: int,
) -> list[dict[str, Any]] | None:
    """Derive renderable poster layers from canonical html_artifact blocks.

    The HTML-first planner now emits storyboard groups with child text/images.
    Legacy poster composite state only contains pre-rendered image/text layers,
    so a pure html_artifact poster would otherwise drop editable text. This
    adapter hydrates source-backed images and renders text blocks on demand.
    """
    artifact = getattr(spec, "html_artifact", None)
    if artifact is None:
        return None
    data = artifact.model_dump(mode="json") if hasattr(artifact, "model_dump") else dict(artifact or {})
    if str(data.get("target") or spec.artifact_type.value) != "poster":
        return None
    theme = data.get("theme") if isinstance(data.get("theme"), dict) else {}
    if has_legacy_source_marker(theme):
        return None
    frames = [
        frame for frame in (data.get("frames") or [])
        if isinstance(frame, dict) and str(frame.get("kind") or "") == "canvas"
    ]
    if not frames:
        return None

    source_layers = ctx.state.get("rendered_layers") or {}
    layers: list[dict[str, Any]] = []
    frame = frames[0]
    for block in frame.get("blocks") or []:
        if isinstance(block, dict):
            _append_poster_layers_from_html_block(
                block,
                layers,
                source_layers=source_layers,
                ctx=ctx,
                cw=cw,
                ch=ch,
                parent_bbox=None,
                z_base=len(layers),
            )
    if not any(L.get("kind") in {"text", "image", "table"} for L in layers):
        return None
    return sorted(layers, key=lambda L: int(L.get("z_index", 0)))


def _append_poster_layers_from_html_block(
    block: dict[str, Any],
    layers: list[dict[str, Any]],
    *,
    source_layers: dict[str, Any],
    ctx: ToolContext,
    cw: int,
    ch: int,
    parent_bbox: dict[str, int] | None,
    z_base: int,
) -> None:
    kind = str(block.get("kind") or "text")
    bbox = _html_block_abs_bbox(block.get("bbox"), parent_bbox)
    style = dict(block.get("style") or {})
    block_id = str(block.get("layer_id") or block.get("block_id") or f"html_block_{len(layers) + 1}")
    z_index = _safe_int(style.get("z_index") or block.get("z_index"), z_base + len(layers) + 1)

    if kind == "group":
        if bbox is not None and _html_style_has_visible_box(style):
            layers.append(_shape_layer_from_html_block(block, bbox, z_index=z_index))
        for child in block.get("children") or []:
            if isinstance(child, dict):
                _append_poster_layers_from_html_block(
                    child,
                    layers,
                    source_layers=source_layers,
                    ctx=ctx,
                    cw=cw,
                    ch=ch,
                    parent_bbox=bbox or parent_bbox,
                    z_base=z_index,
                )
        return

    if bbox is None:
        return

    if kind == "shape":
        layers.append(_shape_layer_from_html_block(block, bbox, z_index=z_index))
        return

    if kind in {"image", "chart", "embed"}:
        hydrated = _hydrate_html_visual_block(block, source_layers)
        src_path = hydrated.get("src_path")
        if not src_path:
            return
        layers.append({
            "layer_id": block_id,
            "name": str(block.get("title") or block.get("role") or block_id),
            "kind": "image",
            "z_index": z_index,
            "bbox": bbox,
            "src_path": src_path,
            "caption": block.get("caption") or hydrated.get("caption"),
            "source": block.get("source") or hydrated.get("source") or "html_artifact",
            "source_id": block.get("source_id") or hydrated.get("source_id"),
            "role": block.get("role"),
            "slot_id": block.get("slot_id"),
            "panel_role": block.get("panel_role"),
        })
        return

    if kind == "table":
        hydrated = _hydrate_html_visual_block(block, source_layers)
        block_has_native_rows = bool(block.get("rows") or block.get("headers"))
        src_path = (
            block.get("src_path")
            or hydrated.get("src_path")
            or str(ctx.layers_dir / f"table_{block_id}.png")
        )
        layers.append({
            "layer_id": block_id,
            "name": str(block.get("title") or block.get("role") or block_id),
            "kind": "table",
            "z_index": z_index,
            "bbox": bbox,
            "rows": block.get("rows") or hydrated.get("rows") or [],
            "headers": block.get("headers") or hydrated.get("headers") or [],
            "caption": block.get("caption") or hydrated.get("caption") or _html_block_text(block),
            "col_highlight_rule": block.get("col_highlight_rule") or hydrated.get("col_highlight_rule") or [],
            "src_path": str(src_path),
            "source": block.get("source") or hydrated.get("source") or "html_artifact",
            "source_id": hydrated.get("source_id") or block.get("source_id") or block_id,
            "table_visual_source": block.get("table_visual_source") or hydrated.get("table_visual_source"),
            "native_table_reconstruction": block_has_native_rows,
            "role": block.get("role"),
            "slot_id": block.get("slot_id"),
            "panel_role": block.get("panel_role"),
        })
        return

    if kind in {"text", "metric", "quote", "caption"}:
        text = _html_block_text(block)
        if not text:
            return
        layer = {
            "layer_id": block_id,
            "name": str(block.get("title") or block.get("role") or block_id),
            "kind": "text",
            "z_index": z_index,
            "bbox": bbox,
            "text": text,
            "font_family": _html_style_value(style, "font_family", "fontFamily") or ctx.settings.default_text_font,
            "font_size_px": _safe_int(_html_style_value(style, "font_size_px", "fontSize"), 34),
            "font_weight": _html_style_value(style, "font_weight", "fontWeight"),
            "font_style": _html_style_value(style, "font_style", "fontStyle"),
            "line_height": _html_style_value(style, "line_height", "lineHeight") or 1.12,
            "letter_spacing": _html_style_value(style, "letter_spacing", "letterSpacing") or 0,
            "text_transform": _html_style_value(style, "text_transform", "textTransform"),
            "align": _html_style_value(style, "align", "textAlign") or block.get("align") or "left",
            "fill": _html_style_value(style, "fill", "color") or "#111827",
            "effects": style.get("effects") or {},
            "source": block.get("source") or "html_artifact",
            "source_id": block.get("source_id"),
            "role": block.get("role"),
            "slot_id": block.get("slot_id"),
            "panel_role": block.get("panel_role"),
        }
        _rerender_poster_text_layer(layer, cw, ch, ctx)
        layers.append(layer)


def _html_style_has_visible_box(style: dict[str, Any]) -> bool:
    fill = str(style.get("fill") or style.get("background") or style.get("backgroundColor") or "").strip().lower()
    stroke = str(style.get("stroke") or style.get("borderColor") or "").strip().lower()
    return bool(fill and fill != "transparent") or bool(stroke and stroke != "transparent")


def _shape_layer_from_html_block(block: dict[str, Any], bbox: dict[str, int], *, z_index: int) -> dict[str, Any]:
    style = dict(block.get("style") or {})
    block_id = str(block.get("layer_id") or block.get("block_id") or "shape")
    return {
        "layer_id": block_id,
        "name": str(block.get("title") or block.get("role") or block_id),
        "kind": "shape",
        "z_index": z_index,
        "bbox": bbox,
        "fill": _html_style_value(style, "fill", "background", "backgroundColor") or "transparent",
        "stroke": _html_style_value(style, "stroke", "borderColor"),
        "stroke_width": _safe_int(_html_style_value(style, "stroke_width", "strokeWidth", "borderWidth"), 0),
        "radius": _safe_int(_html_style_value(style, "radius", "borderRadius"), 0),
        "role": block.get("role"),
        "slot_id": block.get("slot_id"),
        "panel_role": block.get("panel_role"),
    }


def _hydrate_html_visual_block(block: dict[str, Any], source_layers: dict[str, Any]) -> dict[str, Any]:
    src_path = str(block.get("src_path") or "").strip()
    if src_path:
        out = {"src_path": src_path}
        if block.get("table_visual_source"):
            out["table_visual_source"] = block.get("table_visual_source")
        return out
    candidates = [
        block.get("source_id"),
        block.get("asset_id"),
        block.get("layer_id"),
        block.get("block_id"),
    ]
    for candidate in candidates:
        key = str(candidate or "").strip()
        if not key:
            continue
        source = source_layers.get(key)
        if isinstance(source, dict) and source.get("src_path"):
            return source
    return {}


def _is_source_table_crop_layer(layer: dict[str, Any]) -> bool:
    if str(layer.get("kind") or "").lower() != "table":
        return False
    if not str(layer.get("src_path") or "").strip():
        return False
    if str(layer.get("table_visual_source") or "").lower() != "original_pdf_crop":
        return False
    return not bool(layer.get("native_table_reconstruction"))


def _html_block_abs_bbox(value: Any, parent_bbox: dict[str, int] | None) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    try:
        bbox = {key: int(value[key]) for key in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        return None
    if bbox["w"] <= 0 or bbox["h"] <= 0:
        return None
    if parent_bbox is None:
        return bbox
    looks_absolute_inside_parent = (
        bbox["x"] >= parent_bbox["x"] - 2
        and bbox["y"] >= parent_bbox["y"] - 2
        and bbox["x"] + bbox["w"] <= parent_bbox["x"] + parent_bbox["w"] + 2
        and bbox["y"] + bbox["h"] <= parent_bbox["y"] + parent_bbox["h"] + 2
    )
    if looks_absolute_inside_parent:
        return bbox
    looks_relative = (
        bbox["x"] >= 0
        and bbox["y"] >= 0
        and bbox["x"] + bbox["w"] <= parent_bbox["w"] + 2
        and bbox["y"] + bbox["h"] <= parent_bbox["h"] + 2
    )
    if looks_relative:
        return {
            **bbox,
            "x": parent_bbox["x"] + bbox["x"],
            "y": parent_bbox["y"] + bbox["y"],
        }
    return bbox


def _html_block_text(block: dict[str, Any]) -> str:
    values = [
        str(block.get("title") or "").strip(),
        str(block.get("text") or "").strip(),
        *(str(item or "").strip() for item in block.get("items") or []),
    ]
    return "\n".join(value for value in values if value)


def _html_style_value(style: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in style and style[key] is not None:
            return style[key]
    return None


def _text_font_weight(value: Any, family: str | None = None) -> int:
    if value is None:
        return 700 if family and "bold" in family.lower() else 400
    weight = max(100, min(900, _safe_int(value, 400)))
    # svgwrite's validator still follows the older SVG font-weight token set
    # and rejects variable-font numeric weights such as 460 or 720. Quantize
    # SVG attributes to the nearest 100; HTML/PNG renderers keep the exact
    # variable weight elsewhere.
    return max(100, min(900, int(round(weight / 100.0) * 100)))


def _text_font_style(value: Any) -> str:
    return "italic" if value == "italic" else "normal"


def _text_transform_value(value: Any) -> str:
    return "uppercase" if value == "uppercase" else "none"


def _text_manifest_fields(node: Any) -> dict[str, Any]:
    if getattr(node, "kind", None) != "text":
        return {}
    return {
        "font_family": getattr(node, "font_family", None),
        "font_size_px": getattr(node, "font_size_px", None),
        "font_weight": getattr(node, "font_weight", None),
        "font_style": getattr(node, "font_style", None),
        "line_height": getattr(node, "line_height", None),
        "letter_spacing": getattr(node, "letter_spacing", None),
        "text_transform": getattr(node, "text_transform", None),
        "align": getattr(node, "align", None),
    }


def _high_layout_issues(result: LayoutGroundingResult) -> list[dict[str, Any]]:
    return [
        issue for issue in (result.issues or [])
        if issue.get("severity") in {"high", "blocker"}
    ]


def _persist_layout_grounding(
    iter_dir: Path,
    result: LayoutGroundingResult | None,
    *,
    filename: str = "layout_grounding.json",
) -> tuple[Path | None, dict[str, Any]]:
    if result is None:
        return None, {
            "layout_grounding_backend": "not-run",
            "layout_grounding_warnings": [],
            "layout_grounding_issues": [],
        }
    path = iter_dir / filename
    atomic_write_json(path, result.to_dict())
    return path, {
        "layout_grounding_backend": result.backend,
        "layout_grounding_warnings": result.warnings,
        "layout_grounding_issues": result.issues,
    }


def _ground_and_repair_poster_layout(
    layers: list[dict[str, Any]],
    cw: int,
    ch: int,
    html_path: Path,
    ctx: ToolContext,
) -> tuple[LayoutGroundingResult | None, list[dict[str, Any]]]:
    """Use browser DOM geometry to repair poster layout before final export."""
    repairs: list[dict[str, Any]] = []
    result: LayoutGroundingResult | None = None
    needs_final_ground = False

    for pass_idx in range(1, _POSTER_GROUNDING_REPAIR_PASSES + 1):
        write_html(layers, cw, ch, html_path, ctx, inline_images=False)
        result = ground_html_layout(
            html_path,
            ".canvas",
            viewport_width=cw,
            viewport_height=ch,
            timeout_ms=_POSTER_BROWSER_TIMEOUT_MS,
        )
        if result.warnings or result.backend != "playwright":
            break
        issues = _high_layout_issues(result)
        if not issues:
            break
        pass_repairs = _apply_poster_grounding_repairs(
            layers, issues, result, cw, ch, ctx, pass_idx=pass_idx,
        )
        if not pass_repairs:
            break
        repairs.extend(pass_repairs)
        needs_final_ground = True

    if needs_final_ground:
        write_html(layers, cw, ch, html_path, ctx, inline_images=False)
        result = ground_html_layout(
            html_path,
            ".canvas",
            viewport_width=cw,
            viewport_height=ch,
            timeout_ms=_POSTER_BROWSER_TIMEOUT_MS,
        )
    elif result is None:
        write_html(layers, cw, ch, html_path, ctx, inline_images=False)
        result = ground_html_layout(
            html_path,
            ".canvas",
            viewport_width=cw,
            viewport_height=ch,
            timeout_ms=_POSTER_BROWSER_TIMEOUT_MS,
        )

    return result, repairs


def _apply_poster_grounding_repairs(
    layers: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    result: LayoutGroundingResult,
    cw: int,
    ch: int,
    ctx: ToolContext,
    *,
    pass_idx: int,
) -> list[dict[str, Any]]:
    layer_by_id = {
        str(L.get("layer_id")): L for L in layers
        if L.get("layer_id")
    }
    element_by_id = {
        str(e.get("layer_id")): e for e in result.elements
        if e.get("layer_id")
    }
    repairs: list[dict[str, Any]] = []
    changed_text: set[str] = set()

    for issue in sorted(issues, key=_poster_grounding_issue_priority)[:64]:
        issue_type = str(issue.get("type") or "")
        repair: dict[str, Any] | None = None

        if issue_type in {"text_overflow_box", "clipped_bottom", "off_canvas"}:
            lid = str(issue.get("layer_id") or "")
            layer = layer_by_id.get(lid)
            if not layer:
                continue
            if layer.get("kind") == "text":
                repair = _fit_text_layer_for_grounding(
                    layer, issue, cw, ch, ctx, prefer_shrink=_is_bottom_or_footer_issue(layer, issue, ch),
                )
                if repair:
                    changed_text.add(lid)
            if repair is None:
                repair = _nudge_layer_inside_canvas(layer, issue, ch)

        elif issue_type in {"text_text_overlap", "text_visual_overlap"}:
            if issue_type == "text_visual_overlap":
                repair = _repair_grounding_text_visual_overlap(
                    issue, layer_by_id, element_by_id,
                )
                if repair:
                    target_id = None
                else:
                    target_id = _choose_grounding_overlap_target(issue, layer_by_id, element_by_id)
            else:
                target_id = _choose_grounding_overlap_target(issue, layer_by_id, element_by_id)
            target = layer_by_id.get(target_id or "")
            if repair is None and not target:
                continue
            delta = int(math.ceil(float((issue.get("overlap_px") or {}).get("y") or 0)))
            if repair is None and issue_type == "text_visual_overlap":
                repair = _move_layer_y(
                    target,
                    delta=max(_POSTER_TEXT_GAP_PX, delta + _POSTER_TEXT_GAP_PX),
                    ch=ch,
                    reason=issue_type,
                )
            if (
                repair is None
                and target.get("kind") == "text"
                and _is_bottom_or_footer_issue(target, issue, ch)
            ):
                repair = _fit_text_layer_for_grounding(
                    target, issue, cw, ch, ctx, prefer_shrink=True,
                )
                if repair:
                    changed_text.add(str(target.get("layer_id")))
            if repair is None:
                repair = _move_layer_y(
                    target,
                    delta=max(_POSTER_TEXT_GAP_PX, delta + _POSTER_TEXT_GAP_PX),
                    ch=ch,
                    reason=issue_type,
                )
            if repair is None and target.get("kind") == "text":
                repair = _fit_text_layer_for_grounding(
                    target, issue, cw, ch, ctx, prefer_shrink=True,
                )
                if repair:
                    changed_text.add(str(target.get("layer_id")))

        if repair:
            repair["pass"] = pass_idx
            repair["issue_type"] = issue_type
            repairs.append(repair)

    for layer_id in changed_text:
        layer = layer_by_id.get(layer_id)
        if layer is None:
            continue
        try:
            _rerender_poster_text_layer(layer, cw, ch, ctx)
        except Exception as e:
            log(
                "composite.poster_grounding_rerender_failed",
                layer_id=layer_id,
                error=str(e),
            )

    for repair in repairs:
        log("composite.poster_grounding_repair", **repair)
    return repairs


def _poster_grounding_issue_priority(issue: dict[str, Any]) -> tuple[int, float]:
    issue_type = str(issue.get("type") or "")
    order = {
        "clipped_bottom": 0,
        "off_canvas": 1,
        "text_overflow_box": 2,
        "text_text_overlap": 3,
        "text_visual_overlap": 4,
    }.get(issue_type, 9)
    overlap_area = float((issue.get("overlap_px") or {}).get("area") or 0)
    overflow = issue.get("overflow_px") or {}
    overflow_amt = max((float(v or 0) for v in overflow.values()), default=0.0)
    return order, -max(overlap_area, overflow_amt)


def _choose_grounding_overlap_target(
    issue: dict[str, Any],
    layer_by_id: dict[str, dict[str, Any]],
    element_by_id: dict[str, dict[str, Any]],
) -> str | None:
    a_id = str(issue.get("layer_a") or "")
    b_id = str(issue.get("layer_b") or "")
    a = layer_by_id.get(a_id)
    b = layer_by_id.get(b_id)
    if not a or not b:
        return None
    a_el = element_by_id.get(a_id) or {}
    b_el = element_by_id.get(b_id) or {}
    a_rect = a_el.get("ink_rect") or a_el.get("box_rect") or _bbox_rect(a)
    b_rect = b_el.get("ink_rect") or b_el.get("box_rect") or _bbox_rect(b)
    a_center = float(a_rect.get("y") or 0) + float(a_rect.get("h") or 0) / 2
    b_center = float(b_rect.get("y") or 0) + float(b_rect.get("h") or 0) / 2
    if abs(a_center - b_center) > 4:
        return a_id if a_center > b_center else b_id
    # Same row: prefer moving the higher z-index / later visual element.
    az = _safe_int(a.get("z_index"), 0)
    bz = _safe_int(b.get("z_index"), 0)
    return a_id if az >= bz else b_id


def _repair_grounding_text_visual_overlap(
    issue: dict[str, Any],
    layer_by_id: dict[str, dict[str, Any]],
    element_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    a_id = str(issue.get("layer_a") or "")
    b_id = str(issue.get("layer_b") or "")
    a = layer_by_id.get(a_id)
    b = layer_by_id.get(b_id)
    if not a or not b:
        return None

    if a.get("kind") == "text" and _is_grounding_visual_layer(b):
        text_id, text_layer = a_id, a
        visual_id, visual_layer = b_id, b
    elif b.get("kind") == "text" and _is_grounding_visual_layer(a):
        text_id, text_layer = b_id, b
        visual_id, visual_layer = a_id, a
    else:
        return None

    text_rect = _grounding_issue_rect(issue, text_id, text_layer, element_by_id)
    visual_rect = _grounding_issue_rect(issue, visual_id, visual_layer, element_by_id)
    if not text_rect or not visual_rect:
        return None

    repair = _trim_visual_horizontal_edge_for_grounding(
        visual_layer,
        visual_rect,
        text_rect,
        issue,
    )
    if repair:
        return repair

    if _is_caption_like_layer(text_layer):
        return _trim_visual_bottom_for_grounding_caption(
            visual_layer,
            visual_rect,
            text_rect,
            issue,
        )
    return None


def _is_grounding_visual_layer(layer: dict[str, Any]) -> bool:
    kind = str(layer.get("kind") or "")
    if kind == "text":
        return False
    if kind in {"image", "table"}:
        return bool(layer.get("bbox"))
    return bool(layer.get("bbox") and (layer.get("src_path") or layer.get("source_id")))


def _grounding_issue_rect(
    issue: dict[str, Any],
    layer_id: str,
    layer: dict[str, Any],
    element_by_id: dict[str, dict[str, Any]],
) -> dict[str, float] | None:
    if layer_id == str(issue.get("layer_a") or ""):
        rect = issue.get("rect_a")
    elif layer_id == str(issue.get("layer_b") or ""):
        rect = issue.get("rect_b")
    else:
        rect = None
    if not rect:
        element = element_by_id.get(layer_id) or {}
        rect = element.get("ink_rect") or element.get("box_rect")
    if not rect:
        rect = _bbox_rect(layer)
    try:
        return {
            "x": float(rect.get("x") or 0),
            "y": float(rect.get("y") or 0),
            "w": float(rect.get("w") or 0),
            "h": float(rect.get("h") or 0),
        }
    except (AttributeError, TypeError, ValueError):
        return None


def _trim_visual_horizontal_edge_for_grounding(
    visual_layer: dict[str, Any],
    visual_rect: dict[str, float],
    text_rect: dict[str, float],
    issue: dict[str, Any],
) -> dict[str, Any] | None:
    overlap = issue.get("overlap_px") or {}
    overlap_x = float(overlap.get("x") or 0)
    if overlap_x <= 0:
        return None

    edge_limit = max(48.0, min(visual_rect["w"], text_rect["w"]) * 0.16)
    if overlap_x > edge_limit:
        return None

    bbox = visual_layer.get("bbox") or {}
    old_bbox = dict(bbox)
    old_x = _safe_int(bbox.get("x"), 0)
    old_w = _safe_int(bbox.get("w"), 0)
    if old_w <= 0:
        return None
    old_right = old_x + old_w
    min_w = max(64, int(math.floor(old_w * 0.72)))

    visual_left = visual_rect["x"]
    visual_right = visual_rect["x"] + visual_rect["w"]
    text_left = text_rect["x"]
    text_right = text_rect["x"] + text_rect["w"]

    if text_left >= visual_left and text_left >= visual_right - overlap_x - 8:
        new_right = int(math.floor(text_left - _POSTER_GROUNDING_EDGE_GAP_PX))
        new_w = new_right - old_x
        if min_w <= new_w < old_w:
            bbox["w"] = new_w
            return {
                "layer_id": visual_layer.get("layer_id"),
                "reason": "ground_trim_visual_right_edge",
                "old_bbox": old_bbox,
                "new_bbox": dict(bbox),
            }

    if text_right <= visual_right and text_right <= visual_left + overlap_x + 8:
        new_x = int(math.ceil(text_right + _POSTER_GROUNDING_EDGE_GAP_PX))
        new_w = old_right - new_x
        if min_w <= new_w < old_w:
            bbox["x"] = new_x
            bbox["w"] = new_w
            return {
                "layer_id": visual_layer.get("layer_id"),
                "reason": "ground_trim_visual_left_edge",
                "old_bbox": old_bbox,
                "new_bbox": dict(bbox),
            }

    return None


def _trim_visual_bottom_for_grounding_caption(
    visual_layer: dict[str, Any],
    visual_rect: dict[str, float],
    text_rect: dict[str, float],
    issue: dict[str, Any],
) -> dict[str, Any] | None:
    overlap = issue.get("overlap_px") or {}
    overlap_y = float(overlap.get("y") or 0)
    if overlap_y <= 0:
        return None

    visual_top = visual_rect["y"]
    visual_bottom = visual_rect["y"] + visual_rect["h"]
    text_top = text_rect["y"]
    if text_top <= visual_top or visual_bottom <= text_top:
        return None

    if overlap_y > max(48.0, text_rect["h"] + 8.0):
        return None

    bbox = visual_layer.get("bbox") or {}
    old_bbox = dict(bbox)
    old_h = _safe_int(bbox.get("h"), 0)
    if old_h <= 0:
        return None
    min_h = max(48, int(math.floor(old_h * 0.60)))
    new_bottom = int(math.floor(text_top - 8))
    new_h = new_bottom - _safe_int(bbox.get("y"), 0)
    if min_h <= new_h < old_h:
        bbox["h"] = new_h
        return {
            "layer_id": visual_layer.get("layer_id"),
            "reason": "ground_trim_visual_bottom_for_caption",
            "old_bbox": old_bbox,
            "new_bbox": dict(bbox),
        }
    return None


def _is_caption_like_layer(layer: dict[str, Any]) -> bool:
    lid = str(layer.get("layer_id") or "").lower()
    name = str(layer.get("name") or "").lower()
    text = str(layer.get("text") or "").lower()
    haystack = f"{lid} {name} {text[:80]}"
    return any(token in haystack for token in ("caption", "cap_", "label", "figure", "fig.", "table"))


def _bbox_rect(layer: dict[str, Any]) -> dict[str, float]:
    bbox = layer.get("bbox") or {}
    return {
        "x": float(bbox.get("x") or 0),
        "y": float(bbox.get("y") or 0),
        "w": float(bbox.get("w") or 0),
        "h": float(bbox.get("h") or 0),
    }


def _is_bottom_or_footer_issue(layer: dict[str, Any], issue: dict[str, Any], ch: int) -> bool:
    lid = str(layer.get("layer_id") or "").lower()
    name = str(layer.get("name") or "").lower()
    haystack = f"{lid} {name}"
    footerish = any(
        token in haystack
        for token in ("footer", "caption", "cap_", "cite", "source", "takeaway", "key")
    )
    if footerish:
        return True
    overflow = issue.get("overflow_px") or {}
    if float(overflow.get("bottom") or 0) > 0:
        return True
    rect = issue.get("rect_a") or issue.get("rect_b") or issue.get("box_rect") or {}
    y = float(rect.get("y") or 0)
    h = float(rect.get("h") or 0)
    return y + h > 0 and y > 0.82 * ch


def _fit_text_layer_for_grounding(
    layer: dict[str, Any],
    issue: dict[str, Any],
    cw: int,
    ch: int,
    ctx: ToolContext,
    *,
    prefer_shrink: bool,
) -> dict[str, Any] | None:
    bbox = layer.get("bbox") or {}
    old_bbox = dict(bbox)
    old_font = _safe_int(layer.get("font_size_px"), 0)
    if old_font <= 0:
        return None
    original_font = _safe_int(layer.get("_ground_original_font_size_px"), old_font)
    layer["_ground_original_font_size_px"] = original_font
    min_font = max(8, int(math.floor(original_font * _POSTER_GROUNDING_MIN_FONT_SCALE)))

    overflow = issue.get("overflow_px") or {}
    needed_h = _safe_int(bbox.get("h"), 0)
    needed_h += int(math.ceil(float(overflow.get("bottom") or 0))) + _POSTER_TEXT_GAP_PX
    max_h = max(1, ch - _safe_int(bbox.get("y"), 0) - 8)

    if not prefer_shrink and needed_h > _safe_int(bbox.get("h"), 0) and needed_h <= max_h:
        bbox["h"] = min(needed_h, max_h)
        return {
            "layer_id": layer.get("layer_id"),
            "reason": "ground_expand_text_bbox",
            "old_bbox": old_bbox,
            "new_bbox": dict(bbox),
        }

    if old_font > min_font:
        new_font = max(min_font, int(math.floor(old_font * _POSTER_GROUNDING_FONT_STEP)))
        layer["font_size_px"] = new_font
        try:
            measured_h = measure_text_height(
                layer.get("text") or "",
                layer.get("font_family"),
                new_font,
                max(1, _safe_int(bbox.get("w"), cw)),
                ctx,
                line_height=layer.get("line_height"),
                letter_spacing=layer.get("letter_spacing"),
                text_transform=layer.get("text_transform"),
                font_weight=layer.get("font_weight"),
            )
        except Exception:
            measured_h = 0
        if measured_h > 0:
            bbox["h"] = min(
                max_h,
                max(_safe_int(bbox.get("h"), 0), int(math.ceil(measured_h * 1.08))),
            )
        return {
            "layer_id": layer.get("layer_id"),
            "reason": "ground_shrink_text_font",
            "old_font_size_px": old_font,
            "new_font_size_px": new_font,
            "old_bbox": old_bbox,
            "new_bbox": dict(bbox),
        }

    if needed_h > _safe_int(bbox.get("h"), 0):
        bbox["h"] = min(needed_h, max_h)
        if bbox != old_bbox:
            return {
                "layer_id": layer.get("layer_id"),
                "reason": "ground_expand_text_bbox_at_min_font",
                "old_bbox": old_bbox,
                "new_bbox": dict(bbox),
            }
    return None


def _nudge_layer_inside_canvas(
    layer: dict[str, Any],
    issue: dict[str, Any],
    ch: int,
) -> dict[str, Any] | None:
    bbox = layer.get("bbox") or {}
    overflow = issue.get("overflow_px") or {}
    bottom = int(math.ceil(float(overflow.get("bottom") or 0)))
    top = int(math.ceil(float(overflow.get("top") or 0)))
    if bottom > 0:
        return _move_layer_y(layer, delta=-(bottom + 8), ch=ch, reason="ground_nudge_inside_canvas")
    if top > 0:
        return _move_layer_y(layer, delta=top + 8, ch=ch, reason="ground_nudge_inside_canvas")
    if _safe_int(bbox.get("y"), 0) + _safe_int(bbox.get("h"), 0) > ch:
        return _move_layer_y(layer, delta=-(bbox["y"] + bbox["h"] - ch + 8), ch=ch, reason="ground_nudge_inside_canvas")
    return None


def _move_layer_y(
    layer: dict[str, Any],
    *,
    delta: int,
    ch: int,
    reason: str,
) -> dict[str, Any] | None:
    bbox = layer.get("bbox") or {}
    old = dict(bbox)
    y = _safe_int(bbox.get("y"), 0)
    h = _safe_int(bbox.get("h"), 1)
    max_y = max(0, ch - h - 8)
    new_y = max(0, min(max_y, y + int(delta)))
    if new_y == y:
        return None
    bbox["y"] = new_y
    return {
        "layer_id": layer.get("layer_id"),
        "reason": reason,
        "old_bbox": old,
        "new_bbox": dict(bbox),
    }


def _node_bbox(node: Any, role: str | None,
               slide_w: int, slide_h: int) -> tuple[int, int, int, int] | None:
    """Resolve a legacy layer-graph child's effective bbox.

    Order of precedence:
      1. Explicit ``node.bbox`` (planner-supplied absolute coords).
      2. Persisted compatibility slot geometry.

    Returns None when neither source produces a positive-area rect — the
    caller treats that as "this layer has no resolvable position" (the
    very condition the v2.7.5 detector flags)."""
    bbox = getattr(node, "bbox", None)
    if bbox is not None:
        try:
            bx = int(getattr(bbox, "x", 0) or 0)
            by = int(getattr(bbox, "y", 0) or 0)
            bw = int(getattr(bbox, "w", 0) or 0)
            bh = int(getattr(bbox, "h", 0) or 0)
        except (TypeError, ValueError):
            bx = by = bw = bh = 0
        if bw > 0 and bh > 0:
            return bx, by, bw, bh

    slot_bbox = _legacy_slot_bbox(role, getattr(node, "template_slot", None))
    if slot_bbox is not None:
        return slot_bbox

    return None


def _detect_deck_text_overlaps(
    slides: list[Any],
    *,
    slide_w: int,
    slide_h: int,
) -> list[dict[str, Any]]:
    """Walk each slide's text children + flag layout regressions.

    Three classes of warning:
      - ``slot_collision`` (blocker): two persisted legacy text children
        share the same deprecated ``template_slot`` and therefore resolve to
        the same compatibility geometry.
      - ``unanchored_text`` (blocker): a legacy text child has neither an
        explicit bbox nor a recognized persisted slot.
      - ``text_overlaps_shape`` (high): an effective text bbox overlaps a
        non-text sibling (image / table / callout). Catches captions
        landing on top of figures.

    Pure: never mutates ``slides``. Emits structured log events; returns
    the warning list so the legacy composite path can roll it into the payload
    the designer sees on the next turn.
    """
    warnings: list[dict[str, Any]] = []
    for slide in slides:
        slide_id = getattr(slide, "layer_id", None)
        role = getattr(slide, "role", None) or "content"
        children = list(getattr(slide, "children", None) or [])

        text_children: list[tuple[Any, tuple[int, int, int, int] | None]] = []
        nontext_children: list[tuple[Any, tuple[int, int, int, int] | None]] = []
        slot_seen: dict[str, str] = {}

        for child in children:
            kind = getattr(child, "kind", None)
            slot = getattr(child, "template_slot", None)
            cid = getattr(child, "layer_id", None) or "?"

            if kind == "text":
                if slot:
                    prior = slot_seen.get(slot)
                    if prior:
                        entry = {
                            "kind": "slot_collision",
                            "severity": "blocker",
                            "slide_id": slide_id,
                            "template_slot": slot,
                            "layer_a": prior,
                            "layer_b": cid,
                        }
                        warnings.append(entry)
                        log("composite.deck_text_overlap_warning", **entry)
                    else:
                        slot_seen[slot] = cid

                ext = _effective_text_extent(child, role=role)
                if ext is None:
                    if getattr(child, "bbox", None) is None and not slot:
                        entry = {
                            "kind": "unanchored_text",
                            "severity": "blocker",
                            "slide_id": slide_id,
                            "layer_id": cid,
                            "text_preview": (
                                (getattr(child, "text", None) or "")[:80]
                            ),
                        }
                        warnings.append(entry)
                        log("composite.deck_text_overlap_warning", **entry)
                    continue
                text_children.append((child, ext))
            elif kind in ("image", "table", "callout", "background"):
                if kind == "background":
                    continue  # backgrounds are full-bleed by design
                bbox = _node_bbox(child, role, slide_w, slide_h)
                nontext_children.append((child, bbox))

        # Text vs text — same-slide stacking collisions.
        for i in range(len(text_children)):
            la, ea = text_children[i]
            for j in range(i + 1, len(text_children)):
                lb, eb = text_children[j]
                ov = _rects_overlap(ea, eb)
                if ov is None:
                    continue
                _x_ov, y_ov = ov
                entry = {
                    "kind": "text_overlaps_text",
                    "severity": "high",
                    "slide_id": slide_id,
                    "layer_a": getattr(la, "layer_id", None),
                    "layer_b": getattr(lb, "layer_id", None),
                    "y_overlap_px": int(y_ov),
                }
                warnings.append(entry)
                log("composite.deck_text_overlap_warning", **entry)

        # Text vs non-text (image / table / callout).
        for la, ea in text_children:
            for lb, eb in nontext_children:
                if eb is None:
                    continue
                ov = _rects_overlap(ea, eb)
                if ov is None:
                    continue
                x_ov, y_ov = ov
                entry = {
                    "kind": "text_overlaps_shape",
                    "severity": "high",
                    "slide_id": slide_id,
                    "text_layer": getattr(la, "layer_id", None),
                    "shape_layer": getattr(lb, "layer_id", None),
                    "shape_kind": getattr(lb, "kind", None),
                    "x_overlap_px": int(x_ov),
                    "y_overlap_px": int(y_ov),
                }
                warnings.append(entry)
                log("composite.deck_text_overlap_warning", **entry)

    return warnings


def _detect_orphan_callouts(
    slides: list[Any],
    *,
    slide_w: int,
    slide_h: int,
) -> tuple[set[str], list[dict[str, Any]]]:
    """Identify callout layer_ids that should be dropped at composite time.

    A callout is an orphan when:
      - ``anchor_layer_id`` is None or empty, OR
      - ``anchor_layer_id`` references a sibling that doesn't exist on the
        same slide, OR
      - the referenced sibling is not a placeable shape (only
        ``image`` / ``table`` qualify), OR
      - both ``anchor_layer_id`` AND ``callout_region`` are set but the
        region's bbox does not intersect the anchor's bbox (the v2.7.5
        "circle floating in empty space" defect — slide16 of the
        2026-04-26 dogfood).

    Pure inspection over the spec tree — does NOT call into the renderer.
    Returns ``(orphan_layer_ids, warnings)``; the renderer's pass-2
    callout walker honours the set by skipping placement entirely.
    """
    orphans: set[str] = set()
    warnings: list[dict[str, Any]] = []

    for slide in slides:
        slide_id = getattr(slide, "layer_id", None)
        role = getattr(slide, "role", None) or "content"
        children = list(getattr(slide, "children", None) or [])
        sibling_by_id: dict[str, Any] = {}
        for child in children:
            cid = getattr(child, "layer_id", None)
            if cid:
                sibling_by_id[cid] = child

        for child in children:
            if getattr(child, "kind", None) != "callout":
                continue
            cid = getattr(child, "layer_id", None) or "?"
            anchor_id = getattr(child, "anchor_layer_id", None)
            reason: str | None = None

            if not anchor_id:
                reason = "no_anchor_layer_id"
            else:
                anchor = sibling_by_id.get(anchor_id)
                if anchor is None:
                    reason = "anchor_not_on_slide"
                elif getattr(anchor, "kind", None) not in ("image", "table"):
                    reason = "anchor_kind_not_placeable"
                else:
                    region = getattr(child, "callout_region", None)
                    if region is not None:
                        anchor_bbox = _node_bbox(anchor, role, slide_w, slide_h)
                        if anchor_bbox is not None:
                            try:
                                rx = int(getattr(region, "x", 0) or 0)
                                ry = int(getattr(region, "y", 0) or 0)
                                rw = int(getattr(region, "w", 0) or 0)
                                rh = int(getattr(region, "h", 0) or 0)
                            except (TypeError, ValueError):
                                rx = ry = rw = rh = 0
                            if rw > 0 and rh > 0 and _rects_overlap(
                                (rx, ry, rw, rh), anchor_bbox
                            ) is None:
                                reason = "region_outside_anchor_bbox"

            if reason is not None:
                orphans.add(cid)
                entry = {
                    "slide_id": slide_id,
                    "callout_layer_id": cid,
                    "anchor_layer_id": anchor_id,
                    "reason": reason,
                }
                warnings.append(entry)
                log("composite.callout_orphan_warning", **entry)

    return orphans, warnings


# v2.8.2 C3 — closing slot enforcer. Last slide of a deck must contain
# substantive takeaways, not a thin "Thank You" stub.
# Detection only — emits a warning to the planner via tool_result so it
# can populate the closing slide on the next iteration. No auto-fix.
#
# Same blacklist as v2.8.2 C1 sanitizer; kept local to avoid cross-module
# import in case C1 hasn't merged yet.
_CLOSING_STUB_PHRASES: tuple[str, ...] = (
    "thank you",
    "thanks",
    "questions",
    "q&a",
    "q & a",
    "any questions",
)

_CLOSING_PLACEHOLDER_SUBSTRINGS: tuple[str, ...] = (
    "paper title goes here",
    "author one",
    "author two",
    "affiliation goes here",
    "yyyy-mm-dd",
    "your name here",
)


def _collect_closing_text_runs(node: Any) -> list[str]:
    """Returns all non-empty text runs from a slide subtree.

    Walks ``text`` and ``caption`` on the node and recurses into
    ``children``. Empty / whitespace-only runs are filtered out.
    """
    runs: list[str] = []
    text = getattr(node, "text", None)
    if text and str(text).strip():
        runs.append(str(text).strip())
    caption = getattr(node, "caption", None)
    if caption and str(caption).strip():
        runs.append(str(caption).strip())
    for child in (getattr(node, "children", None) or []):
        runs.extend(_collect_closing_text_runs(child))
    return runs


def _find_closing_slide(spec: Any) -> Any | None:
    """Returns the closing slide LayerNode by ``role="closing"`` (preferred)
    or the last slide (``kind="slide"``) in ``layer_graph`` (fallback).
    Returns None if no slides found.
    """
    layer_graph = getattr(spec, "layer_graph", None) or []
    closing = None
    last_slide = None
    for node in layer_graph:
        if getattr(node, "kind", None) != "slide":
            continue
        last_slide = node
        if getattr(node, "role", None) == "closing":
            closing = node
    return closing or last_slide


def _detect_closing_stub(spec: Any) -> list[dict[str, Any]]:
    """Returns warnings if the last slide's content is too thin or stub-like.

    Stub criteria (any one triggers warning):
    - Fewer than 3 non-empty text runs across all descendants
    - All runs match ``_CLOSING_STUB_PHRASES`` (e.g. just "Thank you" / "Q&A")
    - All runs match ``_CLOSING_PLACEHOLDER_SUBSTRINGS``

    Operates on structural properties only — no per-paper heuristics, so
    the check generalizes across paper / blog / .docx / free-text decks.
    Returns ``[]`` when the closing slide is substantive (the common case).
    """
    closing = _find_closing_slide(spec)
    if closing is None:
        return []
    runs = _collect_closing_text_runs(closing)
    warnings: list[dict[str, Any]] = []
    slide_id = (
        getattr(closing, "layer_id", None)
        or getattr(closing, "name", None)
        or "<closing>"
    )
    if len(runs) < 3:
        entry = {
            "slide_id": slide_id,
            "reason": "thin_content",
            "text_run_count": len(runs),
            "preview": runs[:3],
        }
        warnings.append(entry)
        log("composite.closing_stub_warning", **entry)
        return warnings
    lower_runs = [r.lower() for r in runs]
    all_stub = all(
        any(needle in r for needle in _CLOSING_STUB_PHRASES) for r in lower_runs
    )
    if all_stub:
        entry = {
            "slide_id": slide_id,
            "reason": "all_stub_phrases",
            "text_run_count": len(runs),
            "preview": runs[:3],
        }
        warnings.append(entry)
        log("composite.closing_stub_warning", **entry)
        return warnings
    all_placeholder = all(
        any(needle in r for needle in _CLOSING_PLACEHOLDER_SUBSTRINGS)
        for r in lower_runs
    )
    if all_placeholder:
        entry = {
            "slide_id": slide_id,
            "reason": "all_placeholders",
            "text_run_count": len(runs),
            "preview": runs[:3],
        }
        warnings.append(entry)
        log("composite.closing_stub_warning", **entry)
    return warnings


def _aspect_fit_contain(
    src_size: tuple[int, int],
    dst_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    """Compute the (new_w, new_h, off_x, off_y) that fits `src_size`
    into `dst_size` preserving aspect ratio, centered. Letterbox-style.

    Empty source or dest yields a 1×1 no-op at origin so callers don't
    crash on malformed input.
    """
    sw, sh = src_size
    dw, dh = dst_size
    if sw <= 0 or sh <= 0 or dw <= 0 or dh <= 0:
        return 1, 1, 0, 0
    scale = min(dw / sw, dh / sh)
    nw = max(1, int(sw * scale))
    nh = max(1, int(sh * scale))
    off_x = (dw - nw) // 2
    off_y = (dh - nh) // 2
    return nw, nh, off_x, off_y


def _maybe_warn_aspect(layer: dict[str, Any], src_size: tuple[int, int],
                       bbox: tuple[int, int, int, int]) -> None:
    """Emit `composite.bbox_aspect_warning` when the planner's bbox
    aspect ratio diverges from the layer's source content by more than
    `_ASPECT_MISMATCH_WARN_RATIO`. Future planner-prompt tuning can
    consume these warnings to learn which figure kinds get systemically
    under-sized."""
    sw, sh = src_size
    _bx, _by, bw, bh = bbox
    if min(sw, sh, bw, bh) <= 0:
        return
    src_aspect = sw / sh
    bbox_aspect = bw / bh
    ratio = max(src_aspect, bbox_aspect) / min(src_aspect, bbox_aspect)
    if ratio >= _ASPECT_MISMATCH_WARN_RATIO:
        log("composite.bbox_aspect_warning",
            layer_id=layer.get("layer_id"),
            kind=layer.get("kind"),
            src_size=f"{sw}x{sh}",
            bbox=f"{bw}x{bh}",
            aspect_mismatch=round(ratio, 2))


def composite(args: dict[str, Any], *, ctx: ToolContext) -> ToolResultRecord:
    spec = ctx.state.get("design_spec")
    if spec is None:
        return obs_error("propose_design_spec must be called first", category="validation")
    spec = canonicalize_design_spec(spec)
    ctx.state["design_spec"] = spec

    # Landing mode (v1.0 #8) is HTML-only — no PSD/SVG, no per-layer PNGs.
    # It reads the section tree directly from design_spec.layer_graph.
    if spec.artifact_type == ArtifactType.LANDING:
        return _composite_landing(spec, ctx)

    # Deck mode is HTML-first. Legacy layer-graph specs remain readable for
    # compatibility exports; inline images are hydrated from rendered_layers.
    if spec.artifact_type == ArtifactType.DECK:
        return _composite_deck(spec, ctx)

    if should_use_authored_paper_poster(spec, ctx):
        ctx.state[AUTHORED_PAPER_POSTER_REQUIRED_STATE_KEY] = True
        return _composite_authored_paper_poster(spec, ctx)
    if is_academic_paper_poster_context(spec, ctx) and find_authored_paper_poster_frame(spec) is None:
        ctx.state[AUTHORED_PAPER_POSTER_REQUIRED_STATE_KEY] = True
        return obs_error(
            "academic paper poster requires an authored_html HtmlFrame; "
            "legacy/layer poster composite is disabled for this run",
            category="validation",
            payload={
                "issue_id": "candidate_final_not_authored_html",
                "expected_render_mode": "authored_html",
                "repair_route": "revise_authored_html",
                "artifact_type": "poster",
            },
        )

    canvas = spec.canvas
    cw, ch = int(canvas["w_px"]), int(canvas["h_px"])
    html_layers = _poster_layers_from_html_artifact(spec, ctx, cw=cw, ch=ch)

    rendered = ctx.state["rendered_layers"]
    if not rendered and html_layers is None:
        return obs_error(
            "no layers rendered yet — call generate_background and render_text_layer first",
            category="validation",
        )

    # v1.1 paper2any: ingested PDF figures are registered in rendered_layers
    # with bbox=None (they were authored for flow-layout landing/deck use).
    # The planner places them on the poster by giving each a bbox in
    # spec.layer_graph. Hydrate that bbox onto the rendered_layer record
    # before composite walks it. Pattern mirrors _hydrate_landing_image_srcs.
    if html_layers is not None:
        sorted_layers = html_layers
        log(
            "composite.poster.html_artifact_layers",
            layers=len(sorted_layers),
            text_layers=sum(1 for L in sorted_layers if L.get("kind") == "text"),
            image_layers=sum(1 for L in sorted_layers if L.get("kind") == "image"),
            shape_layers=sum(1 for L in sorted_layers if L.get("kind") == "shape"),
        )
    else:
        _hydrate_poster_layer_bboxes(rendered, spec)
        sorted_layers = sorted(rendered.values(), key=lambda L: int(L.get("z_index", 0)))
        # Drop any image/background layers that still have no bbox — the planner
        # declared them in spec but didn't place them, OR they're stale records.
        sorted_layers = [L for L in sorted_layers if L.get("bbox")]

    pre_repair_text_overlap_warnings = _detect_text_overlaps(sorted_layers, ctx=ctx)
    poster_layout_repairs = _repair_poster_text_layout(sorted_layers, cw, ch, ctx)
    sorted_layers = sorted(sorted_layers, key=lambda L: int(L.get("z_index", 0)))

    iter_dir, iter_num = _open_iter_dir(ctx)
    prior_preview_sha = _prior_preview_sha(ctx)
    psd_path = iter_dir / "poster.psd"
    svg_path = iter_dir / "poster.svg"
    html_path = iter_dir / "poster.html"
    browser_html_path = iter_dir / "poster.browser.html"
    preview_path = iter_dir / "preview.png"

    layer_manifest: list[dict[str, Any]] = []

    try:
        layout_grounding_result, layout_grounding_repairs = _ground_and_repair_poster_layout(
            sorted_layers, cw, ch, browser_html_path, ctx,
        )
    except Exception as e:
        return obs_error(f"layout grounding failed: {e}", category="api")

    # The browser-grounding pass mutates layer bboxes/font sizes. Write all
    # export formats only after that so PSD/SVG/HTML/preview agree.
    try:
        _write_psd(sorted_layers, cw, ch, psd_path, layer_manifest, ctx)
    except Exception as e:
        return obs_error(f"PSD write failed: {e}", category="api")

    try:
        _write_svg(sorted_layers, cw, ch, svg_path, ctx)
    except Exception as e:
        return obs_error(f"SVG write failed: {e}", category="api")

    try:
        write_html(sorted_layers, cw, ch, html_path, ctx)
    except Exception as e:
        return obs_error(f"HTML write failed: {e}", category="api")
    quality_payload = _lint_composite_html(
        html_path,
        artifact_type="poster",
        iter_dir=iter_dir,
    )
    density_payload = audit_paper_poster_density(
        sorted_layers,
        {"w_px": cw, "h_px": ch},
        rendered_layers=ctx.state.get("rendered_layers") or {},
        poster_plan_contract=ctx.state.get("poster_plan_contract"),
    )
    information_payload = audit_paper_poster_information(
        sorted_layers,
        {"w_px": cw, "h_px": ch},
        rendered_layers=ctx.state.get("rendered_layers") or {},
        poster_plan_contract=ctx.state.get("poster_plan_contract"),
    )
    poster_contract_payload = audit_poster_plan_contract(
        ctx.state.get("poster_plan_contract"),
        sorted_layers,
        {"w_px": cw, "h_px": ch},
        spec=spec,
    )
    try:
        atomic_write_json(iter_dir / "paper_density.json", {
            "artifact_type": "poster",
            **density_payload,
        })
    except OSError:
        pass
    log(
        "paper.density.done",
        artifact_type="poster",
        findings=len(density_payload["paper_density_findings"]),
        p0=density_payload["paper_density_p0_count"],
        placed_visuals=density_payload["placed_visual_count"],
        visual_area_ratio=density_payload["visual_area_ratio"],
    )
    try:
        atomic_write_json(iter_dir / "paper_information.json", {
            "artifact_type": "poster",
            **information_payload,
        })
    except OSError:
        pass
    log(
        "paper.information.done",
        artifact_type="poster",
        findings=len(information_payload["paper_information_findings"]),
        p0=information_payload["paper_information_p0_count"],
        info_units=information_payload["paper_info_unit_count"],
        visible_words=information_payload["visible_text_word_count"],
        method_callouts=information_payload["method_callout_count"],
        evidence_bullets=information_payload["evidence_bullet_count"],
    )
    try:
        atomic_write_json(iter_dir / "poster_plan_contract_audit.json", {
            "artifact_type": "poster",
            **poster_contract_payload,
        })
    except OSError:
        pass
    log(
        "poster.contract.done",
        artifact_type="poster",
        findings=len(poster_contract_payload.get("poster_contract_findings") or []),
        p0=poster_contract_payload.get("poster_contract_p0_count"),
        placed_visuals=(poster_contract_payload.get("poster_contract_metrics") or {}).get("placed_visual_count"),
        selected_placed=(poster_contract_payload.get("poster_contract_metrics") or {}).get("placed_selected_visual_count"),
    )

    frame_backend = "playwright"
    frame_warnings: list[str] = []
    poster_preview_fallback_used = False
    browser_result = screenshot_html(
        browser_html_path if browser_html_path.exists() else html_path,
        preview_path,
        viewport_width=cw,
        viewport_height=ch,
        selector=".canvas",
        max_edge=ctx.settings.poster_preview_max_edge,
        timeout_ms=_POSTER_BROWSER_TIMEOUT_MS,
    )
    preview_scale = browser_result.scale
    preview_canvas_w_px = browser_result.width_px
    preview_canvas_h_px = browser_result.height_px
    if browser_result.warnings:
        poster_preview_fallback_used = True
        frame_backend = browser_result.backend
        frame_warnings.extend(browser_result.warnings)
        try:
            _write_preview(sorted_layers, cw, ch, preview_path, ctx)
        except Exception as e:
            return obs_error(f"preview render failed: {e}", category="api")
        preview_scale, preview_canvas_w_px, preview_canvas_h_px = (
            downsample_image_to_max_edge(preview_path, ctx.settings.poster_preview_max_edge)
        )
    else:
        frame_backend = browser_result.backend

    layout_grounding_path, layout_grounding_payload = _persist_layout_grounding(
        iter_dir,
        layout_grounding_result,
    )
    text_overlap_warnings = _detect_text_overlaps(sorted_layers, ctx=ctx)
    xref_misses = _detect_missing_figure_xrefs(sorted_layers, spec)

    artifacts = CompositionArtifacts(
        psd_path=str(psd_path),
        svg_path=str(svg_path),
        html_path=str(html_path),
        html_artifact_path=str(iter_dir / "html_artifact.json"),
        preview_path=str(preview_path),
        layer_manifest=layer_manifest,
    )
    ctx.state["composition"] = artifacts
    _refresh_final_links(iter_dir, ctx, [
        "poster.psd",
        "poster.svg",
        "poster.html",
        "preview.png",
        "poster_plan_contract_audit.json",
    ])
    _remove_final_links(ctx, [
        "poster.pdf",
        "paper_poster_render_manifest.json",
        "paper_poster_dom_audit.json",
        "poster_gate_audit.json",
    ])
    log("composite.done",
        iter=iter_num,
        psd=str(psd_path), svg=str(svg_path), html=str(html_path),
        preview=str(preview_path), layers=len(sorted_layers),
        text_overlaps=len(text_overlap_warnings),
        figure_xref_misses=len(xref_misses),
        quality_lint_p0_count=quality_payload["quality_lint_p0_count"],
        paper_density_p0_count=density_payload["paper_density_p0_count"],
        paper_information_p0_count=information_payload["paper_information_p0_count"],
        placed_visual_count=density_payload["placed_visual_count"],
        visual_area_ratio=density_payload["visual_area_ratio"],
        preview_fallback=poster_preview_fallback_used,
        layout_grounding_issues=len((layout_grounding_result.issues if layout_grounding_result else []) or []),
        layout_grounding_repairs=len(layout_grounding_repairs))

    preview_sha = sha256_file(preview_path)
    payload: dict[str, Any] = {
        "artifact_type": "poster",
        "iteration": iter_num,
        "spec_revision": int(ctx.state.get("spec_revision_count") or 0),
        "preview_sha256": preview_sha,
        "psd_sha256": sha256_file(psd_path),
        "svg_sha256": sha256_file(svg_path),
        "html_sha256": sha256_file(html_path),
        "n_layers": len(sorted_layers),
        "canvas": {"w_px": cw, "h_px": ch},
        # Versioned paths: each iteration's outputs survive on disk for
        # DPO / layered-gen training. Use this relative_path; final/ is
        # only a convenience symlink for product consumers.
        "preview_relative_path": f"composites/iter_{iter_num:02d}/preview.png",
        "preview_scale": preview_scale,
        "preview_canvas_w_px": preview_canvas_w_px,
        "preview_canvas_h_px": preview_canvas_h_px,
        "html_relative_path": f"composites/iter_{iter_num:02d}/poster.html",
        "frame_render_backend": frame_backend,
        "frame_render_warnings": frame_warnings,
        "poster_preview_fallback_used": poster_preview_fallback_used,
        **quality_payload,
        **density_payload,
        **information_payload,
        **poster_contract_payload,
        **layout_grounding_payload,
        "layout_grounding_repairs": layout_grounding_repairs,
        "layout_grounding_relative_path": (
            f"composites/iter_{iter_num:02d}/{layout_grounding_path.name}"
            if layout_grounding_path else None
        ),
        "poster_layout_repairs": poster_layout_repairs,
        "pre_repair_text_overlap_warnings": pre_repair_text_overlap_warnings,
        # Real environment state — text overlaps and missing xrefs are
        # actual quality signals. The policy can decide whether to fix
        # them via edit_layer or move on. NOT prose hints.
        "text_overlap_warnings": text_overlap_warnings,
        "xref_misses": xref_misses,
    }
    if prior_preview_sha:
        payload["supersedes_preview_sha256"] = prior_preview_sha
    payload = _attach_html_artifact_contract(
        payload,
        spec=spec,
        iter_dir=iter_dir,
        iter_num=iter_num,
    )
    payload = _attach_design_feedback(
        payload,
        iter_dir=iter_dir,
        iter_num=iter_num,
        ctx=ctx,
    )
    ctx.state["last_composite_payload"] = payload
    _mark_visual_reference_revision_composited(ctx)
    return obs_ok(payload)


def _composite_authored_paper_poster(spec: Any, ctx: ToolContext) -> ToolResultRecord:
    """Composite a paper poster from authored final-canvas HTML/CSS."""
    canvas = spec.canvas
    cw, ch = int(canvas["w_px"]), int(canvas["h_px"])
    iter_dir, iter_num = _open_iter_dir(ctx)
    prior_preview_sha = _prior_preview_sha(ctx)

    try:
        rendered = render_authored_paper_poster(
            spec,
            ctx,
            iter_dir=iter_dir,
            iter_num=iter_num,
            timeout_ms=_POSTER_BROWSER_TIMEOUT_MS,
        )
    except ValueError as e:
        return obs_error(str(e), category="validation")
    except Exception as e:
        return obs_error(f"authored paper poster render failed: {type(e).__name__}: {e}", category="api")

    quality_payload = _lint_composite_html(
        rendered.html_path,
        artifact_type="poster",
        iter_dir=iter_dir,
    )
    sorted_layers = sorted(rendered.pseudo_layers, key=lambda L: int(L.get("z_index", 0)))
    density_payload = audit_paper_poster_density(
        sorted_layers,
        {"w_px": cw, "h_px": ch},
        rendered_layers=ctx.state.get("rendered_layers") or {},
        poster_plan_contract=ctx.state.get("poster_plan_contract"),
    )
    information_payload = audit_paper_poster_information(
        sorted_layers,
        {"w_px": cw, "h_px": ch},
        rendered_layers=ctx.state.get("rendered_layers") or {},
        poster_plan_contract=ctx.state.get("poster_plan_contract"),
    )
    poster_contract_payload = audit_poster_plan_contract(
        ctx.state.get("poster_plan_contract"),
        sorted_layers,
        {"w_px": cw, "h_px": ch},
        spec=spec,
    )
    try:
        atomic_write_json(iter_dir / "paper_density.json", {
            "artifact_type": "poster",
            **density_payload,
        })
        atomic_write_json(iter_dir / "paper_information.json", {
            "artifact_type": "poster",
            **information_payload,
        })
        atomic_write_json(iter_dir / "poster_plan_contract_audit.json", {
            "artifact_type": "poster",
            **poster_contract_payload,
        })
    except OSError:
        pass

    text_overlap_warnings: list[dict[str, Any]] = []
    editorial_flow_contract = (
        str(poster_contract_payload.get("reference_profile") or "") == "conference_editorial_flow"
        or isinstance(poster_contract_payload.get("editorial_flow_contract"), dict)
    )
    xref_misses = [] if editorial_flow_contract else _detect_missing_figure_xrefs(sorted_layers, spec)
    artifacts = CompositionArtifacts(
        html_path=str(rendered.html_path),
        html_artifact_path=str(iter_dir / "html_artifact.json"),
        pdf_path=str(rendered.pdf_path),
        preview_path=str(rendered.preview_path),
        layer_manifest=sorted_layers,
    )
    ctx.state["composition"] = artifacts
    final_files = [
        "poster.html",
        "poster.pdf",
        "preview.png",
        "paper_poster_render_manifest.json",
        "paper_poster_dom_audit.json",
        "poster_gate_audit.json",
        "poster_plan_contract_audit.json",
    ]
    _refresh_final_links(iter_dir, ctx, final_files)
    log(
        "composite.authored_paper_poster.done",
        iter=iter_num,
        html=str(rendered.html_path),
        pdf=str(rendered.pdf_path),
        preview=str(rendered.preview_path),
        layers=len(sorted_layers),
        dom_p0=rendered.dom_audit.get("paper_poster_dom_p0_count"),
        preview_fallback=rendered.preview_fallback_used,
    )

    payload: dict[str, Any] = {
        "artifact_type": "poster",
        "iteration": iter_num,
        "render_mode": "authored_html",
        "spec_revision": int(ctx.state.get("spec_revision_count") or 0),
        "preview_sha256": sha256_file(rendered.preview_path),
        "html_sha256": sha256_file(rendered.html_path),
        "pdf_sha256": sha256_file(rendered.pdf_path) if rendered.pdf_path.exists() else None,
        "n_layers": len(sorted_layers),
        "canvas": {"w_px": cw, "h_px": ch},
        "poster_size": rendered.size,
        "poster_size_source": rendered.size.get("source"),
        "preview_relative_path": f"composites/iter_{iter_num:02d}/preview.png",
        "preview_scale": rendered.preview.scale,
        "preview_canvas_w_px": rendered.preview.width_px,
        "preview_canvas_h_px": rendered.preview.height_px,
        "html_relative_path": f"composites/iter_{iter_num:02d}/poster.html",
        "pdf_relative_path": f"composites/iter_{iter_num:02d}/poster.pdf",
        "paper_poster_render_manifest_relative_path": (
            f"composites/iter_{iter_num:02d}/paper_poster_render_manifest.json"
        ),
        "paper_poster_dom_audit_relative_path": (
            f"composites/iter_{iter_num:02d}/paper_poster_dom_audit.json"
        ),
        "poster_gate_audit_relative_path": (
            f"composites/iter_{iter_num:02d}/poster_gate_audit.json"
        ),
        "poster_gate_backend": rendered.gate_audit.get("backend"),
        "poster_gate_p0_count": int(rendered.gate_audit.get("p0_count") or 0),
        "poster_gate_p1_count": int(rendered.gate_audit.get("p1_count") or 0),
        "poster_gate_findings": [
            finding for finding in rendered.gate_audit.get("findings") or []
            if isinstance(finding, dict)
        ],
        "poster_gate_findings_sample": [
            finding for finding in rendered.gate_audit.get("findings") or []
            if isinstance(finding, dict)
        ][:6],
        "poster_gate_metrics": rendered.gate_audit.get("metrics") or {},
        "frame_render_backend": rendered.preview.backend,
        "frame_render_warnings": rendered.preview.warnings,
        "poster_preview_fallback_used": rendered.preview_fallback_used,
        "pdf_render_backend": rendered.pdf.backend,
        "pdf_render_warnings": rendered.pdf.warnings,
        "layout_grounding_issues": [],
        "layout_grounding_warnings": [],
        "layout_grounding_backend": "authored_html_dom_audit",
        "layout_grounding_repairs": [],
        "poster_layout_repairs": [],
        "pre_repair_text_overlap_warnings": [],
        "text_overlap_warnings": text_overlap_warnings,
        "xref_misses": xref_misses,
        **rendered.sanitized,
        **{k: v for k, v in rendered.dom_audit.items() if k != "dom_layers"},
        "paper_poster_dom_layer_count": len(rendered.dom_audit.get("dom_layers") or []),
        **quality_payload,
        **density_payload,
        **information_payload,
        **poster_contract_payload,
    }
    combined_generation_findings: list[Any] = []
    generation_contract_findings = ctx.state.get("paper_poster_generation_contract_findings")
    if isinstance(generation_contract_findings, list) and generation_contract_findings:
        combined_generation_findings.extend(copy.deepcopy(generation_contract_findings))
    source_contract_findings = ctx.state.get("paper_poster_source_contract_findings")
    if isinstance(source_contract_findings, list) and source_contract_findings:
        combined_generation_findings.extend(copy.deepcopy(source_contract_findings))
    slot_contract_findings = ctx.state.get("paper_poster_slot_contract_findings")
    if isinstance(slot_contract_findings, list) and slot_contract_findings:
        combined_generation_findings.extend(copy.deepcopy(slot_contract_findings))
    if combined_generation_findings:
        payload["authored_generation_contract_findings"] = combined_generation_findings
        payload["authored_generation_contract_p0_count"] = len(combined_generation_findings)
    if prior_preview_sha:
        payload["supersedes_preview_sha256"] = prior_preview_sha
    payload = _attach_html_artifact_contract(
        payload,
        spec=spec,
        iter_dir=iter_dir,
        iter_num=iter_num,
    )
    payload = _attach_design_feedback(
        payload,
        iter_dir=iter_dir,
        iter_num=iter_num,
        ctx=ctx,
    )
    payload = _maybe_retain_dogfood_best_authored_paper_poster(
        iter_dir=iter_dir,
        ctx=ctx,
        payload=payload,
        final_files=final_files,
    )
    ctx.state["last_composite_payload"] = payload
    _mark_visual_reference_revision_composited(ctx)
    return obs_ok(payload)


def _composite_landing(spec: Any, ctx: ToolContext) -> ToolResultRecord:
    """HTML-only landing-mode composite. Reads the section tree from
    design_spec.layer_graph (not ctx.state['rendered_layers'])."""
    layer_graph = list(spec.layer_graph or [])
    if not layer_graph:
        return obs_error(
            "landing design_spec has empty layer_graph — "
            "propose_design_spec with a section tree first",
            category="validation",
        )

    iter_dir, iter_num = _open_iter_dir(ctx)
    prior_preview_sha = _prior_preview_sha(ctx)
    html_path = iter_dir / "index.html"
    preview_path = iter_dir / "preview.png"
    canvas = spec.canvas or {}
    cw = int(canvas.get("w_px", 1200))

    # Re-hydrate image children with src_path from rendered_layers before
    # manifest build / HTML write — see _hydrate_landing_image_srcs docstring.
    _hydrate_landing_image_srcs(layer_graph, ctx)

    manifest: list[dict[str, Any]] = []
    for node in layer_graph:
        kind = getattr(node, "kind", None)
        if kind == "section":
            manifest.append({
                "layer_id": node.layer_id,
                "name": node.name,
                "kind": "section",
                "children": [
                    {"layer_id": c.layer_id, "name": c.name, "kind": c.kind,
                     "text": getattr(c, "text", None),
                     "src_path": getattr(c, "src_path", None),
                     **_text_manifest_fields(c)}
                    for c in (node.children or [])
                ],
            })
        elif kind == "text":
            manifest.append({
                "layer_id": node.layer_id,
                "name": node.name,
                "kind": "text",
                "text": node.text,
                **_text_manifest_fields(node),
            })
        elif kind == "image":
            manifest.append({
                "layer_id": node.layer_id,
                "name": node.name,
                "kind": "image",
                "src_path": node.src_path,
            })
        elif kind == "table":
            manifest.append({
                "layer_id": node.layer_id,
                "name": node.name,
                "kind": "table",
                "src_path": node.src_path,
                "rows": list(node.rows or []),
                "headers": list(node.headers or []),
                "col_highlight_rule": list(node.col_highlight_rule or []),
                "caption": node.caption or "",
            })

    try:
        write_landing_html(spec, html_path, ctx)
    except Exception as e:
        return obs_error(f"landing HTML write failed: {e}", category="api")
    quality_payload = _lint_composite_html(
        html_path,
        artifact_type="landing",
        iter_dir=iter_dir,
    )

    layout_grounding_result = ground_html_layout(
        html_path,
        "body",
        viewport_width=cw,
        viewport_height=900,
    )

    frame_backend = "playwright"
    frame_warnings: list[str] = []
    browser_result = screenshot_html(
        html_path,
        preview_path,
        viewport_width=cw,
        viewport_height=900,
        full_page=True,
    )
    if browser_result.warnings:
        frame_backend = browser_result.backend
        frame_warnings.extend(browser_result.warnings)
        try:
            _write_landing_preview(spec, preview_path, ctx)
        except Exception as e:
            return obs_error(f"landing preview render failed: {e}", category="api")
    else:
        frame_backend = browser_result.backend

    layout_grounding_path, layout_grounding_payload = _persist_layout_grounding(
        iter_dir,
        layout_grounding_result,
    )

    artifacts = CompositionArtifacts(
        psd_path=None,
        svg_path=None,
        html_path=str(html_path),
        html_artifact_path=str(iter_dir / "html_artifact.json"),
        preview_path=str(preview_path),
        layer_manifest=manifest,
    )
    ctx.state["composition"] = artifacts

    section_ct = sum(1 for n in layer_graph if getattr(n, "kind", None) == "section")
    image_ct = sum(
        1 for sec in layer_graph
        for c in (getattr(sec, "children", None) or [])
        if getattr(c, "kind", None) == "image" and getattr(c, "src_path", None)
    )
    log("composite.landing.done",
        html=str(html_path), preview=str(preview_path),
        sections=section_ct, images=image_ct, top_level=len(layer_graph),
        quality_lint_p0_count=quality_payload["quality_lint_p0_count"])

    final_files = ["index.html", "preview.png"]
    _refresh_final_links(iter_dir, ctx, final_files)
    preview_sha = sha256_file(preview_path)
    payload: dict[str, Any] = {
        "artifact_type": "landing",
        "iteration": iter_num,
        "spec_revision": int(ctx.state.get("spec_revision_count") or 0),
        "preview_sha256": preview_sha,
        "html_sha256": sha256_file(html_path),
        "n_sections": section_ct,
        "n_images": image_ct,
        "canvas_width_px": cw,
        "preview_relative_path": f"composites/iter_{iter_num:02d}/preview.png",
        "html_relative_path": f"composites/iter_{iter_num:02d}/index.html",
        "frame_render_backend": frame_backend,
        "frame_render_warnings": frame_warnings,
        **quality_payload,
        **layout_grounding_payload,
        "layout_grounding_repairs": [],
        "layout_grounding_relative_path": (
            f"composites/iter_{iter_num:02d}/{layout_grounding_path.name}"
            if layout_grounding_path else None
        ),
    }
    if prior_preview_sha:
        payload["supersedes_preview_sha256"] = prior_preview_sha
    payload = _attach_html_artifact_contract(
        payload,
        spec=spec,
        iter_dir=iter_dir,
        iter_num=iter_num,
    )
    payload = _attach_design_feedback(
        payload,
        iter_dir=iter_dir,
        iter_num=iter_num,
        ctx=ctx,
    )
    ctx.state["last_composite_payload"] = payload
    _mark_visual_reference_revision_composited(ctx)
    return obs_ok(payload)


def _composite_deck(spec: Any, ctx: ToolContext) -> ToolResultRecord:
    """HTML-first deck composite by default.

    Explicit legacy `deck_export_mode` values keep the old PPTX path available
    for compatibility, but new decks should leave the mode as `"html"`.
    """
    mode = (getattr(spec, "deck_export_mode", None) or "html").lower()
    if mode in {"hybrid", "visual", "editable"}:
        return _composite_deck_legacy_pptx(spec, ctx)
    return _composite_deck_html_first(spec, ctx)


def _composite_deck_html_first(spec: Any, ctx: ToolContext) -> ToolResultRecord:
    """HTML/PDF-primary deck composite.

    Writes:
      - deck.html — authoritative editable HTML deck
      - slides/slide_<i>.png — per-slide browser screenshots
      - preview.png — grid thumbnail for chat/cards
      - deck.pdf — shareable browser/Pillow PDF export
    """
    layer_graph = list(getattr(spec, "layer_graph", None) or [])
    slides = [n for n in layer_graph if getattr(n, "kind", None) == "slide"]
    deck_html_spec = getattr(spec, "deck_html", None)
    if deck_html_spec is None and not slides:
        return obs_error(
            "deck design_spec has no deck_html slides or legacy kind=\"slide\" nodes",
            category="validation",
        )

    if slides:
        from ..util.section_renumber import apply_section_policy
        policy = getattr(ctx.settings, "section_number_policy", "renumber")
        renumbered = apply_section_policy(slides, policy)
        rebuilt: list[Any] = []
        slide_iter = iter(renumbered)
        for node in layer_graph:
            if getattr(node, "kind", None) == "slide":
                rebuilt.append(next(slide_iter))
            else:
                rebuilt.append(node)
        spec.layer_graph = rebuilt
        slides = renumbered
        _hydrate_deck_image_srcs(slides, ctx)

    iter_dir, iter_num = _open_iter_dir(ctx)
    prior_preview_sha = _prior_preview_sha(ctx)
    deck_html_path = iter_dir / "deck.html"
    pdf_path = iter_dir / "deck.pdf"
    slides_dir = iter_dir / "slides"
    slides_dir.mkdir(parents=True, exist_ok=True)
    preview_path = iter_dir / "preview.png"

    canvas = spec.canvas or {}
    slide_w = int(canvas.get("w_px") or 1920)
    slide_h = int(canvas.get("h_px") or 1080)

    try:
        render_result = write_html_first_deck(spec, deck_html_path, ctx)
    except Exception as e:
        return obs_error(f"deck HTML write failed: {e}", category="api")

    quality_payload = _lint_composite_html(
        deck_html_path,
        artifact_type="deck",
        iter_dir=iter_dir,
    )
    # The generic HTML grounder assumes every frame is visible in a single
    # viewport. HTML decks stack slides vertically, so running it over all
    # `.deck-slide` nodes creates false blocker findings for offscreen slides.
    # Deck-specific geometry is covered by screenshots plus
    # `audit_deck_html_layout` below.
    layout_grounding_result: LayoutGroundingResult | None = None

    frame_warnings: list[str] = []
    frame_result = screenshot_deck_slides(
        deck_html_path,
        slides_dir,
        slide_w=slide_w,
        slide_h=slide_h,
    )
    frame_backend = frame_result.backend
    slide_pngs: list[Path] = list(frame_result.paths)
    if frame_result.warnings:
        frame_warnings.extend(frame_result.warnings)
        # Browser screenshots failed. If legacy slide nodes exist, render a
        # simplified preview; otherwise create plain slide screenshots so PDF
        # and final links still complete deterministically.
        slide_pngs = []
        if slides:
            for idx, slide_node in enumerate(slides):
                png_path = slides_dir / f"slide_{idx:02d}.png"
                try:
                    render_slide_preview_png(
                        slide_node, slide_w, slide_h, png_path, ctx,
                        hide_editable=False,
                    )
                    slide_pngs.append(png_path)
                except Exception as e:
                    return obs_error(f"slide {idx} preview render failed: {e}", category="api")
        else:
            for idx in range(render_result.slide_count):
                png_path = slides_dir / f"slide_{idx:02d}.png"
                Image.new("RGB", (slide_w, slide_h), (250, 247, 240)).save(png_path)
                slide_pngs.append(png_path)

    try:
        build_deck_preview_grid(slide_pngs, preview_path)
    except Exception as e:
        return obs_error(f"deck preview grid failed: {e}", category="api")

    pdf_result = export_deck_pdf(
        deck_html_path,
        pdf_path,
        slide_w=slide_w,
        slide_h=slide_h,
        slide_pngs=slide_pngs,
    )
    pdf_warnings = list(pdf_result.warnings)
    if not pdf_path.exists():
        return obs_error(
            f"deck PDF export failed: {'; '.join(pdf_warnings) or 'unknown'}",
            category="api",
        )

    deck_layout_findings = audit_deck_html_layout(
        render_result,
        slide_w=slide_w,
        slide_h=slide_h,
    )
    deck_layout_p0_count = sum(1 for f in deck_layout_findings if f.get("severity") == "P0")

    manifest: list[dict[str, Any]] = []
    by_slide: dict[str, list[Any]] = {}
    for p in render_result.placements:
        by_slide.setdefault(p.slide_id, []).append(p)
    for idx, (slide_id, placements) in enumerate(by_slide.items()):
        manifest.append({
            "layer_id": slide_id,
            "name": slide_id,
            "kind": "slide",
            "index": idx,
            "children": [
                {
                    "layer_id": p.block_id,
                    "name": p.role,
                    "kind": p.kind,
                    "text": p.text,
                    "src_path": p.src_path,
                    "font_family": p.font_family,
                    "font_size_px": p.font_size_px,
                    "font_weight": p.font_weight,
                    "font_style": p.font_style,
                    "line_height": p.line_height,
                    "letter_spacing": p.letter_spacing,
                    "text_transform": p.text_transform,
                }
                for p in placements
            ],
        })

    artifacts = CompositionArtifacts(
        psd_path=None,
        svg_path=None,
        html_path=str(deck_html_path),
        deck_html_path=str(deck_html_path),
        html_artifact_path=str(iter_dir / "html_artifact.json"),
        pdf_path=str(pdf_path),
        pptx_path=None,
        preview_path=str(preview_path),
        layer_manifest=manifest,
    )
    ctx.state["composition"] = artifacts

    image_ct = sum(1 for p in render_result.placements if p.kind == "image" and p.src_path)
    log("composite.deck.html_first.done",
        html=str(deck_html_path), pdf=str(pdf_path), preview=str(preview_path),
        slides=render_result.slide_count, images=image_ct,
        frame_backend=frame_backend,
        frame_warnings=len(frame_warnings),
        pdf_backend=pdf_result.backend,
        pdf_warnings=len(pdf_warnings),
        layout_grounding_issues=0,
        deck_layout_p0_count=deck_layout_p0_count,
        quality_lint_p0_count=quality_payload["quality_lint_p0_count"])

    _refresh_final_links(iter_dir, ctx, ["deck.html", "deck.pdf", "preview.png"])
    layout_grounding_path, layout_grounding_payload = _persist_layout_grounding(
        iter_dir,
        layout_grounding_result,
    )
    preview_sha = sha256_file(preview_path)
    payload: dict[str, Any] = {
        "artifact_type": "deck",
        "iteration": iter_num,
        "spec_revision": int(ctx.state.get("spec_revision_count") or 0),
        "preview_sha256": preview_sha,
        "html_sha256": sha256_file(deck_html_path),
        "pdf_sha256": sha256_file(pdf_path),
        "n_slides": render_result.slide_count,
        "n_images": image_ct,
        "canvas": {"w_px": slide_w, "h_px": slide_h},
        "preview_relative_path": f"composites/iter_{iter_num:02d}/preview.png",
        "html_relative_path": f"composites/iter_{iter_num:02d}/deck.html",
        "pdf_relative_path": f"composites/iter_{iter_num:02d}/deck.pdf",
        "slide_preview_paths": [
            f"composites/iter_{iter_num:02d}/slides/{p.name}" for p in slide_pngs
        ],
        "frame_render_backend": frame_backend,
        "frame_render_warnings": frame_warnings,
        "pdf_render_backend": pdf_result.backend,
        "pdf_render_warnings": pdf_warnings,
        "deck_export_mode": "html",
        "deck_html_layout_stats": render_result.stats,
        "deck_layout_findings": deck_layout_findings,
        "deck_layout_p0_count": deck_layout_p0_count,
        **quality_payload,
        **layout_grounding_payload,
        "layout_grounding_repairs": [],
        "layout_grounding_relative_path": (
            f"composites/iter_{iter_num:02d}/{layout_grounding_path.name}"
            if layout_grounding_path else None
        ),
        # These legacy deck feedback channels remain in the payload shape as
        # empty lists so planner repair prompts and older tests degrade cleanly.
        "text_overlap_warnings": [],
        "orphan_callout_warnings": [],
        "sanitizer_warnings": [],
        "alignment_warnings": [],
        "closing_warnings": [],
    }
    if prior_preview_sha:
        payload["supersedes_preview_sha256"] = prior_preview_sha
    payload = _attach_html_artifact_contract(
        payload,
        spec=spec,
        iter_dir=iter_dir,
        iter_num=iter_num,
    )
    _mark_visual_reference_revision_composited(ctx)
    payload = _attach_design_feedback(
        payload,
        iter_dir=iter_dir,
        iter_num=iter_num,
        ctx=ctx,
    )
    ctx.state["last_composite_payload"] = payload
    return obs_ok(payload)


def _composite_deck_legacy_pptx(spec: Any, ctx: ToolContext) -> ToolResultRecord:
    """PPTX-primary deck composite. Reads the slide tree from
    design_spec.layer_graph (top-level `kind="slide"` nodes). Writes:
      - deck.pptx — native PowerPoint file (editable TextFrames)
      - slides/slide_<i>.png — per-slide Pillow preview thumbs
      - preview.png — grid thumb of the slides (for chat UX + critic)
    """
    layer_graph = list(spec.layer_graph or [])
    slides = [n for n in layer_graph if getattr(n, "kind", None) == "slide"]
    if not slides:
        return obs_error(
            "deck design_spec has no slides — propose_design_spec with a "
            "layer_graph containing at least one kind=\"slide\" node first",
            category="validation",
        )

    # v2.7.2 — apply section_number policy BEFORE hydration / write so the
    # renderer sees a consistent, monotonic numbering. `apply_section_policy`
    # is pure: it returns new LayerNode copies without mutating the spec.
    # Splice the post-policy slides back into the spec's layer_graph in the
    # same positions so write_pptx walks the renumbered nodes. Non-slide
    # entries pass through untouched.
    from ..util.section_renumber import apply_section_policy
    policy = getattr(ctx.settings, "section_number_policy", "renumber")
    renumbered = apply_section_policy(slides, policy)
    rebuilt: list[Any] = []
    slide_iter = iter(renumbered)
    for node in layer_graph:
        if getattr(node, "kind", None) == "slide":
            rebuilt.append(next(slide_iter))
        else:
            rebuilt.append(node)
    spec.layer_graph = rebuilt
    slides = renumbered

    # Hydrate inline images inside slides (same pattern as landing — planner
    # may declare image children separately and call generate_image later).
    _hydrate_deck_image_srcs(slides, ctx)

    iter_dir, iter_num = _open_iter_dir(ctx)
    prior_preview_sha = _prior_preview_sha(ctx)
    pptx_path = iter_dir / "deck.pptx"
    deck_html_path = iter_dir / "deck.html"
    slides_dir = iter_dir / "slides"
    slides_dir.mkdir(parents=True, exist_ok=True)
    base_slides_dir = iter_dir / "slides_base"
    base_slides_dir.mkdir(parents=True, exist_ok=True)
    preview_path = iter_dir / "preview.png"

    canvas = spec.canvas or {}
    slide_w = int(canvas.get("w_px") or 1920)
    slide_h = int(canvas.get("h_px") or 1080)

    # v2.7 — provenance audit BEFORE write_pptx. When the deck has an
    # ingested paper source, every body text layer carrying a numeric
    # token must have an `evidence_quote` matching the ingest text.
    # Strict mode: replace unverified numbers with [?] markers. Empty
    # ingest list → no-op (free-text decks unaffected). Report persisted
    # alongside artifacts for human inspection.
    pv_failures = 0
    if ctx.state.get("ingested"):
        from ..util.provenance import apply_strict_provenance, validate_provenance
        pv_report = validate_provenance(spec, ctx)
        if pv_report.has_failures():
            pv_failures = len(pv_report.failures)
            n_mut = apply_strict_provenance(spec, pv_report)
            log("composite.deck.provenance_fail",
                n_failures=pv_failures, n_mutated=n_mut,
                failure_ids=[f.layer_id for f in pv_report.failures],
                failure_reasons=[f.reason for f in pv_report.failures])
        else:
            log("composite.deck.provenance_ok",
                n_audited=pv_report.n_text_layers_audited,
                n_with_numbers=pv_report.n_layers_with_numbers)
        atomic_write_json(iter_dir / "provenance_report.json",
                          pv_report.to_dict())

    # v2.7.5 — quarantine orphan callouts BEFORE write_pptx so the
    # renderer never sees a callout pointing at empty space. The
    # renderer also re-checks via `ctx.state["orphan_callouts"]` in
    # pass-2 so the gate holds even if a downstream caller bypasses
    # this composite entry point.
    orphan_callout_ids, orphan_callout_warnings = _detect_orphan_callouts(
        slides, slide_w=slide_w, slide_h=slide_h,
    )
    ctx.state["orphan_callouts"] = orphan_callout_ids

    # v2.8.2-C1 — strip placeholder text + debug-named empty shapes BEFORE
    # write_pptx. The orphan-callout pass above only catches callouts that
    # point at empty space; this pass catches callouts that *do* anchor a
    # real region but never had their label rewritten ("Annotation 12",
    # ``callout_05_a`` with empty text), plus title/body leaks like
    # "Paper Title Goes Here" / "arxiv.org/abs/XXXX". Operates on
    # structural properties only — no per-paper heuristics.
    from ..util.export_sanitizer import sanitize_design_spec
    spec, sanitizer_warnings = sanitize_design_spec(spec)
    ctx.state["sanitizer_warnings"] = sanitizer_warnings
    # Refresh `slides` from the sanitized spec so downstream detectors,
    # write_pptx, and the manifest builder all walk the cleaned tree.
    slides = [n for n in spec.layer_graph if getattr(n, "kind", None) == "slide"]

    # Legacy layer-graph decks can omit bboxes. Resolve their persisted slot
    # geometry so collisions, unanchored text, and text-over-shape errors stay
    # visible to the compatibility export path.
    deck_text_overlaps = _detect_deck_text_overlaps(
        slides, slide_w=slide_w, slide_h=slide_h,
    )

    # v2.8.2-C2 — naive title-body alignment validator. Detects slides where
    # the title makes a promise the body/figure doesn't deliver (e.g. title
    # "Training Stage Ablations" but body shows training config with no
    # ablation data). Set-overlap of noun-phrase tokens; no embeddings, no
    # LLM call. Threshold-based warnings only — planner self-corrects on
    # next iteration. Generalizes across paper / blog / .docx etc.
    from ..util.slide_alignment import detect_alignment_warnings
    alignment_warnings = detect_alignment_warnings(spec)
    ctx.state["alignment_warnings"] = alignment_warnings

    # v2.8.2 C3 — closing-content enforcer. Warn the planner when the last
    # slide is a generic "Thank You" stub (or thin / placeholder
    # content). Detection only — the renderer continues with the existing
    # spec; the warning surfaces in the tool_result payload so the planner
    # can populate real takeaways on the next iteration.
    closing_warnings = _detect_closing_stub(spec)
    ctx.state["closing_warnings"] = closing_warnings

    deck_export_mode = (getattr(spec, "deck_export_mode", None) or "hybrid").lower()
    if deck_export_mode not in {"hybrid", "visual", "editable"}:
        deck_export_mode = "hybrid"

    try:
        write_deck_html(spec, deck_html_path, ctx)
    except Exception as e:
        return obs_error(f"deck HTML write failed: {e}", category="api")
    quality_payload = _lint_composite_html(
        deck_html_path,
        artifact_type="deck",
        iter_dir=iter_dir,
    )

    layout_grounding_result = ground_html_layout(
        deck_html_path,
        ".deck-slide",
        viewport_width=slide_w,
        viewport_height=slide_h,
    )

    slide_pngs: list[Path] = []
    frame_warnings: list[str] = []
    frame_result = screenshot_deck_slides(
        deck_html_path,
        slides_dir,
        slide_w=slide_w,
        slide_h=slide_h,
    )
    frame_backend = frame_result.backend
    if frame_result.warnings:
        frame_warnings.extend(frame_result.warnings)
        for idx, slide_node in enumerate(slides):
            png_path = slides_dir / f"slide_{idx:02d}.png"
            try:
                render_slide_preview_png(
                    slide_node, slide_w, slide_h, png_path, ctx,
                    hide_editable=False,
                )
                slide_pngs.append(png_path)
            except Exception as e:
                return obs_error(f"slide {idx} preview render failed: {e}", category="api")
    else:
        slide_pngs = list(frame_result.paths)

    base_slide_pngs: list[Path] = []
    if deck_export_mode == "hybrid":
        base_result = screenshot_deck_slides(
            deck_html_path,
            base_slides_dir,
            slide_w=slide_w,
            slide_h=slide_h,
            hide_selector=".od-editable",
        )
        if base_result.warnings:
            frame_warnings.extend(base_result.warnings)
            frame_backend = (
                "pillow-fallback" if frame_backend != "playwright"
                else "mixed-playwright-pillow"
            )
            for idx, slide_node in enumerate(slides):
                png_path = base_slides_dir / f"slide_{idx:02d}.png"
                try:
                    render_slide_preview_png(
                        slide_node, slide_w, slide_h, png_path, ctx,
                        hide_editable=True,
                    )
                    base_slide_pngs.append(png_path)
                except Exception as e:
                    return obs_error(f"slide {idx} base render failed: {e}", category="api")
        else:
            base_slide_pngs = list(base_result.paths)
    elif deck_export_mode == "visual":
        base_slide_pngs = slide_pngs

    try:
        if deck_export_mode == "editable":
            slide_count = write_pptx(spec, pptx_path, ctx)
        else:
            slide_count = write_pptx_hybrid(
                spec,
                pptx_path,
                ctx,
                slide_pngs=base_slide_pngs or slide_pngs,
                export_mode=deck_export_mode,
            )
    except Exception as e:
        return obs_error(f"PPTX write failed: {e}", category="api")

    # Post-write scrubber catches residual placeholder strings in the emitted
    # PPTX XML that the spec-level sanitizer cannot observe.
    from ..util.export_sanitizer import sanitize_pptx_file
    pptx_scrub_warnings = sanitize_pptx_file(pptx_path)
    sanitizer_warnings = sanitizer_warnings + pptx_scrub_warnings
    ctx.state["sanitizer_warnings"] = sanitizer_warnings

    try:
        build_deck_preview_grid(slide_pngs, preview_path)
    except Exception as e:
        return obs_error(f"deck preview grid failed: {e}", category="api")

    manifest: list[dict[str, Any]] = []
    for idx, slide_node in enumerate(slides):
        entry = {
            "layer_id": slide_node.layer_id,
            "name": slide_node.name,
            "kind": "slide",
            "index": idx,
            "children": [
                {
                    "layer_id": c.layer_id,
                    "name": c.name,
                    "kind": c.kind,
                    "text": getattr(c, "text", None),
                    "src_path": getattr(c, "src_path", None),
                }
                for c in (slide_node.children or [])
            ],
        }
        manifest.append(entry)

    artifacts = CompositionArtifacts(
        psd_path=None,
        svg_path=None,
        html_path=None,
        deck_html_path=str(deck_html_path),
        html_artifact_path=str(iter_dir / "html_artifact.json"),
        pptx_path=str(pptx_path),
        preview_path=str(preview_path),
        layer_manifest=manifest,
    )
    ctx.state["composition"] = artifacts

    image_ct = sum(
        1 for s in slides
        for c in (getattr(s, "children", None) or [])
        if getattr(c, "kind", None) in ("image", "background")
        and getattr(c, "src_path", None)
    )
    log("composite.deck.done",
        pptx=str(pptx_path), html=str(deck_html_path), preview=str(preview_path),
        slides=slide_count, images=image_ct,
        deck_export_mode=deck_export_mode,
        frame_backend=frame_backend,
        frame_warnings=len(frame_warnings),
        layout_grounding_issues=len(layout_grounding_result.issues),
        text_overlaps=len(deck_text_overlaps),
        orphan_callouts=len(orphan_callout_warnings),
        alignment_warnings=len(alignment_warnings),
        closing_warnings=len(closing_warnings),
        quality_lint_p0_count=quality_payload["quality_lint_p0_count"])

    _refresh_final_links(iter_dir, ctx, ["deck.pptx", "deck.html", "preview.png"])
    layout_grounding_path, layout_grounding_payload = _persist_layout_grounding(
        iter_dir,
        layout_grounding_result,
    )
    preview_sha = sha256_file(preview_path)
    payload: dict[str, Any] = {
        "artifact_type": "deck",
        "iteration": iter_num,
        "spec_revision": int(ctx.state.get("spec_revision_count") or 0),
        "preview_sha256": preview_sha,
        "pptx_sha256": sha256_file(pptx_path),
        "html_sha256": sha256_file(deck_html_path),
        "n_slides": slide_count,
        "n_images": image_ct,
        "canvas": {"w_px": slide_w, "h_px": slide_h},
        "preview_relative_path": f"composites/iter_{iter_num:02d}/preview.png",
        "pptx_relative_path": f"composites/iter_{iter_num:02d}/deck.pptx",
        "html_relative_path": f"composites/iter_{iter_num:02d}/deck.html",
        "frame_render_backend": frame_backend,
        "frame_render_warnings": frame_warnings,
        **quality_payload,
        **layout_grounding_payload,
        "layout_grounding_repairs": [],
        "layout_grounding_relative_path": (
            f"composites/iter_{iter_num:02d}/{layout_grounding_path.name}"
            if layout_grounding_path else None
        ),
        "deck_export_mode": deck_export_mode,
        # v2.7.5 — real environment signals the planner reads on the
        # next turn. Empty lists mean a clean render; non-empty means
        # the planner should fix slot wiring (text overlaps) or drop
        # un-anchorable callouts before re-composing.
        "text_overlap_warnings": deck_text_overlaps,
        "orphan_callout_warnings": orphan_callout_warnings,
        # v2.8.2-C1 — placeholder + debug-named-empty shapes the export
        # sanitizer dropped before the compatibility PPTX write. Empty = clean.
        "sanitizer_warnings": sanitizer_warnings,
        # v2.8.2-C2 — naive set-overlap signal. Slides whose title noun
        # phrases don't appear in the body/figure text. Empty = clean.
        "alignment_warnings": alignment_warnings,
        # v2.8.2 C3 — empty list means the closing slide carries real
        # takeaways; non-empty means the planner left a generic closing stub
        # ("Thank You" / "Q&A") and should populate it on the next pass.
        "closing_warnings": closing_warnings,
    }
    if prior_preview_sha:
        payload["supersedes_preview_sha256"] = prior_preview_sha
    payload = _attach_html_artifact_contract(
        payload,
        spec=spec,
        iter_dir=iter_dir,
        iter_num=iter_num,
    )
    payload = _attach_design_feedback(
        payload,
        iter_dir=iter_dir,
        iter_num=iter_num,
        ctx=ctx,
    )
    ctx.state["last_composite_payload"] = payload
    _mark_visual_reference_revision_composited(ctx)
    return obs_ok(payload)


def _mark_visual_reference_revision_composited(ctx: ToolContext) -> None:
    target = ctx.state.get("visual_reference_revision_spec_revision")
    if target is None:
        return
    current = int(ctx.state.get("spec_revision_count") or 0)
    if current >= int(target):
        ctx.state["visual_reference_revision_composited"] = True


def _hydrate_poster_layer_bboxes(rendered: dict[str, dict[str, Any]],
                                 spec: Any) -> None:
    """Copy bbox from spec.layer_graph onto rendered_layers records that
    lack one — poster-specific companion to the landing/deck hydration.

    Ingested PDF figures (v1.1 paper2any) register with bbox=None since they
    have no intrinsic placement — the planner chooses where to put each
    figure on the poster canvas by giving it a bbox inside its
    `propose_design_spec` call. Without this hydration, the poster PSD/SVG
    writers crash on `None["x"]`.

    The spec is authoritative for placement; rendered_layers is authoritative
    for content. We merge by layer_id.
    """
    for node in (spec.layer_graph or []):
        nb = getattr(node, "bbox", None)
        if nb is None:
            continue
        lid = getattr(node, "layer_id", None)
        if lid is None or lid not in rendered:
            continue
        rec = rendered[lid]
        if rec.get("bbox"):
            continue
        try:
            bbox_dict = {"x": int(nb.x), "y": int(nb.y),
                         "w": int(nb.w), "h": int(nb.h)}
            if nb.purpose is not None:
                bbox_dict["purpose"] = nb.purpose
        except AttributeError:
            continue
        rec["bbox"] = bbox_dict
        # Promote z_index from spec if rendered record didn't have one.
        if "z_index" in rec and rec["z_index"] == 0:
            spec_z = getattr(node, "z_index", None)
            if spec_z is not None:
                rec["z_index"] = int(spec_z)


def _hydrate_deck_image_srcs(slides: list[Any], ctx: ToolContext) -> None:
    """Copy src_path from rendered_layers onto each slide's image/background
    children. Mirrors `_hydrate_landing_image_srcs` — see that docstring.
    """
    rendered = ctx.state.get("rendered_layers") or {}
    if not rendered:
        return
    for slide in slides:
        children = list(getattr(slide, "children", None) or [])
        new_children: list[Any] = []
        changed = False
        for child in children:
            kind = getattr(child, "kind", None)
            if kind not in ("image", "background", "table"):
                new_children.append(child)
                continue
            # Tables carry structured rows/headers too — hydrate those
            # alongside src_path, same pattern as image aspect_ratio.
            needs_src = not getattr(child, "src_path", None)
            needs_rows = (kind == "table"
                          and not (getattr(child, "rows", None)
                                   or getattr(child, "headers", None)))
            if not needs_src and not needs_rows:
                new_children.append(child)
                continue
            rec = rendered.get(getattr(child, "layer_id", None))
            if rec and rec.get("src_path"):
                updates: dict[str, Any] = {}
                if needs_src:
                    updates["src_path"] = rec["src_path"]
                    updates["aspect_ratio"] = (rec.get("aspect_ratio")
                                               or getattr(child, "aspect_ratio", None))
                if kind == "table":
                    updates.setdefault("rows", rec.get("rows") or [])
                    updates.setdefault("headers", rec.get("headers") or [])
                    updates.setdefault("col_highlight_rule",
                                       rec.get("col_highlight_rule") or [])
                    if rec.get("caption"):
                        updates["caption"] = rec["caption"]
                try:
                    new_child = child.model_copy(update=updates)
                    new_children.append(new_child)
                    changed = True
                except Exception:
                    for k, v in updates.items():
                        setattr(child, k, v)
                    new_children.append(child)
            else:
                new_children.append(child)
        if changed:
            slide.children = new_children


def _hydrate_landing_image_srcs(layer_graph: list[Any], ctx: ToolContext) -> None:
    """Copy `src_path` from ctx.state['rendered_layers'] onto matching image
    children in the spec's layer_graph — so write_landing_html's data-URI
    embedding finds a real file.

    The planner typically declares the section tree in propose_design_spec
    with children having the intended `layer_id`, then separately invokes
    generate_image(layer_id=...) which puts the PNG + src_path into
    rendered_layers. Without this hydration step, the children nodes have
    no src_path and the renderer would silently skip them.
    """
    rendered = ctx.state.get("rendered_layers") or {}
    if not rendered:
        return
    for section in layer_graph:
        if getattr(section, "kind", None) != "section":
            continue
        children = list(getattr(section, "children", None) or [])
        changed = False
        new_children: list[Any] = []
        for child in children:
            kind = getattr(child, "kind", None)
            if kind not in ("image", "table"):
                new_children.append(child)
                continue
            needs_src = not getattr(child, "src_path", None)
            needs_rows = (kind == "table"
                          and not (getattr(child, "rows", None)
                                   or getattr(child, "headers", None)))
            if not needs_src and not needs_rows:
                new_children.append(child)
                continue  # already has src_path (+ rows for tables)
            rec = rendered.get(getattr(child, "layer_id", None))
            if rec and rec.get("src_path"):
                updates: dict[str, Any] = {}
                if needs_src:
                    updates["src_path"] = rec["src_path"]
                    updates["aspect_ratio"] = (rec.get("aspect_ratio")
                                               or child.aspect_ratio)
                if kind == "table":
                    updates.setdefault("rows", rec.get("rows") or [])
                    updates.setdefault("headers", rec.get("headers") or [])
                    updates.setdefault("col_highlight_rule",
                                       rec.get("col_highlight_rule") or [])
                    if rec.get("caption"):
                        updates["caption"] = rec["caption"]
                try:
                    new_child = child.model_copy(update=updates)
                    new_children.append(new_child)
                    changed = True
                except Exception:
                    for k, v in updates.items():
                        setattr(child, k, v)
                    new_children.append(child)
            else:
                new_children.append(child)
        if changed:
            section.children = new_children


def _write_landing_preview(spec: Any, out_path: Path, ctx: ToolContext) -> None:
    """Render a simplified preview PNG for a landing page — a stacked
    top-down rasterization of each section's headline + subhead.

    Not pixel-accurate with the HTML; it exists so the run has a preview.png
    for chat UX + critique.
    """
    from PIL import Image, ImageDraw, ImageFont

    canvas = spec.canvas or {}
    w = min(1200, int(canvas.get("w_px", 1200)))
    # Grow vertically with the number of sections so no section gets clipped.
    layer_graph = list(spec.layer_graph or [])
    section_count = max(1, sum(
        1 for n in layer_graph if getattr(n, "kind", None) == "section"
    ))
    h = 280 + 280 * section_count

    img = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    def _font(family: str, size: int) -> ImageFont.FreeTypeFont:
        fonts = ctx.settings.fonts
        fname = fonts.get(family) or fonts[ctx.settings.default_text_font]
        path = ctx.settings.fonts_dir / fname
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                pass
        return ImageFont.load_default(size=size)

    y = 80
    x = 64
    for node in layer_graph:
        kind = getattr(node, "kind", None)
        name = (getattr(node, "name", "") or "").lower()
        variant = next(
            (v for v in ("hero", "features", "cta", "footer", "header") if v in name),
            "content",
        )
        # Section banner row
        if kind == "section":
            # Variant stripe
            band_color = {
                "hero": (15, 23, 42),
                "cta": (15, 23, 42),
                "footer": (15, 23, 42),
                "features": (250, 251, 252),
            }.get(variant, (255, 255, 255))
            text_color = (248, 250, 252) if variant in ("hero", "cta", "footer") else (15, 23, 42)
            section_top = y - 20
            draw.rectangle([(0, section_top), (w, section_top + 220)], fill=band_color)
            # Section tag
            tag = f"§ {variant.upper()}"
            tag_font = _font(ctx.settings.default_text_font, 14)
            draw.text((x, section_top + 12), tag,
                      fill=(248, 250, 252, 200) if variant in ("hero", "cta", "footer") else (148, 163, 184),
                      font=tag_font)
            inner_y = section_top + 52
            for child in (getattr(node, "children", None) or []):
                if getattr(child, "kind", None) != "text":
                    continue
                text = (getattr(child, "text", "") or "")[:80]
                raw_size = int(getattr(child, "font_size_px", None) or 40)
                size = max(16, min(48, raw_size // 2))  # downscale for preview
                fam = getattr(child, "font_family", None) or ctx.settings.default_text_font
                try:
                    f = _font(fam, size)
                except Exception:
                    f = _font(ctx.settings.default_text_font, size)
                draw.text((x, inner_y), text, fill=text_color, font=f)
                inner_y += size + 12
                if inner_y > section_top + 200:
                    break
            y = section_top + 240
        elif kind == "text":
            text = (getattr(node, "text", "") or "")[:80]
            size = max(20, min(60, int(getattr(node, "font_size_px", None) or 48) // 2))
            try:
                f = _font(getattr(node, "font_family", None) or ctx.settings.default_text_font, size)
            except Exception:
                f = _font(ctx.settings.default_text_font, size)
            draw.text((x, y), text, fill=(15, 23, 42), font=f)
            y += size + 20

    img.save(out_path, format="PNG", optimize=True)


def _write_psd(layers: list[dict[str, Any]], cw: int, ch: int,
               out_path: Path, manifest: list[dict[str, Any]],
               ctx: ToolContext) -> None:
    psd = PSDImage.new(mode="RGB", size=(cw, ch), depth=8)

    text_group = None

    for L in layers:
        bbox = L["bbox"]
        bx, by, bw, bh = bbox["x"], bbox["y"], bbox["w"], bbox["h"]

        if L["kind"] == "background":
            png = Image.open(L["src_path"])
            if png.mode != "RGB":
                png = png.convert("RGB")
            if png.size != (cw, ch):
                png = png.resize((cw, ch), Image.LANCZOS)
            psd.create_pixel_layer(
                png, name=L["name"], top=0, left=0,
                opacity=255, blend_mode=BlendMode.NORMAL,
                compression=Compression.RLE,
            )
            manifest.append({
                "layer_id": L["layer_id"], "name": L["name"], "kind": "background",
                "png_path": L["src_path"], "bbox": {"x": 0, "y": 0, "w": cw, "h": ch},
            })
        elif L["kind"] == "table":
            if _is_source_table_crop_layer(L):
                png = Image.open(L["src_path"])
                if png.mode != "RGBA":
                    png = png.convert("RGBA")
                _maybe_warn_aspect(L, png.size, (bx, by, bw, bh))
                nw, nh, off_x, off_y = _aspect_fit_contain(png.size, (bw, bh))
                if (nw, nh) != png.size:
                    png = png.resize((nw, nh), Image.LANCZOS)
                psd.create_pixel_layer(
                    png, name=L["name"], top=by + off_y, left=bx + off_x,
                    opacity=255, blend_mode=BlendMode.NORMAL,
                    compression=Compression.RLE,
                )
                manifest.append({
                    "layer_id": L["layer_id"], "name": L["name"], "kind": "table",
                    "png_path": L["src_path"], "bbox": {"x": bx, "y": by, "w": bw, "h": bh},
                    "table_visual_source": L.get("table_visual_source"),
                })
                continue
            try:
                tmp = ctx.layers_dir / f"table_at_bbox_{L['layer_id']}_psd.png"
                render_table_png(
                    rows=L.get("rows") or [],
                    headers=L.get("headers") or [],
                    out_path=tmp,
                    width_px=bw,
                    max_height_px=bh,
                    col_highlight_rule=L.get("col_highlight_rule") or [],
                    font_path=ctx.settings.fonts_dir / "NotoSansSC-Bold.otf",
                    bold_font_path=ctx.settings.fonts_dir / "NotoSansSC-Bold.otf",
                )
                png = Image.open(tmp).convert("RGBA")
            except Exception:
                png = Image.open(L["src_path"]).convert("RGBA")
            psd.create_pixel_layer(
                png, name=L["name"], top=by, left=bx,
                opacity=255, blend_mode=BlendMode.NORMAL,
                compression=Compression.RLE,
            )
            manifest.append({
                "layer_id": L["layer_id"], "name": L["name"], "kind": "table",
                "png_path": L["src_path"], "bbox": {"x": bx, "y": by, "w": bw, "h": bh},
            })
        elif L["kind"] == "image":
            # v1.2.1: contain-fit instead of stretch. The PSD pixel
            # layer is sized to the fitted image (letterbox inside the
            # planner's bbox); `top`/`left` shifted by the centering
            # offset so the figure doesn't drift off-bbox.
            png = Image.open(L["src_path"])
            if png.mode != "RGBA":
                png = png.convert("RGBA")
            _maybe_warn_aspect(L, png.size, (bx, by, bw, bh))
            nw, nh, off_x, off_y = _aspect_fit_contain(png.size, (bw, bh))
            if (nw, nh) != png.size:
                png = png.resize((nw, nh), Image.LANCZOS)
            psd.create_pixel_layer(
                png, name=L["name"], top=by + off_y, left=bx + off_x,
                opacity=255, blend_mode=BlendMode.NORMAL,
                compression=Compression.RLE,
            )
            manifest.append({
                "layer_id": L["layer_id"], "name": L["name"], "kind": "image",
                "png_path": L["src_path"], "bbox": {"x": bx, "y": by, "w": bw, "h": bh},
            })
        elif L["kind"] == "shape":
            shape = _shape_image(bw, bh, L)
            psd.create_pixel_layer(
                shape, name=L["name"], top=by, left=bx,
                opacity=255, blend_mode=BlendMode.NORMAL,
                compression=Compression.RLE,
            )
            manifest.append({
                "layer_id": L["layer_id"], "name": L["name"], "kind": "shape",
                "bbox": {"x": bx, "y": by, "w": bw, "h": bh},
                "fill": L.get("fill"),
                "stroke": L.get("stroke"),
            })
        else:
            # text layer — render_text_layer produces a full-canvas transparent
            # RGBA with glyphs inside bbox, so we crop by bbox then place.
            png = Image.open(L["src_path"])
            if png.mode != "RGBA":
                png = png.convert("RGBA")
            crop = png.crop((bx, by, bx + bw, by + bh))
            if text_group is None:
                text_group = psd.create_group(name="text", open_folder=True)
            layer = psd.create_pixel_layer(
                crop, name=L["name"], top=by, left=bx,
                opacity=255, blend_mode=BlendMode.NORMAL,
                compression=Compression.RLE,
            )
            text_group.append(layer)
            manifest.append({
                "layer_id": L["layer_id"], "name": L["name"], "kind": L["kind"],
                "png_path": L["src_path"], "bbox": {"x": bx, "y": by, "w": bw, "h": bh},
                "text": L.get("text"),
                "font_family": L.get("font_family"),
                "font_size_px": L.get("font_size_px"),
                "font_weight": L.get("font_weight"),
                "font_style": L.get("font_style"),
                "line_height": L.get("line_height"),
                "letter_spacing": L.get("letter_spacing"),
                "text_transform": L.get("text_transform"),
                "align": L.get("align"),
                "fill": L.get("fill"),
            })

    psd.save(str(out_path))


def _write_svg(layers: list[dict[str, Any]], cw: int, ch: int,
               out_path: Path, ctx: ToolContext) -> None:
    text_layers = [L for L in layers if L["kind"] == "text" and L.get("text")]
    bg_layers = [L for L in layers if L["kind"] == "background"]
    shape_layers = [L for L in layers if L["kind"] == "shape"]
    image_layers = [L for L in layers if L["kind"] == "image"]
    table_layers = [L for L in layers if L["kind"] == "table"]

    fonts_used: dict[str, set[str]] = {}
    for L in text_layers:
        family = L.get("font_family") or ctx.settings.default_text_font
        fonts_used.setdefault(family, set()).update(L["text"])

    font_face_css = build_font_face_css(fonts_used, ctx)

    dwg = svgwrite.Drawing(str(out_path), size=(cw, ch))
    dwg.viewbox(0, 0, cw, ch)

    if font_face_css:
        style = dwg.style(content=font_face_css)
        defs = dwg.defs
        defs.add(style)

    for L in bg_layers:
        with open(L["src_path"], "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        dwg.add(dwg.image(
            href=f"data:image/png;base64,{b64}",
            insert=(0, 0), size=(cw, ch),
        ))

    for L in sorted(shape_layers, key=lambda x: int(x.get("z_index", 0))):
        bbox = L["bbox"]
        bx, by, bw, bh = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
        fill = L.get("fill") or "none"
        stroke = L.get("stroke") or "none"
        stroke_width = int(L.get("stroke_width") or 0)
        dwg.add(dwg.rect(
            insert=(bx, by),
            size=(bw, bh),
            rx=int(L.get("radius") or 0),
            ry=int(L.get("radius") or 0),
            fill="none" if str(fill).lower() == "transparent" else fill,
            stroke="none" if str(stroke).lower() == "transparent" or stroke_width <= 0 else stroke,
            stroke_width=stroke_width,
        ))

    # v1.1 paper2any: emit ingested/passthrough images as <image> elements
    # positioned by bbox, ordered by z_index so they layer correctly with text.
    # v1.2.1: preserveAspectRatio="xMidYMid meet" = SVG's letterbox — the
    # renderer scales the image into the bbox without stretching, centered.
    for L in sorted(image_layers, key=lambda x: int(x.get("z_index", 0))):
        bbox = L["bbox"]
        bx, by, bw, bh = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
        with open(L["src_path"], "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        dwg.add(dwg.image(
            href=f"data:image/png;base64,{b64}",
            insert=(bx, by), size=(bw, bh),
            preserveAspectRatio="xMidYMid meet",
        ))

    # v1.2.1: table layers. Re-render at the planner's bbox so the
    # SVG-embedded PNG is font-autoscaled rather than post-squished.
    # preserveAspectRatio is still set so viewers (Illustrator / Inkscape /
    # browsers) letterbox if the embedded PNG doesn't exactly fill bbox.
    for L in sorted(table_layers, key=lambda x: int(x.get("z_index", 0))):
        bbox = L["bbox"]
        bx, by, bw, bh = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
        src_path = L["src_path"]
        if not _is_source_table_crop_layer(L):
            try:
                tmp = ctx.layers_dir / f"table_at_bbox_{L['layer_id']}_svg.png"
                render_table_png(
                    rows=L.get("rows") or [],
                    headers=L.get("headers") or [],
                    out_path=tmp,
                    width_px=bw,
                    max_height_px=bh,
                    col_highlight_rule=L.get("col_highlight_rule") or [],
                    font_path=ctx.settings.fonts_dir / "NotoSansSC-Bold.otf",
                    bold_font_path=ctx.settings.fonts_dir / "NotoSansSC-Bold.otf",
                )
                src_path = str(tmp)
            except Exception:
                pass
        with open(src_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        dwg.add(dwg.image(
            href=f"data:image/png;base64,{b64}",
            insert=(bx, by), size=(bw, bh),
            preserveAspectRatio="xMidYMid meet",
        ))

    for L in text_layers:
        bbox = L["bbox"]
        bx, by, bw, bh = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
        font_size = int(L["font_size_px"])
        align = L.get("align") or "left"
        anchor = {"left": "start", "center": "middle", "right": "end"}[align]
        if align == "center":
            tx = bx + bw // 2
        elif align == "right":
            tx = bx + bw
        else:
            tx = bx
        ty = by + font_size  # top-of-em ≈ baseline shifted down by font_size

        family = L.get("font_family") or ctx.settings.default_text_font
        transform = _text_transform_value(L.get("text_transform"))
        text = str(L["text"]).upper() if transform == "uppercase" else L["text"]
        attrs = {
            "insert": (tx, ty),
            "font_family": f"'{family}'",
            "font_size": font_size,
            "fill": L.get("fill", "#000000"),
            "text_anchor": anchor,
            "style": (
                f"font-weight:{_text_font_weight(L.get('font_weight'), family)};"
                f"font-style:{_text_font_style(L.get('font_style'))};"
                f"letter-spacing:{_safe_float(L.get('letter_spacing'), 0.0):g}px"
            ),
        }
        effects = L.get("effects") or {}
        stroke = effects.get("stroke") or {}
        if stroke.get("width", 0):
            attrs["stroke"] = stroke.get("color", "#000000")
            attrs["stroke_width"] = int(stroke["width"])
        dwg.add(dwg.text(text, **attrs))

    dwg.save(pretty=True)


def _shape_image(width: int, height: int, layer: dict[str, Any]) -> Image.Image:
    img = Image.new("RGBA", (max(1, width), max(1, height)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    fill = _rgba_or_none(layer.get("fill"))
    stroke = _rgba_or_none(layer.get("stroke"))
    stroke_width = max(0, int(layer.get("stroke_width") or 0))
    radius = max(0, int(layer.get("radius") or 0))
    rect = [0, 0, max(0, width - 1), max(0, height - 1)]
    if radius > 0:
        draw.rounded_rectangle(rect, radius=radius, fill=fill, outline=stroke, width=stroke_width or 1)
    else:
        draw.rectangle(rect, fill=fill, outline=stroke, width=stroke_width or 1)
    return img


def _rgba_or_none(value: Any) -> tuple[int, int, int, int] | None:
    raw = str(value or "").strip()
    if not raw or raw.lower() == "transparent":
        return None
    if raw.startswith("#"):
        raw = raw[1:]
        try:
            if len(raw) == 3:
                r, g, b = [int(ch * 2, 16) for ch in raw]
                return (r, g, b, 255)
            if len(raw) == 6:
                return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16), 255)
            if len(raw) == 8:
                return (
                    int(raw[0:2], 16),
                    int(raw[2:4], 16),
                    int(raw[4:6], 16),
                    int(raw[6:8], 16),
                )
        except ValueError:
            return None
    return None


def _write_preview(layers: list[dict[str, Any]], cw: int, ch: int,
                   out_path: Path, ctx: ToolContext) -> None:
    base = Image.new("RGBA", (cw, ch), (255, 255, 255, 255))
    for L in layers:
        kind = L["kind"]
        if kind == "background":
            png = Image.open(L["src_path"])
            if png.mode != "RGBA":
                png = png.convert("RGBA")
            if png.size != (cw, ch):
                png = png.resize((cw, ch), Image.LANCZOS)
            base = Image.alpha_composite(base, png)
        elif kind == "table":
            bbox = L["bbox"]
            bx, by, bw, bh = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
            if _is_source_table_crop_layer(L):
                png = Image.open(L["src_path"])
                if png.mode != "RGBA":
                    png = png.convert("RGBA")
                _maybe_warn_aspect(L, png.size, (bx, by, bw, bh))
                nw, nh, off_x, off_y = _aspect_fit_contain(png.size, (bw, bh))
                if (nw, nh) != png.size:
                    png = png.resize((nw, nh), Image.LANCZOS)
                full = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
                full.paste(png, (bx + off_x, by + off_y))
                base = Image.alpha_composite(base, full)
                continue
            # v1.2.1: re-render native authored tables at the planner's
            # exact bbox dims so under-sized boxes degrade gracefully.
            try:
                tmp = ctx.layers_dir / f"table_at_bbox_{L['layer_id']}.png"
                render_table_png(
                    rows=L.get("rows") or [],
                    headers=L.get("headers") or [],
                    out_path=tmp,
                    width_px=bw,
                    max_height_px=bh,
                    col_highlight_rule=L.get("col_highlight_rule") or [],
                    font_path=ctx.settings.fonts_dir / "NotoSansSC-Bold.otf",
                    bold_font_path=ctx.settings.fonts_dir / "NotoSansSC-Bold.otf",
                )
                png = Image.open(tmp).convert("RGBA")
            except Exception:
                # Fall back to the pre-baked src_path render if rerender fails.
                png = Image.open(L["src_path"]).convert("RGBA")
            full = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
            # render_table_png may return shorter than bh (rows truncated)
            # or narrower than bw; paste at bbox origin, let it be.
            full.paste(png, (bx, by))
            base = Image.alpha_composite(base, full)
        elif kind == "image":
            # v1.2.1: contain-fit (letterbox) instead of stretching to
            # bbox. Matches HTML's object-fit:contain behavior. A wildly
            # under-sized bbox now leaves whitespace around the figure
            # instead of distorting it.
            png = Image.open(L["src_path"])
            if png.mode != "RGBA":
                png = png.convert("RGBA")
            bbox = L["bbox"]
            bx, by, bw, bh = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
            _maybe_warn_aspect(L, png.size, (bx, by, bw, bh))
            nw, nh, off_x, off_y = _aspect_fit_contain(png.size, (bw, bh))
            if (nw, nh) != png.size:
                png = png.resize((nw, nh), Image.LANCZOS)
            full = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
            full.paste(png, (bx + off_x, by + off_y))
            base = Image.alpha_composite(base, full)
        elif kind == "shape":
            bbox = L["bbox"]
            bx, by, bw, bh = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
            full = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
            full.alpha_composite(_shape_image(bw, bh, L), (bx, by))
            base = Image.alpha_composite(base, full)
        else:
            # text layer: already full-canvas transparent RGBA with glyphs
            # positioned inside bbox.
            png = Image.open(L["src_path"])
            if png.mode != "RGBA":
                png = png.convert("RGBA")
            if png.size != (cw, ch):
                full = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
                full.paste(png, (0, 0))
                png = full
            base = Image.alpha_composite(base, png)
    base.convert("RGB").save(out_path, format="PNG", optimize=True)
