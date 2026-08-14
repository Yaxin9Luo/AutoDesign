"""propose_paper_poster_html - HTML-first authoring bridge for paper posters.

The planner writes constrained poster HTML/CSS directly. This tool treats that
DOM as the source of truth, infers editable HtmlBlock records, measures real
browser bboxes when possible, then delegates to propose_design_spec so the
existing authored_html renderer and harness remain authoritative.
"""

from __future__ import annotations

import colorsys
import json
import math
import os
import re
from html import escape
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from PIL import Image, ImageColor, ImageDraw, ImageFont

from ._contract import ToolContext, obs_error
from .propose_design_spec import propose_design_spec
from ..config import effective_poster_harness_mode, resolve_template
from ..schema import ToolResultRecord
from ..util.css_declaration_transform import (
    transform_declaration_list_values,
    transform_stylesheet_declaration_values,
)
from ..util.logging import log
from ..util.math_typesetting import (
    has_tex_math,
    inline_katex_bundle,
    lint_tex_math_source,
    wait_for_autodesign_math,
)

try:  # Worker A helper; keep fallback behavior small if it is absent.
    from ..util.academic_palette import (
        academic_color_system_from_palette_id,
        active_academic_color_system,
        select_academic_color_system,
    )
except Exception:  # pragma: no cover - supports partially landed worktrees
    academic_color_system_from_palette_id = None
    active_academic_color_system = None
    select_academic_color_system = None


_VALID_BLOCK_KINDS = {
    "text", "image", "table", "metric", "quote", "shape", "caption",
    "chart", "embed", "group",
}
_TEXT_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol",
    "small", "span", "code", "kbd", "samp", "var", "mark", "abbr",
}
_INLINE_TEXT_TAGS = {"span", "code", "kbd", "samp", "var", "mark", "abbr"}
_MEANINGFUL_TAGS = {
    "section", "article", "aside", "header", "footer", "div", "figure",
    "figcaption", "blockquote", "img", "table", *_TEXT_TAGS,
}
_VISIBLE_PIPELINE_TEXT_PATTERNS = (
    (
        "authored_html_descriptor",
        re.compile(r"\bauthored\s+html\b", flags=re.IGNORECASE),
        "authored HTML",
    ),
    (
        "generated_evidence_imagery_descriptor",
        re.compile(r"\bgenerated\s+evidence\s+imagery\b", flags=re.IGNORECASE),
        "generated evidence imagery",
    ),
    (
        "source_backed_poster_descriptor",
        re.compile(
            r"\bsource[-\s]+backed\s+(?:paper\s+)?poster\b|\bsource[-\s]+backed\s+authored\b",
            flags=re.IGNORECASE,
        ),
        "source-backed poster/authored",
    ),
    (
        "generic_paper_poster_pipeline_descriptor",
        re.compile(
            r"\bpaper\s+poster\b.{0,120}\b(?:source[-\s]+backed|authored\s+html|generated\s+evidence\s+imagery)\b",
            flags=re.IGNORECASE | re.DOTALL,
        ),
        "paper poster pipeline descriptor",
    ),
)
_IDENTITY_HEADER_SELECTOR = (
    "header.poster-header,header.editorial-header,header.identity-header,"
    "[data-panel-role='identity_header'],[data-panel-role='identity'],[data-role='identity_header'],"
    "[data-slot-id='title_meta'],[data-block-id='title_meta']"
)
_MATH_ALLOWED_CONTAINER_TOKENS = {
    "formula", "math-block", "mathblock", "equation", "equation-block",
    "display-math", "math-display", "proof", "derivation",
}
_MATH_NARROW_CONTAINER_TOKENS = {
    "metric", "metrics", "metric-card", "metric-chip", "setup-metric",
    "stage", "pipeline-stage", "step", "chip", "badge", "pill", "stat",
    "stats", "kpi", "score", "scorecard", "result-card", "result-chip",
    "takeaway-row", "arch-row",
}
_IDENTITY_HEADER_FORBIDDEN_TEXT_RE = re.compile(
    r"\b(?:"
    r"core\s+ideas?|poster\s+focus|key\s+takeaways?|takeaways?|"
    r"thesis|claims?|main\s+idea|method\s+summary|result\s+summary|"
    r"analysis\s+summary|poster\s+reading|what\s+the\s+numbers\s+imply"
    r")\b",
    flags=re.IGNORECASE,
)
_IDENTITY_HEADER_FORBIDDEN_ROLE_TOKENS = (
    "summary",
    "thesis",
    "claim",
    "takeaway",
    "focus",
    "core-idea",
    "main-idea",
    "poster-focus",
    "readout",
    "callout",
    "insight",
    "analysis",
)
_IDENTITY_HEADER_ALLOWED_ROLE_TOKENS = (
    "title",
    "author",
    "byline",
    "affiliation",
    "institution",
    "university",
    "school",
    "lab",
    "company",
)
_IDENTITY_HEADER_IDENTITY_TEXT_RE = re.compile(
    r"\b(?:"
    r"affiliation|institution|university|college|institute|school|department|laborator(?:y|ies)|lab|team|"
    r"corp(?:oration)?|company|inc|ltd|llc|google|meta|microsoft|"
    r"openai|nvidia|adobe|stanford|mit|berkeley|cmu|princeton|harvard"
    r")\b",
    flags=re.IGNORECASE,
)
_IDENTITY_HEADER_DISALLOWED_METADATA_RE = re.compile(
    r"(?:https?://|github\.com|huggingface\.co|\b(?:"
    r"arxiv|doi|cvpr|iccv|eccv|neurips|nips|iclr|icml|acl|emnlp|naacl|"
    r"aaai|ijcai|siggraph|uist|chi|kdd|www|workshop|conference|venue|poster|"
    r"pmlr|jmlr|nber|aea|scientific\s+data|nature|science\s+(?:journal|advances)|journal|proceedings|"
    r"github|hugging\s*face|project(?:\s+page)?|source\s+code|code|model\s+weights|"
    r"dataset|artifact|archive)\b)",
    flags=re.IGNORECASE,
)
_IDENTITY_HEADER_NONIDENTITY_TOPIC_RE = re.compile(
    r"\b(?:"
    r"text|vision|audio|image|images|multimodal(?:ity)?|modality|modalities|"
    r"token(?:s|izer|izers|ization)?|autoregressive|generation|understanding|"
    r"agents?|open[-\s]?weights?|evaluation|release|listed|"
    r"benchmark|architecture|method|pipeline|framework|objective|backbone|"
    r"discrete|native|reconstruction|evidence|results?|analysis|takeaway|"
    r"complexity|country[-\s]?product|countries?|products?|networks?|"
    r"capabilit(?:y|ies)|accumulation|development|growth|income|welfare|"
    r"tariff|trade|path\s+dependence|validation\s+design|data\s+measures"
    r")\b",
    flags=re.IGNORECASE,
)
_DESIGNER_OWNED_CSS_MODES = {
    "authored_css",
    "authored_css_poster",
    "authored_css_layout",
    "browser_layout",
    "designer_owned_css",
    "designer_owned_layout",
    "planner_owned_css",
    "planner_owned_layout",
    "planner_css",
    "free_css",
    "freeform_css",
    "css_first",
    "normal_flow",
    "browser_flow",
    "editorial_flow",
    "editorial_flow_poster",
    "conference_editorial_flow",
}
_SVG_PRESENTATION_COLOR_ATTRIBUTES = {
    "color",
    "fill",
    "flood-color",
    "lighting-color",
    "stop-color",
    "stroke",
}
_CSS_NON_COLOR_KEYWORDS = {
    "currentcolor",
    "inherit",
    "initial",
    "none",
    "revert",
    "revert-layer",
    "transparent",
    "unset",
}
_CSS_ABSOLUTE_COLOR_FUNCTIONS = {
    "color",
    "device-cmyk",
    "hsl",
    "hsla",
    "hwb",
    "lab",
    "lch",
    "oklab",
    "oklch",
    "rgb",
    "rgba",
}
_CSS_DYNAMIC_PSEUDO_CLASSES = {
    "active",
    "any-link",
    "autofill",
    "checked",
    "default",
    "defined",
    "disabled",
    "enabled",
    "focus",
    "focus-visible",
    "focus-within",
    "fullscreen",
    "hover",
    "indeterminate",
    "in-range",
    "invalid",
    "link",
    "modal",
    "open",
    "optional",
    "out-of-range",
    "paused",
    "picture-in-picture",
    "placeholder-shown",
    "playing",
    "popover-open",
    "read-only",
    "read-write",
    "required",
    "state",
    "target",
    "target-current",
    "user-invalid",
    "user-valid",
    "valid",
    "visited",
}
_CSS_LEGACY_PSEUDO_ELEMENTS = {
    "after",
    "before",
    "first-letter",
    "first-line",
}


def propose_paper_poster_html(args: dict[str, Any], *, ctx: ToolContext) -> ToolResultRecord:
    """Compile constrained paper-poster HTML/CSS into a DesignSpec."""
    body_html, css = _extract_html_css(args)
    raw_body_html_for_palette = body_html
    raw_css_for_palette = css
    if not body_html.strip():
        return obs_error(
            "propose_paper_poster_html requires non-empty `html` or `authored_body_html`.",
            category="validation",
            payload={
                "issue_id": "paper_poster_html_empty",
                "repair_route": "submit_non_empty_constrained_html",
                "required_root": "body-internal poster DOM or <main class=\"paper-poster\">",
            },
        )
    raw_math_issues = lint_tex_math_source(body_html)
    if raw_math_issues:
        issue = raw_math_issues[0]
        return obs_error(
            str(issue.get("message") or "paper poster math source is invalid"),
            category="validation",
            payload={
                "issue_id": str(issue.get("id") or "paper_poster_html_math_source_invalid"),
                "repair_route": "repair_tex_math_source",
                "issues": raw_math_issues[:8],
                "hint": str(issue.get("fix") or "Rewrite formulas using valid TeX math delimiters."),
            },
        )

    body_html, css, root_shell = _normalize_authored_html_document_with_root_shell(body_html, css)
    if root_shell:
        ctx.state["paper_poster_html_root_shell"] = root_shell
    else:
        ctx.state.pop("paper_poster_html_root_shell", None)
    css = _alias_nested_poster_root_selectors(css)
    body_html = _unwrap_redundant_editorial_root_wrapper(body_html)
    if not body_html.strip():
        return obs_error(
            "propose_paper_poster_html normalized to an empty poster body.",
            category="validation",
            payload={"issue_id": "paper_poster_html_empty_after_normalization"},
        )

    canvas = _canvas_for_html_first(args, ctx)
    soup = BeautifulSoup(body_html, "html.parser")
    panel_flow_mode = _panel_flow_mode_requested(args, soup, ctx)
    _ensure_dom_block_ids(soup, ctx, panel_flow_mode=panel_flow_mode)
    candidate = _start_html_first_candidate(ctx, _body_inner_html(soup), css, canvas)
    designer_owned_css = _designer_owned_css_mode(args, soup, ctx)
    fixed_slot_contract = _fixed_layout_slot_contract_active(ctx)
    bboxes: dict[str, dict[str, Any]] = {}
    ctx.state.pop("paper_poster_html_editorial_lead_key_diagnostics", None)
    def fail(result: ToolResultRecord, stage: str, *, local_repair_hint: str | None = None) -> ToolResultRecord:
        editorial_lead_key_diagnostics = ctx.state.get("paper_poster_html_editorial_lead_key_diagnostics")
        if isinstance(editorial_lead_key_diagnostics, list) and editorial_lead_key_diagnostics:
            _attach_editorial_lead_key_diagnostics(result, editorial_lead_key_diagnostics)
        if designer_owned_css and isinstance(result.payload, dict) and bboxes:
            _attach_secondary_gate_diagnostics(
                result.payload,
                soup,
                css,
                ctx,
                bboxes,
                canvas,
                primary_stage=stage,
            )
        return _attach_candidate_to_result(ctx, candidate, result, stage=stage, local_repair_hint=local_repair_hint)
    required_color_system = ctx.state.get("required_color_system")
    if (
        isinstance(required_color_system, dict)
        and str(required_color_system.get("palette_id") or "").strip()
    ):
        required_palette_diagnostics = authored_palette_diagnostics(
            raw_body_html_for_palette,
            raw_css_for_palette,
            required_color_system,
            require_selected=True,
        )
        if required_palette_diagnostics:
            ctx.state["paper_poster_html_palette_diagnostics"] = required_palette_diagnostics
        blocking_palette_diagnostics = [
            diagnostic
            for diagnostic in required_palette_diagnostics
            if required_palette_diagnostic_is_blocking(diagnostic)
        ]
        if blocking_palette_diagnostics:
            return fail(
                obs_error(
                    "propose_paper_poster_html rejected authored HTML that does not match the required palette.",
                    category="validation",
                    payload={
                        "issue_id": "paper_poster_html_required_palette_validation_failed",
                        "repair_route": "apply_required_poster_palette",
                        "required_palette_id": required_color_system.get("palette_id"),
                        "issues": blocking_palette_diagnostics,
                        "paper_poster_html_palette_diagnostics": required_palette_diagnostics,
                        "hint": (
                            "Set the exact required data-palette-id and every canonical "
                            "--poster-* CSS variable, and remove foreign shell/UI colors "
                            "before resubmitting."
                        ),
                    },
                ),
                "required_palette",
            )
    math_issues = lint_tex_math_source(body_html)
    if math_issues:
        issue = math_issues[0]
        return fail(obs_error(
            str(issue.get("message") or "paper poster math source is invalid"),
            category="validation",
            payload={
                "issue_id": str(issue.get("id") or "paper_poster_html_math_source_invalid"),
                "repair_route": "repair_tex_math_source",
                "issues": math_issues[:8],
                "hint": str(issue.get("fix") or "Rewrite formulas using valid TeX math delimiters."),
            },
        ), "math_source")
    if designer_owned_css:
        ctx.state["paper_poster_designer_owned_css"] = True
        log("paper_poster_html.designer_owned_css_enabled")
        unsafe_asset_ref_error = _unsafe_external_asset_reference_error(soup, css, ctx)
        if unsafe_asset_ref_error:
            _record_content_quality_blocking(ctx, unsafe_asset_ref_error, stage="unsafe_external_asset_ref")
            return fail(unsafe_asset_ref_error, "unsafe_external_asset_ref")
    pipeline_text_error = _visible_pipeline_text_error(soup, ctx)
    if pipeline_text_error:
        _record_content_quality_blocking(ctx, pipeline_text_error, stage="visible_pipeline_text")
        return fail(pipeline_text_error, "visible_pipeline_text")
    identity_header_error = _identity_header_only_error(soup, ctx)
    if identity_header_error:
        _record_content_quality_blocking(ctx, identity_header_error, stage="identity_header_only")
        return fail(identity_header_error, "identity_header_only")
    editorial_plan_error = _editorial_panel_content_plan_error(args, ctx)
    if editorial_plan_error:
        _record_content_quality_blocking(ctx, editorial_plan_error, stage="editorial_panel_content_plan")
        return fail(editorial_plan_error, "editorial_panel_content_plan")
    if not designer_owned_css:
        slot_repairs = _apply_layout_slot_contract_to_dom(soup, ctx)
        if slot_repairs:
            log(
                "paper_poster_html.layout_slot_contract_applied",
                repairs=len(slot_repairs),
                first_repairs=slot_repairs[:8],
            )
        source_repairs = _apply_layout_slot_source_visuals(soup, ctx)
        if source_repairs:
            log(
                "paper_poster_html.layout_slot_source_visuals_applied",
                repairs=len(source_repairs),
                first_repairs=source_repairs[:8],
            )
        lane_flow_repairs = _normalize_fixed_lane_child_flow(soup)
        if lane_flow_repairs:
            log(
                "paper_poster_html.fixed_lane_child_flow_normalized",
                repairs=len(lane_flow_repairs),
                first_repairs=lane_flow_repairs[:8],
            )
    source_error = _source_coverage_error(soup, ctx)
    if source_error:
        ctx.state["paper_poster_html_source_coverage_warning"] = (
            source_error.payload if isinstance(source_error.payload, dict) else {}
        )
        log(
            "paper_poster_html.source_coverage_block",
            reason="required_source_visual_missing",
            issue_id=(
                source_error.payload or {}
            ).get("issue_id") if isinstance(source_error.payload, dict) else None,
            missing_source_ids=(
                source_error.payload or {}
            ).get("missing_source_ids") if isinstance(source_error.payload, dict) else None,
        )
        return fail(source_error, "source_coverage")
    if not designer_owned_css:
        visible_caption_error = _source_visible_caption_error(soup, ctx)
        if visible_caption_error:
            _record_content_quality_blocking(ctx, visible_caption_error, stage="source_visible_caption")
            return fail(visible_caption_error, "source_visible_caption")
        wrap_error = _source_wrap_error(soup, css, ctx)
        if wrap_error:
            _record_content_quality_blocking(ctx, wrap_error, stage="source_visual_wrap")
            return fail(wrap_error, "source_visual_wrap")
        _clear_content_quality_blocking_issue(ctx, "paper_poster_html_source_wrap_missing")
        ctx.state.pop("paper_poster_html_source_wrap", None)
        flow_shape_error = _source_panel_flow_shape_error(soup, css, ctx)
        if flow_shape_error:
            _record_content_quality_blocking(ctx, flow_shape_error, stage="panel_flow_shape")
            return fail(flow_shape_error, "panel_flow_shape")
        _clear_content_quality_blocking_issue(ctx, "paper_poster_html_panel_flow_shape_failed")
    if not designer_owned_css:
        editorial_shape_error = _editorial_flow_shape_error(soup, css, ctx)
        if editorial_shape_error:
            _record_content_quality_blocking(ctx, editorial_shape_error, stage="editorial_flow_shape")
            return fail(editorial_shape_error, "editorial_flow_shape")
    editorial_lead_key_diagnostics = _paper_poster_editorial_lead_key_diagnostics(soup, ctx)
    _record_editorial_lead_key_diagnostics(ctx, editorial_lead_key_diagnostics)
    numeric_claim_error = _numeric_claim_provenance_error(soup, ctx)
    if numeric_claim_error:
        _record_content_quality_blocking(ctx, numeric_claim_error, stage="numeric_claim")
        return fail(numeric_claim_error, "numeric_claim")
    native_benchmark_error = _native_benchmark_table_policy_error(soup, ctx)
    if native_benchmark_error:
        _record_content_quality_blocking(ctx, native_benchmark_error, stage="native_benchmark_table")
        return fail(native_benchmark_error, "native_benchmark_table")
    _hydrate_image_sources_for_measurement(soup, ctx)
    if not designer_owned_css and not fixed_slot_contract:
        intrinsic_repairs = _apply_intrinsic_source_visual_bboxes(soup, ctx, canvas)
        if intrinsic_repairs:
            log(
                "paper_poster_html.intrinsic_source_visual_fit_applied",
                repair_count=len(intrinsic_repairs),
                first_repairs=intrinsic_repairs[:5],
            )
    html_with_ids = _body_inner_html(soup)
    bboxes = _measure_dom_bboxes(
        html_with_ids,
        css,
        canvas=canvas,
        ctx=ctx,
        candidate=candidate,
        stage="initial_measure",
        root_shell=root_shell,
    )
    if not bboxes:
        bboxes = _fallback_dom_bboxes(soup, canvas)
    if not designer_owned_css and not fixed_slot_contract:
        measured_intrinsic_repairs = _fit_measured_source_visual_bboxes(soup, bboxes, ctx)
        if measured_intrinsic_repairs:
            log(
                "paper_poster_html.intrinsic_source_visual_bbox_measured",
                repair_count=len(measured_intrinsic_repairs),
                first_repairs=measured_intrinsic_repairs[:5],
            )
        _expand_text_bboxes_for_fit(soup, bboxes, canvas)
    if not designer_owned_css and _deterministic_layout_repair_enabled(ctx):
        overlap_repairs = _resolve_severe_text_overlaps(soup, bboxes, canvas)
        if overlap_repairs:
            log(
                "paper_poster_html.text_overlap_auto_repaired",
                repair_count=len(overlap_repairs),
                first_repairs=overlap_repairs[:5],
            )
    boundary_issues = _bbox_boundary_issues(soup, bboxes, canvas)
    expanded_canvas = _expanded_canvas_for_boundary_issues(boundary_issues, canvas, ctx)
    if expanded_canvas is not None:
        old_canvas = dict(canvas)
        canvas = expanded_canvas
        html_with_ids = _body_inner_html(soup)
        bboxes = _measure_dom_bboxes(
            html_with_ids,
            css,
            canvas=canvas,
            ctx=ctx,
            candidate=candidate,
            stage="expanded_canvas_measure",
            root_shell=root_shell,
        )
        if not bboxes:
            bboxes = _fallback_dom_bboxes(soup, canvas)
        if not designer_owned_css and not fixed_slot_contract:
            measured_intrinsic_repairs = _fit_measured_source_visual_bboxes(soup, bboxes, ctx)
            if measured_intrinsic_repairs:
                log(
                    "paper_poster_html.intrinsic_source_visual_bbox_measured",
                    repair_count=len(measured_intrinsic_repairs),
                    first_repairs=measured_intrinsic_repairs[:5],
                    reason="after_canvas_expand",
                )
            _expand_text_bboxes_for_fit(soup, bboxes, canvas)
        boundary_issues = _bbox_boundary_issues(soup, bboxes, canvas)
        ctx.state["paper_poster_html_canvas_auto_expand"] = {
            "old_canvas": old_canvas,
            "new_canvas": dict(canvas),
            "residual_boundary_issues": len(boundary_issues),
        }
        log(
            "paper_poster_html.canvas_auto_expanded",
            old_canvas=old_canvas,
            new_canvas=canvas,
            residual_boundary_issues=len(boundary_issues),
        )
    rendered_source_error = _source_coverage_error(
        soup,
        ctx,
        bboxes=bboxes,
        canvas=canvas,
    )
    if rendered_source_error:
        _record_content_quality_blocking(ctx, rendered_source_error, stage="rendered_source_coverage")
        return fail(rendered_source_error, "rendered_source_coverage")
    reference_style_error = _reference_style_contract_error(
        soup,
        css,
        ctx,
        root_shell=root_shell,
        bboxes=bboxes,
    )
    if reference_style_error:
        _record_content_quality_blocking(ctx, reference_style_error, stage="reference_style_contract")
        return fail(
            reference_style_error,
            "reference_style_contract",
            local_repair_hint=_local_repair_hint(soup, bboxes, canvas),
        )
    narrow_math_error = _narrow_math_container_error(candidate, canvas, ctx)
    if narrow_math_error:
        _record_content_quality_blocking(ctx, narrow_math_error, stage="narrow_math_container")
        return fail(
            narrow_math_error,
            "narrow_math_container",
            local_repair_hint=_local_repair_hint(soup, bboxes, canvas, shell_error=narrow_math_error),
        )
    if designer_owned_css:
        shell_error = _designer_owned_flow_canvas_shell_error(soup, bboxes, canvas)
        if shell_error:
            local_hint = _local_repair_hint(soup, bboxes, canvas, shell_error=shell_error)
            _record_content_quality_blocking(ctx, shell_error, stage="designer_owned_canvas_shell")
            return fail(shell_error, "designer_owned_canvas_shell", local_repair_hint=local_hint)
        local_flow_overflow_error = _designer_owned_local_flow_overflow_error(soup, bboxes, canvas)
        if local_flow_overflow_error:
            row_allocation_error = _designer_owned_row_allocation_density_regression_error(
                soup,
                bboxes,
                canvas,
                local_flow_overflow_error,
            )
            if row_allocation_error:
                local_hint = _local_repair_hint(soup, bboxes, canvas, shell_error=row_allocation_error)
                _record_content_quality_blocking(ctx, row_allocation_error, stage="row_allocation_density")
                return fail(row_allocation_error, "row_allocation_density", local_repair_hint=local_hint)
            local_hint = _local_repair_hint(soup, bboxes, canvas, shell_error=local_flow_overflow_error)
            _record_content_quality_blocking(ctx, local_flow_overflow_error, stage="local_flow_overflow")
            return fail(local_flow_overflow_error, "local_flow_overflow", local_repair_hint=local_hint)
        heading_flow_overflow_error = _designer_owned_heading_flow_overflow_error(soup, bboxes, canvas)
        if heading_flow_overflow_error:
            _record_content_quality_blocking(ctx, heading_flow_overflow_error, stage="heading_flow_overflow")
            return fail(
                heading_flow_overflow_error,
                "heading_flow_overflow",
                local_repair_hint=_local_repair_hint(soup, bboxes, canvas),
            )
        editorial_shape_error = _editorial_flow_shape_error(soup, css, ctx)
        if editorial_shape_error:
            _record_content_quality_blocking(ctx, editorial_shape_error, stage="editorial_flow_shape")
            return fail(
                editorial_shape_error,
                "editorial_flow_shape",
                local_repair_hint=_local_repair_hint(soup, bboxes, canvas),
            )
        visible_caption_error = _source_visible_caption_error(soup, ctx)
        if visible_caption_error:
            _record_content_quality_blocking(ctx, visible_caption_error, stage="source_visible_caption")
            return fail(visible_caption_error, "source_visible_caption")
        wrap_error = _source_wrap_error(soup, css, ctx)
        if wrap_error:
            _record_content_quality_blocking(ctx, wrap_error, stage="source_visual_wrap")
            return fail(wrap_error, "source_visual_wrap")
        _clear_content_quality_blocking_issue(ctx, "paper_poster_html_source_wrap_missing")
        ctx.state.pop("paper_poster_html_source_wrap", None)
        flow_shape_error = _source_panel_flow_shape_error(soup, css, ctx)
        if flow_shape_error:
            _record_content_quality_blocking(ctx, flow_shape_error, stage="panel_flow_shape")
            return fail(flow_shape_error, "panel_flow_shape")
        _clear_content_quality_blocking_issue(ctx, "paper_poster_html_panel_flow_shape_failed")
        typography_error = _paper_poster_typography_contract_error(soup, bboxes, ctx)
        if typography_error:
            _attach_editorial_lead_key_diagnostics(typography_error, editorial_lead_key_diagnostics)
            _record_content_quality_blocking(ctx, typography_error, stage="typography_contract")
            return fail(
                typography_error,
                "typography_contract",
                local_repair_hint=_local_repair_hint(soup, bboxes, canvas),
            )
        visual_size_error = _source_visual_size_error(soup, bboxes, ctx)
        if visual_size_error:
            _record_content_quality_blocking(ctx, visual_size_error, stage="source_visual_size")
            return fail(
                visual_size_error,
                "source_visual_size",
                local_repair_hint=_local_repair_hint(soup, bboxes, canvas),
            )
        if not _active_reference_style_contract(ctx):
            density_error = _post_overflow_density_conservation_error(soup, bboxes, canvas, ctx)
            if density_error:
                _record_content_quality_blocking(ctx, density_error, stage="post_overflow_density_conservation")
                return fail(
                    density_error,
                    "post_overflow_density_conservation",
                    local_repair_hint=_local_repair_hint(soup, bboxes, canvas),
                )
            editorial_fill_error = _editorial_flow_fill_error(soup, bboxes, canvas, ctx)
            if editorial_fill_error:
                _record_content_quality_blocking(ctx, editorial_fill_error, stage="editorial_flow_fill")
                return fail(
                    editorial_fill_error,
                    "editorial_flow_fill",
                    local_repair_hint=_local_repair_hint(soup, bboxes, canvas),
                )
    if boundary_issues:
        severe_boundary_issues = _severe_boundary_issues(boundary_issues, canvas)
        error = obs_error(
            "propose_paper_poster_html found blocks outside the fixed poster canvas.",
            category="validation",
            payload={
                "issue_id": "paper_poster_html_block_out_of_bounds",
                "repair_route": "planner_repair_fixed_canvas_geometry",
                "canvas": canvas,
                "issues": (severe_boundary_issues or boundary_issues)[:12],
                "severity_reason": (
                    "severe_canvas_overflow"
                    if severe_boundary_issues else
                    "planner_only_canvas_overflow"
                ),
                "hint": (
                    "Measured [data-block-id] elements should fit inside the canvas. "
                    "Repair the authored HTML/CSS layout inside the planner loop before "
                    "resubmitting."
                ),
            },
        )
        return fail(
            error,
            "canvas_boundary",
            local_repair_hint=_local_repair_hint(soup, bboxes, canvas),
        )
    clipping_issues = _text_clipping_issues(soup, bboxes)
    if clipping_issues:
        severe_clipping_issues = _severe_text_clipping_issues(clipping_issues)
        if severe_clipping_issues:
            error = obs_error(
                "propose_paper_poster_html found severe editable text clipping after measurement.",
                category="validation",
                payload={
                    "issue_id": "paper_poster_html_severe_text_clipping",
                    "repair_route": "planner_repair_text_box_fit",
                    "issues": severe_clipping_issues[:12],
                    "hint": (
                        "Measured text/caption/quote/metric elements must not clip their own content. "
                        "Repair by moving lanes, increasing the box, lowering type size/line-height, "
                        "or splitting content into explicit non-overlapping rows/columns before "
                        "resubmitting HTML. Preserve the source-backed content density; local copy "
                        "trimming is a last resort, not the default fix."
                    ),
                },
            )
            return fail(
                error,
                "text_clipping",
                local_repair_hint=_local_repair_hint(soup, bboxes, canvas),
            )
        ctx.state["paper_poster_html_text_clipping_warnings"] = clipping_issues
        log(
            "paper_poster_html.text_clipping_warn",
            issue_count=len(clipping_issues),
            first_issues=clipping_issues[:3],
            severe_count=len(severe_clipping_issues),
            reason=(
                "designer_owned_css_feedback"
                if designer_owned_css and severe_clipping_issues else
                "designer_repair_feedback"
                if severe_clipping_issues else
                "non_severe_polish"
            ),
        )
    overlap_issues = _text_overlap_issues(soup, bboxes)
    if overlap_issues:
        severe_overlap_issues = _severe_text_overlap_issues(overlap_issues)
        if severe_overlap_issues:
            error = obs_error(
                "propose_paper_poster_html found severe editable text overlaps after measurement.",
                category="validation",
                payload={
                    "issue_id": "paper_poster_html_severe_text_overlap",
                    "repair_route": "reserve_fixed_text_lanes_or_reflow",
                    "issues": severe_overlap_issues[:12],
                    "hint": (
                        "Do not let headings, authors, captions, bullets, or prose share the same lane. "
                        "Repair overlaps by moving boxes, expanding lanes, reducing font size/line-height, "
                        "or splitting dense text into columns/table rows before resubmitting HTML. Do not "
                        "delete source-backed content to make the gate pass unless the same information is "
                        "preserved elsewhere."
                    ),
                },
            )
            return fail(
                error,
                "text_overlap",
                local_repair_hint=_local_repair_hint(soup, bboxes, canvas),
            )
        log(
            "paper_poster_html.text_overlap_warn",
            issue_count=len(overlap_issues),
            first_issues=overlap_issues[:3],
            severe_count=len(severe_overlap_issues),
            reason=(
                "designer_owned_css_feedback"
                if designer_owned_css and severe_overlap_issues else
                "designer_repair_feedback"
                if severe_overlap_issues else
                "non_severe_polish"
            ),
        )
    fill_issues = (
        _canvas_fill_issues(soup, bboxes, canvas)
        if _dogfood_dense_mode(ctx) and not _active_reference_style_contract(ctx)
        else []
    )
    if fill_issues:
        severe_fill_issues = _severe_canvas_fill_issues(fill_issues, canvas)
        if severe_fill_issues:
            error = obs_error(
                "propose_paper_poster_html found severe canvas underfill for a dense reference poster.",
                category="validation",
                payload={
                    "issue_id": "paper_poster_html_severe_canvas_underfill",
                    "repair_route": "extend_dense_storyboard_to_bottom_edge",
                    "issues": severe_fill_issues[:6],
                    "hint": (
                        "The reference contract requires dense bottom-half usage. "
                        "If this happened after fixing overflow, revert to the previous near-miss "
                        "composition instead of continuing a globally compressed layout. Fill the "
                        "bottom by restoring readable source figures/tables and local flow units, "
                        "then shorten low-value prose and tighten gaps only where needed. Do not "
                        "solve overflow by collapsing all sections into a shallow strip at the top."
                    ),
                },
            )
            return fail(
                error,
                "canvas_fill",
                local_repair_hint=_local_repair_hint(soup, bboxes, canvas),
            )
        ctx.state["paper_poster_html_canvas_fill_warnings"] = fill_issues
        log(
            "paper_poster_html.canvas_fill_warn",
            issue_count=len(fill_issues),
            first_issues=fill_issues[:3],
            reason=(
                "near_threshold_underfill_allowed_until_dom_audit"
            ),
        )
    geometry_css = "" if designer_owned_css else _compiled_geometry_css(
        soup,
        bboxes,
        fixed_slot_contract=fixed_slot_contract,
    )
    blocks = _compile_blocks_from_dom(soup, bboxes, ctx)
    if not blocks:
        return fail(obs_error(
            "propose_paper_poster_html could not infer any editable poster blocks.",
            category="validation",
            payload={
                "issue_id": "paper_poster_html_no_blocks",
                "repair_route": "add_semantic_panels_text_figures",
            },
        ), "no_blocks")
    panel_plan_issues = _dogfood_panel_content_plan_issues(args, blocks, canvas, ctx)
    panel_density_issues = (
        []
        if _active_reference_style_contract(ctx)
        else _dogfood_panel_density_issues(blocks, canvas, ctx)
    )
    table_issues = _dogfood_benchmark_table_issues(blocks, ctx)
    if panel_density_issues:
        ctx.state["paper_poster_html_panel_density_diagnostics"] = {
            "issue_count": len(panel_density_issues),
            "issue_ids": [str(issue.get("id") or "") for issue in panel_density_issues[:12]],
            "issues": panel_density_issues[:12],
        }
        log(
            "paper_poster_html.panel_density_diagnostic",
            issue_count=len(panel_density_issues),
            issue_ids=[str(issue.get("id") or "") for issue in panel_density_issues[:12]],
            first_issues=panel_density_issues[:5],
            reason="designer_local_repair_diagnostic",
        )
    else:
        ctx.state.pop("paper_poster_html_panel_density_diagnostics", None)
    contract_issues = [*panel_plan_issues, *panel_density_issues, *table_issues]
    if contract_issues:
        ctx.state.pop("paper_poster_html_content_contract_warnings", None)
        ctx.state.pop("paper_poster_html_content_contract_soft_deferred", None)
        blocking_issues = contract_issues[:12]
        blocking_issue_ids = [str(issue.get("id") or "") for issue in blocking_issues]
        ctx.state["paper_poster_html_content_contract_blocking"] = {
            "issue_count": len(contract_issues),
            "issue_ids": blocking_issue_ids[:12],
            "issues": blocking_issues,
        }
        error = obs_error(
            "propose_paper_poster_html found under-budget panel content for the dense paper-poster contract.",
            category="validation",
            payload={
                "issue_id": "paper_poster_html_panel_content_contract_failed",
                "repair_route": "planner_repair_panel_content_contract",
                "issues": blocking_issues,
                "hint": (
                    "Submit real large-panel content before finalization: the contract-defined "
                    "CVPR three-column main-panel grid, enough native source text, result/benchmark evidence, "
                    "and panel interiors that are visibly filled by measured text, tables, figures, "
                    "captions, or paper-grounded readouts."
                ),
            },
        )
        feedback_state = _content_contract_hard_feedback_state(ctx)
        if feedback_state["exhausted"]:
            ctx.state["designer_contract_abort"] = {
                "reason": "paper_poster_content_contract_unresolved",
                "tool": "propose_paper_poster_html",
                "hard_feedback_count": feedback_state["count"],
                "hard_feedback_limit": feedback_state["limit"],
                "issue_ids": blocking_issue_ids[:12],
            }
        log(
            "paper_poster_html.panel_content_contract_failed",
            issue_count=len(contract_issues),
            issue_ids=blocking_issue_ids[:12],
            first_issues=blocking_issues[:5],
            hard_feedback_count=feedback_state["count"],
            hard_feedback_limit=feedback_state["limit"],
            reason=(
                "content_contract_budget_exhausted"
                if feedback_state["exhausted"] else
                "designer_repair_feedback_for_content_gap"
            ),
        )
        return fail(
            error,
            "panel_content_contract",
            local_repair_hint=_local_repair_hint(soup, bboxes, canvas),
        )
    else:
        ctx.state.pop("paper_poster_html_content_contract_blocking", None)
        ctx.state.pop("paper_poster_html_content_contract_warnings", None)
        ctx.state.pop("paper_poster_html_content_contract_soft_deferred", None)
        ctx.state["paper_poster_html_content_contract_passed"] = True

    design_spec = _build_design_spec(
        args,
        ctx,
        canvas=canvas,
        body_html=html_with_ids,
        css=_join_css(css, geometry_css, canvas=None if designer_owned_css else canvas),
        blocks=blocks,
        designer_owned_css=designer_owned_css,
        root_shell=root_shell,
    )
    palette_diagnostics = _authored_palette_diagnostics(
        raw_body_html_for_palette,
        raw_css_for_palette,
        design_spec.get("color_system") if isinstance(design_spec.get("color_system"), dict) else {},
    )
    palette_diagnostics.extend(
        _authored_reference_style_diagnostics(
            raw_body_html_for_palette,
            raw_css_for_palette,
            ctx.state.get("reference_style_contract"),
        )
    )
    if palette_diagnostics:
        ctx.state["paper_poster_html_palette_diagnostics"] = palette_diagnostics
    else:
        ctx.state.pop("paper_poster_html_palette_diagnostics", None)
    log(
        "paper_poster_html.compiled",
        block_count=len(blocks),
        measured_bbox_count=len(bboxes),
        canvas=canvas,
    )
    result = propose_design_spec({"design_spec": design_spec}, ctx=ctx)
    if isinstance(result.payload, dict):
        result.payload["html_first_compiled"] = True
        result.payload["html_first_block_count"] = len(blocks)
        result.payload["html_first_authoring_mode"] = (
            "designer_owned_css" if designer_owned_css else "compiled_geometry"
        )
        if palette_diagnostics:
            result.payload["paper_poster_html_palette_diagnostics"] = palette_diagnostics
        editorial_lead_key_diagnostics = ctx.state.get("paper_poster_html_editorial_lead_key_diagnostics")
        if isinstance(editorial_lead_key_diagnostics, list) and editorial_lead_key_diagnostics:
            result.payload["paper_poster_html_editorial_lead_key_diagnostics"] = editorial_lead_key_diagnostics
        _mark_candidate_status(ctx, candidate, status="accepted", stage="compiled", payload=result.payload)
    return result


def required_palette_diagnostic_is_blocking(diagnostic: dict[str, Any]) -> bool:
    issue_id = str(diagnostic.get("issue_id") or "")
    if issue_id in {
        "paper_poster_html_required_palette_mismatch",
        "paper_poster_html_palette_id_missing",
        "paper_poster_html_palette_css_variable_mismatch",
    }:
        return True
    if issue_id != "paper_poster_html_palette_extra_authored_hex":
        return False
    return bool(
        diagnostic.get("shell_extra_colors")
        or diagnostic.get("shell_extra_hexes")
    )


def _extract_html_css(args: dict[str, Any]) -> tuple[str, str]:
    html_value = (
        args.get("html")
        or args.get("body_html")
        or args.get("authored_body_html")
        or ""
    )
    css_value = args.get("css") or args.get("authored_css") or ""
    return str(html_value or ""), str(css_value or "")


def _start_html_first_candidate(
    ctx: ToolContext,
    body_html: str,
    css: str,
    canvas: dict[str, Any],
) -> dict[str, Any]:
    count = int(ctx.state.get("paper_poster_html_candidate_count") or 0) + 1
    ctx.state["paper_poster_html_candidate_count"] = count
    candidate_id = f"candidate_{count:03d}"
    base_dir = ctx.run_dir / "html_first" / "candidates" / candidate_id
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / "body.html").write_text(body_html, encoding="utf-8")
    (base_dir / "style.css").write_text(css, encoding="utf-8")
    candidate = {
        "candidate_id": candidate_id,
        "candidate_dir": str(base_dir),
        "candidate_relative_dir": _relative_to_run_dir(ctx, base_dir),
        "body_html": str(base_dir / "body.html"),
        "style_css": str(base_dir / "style.css"),
        "status": "draft",
        "canvas": dict(canvas),
    }
    _write_candidate_manifest(ctx, candidate)
    ctx.state["paper_poster_html_latest_candidate"] = dict(candidate)
    log(
        "paper_poster_html.candidate_started",
        candidate_id=candidate_id,
        candidate_relative_dir=candidate["candidate_relative_dir"],
    )
    return candidate


def _relative_to_run_dir(ctx: ToolContext, path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(ctx.run_dir.resolve()))
    except Exception:
        return str(path)


def _write_candidate_manifest(
    ctx: ToolContext,
    candidate: dict[str, Any],
    *,
    payload: dict[str, Any] | None = None,
) -> None:
    candidate_dir = Path(str(candidate.get("candidate_dir") or ""))
    if not candidate_dir:
        return
    manifest = {
        "candidate_id": candidate.get("candidate_id"),
        "candidate_dir": candidate.get("candidate_dir"),
        "candidate_relative_dir": candidate.get("candidate_relative_dir"),
        "status": candidate.get("status") or "draft",
        "stage": candidate.get("stage") or "draft",
        "canvas": candidate.get("canvas") or {},
        "body_html": _relative_to_run_dir(ctx, Path(str(candidate.get("body_html") or ""))),
        "style_css": _relative_to_run_dir(ctx, Path(str(candidate.get("style_css") or ""))),
        "measure_html": candidate.get("measure_html_relative") or candidate.get("measure_html"),
        "preview_png": candidate.get("preview_png_relative") or candidate.get("preview_png"),
        "measurement_json": candidate.get("measurement_json_relative") or candidate.get("measurement_json"),
        "validation_overlay_png": candidate.get("validation_overlay_png_relative") or candidate.get("validation_overlay_png"),
        "visual_repair_packet_json": (
            candidate.get("visual_repair_packet_json_relative")
            or candidate.get("visual_repair_packet_json")
        ),
        "visual_repair_dir": candidate.get("visual_repair_dir_relative") or candidate.get("visual_repair_dir"),
        "candidate_score": candidate.get("candidate_score"),
        "candidate_score_reasons": candidate.get("candidate_score_reasons") or [],
        "is_best_candidate": bool(candidate.get("is_best_candidate")),
        "is_locked_base_candidate": bool(candidate.get("is_locked_base_candidate")),
    }
    if payload:
        manifest["payload"] = {
            key: payload.get(key)
            for key in (
                "issue_id",
                "repair_route",
                "candidate_id",
                "local_repair_hint",
                "hint",
                "issues",
                "first_issues",
                "html_first_block_count",
                "html_first_authoring_mode",
                "html_first_candidate_id",
                "locked_base_candidate_id",
                "locked_base_candidate_relative_dir",
                "locked_base_candidate_preview_png",
                "locked_base_candidate_measurement_json",
                "locked_base_candidate_visual_repair_packet_json",
                "locked_base_candidate_visual_repair_dir",
                "locked_base_candidate_score",
                "candidate_score",
                "candidate_score_reasons",
                "candidate_visual_repair_packet_json",
                "candidate_visual_repair_packet_json_relative",
                "candidate_visual_repair_dir",
                "candidate_visual_repair_dir_relative",
                "visual_fill_feedback",
                "primary_blocking_issue_id",
                "secondary_gate_issues",
                "all_gate_diagnostics",
                "paper_poster_html_palette_diagnostics",
                "paper_poster_html_editorial_lead_key_diagnostics",
                "density_conservation",
            )
            if key in payload
        }
    try:
        (candidate_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        log("paper_poster_html.candidate_manifest_write_failed", error=f"{type(exc).__name__}: {exc}")


def _mark_candidate_status(
    ctx: ToolContext,
    candidate: dict[str, Any],
    *,
    status: str,
    stage: str,
    payload: dict[str, Any] | None = None,
) -> None:
    candidate["status"] = status
    candidate["stage"] = stage
    if payload is not None:
        payload["html_first_candidate_id"] = candidate.get("candidate_id")
    _score_and_maybe_lock_html_first_candidate(ctx, candidate, status=status, stage=stage, payload=payload)
    ctx.state["paper_poster_html_latest_candidate"] = dict(candidate)
    _write_candidate_manifest(ctx, candidate, payload=payload)
    log(
        "paper_poster_html.candidate_status",
        candidate_id=candidate.get("candidate_id"),
        status=status,
        stage=stage,
    )


def _attach_candidate_to_result(
    ctx: ToolContext,
    candidate: dict[str, Any],
    result: ToolResultRecord,
    *,
    stage: str,
    local_repair_hint: str | None = None,
) -> ToolResultRecord:
    if not isinstance(result.payload, dict):
        result.payload = {}
    result.payload["candidate_id"] = candidate.get("candidate_id")
    result.payload["candidate_dir"] = candidate.get("candidate_dir")
    result.payload["candidate_relative_dir"] = candidate.get("candidate_relative_dir")
    result.payload["validation_stage"] = stage
    for key in (
        "measure_html",
        "measure_html_relative",
        "preview_png",
        "preview_png_relative",
        "measurement_json",
        "measurement_json_relative",
    ):
        if candidate.get(key):
            result.payload[f"candidate_{key}"] = candidate.get(key)
    if local_repair_hint:
        result.payload["local_repair_hint"] = local_repair_hint[:1200]
    _write_candidate_validation_overlay(ctx, candidate, result.payload, stage=stage)
    _write_candidate_visual_repair_packet(ctx, candidate, result.payload, stage=stage)
    _mark_candidate_status(ctx, candidate, status="validation_error", stage=stage, payload=result.payload)
    _attach_locked_base_candidate_to_payload(ctx, result.payload)
    result.payload["candidate_score"] = candidate.get("candidate_score")
    result.payload["candidate_score_reasons"] = list(candidate.get("candidate_score_reasons") or [])
    visual_fill_feedback = _candidate_visual_fill_feedback(ctx, candidate)
    if visual_fill_feedback:
        result.payload["visual_fill_feedback"] = visual_fill_feedback
    return result


def _write_candidate_validation_overlay(
    ctx: ToolContext,
    candidate: dict[str, Any],
    payload: dict[str, Any],
    *,
    stage: str,
) -> None:
    preview_path = Path(str(candidate.get("preview_png") or ""))
    measurement_path = Path(str(candidate.get("measurement_json") or ""))
    candidate_dir = Path(str(candidate.get("candidate_dir") or ""))
    if not preview_path.exists() or not measurement_path.exists() or not candidate_dir:
        return
    try:
        measurement = json.loads(measurement_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    bboxes = measurement.get("bboxes") if isinstance(measurement, dict) else {}
    canvas = measurement.get("canvas") if isinstance(measurement, dict) else {}
    if not isinstance(bboxes, dict) or not isinstance(canvas, dict):
        return
    try:
        image = Image.open(preview_path).convert("RGBA")
    except OSError:
        return
    cw = max(1, _safe_int(canvas.get("w_px"), default=image.width))
    scale = image.width / float(cw)
    issue_id = str(payload.get("issue_id") or "")
    raw_issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
    issues = [
        issue
        for issue in raw_issues
        if not _visual_repair_should_skip_issue(issue, issue_id, ctx)
    ]
    overlay_h = 108
    annotated = Image.new("RGBA", (image.width, image.height + overlay_h), (255, 255, 255, 255))
    annotated.paste(image, (0, overlay_h))
    draw = ImageDraw.Draw(annotated, "RGBA")
    title_font = _validation_overlay_font(22)
    small_font = _validation_overlay_font(14)
    draw.text((16, 14), f"Harness validation overlay: {issue_id or stage}", fill=(17, 24, 39, 255), font=title_font)
    draw.text((16, 46), f"candidate={candidate.get('candidate_id') or ''} stage={stage}", fill=(55, 65, 81, 255), font=small_font)
    if issues:
        draw.text((16, 70), _validation_issue_summary(issues[0]), fill=(55, 65, 81, 255), font=small_font)
    else:
        draw.text((16, 70), "No structured issue payload; overlay shows candidate measurement context only.", fill=(55, 65, 81, 255), font=small_font)
    targets = _validation_overlay_targets(issues, bboxes)
    for index, target in enumerate(targets[:16]):
        color = _VALIDATION_OVERLAY_COLORS[index % len(_VALIDATION_OVERLAY_COLORS)]
        _draw_validation_overlay_box(
            draw,
            target.get("bbox") if isinstance(target.get("bbox"), dict) else {},
            label=str(target.get("label") or ""),
            color=color,
            scale=scale,
            y_offset=overlay_h,
        )
    output_path = candidate_dir / "validation_overlay.png"
    try:
        annotated.convert("RGB").save(output_path)
    except OSError:
        return
    candidate["validation_overlay_png"] = str(output_path)
    candidate["validation_overlay_png_relative"] = _relative_to_run_dir(ctx, output_path)
    payload["candidate_validation_overlay_png"] = candidate["validation_overlay_png"]
    payload["candidate_validation_overlay_png_relative"] = candidate["validation_overlay_png_relative"]


def _write_candidate_visual_repair_packet(
    ctx: ToolContext,
    candidate: dict[str, Any],
    payload: dict[str, Any],
    *,
    stage: str,
) -> None:
    preview_path = Path(str(candidate.get("preview_png") or ""))
    measurement_path = Path(str(candidate.get("measurement_json") or ""))
    candidate_dir = Path(str(candidate.get("candidate_dir") or ""))
    if not preview_path.exists() or not measurement_path.exists() or not candidate_dir:
        return
    try:
        measurement = json.loads(measurement_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    bboxes = measurement.get("bboxes") if isinstance(measurement, dict) else {}
    canvas = measurement.get("canvas") if isinstance(measurement, dict) else {}
    if not isinstance(bboxes, dict) or not isinstance(canvas, dict):
        return
    try:
        image = Image.open(preview_path).convert("RGB")
    except OSError:
        return
    visual_dir = candidate_dir / "visual_repair"
    crops_dir = visual_dir / "crops"
    overlays_dir = visual_dir / "overlays"
    try:
        crops_dir.mkdir(parents=True, exist_ok=True)
        overlays_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    cw = max(1, _safe_int(canvas.get("w_px"), default=image.width))
    ch = max(1, _safe_int(canvas.get("h_px"), default=image.height))
    scale_x = image.width / float(cw)
    scale_y = image.height / float(ch)
    primary_issue_id = str(payload.get("issue_id") or "")
    primary_issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
    crops: list[dict[str, Any]] = []
    primary_issues = [
        issue
        for issue in primary_issues
        if not _visual_repair_should_skip_issue(issue, primary_issue_id, ctx)
    ]
    for index, issue in enumerate(primary_issues[:8]):
        crop = _write_visual_repair_crop(
            ctx,
            image,
            bboxes,
            canvas,
            issue,
            role="primary",
            stage=stage,
            issue_id=primary_issue_id,
            issue_index=index,
            diagnostic_only=False,
            confidence="high",
            depends_on=[],
            out_dir=crops_dir,
            scale_x=scale_x,
            scale_y=scale_y,
        )
        if crop:
            crops.append(crop)
    blank_fill_required_targets = (
        payload.get("required_blank_fill_targets")
        if isinstance(payload.get("required_blank_fill_targets"), list) else
        []
    )
    if primary_issue_id != "paper_poster_html_editorial_flow_fill_failed":
        for index, target in enumerate(blank_fill_required_targets[:8]):
            if not isinstance(target, dict):
                continue
            issue = {
                "id": "blank_fill_required",
                "failure_kind": "blank_fill_required",
                "target_problem": "blank_source_or_section_fill",
                "acceptance_mode": "blank_fill",
                **target,
            }
            crop = _write_visual_repair_crop(
                ctx,
                image,
                bboxes,
                canvas,
                issue,
                role="blank_fill",
                stage="required_blank_fill",
                issue_id="paper_poster_html_blank_fill_required",
                issue_index=index,
                diagnostic_only=False,
                confidence="high",
                depends_on=[primary_issue_id] if primary_issue_id else [],
                out_dir=crops_dir,
                scale_x=scale_x,
                scale_y=scale_y,
            )
            if crop:
                crops.append(crop)
    secondary_entries: list[dict[str, Any]] = []
    all_overlays: list[dict[str, Any]] = []
    for diag_index, diagnostic in enumerate(payload.get("secondary_gate_issues") or []):
        if not isinstance(diagnostic, dict):
            continue
        diag_stage = str(diagnostic.get("stage") or f"secondary_{diag_index:02d}")
        diag_issue_id = str(diagnostic.get("issue_id") or "")
        raw_diag_issues = diagnostic.get("issues") if isinstance(diagnostic.get("issues"), list) else []
        diag_issues = [
            issue
            for issue in raw_diag_issues
            if not _visual_repair_should_skip_issue(issue, diag_issue_id, ctx)
        ]
        overlay_path = _write_visual_repair_secondary_overlay(
            ctx,
            image,
            bboxes,
            diag_issues,
            candidate_id=str(candidate.get("candidate_id") or ""),
            stage=diag_stage,
            issue_id=diag_issue_id,
            out_dir=overlays_dir,
            index=diag_index,
            scale_x=scale_x,
            scale_y=scale_y,
        )
        overlays: list[dict[str, Any]] = []
        if overlay_path:
            overlays.append({
                "role": "secondary",
                "diagnostic_only": True,
                "stage": diag_stage,
                "issue_id": diag_issue_id,
                "path": str(overlay_path),
                "relative_path": _relative_to_run_dir(ctx, overlay_path),
            })
            all_overlays.extend(overlays)
        diag_crops: list[dict[str, Any]] = []
        for issue_index, issue in enumerate(diag_issues[:4]):
            crop = _write_visual_repair_crop(
                ctx,
                image,
                bboxes,
                canvas,
                issue,
                role="secondary",
                stage=diag_stage,
                issue_id=diag_issue_id,
                issue_index=issue_index,
                diagnostic_only=True,
                confidence=str(diagnostic.get("confidence") or "medium"),
                depends_on=[
                    str(item)
                    for item in (diagnostic.get("depends_on") or [])
                    if str(item or "").strip()
                ],
                out_dir=crops_dir,
                scale_x=scale_x,
                scale_y=scale_y,
            )
            if crop:
                crops.append(crop)
                diag_crops.append(crop)
        secondary_entries.append({
            "diagnostic_only": True,
            "blocked_by_primary_issue_id": diagnostic.get("blocked_by_primary_issue_id"),
            "stage": diag_stage,
            "issue_id": diag_issue_id,
            "repair_route": diagnostic.get("repair_route"),
            "confidence": diagnostic.get("confidence"),
            "depends_on": diagnostic.get("depends_on") or [],
            "hint": diagnostic.get("hint"),
            "crops": [crop.get("id") for crop in diag_crops],
            "overlays": [overlay.get("relative_path") for overlay in overlays],
        })
    overlay_rel = candidate.get("validation_overlay_png_relative") or candidate.get("validation_overlay_png")
    packet = {
        "version": 1,
        "candidate_id": candidate.get("candidate_id"),
        "validation_stage": stage,
        "primary_issue_id": primary_issue_id,
        "preview_png": candidate.get("preview_png_relative") or candidate.get("preview_png"),
        "measurement_json": candidate.get("measurement_json_relative") or candidate.get("measurement_json"),
        "validation_overlay_png": overlay_rel,
        "canvas": canvas,
        "primary": {
            "issue_id": primary_issue_id,
            "repair_route": payload.get("repair_route"),
            "crops": [crop.get("id") for crop in crops if crop.get("role") in {"primary", "blank_fill"}],
        },
        "blank_fill_plan": payload.get("blank_fill_plan") if isinstance(payload.get("blank_fill_plan"), dict) else {},
        "required_blank_fill_targets": blank_fill_required_targets[:12],
        "secondary_diagnostics": secondary_entries,
        "crops": crops,
        "overlays": all_overlays,
    }
    packet_path = visual_dir / "packet.json"
    try:
        packet_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return
    candidate["visual_repair_dir"] = str(visual_dir)
    candidate["visual_repair_dir_relative"] = _relative_to_run_dir(ctx, visual_dir)
    candidate["visual_repair_packet_json"] = str(packet_path)
    candidate["visual_repair_packet_json_relative"] = _relative_to_run_dir(ctx, packet_path)
    payload["candidate_visual_repair_dir"] = candidate["visual_repair_dir"]
    payload["candidate_visual_repair_dir_relative"] = candidate["visual_repair_dir_relative"]
    payload["candidate_visual_repair_packet_json"] = candidate["visual_repair_packet_json"]
    payload["candidate_visual_repair_packet_json_relative"] = candidate["visual_repair_packet_json_relative"]


def _write_visual_repair_secondary_overlay(
    ctx: ToolContext,
    image: Image.Image,
    bboxes: dict[str, Any],
    issues: list[Any],
    *,
    candidate_id: str,
    stage: str,
    issue_id: str,
    out_dir: Path,
    index: int,
    scale_x: float,
    scale_y: float,
) -> Path | None:
    if not issues:
        return None
    overlay_h = 108
    annotated = Image.new("RGBA", (image.width, image.height + overlay_h), (255, 255, 255, 255))
    annotated.paste(image.convert("RGBA"), (0, overlay_h))
    draw = ImageDraw.Draw(annotated, "RGBA")
    title_font = _validation_overlay_font(22)
    small_font = _validation_overlay_font(14)
    draw.text((16, 14), f"Secondary diagnostic: {stage} / {issue_id}", fill=(17, 24, 39, 255), font=title_font)
    draw.text((16, 46), f"candidate={candidate_id} diagnostic_only=true", fill=(55, 65, 81, 255), font=small_font)
    draw.text((16, 70), _validation_issue_summary(issues[0]), fill=(55, 65, 81, 255), font=small_font)
    for target_index, target in enumerate(_validation_overlay_targets(issues, bboxes)[:16]):
        color = _VALIDATION_OVERLAY_COLORS[target_index % len(_VALIDATION_OVERLAY_COLORS)]
        _draw_validation_overlay_box(
            draw,
            target.get("bbox") if isinstance(target.get("bbox"), dict) else {},
            label=str(target.get("label") or ""),
            color=color,
            scale=scale_x,
            y_offset=overlay_h,
        )
    output_path = out_dir / f"secondary_{index:02d}_{_visual_repair_slug(stage)}_{_visual_repair_slug(issue_id)}.png"
    try:
        annotated.convert("RGB").save(output_path)
    except OSError:
        return None
    return output_path


def _write_visual_repair_crop(
    ctx: ToolContext,
    image: Image.Image,
    bboxes: dict[str, Any],
    canvas: dict[str, Any],
    issue: Any,
    *,
    role: str,
    stage: str,
    issue_id: str,
    issue_index: int,
    diagnostic_only: bool,
    confidence: str,
    depends_on: list[str],
    out_dir: Path,
    scale_x: float,
    scale_y: float,
) -> dict[str, Any] | None:
    if not isinstance(issue, dict):
        return None
    target = _visual_repair_target_for_issue(issue, bboxes)
    bbox = target.get("bbox_canvas") if isinstance(target.get("bbox_canvas"), dict) else None
    if not bbox:
        return None
    cw = max(1, _safe_int(canvas.get("w_px"), default=image.width))
    ch = max(1, _safe_int(canvas.get("h_px"), default=image.height))
    pad = 24
    padded = {
        "x": int(bbox.get("x") or 0) - pad,
        "y": int(bbox.get("y") or 0) - pad,
        "w": int(bbox.get("w") or 0) + pad * 2,
        "h": int(bbox.get("h") or 0) + pad * 2,
    }
    crop_canvas = _clip_bbox_to_canvas(padded, cw=cw, ch=ch)
    if not crop_canvas:
        return None
    crop_image = _visual_repair_image_bbox(crop_canvas, image.width, image.height, scale_x, scale_y)
    if not crop_image:
        return None
    target_slug = _visual_repair_slug(
        str(
            target.get("target_id")
            or issue.get("block_id")
            or issue.get("container_id")
            or issue.get("panel_id")
            or issue.get("source_id")
            or "target"
        )
    )
    file_name = (
        f"{role}_{issue_index:02d}_{_visual_repair_slug(stage)}_"
        f"{_visual_repair_slug(issue_id or str(issue.get('id') or 'issue'))}_{target_slug}.png"
    )[:180]
    if not file_name.endswith(".png"):
        file_name = file_name[:176] + ".png"
    output_path = out_dir / file_name
    try:
        image.crop((
            crop_image["x"],
            crop_image["y"],
            crop_image["x"] + crop_image["w"],
            crop_image["y"] + crop_image["h"],
        )).save(output_path)
    except OSError:
        return None
    crop_id = f"{role}_{issue_index:02d}_{target_slug}"
    return {
        "id": crop_id,
        "role": role,
        "diagnostic_only": diagnostic_only,
        "stage": stage,
        "issue_id": issue_id,
        "issue_index": issue_index,
        "failure_kind": issue.get("failure_kind") or issue.get("id"),
        "confidence": confidence,
        "depends_on": depends_on,
        "target": {key: value for key, value in target.items() if key != "bbox_canvas"},
        "bbox_canvas": bbox,
        "crop_bbox_canvas": crop_canvas,
        "bbox_image": crop_image,
        "padding_px": pad,
        "path": str(output_path),
        "relative_path": _relative_to_run_dir(ctx, output_path),
        "metrics": _visual_repair_metrics_from_issue(issue),
        "diagnostic_geometry": _visual_repair_diagnostic_geometry_from_issue(issue),
        "required_targets": _visual_repair_required_targets_from_issue(issue),
    }


def _visual_repair_should_skip_issue(issue: Any, issue_id: str, ctx: ToolContext) -> bool:
    if not isinstance(issue, dict):
        return False
    if issue_id != "paper_poster_html_source_wrap_missing":
        return False
    panel_role = str(issue.get("panel_role") or "").strip().lower().replace("-", "_").replace(" ", "_")
    panel_id = str(issue.get("panel_id") or "").strip().lower()
    if panel_role != "identity_header" and panel_id not in {"identity_header", "title_meta"}:
        return False
    source_id = str(issue.get("source_id") or "").strip()
    return bool(source_id and _is_explicit_identity_asset_source_id(source_id, ctx))


def _visual_repair_target_for_issue(issue: dict[str, Any], bboxes: dict[str, Any]) -> dict[str, Any]:
    for bbox_key, label_key in (
        ("blank_bbox_canvas", "flow_unit_id"),
        ("bbox", "bbox"),
        ("container_bbox", "container_id"),
        ("section_bbox", "section_id"),
    ):
        box = _bbox_only(issue.get(bbox_key) if isinstance(issue.get(bbox_key), dict) else None)
        if box:
            return {
                "target_source_field": bbox_key,
                "target_id": str(issue.get(label_key) or issue.get("block_id") or issue.get("id") or ""),
                "selector": _selector_for_visual_target(issue.get(label_key) or issue.get("block_id")),
                "bbox_canvas": box,
                **_visual_repair_issue_ids(issue),
            }
    if issue.get("same_flow_fill_required"):
        fields = (
            "flow_unit_id",
            "panel_id",
            "source_id",
            "asset_block_id",
            "block_id",
            "overflow_block_id",
            "container_id",
            "section_id",
            "source_block_id",
            "left_block_id",
            "right_block_id",
        )
    else:
        fields = (
        "block_id",
        "overflow_block_id",
        "container_id",
        "section_id",
        "flow_unit_id",
        "panel_id",
        "asset_block_id",
        "source_block_id",
        "left_block_id",
        "right_block_id",
        "source_id",
        )
    for field in fields:
        raw = str(issue.get(field) or "").strip()
        if not raw:
            continue
        box = _bbox_for_visual_target(raw, bboxes)
        if box:
            return {
                "target_source_field": field,
                "target_id": raw,
                "selector": _selector_for_visual_target(raw),
                "target_scope": issue.get("target_scope"),
                "bbox_canvas": box,
                **_visual_repair_issue_ids(issue),
            }
    return {"bbox_canvas": None, **_visual_repair_issue_ids(issue)}


def _bbox_for_visual_target(target_id: str, bboxes: dict[str, Any]) -> dict[str, int] | None:
    if isinstance(bboxes.get(target_id), dict):
        return _bbox_only(bboxes.get(target_id))
    if target_id.startswith(("ingest_fig_", "ingest_table_", "ingest_img_")):
        for key, value in bboxes.items():
            if str(key).startswith(f"visual_{target_id}") or str(key).endswith(target_id):
                box = _bbox_only(value if isinstance(value, dict) else None)
                if box:
                    return box
    return None


def _selector_for_visual_target(target_id: Any) -> str:
    raw = str(target_id or "").strip()
    if not raw:
        return ""
    if raw.startswith(("ingest_fig_", "ingest_table_", "ingest_img_")):
        return f'[data-source-id="{raw}"], [data-layer-id="{raw}"]'
    return f'[data-block-id="{raw}"]'


def _visual_repair_issue_ids(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        key: issue.get(key)
        for key in (
            "block_id",
            "overflow_block_id",
            "container_id",
            "section_id",
            "flow_unit_id",
            "panel_id",
            "source_id",
            "asset_block_id",
            "source_block_id",
            "left_block_id",
            "right_block_id",
            "target_scope",
            "asset_block_id",
            "target_kind",
            "insert_selector",
        )
        if issue.get(key) not in (None, "", [], {})
    }


def _visual_repair_metrics_from_issue(issue: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "bottom_overflow_px",
        "scroll_overflow_px",
        "source_panel_width_ratio",
        "source_panel_area_ratio",
        "object_fit_width_fill_ratio",
        "object_fit_area_fill_ratio",
        "panel_scroll_overflow_px",
        "required_source_height_px",
        "rendered_source_height_px",
        "side_text_coverage_ratio",
        "local_word_count",
        "words_to_add_min",
        "words_to_add_max",
        "remaining_safe_words",
        "safe_word_budget",
        "over_readout_budget",
        "visual_salience_score",
        "target_line_count",
        "coverage_gap",
    )
    return {key: issue.get(key) for key in keys if issue.get(key) not in (None, "", [], {})}


def _visual_repair_diagnostic_geometry_from_issue(issue: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "source_width_px",
        "source_height_px",
        "panel_width_px",
        "panel_height_px",
        "panel_client_height_px",
        "panel_scroll_height_px",
        "panel_scroll_overflow_px",
        "intrinsic_aspect_ratio",
        "wrapper_aspect_ratio",
        "object_fit_rendered_width_px",
        "object_fit_rendered_height_px",
        "object_fit_width_fill_ratio",
        "object_fit_height_fill_ratio",
        "object_fit_area_fill_ratio",
        "source_panel_width_ratio",
        "source_panel_area_ratio",
        "side_text_coverage_ratio",
        "blank_sidecar_height_ratio",
        "local_word_count",
        "target_problem",
        "repair_intent",
        "primary_repair_action",
        "threshold_gap",
        "threshold_gap_is_minor",
        "target_kind",
        "blank_bbox_canvas",
        "tail_gap_px",
        "usable_blank_px",
        "visual_salience_level",
        "blank_fill_severity",
        "required_repair_mode",
        "prose_fill_required",
        "compact_rebalance_required",
        "safe_primary_repair_action",
    )
    return {key: issue.get(key) for key in keys if issue.get(key) not in (None, "", [], {})}


def _visual_repair_required_targets_from_issue(issue: dict[str, Any]) -> dict[str, Any]:
    if issue.get("target_problem") == "readable_visual_wrapper_polish":
        targets = {
            "acceptance_mode": {
                "wrapper_polish": "near_miss_reference",
                "source_visual_geometry": "must_not_regress",
            },
            "target_problem": issue.get("target_problem"),
            "repair_intent": issue.get("repair_intent"),
            "primary_repair_action": issue.get("primary_repair_action"),
            "recommended_first_action": issue.get("recommended_first_action"),
            "must_not_regress_geometry": issue.get("must_not_regress_geometry") or {},
            "do_not_fix_by": issue.get("do_not_fix_by") or [],
            "soft_finalizable": issue.get("soft_finalizable"),
            "severity": issue.get("severity"),
        }
        return {key: value for key, value in targets.items() if value not in (None, "", [], {})}
    if issue.get("acceptance_mode") == "blank_fill" or issue.get("target_kind") in {
        "source_flow_side_lane",
        "source_flow_side_lane_tail",
        "section_tail_blank",
        "section_internal_gap_blank",
        "column_bottom_blank",
    }:
        blank_fill_acceptance = (
            "hard_compact_rebalance_target"
            if issue.get("compact_rebalance_required")
            else "hard_text_fill_target"
            if issue.get("prose_fill_required") is not False
            else "hard_local_rebalance_target"
        )
        targets = {
            "acceptance_mode": {
                "blank_fill": blank_fill_acceptance,
                "source_visual_geometry": "must_not_regress",
            },
            "target_kind": issue.get("target_kind"),
            "source_id": issue.get("source_id"),
            "flow_unit_id": issue.get("flow_unit_id"),
            "section_id": issue.get("section_id"),
            "column_id": issue.get("column_id"),
            "insert_selector": issue.get("insert_selector"),
            "insert_position": issue.get("insert_position"),
            "words_to_add_min": issue.get("words_to_add_min"),
            "words_to_add_max": issue.get("words_to_add_max"),
            "remaining_safe_words": issue.get("remaining_safe_words"),
            "safe_word_budget": issue.get("safe_word_budget"),
            "over_readout_budget": issue.get("over_readout_budget"),
            "blank_fill_severity": issue.get("blank_fill_severity"),
            "visual_salience_score": issue.get("visual_salience_score"),
            "visual_salience_level": issue.get("visual_salience_level"),
            "required_repair_mode": issue.get("required_repair_mode"),
            "prose_fill_required": issue.get("prose_fill_required"),
            "compact_rebalance_required": issue.get("compact_rebalance_required"),
            "safe_primary_repair_action": issue.get("safe_primary_repair_action"),
            "target_line_count": issue.get("target_line_count"),
            "required_min_words": issue.get("required_min_words"),
            "required_min_side_text_coverage_ratio": issue.get("required_min_side_text_coverage_ratio"),
            "allowed_filler_block_ids": issue.get("allowed_filler_block_ids") or [],
            "primary_repair_action": issue.get("primary_repair_action"),
            "required_dom_shape": issue.get("required_dom_shape"),
            "content_requirements": issue.get("content_requirements") or [],
            "must_not_regress_geometry": issue.get("must_not_regress_geometry") or issue.get("readable_visual_geometry") or {},
            "do_not_fix_by": [
                "widening_image_only",
                "shrinking_global_typography",
                "changing_poster_columns",
                "changing_canvas_or_template",
                "decorative_filler",
            ],
        }
        return {key: value for key, value in targets.items() if value not in (None, "", [], {})}
    if issue.get("acceptance_mode") == "same_flow_fill":
        targets = {
            "acceptance_mode": {
                "same_flow_fill": "hard_flow_fill_target",
                "source_visual_geometry": "must_not_regress",
            },
            "target_scope": issue.get("target_scope"),
            "preserve_current_visual_size": bool(issue.get("preserve_current_visual_size")),
            "required_dom_shape": issue.get("required_dom_shape"),
            "same_flow_fill_targets": issue.get("same_flow_fill_targets") or {},
            "must_not_regress_geometry": issue.get("must_not_regress_geometry") or {},
        }
        for key in (
            "target_problem",
            "repair_intent",
            "primary_repair_action",
            "recommended_first_action",
            "threshold_gap",
            "threshold_gap_is_minor",
            "do_not_fix_by",
        ):
            if issue.get(key) not in (None, "", [], {}):
                targets[key] = issue.get(key)
        return {key: value for key, value in targets.items() if value not in (None, "", [], {})}
    targets = {
        key: issue.get(key)
        for key in (
            "required_panel_width_ratio",
            "required_source_area_ratio",
            "required_object_fit_fill_ratio",
            "required_object_fit_area_ratio",
            "required_min_side_text_coverage_ratio",
            "required_min_words",
        )
        if issue.get(key) not in (None, "", [], {})
    }
    required_height = issue.get("required_source_height_px")
    if required_height not in (None, "", [], {}):
        targets["height_px_reference"] = required_height
        targets["height_px_reference_is_adaptive"] = True
        targets["height_px_interpretation"] = (
            "Diagnostic lower-bound reference only; prefer satisfying width/area/object-fit ratios "
            "with local reflow and do not force this pixel height if it creates same-panel overflow."
        )
    acceptance_mode: dict[str, str] = {}
    if issue.get("required_panel_width_ratio") not in (None, "", [], {}):
        acceptance_mode["required_panel_width_ratio"] = "hard_ratio_target"
    if issue.get("required_source_area_ratio") not in (None, "", [], {}):
        acceptance_mode["required_source_area_ratio"] = "hard_ratio_target"
    if issue.get("required_object_fit_fill_ratio") not in (None, "", [], {}):
        acceptance_mode["required_object_fit_fill_ratio"] = "hard_wrapper_fill_target"
    if issue.get("required_object_fit_area_ratio") not in (None, "", [], {}):
        acceptance_mode["required_object_fit_area_ratio"] = "hard_wrapper_fill_target"
    if issue.get("required_min_side_text_coverage_ratio") not in (None, "", [], {}):
        acceptance_mode["required_min_side_text_coverage_ratio"] = "hard_when_floated_sidecar"
    if required_height not in (None, "", [], {}):
        acceptance_mode["height_px_reference"] = "adaptive_diagnostic_reference"
    if acceptance_mode:
        targets["acceptance_mode"] = acceptance_mode
    for key in ("target_problem", "repair_intent", "primary_repair_action", "recommended_first_action", "threshold_gap", "threshold_gap_is_minor", "do_not_fix_by"):
        if issue.get(key) not in (None, "", [], {}):
            targets[key] = issue.get(key)
    recommended_layout = issue.get("recommended_layout")
    if recommended_layout not in (None, "", [], {}):
        targets["recommended_layout"] = recommended_layout
    return targets


def _visual_repair_image_bbox(
    bbox: dict[str, int],
    image_w: int,
    image_h: int,
    scale_x: float,
    scale_y: float,
) -> dict[str, int] | None:
    x1 = max(0, int(round(_safe_int(bbox.get("x")) * scale_x)))
    y1 = max(0, int(round(_safe_int(bbox.get("y")) * scale_y)))
    x2 = min(image_w, int(round((_safe_int(bbox.get("x")) + _safe_int(bbox.get("w"))) * scale_x)))
    y2 = min(image_h, int(round((_safe_int(bbox.get("y")) + _safe_int(bbox.get("h"))) * scale_y)))
    if x2 <= x1 or y2 <= y1:
        return None
    return {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1}


def _visual_repair_slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip().lower()).strip("-._")
    return (text or "item")[:64]


_VALIDATION_OVERLAY_COLORS = (
    (220, 38, 38, 255),
    (37, 99, 235, 255),
    (22, 163, 74, 255),
    (245, 158, 11, 255),
    (147, 51, 234, 255),
    (14, 165, 233, 255),
)


def _validation_overlay_font(size: int) -> ImageFont.ImageFont:
    for path in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _validation_issue_summary(issue: Any) -> str:
    if not isinstance(issue, dict):
        return str(issue)[:220]
    parts = [
        str(issue.get("id") or "issue"),
        f"container={issue.get('container_id')}" if issue.get("container_id") else "",
        f"section={issue.get('section_id')}" if issue.get("section_id") else "",
        f"block={issue.get('block_id') or issue.get('overflow_block_id')}" if issue.get("block_id") or issue.get("overflow_block_id") else "",
        f"bottom_overflow={issue.get('bottom_overflow_px')}px" if issue.get("bottom_overflow_px") not in (None, "") else "",
    ]
    return " | ".join(part for part in parts if part)[:260]


def _validation_overlay_targets(issues: list[Any], bboxes: dict[str, Any]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int, int, int]] = set()

    def add(label: str, bbox: Any) -> None:
        box = _bbox_only(bbox)
        if not box:
            return
        key = (
            label,
            _safe_int(box.get("x"), default=0),
            _safe_int(box.get("y"), default=0),
            _safe_int(box.get("w"), default=0),
            _safe_int(box.get("h"), default=0),
        )
        if key in seen:
            return
        seen.add(key)
        targets.append({"label": label, "bbox": box})

    for issue in issues:
        if not isinstance(issue, dict):
            continue
        issue_label = str(issue.get("id") or "issue")
        add("blank-fill:" + issue_label, issue.get("blank_bbox_canvas"))
        add(issue_label, issue.get("bbox"))
        add("container:" + str(issue.get("container_id") or ""), issue.get("container_bbox"))
        add("section:" + str(issue.get("section_id") or ""), issue.get("section_bbox"))
        for key in (
            "block_id",
            "overflow_block_id",
            "container_id",
            "section_id",
            "flow_unit_id",
            "panel_id",
            "asset_block_id",
            "source_block_id",
            "left_block_id",
            "right_block_id",
            "source_id",
        ):
            block_id = str(issue.get(key) or "").strip()
            if not block_id:
                continue
            bbox = bboxes.get(block_id)
            if not isinstance(bbox, dict):
                bbox = _bbox_for_visual_target(block_id, bboxes)
            if isinstance(bbox, dict):
                add(f"{key}:{block_id}", bbox)
    return targets


def _draw_validation_overlay_box(
    draw: ImageDraw.ImageDraw,
    bbox: dict[str, Any],
    *,
    label: str,
    color: tuple[int, int, int, int],
    scale: float,
    y_offset: int,
) -> None:
    x = int(round(_safe_float(bbox.get("x")) * scale))
    y = int(round(_safe_float(bbox.get("y")) * scale)) + y_offset
    w = int(round(_safe_float(bbox.get("w")) * scale))
    h = int(round(_safe_float(bbox.get("h")) * scale))
    if w <= 0 or h <= 0:
        return
    draw.rectangle((x, y, x + w, y + h), outline=color, width=5)
    if not label:
        return
    font = _validation_overlay_font(13)
    tx = max(0, x + 5)
    ty = max(y_offset, y - 22)
    label_text = label[:80]
    text_box = draw.textbbox((tx, ty), label_text, font=font)
    draw.rectangle(
        (text_box[0] - 3, text_box[1] - 2, text_box[2] + 3, text_box[3] + 2),
        fill=(255, 255, 255, 235),
        outline=color,
        width=1,
    )
    draw.text((tx, ty), label_text, fill=color, font=font)


def _attach_secondary_gate_diagnostics(
    payload: dict[str, Any],
    soup: BeautifulSoup,
    css: str,
    ctx: ToolContext,
    bboxes: dict[str, dict[str, Any]],
    canvas: dict[str, Any],
    *,
    primary_stage: str,
) -> None:
    primary_issue_id = str(payload.get("issue_id") or primary_stage or "")
    stage_order = [
        "heading_flow_overflow",
        "source_visual_wrap",
        "typography_contract",
        "source_visual_size",
        "editorial_flow_fill",
    ]
    try:
        primary_index = stage_order.index(primary_stage)
    except ValueError:
        primary_index = -1
    shadow_ctx = _shadow_tool_context(ctx)
    diagnostics: list[dict[str, Any]] = []
    collectors = [
        (
            "heading_flow_overflow",
            lambda: _designer_owned_heading_flow_overflow_error(soup, bboxes, canvas),
            "medium",
        ),
        (
            "source_visual_wrap",
            lambda: _source_wrap_error(soup, css, shadow_ctx),
            "medium",
        ),
        (
            "typography_contract",
            lambda: _paper_poster_typography_contract_error(soup, bboxes, shadow_ctx),
            "medium",
        ),
        (
            "source_visual_size",
            lambda: _source_visual_size_error(soup, bboxes, shadow_ctx),
            "low" if primary_issue_id in {
                "paper_poster_html_local_flow_overflow",
                "paper_poster_html_heading_flow_overflow",
                "paper_poster_html_row_allocation_density_regression",
            } else "medium",
        ),
    ]
    if not _active_reference_style_contract(ctx):
        collectors.append((
            "editorial_flow_fill",
            lambda: _editorial_flow_fill_error(soup, bboxes, canvas, shadow_ctx),
            "low" if primary_issue_id in {
                "paper_poster_html_local_flow_overflow",
                "paper_poster_html_heading_flow_overflow",
                "paper_poster_html_row_allocation_density_regression",
            } else "medium",
        ))
    for stage, collect, confidence in collectors:
        try:
            stage_index = stage_order.index(stage)
        except ValueError:
            stage_index = 999
        if stage_index <= primary_index:
            continue
        try:
            result = collect()
        except Exception as exc:
            log(
                "paper_poster_html.secondary_gate_diagnostic_failed",
                stage=stage,
                error=f"{type(exc).__name__}: {exc}",
            )
            continue
        diagnostic = _secondary_gate_diagnostic_from_result(
            result,
            stage=stage,
            primary_issue_id=primary_issue_id,
            confidence=confidence,
        )
        if diagnostic:
            diagnostics.append(diagnostic)
    if not diagnostics:
        return
    payload["primary_blocking_issue_id"] = primary_issue_id
    payload["secondary_gate_issues"] = diagnostics[:8]
    payload["all_gate_diagnostics"] = [
        {
            "stage": primary_stage,
            "issue_id": primary_issue_id,
            "primary": True,
            "diagnostic_only": False,
        },
        *diagnostics[:8],
    ]
    _attach_required_blank_fill_followup(payload, diagnostics[:8], primary_issue_id=primary_issue_id)
    log(
        "paper_poster_html.secondary_gate_diagnostics",
        primary_issue_id=primary_issue_id,
        issue_count=len(diagnostics),
        stages=[item.get("stage") for item in diagnostics[:8]],
    )


def _shadow_tool_context(ctx: ToolContext) -> ToolContext:
    shadow = ToolContext(
        settings=ctx.settings,
        run_dir=ctx.run_dir,
        layers_dir=ctx.layers_dir,
        run_id=f"{ctx.run_id}-secondary-diagnostics",
        cancellation_token=ctx.cancellation_token,
    )
    shadow.state = dict(ctx.state)
    return shadow


def _secondary_gate_diagnostic_from_result(
    result: ToolResultRecord | None,
    *,
    stage: str,
    primary_issue_id: str,
    confidence: str,
) -> dict[str, Any] | None:
    if not result or result.status != "error" or not isinstance(result.payload, dict):
        return None
    issue_id = str(result.payload.get("issue_id") or "")
    if not issue_id:
        return None
    issues = result.payload.get("issues") if isinstance(result.payload.get("issues"), list) else []
    diagnostic = {
        "diagnostic_only": True,
        "blocked_by_primary_issue_id": primary_issue_id,
        "stage": stage,
        "issue_id": issue_id,
        "repair_route": result.payload.get("repair_route"),
        "confidence": confidence,
        "depends_on": [primary_issue_id] if primary_issue_id else [],
        "issues": issues[:4],
        "hint": result.payload.get("hint"),
    }
    for key in (
        "blank_fill_plan",
        "required_blank_fill_targets",
        "blank_fill_required",
        "post_overflow_required_followup",
        "required_co_repair",
    ):
        value = result.payload.get(key)
        if value not in (None, "", [], {}):
            diagnostic[key] = value
    return diagnostic


def _attach_required_blank_fill_followup(
    payload: dict[str, Any],
    diagnostics: list[dict[str, Any]],
    *,
    primary_issue_id: str,
) -> None:
    plan = _merge_blank_fill_plans([
        payload.get("blank_fill_plan") if isinstance(payload.get("blank_fill_plan"), dict) else {},
        *[
            diagnostic.get("blank_fill_plan")
            for diagnostic in diagnostics
            if isinstance(diagnostic.get("blank_fill_plan"), dict)
        ],
    ])
    targets = plan.get("targets") if isinstance(plan.get("targets"), list) else []
    if not targets:
        return
    required_targets = _required_blank_fill_targets(plan)
    payload["blank_fill_plan"] = plan
    required_keys = {_blank_fill_target_key(target) for target in required_targets if isinstance(target, dict)}
    _finalize_advisory_blank_fill_targets(targets, required_keys)
    advisory_targets = [
        target for target in targets
        if isinstance(target, dict) and _blank_fill_target_key(target) not in required_keys
    ]
    if advisory_targets:
        payload["advisory_blank_fill_targets"] = advisory_targets[:12]
    if not required_targets:
        payload["blank_fill_required"] = False
        return
    required_plan = {**plan, "targets": required_targets[:12], "required_targets": required_targets[:12], "blank_fill_required": True}
    payload["required_blank_fill_targets"] = required_targets[:12]
    payload["blank_fill_required"] = True
    if primary_issue_id in {
        "paper_poster_html_local_flow_overflow",
        "paper_poster_html_row_allocation_density_regression",
        "paper_poster_html_post_overflow_density_conservation_failed",
    }:
        payload["post_overflow_required_followup"] = {
            "blank_fill": required_plan,
            "reason": "overflow repair must restore local density after fit is cleared",
        }
        return
    if primary_issue_id != "paper_poster_html_editorial_flow_fill_failed":
        payload["required_co_repair"] = {
            **(payload.get("required_co_repair") if isinstance(payload.get("required_co_repair"), dict) else {}),
            "blank_fill": required_plan,
            "reason": "high-confidence blank source/section fill target should be repaired with the primary blocker",
        }


def _required_blank_fill_targets(plan: dict[str, Any]) -> list[dict[str, Any]]:
    targets = plan.get("targets") if isinstance(plan.get("targets"), list) else []
    required: list[dict[str, Any]] = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        kind = str(target.get("target_kind") or "")
        local_words = _safe_int(target.get("local_word_count"), default=0)
        words_min = _safe_int(target.get("words_to_add_min"), default=0)
        remaining_safe = _blank_fill_remaining_safe_words(target, kind=kind, local_words=local_words)
        visually_obvious = _blank_fill_target_visually_obvious(target, kind=kind)
        if not bool(target.get("required_co_repair_eligible", True)) and not visually_obvious:
            continue
        compact_rebalance_required = _blank_fill_compact_rebalance_required(
            target,
            kind=kind,
            words_min=words_min,
            remaining_safe_words=remaining_safe,
        )
        if kind in {"source_flow_side_lane", "source_flow_side_lane_tail"} and not compact_rebalance_required:
            if remaining_safe < 8 or (words_min and words_min > remaining_safe):
                continue
        promotion = str(target.get("promotion") or target.get("blank_fill_severity") or "").strip().lower()
        if not promotion:
            promotion = "required" if (compact_rebalance_required or visually_obvious) else "advisory"
        if promotion == "advisory" and not compact_rebalance_required and not visually_obvious:
            continue
        if kind == "source_flow_side_lane":
            _finalize_blank_fill_target_routing(
                target,
                required=True,
                compact_rebalance_required=compact_rebalance_required,
                words_min=words_min,
                remaining_safe_words=remaining_safe,
            )
            required.append(target)
            continue
        if kind == "source_flow_side_lane_tail":
            lane_gap = _safe_int(target.get("lane_tail_gap_px"), default=0)
            required_gap = _safe_int(target.get("required_blank_fill_gap_px"), default=90)
            near_required_gap = max(1, int(math.floor(required_gap * 0.85)))
            if (
                visually_obvious
                and lane_gap >= near_required_gap
                and (compact_rebalance_required or (remaining_safe >= 8 and words_min <= remaining_safe))
            ):
                _finalize_blank_fill_target_routing(
                    target,
                    required=True,
                    compact_rebalance_required=compact_rebalance_required,
                    words_min=words_min,
                    remaining_safe_words=remaining_safe,
                )
                required.append(target)
            continue
        if kind in {"section_tail_blank", "section_internal_gap_blank"}:
            confidence = str(target.get("tail_gap_confidence") or "medium")
            words_min = _safe_int(target.get("words_to_add_min"), default=999)
            tail_gap = _safe_int(target.get("tail_gap_px") or target.get("internal_gap_px"), default=0)
            required_blank = _safe_int(target.get("required_blank_fill_gap_px"), default=60)
            if (
                visually_obvious
                and confidence == "high"
                and tail_gap >= required_blank
                and (words_min <= 24 or compact_rebalance_required)
            ):
                _finalize_blank_fill_target_routing(
                    target,
                    required=True,
                    compact_rebalance_required=compact_rebalance_required,
                    words_min=words_min,
                    remaining_safe_words=remaining_safe,
                )
                required.append(target)
    ranked_required = sorted(required, key=_blank_fill_target_salience_sort_key)
    required = ranked_required[:4]
    required_keys = {_blank_fill_target_key(target) for target in required}
    for rank, target in enumerate(ranked_required, start=1):
        target["visual_salience_rank"] = rank
        if _blank_fill_target_key(target) not in required_keys:
            target["promotion"] = "advisory"
            target["blank_fill_severity"] = "advisory"
            target["required_co_repair_eligible"] = False
            target["required_demoted_reason"] = "lower_visual_salience_than_required_limit"
            _finalize_blank_fill_target_routing(
                target,
                required=False,
                compact_rebalance_required=False,
                words_min=_safe_int(target.get("words_to_add_min"), default=0),
                remaining_safe_words=_blank_fill_remaining_safe_words(
                    target,
                    kind=str(target.get("target_kind") or ""),
                    local_words=_safe_int(target.get("local_word_count"), default=0),
                ),
            )
    return required


def _blank_fill_remaining_safe_words(target: dict[str, Any], *, kind: str, local_words: int) -> int:
    if target.get("remaining_safe_words") not in (None, "", [], {}):
        return max(0, _safe_int(target.get("remaining_safe_words"), default=0))
    if kind in {"source_flow_side_lane", "source_flow_side_lane_tail"}:
        return max(0, _MAX_SOURCE_FLOW_EXPLANATION_WORDS - local_words - 8)
    if kind in {"section_tail_blank", "section_internal_gap_blank"}:
        return 24
    return 0


def _blank_fill_target_visually_obvious(target: dict[str, Any], *, kind: str) -> bool:
    score = _safe_float(target.get("visual_salience_score"), default=0.0)
    if kind == "source_flow_side_lane":
        return score >= 0.56
    if kind == "source_flow_side_lane_tail":
        lane_gap = _safe_int(target.get("lane_tail_gap_px"), default=0)
        required_gap = _safe_int(target.get("required_blank_fill_gap_px"), default=90)
        near_required_gap = max(1, int(math.floor(required_gap * 0.85)))
        return score >= 0.56 and lane_gap >= near_required_gap
    if kind in {"section_tail_blank", "section_internal_gap_blank"}:
        confidence = str(target.get("tail_gap_confidence") or "medium")
        tail_gap = _safe_int(target.get("tail_gap_px") or target.get("internal_gap_px"), default=0)
        required_gap = _safe_int(target.get("required_blank_fill_gap_px"), default=60)
        return confidence == "high" and score >= 0.56 and tail_gap >= required_gap
    return False


def _finalize_advisory_blank_fill_targets(
    targets: list[dict[str, Any]],
    required_keys: set[tuple[str, str, str]],
) -> None:
    for target in targets:
        if not isinstance(target, dict) or _blank_fill_target_key(target) in required_keys:
            continue
        kind = str(target.get("target_kind") or "")
        local_words = _safe_int(target.get("local_word_count"), default=0)
        _finalize_blank_fill_target_routing(
            target,
            required=False,
            compact_rebalance_required=False,
            words_min=_safe_int(target.get("words_to_add_min"), default=0),
            remaining_safe_words=_blank_fill_remaining_safe_words(target, kind=kind, local_words=local_words),
        )


def _blank_fill_compact_rebalance_required(
    target: dict[str, Any],
    *,
    kind: str,
    words_min: int,
    remaining_safe_words: int,
) -> bool:
    if bool(target.get("compact_rebalance_required")):
        return True
    repair_mode = str(
        target.get("required_repair_mode")
        or target.get("safe_primary_repair_action")
        or target.get("primary_repair_action")
        or ""
    )
    if any(marker in repair_mode for marker in ("compact", "rebalance", "stack", "reduce")):
        return True
    over_budget = (
        bool(target.get("over_readout_budget"))
        or remaining_safe_words < 8
        or (words_min > 0 and words_min > remaining_safe_words)
    )
    if kind in {"source_flow_side_lane", "source_flow_side_lane_tail"} and over_budget:
        return _blank_fill_target_visually_obvious(target, kind=kind)
    if kind in {"section_tail_blank", "section_internal_gap_blank"} and words_min > 24:
        return _blank_fill_target_visually_obvious(target, kind=kind)
    return False


def _finalize_blank_fill_target_routing(
    target: dict[str, Any],
    *,
    required: bool,
    compact_rebalance_required: bool,
    words_min: int,
    remaining_safe_words: int,
) -> None:
    kind = str(target.get("target_kind") or "")
    target["blank_fill_severity"] = "required" if required else "advisory"
    target["promotion"] = "required" if required else "advisory"
    safe_words = max(0, remaining_safe_words)
    target["remaining_safe_words"] = safe_words
    target["safe_word_budget"] = safe_words
    budget_checked_kind = kind in {"source_flow_side_lane", "source_flow_side_lane_tail", "section_tail_blank", "section_internal_gap_blank"}
    target["over_readout_budget"] = bool(
        target.get("over_readout_budget")
        or (budget_checked_kind and words_min > 0 and words_min > safe_words)
    )
    score = _safe_float(target.get("visual_salience_score"), default=0.0)
    if target.get("visual_salience_level") in (None, ""):
        target["visual_salience_level"] = _blank_fill_visual_salience_level(score)
    if not required:
        target["required_repair_mode"] = str(target.get("required_repair_mode") or "advisory")
        target["prose_fill_required"] = False
        target["compact_rebalance_required"] = False
        return
    current_mode = str(target.get("required_repair_mode") or "").strip()
    if compact_rebalance_required:
        if kind in {"source_flow_side_lane", "source_flow_side_lane_tail"}:
            target["required_repair_mode"] = "compact_rebalance_source_flow"
            target["required_repair_reason"] = "visible_blank_exceeds_safe_readout_budget"
            target["required_repair_modes"] = [
                "compact_existing_readout",
                "rebalance_native_rows",
                "stack_asset_and_readout",
                "reduce_flow_unit_or_section_height",
            ]
        elif kind in {"section_tail_blank", "section_internal_gap_blank"}:
            target["required_repair_mode"] = (
                "compact_rebalance_section_internal_gap"
                if kind == "section_internal_gap_blank" else
                "compact_rebalance_section_tail"
            )
            target["required_repair_reason"] = (
                "visible_internal_gap_too_large_for_prose_fill"
                if kind == "section_internal_gap_blank" else
                "visible_tail_blank_too_large_for_prose_fill"
            )
            target["required_repair_modes"] = [
                "add_native_metric_rows",
                "rebalance_section_height",
                "reduce_section_height_to_visual_section",
            ]
        else:
            target["required_repair_mode"] = current_mode or "compact_rebalance"
            target["required_repair_modes"] = ["compact_rebalance"]
    elif current_mode:
        target["required_repair_mode"] = current_mode
    elif kind in {"source_flow_side_lane", "source_flow_side_lane_tail"}:
        target["required_repair_mode"] = "prose_or_native_flow_fill"
        target["required_repair_modes"] = ["append_direct_sibling_source_readout", "add_native_metric_rows"]
    elif kind in {"section_tail_blank", "section_internal_gap_blank"}:
        target["required_repair_mode"] = "add_native_metric_rows"
        target["required_repair_modes"] = ["add_native_metric_rows"]
    else:
        target["required_repair_mode"] = "local_blank_fill"
        target["required_repair_modes"] = ["local_blank_fill"]
    target["compact_rebalance_required"] = bool(compact_rebalance_required)
    target["prose_fill_required"] = bool(not compact_rebalance_required and words_min > 0)


def _blank_fill_target_salience_sort_key(target: dict[str, Any]) -> tuple[float, int]:
    return (-_safe_float(target.get("visual_salience_score"), default=0.0), -_blank_fill_target_area(target))


def _blank_fill_target_area(target: dict[str, Any]) -> int:
    blank_box = target.get("blank_bbox_canvas")
    if not isinstance(blank_box, dict):
        return 0
    return max(0, _safe_int(blank_box.get("w"), default=0)) * max(0, _safe_int(blank_box.get("h"), default=0))


def _blank_fill_target_key(target: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(target.get("target_kind") or ""),
        str(target.get("flow_unit_id") or target.get("section_id") or target.get("column_id") or ""),
        str(target.get("source_id") or target.get("asset_block_id") or ""),
    )


def _blank_fill_visual_salience_score(
    target_kind: str,
    blank_box: dict[str, int] | None,
    *,
    cw: int,
    ch: int,
    usable_blank_px: int = 0,
    over_readout_budget: bool = False,
    required_eligible: bool = False,
) -> float:
    area_ratio = 0.0
    if blank_box:
        area_ratio = _blank_fill_target_area({"blank_bbox_canvas": blank_box}) / max(1, cw * ch)
    kind_weight = {
        "source_flow_side_lane": 0.44,
        "source_flow_side_lane_tail": 0.38,
        "section_tail_blank": 0.28,
        "section_internal_gap_blank": 0.32,
        "column_bottom_blank": 0.16,
    }.get(target_kind, 0.10)
    score = kind_weight + min(0.34, area_ratio * 5.0) + min(0.16, max(0, usable_blank_px) / max(1, ch) * 1.5)
    if required_eligible:
        score += 0.08
    return round(max(0.0, min(score, 1.0)), 3)


def _blank_fill_visual_salience_level(score: float) -> str:
    if score >= 0.65:
        return "high"
    if score >= 0.45:
        return "medium"
    return "low"


def _merge_blank_fill_plans(plans: list[Any]) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        for target in plan.get("targets") or []:
            if not isinstance(target, dict):
                continue
            key = _blank_fill_target_key(target)
            if key in seen:
                continue
            seen.add(key)
            targets.append(target)
    if not targets:
        return {}
    required_targets = _required_blank_fill_targets({"targets": targets})
    required_keys = {_blank_fill_target_key(target) for target in required_targets if isinstance(target, dict)}
    _finalize_advisory_blank_fill_targets(targets, required_keys)
    advisory_targets = [
        target for target in sorted(targets, key=_blank_fill_target_salience_sort_key)
        if isinstance(target, dict) and _blank_fill_target_key(target) not in required_keys
    ]
    suppressed_targets = [
        target for target in advisory_targets
        if isinstance(target, dict)
        and (
            target.get("required_co_repair_eligible") is False
            or str(target.get("promotion") or target.get("blank_fill_severity") or "") == "advisory"
        )
    ]
    return {
        "version": 1,
        "normalization_version": 1,
        "blank_fill_required": bool(required_targets),
        "required_target_count": len(required_targets),
        "advisory_target_count": len(advisory_targets),
        "suppressed_target_count": len(suppressed_targets),
        "targets": targets[:12],
        "required_targets": required_targets[:12],
        "advisory_targets": advisory_targets[:12],
        "suppressed_targets": suppressed_targets[:12],
        "instructions": (
            "Repair each target locally. Add concise source-backed facts only when prose_fill_required is true; "
            "otherwise use compact native rows, readout rebalance, stacking, or local section-height reduction. "
            "Do not invent facts, resize the canvas, change global columns, or solve blank lanes by merely enlarging images."
        ),
    }


def _score_and_maybe_lock_html_first_candidate(
    ctx: ToolContext,
    candidate: dict[str, Any],
    *,
    status: str,
    stage: str,
    payload: dict[str, Any] | None,
) -> None:
    score, reasons = _score_html_first_candidate(ctx, candidate, status=status, stage=stage, payload=payload)
    candidate["candidate_score"] = score
    candidate["candidate_score_reasons"] = reasons
    _update_html_first_best_candidate(ctx, candidate)
    if status == "validation_error" and _html_first_candidate_is_repairable_base(stage, payload, score):
        _update_html_first_locked_base_candidate(ctx, candidate)


def _score_html_first_candidate(
    ctx: ToolContext,
    candidate: dict[str, Any],
    *,
    status: str,
    stage: str,
    payload: dict[str, Any] | None,
) -> tuple[int, list[str]]:
    body_html = _read_candidate_text(candidate, "body_html")
    css = _read_candidate_text(candidate, "style_css")
    soup = BeautifulSoup(body_html, "html.parser")
    score = 0
    reasons: list[str] = []
    if status == "accepted":
        score += 1000
        reasons.append("accepted_design_spec")
    elif status == "validation_error":
        score += 100
        reasons.append("validation_near_miss")
    stage_adjustments = {
        "local_flow_overflow": (170, "local_overflow_repairable"),
        "editorial_flow_fill": (140, "editorial_fill_repairable"),
        "narrow_math_container": (80, "narrow_math_repairable"),
        "row_allocation_density": (-130, "row_allocation_density_invalid"),
        "designer_owned_canvas_shell": (70, "shell_repairable"),
        "source_visual_size": (25, "source_visual_size_blocked"),
        "block_boundary": (-20, "block_boundary"),
        "identity_header_only": (-180, "identity_header_invalid"),
        "reference_style_contract": (-260, "reference_style_contract_invalid"),
        "editorial_flow_shape": (-220, "editorial_shape_invalid"),
        "panel_flow_shape": (-180, "panel_flow_shape_invalid"),
        "source_coverage": (-240, "source_coverage_invalid"),
        "source_visual_wrap": (-140, "source_wrap_invalid"),
    }
    delta, reason = stage_adjustments.get(stage, (0, ""))
    if delta:
        score += delta
        reasons.append(reason)
    if soup.select_one(".editorial-poster,[data-layout-mode='editorial-flow']"):
        score += 80
        reasons.append("editorial_flow_root")
    column_count = len(soup.select(".poster-column,[data-column-id]"))
    expected_column_count = _reference_region_count(ctx) or 3
    if column_count == expected_column_count:
        score += 100
        reasons.append(
            "three_columns"
            if expected_column_count == 3
            else f"expected_columns_{expected_column_count}"
        )
    elif column_count:
        score -= abs(column_count - expected_column_count) * 35
        reasons.append(f"column_count_{column_count}")
    section_count = len(soup.select(".poster-section"))
    if 3 <= section_count <= 9:
        score += 55
        reasons.append("conference_section_count")
    elif section_count:
        score -= 35
        reasons.append(f"section_count_{section_count}")
    flow_count = len(soup.select(".figure-flow-unit,.source-flow-unit"))
    if flow_count:
        score += min(90, flow_count * 15)
        reasons.append(f"source_flow_units_{flow_count}")
    source_count = len(soup.find_all(attrs={"data-source-id": True}))
    if source_count:
        score += min(70, source_count * 8)
        reasons.append(f"source_bound_nodes_{source_count}")
    if re.search(r"\b(?:github|hugging\s*face|project\s+page)\b", soup.get_text(" ", strip=True), flags=re.IGNORECASE):
        score += 25
        reasons.append("project_resource_links")
    if ".poster-grid" in css or soup.select_one(".poster-grid"):
        score -= 100
        reasons.append("legacy_poster_grid")
    if len(soup.select(".flow-panel")) >= 5:
        score -= 80
        reasons.append("legacy_flow_panel_stack")
    issue_id = ""
    issues: list[Any] = []
    if isinstance(payload, dict):
        issue_id = str(payload.get("issue_id") or "")
        issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
    if issue_id == "paper_poster_html_source_visual_too_small":
        score -= 80
        reasons.append("source_visual_too_small")
    if issue_id == "paper_poster_html_editorial_flow_shape_failed":
        score -= 180
        reasons.append("editorial_flow_shape_failed")
    if issue_id == "paper_poster_html_reference_style_contract_failed":
        score -= 260
        reasons.append("reference_style_contract_failed")
    if issue_id == "paper_poster_html_designer_flow_canvas_overflow":
        if _issues_are_pure_scroll_overflow(issues):
            score += 65
            reasons.append("pure_scroll_overflow_near_miss")
        else:
            score -= 120
            reasons.append("global_shell_overflow")
    if issue_id == "paper_poster_html_root_wrapper_padding_overflow":
        score -= 60
        reasons.append("root_wrapper_padding_overflow")
    if issue_id == "paper_poster_html_row_allocation_density_regression":
        score -= 260
        reasons.append("row_allocation_density_regression")
    if issue_id == "paper_poster_html_local_flow_overflow":
        overflow_px = _local_flow_overflow_px(issues)
        if overflow_px > 0:
            score -= min(220, max(1, overflow_px // 4))
            reasons.append(f"local_overflow_px_{overflow_px}")
    measurement = _read_candidate_measurement(candidate)
    bboxes = measurement.get("bboxes") if isinstance(measurement, dict) else None
    if isinstance(bboxes, dict):
        fill_score, fill_reasons = _html_first_measurement_fill_score(soup, bboxes, candidate.get("canvas") or {})
        score += fill_score
        reasons.extend(fill_reasons)
    return score, reasons[:16]


def _read_candidate_text(candidate: dict[str, Any], key: str) -> str:
    path = Path(str(candidate.get(key) or ""))
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _read_candidate_measurement(candidate: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(candidate.get("measurement_json") or ""))
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _candidate_visual_fill_feedback(ctx: ToolContext, candidate: dict[str, Any]) -> dict[str, Any]:
    current = _candidate_fill_snapshot(candidate)
    locked = ctx.state.get("paper_poster_html_locked_base_candidate")
    locked_snapshot = _candidate_fill_snapshot(locked) if isinstance(locked, dict) else {}
    feedback: dict[str, Any] = {
        "candidate": current,
    }
    if locked_snapshot:
        feedback["locked_base_candidate"] = locked_snapshot
        current_score = _safe_int(candidate.get("candidate_score"), 0)
        locked_score = _safe_int(locked.get("candidate_score"), 0) if isinstance(locked, dict) else 0
        feedback["current_vs_locked_base"] = {
            "candidate_score_delta": current_score - locked_score,
            "current_candidate_id": candidate.get("candidate_id"),
            "locked_base_candidate_id": locked.get("candidate_id") if isinstance(locked, dict) else "",
        }
    if current.get("fill_issues") or current.get("severe_fill_issues") or locked_snapshot:
        return feedback
    return {}


def _candidate_fill_snapshot(candidate: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        return {}
    snapshot: dict[str, Any] = {
        "candidate_id": candidate.get("candidate_id"),
        "candidate_relative_dir": candidate.get("candidate_relative_dir"),
        "preview_png": candidate.get("preview_png_relative") or candidate.get("preview_png"),
        "measurement_json": candidate.get("measurement_json_relative") or candidate.get("measurement_json"),
        "candidate_score": candidate.get("candidate_score"),
        "candidate_score_reasons": list(candidate.get("candidate_score_reasons") or []),
    }
    measurement = _read_candidate_measurement(candidate)
    bboxes = measurement.get("bboxes") if isinstance(measurement, dict) else None
    canvas = measurement.get("canvas") if isinstance(measurement, dict) else None
    if not isinstance(canvas, dict):
        canvas = candidate.get("canvas") if isinstance(candidate.get("canvas"), dict) else {}
    body_html = _read_candidate_text(candidate, "body_html")
    if isinstance(bboxes, dict) and canvas and body_html:
        soup = BeautifulSoup(body_html, "html.parser")
        fill_issues = _canvas_fill_issues(soup, bboxes, canvas)
        severe = _severe_canvas_fill_issues(fill_issues, canvas)
        snapshot["fill_issues"] = fill_issues[:8]
        snapshot["severe_fill_issues"] = severe[:8]
        metrics = _fill_metrics_from_issues(fill_issues)
        if metrics:
            snapshot["fill_metrics"] = metrics
    return {key: value for key, value in snapshot.items() if value not in (None, "", [])}


def _fill_metrics_from_issues(issues: list[dict[str, Any]]) -> dict[str, Any]:
    metric_keys = (
        "content_bottom_ratio",
        "content_bottom_px",
        "lower_quarter_content_coverage",
        "lower_half_content_coverage",
        "middle_lower_content_coverage",
        "min_content_bottom_ratio",
        "min_lower_quarter_content_coverage",
        "min_lower_half_content_coverage",
        "min_middle_lower_content_coverage",
    )
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        metrics = {key: issue.get(key) for key in metric_keys if issue.get(key) not in (None, "")}
        if metrics:
            return metrics
    return {}


def _html_first_measurement_fill_score(
    soup: BeautifulSoup,
    bboxes: dict[str, Any],
    canvas: dict[str, Any],
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    ch = _safe_int(canvas.get("h_px"), 0) if isinstance(canvas, dict) else 0
    columns = soup.select(".poster-columns,[data-layout-region='editorial_columns']")
    for columns_tag in columns[:1]:
        bbox = _bbox_for_tag(columns_tag, bboxes)
        if bbox and ch:
            bottom_gap = ch - int(bbox.get("y", 0) + bbox.get("h", 0))
            if 0 <= bottom_gap <= max(140, int(ch * 0.08)):
                score += 70
                reasons.append("body_shell_reaches_canvas_bottom")
            elif bottom_gap > max(180, int(ch * 0.12)):
                score -= 75
                reasons.append("body_shell_underfilled")
    good_columns = 0
    underfilled_columns = 0
    for column in soup.select(".poster-column,[data-column-id]"):
        column_bbox = _bbox_for_tag(column, bboxes)
        if not column_bbox:
            continue
        sections = [
            (section, _bbox_for_tag(section, bboxes))
            for section in _direct_editorial_sections(column)
        ]
        valid = [(section, bbox) for section, bbox in sections if bbox]
        if not valid:
            continue
        last_bottom = max(int(bbox["y"] + bbox["h"]) for _, bbox in valid)
        column_bottom = int(column_bbox["y"] + column_bbox["h"])
        gap = column_bottom - last_bottom
        if 0 <= gap <= max(120, int(column_bbox["h"] * 0.10)):
            good_columns += 1
        elif gap > max(160, int(column_bbox["h"] * 0.14)):
            underfilled_columns += 1
    if good_columns >= 3:
        score += 90
        reasons.append("all_columns_panel_boxes_fill")
    elif good_columns:
        score += good_columns * 20
        reasons.append(f"columns_panel_boxes_fill_{good_columns}")
    if underfilled_columns:
        score -= underfilled_columns * 35
        reasons.append(f"underfilled_columns_{underfilled_columns}")
    return score, reasons


def _issues_are_pure_scroll_overflow(issues: list[Any]) -> bool:
    if not issues:
        return False
    for issue in issues:
        if not isinstance(issue, dict):
            return False
        overflow = issue.get("overflow_px") if isinstance(issue.get("overflow_px"), dict) else {}
        scroll = issue.get("scroll_overflow_px") if isinstance(issue.get("scroll_overflow_px"), dict) else {}
        if any(_safe_int(value) > 3 for value in overflow.values()):
            return False
        if not any(_safe_int(value) > 3 for value in scroll.values()):
            return False
    return True


def _local_flow_overflow_px(issues: list[Any]) -> int:
    max_overflow = 0
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        scroll = issue.get("scroll_overflow_px") if isinstance(issue.get("scroll_overflow_px"), dict) else {}
        for value in scroll.values():
            max_overflow = max(max_overflow, _safe_int(value, 0))
    return max_overflow


def _candidate_state_record(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": candidate.get("candidate_id"),
        "candidate_dir": candidate.get("candidate_dir"),
        "candidate_relative_dir": candidate.get("candidate_relative_dir"),
        "body_html": candidate.get("body_html"),
        "style_css": candidate.get("style_css"),
        "measure_html": candidate.get("measure_html"),
        "measure_html_relative": candidate.get("measure_html_relative"),
        "preview_png": candidate.get("preview_png"),
        "preview_png_relative": candidate.get("preview_png_relative"),
        "measurement_json": candidate.get("measurement_json"),
        "measurement_json_relative": candidate.get("measurement_json_relative"),
        "validation_overlay_png": candidate.get("validation_overlay_png"),
        "validation_overlay_png_relative": candidate.get("validation_overlay_png_relative"),
        "visual_repair_dir": candidate.get("visual_repair_dir"),
        "visual_repair_dir_relative": candidate.get("visual_repair_dir_relative"),
        "visual_repair_packet_json": candidate.get("visual_repair_packet_json"),
        "visual_repair_packet_json_relative": candidate.get("visual_repair_packet_json_relative"),
        "status": candidate.get("status"),
        "stage": candidate.get("stage"),
        "canvas": candidate.get("canvas") or {},
        "candidate_score": candidate.get("candidate_score"),
        "candidate_score_reasons": list(candidate.get("candidate_score_reasons") or []),
    }


def _html_first_candidate_rank(candidate: dict[str, Any] | None) -> tuple[int, int, int]:
    if not isinstance(candidate, dict):
        return (-1, -10_000, -1)
    return (
        1 if _html_first_candidate_is_accepted(candidate) else 0,
        _safe_int(candidate.get("candidate_score"), -10_000),
        _html_first_candidate_numeric_suffix(candidate),
    )


def _html_first_candidate_is_accepted(candidate: dict[str, Any]) -> bool:
    if str(candidate.get("status") or "").lower() == "accepted":
        return True
    reasons = candidate.get("candidate_score_reasons")
    return isinstance(reasons, list) and any(str(item) == "accepted_design_spec" for item in reasons)


def _html_first_candidate_numeric_suffix(candidate: dict[str, Any]) -> int:
    best = 0
    for value in (
        str(candidate.get("candidate_id") or ""),
        str(candidate.get("candidate_relative_dir") or ""),
        str(candidate.get("candidate_dir") or ""),
    ):
        for match in re.finditer(r"candidate_(\d+)", value):
            best = max(best, _safe_int(match.group(1), 0))
    return best


def _update_html_first_best_candidate(ctx: ToolContext, candidate: dict[str, Any]) -> None:
    candidate["is_best_candidate"] = False
    current = ctx.state.get("paper_poster_html_best_candidate")
    if _html_first_candidate_rank(candidate) <= _html_first_candidate_rank(current if isinstance(current, dict) else None):
        return
    previous_id = str(current.get("candidate_id") or "") if isinstance(current, dict) else ""
    if previous_id:
        _mark_candidate_manifest_flag(ctx, previous_id, "is_best_candidate", False)
    candidate["is_best_candidate"] = True
    ctx.state["paper_poster_html_best_candidate"] = _candidate_state_record(candidate)
    _mark_candidate_manifest_flag(ctx, str(candidate.get("candidate_id") or ""), "is_best_candidate", True)


def _update_html_first_locked_base_candidate(ctx: ToolContext, candidate: dict[str, Any]) -> None:
    candidate["is_locked_base_candidate"] = False
    current = ctx.state.get("paper_poster_html_locked_base_candidate")
    current_score = _safe_int(current.get("candidate_score"), -10_000) if isinstance(current, dict) else -10_000
    score = _safe_int(candidate.get("candidate_score"), -10_000)
    if score <= current_score:
        return
    previous_id = str(current.get("candidate_id") or "") if isinstance(current, dict) else ""
    if previous_id:
        _mark_candidate_manifest_flag(ctx, previous_id, "is_locked_base_candidate", False)
    candidate["is_locked_base_candidate"] = True
    ctx.state["paper_poster_html_locked_base_candidate"] = _candidate_state_record(candidate)
    _mark_candidate_manifest_flag(ctx, str(candidate.get("candidate_id") or ""), "is_locked_base_candidate", True)


def _mark_candidate_manifest_flag(ctx: ToolContext, candidate_id: str, key: str, value: bool) -> None:
    if not candidate_id:
        return
    manifest_path = ctx.run_dir / "html_first" / "candidates" / candidate_id / "manifest.json"
    if not manifest_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(manifest, dict):
            manifest[key] = bool(value)
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        return


def _html_first_candidate_is_repairable_base(
    stage: str,
    payload: dict[str, Any] | None,
    score: int,
) -> bool:
    if score < 250:
        return False
    repairable_stages = {
        "local_flow_overflow",
        "editorial_flow_fill",
        "source_visual_size",
        "designer_owned_canvas_shell",
        "block_boundary",
        "text_clipping",
        "narrow_math_container",
    }
    if stage in repairable_stages:
        return True
    issue_id = str((payload or {}).get("issue_id") or "")
    return issue_id in {
        "paper_poster_html_local_flow_overflow",
        "paper_poster_html_designer_flow_canvas_overflow",
        "paper_poster_html_root_wrapper_padding_overflow",
        "paper_poster_html_editorial_flow_fill_failed",
        "paper_poster_html_source_visual_too_small",
        "paper_poster_html_block_out_of_bounds",
        "paper_poster_html_narrow_math_container",
    }


def _attach_locked_base_candidate_to_payload(ctx: ToolContext, payload: dict[str, Any]) -> None:
    locked = ctx.state.get("paper_poster_html_locked_base_candidate")
    if not isinstance(locked, dict):
        return
    payload["locked_base_candidate_id"] = locked.get("candidate_id")
    payload["locked_base_candidate_relative_dir"] = locked.get("candidate_relative_dir")
    payload["locked_base_candidate_preview_png"] = locked.get("preview_png_relative") or locked.get("preview_png")
    payload["locked_base_candidate_measurement_json"] = locked.get("measurement_json_relative") or locked.get("measurement_json")
    payload["locked_base_candidate_visual_repair_packet_json"] = (
        locked.get("visual_repair_packet_json_relative")
        or locked.get("visual_repair_packet_json")
    )
    payload["locked_base_candidate_visual_repair_dir"] = (
        locked.get("visual_repair_dir_relative")
        or locked.get("visual_repair_dir")
    )
    payload["locked_base_candidate_score"] = locked.get("candidate_score")


def _designer_owned_css_mode(args: dict[str, Any], soup: BeautifulSoup, ctx: ToolContext) -> bool:
    truthy_keys = (
        "designer_owned_css",
        "planner_owned_css",
        "free_css",
        "css_first",
        "browser_flow",
    )
    for key in truthy_keys:
        if _is_truthy_attr(args.get(key)):
            return True
    fixed_slot_contract = _fixed_layout_slot_contract_active(ctx)
    state = ctx.state if isinstance(ctx.state, dict) else {}
    contract = state.get("poster_plan_contract")
    if isinstance(contract, dict):
        for key in (
            "layout_mode",
            "poster_layout_mode",
            "css_mode",
            "compiler_mode",
            "mode",
            "layout_archetype",
        ):
            if _designer_owned_css_token(contract.get(key)):
                return True
        for nested_key in (
            "layout_slot_contract",
            "designer_authoring_blueprint",
            "authored_html_skeleton",
        ):
            nested = contract.get(nested_key)
            if not isinstance(nested, dict):
                continue
            for key in (
                "layout_mode",
                "poster_layout_mode",
                "css_mode",
                "compiler_mode",
                "mode",
                "render_mode",
                "render_contract",
            ):
                if _designer_owned_css_token(nested.get(key)):
                    return True
    for key in (
        "layout_mode",
        "poster_layout_mode",
        "css_mode",
        "compiler_mode",
        "render_contract",
        "layout",
        "archetype",
    ):
        if not fixed_slot_contract and _designer_owned_css_token(args.get(key)):
            return True
    if not fixed_slot_contract and _is_truthy_attr(state.get("paper_poster_designer_owned_css")):
        return True
    for key in ("paper_poster_layout_mode", "paper_poster_css_mode", "paper_poster_compiler_mode"):
        if not fixed_slot_contract and _designer_owned_css_token(state.get(key)):
            return True
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        classes = tag.get("class")
        class_tokens = [str(token).strip() for token in classes] if isinstance(classes, list) else str(classes or "").split()
        if {"flow-panel", "poster-grid", "flow-poster"}.intersection(class_tokens):
            return True
        for key in (
            "data-layout-mode",
            "data-poster-layout-mode",
            "data-css-mode",
            "data-compiler-mode",
            "data-render-contract",
            "data-render-mode",
        ):
            if not fixed_slot_contract and _designer_owned_css_token(tag.get(key)):
                return True
        for key in (
            "data-designer-owned-css",
            "data-planner-owned-css",
            "data-free-css",
            "data-css-first",
        ):
            if _is_truthy_attr(tag.get(key)):
                return True
    return False


def _fixed_layout_slot_contract_active(ctx: ToolContext) -> bool:
    state = ctx.state if isinstance(ctx.state, dict) else {}
    contract = state.get("poster_plan_contract")
    if not isinstance(contract, dict):
        return False
    layout = contract.get("layout_slot_contract")
    if not isinstance(layout, dict) or not layout.get("slots"):
        return False
    archetype = str(
        contract.get("layout_archetype")
        or contract.get("archetype")
        or ""
    ).strip().lower()
    canvas_plan = state.get("canvas_plan")
    preset = ""
    if isinstance(canvas_plan, dict):
        preset = str(canvas_plan.get("preset_id") or "").strip().lower()
    return archetype in {"cvpr-landscape", "cvpr_landscape"} or preset == "cvpr-landscape"


def _designer_owned_css_token(value: Any) -> bool:
    if value is None:
        return False
    token = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
    return token in _DESIGNER_OWNED_CSS_MODES


def _is_truthy_attr(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def _normalize_authored_html_document(body_html: str, css: str) -> tuple[str, str]:
    body, normalized_css, _root_shell = _normalize_authored_html_document_with_root_shell(body_html, css)
    return body, normalized_css


def _normalize_authored_html_document_with_root_shell(body_html: str, css: str) -> tuple[str, str, dict[str, Any] | None]:
    soup = BeautifulSoup(str(body_html or ""), "html.parser")
    # Drop author scaffolding comments (section banners like `<!-- BODY -->`,
    # `<!-- /poster-body -->`). They are non-load-bearing — the pipeline keys off
    # data-block-id / data-panel-role, never comment markers. A downstream
    # re-serialization can mangle a comment that sits next to the poster-root
    # boundary, dropping the `<!--`/`-->` delimiters and leaving the inner text as a
    # visible node (e.g. " /poster-body" or " ===== BODY ====="), which then paints
    # onto the rendered poster. Removing them here makes that impossible.
    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()
    css_parts = [str(css or "")]
    for style_tag in list(soup.find_all("style")):
        style_css = style_tag.get_text("\n", strip=False)
        media = str(style_tag.get("media") or "").strip()
        if media and not _css_media_is_unconditional_all(media):
            style_css = f"@media {media} {{\n{style_css}\n}}"
        css_parts.append(style_css)
        style_tag.decompose()
    poster_root = soup.select_one(".paper-poster")
    if isinstance(poster_root, Tag):
        body, root_shell = _decode_poster_root_contents_preserving_flow_wrapper(soup, poster_root)
    elif soup.body is not None:
        body = soup.body.decode_contents()
        root_shell = None
    else:
        body = str(soup)
        root_shell = None
    return body.strip(), "\n\n".join(part for part in css_parts if part.strip()), root_shell


_NESTED_POSTER_ROOT_CLASS_ALIASES = {
    ".flow-poster.paper-poster": ".paper-poster > .flow-poster",
    ".paper-poster.flow-poster": ".paper-poster > .flow-poster",
    ".editorial-poster.paper-poster": ".paper-poster > .editorial-poster",
    ".paper-poster.editorial-poster": ".paper-poster > .editorial-poster",
    ".authored-css-poster.paper-poster": ".paper-poster > .authored-css-poster",
    ".paper-poster.authored-css-poster": ".paper-poster > .authored-css-poster",
}


def _alias_nested_poster_root_selectors(css: str) -> str:
    text = str(css or "")
    if ".paper-poster" not in text:
        return text
    if not any(token in text for token in _NESTED_POSTER_ROOT_CLASS_ALIASES):
        return text

    def repl(match: re.Match[str]) -> str:
        selector_group = match.group(1)
        selectors = [part.strip() for part in selector_group.split(",") if part.strip()]
        if not selectors:
            return match.group(0)
        expanded: list[str] = []
        changed = False
        for selector in selectors:
            if selector.startswith("@"):
                expanded.append(selector)
                continue
            expanded.append(selector)
            for token, alias in _NESTED_POSTER_ROOT_CLASS_ALIASES.items():
                if token not in selector:
                    continue
                expanded.append(selector.replace(token, alias))
                changed = True
        if not changed:
            return match.group(0)
        deduped = list(dict.fromkeys(expanded))
        return ", ".join(deduped) + "{"

    return re.sub(r"([^{}]+)\{", repl, text)


_TRANSPARENT_EDITORIAL_ROOT_CLASSES = {
    "authored-css-poster",
}


def _has_direct_editorial_header_and_columns(tag: Tag) -> bool:
    direct_children = [
        child
        for child in tag.find_all(True, recursive=False)
        if isinstance(child, Tag)
    ]
    has_direct_header = any(
        child.name == "header"
        or bool({"poster-header", "identity-header"}.intersection(_class_tokens(child)))
        for child in direct_children
    )
    has_direct_columns = any(
        "poster-columns" in _class_tokens(child)
        for child in direct_children
    )
    return bool(has_direct_header and has_direct_columns)


def _unwrap_redundant_editorial_root_wrapper(body_html: str) -> str:
    soup = BeautifulSoup(str(body_html or ""), "html.parser")
    top_tags = [child for child in soup.contents if isinstance(child, Tag)]
    nonempty_text = [
        str(child).strip()
        for child in soup.contents
        if not isinstance(child, Tag) and str(child).strip()
    ]
    if len(top_tags) != 1 or nonempty_text:
        return body_html
    wrapper = top_tags[0]
    classes = _class_tokens(wrapper)
    if "paper-poster" in classes or "flow-poster" in classes:
        return body_html
    if "editorial-poster" in classes:
        return body_html
    layout_mode = str(
        wrapper.get("data-layout-mode")
        or wrapper.get("data-poster-layout-mode")
        or wrapper.get("data-css-mode")
        or ""
    ).strip()
    transparent_editorial_shell = (
        bool(classes.intersection(_TRANSPARENT_EDITORIAL_ROOT_CLASSES))
        or _designer_owned_css_token(layout_mode)
    )
    if not transparent_editorial_shell:
        return body_html
    if not _has_direct_editorial_header_and_columns(wrapper):
        return body_html
    return wrapper.decode_contents().strip()


def _decode_poster_root_contents_preserving_flow_wrapper(soup: BeautifulSoup, poster_root: Tag) -> tuple[str, dict[str, Any] | None]:
    classes = [
        str(cls).strip()
        for cls in (poster_root.get("class") or [])
        if str(cls).strip() and str(cls).strip() != "paper-poster"
    ]
    attrs: dict[str, Any] = {}
    for key, value in list(poster_root.attrs.items()):
        if key in {"class", "id"}:
            continue
        if key.startswith("data-frame") or key in {"data-w", "data-h"}:
            continue
        if key in {"style", "data-layout-mode", "data-poster-layout-mode", "data-css-mode", "data-compiler-mode"}:
            attrs[key] = value
    if not classes and not attrs:
        return poster_root.decode_contents(), None
    if "editorial-poster" in classes and _has_direct_editorial_header_and_columns(poster_root):
        return poster_root.decode_contents(), _root_shell_from_poster_root(poster_root, classes)
    wrapper = soup.new_tag("div")
    if classes:
        wrapper["class"] = classes
    for key, value in attrs.items():
        wrapper[key] = value
    for child in list(poster_root.contents):
        wrapper.append(child.extract())
    return str(wrapper), None


def _root_shell_from_poster_root(poster_root: Tag, classes: list[str]) -> dict[str, Any]:
    attrs: dict[str, str] = {}
    for key, value in list(poster_root.attrs.items()):
        key_str = str(key or "").strip()
        if key_str in {"class", "id", "style"}:
            continue
        if key_str.startswith("data-frame") or key_str in {"data-w", "data-h"}:
            continue
        if not key_str.startswith("data-"):
            continue
        attrs[key_str] = str(value)
    return {
        "classes": [cls for cls in classes if cls and cls != "paper-poster"],
        "attrs": attrs,
        "transparent_editorial_root": True,
    }


def _paper_poster_main_open_tag(root_shell: dict[str, Any] | None, *, base_attrs: dict[str, Any] | None = None) -> str:
    classes = ["paper-poster"]
    attrs: dict[str, str] = {}
    if isinstance(base_attrs, dict):
        attrs.update({
            str(key): str(value)
            for key, value in base_attrs.items()
            if _safe_root_shell_attr_name(str(key))
        })
    if isinstance(root_shell, dict):
        for cls in root_shell.get("classes") or []:
            cls_str = str(cls).strip()
            if cls_str and cls_str not in classes and _safe_html_class_token(cls_str):
                classes.append(cls_str)
        shell_attrs = root_shell.get("attrs")
        if isinstance(shell_attrs, dict):
            for key, value in shell_attrs.items():
                key_str = str(key or "").strip()
                if key_str in attrs or not _safe_root_shell_attr_name(key_str):
                    continue
                attrs[key_str] = str(value)
    attr_parts = [f'class="{escape(" ".join(classes), quote=True)}"']
    attr_parts.extend(
        f'{key}="{escape(value, quote=True)}"'
        for key, value in attrs.items()
    )
    return f"<main {' '.join(attr_parts)}>"


def _safe_html_class_token(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+", value))


def _safe_root_shell_attr_name(value: str) -> bool:
    if value in {"class", "id", "style"}:
        return False
    if value.startswith("data-frame") or value in {"data-w", "data-h"}:
        return False
    return bool(re.fullmatch(r"data-[A-Za-z0-9_.:-]+", value))


def _visible_pipeline_text_error(soup: BeautifulSoup, ctx: ToolContext) -> ToolResultRecord | None:
    issues: list[dict[str, Any]] = []
    for text_node in soup.find_all(string=True):
        if isinstance(text_node, Comment):
            continue
        parent = text_node.parent
        if not isinstance(parent, Tag) or str(parent.name or "").lower() in {"script", "style", "template"}:
            continue
        text = " ".join(str(text_node).split())
        if not text:
            continue
        context = " ".join(parent.get_text(" ", strip=True).split()) or text
        for issue_id, pattern, label in _VISIBLE_PIPELINE_TEXT_PATTERNS:
            if not pattern.search(context):
                continue
            issues.append({
                "id": issue_id,
                "forbidden_text": label,
                "block_id": str(parent.get("data-block-id") or ""),
                "lane": str(parent.get("data-lane") or ""),
                "role": str(parent.get("data-role") or parent.get("data-panel-role") or ""),
                "text": context[:300],
            })
            break
    if not issues:
        return None
    ctx.state["paper_poster_html_visible_pipeline_text"] = {"issues": issues[:12]}
    log(
        "paper_poster_html.visible_pipeline_text_block",
        issue_count=len(issues),
        first_issues=issues[:3],
    )
    return obs_error(
        "propose_paper_poster_html found visible generator/provenance boilerplate in poster text.",
        category="validation",
        payload={
            "issue_id": "paper_poster_html_visible_pipeline_text",
            "repair_route": "replace_pipeline_boilerplate_with_paper_identity_or_scientific_content",
            "issues": issues[:12],
            "forbidden_examples": [
                "Paper poster",
                "source-backed authored HTML",
                "no generated evidence imagery",
                "authored HTML",
            ],
            "hint": (
                "Poster-visible copy must be paper content. The title/header band should contain "
                "exactly three compact visible text rows and nothing else: title, authors, and "
                "plain-text school/institution/company names; body panels should contain scientific claims, evidence, captions, "
                "tables, and takeaways. Keep pipeline/provenance descriptors in metadata only."
            ),
        },
    )


def _identity_header_only_error(soup: BeautifulSoup, ctx: ToolContext) -> ToolResultRecord | None:
    headers = _identity_header_tags(soup)
    if not headers:
        return None
    issues: list[dict[str, Any]] = []
    for header in headers:
        header_id = str(header.get("data-block-id") or header.get("data-slot-id") or "identity_header")
        for asset in header.find_all(["figure", "img", "svg", "table"]):
            if not isinstance(asset, Tag):
                continue
            source_id = _source_id_for_tag(asset, ctx)
            issues.append({
                "id": "identity_header_nonidentity_source" if source_id else "identity_header_nontext_asset",
                "header_id": header_id,
                "block_id": str(asset.get("data-block-id") or ""),
                "source_id": source_id,
                "repair": (
                    "Remove header images, SVGs, figures, and tables. The header is exactly three "
                    "text rows only: title, authors, and plain-text school/institution/company names."
                ),
            })
        for tag in header.find_all(True):
            if not isinstance(tag, Tag) or tag is header:
                continue
            role_blob = _semantic_role_blob(tag).replace("_", "-")
            if (
                any(token in role_blob for token in _IDENTITY_HEADER_FORBIDDEN_ROLE_TOKENS)
                and not _identity_header_role_allowed(tag, ctx)
            ):
                issues.append({
                    "id": "identity_header_forbidden_role",
                    "header_id": header_id,
                    "block_id": str(tag.get("data-block-id") or ""),
                    "role": _identity_header_role_summary(tag),
                    "text": " ".join(tag.get_text(" ", strip=True).split())[:220],
                    "repair": (
                        "Header children may describe only title, authors, or plain-text school/institution/company names; "
                        "remove any fourth header/meta/subtitle row or side identity rail, and move thesis, focus, readout, or takeaway content into body sections."
                    ),
                })
        for text_node in header.find_all(string=True):
            if isinstance(text_node, Comment):
                continue
            parent = text_node.parent
            if not isinstance(parent, Tag) or str(parent.name or "").lower() in {"script", "style", "template"}:
                continue
            text = " ".join(str(text_node).split())
            if not text:
                continue
            context = " ".join(parent.get_text(" ", strip=True).split()) or text
            logo_rail_issue = _identity_header_logo_rail_descriptor_issue(parent, header, header_id, context, ctx)
            if logo_rail_issue:
                issues.append(logo_rail_issue)
                continue
            if _identity_header_role_allowed(parent, ctx):
                if _identity_header_allowed_role_has_nonidentity_text(parent, context, ctx):
                    issues.append({
                        "id": "identity_header_nonidentity_allowed_role_text",
                        "header_id": header_id,
                        "block_id": str(parent.get("data-block-id") or ""),
                        "role": _identity_header_role_summary(parent),
                        "word_count": _visible_word_count(context),
                        "text": context[:220],
                    "repair": (
                        "This header element is labeled like identity metadata, but generated "
                        "headers now allow exactly three visible text rows only: title, authors, "
                        "and plain-text school/institution/company names. Remove venue, resource, citation/contact, badge, icon, QR, link, slogan, and readout "
                        "text from the header."
                    ),
                })
                continue
            if _IDENTITY_HEADER_FORBIDDEN_TEXT_RE.search(context):
                issues.append({
                    "id": "identity_header_forbidden_descriptor",
                    "header_id": header_id,
                    "block_id": str(parent.get("data-block-id") or ""),
                    "role": _identity_header_role_summary(parent),
                    "text": context[:220],
                    "repair": (
                        "Remove Core idea / Poster focus / thesis / takeaway text from the header; "
                        "put that content in a body poster-section."
                    ),
                })
                continue
            if _identity_header_body_copy_like(parent, context):
                issues.append({
                    "id": "identity_header_body_copy",
                    "header_id": header_id,
                    "block_id": str(parent.get("data-block-id") or ""),
                    "role": _identity_header_role_summary(parent),
                    "word_count": _visible_word_count(context),
                    "text": context[:220],
                    "repair": (
                        "The header is identity only. Move summary/tagline/body-copy sentences "
                        "into Motivation, Method, Results, or Analysis sections."
                    ),
                })
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in issues:
        key = (
            str(issue.get("id") or ""),
            str(issue.get("block_id") or ""),
            str(issue.get("text") or issue.get("source_id") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)
    if not deduped:
        return None
    ctx.state["paper_poster_html_identity_header_issues"] = {"issues": deduped[:12]}
    log(
        "paper_poster_html.identity_header_only_block",
        issue_count=len(deduped),
        first_issues=deduped[:3],
    )
    return obs_error(
        "propose_paper_poster_html found non-identity content inside the poster header.",
        category="validation",
        payload={
            "issue_id": "paper_poster_html_identity_header_only_failed",
            "repair_route": "move_header_body_content_to_main_sections",
            "issues": deduped[:12],
            "hint": (
                "The conference poster header is limited to exactly these three visible paper-identity rows: title, authors, "
                "and plain-text school/institution/company names. Do not put "
                "venue/conference/arXiv/archive metadata, citation/contact text, project/code/resource links, logos, image badges, icons, QR codes, "
                "Core idea, Poster focus, summary, thesis, takeaway, method/result readouts, "
                "paper source figures/tables, or explanatory captions under header identity lines/labels "
                "in the header."
            ),
        },
    )


def _identity_header_tags(soup: BeautifulSoup) -> list[Tag]:
    out: list[Tag] = []
    seen: set[int] = set()
    for tag in soup.select(_IDENTITY_HEADER_SELECTOR):
        if isinstance(tag, Tag) and id(tag) not in seen:
            seen.add(id(tag))
            out.append(tag)
    return out


def _nearest_identity_header(tag: Tag | None) -> Tag | None:
    current = tag
    while isinstance(current, Tag):
        if _tag_is_identity_header(current):
            return current
        current = current.parent if isinstance(current.parent, Tag) else None
    return None


def _tag_is_identity_header(tag: Tag) -> bool:
    classes = _class_tokens(tag)
    if str(tag.name or "").lower() == "header" and classes & {"poster-header", "editorial-header"}:
        return True
    if str(tag.name or "").lower() == "header" and "identity-header" in classes:
        return True
    if str(tag.get("data-panel-role") or "").strip().lower() in {"identity_header", "identity"}:
        return True
    if str(tag.get("data-role") or "").strip().lower() == "identity_header":
        return True
    if str(tag.get("data-slot-id") or "") == "title_meta":
        return True
    return str(tag.get("data-block-id") or "") == "title_meta"


def _identity_header_role_allowed(tag: Tag, ctx: ToolContext) -> bool:
    name = str(tag.name or "").lower()
    if name in {"script", "style", "template"}:
        return True
    if name == "h1":
        return True
    if name in {"img", "svg"}:
        return False
    role_blob = _semantic_role_blob(tag).replace("_", "-")
    if any(token in role_blob for token in _IDENTITY_HEADER_ALLOWED_ROLE_TOKENS):
        return True
    text = " ".join(tag.get_text(" ", strip=True).split())
    if not text:
        return True
    if name in {"strong", "b", "span"} and _visible_word_count(text) <= 4:
        parent = tag.parent if isinstance(tag.parent, Tag) else None
        return bool(parent is not None and _identity_header_role_allowed(parent, ctx))
    return False


def _identity_header_body_copy_like(tag: Tag, text: str) -> bool:
    if _visible_word_count(text) < 10:
        return False
    name = str(tag.name or "").lower()
    if name in {"h1", "h2", "h3", "small"}:
        return False
    role_blob = _semantic_role_blob(tag).replace("_", "-")
    if any(token in role_blob for token in _IDENTITY_HEADER_ALLOWED_ROLE_TOKENS):
        return False
    return name in {"p", "div", "aside", "section", "span"}


def _identity_header_allowed_role_has_nonidentity_text(tag: Tag, text: str, ctx: ToolContext) -> bool:
    text = " ".join(str(text or "").split())
    if not text:
        return False
    name = str(tag.name or "").lower()
    if name in {"h1", "script", "style", "template"}:
        return False
    role_blob = _semantic_role_blob(tag).replace("_", "-")
    if any(token in role_blob for token in ("badge", "text-badge", "identity-badge", "venue-badge", "source-badge")):
        return True
    if _IDENTITY_HEADER_DISALLOWED_METADATA_RE.search(text):
        return True
    if _visible_word_count(text) < 3:
        return False
    if any(token in role_blob for token in ("logo", "venue", "conference", "workshop", "arxiv", "archive", "doi", "resource", "project", "github", "hugging", "link")):
        return True
    if any(token in role_blob for token in ("affiliation", "institution", "university", "institute", "lab", "company")):
        if _IDENTITY_HEADER_FORBIDDEN_TEXT_RE.search(text):
            return True
        if _IDENTITY_HEADER_NONIDENTITY_TOPIC_RE.search(text) and not _IDENTITY_HEADER_IDENTITY_TEXT_RE.search(text):
            return True
        return False
    if _IDENTITY_HEADER_FORBIDDEN_TEXT_RE.search(text):
        return True
    if _IDENTITY_HEADER_IDENTITY_TEXT_RE.search(text):
        return False
    if _looks_like_author_list(text):
        return False
    return bool(_IDENTITY_HEADER_NONIDENTITY_TOPIC_RE.search(text))


def _identity_header_logo_rail_descriptor_issue(
    tag: Tag,
    header: Tag,
    header_id: str,
    text: str,
    ctx: ToolContext,
) -> dict[str, Any] | None:
    if not _identity_header_logo_rail_text_parent(tag, header):
        return None
    if _visible_word_count(text) < 3:
        return None
    return {
        "id": "identity_header_logo_rail_descriptor",
        "header_id": header_id,
        "block_id": str(tag.get("data-block-id") or ""),
        "role": _identity_header_role_summary(tag),
        "word_count": _visible_word_count(text),
        "text": text[:220],
        "repair": (
            "Remove header logo/badge rails. The header should be exactly three text rows only: "
            "title, authors, and plain-text school/institution/company names."
        ),
    }


def _identity_header_logo_rail_text_parent(tag: Tag, header: Tag) -> bool:
    node: Tag | None = tag
    while isinstance(node, Tag) and node is not header:
        role_blob = _semantic_role_blob(node).replace("_", "-")
        if any(token in role_blob for token in (
            "logo-wrap",
            "logo-rail",
            "brand-rail",
            "badge-stack",
            "identity-rail",
            "logo",
            "badge",
        )):
            return True
        if node.find(["img", "svg"], recursive=False) is not None:
            return True
        node = node.parent if isinstance(node.parent, Tag) else None
    return False


def _identity_header_compact_badge_text_allowed(tag: Tag, text: str, ctx: ToolContext | None = None) -> bool:
    clean = " ".join(str(text or "").split())
    if not clean:
        return True
    words = _visible_word_count(clean)
    if _IDENTITY_HEADER_FORBIDDEN_TEXT_RE.search(clean):
        return False
    if _IDENTITY_HEADER_DISALLOWED_METADATA_RE.search(clean):
        return False
    if _IDENTITY_HEADER_NONIDENTITY_TOPIC_RE.search(clean) and not _IDENTITY_HEADER_IDENTITY_TEXT_RE.search(clean):
        return False
    if _identity_header_verified_metadata_label_allowed(clean, ctx):
        return True
    if words <= 2 and _IDENTITY_HEADER_IDENTITY_TEXT_RE.search(clean):
        return True
    role_blob = _semantic_role_blob(tag).replace("_", "-")
    if (
        any(token in role_blob for token in ("text-badge", "venue-badge", "source-badge", "identity-badge", "badge"))
        and words <= 3
        and _IDENTITY_HEADER_IDENTITY_TEXT_RE.search(clean)
    ):
        return True
    return False


def _identity_header_verified_metadata_label_allowed(text: str, ctx: ToolContext | None) -> bool:
    if ctx is None or not isinstance(ctx.state, dict):
        return False
    if _visible_word_count(text) > 8:
        return False
    key = _identity_header_label_key(text)
    if not key:
        return False
    labels: set[str] = set()

    def add(value: Any) -> None:
        label_key = _identity_header_label_key(value)
        if label_key:
            labels.add(label_key)

    rendered = ctx.state.get("rendered_layers")
    if isinstance(rendered, dict):
        for layer in rendered.values():
            if not isinstance(layer, dict) or not layer.get("is_identity_asset"):
                continue
            if layer.get("identity_allowed_to_place") is False:
                continue
            for field in ("identity_entity_name", "entity_name", "label", "name"):
                add(layer.get(field))
    return key in labels


def _identity_header_label_key(value: Any) -> str:
    text = " ".join(str(value or "").split()).lower()
    if not text:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _looks_like_author_list(text: str) -> bool:
    clean = re.sub(r"[\d¹²³⁴⁵⁶⁷⁸⁹⁰*†‡§]+", "", text or "")
    parts = [
        part.strip()
        for part in re.split(r"\s*(?:,|;|·|\band\b|&)\s*", clean)
        if part.strip()
    ]
    if len(parts) < 2:
        return False
    name_like = 0
    for part in parts:
        words = [w for w in re.split(r"\s+", part) if w]
        if 1 <= len(words) <= 4 and all(re.match(r"^[A-Z][A-Za-z'.-]*$", w) for w in words[:3]):
            name_like += 1
    return name_like >= 2


def _is_explicit_identity_asset_source_id(source_id: str, ctx: ToolContext | None) -> bool:
    key = str(source_id or "").strip()
    if not key or ctx is None or not isinstance(ctx.state, dict):
        return False
    rendered = ctx.state.get("rendered_layers")
    if isinstance(rendered, dict):
        rec = rendered.get(key)
        if isinstance(rec, dict) and rec.get("is_identity_asset"):
            return True
        for layer_id, layer in rendered.items():
            if not isinstance(layer, dict) or not layer.get("is_identity_asset"):
                continue
            explicit_ids = {
                str(layer_id or ""),
                str(layer.get("asset_id") or ""),
                str(layer.get("rendered_layer_id") or ""),
                str(layer.get("layer_id") or ""),
                str(layer.get("source_id") or ""),
            }
            if key in explicit_ids:
                return True
    return False


def _is_identity_header_source_asset(tag: Tag, panel: Tag | None, source_id: str, ctx: ToolContext | None) -> bool:
    if not _is_explicit_identity_asset_source_id(source_id, ctx):
        return False
    if panel is not None and _is_identity_header_container(panel):
        return True
    node: Tag | None = tag
    while isinstance(node, Tag):
        if _is_identity_header_container(node):
            return True
        name = str(node.name or "").lower()
        if name in {"main", "body", "html"}:
            break
        node = node.parent if isinstance(node.parent, Tag) else None
    return False


def _is_identity_header_container(tag: Tag) -> bool:
    name = str(tag.name or "").lower()
    if name == "header":
        return True
    role = " ".join(
        str(tag.get(key) or "")
        for key in ("data-panel-role", "data-role", "data-slot-id", "data-block-id")
    ).strip().lower().replace("-", "_")
    return "identity_header" in role or "title_meta" in role


def _is_identity_header_asset_tag(tag: Tag, ctx: ToolContext) -> bool:
    source_id = _source_id_for_tag_or_descendant(tag, ctx)
    if source_id and _is_source_visual_id(source_id):
        return _is_explicit_identity_asset_source_id(source_id, ctx)
    if source_id and _is_explicit_identity_asset_source_id(source_id, ctx):
        return True
    role_blob = _semantic_role_blob(tag, include_ancestors=True).replace("_", "-")
    if any(token in role_blob for token in ("identity", "logo", "venue", "conference", "affiliation", "institution")):
        return True
    if not source_id:
        return False
    if source_id.startswith("identity") or "identity" in source_id or "logo" in source_id:
        return True
    return False


def _identity_header_role_summary(tag: Tag) -> str:
    return " ".join(
        str(tag.get(key) or "")
        for key in ("data-block-id", "data-role", "role", "class", "data-panel-role")
        if str(tag.get(key) or "").strip()
    )[:180]


def _canvas_for_html_first(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    args_canvas = args.get("canvas") if isinstance(args.get("canvas"), dict) else None
    canvas_plan = ctx.state.get("canvas_plan") if isinstance(ctx.state.get("canvas_plan"), dict) else None
    contract_plan = (
        (ctx.state.get("poster_plan_contract") or {}).get("canvas_plan")
        if isinstance(ctx.state.get("poster_plan_contract"), dict) else None
    )
    planned_candidates = (
        canvas_plan.get("canvas") if isinstance(canvas_plan, dict) else None,
        contract_plan.get("canvas") if isinstance(contract_plan, dict) else None,
        _canvas_from_plan_preset(canvas_plan),
        _canvas_from_plan_preset(contract_plan),
    )
    if (
        isinstance(args_canvas, dict)
        and str(args_canvas.get("canvas_plan_override_reason") or "").strip()
        and not _has_active_canvas_plan(ctx)
    ):
        canvas = _normalized_canvas_record(args_canvas)
        if canvas is not None:
            canvas["canvas_plan_override_reason"] = str(args_canvas.get("canvas_plan_override_reason") or "").strip()
            return canvas
    for candidate in (*planned_candidates, args_canvas):
        canvas = _normalized_canvas_record(candidate)
        if canvas is not None:
            return canvas
    return {"w_px": 3072, "h_px": 1536, "dpi": 150, "aspect_ratio": "2:1", "color_mode": "RGB"}


def _canvas_from_plan_preset(plan: Any) -> dict[str, Any] | None:
    if not isinstance(plan, dict):
        return None
    preset = str(plan.get("preset_id") or plan.get("template") or "").strip()
    if not preset:
        return None
    resolved = resolve_template(preset)
    return dict(resolved) if isinstance(resolved, dict) else None


def _has_active_canvas_plan(ctx: ToolContext) -> bool:
    plan = ctx.state.get("canvas_plan") if isinstance(ctx.state, dict) else None
    if not isinstance(plan, dict):
        return False
    return (
        _normalized_canvas_record(plan.get("canvas")) is not None
        or _normalized_canvas_record(_canvas_from_plan_preset(plan)) is not None
    )


def _normalized_canvas_record(candidate: Any) -> dict[str, Any] | None:
    if not isinstance(candidate, dict):
        return None
    try:
        w_px = int(candidate.get("w_px") or 0)
        h_px = int(candidate.get("h_px") or 0)
        dpi = int(candidate.get("dpi") or 150)
    except (TypeError, ValueError):
        return None
    if w_px <= 0 or h_px <= 0:
        return None
    return {
        "w_px": w_px,
        "h_px": h_px,
        "dpi": dpi,
        "aspect_ratio": str(candidate.get("aspect_ratio") or _aspect_ratio_label(candidate)),
        "color_mode": str(candidate.get("color_mode") or "RGB"),
    }


def _aspect_ratio_label(canvas: dict[str, Any]) -> str:
    w = max(1, int(canvas.get("w_px") or 1))
    h = max(1, int(canvas.get("h_px") or 1))
    if w >= h:
        return f"{round(w / h, 3)}:1"
    return f"1:{round(h / w, 3)}"


def _cvpr_poster_size_metadata() -> dict[str, Any]:
    return {
        "preset": "cvpr-landscape",
        "label": "CVPR 84x42 in landscape",
        "orientation": "landscape",
        "source": "template",
        "width_in": 84.0,
        "height_in": 42.0,
        "width_mm": 2133.6,
        "height_mm": 1066.8,
    }


def _record_content_quality_blocking(
    ctx: ToolContext,
    result: ToolResultRecord,
    *,
    stage: str,
) -> None:
    payload = result.payload if isinstance(result.payload, dict) else {}
    blocking = ctx.state.setdefault("paper_poster_html_content_contract_blocking", {})
    if not isinstance(blocking, dict):
        blocking = {}
        ctx.state["paper_poster_html_content_contract_blocking"] = blocking
    issue_ids = blocking.get("issue_ids")
    if not isinstance(issue_ids, list):
        issue_ids = []
    issue_id = str(payload.get("issue_id") or stage or "content_quality_blocking")
    if issue_id and issue_id not in issue_ids:
        issue_ids.append(issue_id)
    issues = blocking.get("issues")
    if not isinstance(issues, list):
        issues = []
    issues.append({
        "stage": stage,
        "issue_id": issue_id,
        "message": (result.error_message or "")[:1000],
        "payload": payload,
    })
    blocking.update({
        "reason": "content_quality_blocking",
        "issue_count": len(issue_ids),
        "issue_ids": issue_ids,
        "issues": issues[:12],
    })


def _clear_content_quality_blocking_issue(ctx: ToolContext, issue_id: str) -> None:
    blocking = ctx.state.get("paper_poster_html_content_contract_blocking")
    if not isinstance(blocking, dict):
        return
    issue_id = str(issue_id or "").strip()
    if not issue_id:
        return
    issue_ids = [
        str(item)
        for item in (blocking.get("issue_ids") or [])
        if str(item or "") != issue_id
    ]
    issues = [
        item for item in (blocking.get("issues") or [])
        if not (
            isinstance(item, dict)
            and str(item.get("issue_id") or "") == issue_id
        )
    ]
    if issue_ids or issues:
        blocking.update({
            "issue_count": len(issue_ids),
            "issue_ids": issue_ids,
            "issues": issues[:12],
        })
    else:
        ctx.state.pop("paper_poster_html_content_contract_blocking", None)


def _content_contract_hard_feedback_state(ctx: ToolContext) -> dict[str, int | bool]:
    raw_limit = os.getenv("POSTER_CONTENT_CONTRACT_HARD_FEEDBACK_PASSES", "").strip()
    try:
        limit = int(raw_limit) if raw_limit else 2
    except ValueError:
        limit = 2
    limit = max(1, limit)
    count = _safe_int(ctx.state.get("paper_poster_html_content_contract_hard_feedback_count")) + 1
    ctx.state["paper_poster_html_content_contract_hard_feedback_count"] = count
    return {
        "count": count,
        "limit": limit,
        "exhausted": count > limit,
    }


def _deterministic_layout_repair_enabled(ctx: ToolContext | None = None) -> bool:
    raw = (
        os.getenv("AUTODESIGN_DETERMINISTIC_LAYOUT_REPAIR", "")
        or os.getenv("DESIGN_ANYTHING_DETERMINISTIC_LAYOUT_REPAIR", "")
        or os.getenv("POSTER_DETERMINISTIC_LAYOUT_REPAIR", "")
    ).strip().lower()
    if raw:
        return raw in {"1", "true", "yes", "on"}
    return ctx is not None and effective_poster_harness_mode(ctx.settings) == "dogfood"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _dogfood_dense_mode(ctx: ToolContext) -> bool:
    if effective_poster_harness_mode(ctx.settings) != "dogfood":
        return False
    contract = ctx.state.get("poster_plan_contract") if isinstance(ctx.state, dict) else None
    if not isinstance(contract, dict):
        return True
    return (
        str(contract.get("reference_profile") or "") in {"research_synthesis_dense", "conference_editorial_flow"}
        or isinstance(contract.get("content_fill_targets"), dict)
    )


def _editorial_flow_mode(ctx: ToolContext) -> bool:
    contract = ctx.state.get("poster_plan_contract") if isinstance(ctx.state, dict) else None
    return isinstance(contract, dict) and str(contract.get("reference_profile") or "") == "conference_editorial_flow"


def _active_reference_style_contract(ctx: ToolContext) -> dict[str, Any]:
    has_reference_metadata = bool(
        ctx.state.get("reference_poster")
        or ctx.state.get("reference_poster_path")
        or (ctx.run_dir / "reference_poster" / "reference_source_metadata.json").exists()
    )
    if not has_reference_metadata:
        return {}
    value = ctx.state.get("reference_style_contract") if isinstance(ctx.state, dict) else None
    if not isinstance(value, dict) or not value:
        path = ctx.run_dir / "reference_style_contract.json"
        if not path.exists():
            return {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        value = loaded if isinstance(loaded, dict) else {}
    if (
        str(value.get("transfer_mode") or "") != "reference_first_reconstruction"
        or not str(value.get("style_reference_id") or "").strip()
        or not isinstance(value.get("style_tokens"), dict)
        or _safe_int(value.get("version"), 0) not in {3, 4}
    ):
        return {}
    ctx.state["reference_style_contract"] = value
    return value


def _reference_region_count(ctx: ToolContext) -> int:
    reference = _active_reference_style_contract(ctx)
    tokens = reference.get("style_tokens") if isinstance(reference.get("style_tokens"), dict) else {}
    columns = tokens.get("column_structure") if isinstance(tokens.get("column_structure"), dict) else {}
    per_column = columns.get("major_sections_per_column")
    if isinstance(per_column, list) and 2 <= len(per_column) <= 6:
        return len(per_column)
    return 0


def _reference_style_contract_error(
    soup: BeautifulSoup,
    css: str,
    ctx: ToolContext,
    *,
    root_shell: dict[str, Any] | None = None,
    bboxes: dict[str, dict[str, Any]] | None = None,
) -> ToolResultRecord | None:
    reference = _active_reference_style_contract(ctx)
    if not reference:
        return None
    open_tag = _paper_poster_main_open_tag(root_shell)
    check_soup = BeautifulSoup(
        f"<html><body>{open_tag}{str(soup)}</main></body></html>",
        "html.parser",
    )
    tokens = reference.get("style_tokens") if isinstance(reference.get("style_tokens"), dict) else {}
    issues: list[dict[str, Any]] = []

    column_structure = tokens.get("column_structure") if isinstance(tokens.get("column_structure"), dict) else {}
    expected_per_column = column_structure.get("major_sections_per_column")
    if isinstance(expected_per_column, list) and 2 <= len(expected_per_column) <= 6:
        expected = [max(1, min(3, _safe_int(item, 1))) for item in expected_per_column]
        columns = check_soup.select(".poster-column")
        expected_column_count = len(expected)
        if len(columns) != expected_column_count:
            issues.append({
                "failure_kind": "reference_column_count_mismatch",
                "expected": expected_column_count,
                "actual": len(columns),
                "repair": (
                    f"Restore exactly {expected_column_count} .poster-column body regions from the reference blueprint."
                ),
            })
        else:
            actual = [
                sum(
                    1 for child in column.find_all(recursive=False)
                    if isinstance(child, Tag) and "poster-section" in _class_tokens(child)
                )
                for column in columns
            ]
            if actual != expected:
                issues.append({
                    "failure_kind": "reference_major_section_count_mismatch",
                    "expected": expected,
                    "actual": actual,
                    "repair": "Merge extra major sections into h3/subsection/inline-label content inside the reference-owned major sections.",
                })
            descendant_counts = [len(column.select(".poster-section")) for column in columns]
            if descendant_counts != expected:
                issues.append({
                    "failure_kind": "reference_nested_major_section_leakage",
                    "expected": expected,
                    "actual_descendant_counts": descendant_counts,
                    "repair": "Remove nested .poster-section panels; represent nested topics with h3/subsection/inline-label content only.",
                })
            if isinstance(bboxes, dict):
                geometry_issue = _reference_region_geometry_issue(
                    columns,
                    bboxes,
                    tokens.get("layout_rhythm") if isinstance(tokens.get("layout_rhythm"), dict) else {},
                )
                if geometry_issue:
                    issues.append(geometry_issue)

    chrome = tokens.get("chrome_treatment") if isinstance(tokens.get("chrome_treatment"), dict) else {}
    chrome_nodes = check_soup.select(".reference-chrome,[data-style-role='chrome-layer']")
    direct_chrome_nodes = [
        node for node in chrome_nodes
        if isinstance(node, Tag)
        and isinstance(node.parent, Tag)
        and "paper-poster" in _class_tokens(node.parent)
    ]
    chrome_present = bool(chrome.get("present"))
    if chrome_present and (len(chrome_nodes) != 1 or len(direct_chrome_nodes) != 1):
        issues.append({
            "failure_kind": "reference_chrome_layer_missing",
            "repair": (
                "Restore exactly one root-level .reference-chrome/[data-style-role=chrome-layer] behind content; "
                "do not attach ornamental routes to sections or columns."
            ),
        })
    elif chrome_present and direct_chrome_nodes and not direct_chrome_nodes[0].find(True):
        issues.append({
            "failure_kind": "reference_chrome_layer_empty",
            "repair": (
                "Rebuild the observed chrome as explicit non-content child elements inside the root-level chrome layer; "
                "an empty marker does not reproduce the reference geometry."
            ),
        })
    elif chrome_present:
        measured_chrome_visible = _reference_measured_chrome_visible(ctx)
        if measured_chrome_visible is False:
            issues.append({
                "failure_kind": "reference_chrome_layer_not_visible",
                "repair": (
                    "Restore at least one visible reference-matched route/rail child inside the root chrome layer; "
                    "hidden or transparent marker elements do not reproduce the reference."
                ),
            })
    elif not chrome_present and chrome_nodes:
        issues.append({
            "failure_kind": "reference_unexpected_chrome_layer",
            "repair": "Remove ornamental chrome because the active reference contract does not contain it.",
        })
    measured_pseudo_chrome = _reference_measured_content_pseudo_chrome(ctx) if chrome_present else None
    pseudo_chrome_selectors = (
        measured_pseudo_chrome
        if measured_pseudo_chrome is not None
        else _reference_content_pseudo_chrome_selectors(css) if chrome_present else []
    )
    if pseudo_chrome_selectors:
        issues.append({
            "failure_kind": "reference_chrome_attached_to_content_regions",
            "selectors": pseudo_chrome_selectors[:8],
            "repair": (
                "Move ornamental routes/rails out of section/column pseudo-elements and into the root-level "
                "reference chrome layer, confined to gutters or section edges."
            ),
        })
    if chrome_present:
        issues.extend(_reference_chrome_crossing_issues(ctx))

    lead_band = tokens.get("lead_band") if isinstance(tokens.get("lead_band"), dict) else {}
    if bool(lead_band.get("present")) and not check_soup.select_one(
        "[data-style-role='lead-band'],[data-reference-role='lead-band'],.reference-lead-band,.lead-band"
    ):
        issues.append({
            "failure_kind": "reference_lead_band_missing",
            "repair": "Restore the reference-defined full-width target-paper lead band directly below the identity header.",
        })

    if isinstance(bboxes, dict):
        missing_border_measurements = _reference_missing_border_measurements(check_soup, bboxes, ctx=ctx)
        if missing_border_measurements:
            issues.append({
                "failure_kind": "reference_style_computed_measurement_unavailable",
                "missing_targets": missing_border_measurements[:12],
                "repair": (
                    "Re-run browser measurement before accepting reference fidelity; do not infer visible border "
                    "state from raw CSS when computed styles are unavailable."
                ),
            })

    header = tokens.get("header_treatment") if isinstance(tokens.get("header_treatment"), dict) else {}
    if isinstance(bboxes, dict) and str(header.get("top_rule") or "none") == "none":
        header_sides = (
            ("top", "right", "bottom", "left")
            if str(header.get("mode") or "") == "open_white"
            else ("top",)
        )
        issues.extend(_reference_computed_border_issues(
            check_soup,
            bboxes,
            ".paper-poster,.poster-header,.identity-header,[data-panel-role='identity_header']",
            header_sides,
            "reference_header_top_rule_leakage",
            "Remove the default colored header/root frame and keep the reference identity area open.",
            ctx=ctx,
        ))
    if isinstance(bboxes, dict) and str(header.get("mode") or "") == "open_white":
        roles = reference.get("color_system", {}).get("roles", {})
        expected_background = str(roles.get("background") or "#FFFFFF") if isinstance(roles, dict) else "#FFFFFF"
        issues.extend(_reference_computed_background_issues(
            check_soup,
            bboxes,
            ".paper-poster,.poster-header,.identity-header,[data-panel-role='identity_header']",
            expected_background,
            ctx=ctx,
        ))
        issues.extend(_reference_header_chrome_issues(
            check_soup,
            bboxes,
            expected_alignment=str(header.get("alignment") or "left"),
            expected_background=expected_background,
            ctx=ctx,
        ))
    if isinstance(bboxes, dict) and str(header.get("alignment") or "") in {"left", "center", "right"}:
        issues.extend(_reference_computed_alignment_issues(
            check_soup,
            bboxes,
            ".poster-header h1,.identity-header h1,[data-panel-role='identity_header'] h1,.poster-title",
            str(header.get("alignment")),
            ctx=ctx,
        ))

    structure = tokens.get("section_structure") if isinstance(tokens.get("section_structure"), dict) else {}
    if isinstance(bboxes, dict) and str(structure.get("inter_section_dividers") or "none") == "none":
        issues.extend(_reference_computed_border_issues(
            check_soup,
            bboxes,
            ".poster-section",
            ("top", "bottom"),
            "reference_section_divider_leakage",
            "Remove outer top/bottom borders from .poster-section; keep only the reference heading treatment.",
            ctx=ctx,
        ))
    if isinstance(bboxes, dict) and str(structure.get("vertical_accent_rules") or "none") == "none":
        issues.extend(_reference_computed_border_issues(
            check_soup,
            bboxes,
            ".lead-key,.readout,.callout,[data-role*='readout'],[data-role*='callout']",
            ("left",),
            "reference_vertical_rule_leakage",
            "Remove colored side stems that are absent from the reference.",
            ctx=ctx,
        ))

    formula = tokens.get("formula_treatment") if isinstance(tokens.get("formula_treatment"), dict) else {}
    if isinstance(bboxes, dict) and str(formula.get("frame") or "none") == "none":
        issues.extend(_reference_computed_border_issues(
            check_soup,
            bboxes,
            ".formula,.formula-slot,.math-block,.equation-block,[data-block-kind='formula'],[data-role*='formula']",
            ("top", "right", "bottom", "left"),
            "reference_formula_frame_leakage",
            "Remove formula separator rules and frames; keep equations in normal content flow.",
            ctx=ctx,
        ))
        issues.extend(_reference_formula_internal_rule_issues(ctx=ctx))

    table = tokens.get("table_treatment") if isinstance(tokens.get("table_treatment"), dict) else {}
    if isinstance(bboxes, dict) and (
        not bool(table.get("observed")) or str(table.get("rule_style") or "none") == "none"
    ):
        issues.extend(_reference_computed_border_issues(
            check_soup,
            bboxes,
            "table,.booktabs,.native-table",
            ("top", "bottom"),
            "reference_table_outer_rule_leakage",
            "Remove native-table outer top/bottom rules; one subtle header underline remains allowed.",
            ctx=ctx,
        ))
        issues.extend(_reference_table_internal_rule_issues(ctx))

    if not issues:
        return None
    return obs_error(
        "propose_paper_poster_html found visible structure or rules that conflict with the active reference poster.",
        category="validation",
        payload={
            "issue_id": "paper_poster_html_reference_style_contract_failed",
            "primary_blocking_issue_id": "paper_poster_html_reference_style_contract_failed",
            "repair_route": "restore_reference_first_visual_structure",
            "severity": "hard",
            "blocks_soft_accept": True,
            "style_reference_id": reference.get("style_reference_id"),
            "contract_version": reference.get("version"),
            "issues": issues[:12],
            "hint": (
                "Restore the reference-owned top-level structure and visible rule treatment without changing "
                "target-paper text, source ids, figures, tables, or evidence. Do not restore the default AutoDesign skin."
            ),
        },
    )


def _reference_content_pseudo_chrome_selectors(css: str) -> list[str]:
    selectors: list[str] = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", str(css or "")):
        selector_text = match.group(1).strip()
        declarations = match.group(2)
        content = re.search(r"(?:^|;)\s*content\s*:\s*([^;]+)", declarations, flags=re.I)
        if not content or content.group(1).strip().lower() in {"none", "normal"}:
            continue
        if re.search(r"(?:^|;)\s*display\s*:\s*none\b", declarations, flags=re.I):
            continue
        if re.search(r"(?:^|;)\s*opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)", declarations, flags=re.I):
            continue
        for selector in selector_text.split(","):
            pseudo = re.search(r"::(?:before|after)", selector, flags=re.I)
            if not pseudo:
                continue
            prefix = selector[:pseudo.start()].strip()
            final_compound = re.split(r"[\s>+~]+", prefix)[-1]
            if re.search(
                r"(?:\.poster-(?:section|column)|\[data-style-role\s*=\s*['\"]?(?:section|column)['\"]?\]|^section(?:[.#\[].*)?$)",
                final_compound,
                flags=re.I,
            ):
                selectors.append(selector.strip())
    return selectors


def _reference_measured_content_pseudo_chrome(ctx: ToolContext) -> list[str] | None:
    measurements = (
        ctx.state.get("paper_poster_html_reference_rule_measurements")
        if isinstance(ctx.state, dict)
        else None
    )
    if not isinstance(measurements, dict) or "contentRegionPseudos" not in measurements:
        return None
    items = measurements.get("contentRegionPseudos")
    selectors: list[str] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        style = _computed_style_for_measurement(item)
        content = str(item.get("content") or "").strip().lower()
        if _reference_style_measurement_hidden(style) or content in {"", "none", "normal"}:
            continue
        visible_background = not _reference_computed_color_transparent(
            str(style.get("background_color") or "transparent")
        )
        visible_border = any(
            _reference_computed_border_visible(style, side)
            for side in ("top", "right", "bottom", "left")
        )
        shadow = str(style.get("box_shadow") or "").strip().lower()
        if visible_background or visible_border or (shadow and shadow != "none"):
            selectors.append(
                f"{item.get('tagName') or 'content-region'}{item.get('pseudo') or ''}"
            )
    return selectors


def _reference_measured_chrome_visible(ctx: ToolContext) -> bool | None:
    measurements = (
        ctx.state.get("paper_poster_html_reference_rule_measurements")
        if isinstance(ctx.state, dict)
        else None
    )
    if not isinstance(measurements, dict) or "chromeNodes" not in measurements:
        return None
    items = measurements.get("chromeNodes")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        style = _computed_style_for_measurement(item)
        if _reference_style_measurement_hidden(style) or _reference_pseudo_measurement_empty(item):
            continue
        visible_background = not _reference_computed_color_transparent(
            str(style.get("background_color") or "transparent")
        )
        visible_border = any(
            _reference_computed_border_visible(style, side)
            for side in ("top", "right", "bottom", "left")
        )
        shadow = str(style.get("box_shadow") or "").strip().lower()
        if visible_background or visible_border or (shadow and shadow != "none"):
            return True
    return False


def _reference_chrome_crossing_issues(ctx: ToolContext) -> list[dict[str, Any]]:
    measurements = (
        ctx.state.get("paper_poster_html_reference_rule_measurements")
        if isinstance(ctx.state, dict)
        else None
    )
    if not isinstance(measurements, dict):
        return []
    chrome_nodes = measurements.get("chromeNodes")
    content_regions = measurements.get("contentRegions")
    if not isinstance(chrome_nodes, list) or not isinstance(content_regions, list):
        return []

    region_rects = [
        rect for item in content_regions
        if isinstance(item, dict)
        and (rect := _reference_measurement_rect(item, inset_px=8.0)) is not None
    ]
    crossings: list[dict[str, Any]] = []
    for item in chrome_nodes:
        if not isinstance(item, dict):
            continue
        style = _computed_style_for_measurement(item)
        if _reference_style_measurement_hidden(style) or _reference_pseudo_measurement_empty(item):
            continue
        for paint_rect in _reference_chrome_paint_rects(item, style):
            for region_rect in region_rects:
                overlap = _reference_rect_intersection(paint_rect, region_rect)
                if overlap is None:
                    continue
                crossings.append({
                    "chrome_target": str(item.get("tagName") or "chrome-node"),
                    "chrome_rect": paint_rect,
                    "content_rect": region_rect,
                    "overlap_rect": overlap,
                })
                break
            if crossings:
                break
        if crossings:
            break
    if not crossings:
        return []
    return [{
        "failure_kind": "reference_chrome_crosses_content",
        "crossings": crossings,
        "repair": (
            "Move ornamental chrome into measured gutters or section-edge space; no painted chrome pixels "
            "may enter the interior of a text, figure, table, or formula section."
        ),
    }]


def _reference_chrome_paint_rects(
    item: dict[str, Any],
    style: dict[str, Any],
) -> list[dict[str, float]]:
    rect = _reference_measurement_rect(item)
    if rect is None:
        return []
    painted: list[dict[str, float]] = []
    if not _reference_computed_color_transparent(
        str(style.get("background_color") or "transparent")
    ):
        painted.append(rect)
    x, y, width, height = (rect[key] for key in ("x", "y", "w", "h"))
    for side in ("top", "right", "bottom", "left"):
        if not _reference_computed_border_visible(style, side):
            continue
        thickness = min(
            width if side in {"left", "right"} else height,
            max(0.0, _safe_float(style.get(f"border_{side}_width_px"), 0.0)),
        )
        if thickness <= 0:
            continue
        if side == "top":
            painted.append({"x": x, "y": y, "w": width, "h": thickness})
        elif side == "right":
            painted.append({"x": x + width - thickness, "y": y, "w": thickness, "h": height})
        elif side == "bottom":
            painted.append({"x": x, "y": y + height - thickness, "w": width, "h": thickness})
        else:
            painted.append({"x": x, "y": y, "w": thickness, "h": height})
    shadow = str(style.get("box_shadow") or "").strip().lower()
    if not painted and shadow and shadow != "none":
        painted.append(rect)
    return painted


def _reference_pseudo_measurement_empty(item: dict[str, Any]) -> bool:
    if not str(item.get("pseudo") or ""):
        return False
    return str(item.get("content") or "").strip().lower() in {"", "none", "normal"}


def _reference_measurement_rect(
    item: dict[str, Any],
    *,
    inset_px: float = 0.0,
) -> dict[str, float] | None:
    x = _safe_float(item.get("x"), 0.0) + inset_px
    y = _safe_float(item.get("y"), 0.0) + inset_px
    width = _safe_float(item.get("w"), 0.0) - inset_px * 2
    height = _safe_float(item.get("h"), 0.0) - inset_px * 2
    if width <= 0 or height <= 0:
        return None
    return {"x": x, "y": y, "w": width, "h": height}


def _reference_rect_intersection(
    left: dict[str, float],
    right: dict[str, float],
) -> dict[str, float] | None:
    x = max(left["x"], right["x"])
    y = max(left["y"], right["y"])
    right_edge = min(left["x"] + left["w"], right["x"] + right["w"])
    bottom_edge = min(left["y"] + left["h"], right["y"] + right["h"])
    if right_edge <= x or bottom_edge <= y:
        return None
    return {"x": x, "y": y, "w": right_edge - x, "h": bottom_edge - y}


def _reference_region_geometry_issue(
    columns: list[Tag],
    bboxes: dict[str, dict[str, Any]],
    layout_rhythm: dict[str, Any],
) -> dict[str, Any] | None:
    expected_boxes = layout_rhythm.get("region_boxes")
    if not isinstance(expected_boxes, list) or len(expected_boxes) != len(columns):
        return None
    root = bboxes.get("__paper_poster_root__")
    if not isinstance(root, dict):
        return None
    root_w = _safe_float(root.get("w"), 0.0)
    root_h = _safe_float(root.get("h"), 0.0)
    if root_w <= 0 or root_h <= 0:
        return None
    actual_boxes: list[dict[str, float]] = []
    deltas: list[dict[str, float]] = []
    for index, (column, expected) in enumerate(zip(columns, expected_boxes, strict=True)):
        measured = _bbox_for_tag(column, bboxes)
        if not isinstance(measured, dict) or not isinstance(expected, dict):
            return None
        actual = {
            "x_pct": (_safe_float(measured.get("x"), 0.0) / root_w) * 100,
            "y_pct": (_safe_float(measured.get("y"), 0.0) / root_h) * 100,
            "w_pct": (_safe_float(measured.get("w"), 0.0) / root_w) * 100,
            "h_pct": (_safe_float(measured.get("h"), 0.0) / root_h) * 100,
        }
        delta = {
            key: abs(actual[key] - _safe_float(expected.get(key), actual[key]))
            for key in ("x_pct", "y_pct", "w_pct", "h_pct")
        }
        actual_boxes.append({key: round(value, 3) for key, value in actual.items()})
        deltas.append({key: round(value, 3) for key, value in delta.items()})
    if not any(
        delta["x_pct"] > 4.0
        or delta["y_pct"] > 4.0
        or delta["w_pct"] > 5.0
        or delta["h_pct"] > 5.0
        for delta in deltas
    ):
        return None
    return {
        "failure_kind": "reference_region_geometry_mismatch",
        "expected_region_boxes": expected_boxes,
        "actual_region_boxes": actual_boxes,
        "absolute_delta_pct": deltas,
        "repair": (
            "Restore the staged reference blueprint's measured body-region positions and proportions; "
            "do not replace asymmetric/weighted geometry with equal columns."
        ),
    }


def _reference_computed_border_issues(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, Any]],
    selector: str,
    sides: tuple[str, ...],
    failure_kind: str,
    repair: str,
    *,
    ctx: ToolContext | None = None,
) -> list[dict[str, Any]]:
    elements = []
    seen: set[int] = set()
    try:
        selected = soup.select(selector)
    except Exception:
        selected = []
    for tag in selected:
        if isinstance(tag, Tag) and id(tag) not in seen:
            elements.append(tag)
            seen.add(id(tag))
    if not elements:
        return []
    issues = []
    for tag in elements:
        block_id = (
            "__paper_poster_root__"
            if "paper-poster" in _class_tokens(tag)
            else str(tag.get("data-block-id") or "").strip()
        )
        style = _reference_measured_style(bboxes, block_id, ctx=ctx)
        if _reference_style_measurement_hidden(style):
            continue
        for side in sides:
            if not _reference_computed_border_visible(style, side):
                continue
            issues.append({
                "failure_kind": failure_kind,
                "target": _reference_tag_descriptor(tag),
                "border_side": side,
                "actual": {
                    "width_px": style.get(f"border_{side}_width_px"),
                    "style": style.get(f"border_{side}_style"),
                    "color": style.get(f"border_{side}_color"),
                },
                "repair": repair,
            })
            break
    return issues[:6]


def _reference_missing_border_measurements(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, Any]],
    *,
    ctx: ToolContext | None = None,
) -> list[str]:
    selector = (
        ".paper-poster,.poster-header,.identity-header,[data-panel-role='identity_header'],"
        ".poster-section,.formula,.formula-slot,.math-block,.equation-block,"
        "[data-block-kind='formula'],[data-role*='formula'],table"
    )
    missing: list[str] = []
    seen: set[str] = set()
    for tag in soup.select(selector):
        if not isinstance(tag, Tag):
            continue
        block_id = (
            "__paper_poster_root__"
            if "paper-poster" in _class_tokens(tag)
            else str(tag.get("data-block-id") or "").strip()
        )
        descriptor = _reference_tag_descriptor(tag)
        key = block_id or descriptor
        if key in seen:
            continue
        seen.add(key)
        style = _reference_measured_style(bboxes, block_id, ctx=ctx)
        if _reference_style_measurement_hidden(style):
            continue
        if not all(
            f"border_{side}_width_px" in style
            and f"border_{side}_style" in style
            and f"border_{side}_color" in style
            for side in ("top", "right", "bottom", "left")
        ):
            missing.append(descriptor)
    return missing


def _reference_measured_style(
    bboxes: dict[str, dict[str, Any]],
    block_id: str,
    *,
    ctx: ToolContext | None = None,
) -> dict[str, Any]:
    measured = bboxes.get(block_id) if block_id else None
    if isinstance(measured, dict) and isinstance(measured.get("_computed_style"), dict):
        return measured["_computed_style"]
    style_measurements = (
        ctx.state.get("paper_poster_html_computed_style_measurements")
        if ctx is not None and isinstance(ctx.state, dict)
        else None
    )
    style = style_measurements.get(block_id) if isinstance(style_measurements, dict) else None
    return style if isinstance(style, dict) else {}


def _reference_computed_background_issues(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, Any]],
    selector: str,
    expected_background: str,
    *,
    ctx: ToolContext | None = None,
) -> list[dict[str, Any]]:
    expected_rgb = _reference_css_rgb(expected_background) or (255, 255, 255)
    issues: list[dict[str, Any]] = []
    for tag in soup.select(selector):
        if not isinstance(tag, Tag):
            continue
        block_id = (
            "__paper_poster_root__"
            if "paper-poster" in _class_tokens(tag)
            else str(tag.get("data-block-id") or "").strip()
        )
        style = _reference_measured_style(bboxes, block_id, ctx=ctx)
        if _reference_style_measurement_hidden(style):
            continue
        background = str(style.get("background_color") or "transparent")
        if _reference_computed_color_transparent(background):
            continue
        actual_rgb = _reference_css_rgb(background)
        if actual_rgb is None or max(abs(actual_rgb[i] - expected_rgb[i]) for i in range(3)) <= 24:
            continue
        issues.append({
            "failure_kind": "reference_header_background_leakage",
            "target": _reference_tag_descriptor(tag),
            "actual_background": background,
            "expected_background": expected_background,
            "repair": "Remove the filled header skin and restore the reference open-white identity area.",
        })
    return issues[:4]


def _reference_computed_alignment_issues(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, Any]],
    selector: str,
    expected_alignment: str,
    *,
    ctx: ToolContext | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for tag in soup.select(selector):
        if not isinstance(tag, Tag):
            continue
        block_id = str(tag.get("data-block-id") or "").strip()
        style = _reference_measured_style(bboxes, block_id, ctx=ctx)
        if _reference_style_measurement_hidden(style):
            continue
        actual = str(style.get("align") or "").strip().lower()
        if not actual or actual == expected_alignment:
            continue
        issues.append({
            "failure_kind": "reference_header_alignment_mismatch",
            "target": _reference_tag_descriptor(tag),
            "actual_alignment": actual,
            "expected_alignment": expected_alignment,
            "repair": "Restore the reference title alignment instead of the default header alignment.",
        })
    return issues[:4]


def _reference_css_rgb(value: str) -> tuple[int, int, int] | None:
    text = str(value or "").strip().lower()
    hex_match = re.fullmatch(r"#([0-9a-f]{6})", text)
    if hex_match:
        raw = hex_match.group(1)
        return tuple(int(raw[index:index + 2], 16) for index in (0, 2, 4))
    rgb_match = re.match(r"rgba?\(\s*(\d+(?:\.\d+)?)\D+(\d+(?:\.\d+)?)\D+(\d+(?:\.\d+)?)", text)
    if not rgb_match:
        return None
    return tuple(max(0, min(255, int(round(float(part))))) for part in rgb_match.groups())


def _reference_style_measurement_hidden(style: dict[str, Any]) -> bool:
    return (
        style.get("rendered") is False
        or str(style.get("display") or "").strip().lower() == "none"
        or str(style.get("visibility") or "").strip().lower() == "hidden"
        or _safe_float(style.get("opacity"), 1.0) <= 0.001
    )


def _reference_header_chrome_issues(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, Any]],
    *,
    expected_alignment: str,
    expected_background: str,
    ctx: ToolContext | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    expected_rgb = _reference_css_rgb(expected_background) or (255, 255, 255)
    body_style = _reference_measured_style(bboxes, "__paper_poster_body__", ctx=ctx)
    body_background = str(body_style.get("background_color") or "transparent")
    body_rgb = _reference_css_rgb(body_background)
    if (
        not _reference_style_measurement_hidden(body_style)
        and not _reference_computed_color_transparent(body_background)
        and body_rgb is not None
        and max(abs(body_rgb[index] - expected_rgb[index]) for index in range(3)) > 24
    ):
        issues.append({
            "failure_kind": "reference_header_background_leakage",
            "target": "body",
            "actual_background": body_background,
            "expected_background": expected_background,
            "repair": "Restore the reference open-white canvas behind the transparent poster/header shell.",
        })
    for tag in soup.select(".paper-poster,.poster-header,.identity-header,[data-panel-role='identity_header']"):
        if not isinstance(tag, Tag):
            continue
        block_id = (
            "__paper_poster_root__"
            if "paper-poster" in _class_tokens(tag)
            else str(tag.get("data-block-id") or "").strip()
        )
        style = _reference_measured_style(bboxes, block_id, ctx=ctx)
        if _reference_style_measurement_hidden(style):
            continue
        shadow = str(style.get("box_shadow") or "").strip().lower()
        outline_visible = (
            _safe_float(style.get("outline_width_px")) > 0
            and str(style.get("outline_style") or "none").lower() not in {"none", "hidden"}
            and not _reference_computed_color_transparent(str(style.get("outline_color") or "transparent"))
        )
        if (shadow and shadow != "none") or outline_visible:
            issues.append({
                "failure_kind": "reference_header_chrome_leakage",
                "target": _reference_tag_descriptor(tag),
                "repair": "Remove header/root outline, shadow, and decorative frame chrome absent from the reference.",
            })
        if expected_alignment == "left" and "paper-poster" not in _class_tokens(tag):
            display = str(style.get("display") or "").lower()
            flex_direction = str(style.get("flex_direction") or "row").lower()
            centered_layout = (
                str(style.get("align") or "").lower() == "center"
                or str(style.get("justify_items") or "").lower() == "center"
                or (display == "flex" and flex_direction.startswith("column") and str(style.get("align_items") or "").lower() == "center")
                or (display == "flex" and not flex_direction.startswith("column") and str(style.get("justify_content") or "").lower() == "center")
                or (display == "grid" and str(style.get("justify_content") or "").lower() == "center")
            )
            if centered_layout:
                issues.append({
                    "failure_kind": "reference_header_layout_alignment_mismatch",
                    "target": _reference_tag_descriptor(tag),
                    "expected_alignment": expected_alignment,
                    "repair": "Restore the reference left-aligned identity cluster; do not center it through flex/grid layout.",
                })
    measurements = (
        ctx.state.get("paper_poster_html_reference_rule_measurements")
        if ctx is not None and isinstance(ctx.state, dict)
        else None
    )
    header_pseudos = measurements.get("headerPseudos") if isinstance(measurements, dict) else None
    for item in header_pseudos if isinstance(header_pseudos, list) else []:
        if not isinstance(item, dict):
            continue
        style = _computed_style_for_measurement(item)
        content = str(item.get("content") or "").strip().lower()
        if _reference_style_measurement_hidden(style) or content in {"", "none", "normal"}:
            continue
        background = str(style.get("background_color") or "transparent")
        actual_rgb = _reference_css_rgb(background)
        visible_background = (
            not _reference_computed_color_transparent(background)
            and actual_rgb is not None
            and max(abs(actual_rgb[index] - expected_rgb[index]) for index in range(3)) > 24
        )
        visible_border = any(
            _reference_computed_border_visible(style, side)
            for side in ("top", "right", "bottom", "left")
        )
        shadow = str(style.get("box_shadow") or "").strip().lower()
        if not visible_background and not visible_border and (not shadow or shadow == "none"):
            continue
        issues.append({
            "failure_kind": "reference_header_pseudo_chrome_leakage",
            "target": f"{item.get('tagName') or 'header'}{item.get('pseudo') or ''}",
            "repair": "Remove header/root pseudo-element bars, frames, and decorative chrome absent from the reference.",
        })
    return issues[:8]


def _reference_formula_internal_rule_issues(
    *,
    ctx: ToolContext | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    measurements = (
        ctx.state.get("paper_poster_html_reference_rule_measurements")
        if ctx is not None and isinstance(ctx.state, dict)
        else None
    )
    formula_rules = measurements.get("formulaRules") if isinstance(measurements, dict) else None
    for item in formula_rules if isinstance(formula_rules, list) else []:
        if not isinstance(item, dict):
            continue
        style = _computed_style_for_measurement(item)
        if _reference_style_measurement_hidden(style):
            continue
        issues.append({
            "failure_kind": "reference_formula_internal_rule_leakage",
            "target": "formula hr",
            "repair": "Remove visible horizontal-rule elements from formula flow; keep equations unframed.",
        })
    formula_pseudos = measurements.get("formulaPseudos") if isinstance(measurements, dict) else None
    for item in formula_pseudos if isinstance(formula_pseudos, list) else []:
        if not isinstance(item, dict):
            continue
        style = _computed_style_for_measurement(item)
        content = str(item.get("content") or "").strip().lower()
        if _reference_style_measurement_hidden(style) or content in {"", "none", "normal"}:
            continue
        if not any(_reference_computed_border_visible(style, side) for side in ("top", "bottom")):
            continue
        issues.append({
            "failure_kind": "reference_formula_pseudo_rule_leakage",
            "target": f"{item.get('tagName') or 'formula'}{item.get('pseudo') or ''}",
            "repair": "Remove formula ::before/::after separator rules that are absent from the reference.",
        })
    return issues[:4]


def _reference_table_internal_rule_issues(ctx: ToolContext | None) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    measurements = (
        ctx.state.get("paper_poster_html_reference_rule_measurements")
        if ctx is not None and isinstance(ctx.state, dict)
        else None
    )
    table_rows = measurements.get("tableRows") if isinstance(measurements, dict) else None
    for item in table_rows if isinstance(table_rows, list) else []:
        if not isinstance(item, dict):
            continue
        style = _computed_style_for_measurement(item)
        if _reference_style_measurement_hidden(style):
            continue
        if not any(_reference_computed_border_visible(style, side) for side in ("top", "bottom")):
            continue
        issues.append({
            "failure_kind": "reference_table_row_rule_leakage",
            "target": f"{item.get('tagName') or 'table-row'}: {str(item.get('text') or '')[:80]}",
            "repair": "Remove row-by-row table rules; retain at most one subtle header underline.",
        })
    return issues[:6]


def _reference_computed_border_visible(style: dict[str, Any], side: str) -> bool:
    width = _safe_float(style.get(f"border_{side}_width_px"), 0.0)
    border_style = str(style.get(f"border_{side}_style") or "none").strip().lower()
    color = str(style.get(f"border_{side}_color") or "transparent").strip().lower()
    if width <= 0 or border_style in {"none", "hidden"}:
        return False
    if _reference_computed_color_transparent(color):
        return False
    return True


def _reference_computed_color_transparent(color: str) -> bool:
    text = str(color or "").strip().lower()
    if not text or text == "transparent":
        return True
    if re.search(r"rgba\([^)]*,\s*0(?:\.0+)?\s*\)$", text):
        return True
    if re.search(r"rgba?\([^)]*/\s*0(?:\.0+)?%?\s*\)$", text):
        return True
    return False


def _reference_tag_descriptor(tag: Tag) -> str:
    block_id = str(tag.get("data-block-id") or tag.get("id") or "").strip()
    classes = ".".join(sorted(_class_tokens(tag)))
    if block_id:
        return f"{tag.name}#{block_id}" + (f".{classes}" if classes else "")
    return str(tag.name or "element") + (f".{classes}" if classes else "")


def _panel_flow_mode_requested(args: dict[str, Any], soup: BeautifulSoup, ctx: ToolContext) -> bool:
    if _designer_owned_css_token(args.get("browser_flow")):
        return True
    if _dogfood_dense_mode(ctx) and _designer_owned_css_mode(args, soup, ctx):
        return True
    return False


def _ensure_dom_block_ids(
    soup: BeautifulSoup,
    ctx: ToolContext | None = None,
    *,
    panel_flow_mode: bool = False,
) -> None:
    used: set[str] = set()
    counters: dict[str, int] = {}
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        if not _is_meaningful_block_tag(tag):
            if tag.get("data-block-id"):
                del tag["data-block-id"]
            continue
        if str(tag.name or "").lower() == "img":
            source_wrapper = _source_wrap_tag(tag)
            if source_wrapper is not tag and _source_id_for_tag_or_descendant(source_wrapper, ctx):
                if tag.get("data-block-id"):
                    del tag["data-block-id"]
                continue
        if panel_flow_mode and _inside_panel_flow(tag) and not _is_panel_flow_measured_tag(tag, ctx):
            if tag.get("data-block-id"):
                del tag["data-block-id"]
            continue
        existing = str(tag.get("data-block-id") or "").strip()
        if existing:
            block_id = _unique_block_id(_safe_block_id(existing, "block"), used)
        else:
            prefix = _block_id_prefix(tag, ctx)
            counters[prefix] = counters.get(prefix, 0) + 1
            block_id = _unique_block_id(f"{prefix}_{counters[prefix]:02d}", used)
        tag["data-block-id"] = block_id
        used.add(block_id)


def _inside_panel_flow(tag: Tag) -> bool:
    node: Tag | None = tag
    while isinstance(node, Tag):
        if _is_panel_flow_root(node):
            return True
        if str(node.name or "").lower() in {"main", "body", "html"}:
            return False
        node = node.parent
    return False


def _is_panel_flow_measured_tag(tag: Tag, ctx: ToolContext | None = None) -> bool:
    if _is_panel_flow_root(tag):
        return True
    name = str(tag.name or "").lower()
    classes = {str(cls).strip().lower() for cls in (tag.get("class") or []) if str(cls).strip()}
    role_blob = " ".join(
        str(tag.get(key) or "")
        for key in ("data-role", "role", "data-block-kind", "data-kind")
    ).lower()
    if _is_source_flow_unit(tag):
        return True
    if name == "table":
        return True
    if name in {"div", "section"} and (
        bool(classes & {"formula", "formula-slot", "math-block", "equation", "equation-block"})
        or any(token in role_blob for token in ("formula", "equation"))
    ):
        return True
    if name in {"div", "section", "aside", "ul", "ol"} and (
        classes & {"result-band", "metric-row", "native-table", "benchmark-table", "stage-table"}
        or any(token in role_blob for token in ("result-band", "metric", "benchmark", "ablation", "native-table"))
    ):
        return True
    if _is_panel_flow_text_measured_tag(tag):
        return True
    source_id = _source_id_for_tag_or_descendant(tag, ctx)
    if not source_id:
        return False
    if name == "img" and _source_wrap_tag(tag) is not tag:
        return False
    return name in {"figure", "img", "table"}


def _is_panel_flow_text_measured_tag(tag: Tag) -> bool:
    name = str(tag.name or "").lower()
    text = tag.get_text(" ", strip=True)
    word_count = len(re.findall(r"[A-Za-z0-9][A-Za-z0-9_+./:%-]*", text or ""))
    if word_count < 2:
        return False
    if name in {"p", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}:
        return True
    if name not in {"div", "aside"}:
        return False
    classes = {str(cls).strip().lower() for cls in (tag.get("class") or []) if str(cls).strip()}
    role_blob = " ".join(
        str(tag.get(key) or "")
        for key in ("data-role", "role", "class", "data-block-kind", "data-kind")
    ).lower()
    leaf_text_tokens = {
        "callout", "readout", "takeaway", "quote-band", "note", "micro-note",
        "result-band", "metric-row", "native-summary", "pipeline-step",
    }
    if not (classes & leaf_text_tokens or any(token in role_blob for token in leaf_text_tokens)):
        return False
    return not any(
        isinstance(child, Tag)
        and str(child.name or "").lower() in {"p", "li", "blockquote", "h1", "h2", "h3", "h4", "ul", "ol", "table", "figure"}
        for child in tag.find_all(True, recursive=False)
    )


def _is_panel_flow_root(tag: Tag) -> bool:
    name = str(tag.name or "").lower()
    classes = {str(cls).strip() for cls in (tag.get("class") or []) if str(cls).strip()}
    if "flow-panel" in classes or "panel" in classes:
        return name in {"article", "section", "aside", "div", "header", "footer"}
    return bool(
        tag.get("data-panel-role")
        or tag.get("data-slot-id")
        or tag.get("data-panel")
    )


def _is_meaningful_block_tag(tag: Tag) -> bool:
    name = str(tag.name or "").lower()
    if name not in _MEANINGFUL_TAGS:
        return False
    if name in _INLINE_TEXT_TAGS:
        explicit_kind = str(tag.get("data-block-kind") or tag.get("data-kind") or "").strip().lower()
        if explicit_kind in {"metric", "caption", "quote"}:
            return True
        if _has_text_flow_ancestor(tag):
            return False
        text = tag.get_text(" ", strip=True)
        return len(text.split()) >= 5 or bool(tag.get("data-role") or tag.get("data-panel-role"))
    return True


def _has_text_flow_ancestor(tag: Tag) -> bool:
    parent = tag.parent
    while isinstance(parent, Tag):
        if str(parent.name or "").lower() in {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            return True
        parent = parent.parent
    return False


def _block_id_prefix(tag: Tag, ctx: ToolContext | None = None) -> str:
    name = str(tag.name or "").lower()
    source_id = _source_id_for_tag_or_descendant(tag, ctx)
    if source_id and name in {"figure", "picture", "img", "table"}:
        if name == "table" or str(source_id).startswith("ingest_table_"):
            return _safe_block_id(f"visual_{source_id}", "visual")
        return _safe_block_id(f"visual_{source_id}", "visual")
    kind = _infer_block_kind(tag)
    if kind == "group":
        role = _infer_panel_role(tag)
        if role:
            return _safe_block_id(f"panel_{role}", "panel")
        return "panel"
    if kind in {"image", "chart", "embed"}:
        if source_id:
            return _safe_block_id(f"visual_{source_id}", "visual")
        return "visual"
    if kind == "table":
        return "table"
    if kind == "caption":
        return "caption"
    if str(tag.name or "").lower() in {"h1", "h2", "h3"}:
        return "heading"
    return kind


def _unique_block_id(base: str, used: set[str]) -> str:
    block_id = base or "block"
    if block_id not in used:
        return block_id
    idx = 2
    while f"{block_id}_{idx}" in used:
        idx += 1
    return f"{block_id}_{idx}"


def _safe_block_id(value: str, fallback: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "")).strip("_").lower()
    safe = re.sub(r"_+", "_", safe)
    if not safe:
        safe = fallback
    if safe[0].isdigit():
        safe = f"{fallback}_{safe}"
    return safe


def _body_inner_html(soup: BeautifulSoup) -> str:
    return "".join(str(child) for child in soup.contents).strip()


def _hydrate_image_sources_for_measurement(soup: BeautifulSoup, ctx: ToolContext) -> None:
    used_block_ids = {
        str(tag.get("data-block-id") or "").strip()
        for tag in soup.find_all(attrs={"data-block-id": True})
        if isinstance(tag, Tag) and str(tag.get("data-block-id") or "").strip()
    }
    for tag in soup.find_all("img"):
        if not isinstance(tag, Tag):
            continue
        src = str(tag.get("src") or "").strip()
        source_id = _source_id_for_tag(tag, ctx)
        if source_id and not str(tag.get("data-block-id") or "").strip():
            block_id = _unique_block_id(
                _safe_block_id(f"source_image_{source_id}", "source_image"),
                used_block_ids,
            )
            tag["data-block-id"] = block_id
            used_block_ids.add(block_id)
        if src and not _image_src_needs_source_rewrite(src, ctx):
            continue
        src_path = _source_path_for_id(source_id, ctx)
        if not src_path and src:
            src_path = _local_image_path_for_src(src, ctx)
        if src_path:
            tag["src"] = src_path


def _apply_intrinsic_source_visual_bboxes(
    soup: BeautifulSoup,
    ctx: ToolContext,
    canvas: dict[str, Any],
) -> list[dict[str, Any]]:
    repairs: list[dict[str, Any]] = []
    for tag in soup.find_all("img"):
        if not isinstance(tag, Tag):
            continue
        source_id = _source_id_for_tag(tag, ctx)
        if not source_id or not _is_source_visual_tag(tag, source_id):
            continue
        src = str(tag.get("src") or "").strip()
        aspect = _source_aspect_for_id(source_id, ctx, src=src)
        if aspect <= 0:
            continue
        max_bbox = _source_visual_max_bbox_for_tag(tag, canvas)
        if max_bbox is None:
            continue
        fitted = _fit_bbox_to_aspect(max_bbox, aspect)
        wrapper_repair = _adapt_image_only_source_wrapper_to_fit(tag, max_bbox, fitted, canvas)
        image_bbox = {"x": 0, "y": 0, "w": fitted["w"], "h": fitted["h"]} if wrapper_repair else fitted
        _merge_position_style(
            tag,
            image_bbox,
            extra_rules=[
                "object-fit:contain",
                "object-position:center top",
            ],
        )
        repairs.append({
            "source_id": source_id,
            "block_id": str(tag.get("data-block-id") or ""),
            "max_bbox": max_bbox,
            "fitted_bbox": fitted,
            "source_aspect": round(aspect, 4),
            "wrapper_repair": wrapper_repair,
        })
    return repairs


def _fit_measured_source_visual_bboxes(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    ctx: ToolContext,
) -> list[dict[str, Any]]:
    repairs: list[dict[str, Any]] = []
    for tag in soup.find_all("img"):
        if not isinstance(tag, Tag):
            continue
        block_id = str(tag.get("data-block-id") or "").strip()
        if not block_id or block_id not in bboxes:
            continue
        source_id = _source_id_for_tag(tag, ctx)
        if not source_id or not _is_source_visual_tag(tag, source_id):
            continue
        aspect = _source_aspect_for_id(source_id, ctx, src=str(tag.get("src") or ""))
        if aspect <= 0:
            continue
        bbox = _bbox_only(bboxes.get(block_id))
        if bbox is None:
            continue
        fitted = _fit_bbox_to_aspect(bbox, aspect)
        if (
            abs(fitted["x"] - bbox["x"]) <= 1
            and abs(fitted["y"] - bbox["y"]) <= 1
            and abs(fitted["w"] - bbox["w"]) <= 1
            and abs(fitted["h"] - bbox["h"]) <= 1
        ):
            continue
        bboxes[block_id].update(fitted)
        repairs.append({
            "source_id": source_id,
            "block_id": block_id,
            "measured_bbox": bbox,
            "fitted_bbox": fitted,
            "source_aspect": round(aspect, 4),
        })
    return repairs


def _is_source_visual_tag(tag: Tag, source_id: str) -> bool:
    hay = " ".join(
        str(tag.get(key) or "")
        for key in (
            "data-block-id",
            "data-role",
            "role",
            "class",
            "data-source-id",
            "data-layer-id",
            "src",
        )
    ).lower()
    if any(token in hay for token in ("identity", "logo", "badge", "avatar", "qr")):
        return False
    sid = str(source_id or "").lower()
    return (
        sid.startswith(("ingest_fig", "ingest_table", "source_visual"))
        or any(
            token in hay
            for token in (
                "source_visual",
                "source-figure",
                "source_figure",
                "local_evidence",
                "paper_visual",
                "figure",
                "chart",
                "plot",
                "table",
                "source_visuals/",
            )
        )
    )


def _source_visual_max_bbox_for_tag(tag: Tag, canvas: dict[str, Any]) -> dict[str, int] | None:
    own = None if _style_bbox_uses_percent(tag) else _bbox_from_tag_attrs(tag, canvas)
    if own is not None:
        return own
    parent = tag.parent
    while isinstance(parent, Tag):
        bbox = _bbox_from_tag_attrs(parent, canvas)
        if bbox is not None:
            return {"x": 0, "y": 0, "w": bbox["w"], "h": bbox["h"]}
        parent = parent.parent
    return None


def _adapt_image_only_source_wrapper_to_fit(
    tag: Tag,
    max_bbox: dict[str, int],
    fitted: dict[str, int],
    canvas: dict[str, Any],
) -> dict[str, Any] | None:
    if (
        fitted["w"] >= round(max_bbox["w"] * 0.92)
        and fitted["h"] >= round(max_bbox["h"] * 0.92)
    ):
        return None
    wrapper = _image_only_source_wrapper(tag)
    if wrapper is None:
        return None
    wrapper_bbox = _bbox_from_tag_attrs(wrapper, canvas)
    if wrapper_bbox is None:
        return None
    if abs(wrapper_bbox["w"] - max_bbox["w"]) > 2 or abs(wrapper_bbox["h"] - max_bbox["h"]) > 2:
        return None
    adapted = {
        "x": wrapper_bbox["x"] + max(0, fitted["x"] - max_bbox["x"]),
        "y": wrapper_bbox["y"] + max(0, fitted["y"] - max_bbox["y"]),
        "w": fitted["w"],
        "h": fitted["h"],
    }
    _merge_position_style(wrapper, adapted)
    return {
        "block_id": str(wrapper.get("data-block-id") or ""),
        "from_bbox": wrapper_bbox,
        "to_bbox": adapted,
    }


def _image_only_source_wrapper(tag: Tag) -> Tag | None:
    parent = tag.parent
    while isinstance(parent, Tag):
        name = str(parent.name or "").lower()
        if name in {"main", "body", "html"}:
            return None
        if _is_source_wrapper(parent) and _wrapper_has_only_this_image(parent, tag):
            return parent
        parent = parent.parent
    return None


def _is_source_wrapper(tag: Tag) -> bool:
    classes = " ".join(str(item) for item in (tag.get("class") or []))
    hay = " ".join(
        str(value or "")
        for value in (
            tag.get("data-lane"),
            tag.get("data-role"),
            tag.get("data-block-kind"),
            tag.get("role"),
            classes,
        )
    ).lower()
    return (
        "source" in hay
        or "visual" in hay
        or "figure" in hay
        or str(tag.name or "").lower() == "figure"
    )


def _wrapper_has_only_this_image(wrapper: Tag, image_tag: Tag) -> bool:
    for child in wrapper.find_all(True):
        if child is image_tag:
            continue
        if child.find(lambda descendant: descendant is image_tag):
            continue
        name = str(child.name or "").lower()
        if name == "img":
            return False
        if child.get_text(" ", strip=True):
            return False
        if str(child.get("data-block-id") or "").strip():
            return False
    return True


def _style_bbox_uses_percent(tag: Tag) -> bool:
    style = str(tag.get("style") or "")
    return any(
        re.search(rf"(?:^|;)\s*{prop}\s*:\s*-?\d+(?:\.\d+)?\s*%", style, flags=re.IGNORECASE)
        for prop in ("left", "top", "width", "height")
    )


def _fit_bbox_to_aspect(bbox: dict[str, int], aspect: float) -> dict[str, int]:
    max_w = max(1, int(bbox["w"]))
    max_h = max(1, int(bbox["h"]))
    fit_w = float(max_w)
    fit_h = fit_w / aspect
    if fit_h > max_h:
        fit_h = float(max_h)
        fit_w = fit_h * aspect
    w = max(1, min(max_w, int(round(fit_w))))
    h = max(1, min(max_h, int(round(fit_h))))
    return {
        "x": int(bbox["x"]) + max(0, int(round((max_w - w) / 2.0))),
        "y": int(bbox["y"]),
        "w": w,
        "h": h,
    }


def _source_aspect_for_id(source_id: str, ctx: ToolContext, *, src: str = "") -> float:
    key = str(source_id or "").strip()
    if not key or not isinstance(ctx.state, dict):
        return 0.0
    rendered = ctx.state.get("rendered_layers")
    if isinstance(rendered, dict) and isinstance(rendered.get(key), dict):
        rec = rendered[key]
        for value in (rec.get("image_size"), rec.get("aspect_ratio")):
            aspect = _aspect_from_source_value(value)
            if aspect > 0:
                return aspect
    provenance = ctx.state.get("paper_visual_provenance")
    if isinstance(provenance, dict):
        for asset in provenance.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            asset_id = str(
                asset.get("layer_id")
                or asset.get("asset_id")
                or asset.get("source_id")
                or ""
            ).strip()
            if asset_id != key:
                continue
            for value in (
                asset.get("image_size"),
                asset.get("aspect_ratio"),
                {
                    "w": asset.get("output_width_px"),
                    "h": asset.get("output_height_px"),
                },
            ):
                aspect = _aspect_from_source_value(value)
                if aspect > 0:
                    return aspect
    return _aspect_from_image_path(src, ctx)


def _aspect_from_source_value(value: Any) -> float:
    if isinstance(value, dict):
        try:
            w = float(value.get("w") or value.get("width") or value.get("naturalWidth") or 0)
            h = float(value.get("h") or value.get("height") or value.get("naturalHeight") or 0)
        except (TypeError, ValueError):
            return 0.0
        return w / h if w > 0 and h > 0 else 0.0
    raw = str(value or "").strip()
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*$", raw)
    if match:
        try:
            w = float(match.group(1))
            h = float(match.group(2))
        except ValueError:
            return 0.0
        return w / h if w > 0 and h > 0 else 0.0
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*$", raw)
    if match:
        try:
            w = float(match.group(1))
            h = float(match.group(2))
        except ValueError:
            return 0.0
        return w / h if w > 0 and h > 0 else 0.0
    try:
        aspect = float(raw)
    except ValueError:
        return 0.0
    return aspect if aspect > 0 else 0.0


def _aspect_from_image_path(src: str, ctx: ToolContext) -> float:
    path = _local_image_path_for_src(src, ctx)
    if not path:
        return 0.0
    try:
        with Image.open(path) as image:
            w, h = image.size
    except Exception:
        return 0.0
    return float(w) / float(h) if w > 0 and h > 0 else 0.0


def _image_src_needs_source_rewrite(src: str, ctx: ToolContext) -> bool:
    value = str(src or "").strip()
    if not value:
        return True
    if re.fullmatch(r"\{\{\s*(?:layer|asset)\s*:\s*[^{}]+?\s*\}\}", value):
        return True
    if re.fullmatch(r"\{\{\s*ingest_(?:fig|table)_[A-Za-z0-9_-]+\s*\}\}", value):
        return True
    if value.startswith(("http://", "https://", "//", "data:", "javascript:", "file:")):
        return False
    if value.startswith("layers/"):
        candidate = Path(value)
        if candidate.exists():
            return False
        if (ctx.run_dir / "html_first" / value).exists():
            return False
        return True
    return False


def _unsafe_external_asset_reference_error(soup: BeautifulSoup, css: str, ctx: ToolContext) -> ToolResultRecord | None:
    issues: list[dict[str, Any]] = []
    for tag in soup.find_all(["img", "source", "image", "video"]):
        if not isinstance(tag, Tag):
            continue
        for attr in ("src", "href", "xlink:href", "poster"):
            value = str(tag.get(attr) or "").strip()
            if value and _asset_ref_is_unsafe_for_designer_owned_html(value, tag, ctx):
                issues.append(_unsafe_asset_issue(tag, attr, value, ctx))
        srcset = str(tag.get("srcset") or "").strip()
        if srcset:
            for value in _srcset_urls(srcset):
                if _asset_ref_is_unsafe_for_designer_owned_html(value, tag, ctx):
                    issues.append(_unsafe_asset_issue(tag, "srcset", value, ctx))
                    break
    for value in _css_url_values(css):
        if _asset_ref_is_unsafe_for_designer_owned_html(value, None, ctx, allow_known_data_uri=False):
            issues.append({
                "failure_kind": "unsafe_css_asset_url",
                "attribute": "css url()",
                "src_preview": value[:160],
                "expected": (
                "Use staged local source/layer assets instead of remote, absolute, "
                "data, file, or javascript image URLs."
            ),
            })
            break
    if not issues:
        return None
    return obs_error(
        "Designer-owned poster HTML must use staged local source/layer assets, not remote or inline image URLs.",
        category="validation",
        payload={
            "issue_id": "paper_poster_html_unsafe_external_asset_ref",
            "repair_route": "use_staged_local_source_or_layer_assets",
            "issues": issues[:12],
            "required": (
                "Use relative local paths/data-source-id/data-layer-id from staged source assets; "
                "do not add remote https://, absolute, data:, file:, javascript:, or blob: image URLs."
            ),
            "local_asset_contract": {
                "allowed_ref_kinds": [
                    "registered rendered_layers src_path",
                    "registered paper_visual_provenance local path",
                    "existing staged run_dir/html_first asset",
                ],
                "required_binding": "Prefer data-source-id/data-layer-id matching the staged source asset.",
                "forbidden_ref_prefixes": ["http://", "https://", "//", "data:", "file:", "javascript:", "blob:"],
            },
        },
    )


def _asset_ref_is_unsafe_for_designer_owned_html(
    value: str,
    tag: Tag | None,
    ctx: ToolContext,
    *,
    allow_known_data_uri: bool = True,
) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    lower = raw.lower()
    if lower.startswith(("http://", "https://", "//", "file:", "javascript:", "blob:")):
        return True
    if lower.startswith("data:"):
        return True
    if Path(raw).is_absolute():
        return True
    source_id = ""
    if tag is not None:
        source_id = _source_id_for_tag(tag, ctx) or str(tag.get("data-source-id") or tag.get("data-layer-id") or "").strip()
    if _asset_ref_matches_registered_or_staged_asset(raw, source_id, ctx):
        return False
    if tag is not None and source_id and _source_id_exists(source_id, ctx) and not raw:
        return False
    return True


def _asset_ref_matches_registered_or_staged_asset(value: str, source_id: str, ctx: ToolContext) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    canonical_id = _canonical_source_id(raw, ctx, allow_plain=False)
    expected_ids = _unique_nonempty([source_id, canonical_id])
    for expected_id in expected_ids:
        if expected_id and _source_id_exists(expected_id, ctx):
            if canonical_id == expected_id:
                return True
            source_path = _source_path_for_id(expected_id, ctx)
            if source_path and _asset_ref_matches_local_path(raw, source_path, ctx):
                return True
    for asset_id, asset_path in _registered_asset_paths(ctx):
        if source_id and asset_id and source_id != asset_id:
            continue
        if _asset_ref_matches_local_path(raw, asset_path, ctx):
            return True
    local_path = _local_image_path_for_src(raw, ctx)
    if not local_path:
        return False
    try:
        resolved = Path(local_path).resolve()
    except OSError:
        resolved = Path(local_path)
    return _path_is_within(resolved, ctx.run_dir)


def _registered_asset_paths(ctx: ToolContext) -> list[tuple[str, str]]:
    paths: list[tuple[str, str]] = []
    rendered = ctx.state.get("rendered_layers") if isinstance(ctx.state, dict) else None
    if isinstance(rendered, dict):
        for layer_id, rec in rendered.items():
            if not isinstance(rec, dict):
                continue
            for field in ("src_path", "output_file", "local_asset_path", "path"):
                src = str(rec.get(field) or "").strip()
                if src:
                    paths.append((str(layer_id), src))
    provenance = ctx.state.get("paper_visual_provenance") if isinstance(ctx.state, dict) else None
    if isinstance(provenance, dict):
        for asset in provenance.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            asset_id = str(asset.get("layer_id") or asset.get("asset_id") or asset.get("source_id") or "").strip()
            for field in ("src_path", "output_file", "local_asset_path", "path"):
                src = str(asset.get(field) or "").strip()
                if src:
                    paths.append((asset_id, src))
    return paths


def _asset_ref_matches_local_path(value: str, asset_path: str, ctx: ToolContext) -> bool:
    raw = str(value or "").strip()
    target_raw = str(asset_path or "").strip()
    if not raw or not target_raw:
        return False
    if raw == target_raw:
        return True
    raw_candidates = _candidate_asset_paths(raw, ctx)
    target_candidates = _candidate_asset_paths(target_raw, ctx)
    for raw_candidate in raw_candidates:
        for target_candidate in target_candidates:
            try:
                if raw_candidate.resolve() == target_candidate.resolve():
                    return True
            except OSError:
                if raw_candidate == target_candidate:
                    return True
    return False


def _candidate_asset_paths(value: str, ctx: ToolContext) -> list[Path]:
    raw = str(value or "").strip()
    if not raw:
        return []
    path = Path(raw)
    if path.is_absolute():
        return [path]
    return [
        ctx.run_dir / raw,
        ctx.run_dir / "html_first" / raw,
        path,
    ]


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _unsafe_asset_issue(tag: Tag, attr: str, value: str, ctx: ToolContext) -> dict[str, Any]:
    block_id = str(tag.get("data-block-id") or "").strip()
    source_id = _source_id_for_tag(tag, ctx) or str(tag.get("data-source-id") or tag.get("data-layer-id") or "").strip()
    ref_kind = "unstaged_relative_asset"
    lower = str(value or "").strip().lower()
    if lower.startswith(("http://", "https://", "//")):
        ref_kind = "remote_asset_url"
    elif lower.startswith("data:"):
        ref_kind = "inline_data_uri"
    elif lower.startswith(("file:", "javascript:", "blob:")) or Path(str(value or "")).is_absolute():
        ref_kind = "forbidden_absolute_or_runtime_url"
    elif source_id and not _source_id_exists(source_id, ctx):
        ref_kind = "unknown_source_or_identity_id"
    return {
        "failure_kind": "unsafe_external_asset_reference",
        "ref_kind": ref_kind,
        "tag": tag.name,
        "attribute": attr,
        "block_id": block_id,
        "source_id": source_id,
        "src_preview": value[:160],
        "expected": "Use a relative staged local source/identity layer path and matching data-source-id/data-layer-id.",
    }


def _srcset_urls(srcset: str) -> list[str]:
    urls: list[str] = []
    for part in str(srcset or "").split(","):
        token = part.strip().split()
        if token:
            urls.append(token[0])
    return urls


def _css_url_values(css: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(r"url\(\s*(['\"]?)(.*?)\1\s*\)", str(css or ""), flags=re.IGNORECASE | re.DOTALL):
        values.append(str(match.group(2) or "").strip())
    return values


def _local_image_path_for_src(src: str, ctx: ToolContext) -> str:
    value = str(src or "").strip()
    if not value or value.startswith(("http://", "https://", "//", "data:", "javascript:", "file:")):
        return ""
    candidates = [Path(value)]
    if not Path(value).is_absolute():
        candidates = [
            ctx.run_dir / value,
            ctx.run_dir / "html_first" / value,
            Path(value),
        ]
    for candidate in candidates:
        if candidate.exists():
            try:
                return str(candidate.resolve())
            except OSError:
                return str(candidate)
    return ""


def _source_coverage_error(
    soup: BeautifulSoup,
    ctx: ToolContext,
    *,
    bboxes: dict[str, dict[str, Any]] | None = None,
    canvas: dict[str, Any] | None = None,
) -> ToolResultRecord | None:
    required = _required_source_ids(ctx)
    if not required:
        return None
    placed = _placed_source_ids(soup, ctx, bboxes=bboxes, canvas=canvas)
    missing = [source_id for source_id in required if source_id not in placed]
    if not missing and len(placed.intersection(required)) >= len(required):
        return None
    return obs_error(
        "propose_paper_poster_html is missing required selected paper visuals.",
        category="validation",
        payload={
            "issue_id": "paper_poster_html_source_coverage_low",
            "repair_route": "place_selected_source_visuals",
            "required_source_ids": required,
            "placed_source_ids": sorted(placed),
            "missing_source_ids": missing,
            "hint": (
                "Place each required ID as a real source visual before adding "
                "lower-priority prose, e.g. <img data-source-id=\"%s\" alt=\"...\">."
            ) % (missing[0] if missing else required[0]),
        },
    )


_MIN_SOURCE_FLOW_EXPLANATION_WORDS = 6
_MAX_SOURCE_FLOW_EXPLANATION_WORDS = 140
_MIN_SOURCE_VISUAL_PANEL_WIDTH_RATIO = 0.48
_MIN_SOURCE_VISUAL_LARGE_PANEL_WIDTH_RATIO = 0.58
_MIN_SOURCE_VISUAL_WIDE_PANEL_WIDTH_RATIO = 0.82
_MIN_SOURCE_VISUAL_OBJECT_FIT_FILL_RATIO = 0.72
_MIN_SOURCE_VISUAL_OBJECT_FIT_AREA_RATIO = 0.62
_OBVIOUS_SOURCE_VISUAL_OBJECT_FIT_AREA_FAILURE_RATIO = 0.42
_MIN_SOURCE_VISUAL_SIDE_TEXT_WORDS = 24
_MIN_SOURCE_VISUAL_LARGE_SIDE_TEXT_WORDS = 28
_MIN_SOURCE_VISUAL_SIDE_TEXT_COVERAGE_RATIO = 0.30
_MIN_SOURCE_VISUAL_READABLE_WIDTH_PX = 260
_MIN_SOURCE_VISUAL_FLOW_FILL_MIN_WIDTH_RATIO = 0.28
_VISIBLE_SOURCE_CAPTION_PREFIX_RE = re.compile(
    r"^\s*(?:fig(?:ure)?\.?|table)\s*\d+[a-z]?\s*(?:[.:;,)\-]|\b)",
    re.I,
)


def _source_visible_caption_error(soup: BeautifulSoup, ctx: ToolContext) -> ToolResultRecord | None:
    if not _dogfood_dense_mode(ctx):
        return None
    issues: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw_tag in soup.find_all(["figure", "img", "table"]):
        if not isinstance(raw_tag, Tag):
            continue
        tag = _source_wrap_tag(raw_tag)
        if id(tag) in seen:
            continue
        source_id = _source_id_for_tag(tag, ctx)
        if not source_id:
            continue
        caption_text = _visible_source_caption_text(tag)
        if not caption_text:
            continue
        seen.add(id(tag))
        panel = _nearest_source_wrap_panel(tag)
        issues.append({
            "source_id": source_id,
            "block_id": str(tag.get("data-block-id") or raw_tag.get("data-block-id") or "").strip(),
            "panel_id": str(panel.get("data-block-id") or panel.get("data-slot-id") or "").strip() if panel else "",
            "caption_text": caption_text[:240],
        })
    if not issues:
        return None
    log(
        "paper_poster_html.source_visible_caption_block",
        issue_count=len(issues),
        first_issues=issues[:4],
    )
    return obs_error(
        "propose_paper_poster_html found visible captions under source figures/tables.",
        category="validation",
        payload={
            "issue_id": "paper_poster_html_source_visible_caption",
            "repair_route": "move_source_caption_into_flow_readout",
            "issues": issues[:12],
            "hint": (
                "Do not use visible <figcaption> or caption-class rows for paper source figures/tables. "
                "Keep each source asset and its short explanation in the same .figure-flow-unit/"
                ".source-flow-unit, but write the explanation as normal local prose/readout next to or "
                "around the asset."
            ),
        },
    )


def _source_wrap_error(soup: BeautifulSoup, css: str, ctx: ToolContext) -> ToolResultRecord | None:
    if not _dogfood_dense_mode(ctx):
        return None
    required = set(_source_flow_required_ids(ctx))
    required.update(source_id for source_id in _placed_source_ids(soup, ctx) if _is_source_visual_id(source_id))
    if not required:
        return None
    candidates = _source_wrap_candidates(soup, css, ctx, required)
    if not candidates:
        return None
    violations = [
        item for item in candidates
        if (
            not item.get("wrap_evidence")
            or item.get("separate_layout_evidence")
            or item.get("flow_unit_violations")
            or item.get("visible_figcaption")
            or int(item.get("local_words") or 0) < _MIN_SOURCE_FLOW_EXPLANATION_WORDS
            or int(item.get("local_words") or 0) > _MAX_SOURCE_FLOW_EXPLANATION_WORDS
        )
    ]
    if not violations:
        return None
    wrapped = [item for item in candidates if item.get("wrap_evidence")]
    issues = []
    for item in violations[:12]:
        separate_layout = list(item.get("separate_layout_evidence") or [])
        flow_unit_violations = list(item.get("flow_unit_violations") or [])
        flow_unit_violation_details = list(item.get("flow_unit_violation_details") or [])
        local_words = int(item.get("local_words") or item.get("panel_words") or 0)
        failure_kind = _source_wrap_failure_kind(
            item,
            separate_layout=separate_layout,
            flow_unit_violations=flow_unit_violations,
            local_words=local_words,
        )
        issue_severity, soft_finalizable = _source_wrap_issue_severity(failure_kind, local_words)
        if flow_unit_violations:
            reason = str(flow_unit_violations[0])
        elif item.get("visible_figcaption"):
            reason = "source figure/table uses a visible figcaption; move that text into local flow prose/readout instead"
        elif separate_layout and not item.get("wrap_evidence"):
            reason = "source figure/table is split into a media/text layout and lacks float/shape-outside wrap"
        elif separate_layout:
            reason = "source figure/table is still inside a separate media/text layout wrapper"
        elif local_words <= 0:
            reason = "source figure/table has no local explanatory prose in its own flow unit"
        elif local_words < _MIN_SOURCE_FLOW_EXPLANATION_WORDS:
            reason = "source figure/table has too little local explanatory prose in its own flow unit"
        elif local_words > _MAX_SOURCE_FLOW_EXPLANATION_WORDS:
            reason = (
                "source figure/table local readout is excessively long; keep each asset explanation "
                f"under roughly {_MAX_SOURCE_FLOW_EXPLANATION_WORDS} words and use the visual/table as the subject"
            )
        else:
            reason = "source figure/table is present, but not authored as float/shape-outside text wrap"
        issues.append({
            "source_id": item.get("source_id"),
            "block_id": item.get("block_id"),
            "panel_id": item.get("panel_id"),
            "panel_role": item.get("panel_role"),
            "panel_words": item.get("panel_words"),
            "local_words": item.get("local_words"),
            "flow_unit": item.get("flow_unit"),
            "failure_kind": failure_kind,
            "actual_wrapper_label": item.get("actual_wrapper_label"),
            "actual_parent_label": item.get("actual_parent_label"),
            "expected_parent": "panel-root direct child",
            "required_dom_shape": (
                "section.figure-flow-unit[data-source-id] as a direct child of the panel, "
                "containing figure.flow-asset[data-source-id] plus direct h/p/list readout text"
            ),
            "reason": reason,
            "separate_layout_evidence": separate_layout[:4],
            "flow_unit_violations": flow_unit_violations[:4],
            "flow_unit_violation_details": flow_unit_violation_details[:4],
            "visible_figcaption": item.get("visible_figcaption"),
            "severity": issue_severity,
            "soft_finalizable": soft_finalizable,
        })
    ctx.state["paper_poster_html_source_wrap"] = {
        "candidate_count": len(candidates),
        "wrapped_count": len(wrapped),
        "required_wrapped_count": len(candidates),
        "issues": issues,
    }
    log(
        "paper_poster_html.source_wrap_missing",
        candidate_count=len(candidates),
        wrapped_count=len(wrapped),
        required_wrapped_count=len(candidates),
        first_issues=issues[:3],
    )
    return obs_error(
        "propose_paper_poster_html found source figures/tables that are not authored as per-asset DOM flow units with their explanatory text.",
        category="validation",
        payload={
            "issue_id": "paper_poster_html_source_wrap_missing",
            "repair_route": "author_browser_flow_figure_text_wrap",
            "issues": issues,
            "candidate_count": len(candidates),
            "wrapped_count": len(wrapped),
            "required_wrapped_count": len(candidates),
            "hint": (
                "For every source figure/table, create its own local flow unit in the surrounding section: "
                "<section class=\"figure-flow-unit\" data-source-id=\"...\">"
                "<figure class=\"flow-asset float-right\" data-source-id=\"...\">"
                "<img ...></figure><p class=\"readout\">local explanation for this one asset</p>"
                "</section>. Use CSS such as `.figure-flow-unit{display:flow-root}` and "
                "`.flow-asset.asset-large.float-right{float:right;width:64%;max-height:360px;"
                "margin:0 0 .6em 1em;shape-outside:inset(0 round 6px);}` or "
                "`.flow-asset.asset-medium{width:54%;max-height:300px;}` for secondary visuals. "
                "Let each local readout be as long as the visual needs, usually around 20-90 words, "
                f"with only very long readouts over {_MAX_SOURCE_FLOW_EXPLANATION_WORDS} words blocked. "
                "If a panel needs multiple source assets, use "
                "multiple .figure-flow-unit/.source-flow-unit blocks, one per asset. Do not render "
                "a visible figcaption under every image; move that text into the local readout. "
                "Do not use media-grid, media-top, side-stack, support-strip, two-up, analysis-grid, "
                "or one shared panel-wide text flow for multiple source assets."
            ),
        },
    )


def _source_flow_required_ids(ctx: ToolContext) -> set[str]:
    ids = set(_required_source_ids(ctx))
    rendered = ctx.state.get("rendered_layers") if isinstance(ctx.state, dict) else None
    if isinstance(rendered, dict):
        for layer_id, rec in rendered.items():
            if not isinstance(rec, dict):
                continue
            if str(rec.get("source") or "") == "ingested_pdf" or _is_source_visual_id(str(layer_id)):
                ids.add(str(layer_id))
            source_id = str(rec.get("source_id") or "").strip()
            if source_id:
                ids.add(source_id)
    return {item for item in ids if item}


def _is_source_visual_id(layer_id: str) -> bool:
    return str(layer_id or "").startswith(("ingest_fig_", "ingest_table_", "ingest_img_"))


def _source_panel_flow_shape_error(soup: BeautifulSoup, css: str, ctx: ToolContext) -> ToolResultRecord | None:
    if not _dogfood_dense_mode(ctx):
        return None
    if _editorial_flow_mode(ctx):
        return None
    required = _source_flow_required_ids(ctx)
    if not required:
        return None
    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for raw_tag in soup.find_all(["figure", "img", "table"]):
        if not isinstance(raw_tag, Tag):
            continue
        tag = _source_wrap_tag(raw_tag)
        source_id = (
            _source_id_for_tag(tag, ctx)
            or _source_id_for_tag(raw_tag, ctx)
            or _source_id_for_tag_or_descendant(tag, ctx)
        )
        if source_id not in required:
            continue
        if source_id.startswith("ingest_table_") and not _is_bound_source_table_crop_tag(tag, ctx):
            continue
        panel = _nearest_source_wrap_panel(tag)
        if panel is None:
            continue
        if _is_identity_header_source_asset(tag, panel, source_id, ctx):
            continue
        key = (source_id, id(panel))
        if key in seen:
            continue
        seen.add(key)
        reasons = _panel_flow_shape_violations(panel, tag, css)
        if not reasons:
            continue
        issues.append({
            "source_id": source_id,
            "panel_id": str(panel.get("data-block-id") or panel.get("data-slot-id") or "").strip(),
            "panel_role": str(panel.get("data-panel-role") or panel.get("data-role") or "").strip(),
            "source_tag": str(tag.name or "").lower(),
            "source_block_id": str(tag.get("data-block-id") or raw_tag.get("data-block-id") or "").strip(),
            "reasons": reasons,
        })
    if not issues:
        return None
    ctx.state["paper_poster_html_panel_flow_shape"] = {
        "issue_count": len(issues),
        "issues": issues[:12],
    }
    log(
        "paper_poster_html.panel_flow_shape_failed",
        issue_count=len(issues),
        first_issues=issues[:4],
    )
    return obs_error(
        "propose_paper_poster_html found source panels that are not authored as per-asset flow DOM units.",
        category="validation",
        payload={
            "issue_id": "paper_poster_html_panel_flow_shape_failed",
            "repair_route": "rewrite_source_panels_as_per_asset_flow_units",
            "issues": issues[:12],
            "hint": (
                "Match out/wrap_demo/index.html at the source-asset level: the panel root should contain "
                "one direct-child <section class=\"figure-flow-unit\"> per source asset. Inside each unit, "
                "put <figure class=\"flow-asset wrap-right\" data-source-id=\"...\">...</figure> and the "
                "local <p> readout/takeaway as direct siblings so text wraps around that asset. "
                "Use `.figure-flow-unit{display:flow-root}` to separate figures from each other. "
                "Do not wrap source-panel contents in .slot-flow/.flow-body/.panel-body, support strips, "
                "figure strips, media grids, or one shared multi-image text flow."
            ),
        },
    )


def _editorial_flow_shape_error(soup: BeautifulSoup, css: str, ctx: ToolContext) -> ToolResultRecord | None:
    if not _editorial_flow_mode(ctx):
        return None
    columns = [
        tag for tag in soup.find_all(True)
        if isinstance(tag, Tag) and _is_editorial_column_tag(tag)
    ]
    sections = [
        tag for tag in soup.find_all(True)
        if isinstance(tag, Tag) and _is_editorial_section_tag(tag)
    ]
    flow_panels = [
        tag for tag in soup.find_all(True)
        if isinstance(tag, Tag) and "flow-panel" in _class_tokens(tag)
    ]
    poster_grid = [
        tag for tag in soup.find_all(True)
        if isinstance(tag, Tag) and ("poster-grid" in _class_tokens(tag) or str(tag.get("data-layout-region") or "") == "main_panels")
    ]
    reference = _active_reference_style_contract(ctx)
    reference_tokens = reference.get("style_tokens") if isinstance(reference.get("style_tokens"), dict) else {}
    reference_columns = reference_tokens.get("column_structure") if isinstance(reference_tokens.get("column_structure"), dict) else {}
    reference_counts = reference_columns.get("major_sections_per_column")
    if isinstance(reference_counts, list) and 2 <= len(reference_counts) <= 6:
        expected_column_count = len(reference_counts)
        minimum_section_count = sum(max(1, min(3, _safe_int(item, 1))) for item in reference_counts)
        max_sections_by_column = [max(1, min(3, _safe_int(item, 1))) for item in reference_counts]
    else:
        expected_column_count = 3
        minimum_section_count = 3
        max_sections_by_column = [3, 3, 3]
    issues: list[dict[str, Any]] = []
    if len(columns) != expected_column_count:
        issues.append({
            "id": "editorial_column_count_wrong",
            "column_count": len(columns),
            "repair": (
                f"Use exactly {expected_column_count} reference-owned .poster-column/[data-column-id] body regions below the identity header."
            ),
        })
    if len(sections) < minimum_section_count:
        issues.append({
            "id": "editorial_section_count_low",
            "section_count": len(sections),
            "repair": (
                f"Restore the reference-owned direct-child section counts {reference_counts or [1, 1, 1]}."
            ),
        })
    for index, column in enumerate(columns):
        column_id = str(column.get("data-column-id") or column.get("data-block-id") or "").strip()
        column_sections = _direct_editorial_sections(column)
        if not column_sections:
            issues.append({
                "id": "editorial_column_empty",
                "column_id": column_id,
                "repair": "Each editorial column needs one to three substantive .poster-section blocks.",
            })
        max_sections = max_sections_by_column[index] if index < len(max_sections_by_column) else 3
        if len(column_sections) > max_sections:
            issues.append({
                "id": "editorial_column_section_count_high",
                "column_id": column_id,
                "section_count": len(column_sections),
                "repair": f"Use at most {max_sections} direct-child sections in this reference-owned body region.",
            })
    css_pack_issues = _editorial_flow_css_pack_issues(css)
    issues.extend(css_pack_issues)
    issues.extend(_editorial_flow_dom_pack_issues(soup))
    issues.extend(_editorial_contribution_panel_issues(soup))
    if poster_grid or len(flow_panels) >= 5:
        issues.append({
            "id": "six_panel_grid_regression",
            "flow_panel_count": len(flow_panels),
            "poster_grid_count": len(poster_grid),
            "repair": "Do not submit the old six-card poster-grid/flow-panel structure for conference_editorial_flow.",
        })
    if not issues:
        return None
    return obs_error(
        "propose_paper_poster_html found an editorial-flow poster shaped like the old panel grid.",
        category="validation",
        payload={
            "issue_id": "paper_poster_html_editorial_flow_shape_failed",
            "repair_route": "restore_reference_owned_editorial_regions" if reference else "rewrite_as_three_column_editorial_flow",
            "issues": issues,
            "hint": (
                f"For this poster use <div class=\"poster-columns\"> with exactly {expected_column_count} "
                "<section class=\"poster-column\"> reference-owned body regions. Each region contains one to "
                "three <section class=\"poster-section\"> blocks; one or two sections is normal, "
                "three is the upper bound, and four is invalid. Allocate those real poster-section "
                "rows across the full column height with CSS grid/flex, then fill each section with "
                "real source assets/tables and local prose. Do not use a separate .bottom-fill spacer "
                "or overflow clipping to fake column fill. "
                "A Core contributions section cannot be a large pure-text mini-card wall; merge it "
                "into Motivation or add a real source/native evidence unit plus local readout. "
                "Do not use .poster-grid, six .flow-panel cards, child lanes, or panel_content_plan slots."
            ),
        },
    )


def _direct_editorial_sections(column: Tag) -> list[Tag]:
    sections: list[Tag] = []
    for child in column.find_all(True, recursive=False):
        if isinstance(child, Tag) and _is_editorial_section_tag(child):
            sections.append(child)
    return sections


def _section_heading_text(tag: Tag) -> str:
    heading = tag.find(["h1", "h2", "h3", "h4"])
    if not isinstance(heading, Tag):
        return ""
    return heading.get_text(" ", strip=True)[:120]


def _editorial_contribution_panel_issues(soup: BeautifulSoup) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for section in [
        tag for tag in soup.find_all(True)
        if isinstance(tag, Tag) and _is_editorial_section_tag(tag)
    ]:
        identity = _editorial_section_identity(section)
        if not re.search(r"\b(?:core\s+)?contributions?\b|\bkey\s+contributions?\b", identity, flags=re.I):
            continue
        source_count = _panel_source_binding_count(section)
        native_count = _editorial_section_native_unit_count(section)
        word_count = _visible_word_count(section.get_text(" ", strip=True))
        mini_card_count = _editorial_section_mini_card_count(section)
        if source_count == 0 and native_count == 0 and (word_count >= 45 or mini_card_count >= 4):
            issues.append({
                "id": "editorial_contribution_text_only_panel",
                "section_id": str(section.get("data-block-id") or section.get("data-panel-role") or "").strip(),
                "word_count": word_count,
                "mini_card_count": mini_card_count,
                "source_binding_count": source_count,
                "native_unit_count": native_count,
                "repair": (
                    "Do not spend a large Core contributions panel on pure prose or mini-note boxes. "
                    "Either merge the contribution copy into the Motivation section, or turn this "
                    "section into a source/native evidence unit: architecture/pipeline figure, "
                    "compact native contribution table, metric/process row, and local readout."
                ),
            })
    return issues


def _editorial_section_identity(section: Tag) -> str:
    title = ""
    heading = section.find(["h1", "h2", "h3", "h4"])
    if isinstance(heading, Tag):
        title = heading.get_text(" ", strip=True)
    return " ".join(
        str(value or "")
        for value in (
            section.get("data-block-id"),
            section.get("data-panel-role"),
            section.get("data-role"),
            section.get("class"),
            title,
        )
    )


def _editorial_section_native_unit_count(section: Tag) -> int:
    count = 0
    for tag in section.find_all(True):
        if not isinstance(tag, Tag):
            continue
        name = str(tag.name or "").lower()
        role_blob = " ".join(
            str(value or "")
            for value in (
                tag.get("data-role"),
                tag.get("role"),
                tag.get("class"),
                tag.get("data-block-kind"),
                tag.get("data-block-id"),
            )
        ).lower()
        if name == "table" or any(
            token in role_blob
            for token in (
                "native-table",
                "metric",
                "result-band",
                "formula",
                "process-step",
                "benchmark",
                "score",
                "stat",
                "stage-row",
            )
        ):
            count += 1
    return count


def _editorial_section_mini_card_count(section: Tag) -> int:
    count = 0
    for tag in section.find_all(True):
        if not isinstance(tag, Tag):
            continue
        role_blob = " ".join(
            str(value or "")
            for value in (
                tag.get("data-role"),
                tag.get("role"),
                tag.get("class"),
                tag.get("data-block-id"),
            )
        ).lower()
        if any(token in role_blob for token in ("mini-note", "mini-card", "card", "chip")):
            count += 1
    return count


def _editorial_flow_css_pack_issues(css: str) -> list[dict[str, Any]]:
    compact = re.sub(r"\s+", " ", str(css or "")).strip()
    issues: list[dict[str, Any]] = []
    for class_name in ("bottom-fill",):
        for selector, body in _css_rules_with_class(compact, class_name):
            if (
                re.search(r"(?:^|;)\s*height\s*:\s*100%", body, flags=re.IGNORECASE)
                or re.search(r"(?:^|;)\s*flex\s*:\s*1(?:\s|;|$)", body, flags=re.IGNORECASE)
            ):
                issues.append({
                    "id": "editorial_section_stretch_fill",
                    "selector": f".{class_name}",
                    "matched_selector": selector,
                    "repair": (
                        "Do not add a separate bottom-fill spacer. Allocate real .poster-section "
                        "rows across the column height; if a section is tall, its bottom half must "
                        "contain real source assets, tables, or prose."
                    ),
                })
                return issues
    for class_name in ("poster-column", "poster-section"):
        for selector, body in _css_rules_with_class(compact, class_name):
            if re.search(r"(?:^|;)\s*overflow(?:-[xy])?\s*:\s*(?:hidden|clip)\b", body, flags=re.IGNORECASE):
                issues.append({
                    "id": "editorial_content_cropping_overflow_hidden",
                    "selector": f".{class_name}",
                    "matched_selector": selector,
                    "repair": (
                        "Do not crop poster columns or sections with overflow:hidden/clip. "
                        "Make content fit by shortening local text, tightening gaps, and using "
                        "small bounded source-asset height adjustments."
                    ),
                })
                return issues
    shallow_asset = _editorial_shallow_asset_css_issue(compact)
    if shallow_asset:
        issues.append(shallow_asset)
    return issues


def _editorial_flow_dom_pack_issues(soup: BeautifulSoup) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        classes = _class_tokens(tag)
        style = str(tag.get("style") or "")
        if ("poster-column" in classes or "poster-section" in classes) and re.search(
            r"(?:^|;)\s*overflow(?:-[xy])?\s*:\s*(?:hidden|clip)\b",
            style,
            flags=re.IGNORECASE,
        ):
            selector = ".poster-column" if "poster-column" in classes else ".poster-section"
            issues.append({
                "id": "editorial_content_cropping_overflow_hidden",
                "selector": selector,
                "matched_selector": "inline-style",
                "repair": (
                    "Do not crop poster columns or sections with overflow:hidden/clip. "
                    "Make content fit by shortening local text, tightening gaps, and using "
                    "small bounded source-asset height adjustments."
                ),
            })
            return issues
        if "bottom-fill" in classes and (
            re.search(r"(?:^|;)\s*height\s*:\s*100%", style, flags=re.IGNORECASE)
            or re.search(r"(?:^|;)\s*flex\s*:\s*1(?:\s|;|$)", style, flags=re.IGNORECASE)
        ):
            issues.append({
                "id": "editorial_section_stretch_fill",
                "selector": ".bottom-fill",
                "matched_selector": "inline-style",
                "repair": (
                    "Do not add a separate bottom-fill spacer. Allocate real .poster-section rows "
                    "across the column height, then fill each section with real content."
                ),
            })
            return issues
    return issues


def _editorial_shallow_asset_css_issue(css: str) -> dict[str, Any] | None:
    checks = (
        ("asset-wide", 260),
        ("asset-large", 240),
        ("asset-medium", 200),
    )
    for class_name, floor in checks:
        for selector, body in _css_rules_with_class(css, class_name):
            max_height = re.search(r"max-height\s*:\s*(\d+(?:\.\d+)?)px", body, flags=re.IGNORECASE)
            if not max_height:
                continue
            value = float(max_height.group(1))
            if value < floor:
                return {
                    "id": "editorial_source_asset_shallow_strip",
                    "selector": f".{class_name}",
                    "matched_selector": selector,
                    "max_height_px": value,
                    "min_recommended_px": floor,
                    "repair": (
                        "Do not fix overflow by shrinking source figures/tables into shallow strips. "
                        "Restore readable visual height, then shorten low-value prose and tighten local spacing."
                    ),
                }
    return None


def _css_rules_with_class(css: str, class_name: str) -> list[tuple[str, str]]:
    if not css or not class_name:
        return []
    class_pattern = re.compile(rf"\.{re.escape(class_name)}(?![\w-])", flags=re.IGNORECASE)
    rules: list[tuple[str, str]] = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css, flags=re.S):
        selector_blob = match.group(1)
        body = match.group(2)
        selectors = [part.strip() for part in selector_blob.split(",") if part.strip()]
        for selector in selectors:
            if class_pattern.search(selector):
                rules.append((selector, body))
                break
    return rules


def _is_editorial_column_tag(tag: Tag) -> bool:
    classes = _class_tokens(tag)
    return bool("poster-column" in classes or str(tag.get("data-column-id") or "").strip())


def _is_editorial_section_tag(tag: Tag) -> bool:
    classes = _class_tokens(tag)
    return bool("poster-section" in classes or str(tag.get("data-section-role") or "").strip())


_INNER_PANEL_FLOW_WRAPPER_CLASS_TOKENS = (
    "slot-flow",
    "flow-body",
    "panel-body",
    "panel_body",
    "content-body",
    "content_body",
    "content-flow",
    "content_flow",
    "body-lane",
    "body_lane",
)


def _panel_flow_shape_violations(panel: Tag, source_tag: Tag, css: str) -> list[str]:
    return _source_flow_unit_violations(panel, source_tag, css)


_SOURCE_FLOW_UNIT_CLASS_TOKENS = {
    "figure-flow-unit",
    "source-flow-unit",
    "asset-flow-unit",
    "visual-flow-unit",
    "evidence-flow-unit",
    "table-flow-unit",
}


def _source_wrap_failure_kind(
    item: dict[str, Any],
    *,
    separate_layout: list[Any],
    flow_unit_violations: list[Any],
    local_words: int,
) -> str:
    if item.get("visible_figcaption"):
        return "visible_figcaption"
    if flow_unit_violations:
        if separate_layout:
            return "nested_unsupported_wrapper"
        if any("unsupported inner panel wrapper" in str(value) for value in flow_unit_violations):
            return "nested_unsupported_wrapper"
        if any("not a direct child" in str(value) for value in flow_unit_violations):
            return "flow_unit_not_direct_child"
        return "invalid_source_flow_unit"
    if separate_layout:
        return "separate_media_text_layout"
    if not item.get("wrap_evidence"):
        return "missing_float_wrap_evidence"
    if local_words <= 0:
        return "missing_local_readout"
    if local_words < _MIN_SOURCE_FLOW_EXPLANATION_WORDS:
        return "local_readout_too_short"
    if local_words > _MAX_SOURCE_FLOW_EXPLANATION_WORDS:
        return "local_readout_too_long"
    return "invalid_source_flow_unit"


def _source_wrap_issue_severity(failure_kind: str, local_words: int) -> tuple[str, bool]:
    if (
        failure_kind == "local_readout_too_long"
        and local_words <= int(math.ceil(_MAX_SOURCE_FLOW_EXPLANATION_WORDS * 1.10))
    ):
        return "near_miss", True
    return "hard", False


def _source_flow_unit_violations(panel: Tag, source_tag: Tag, css: str) -> list[str]:
    return [
        str(detail.get("why_blocking") or "").strip()
        for detail in _source_flow_unit_violation_details(panel, source_tag, css)
        if str(detail.get("why_blocking") or "").strip()
    ]


def _source_flow_unit_violation_details(panel: Tag, source_tag: Tag, css: str) -> list[dict[str, Any]]:
    editorial = _source_in_editorial_flow(source_tag)
    details: list[dict[str, Any]] = []
    unit = _source_flow_unit_for_tag(source_tag, panel)
    if unit is None:
        if editorial and _nearest_editorial_source_context(source_tag, panel) is not None:
            return []
        wrapper = _nearest_inner_panel_wrapper(source_tag, panel)
        if wrapper is not None:
            details.append(_source_flow_violation_detail(
                "nested_unsupported_wrapper",
                wrapper,
                why=(
                    "source asset is inside an unsupported inner panel wrapper "
                    f"{_tag_label(wrapper)} instead of its own .figure-flow-unit/.source-flow-unit"
                ),
                repair_hint=(
                    "Move this source visual into its own panel direct-child .figure-flow-unit/"
                    ".source-flow-unit with the readout as a direct sibling."
                ),
            ))
        else:
            details.append(_source_flow_violation_detail(
                "missing_source_flow_unit",
                source_tag,
                why="source asset is not inside its own direct-child .figure-flow-unit/.source-flow-unit",
                repair_hint=(
                    "Create one panel direct-child .figure-flow-unit/.source-flow-unit for this source "
                    "asset and put its local readout inside that same unit."
                ),
            ))
        return _dedupe_source_flow_violation_details(details)

    if unit is panel:
        if _panel_source_binding_count(panel) > 1:
            details.append(_source_flow_violation_detail(
                "multi_source_shared_panel_flow",
                panel,
                why=(
                    "multi-source panel places several assets in one shared panel flow; split each "
                    "source into its own .figure-flow-unit/.source-flow-unit"
                ),
                repair_hint="Create one direct-child source flow unit per source asset.",
            ))
        if _panel_has_single_inner_flow_wrapper(panel):
            wrapper = next(
                (
                    child for child in panel.find_all(True, recursive=False)
                    if isinstance(child, Tag) and _is_inner_panel_flow_wrapper(child)
                ),
                panel,
            )
            details.append(_source_flow_violation_detail(
                "single_inner_flow_wrapper",
                wrapper,
                why=(
                    "panel content is nested in a single .slot-flow/.flow-body wrapper; split source "
                    "assets into direct-child flow units"
                ),
                repair_hint="Lift source flow units to direct children of the panel root.",
            ))
        details.extend(_panel_direct_children_layout_split_details(
            panel,
            css,
            why="panel direct children include grid/flex source/text split wrappers",
        ))
    else:
        if unit.parent is not panel:
            details.append(_source_flow_violation_detail(
                "flow_unit_not_direct_child",
                unit,
                why="source flow unit is not a direct child of the panel root",
                repair_hint="Move the .figure-flow-unit/.source-flow-unit directly under the poster section panel.",
            ))
        unit_display = _tag_display_layout_evidence(unit, css)
        if unit_display:
            first = unit_display[0]
            details.append(_source_flow_violation_detail(
                "flow_unit_self_display_layout",
                unit,
                evidence=first,
                why="source flow unit uses grid/flex source/text split wrappers",
                repair_hint=(
                    "Set the source flow unit itself to display:flow-root or display:block. Keep any "
                    "grid/flex helper layout off the source-flow-unit selector."
                ),
            ))
        else:
            details.extend(_panel_direct_children_layout_split_details(
                unit,
                css,
                why="source flow unit uses grid/flex source/text split wrappers",
            ))

    if not _flow_unit_has_direct_flow_text(unit):
        details.append(_source_flow_violation_detail(
            "missing_direct_flow_readout",
            unit,
            why="source flow unit has no direct h/p/list text siblings for the floated source asset",
            repair_hint="Add direct h/p/ul/ol/table/div source-backed readout siblings inside this flow unit.",
        ))
    return _dedupe_source_flow_violation_details(details)


def _source_in_editorial_flow(tag: Tag) -> bool:
    node: Tag | None = tag
    while isinstance(node, Tag):
        classes = _class_tokens(node)
        if "editorial-poster" in classes or "poster-column" in classes or "poster-section" in classes:
            return True
        if str(node.get("data-layout-mode") or "") == "editorial-flow":
            return True
        if str(node.name or "").lower() in {"body", "html"}:
            break
        node = node.parent
    return False


def _nearest_editorial_source_context(tag: Tag, panel: Tag) -> Tag | None:
    node: Tag | None = tag.parent
    while isinstance(node, Tag) and node is not panel:
        if _is_source_flow_unit(node) or _is_editorial_section_tag(node):
            if _visible_panel_word_count(node, exclude=_source_wrap_tag(tag)) >= _MIN_SOURCE_FLOW_EXPLANATION_WORDS:
                return node
        node = node.parent
    if (
        _is_editorial_section_tag(panel)
        and _panel_source_binding_count(panel) == 1
        and _visible_panel_word_count(panel, exclude=_source_wrap_tag(tag)) >= _MIN_SOURCE_FLOW_EXPLANATION_WORDS
    ):
        return panel
    return None


def _source_flow_unit_for_tag(tag: Tag, panel: Tag) -> Tag | None:
    node: Tag | None = tag
    while isinstance(node, Tag):
        if _is_source_flow_unit(node):
            parent = node.parent if isinstance(node.parent, Tag) else None
            if parent is panel or _is_transparent_source_flow_host(parent, panel):
                return node
            return None
        if node is panel:
            break
        node = node.parent if isinstance(node.parent, Tag) else None
    return panel if _is_source_flow_unit(panel) else None


def _is_transparent_source_flow_host(tag: Tag | None, panel: Tag) -> bool:
    if not isinstance(tag, Tag) or tag.parent is not panel:
        return False
    name = str(tag.name or "").lower()
    if name not in {"div", "section"}:
        return False
    classes = _class_tokens(tag)
    transparent_tokens = {
        "section-body",
        "panel-body",
        "content",
        "section-content",
        "panel-content",
        "body",
    }
    role_blob = _semantic_role_blob(tag, include_ancestors=False)
    return bool(classes & transparent_tokens) or any(token in role_blob for token in transparent_tokens)


def _is_source_flow_unit(tag: Tag) -> bool:
    classes = {str(cls).strip().lower() for cls in (tag.get("class") or []) if str(cls).strip()}
    if classes & _SOURCE_FLOW_UNIT_CLASS_TOKENS:
        return True
    for key in (
        "data-flow-unit",
        "data-source-flow-unit",
        "data-asset-flow-unit",
        "data-figure-flow-unit",
        "data-layout-mode",
        "data-role",
        "role",
    ):
        value = str(tag.get(key) or "").strip().lower().replace("_", "-")
        if any(token in value for token in _SOURCE_FLOW_UNIT_CLASS_TOKENS):
            return True
    return False


def _flow_unit_has_direct_flow_text(unit: Tag) -> bool:
    for child in unit.find_all(True, recursive=False):
        if not isinstance(child, Tag):
            continue
        name = str(child.name or "").lower()
        if name in {"figure", "img", "table", "figcaption", "caption"}:
            continue
        if name in {"h1", "h2", "h3", "h4", "p", "ul", "ol", "blockquote", "div", "aside"}:
            if _visible_word_count(child.get_text(" ", strip=True)) >= 2:
                return True
    return False


def _panel_source_binding_count(panel: Tag) -> int:
    seen: set[int] = set()
    count = 0
    for raw_tag in panel.find_all(["figure", "img", "table"]):
        if not isinstance(raw_tag, Tag):
            continue
        tag = _source_wrap_tag(raw_tag)
        if id(tag) in seen:
            continue
        if not _tag_has_source_binding(tag):
            continue
        seen.add(id(tag))
        count += 1
    return count


def _tag_has_source_binding(tag: Tag) -> bool:
    for key in ("data-source-id", "data-layer-id", "data-asset-id"):
        if str(tag.get(key) or "").strip():
            return True
    for child in tag.find_all(True):
        if not isinstance(child, Tag):
            continue
        for key in ("data-source-id", "data-layer-id", "data-asset-id"):
            if str(child.get(key) or "").strip():
                return True
    return False


def _nearest_inner_panel_wrapper(tag: Tag, panel: Tag) -> Tag | None:
    node = tag.parent
    while isinstance(node, Tag) and node is not panel:
        if _is_inner_panel_flow_wrapper(node):
            return node
        node = node.parent
    return None


def _panel_has_single_inner_flow_wrapper(panel: Tag) -> bool:
    element_children = [
        child for child in panel.find_all(True, recursive=False)
        if isinstance(child, Tag)
    ]
    return len(element_children) == 1 and _is_inner_panel_flow_wrapper(element_children[0])


def _is_inner_panel_flow_wrapper(tag: Tag) -> bool:
    classes = {str(cls).strip().lower() for cls in (tag.get("class") or []) if str(cls).strip()}
    if any(token in classes for token in _INNER_PANEL_FLOW_WRAPPER_CLASS_TOKENS):
        return True
    role_blob = " ".join(
        str(tag.get(key) or "")
        for key in ("data-flow-region", "data-lane", "data-role", "role")
    ).lower()
    return any(token in role_blob for token in _INNER_PANEL_FLOW_WRAPPER_CLASS_TOKENS)


def _panel_has_direct_flow_text(panel: Tag) -> bool:
    for child in panel.find_all(True, recursive=False):
        if not isinstance(child, Tag):
            continue
        name = str(child.name or "").lower()
        if name in {"h1", "h2", "h3", "h4", "p", "ul", "ol", "blockquote"}:
            if _visible_word_count(child.get_text(" ", strip=True)) >= 2:
                return True
    return False


def _visible_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’._+:/%-]*", str(text or "")))


def _source_flow_violation_detail(
    violation_kind: str,
    element: Tag,
    *,
    why: str,
    repair_hint: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    detail = {
        "violation_kind": violation_kind,
        "element_label": _tag_label(element),
        "element_block_id": str(element.get("data-block-id") or "").strip(),
        "why_blocking": why,
        "repair_hint": repair_hint,
    }
    if evidence:
        for key in (
            "matched_selector",
            "matched_display",
            "matched_class",
            "selector_scope",
        ):
            value = evidence.get(key)
            if value:
                detail[key] = value
    return detail


def _dedupe_source_flow_violation_details(details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for detail in details:
        key = (
            str(detail.get("violation_kind") or ""),
            str(detail.get("element_label") or ""),
            str(detail.get("matched_selector") or ""),
            str(detail.get("why_blocking") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(detail)
    return deduped


def _panel_direct_children_layout_split_details(
    panel: Tag,
    css: str,
    *,
    why: str,
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for child in panel.find_all(True, recursive=False):
        if not isinstance(child, Tag):
            continue
        if not _descendant_has_source_id(child):
            continue
        if _is_pure_source_visual_shell(child):
            continue
        classes = [str(cls).strip() for cls in (child.get("class") or []) if str(cls).strip()]
        split_class = next(
            (
                cls for cls in classes
                if any(token in cls.lower() for token in _SEPARATE_SOURCE_LAYOUT_CLASS_TOKENS)
            ),
            "",
        )
        if split_class:
            details.append(_source_flow_violation_detail(
                "source_text_split_child",
                child,
                evidence={
                    "matched_class": split_class,
                    "selector_scope": "class_token",
                },
                why=why,
                repair_hint=(
                    "Remove this media/text split child wrapper. Put the source visual shell and its "
                    "readout/native rows as direct siblings inside one .figure-flow-unit/.source-flow-unit."
                ),
            ))
            continue
        display_evidence = _tag_display_layout_evidence(child, css)
        if display_evidence:
            details.append(_source_flow_violation_detail(
                "source_text_split_child",
                child,
                evidence=display_evidence[0],
                why=why,
                repair_hint=(
                    "This direct child contains source evidence and text inside a grid/flex split. "
                    "Flatten it so the visual shell and readout are direct siblings in the source flow unit."
                ),
            ))
            continue
        style = str(child.get("style") or "")
        inline_display = re.search(
            r"(?:^|;)\s*display\s*:\s*(grid|inline-grid|flex|inline-flex)\b",
            style,
            re.I,
        )
        if inline_display:
            details.append(_source_flow_violation_detail(
                "source_text_split_child",
                child,
                evidence={
                    "matched_selector": "inline style",
                    "matched_display": inline_display.group(1).lower(),
                    "selector_scope": "inline",
                },
                why=why,
                repair_hint=(
                    "Remove inline grid/flex from this mixed source/text child and make source visual "
                    "and readout direct siblings in the flow unit."
                ),
            ))
    return _dedupe_source_flow_violation_details(details)


def _panel_direct_children_use_layout_split(panel: Tag, css: str) -> bool:
    return bool(_panel_direct_children_layout_split_details(
        panel,
        css,
        why="panel direct children include grid/flex source/text split wrappers",
    ))


def _tag_declares_display_layout(tag: Tag, css: str) -> bool:
    if _tag_display_layout_evidence(tag, css):
        return True
    style = str(tag.get("style") or "")
    return bool(re.search(r"(?:^|;)\s*display\s*:\s*(?:grid|inline-grid|flex|inline-flex)\b", style, re.I))


def _tag_display_layout_evidence(tag: Tag, css: str) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    classes = [str(cls).strip() for cls in (tag.get("class") or []) if str(cls).strip()]
    for cls in classes:
        evidence.extend(_css_class_display_layout_evidence(cls, css))
    style = str(tag.get("style") or "")
    inline_display = re.search(
        r"(?:^|;)\s*display\s*:\s*(grid|inline-grid|flex|inline-flex)\b",
        style,
        re.I,
    )
    if inline_display:
        evidence.append({
            "matched_selector": "inline style",
            "matched_display": inline_display.group(1).lower(),
            "selector_scope": "inline",
        })
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in evidence:
        key = (
            str(item.get("matched_class") or ""),
            str(item.get("matched_selector") or ""),
            str(item.get("matched_display") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _is_pure_source_visual_shell(tag: Tag) -> bool:
    if not _descendant_has_source_id(tag):
        return False
    clone = BeautifulSoup(str(tag), "html.parser")
    root = clone.find(True)
    if not isinstance(root, Tag):
        return False
    for visual in list(root.find_all(["figure", "img", "table", "figcaption", "caption"])):
        if isinstance(visual, Tag):
            visual.decompose()
    return _visible_word_count(root.get_text(" ", strip=True)) < 2


def _descendant_has_source_id(tag: Tag) -> bool:
    if str(tag.get("data-source-id") or tag.get("data-layer-id") or tag.get("data-asset-id") or "").strip():
        return True
    return any(
        isinstance(child, Tag)
        and str(child.get("data-source-id") or child.get("data-layer-id") or child.get("data-asset-id") or "").strip()
        for child in tag.find_all(True)
    )


def _tag_label(tag: Tag) -> str:
    classes = ".".join(str(cls).strip() for cls in (tag.get("class") or []) if str(cls).strip())
    name = str(tag.name or "node").lower()
    return f"<{name}{'.' + classes if classes else ''}>"


def _source_wrap_candidates(
    soup: BeautifulSoup,
    css: str,
    ctx: ToolContext,
    required: set[str],
) -> list[dict[str, Any]]:
    seen: set[int] = set()
    candidates: list[dict[str, Any]] = []
    for raw_tag in soup.find_all(["figure", "img", "table"]):
        if not isinstance(raw_tag, Tag):
            continue
        tag = _source_wrap_tag(raw_tag)
        if id(tag) in seen:
            continue
        source_id = (
            _source_id_for_tag(tag, ctx)
            or _source_id_for_tag(raw_tag, ctx)
            or _source_id_for_tag_or_descendant(tag, ctx)
        )
        if source_id not in required:
            continue
        if source_id.startswith("ingest_table_") and not _is_bound_source_table_crop_tag(tag, ctx):
            continue
        panel = _nearest_source_wrap_panel(tag)
        if panel is None:
            continue
        if _is_identity_header_source_asset(tag, panel, source_id, ctx):
            seen.add(id(tag))
            continue
        flow_unit = _source_flow_unit_for_tag(tag, panel)
        editorial_context = _nearest_editorial_source_context(tag, panel) if _source_in_editorial_flow(tag) else None
        local_container = flow_unit or editorial_context or panel
        local_words = _visible_panel_word_count(local_container, exclude=tag)
        seen.add(id(tag))
        flow_label = flow_unit or editorial_context
        actual_wrapper = _nearest_separate_source_layout_wrapper(tag, css, panel) or _nearest_inner_panel_wrapper(tag, panel)
        parent = tag.parent if isinstance(tag.parent, Tag) else None
        flow_unit_violation_details = _source_flow_unit_violation_details(panel, tag, css)
        candidates.append({
            "source_id": source_id,
            "block_id": str(tag.get("data-block-id") or raw_tag.get("data-block-id") or "").strip(),
            "panel_id": str(panel.get("data-block-id") or panel.get("data-slot-id") or "").strip(),
            "panel_role": str(panel.get("data-panel-role") or panel.get("data-role") or "").strip(),
            "panel_words": _visible_panel_word_count(panel, exclude=tag),
            "local_words": local_words,
            "flow_unit": _tag_label(flow_label) if flow_label is not None and flow_label is not panel else "panel-root",
            "actual_wrapper_label": _tag_label(actual_wrapper or flow_label or parent or tag),
            "actual_parent_label": _tag_label(parent) if parent is not None else "",
            "flow_unit_violations": [
                str(detail.get("why_blocking") or "").strip()
                for detail in flow_unit_violation_details
                if str(detail.get("why_blocking") or "").strip()
            ],
            "flow_unit_violation_details": flow_unit_violation_details,
            "visible_figcaption": _visible_figcaption_text(tag),
            "wrap_evidence": _tag_wrap_evidence(tag, css),
            "separate_layout_evidence": _source_separate_layout_evidence(tag, css, panel),
        })
    return candidates


def _designer_owned_flow_canvas_shell_error(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
) -> ToolResultRecord | None:
    cw = int(canvas["w_px"])
    ch = int(canvas["h_px"])
    tolerance = 3
    issues: list[dict[str, Any]] = []
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        classes = _class_tokens(tag)
        layout_region = str(tag.get("data-layout-region") or "").strip().lower()
        designer_flow_mode = any(
            _designer_owned_css_token(tag.get(key))
            for key in (
                "data-layout-mode",
                "data-poster-layout-mode",
                "data-css-mode",
                "data-compiler-mode",
                "data-render-contract",
                "data-render-mode",
            )
        )
        if not (
            "flow-poster" in classes
            or "poster-grid" in classes
            or "poster-columns" in classes
            or "poster-column" in classes
            or "editorial-poster" in classes
            or designer_flow_mode
            or layout_region == "main_panels"
        ):
            continue
        block_id = str(tag.get("data-block-id") or "").strip()
        measured = bboxes.get(block_id)
        bbox = _bbox_only(measured)
        if not block_id or bbox is None:
            continue
        x = int(bbox.get("x") or 0)
        y = int(bbox.get("y") or 0)
        w = int(bbox.get("w") or 0)
        h = int(bbox.get("h") or 0)
        metrics = measured.get("_layout_metrics") if isinstance(measured, dict) else {}
        metrics = metrics if isinstance(metrics, dict) else {}
        client_w = float(metrics.get("client_width_px") or w or 0)
        client_h = float(metrics.get("client_height_px") or h or 0)
        scroll_w = float(metrics.get("scroll_width_px") or client_w or 0)
        scroll_h = float(metrics.get("scroll_height_px") or client_h or 0)
        scroll_overflow = {
            "right": max(0, int(round(scroll_w - client_w))),
            "bottom": max(0, int(round(scroll_h - client_h))),
        }
        overflow = {
            "left": max(0, -x),
            "top": max(0, -y),
            "right": max(0, x + w - cw),
            "bottom": max(0, y + h - ch),
        }
        max_overflow = max(overflow.values())
        max_scroll_overflow = max(scroll_overflow.values())
        computed_style = measured.get("_computed_style") if isinstance(measured, dict) else {}
        computed_style = computed_style if isinstance(computed_style, dict) else {}
        padding_x = _safe_float(computed_style.get("padding_left_px")) + _safe_float(computed_style.get("padding_right_px"))
        padding_y = _safe_float(computed_style.get("padding_top_px")) + _safe_float(computed_style.get("padding_bottom_px"))
        root_wrapper_padding_overflow = (
            "editorial-poster" in classes
            and x >= -tolerance
            and y >= -tolerance
            and w >= int(cw * 0.94)
            and h >= int(ch * 0.94)
            and max_overflow > tolerance
            and max_overflow <= max(96, int(round(max(padding_x, padding_y))) + 8)
            and (
                padding_x > tolerance
                or padding_y > tolerance
                or str(computed_style.get("box_sizing") or "").lower() == "content-box"
            )
        )
        special_full_canvas_body_row = (
            (
                "poster-column" in classes
                or "poster-columns" in classes
            )
            and y > 0
            and h >= ch - tolerance
            and overflow.get("bottom", 0) > 20
        )
        if (
            max_overflow <= tolerance
            and not special_full_canvas_body_row
            and not ("flow-poster" in classes and h > ch + tolerance)
        ):
            continue
        if max_overflow <= tolerance and max_scroll_overflow > tolerance and not special_full_canvas_body_row:
            continue
        issue_id = "designer_flow_canvas_overflow"
        repair = (
            "Keep the outer designer-owned flow inside the fixed canvas. Preserve the "
            "composition and make local height repairs; do not hide overflowing lower "
            "content."
        )
        if root_wrapper_padding_overflow:
            issue_id = "root_wrapper_padding_overflow"
            repair = (
                "The nested `.editorial-poster` root is full-canvas and also has padding/content-box "
                "sizing, so its wrapper grows beyond `.paper-poster`. Keep the canvas fixed and scope "
                "the wrapper repair to the visible canvas box with `grid-row:1/-1`, pixel width/height "
                "caps from the measured bbox, and `box-sizing:border-box`; do not compress poster content "
                "for this issue."
            )
        elif "poster-column" in classes and special_full_canvas_body_row:
            issue_id = "poster_column_full_canvas_height_below_header"
            repair = (
                "This column starts below the header but is as tall as the full canvas, so it "
                "overflows. Do not set `.poster-column` to 1536px/100vh/full-canvas height. "
                "Instead make `.editorial-poster` the fixed-height grid, put `.poster-columns` "
                "in the `minmax(0,1fr)` body row with `min-height:0;align-items:stretch`, and let "
                "columns use that remaining row height."
            )
        elif "poster-columns" in classes and special_full_canvas_body_row:
            issue_id = "poster_columns_full_canvas_height_below_header"
            repair = (
                "The columns container starts below the header but is as tall as the full canvas. "
                "Set the root editorial grid to the fixed canvas height and let `.poster-columns` "
                "stretch only inside the remaining `1fr` body row; do not give the body row a "
                "full-canvas pixel/viewport height."
            )
        issue = {
            "id": issue_id,
            "block_id": block_id,
            "classes": sorted(classes),
            "bbox": {"x": x, "y": y, "w": w, "h": h},
            "canvas": {"w": cw, "h": ch},
            "overflow_px": overflow,
            "scroll_overflow_px": scroll_overflow,
            "layout_metrics": metrics,
            "computed_style": computed_style,
            "repair": repair,
        }
        if root_wrapper_padding_overflow:
            issue["deterministic_repair"] = {
                "kind": "cap_nested_editorial_root_to_canvas_visible_box",
                "selector": ".paper-poster > .editorial-poster",
                "width_px": max(1, int(cw - max(0, x))),
                "height_px": max(1, int(ch - max(0, y))),
                "grid_row": "1 / -1",
                "grid_column": "1 / -1",
                "forbid_percent_height": True,
            }
        issues.append(issue)
    if not issues:
        return None
    root_wrapper_only = all(
        isinstance(issue, dict) and issue.get("id") == "root_wrapper_padding_overflow"
        for issue in issues
    )
    return obs_error(
        (
            "propose_paper_poster_html found a nested editorial root wrapper padded beyond the fixed poster canvas."
            if root_wrapper_only else
            "propose_paper_poster_html found designer-owned flow containers outside the fixed poster canvas."
        ),
        category="validation",
        payload={
            "issue_id": (
                "paper_poster_html_root_wrapper_padding_overflow"
                if root_wrapper_only else
                "paper_poster_html_designer_flow_canvas_overflow"
            ),
            "repair_route": (
                "repair_root_wrapper_padding_box_sizing"
                if root_wrapper_only else
                "constrain_designer_owned_flow_shell"
            ),
            "issues": issues[:8],
            "hint": (
                "The nested `.editorial-poster` wrapper is the fixed-canvas child and should not add "
                "content-box padding beyond `.paper-poster`. Patch only wrapper sizing with scoped CSS "
                "from `issues[].deterministic_repair`: span the root grid with `grid-row:1/-1`, cap "
                "width/height to the measured visible canvas box, and use `box-sizing:border-box`. "
                "Do not use a blind `height:100%` repair that can collapse into the header row. "
                "Do not shrink body typography, source figures, tables, or section padding to clear this wrapper error."
                if root_wrapper_only else
                "Designer-owned paper poster CSS must keep the outer flow inside the fixed canvas. "
                "If columns begin below the header, their height must be the remaining body-row "
                "height, not the full canvas height. Use `.editorial-poster{height:100%;display:grid;"
                "grid-template-rows:<compact-header> minmax(0,1fr);}` and stretch `.poster-columns` "
                "inside that second row. Preserve the current composition and make local height "
                "repairs only: shorten low-value prose/readouts, reduce section gaps and padding, "
                "and then make only small bounded source-asset height adjustments. Do not rewrite "
                "the poster into a new compressed layout, do not add `overflow:hidden` to "
                "`.poster-column` or `.poster-section` to hide lower content, and do not shrink "
                "source figures/tables below readable conference-poster sizes."
            ),
        },
    )


def _designer_owned_local_flow_overflow_error(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
) -> ToolResultRecord | None:
    cw = int(canvas["w_px"])
    ch = int(canvas["h_px"])
    tolerance = 3
    issues: list[dict[str, Any]] = []
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        classes = _class_tokens(tag)
        layout_region = str(tag.get("data-layout-region") or "").strip().lower()
        editorial_section = "poster-section" in classes and _source_in_editorial_flow(tag)
        editorial_flow_unit = bool(
            classes & {"figure-flow-unit", "source-flow-unit"}
        ) and _source_in_editorial_flow(tag)
        if not (
            "editorial-poster" in classes
            or "poster-columns" in classes
            or "poster-column" in classes
            or editorial_section
            or editorial_flow_unit
            or layout_region in {"editorial_columns", "main_panels"}
        ):
            continue
        block_id = str(tag.get("data-block-id") or "").strip()
        measured = bboxes.get(block_id)
        bbox = _bbox_only(measured)
        if not block_id or bbox is None:
            continue
        x = int(bbox.get("x") or 0)
        y = int(bbox.get("y") or 0)
        w = int(bbox.get("w") or 0)
        h = int(bbox.get("h") or 0)
        overflow = {
            "left": max(0, -x),
            "top": max(0, -y),
            "right": max(0, x + w - cw),
            "bottom": max(0, y + h - ch),
        }
        if any(value > tolerance for value in overflow.values()):
            continue
        metrics = measured.get("_layout_metrics") if isinstance(measured, dict) else {}
        metrics = metrics if isinstance(metrics, dict) else {}
        client_h = float(metrics.get("client_height_px") or h or 0)
        scroll_h = float(metrics.get("scroll_height_px") or client_h or 0)
        bottom_overflow = max(0, int(round(scroll_h - client_h)))
        if bottom_overflow <= tolerance:
            continue
        issue = _local_flow_issue_for_container(
            tag,
            bboxes,
            canvas,
            bottom_overflow=bottom_overflow,
            client_height_px=client_h,
            scroll_height_px=scroll_h,
        )
        near_miss_threshold = max(8, int(round(_median_body_line_height_px(tag, bboxes) * 0.5)))
        visible_overflow = _local_flow_has_obvious_visible_overflow(tag, soup, bboxes, canvas)
        is_near_miss = bottom_overflow <= near_miss_threshold and not visible_overflow
        issue.update({
            "severity": "near_miss" if is_near_miss else "hard",
            "soft_finalizable": is_near_miss,
            "visible_overflow": visible_overflow,
            "near_miss_threshold_px": near_miss_threshold,
        })
        issues.append(issue)
    if not issues:
        return None
    all_issues = _dedupe_local_flow_issues(issues)
    detailed_kinds = {"poster_section", "source_flow_unit", "figure_flow_unit"}
    if any(str(issue.get("container_kind") or "") in detailed_kinds for issue in all_issues):
        issues = [
            issue for issue in all_issues
            if str(issue.get("container_kind") or "") in detailed_kinds
        ]
    else:
        issues = all_issues
    issues = _dedupe_local_flow_issues(issues)
    hard_issues = [issue for issue in all_issues if str(issue.get("severity") or "hard") != "near_miss"]
    soft_finalizable = not hard_issues
    payload_issues = list(issues)
    if hard_issues and not any(str(issue.get("severity") or "hard") != "near_miss" for issue in payload_issues):
        payload_issues = hard_issues[:2] + payload_issues
    visible_overflow = any(bool(issue.get("visible_overflow")) for issue in all_issues)
    return obs_error(
        "propose_paper_poster_html found local flow content taller than its filled column/section container.",
        category="validation",
        payload={
            "issue_id": "paper_poster_html_local_flow_overflow",
            "repair_route": "repair_local_column_section_overflow",
            "severity": "near_miss" if soft_finalizable else "hard",
            "soft_finalizable": soft_finalizable,
            "visible_overflow": visible_overflow,
            "near_miss_issue_count": len(all_issues) - len(hard_issues),
            "hard_issue_count": len(hard_issues),
            "hidden_hard_issue_count": max(0, len(hard_issues) - sum(1 for issue in payload_issues[:8] if str(issue.get("severity") or "hard") != "near_miss")),
            "issues": payload_issues[:8],
            "hint": (
                "The outer poster shell fills the fixed canvas; do not rebuild or globally "
                "compress it. Patch the named column/section/source-flow unit only: shorten "
                "low-value local prose, tighten section spacing, move an optional source-flow "
                "unit into an underfilled sibling section, or make a small bounded max-height "
                "adjustment while keeping source figures/tables readable."
            ),
        },
    )


def _dedupe_local_flow_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped_issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for issue in issues:
        key = (
            str(issue.get("container_kind") or ""),
            str(issue.get("container_id") or ""),
            str(issue.get("column_id") or ""),
            str(issue.get("section_id") or ""),
            str(issue.get("flow_unit_id") or issue.get("overflow_block_id") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped_issues.append(issue)
    return deduped_issues


def _local_flow_has_obvious_visible_overflow(
    container: Tag,
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
) -> bool:
    if _local_flow_has_bbox_canvas_escape(container, bboxes, canvas):
        return True
    if _local_flow_has_visible_clipping(container, bboxes):
        return True
    container_ids = {
        str(tag.get("data-block-id") or "").strip()
        for tag in [container, *container.find_all(True)]
        if isinstance(tag, Tag) and str(tag.get("data-block-id") or "").strip()
    }
    if not container_ids:
        return False
    for issue in _severe_text_overlap_issues(_text_overlap_issues(soup, bboxes)):
        if (
            str(issue.get("left_block_id") or "") in container_ids
            or str(issue.get("right_block_id") or "") in container_ids
        ):
            return True
    return False


def _local_flow_has_bbox_canvas_escape(
    container: Tag,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
) -> bool:
    cw = max(1, _safe_int(canvas.get("w_px"), default=1))
    ch = max(1, _safe_int(canvas.get("h_px"), default=1))
    for tag in [container, *container.find_all(True)]:
        if not isinstance(tag, Tag):
            continue
        bbox = _bbox_only(_bbox_for_tag(tag, bboxes))
        if not bbox:
            continue
        x = _safe_int(bbox.get("x"), default=0)
        y = _safe_int(bbox.get("y"), default=0)
        w = _safe_int(bbox.get("w"), default=0)
        h = _safe_int(bbox.get("h"), default=0)
        if x < -3 or y < -3 or x + w > cw + 3 or y + h > ch + 3:
            return True
    return False


def _local_flow_has_visible_clipping(
    container: Tag,
    bboxes: dict[str, dict[str, int]],
) -> bool:
    for tag in [container, *container.find_all(True)]:
        if not isinstance(tag, Tag):
            continue
        if _infer_block_kind(tag) not in {"text", "caption", "quote", "metric"}:
            continue
        bbox = _bbox_for_tag(tag, bboxes)
        visible = bbox.get("_visible_bbox") if isinstance(bbox, dict) else None
        if not isinstance(bbox, dict) or not isinstance(visible, dict):
            continue
        raw = _bbox_only(bbox)
        shown = _bbox_only(visible)
        if not raw or not shown:
            continue
        raw_area = max(1, _bbox_plain_area(raw))
        shown_area = max(0, _bbox_plain_area(shown))
        if shown_area < raw_area * 0.96:
            return True
    return False


def _designer_owned_heading_flow_overflow_error(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
) -> ToolResultRecord | None:
    cw = int(canvas["w_px"])
    ch = int(canvas["h_px"])
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()

    for header in _identity_header_tags(soup):
        issue = _heading_flow_issue_for_tag(
            header,
            bboxes,
            canvas,
            role="identity_header",
            failure_kind="identity_header_scroll_overflow",
            threshold_ratio=0.08,
            min_threshold_px=12.0,
        )
        if issue:
            block_id = str(issue.get("block_id") or issue.get("container_id") or "")
            if block_id and block_id not in seen:
                seen.add(block_id)
                issues.append(issue)

    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if not isinstance(tag, Tag):
            continue
        role = "title" if str(tag.name or "").lower() == "h1" else "section_heading"
        issue = _heading_flow_issue_for_tag(
            tag,
            bboxes,
            canvas,
            role=role,
            failure_kind="heading_text_box_overflow",
            threshold_ratio=0.08,
            min_threshold_px=6.0,
        )
        if issue:
            block_id = str(issue.get("block_id") or "")
            if block_id and block_id not in seen:
                seen.add(block_id)
                issues.append(issue)

    if not issues:
        return None
    return obs_error(
        "propose_paper_poster_html found title/header/section heading content taller than its allocated lane.",
        category="validation",
        payload={
            "issue_id": "paper_poster_html_heading_flow_overflow",
            "repair_route": "repair_heading_header_lane_fit",
            "issues": issues[:8],
            "canvas": {"w": cw, "h": ch},
            "hint": (
                "Repair the named header/title/heading lane, not the whole poster: keep the identity header "
                "identity-only, compact title/authors/school-institution-company rows, reduce only local heading "
                "font-size/line-height/padding if needed, or allocate a little more header height from the "
                "body grid. Do not hide overflow or push header text over the poster body."
            ),
        },
    )


def _heading_flow_issue_for_tag(
    tag: Tag,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
    *,
    role: str,
    failure_kind: str,
    threshold_ratio: float,
    min_threshold_px: float,
) -> dict[str, Any] | None:
    block_id = str(tag.get("data-block-id") or tag.get("data-slot-id") or "").strip()
    measured = bboxes.get(block_id) if block_id else None
    if not block_id or not isinstance(measured, dict):
        return None
    bbox = _bbox_only(measured)
    if not bbox:
        return None
    cw = int(canvas["w_px"])
    ch = int(canvas["h_px"])
    x = int(bbox.get("x") or 0)
    y = int(bbox.get("y") or 0)
    w = int(bbox.get("w") or 0)
    h = int(bbox.get("h") or 0)
    if (
        x < -3
        or y < -3
        or x + w > cw + 3
        or y + h > ch + 3
    ):
        return None
    metrics = measured.get("_layout_metrics") if isinstance(measured.get("_layout_metrics"), dict) else {}
    if not metrics:
        return None
    client_w = _safe_float(metrics.get("client_width_px") or w)
    client_h = _safe_float(metrics.get("client_height_px") or h)
    scroll_w = _safe_float(metrics.get("scroll_width_px") or client_w)
    scroll_h = _safe_float(metrics.get("scroll_height_px") or client_h)
    width_overflow = max(0.0, scroll_w - client_w)
    height_overflow = max(0.0, scroll_h - client_h)
    height_threshold = max(min_threshold_px, client_h * threshold_ratio)
    width_threshold = max(12.0, client_w * 0.03)
    scroll_metric_exceeds_threshold = height_overflow > height_threshold or width_overflow > width_threshold
    style = measured.get("_computed_style") if isinstance(measured.get("_computed_style"), dict) else {}
    lane_tag = tag if role == "identity_header" else _heading_lane_tag_for_tag(tag)
    lane_bbox = _bbox_only(_bbox_for_tag(lane_tag, bboxes)) if isinstance(lane_tag, Tag) else None
    if not lane_bbox:
        lane_bbox = bbox
    visible_lines = _bbox_list_from_measurement(measured.get("_text_line_bboxes"))
    raw_lines = _bbox_list_from_measurement(measured.get("_raw_text_line_bboxes")) or visible_lines
    line_union = _bbox_union(raw_lines) if raw_lines else None
    visible_line_union = _bbox_union(visible_lines) if visible_lines else None
    child_union = _heading_child_bbox_union(tag, bboxes) if role == "identity_header" else None
    layout_evidence = _heading_flow_layout_evidence(
        tag,
        role=role,
        style=style,
        bbox=bbox,
        lane_bbox=lane_bbox,
        line_union=line_union,
        visible_line_union=visible_line_union,
        child_union=child_union,
        bboxes=bboxes,
        height_overflow=height_overflow,
        width_overflow=width_overflow,
        height_threshold=height_threshold,
        width_threshold=width_threshold,
    )
    if not layout_evidence:
        return None
    if not scroll_metric_exceeds_threshold and not any(
        evidence.get("kind") in {
            "text_line_overflows_lane",
            "child_block_overflows_lane",
            "text_line_clipped",
            "horizontal_heading_overflow",
            "heading_overlaps_body",
            "heading_overlaps_sibling_content",
        }
        for evidence in layout_evidence
        if isinstance(evidence, dict)
    ):
        return None
    text = " ".join(tag.get_text(" ", strip=True).split())
    target_block_ids = _unique_nonempty([block_id])
    target_selectors = _selectors_for_ids(target_block_ids)
    header = _nearest_identity_header(tag)
    allowed_selectors = _unique_nonempty([
        *target_selectors,
        _repair_selector_for_tag(header),
        ".editorial-poster",
    ])
    forbidden_selectors = [
        ".poster-column",
        ".poster-section",
        ".figure-flow-unit",
        ".source-flow-unit",
        "[data-source-id]",
        "[data-layer-id]",
    ]
    preserve_selectors = [
        "[data-source-id]",
        "[data-layer-id]",
        ".poster-column",
        ".poster-section",
        ".figure-flow-unit",
        ".source-flow-unit",
    ]
    return {
        "id": "heading_flow_scroll_overflow",
        "failure_kind": failure_kind,
        "role": role,
        "block_id": block_id,
        "container_id": block_id,
        "client_width_px": round(client_w, 2),
        "scroll_width_px": round(scroll_w, 2),
        "client_height_px": round(client_h, 2),
        "scroll_height_px": round(scroll_h, 2),
        "bottom_overflow_px": round(height_overflow, 2),
        "width_overflow_px": round(width_overflow, 2),
        "scroll_overflow_px": {"bottom": round(height_overflow, 2), "right": round(width_overflow, 2)},
        "bbox": bbox,
        "lane_bbox": lane_bbox,
        "text_line_bbox": line_union,
        "visible_text_line_bbox": visible_line_union,
        "layout_evidence": layout_evidence,
        "actual_font_size_px": style.get("font_size_px"),
        "actual_font_weight": style.get("font_weight"),
        "actual_line_height": style.get("line_height"),
        "sample_text": text[:180],
        "repair_scope": {
            "mode": "heading_lane",
            "target_block_ids": target_block_ids,
            "allowed_selectors": allowed_selectors,
            "forbidden_selectors": forbidden_selectors,
            "preserve_selectors": preserve_selectors,
            "allowed_operations": [
                "compact title/authors/school-institution-company rows inside the named header lane",
                "reduce only local title or heading font-size/line-height/padding",
                "allocate a little more header or heading lane height from the body grid",
            ],
            "forbidden_operations": [
                "rewrite body sections",
                "delete source figures or tables",
                "move body contribution/readout prose into the header",
                "mask overflow with overflow hidden or clipping",
            ],
        },
        "target_block_ids": target_block_ids,
        "target_selectors": target_selectors,
        "allowed_selectors": allowed_selectors,
        "forbidden_selectors": forbidden_selectors,
        "preserve_selectors": preserve_selectors,
        "expected": (
            "Header/title/section heading text must fit its own allocated lane with no scroll overflow "
            "or spill into the poster body."
        ),
        "repair": (
            "Patch this local header/heading lane: reduce only local title/heading size, line-height, "
            "or padding; compact authors/meta rows; or allocate a little more header/heading height. "
            "Do not use overflow:hidden to mask the issue."
        ),
    }


def _heading_lane_tag_for_tag(tag: Tag) -> Tag | None:
    header = _nearest_identity_header(tag)
    if isinstance(header, Tag):
        return header
    for parent in tag.parents:
        if not isinstance(parent, Tag):
            continue
        classes = _class_tokens(parent)
        if classes & {"title-cluster", "section-heading", "heading-row", "header-title", "poster-header"}:
            return parent
        role = str(parent.get("data-panel-role") or parent.get("data-role") or "").strip().lower()
        if role in {"identity_header", "poster_header", "section_heading", "heading"}:
            return parent
    return tag.parent if isinstance(tag.parent, Tag) else None


def _bbox_list_from_measurement(value: Any) -> list[dict[str, int]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, int]] = []
    for item in value:
        box = _bbox_only(item if isinstance(item, dict) else None)
        if box:
            out.append(box)
    return out


def _bbox_union(boxes: list[dict[str, int]]) -> dict[str, int] | None:
    valid = [_bbox_only(box) for box in boxes]
    valid = [box for box in valid if box]
    if not valid:
        return None
    left = min(int(box.get("x") or 0) for box in valid)
    top = min(int(box.get("y") or 0) for box in valid)
    right = max(int(box.get("x") or 0) + int(box.get("w") or 0) for box in valid)
    bottom = max(int(box.get("y") or 0) + int(box.get("h") or 0) for box in valid)
    return {"x": left, "y": top, "w": max(1, right - left), "h": max(1, bottom - top)}


def _heading_child_bbox_union(tag: Tag, bboxes: dict[str, dict[str, int]]) -> dict[str, int] | None:
    boxes: list[dict[str, int]] = []
    for child in tag.find_all(True):
        if not isinstance(child, Tag):
            continue
        child_id = str(child.get("data-block-id") or "").strip()
        if not child_id:
            continue
        box = _bbox_only(bboxes.get(child_id) if isinstance(bboxes.get(child_id), dict) else None)
        if box:
            boxes.append(box)
    return _bbox_union(boxes)


def _bbox_overflow_sides(
    inner: dict[str, int] | None,
    outer: dict[str, int] | None,
    *,
    tolerance: float = 3.0,
) -> dict[str, float]:
    if not inner or not outer:
        return {}
    left = max(0.0, float(outer.get("x") or 0) - float(inner.get("x") or 0))
    top = max(0.0, float(outer.get("y") or 0) - float(inner.get("y") or 0))
    right = max(
        0.0,
        (float(inner.get("x") or 0) + float(inner.get("w") or 0))
        - (float(outer.get("x") or 0) + float(outer.get("w") or 0)),
    )
    bottom = max(
        0.0,
        (float(inner.get("y") or 0) + float(inner.get("h") or 0))
        - (float(outer.get("y") or 0) + float(outer.get("h") or 0)),
    )
    return {
        key: round(value, 2)
        for key, value in {"left": left, "top": top, "right": right, "bottom": bottom}.items()
        if value > tolerance
    }


def _heading_bbox_overlap_area(a: dict[str, int] | None, b: dict[str, int] | None) -> float:
    if not a or not b:
        return 0.0
    left = max(float(a.get("x") or 0), float(b.get("x") or 0))
    top = max(float(a.get("y") or 0), float(b.get("y") or 0))
    right = min(float(a.get("x") or 0) + float(a.get("w") or 0), float(b.get("x") or 0) + float(b.get("w") or 0))
    bottom = min(float(a.get("y") or 0) + float(a.get("h") or 0), float(b.get("y") or 0) + float(b.get("h") or 0))
    if right <= left or bottom <= top:
        return 0.0
    return (right - left) * (bottom - top)


def _heading_flow_layout_evidence(
    tag: Tag,
    *,
    role: str,
    style: dict[str, Any],
    bbox: dict[str, int],
    lane_bbox: dict[str, int],
    line_union: dict[str, int] | None,
    visible_line_union: dict[str, int] | None,
    child_union: dict[str, int] | None,
    bboxes: dict[str, dict[str, int]],
    height_overflow: float,
    width_overflow: float,
    height_threshold: float,
    width_threshold: float,
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    line_overflow = _bbox_overflow_sides(line_union, lane_bbox, tolerance=3.0)
    child_overflow = _bbox_overflow_sides(child_union, lane_bbox, tolerance=3.0)
    if line_overflow:
        evidence.append({"kind": "text_line_overflows_lane", "overflow_px": line_overflow})
    if child_overflow:
        evidence.append({"kind": "child_block_overflows_lane", "overflow_px": child_overflow})
    overflow_modes = {
        str((style or {}).get(key) or "").strip().lower()
        for key in ("overflow", "overflow_x", "overflow_y")
    }
    clips_overflow = bool(overflow_modes & {"hidden", "clip", "scroll", "auto"})
    raw_visible_delta = _bbox_overflow_sides(line_union, visible_line_union, tolerance=2.0)
    if raw_visible_delta and clips_overflow:
        evidence.append({"kind": "text_line_clipped", "overflow_px": raw_visible_delta})

    white_space = str((style or {}).get("white_space") or "").strip().lower()
    if width_overflow > width_threshold and (line_overflow.get("right") or white_space in {"nowrap", "pre", "pre-line", "pre-wrap"}):
        evidence.append({"kind": "horizontal_heading_overflow", "overflow_px": round(width_overflow, 2)})

    if role == "identity_header" or _nearest_identity_header(tag):
        root = tag
        for parent in tag.parents:
            if isinstance(parent, Tag):
                root = parent
        columns = root.select_one(".poster-columns,.columns,[data-role='poster-columns']") if isinstance(root, Tag) else None
        columns_bbox = _bbox_only(_bbox_for_tag(columns, bboxes)) if isinstance(columns, Tag) else None
        target = line_union or child_union or bbox
        overlap_area = _heading_bbox_overlap_area(target, columns_bbox)
        if overlap_area > 24.0:
            evidence.append({"kind": "heading_overlaps_body", "overlap_area_px": round(overlap_area, 2)})
    else:
        lane_tag = _heading_lane_tag_for_tag(tag)
        target = line_union or bbox
        if isinstance(lane_tag, Tag):
            tag_ids = {
                str(item.get("data-block-id") or "").strip()
                for item in [tag, *tag.find_all(True)]
                if isinstance(item, Tag)
            }
            for sibling in lane_tag.find_all(True):
                if not isinstance(sibling, Tag):
                    continue
                sibling_id = str(sibling.get("data-block-id") or "").strip()
                if not sibling_id or sibling_id in tag_ids:
                    continue
                sibling_bbox = _bbox_only(_bbox_for_tag(sibling, bboxes))
                overlap_area = _heading_bbox_overlap_area(target, sibling_bbox)
                if overlap_area > 24.0:
                    evidence.append({
                        "kind": "heading_overlaps_sibling_content",
                        "sibling_block_id": sibling_id,
                        "overlap_area_px": round(overlap_area, 2),
                    })
                    break

    if not evidence and not line_union and not child_union:
        severe_height = height_overflow > max(height_threshold * 2.0, 24.0)
        severe_width = width_overflow > max(width_threshold * 2.0, 36.0)
        if severe_height or severe_width:
            evidence.append({
                "kind": "large_scroll_metric_without_line_boxes",
                "height_overflow_px": round(height_overflow, 2),
                "width_overflow_px": round(width_overflow, 2),
            })
    return evidence


def _designer_owned_row_allocation_density_regression_error(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
    local_flow_error: ToolResultRecord,
) -> ToolResultRecord | None:
    payload = local_flow_error.payload if isinstance(local_flow_error.payload, dict) else {}
    raw_issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
    issues = [issue for issue in raw_issues if isinstance(issue, dict)]
    if not issues:
        return None

    cw = int(canvas["w_px"])
    ch = int(canvas["h_px"])
    max_scroll_overflow = _local_flow_overflow_px(issues)
    enriched_issues: list[dict[str, Any]] = []
    reasons: list[dict[str, Any]] = []

    if max_scroll_overflow >= 160:
        reasons.append({
            "id": "large_local_scroll_overflow",
            "max_scroll_overflow_px": max_scroll_overflow,
            "threshold_px": 160,
        })

    for issue in issues:
        enriched = dict(issue)
        container_id = str(issue.get("container_id") or "")
        measured = bboxes.get(container_id) if container_id else None
        metrics = measured.get("_layout_metrics") if isinstance(measured, dict) else {}
        metrics = metrics if isinstance(metrics, dict) else {}
        fallback_h = measured.get("h") if isinstance(measured, dict) else 0
        client_h = _safe_float(metrics.get("client_height_px") or fallback_h)
        scroll_h = _safe_float(metrics.get("scroll_height_px") or client_h)
        if client_h > 0:
            enriched["container_client_height_px"] = round(client_h, 2)
        if scroll_h > 0:
            enriched["container_scroll_height_px"] = round(scroll_h, 2)
        if client_h > 0 and scroll_h > 0:
            ratio = round(scroll_h / max(1.0, client_h), 3)
            enriched["scroll_to_client_ratio"] = ratio
            if client_h < 0.35 * scroll_h:
                reasons.append({
                    "id": "container_height_collapsed_vs_content",
                    "container_id": container_id,
                    "client_height_px": round(client_h, 2),
                    "scroll_height_px": round(scroll_h, 2),
                    "threshold_ratio": 0.35,
                })
        section_id = str(issue.get("section_id") or "")
        section_bbox = bboxes.get(section_id) if section_id else None
        if isinstance(section_bbox, dict):
            enriched["section_height_px"] = _safe_int(section_bbox.get("h"))
        column_id = str(issue.get("column_id") or "")
        column_tag = _find_column_by_label(soup, column_id)
        column_bbox = _bbox_for_tag(column_tag, bboxes) if column_tag is not None else None
        if isinstance(column_bbox, dict):
            enriched["column_height_px"] = _safe_int(column_bbox.get("h"))
        enriched_issues.append(enriched)

    body_bottom = _editorial_body_target_bottom(soup, bboxes, canvas)
    columns_shell = soup.select_one(
        ".poster-columns,[data-layout-region='editorial_columns'],[data-layout-region='main_panels']"
    )
    columns_shell_bbox = _bbox_for_tag(columns_shell, bboxes) if isinstance(columns_shell, Tag) else None
    if columns_shell_bbox:
        columns_bottom = int(columns_shell_bbox.get("y", 0) + columns_shell_bbox.get("h", 0))
        bottom_gap = body_bottom - columns_bottom
        if bottom_gap > int(ch * 0.25):
            reasons.append({
                "id": "columns_shell_stops_above_body_bottom",
                "columns_bottom_px": columns_bottom,
                "body_bottom_px": body_bottom,
                "bottom_gap_px": bottom_gap,
                "threshold_px": int(ch * 0.25),
            })

    for column in soup.select(".poster-column,[data-column-id]")[:4]:
        if not isinstance(column, Tag):
            continue
        column_bbox = _bbox_for_tag(column, bboxes)
        if not column_bbox:
            continue
        section_bboxes = [
            _bbox_for_tag(section, bboxes)
            for section in _direct_editorial_sections(column)
        ]
        valid_section_bboxes = [bbox for bbox in section_bboxes if bbox]
        if not valid_section_bboxes:
            continue
        last_bottom = max(int(bbox["y"] + bbox["h"]) for bbox in valid_section_bboxes)
        column_bottom = int(column_bbox["y"] + column_bbox["h"])
        gap = column_bottom - last_bottom
        if gap > int(ch * 0.25):
            reasons.append({
                "id": "column_sections_stop_above_column_bottom",
                "column_id": _column_label(column),
                "column_bottom_px": column_bottom,
                "sections_bottom_px": last_bottom,
                "bottom_gap_px": gap,
                "threshold_px": int(ch * 0.25),
            })

    fill_issues = _canvas_fill_issues(soup, bboxes, canvas)
    severe_fill = _severe_canvas_fill_issues(fill_issues, canvas)
    if severe_fill:
        reasons.append({
            "id": "severe_lower_band_fill_drop",
            "issues": severe_fill[:4],
        })

    if not reasons:
        return None

    visual_fill_feedback = {
        "candidate": {
            "fill_issues": fill_issues[:8],
            "severe_fill_issues": severe_fill[:8],
            "fill_metrics": _fill_metrics_from_issues(fill_issues),
        }
    }
    return obs_error(
        "propose_paper_poster_html found row allocation and density regression in the editorial poster body.",
        category="validation",
        payload={
            "issue_id": "paper_poster_html_row_allocation_density_regression",
            "repair_route": "restore_editorial_row_allocation_preserve_density",
            "issues": enriched_issues[:8],
            "row_allocation_reasons": reasons[:8],
            "visual_fill_feedback": visual_fill_feedback,
            "canvas": {"w": cw, "h": ch},
            "hint": (
                "This is a row allocation/density failure, not a small local text overflow. Restore the fixed "
                "editorial grid and body rows: `.editorial-poster{height:100%;min-height:0;display:grid;"
                "grid-template-rows:<compact-header> minmax(0,1fr);}`, `.poster-columns{min-height:0;"
                "align-self:stretch;align-items:stretch;height:100%;}`, and `.poster-section` rows that "
                "allocate real height to content. Do not shrink body font, source figures/tables, or global "
                "padding as the primary repair."
            ),
        },
    )


def _find_column_by_label(soup: BeautifulSoup, label: str) -> Tag | None:
    if not label:
        return None
    for column in soup.select(".poster-column,[data-column-id]"):
        if isinstance(column, Tag) and _column_label(column) == label:
            return column
    return None


def _local_flow_issue_for_container(
    container: Tag,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
    *,
    bottom_overflow: int,
    client_height_px: float | None = None,
    scroll_height_px: float | None = None,
) -> dict[str, Any]:
    container_bbox = _bbox_for_tag(container, bboxes) or {}
    container_bbox_only = _bbox_only(container_bbox) or {}
    container_bottom = int(container_bbox.get("y", 0) + container_bbox.get("h", 0))
    overflow_tag, overflow_bbox = _deepest_overflowing_descendant(container, bboxes, container_bottom)
    container_classes = _class_tokens(container)
    if "poster-section" in container_classes:
        section = container
    else:
        section = _nearest_ancestor_with_class(overflow_tag or container, {"poster-section"})
    flow_unit = (
        _nearest_ancestor_with_class(overflow_tag, {"figure-flow-unit", "source-flow-unit"})
        if overflow_tag else None
    )
    if flow_unit is None and container_classes & {"figure-flow-unit", "source-flow-unit"}:
        flow_unit = container
    column = _nearest_ancestor_with_class(overflow_tag or container, {"poster-column"})
    overflow_block_id = str((overflow_tag or container).get("data-block-id") or "")
    container_id = str(container.get("data-block-id") or _column_label(container))
    column_id = _column_label(column) if isinstance(column, Tag) else ""
    section_id = _section_label(section) if isinstance(section, Tag) else ""
    flow_unit_id = str(flow_unit.get("data-block-id") or "") if isinstance(flow_unit, Tag) else ""
    target_block_ids = _unique_nonempty([container_id, section_id, flow_unit_id, overflow_block_id])
    target_selectors = _selectors_for_ids(target_block_ids)
    same_column_siblings = _same_column_section_ids(column, exclude={section_id})
    allowed_selectors = _unique_nonempty([
        *target_selectors,
        _repair_selector_for_tag(column) if isinstance(column, Tag) else "",
        *(_selectors_for_ids(same_column_siblings[:6])),
    ])
    forbidden_selectors = [
        ".paper-poster",
        "body",
        ".poster-columns",
        ".poster-column:not(current-column)",
        ".poster-section:not(target-or-same-column-sibling)",
    ]
    preserve_selectors = [
        "[data-source-id]",
        "[data-layer-id]",
        ".figure-flow-unit",
        ".source-flow-unit",
    ]
    sample_text = _compact_sample_text(overflow_tag or container)
    content_bottom = (
        int(overflow_bbox.get("y", 0) + overflow_bbox.get("h", 0))
        if overflow_bbox else container_bottom + bottom_overflow
    )
    section_bbox = _bbox_for_tag(section, bboxes) if isinstance(section, Tag) else None
    section_bbox_only = _bbox_only(section_bbox) if isinstance(section_bbox, dict) else None
    container_kind = "generic"
    layout_region = str(container.get("data-layout-region") or "").strip().lower()
    if "editorial-poster" in container_classes:
        container_kind = "editorial_poster"
    elif "poster-columns" in container_classes or layout_region in {"editorial_columns", "main_panels"}:
        container_kind = "poster_columns"
    elif "poster-column" in container_classes:
        container_kind = "poster_column"
    elif "poster-section" in container_classes:
        container_kind = "poster_section"
    elif "figure-flow-unit" in container_classes:
        container_kind = "figure_flow_unit"
    elif "source-flow-unit" in container_classes:
        container_kind = "source_flow_unit"
    client_h = float(client_height_px or container_bbox.get("h") or 0)
    scroll_h = float(scroll_height_px or client_h or 0)
    return {
        "id": "local_flow_scroll_overflow",
        "container_kind": container_kind,
        "container_id": container_id,
        "column_id": column_id,
        "section_id": section_id,
        "flow_unit_id": flow_unit_id,
        "overflow_block_id": overflow_block_id,
        "client_height_px": round(client_h, 2),
        "scroll_height_px": round(scroll_h, 2),
        "bottom_overflow_px": bottom_overflow,
        "scroll_overflow_px": {"bottom": bottom_overflow},
        "container_bbox": container_bbox_only,
        "section_bbox": section_bbox_only or container_bbox_only,
        "container_bottom_px": container_bottom,
        "content_bottom_px": content_bottom,
        "canvas_bottom_px": int(canvas["h_px"]),
        "sample_text": sample_text[:180],
        "same_column_sibling_section_ids": same_column_siblings[:8],
        "repair_scope": {
            "mode": "local_section_flow",
            "target_block_ids": target_block_ids,
            "allowed_selectors": allowed_selectors,
            "forbidden_selectors": forbidden_selectors,
            "preserve_selectors": preserve_selectors,
            "allowed_operations": [
                "trim low-value prose inside target section/flow unit",
                "tighten spacing inside target section/flow unit",
                "rebalance row height with same-column sibling sections",
                "move optional secondary content within the same column",
            ],
            "forbidden_operations": [
                "rewrite whole poster structure",
                "delete non-target source figures or tables",
                "change fixed canvas/template semantics",
                "globally shrink body typography",
            ],
        },
        "target_block_ids": target_block_ids,
        "target_selectors": target_selectors,
        "allowed_selectors": allowed_selectors,
        "forbidden_selectors": forbidden_selectors,
        "preserve_selectors": preserve_selectors,
        "repair": (
            "Repair this local section/flow unit, not the whole poster: trim prose, rebalance "
            "this column's row heights, or move/drop only optional secondary flow content."
        ),
    }


def _deepest_overflowing_descendant(
    container: Tag,
    bboxes: dict[str, dict[str, int]],
    container_bottom: int,
) -> tuple[Tag | None, dict[str, int] | None]:
    best: tuple[int, int, Tag, dict[str, int]] | None = None
    for child in container.find_all(True):
        if not isinstance(child, Tag):
            continue
        child_id = str(child.get("data-block-id") or "").strip()
        child_bbox = _bbox_only(bboxes.get(child_id))
        if not child_id or not child_bbox:
            continue
        bottom = int(child_bbox["y"] + child_bbox["h"])
        if bottom <= container_bottom + 3:
            continue
        depth = len(list(child.parents))
        ranked = (bottom, depth, child, child_bbox)
        if best is None or ranked[:2] > best[:2]:
            best = ranked
    if best is None:
        return None, None
    return best[2], best[3]


def _unique_nonempty(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _css_attr_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _selector_for_block_id(block_id: str) -> str:
    block_id = str(block_id or "").strip()
    if not block_id:
        return ""
    return f'[data-block-id="{_css_attr_value(block_id)}"]'


def _selectors_for_ids(block_ids: list[str]) -> list[str]:
    return [selector for selector in (_selector_for_block_id(block_id) for block_id in block_ids) if selector]


def _repair_selector_for_tag(tag: Tag | None) -> str:
    if not isinstance(tag, Tag):
        return ""
    block_id = str(tag.get("data-block-id") or "").strip()
    if block_id:
        return _selector_for_block_id(block_id)
    column_id = str(tag.get("data-column-id") or "").strip()
    if column_id:
        return f'[data-column-id="{_css_attr_value(column_id)}"]'
    panel_role = str(tag.get("data-panel-role") or "").strip()
    if panel_role:
        return f'[data-panel-role="{_css_attr_value(panel_role)}"]'
    return ""


def _same_column_section_ids(column: Tag | None, *, exclude: set[str] | None = None) -> list[str]:
    if not isinstance(column, Tag):
        return []
    excluded = exclude or set()
    section_ids: list[str] = []
    for section in _direct_editorial_sections(column):
        section_id = _section_label(section)
        if section_id and section_id not in excluded:
            section_ids.append(section_id)
    return section_ids


def _compact_sample_text(tag: Tag | None) -> str:
    if not isinstance(tag, Tag):
        return ""
    return " ".join(tag.get_text(" ", strip=True).split())


def _nearest_ancestor_with_class(tag: Tag | None, classes: set[str]) -> Tag | None:
    current = tag
    while isinstance(current, Tag):
        if _class_tokens(current) & classes:
            return current
        current = current.parent if isinstance(current.parent, Tag) else None
    return None


def _editorial_flow_fill_error(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
    ctx: ToolContext,
) -> ToolResultRecord | None:
    if not _editorial_flow_mode(ctx):
        return None
    ch = int(canvas["h_px"])
    body_bottom = _editorial_body_target_bottom(soup, bboxes, canvas)
    column_gap_threshold = max(130, int(round(ch * 0.075)))
    issues: list[dict[str, Any]] = []
    columns_shell = soup.select_one(
        ".poster-columns,[data-layout-region='editorial_columns'],[data-layout-region='main_panels']"
    )
    columns_shell_bbox = _bbox_for_tag(columns_shell, bboxes) if isinstance(columns_shell, Tag) else None
    if columns_shell_bbox:
        shell_bottom = int(columns_shell_bbox["y"] + columns_shell_bbox["h"])
        shell_gap = body_bottom - shell_bottom
        if shell_gap > column_gap_threshold:
            issues.append({
                "id": "editorial_body_shell_underfilled",
                "columns_id": str(columns_shell.get("data-block-id") or columns_shell.get("data-layout-region") or "poster-columns"),
                "body_bottom_px": body_bottom,
                "columns_bottom_px": shell_bottom,
                "bottom_gap_px": shell_gap,
                "required_max_gap_px": column_gap_threshold,
                "repair": (
                    "The body columns shell is auto-height and stops before the fixed poster canvas "
                    "bottom. Keep the same canvas, but make the authored editorial shell consume the "
                    "available height: `.editorial-poster{height:100%;min-height:100%;display:grid;"
                    "grid-template-rows:<compact-header> minmax(0,1fr);}` plus `.poster-columns{min-height:0;"
                    "align-self:stretch;align-items:stretch;}` and `.poster-column{min-height:0;}`. "
                    "Then allocate the real `.poster-section` rows across each stretched column; do not "
                    "globally scale the whole poster down or leave a bottom spacer outside panels."
                ),
            })
    columns = [
        tag for tag in soup.find_all(True)
        if isinstance(tag, Tag) and _is_editorial_column_tag(tag)
    ]
    for column in columns:
        column_bbox = _bbox_for_tag(column, bboxes)
        if not column_bbox:
            continue
        sections = [
            (section, _bbox_for_tag(section, bboxes))
            for section in _direct_editorial_sections(column)
        ]
        valid_sections = [
            (section, bbox) for section, bbox in sections
            if bbox and int(bbox.get("h") or 0) > 8
        ]
        if not valid_sections:
            continue
        valid_sections.sort(key=lambda item: (int(item[1].get("y") or 0), int(item[1].get("x") or 0)))
        last_section, last_bbox = valid_sections[-1]
        last_bottom = int(last_bbox["y"] + last_bbox["h"])
        column_bottom = min(
            ch,
            max(body_bottom, int(column_bbox["y"] + column_bbox["h"])),
        )
        bottom_gap = column_bottom - last_bottom
        if bottom_gap > column_gap_threshold:
            issues.append({
                "id": "editorial_column_bottom_underfilled",
                "column_id": _column_label(column),
                "last_section": _section_label(last_section),
                "bottom_gap_px": bottom_gap,
                "required_max_gap_px": column_gap_threshold,
                "repair": (
                    "Use the column height with local content: enlarge existing source figures/tables "
                    "or add concise source-backed readout/native rows in the last section. Do not "
                    "globally compress the poster into a shallow top band."
                ),
            })
        for section, section_bbox in valid_sections:
            descendants = _editorial_section_content_bboxes(section, bboxes, canvas)
            if not descendants:
                continue
            block_bottoms = [
                int(bbox["y"] + bbox["h"])
                for bbox in descendants
                if str(bbox.get("_content_bbox_source") or "block_bbox") == "block_bbox"
            ]
            text_bottoms = [
                int(bbox["y"] + bbox["h"])
                for bbox in descendants
                if str(bbox.get("_content_bbox_source") or "") == "text_line_union"
            ]
            block_content_bottom = max(block_bottoms) if block_bottoms else None
            visible_text_bottom = max(text_bottoms) if text_bottoms else None
            content_bottom = max(
                [value for value in (block_content_bottom, visible_text_bottom) if isinstance(value, int)]
            )
            if block_content_bottom is not None and visible_text_bottom is not None:
                content_bottom_source = "mixed"
            elif visible_text_bottom is not None:
                content_bottom_source = "text_line_union"
            else:
                content_bottom_source = "block_bbox"
            section_bottom = int(section_bbox["y"] + section_bbox["h"])
            section_h = int(section_bbox["h"])
            section_gap = section_bottom - content_bottom
            line_h = _median_body_line_height_px(section, bboxes)
            section_threshold = max(40, int(round(line_h * 2.0)))
            if section_h >= 170 and section_gap > section_threshold:
                has_source = bool(section.find(attrs={"data-source-id": True}) or section.find(attrs={"data-layer-id": True}))
                tail_gap_confidence = "high" if visible_text_bottom is not None else "medium"
                repair = (
                    "The section reserves height that its flow content does not use. Fill the "
                    "tail by expanding the existing source/table flow, adding a source-backed "
                    "readout/native result row, or reducing the section height so lower sections "
                    "can use the space."
                )
                if not has_source:
                    repair = (
                        "This section is mostly text/native cards but owns too much column height. "
                        "Either merge it into the previous section, convert it into a source-backed "
                        "section with a real figure/table/native evidence unit, or reduce this row "
                        "and give the height to a visual source section. Do not leave a large blank "
                        "tail inside the section."
                    )
                issues.append({
                    "id": "editorial_section_tail_blank",
                    "section_id": _section_label(section),
                    "section_title": _section_heading_text(section),
                    "column_id": _column_label(column),
                    "tail_gap_px": section_gap,
                    "required_max_gap_px": section_threshold,
                    "section_bottom_px": section_bottom,
                    "content_bottom_px": content_bottom,
                    "content_bottom_source": content_bottom_source,
                    "visible_text_bottom_px": visible_text_bottom,
                    "block_content_bottom_px": block_content_bottom,
                    "tail_gap_confidence": tail_gap_confidence,
                    "has_source_asset": has_source,
                    "repair": repair,
                })
            internal_gap_issue = _editorial_section_internal_gap_issue(
                section,
                section_bbox,
                descendants,
                bboxes,
                canvas,
            )
            if internal_gap_issue:
                issues.append(internal_gap_issue)
    issues.extend(_editorial_source_flow_readout_issues(soup, bboxes, canvas, ctx))
    blank_fill_plan = _blank_fill_plan_from_editorial_issues(issues, soup, bboxes, canvas)
    if not issues:
        return None
    ctx.state["paper_poster_html_editorial_flow_fill"] = {
        "issues": issues[:12],
        "blank_fill_plan": blank_fill_plan,
    }
    log(
        "paper_poster_html.editorial_flow_fill_block",
        issue_count=len(issues),
        first_issues=issues[:4],
    )
    return obs_error(
        "propose_paper_poster_html found local fill problems in the editorial-flow poster.",
        category="validation",
        payload={
            "issue_id": "paper_poster_html_editorial_flow_fill_failed",
            "repair_route": "repair_local_section_and_source_flow_fill",
            "issues": issues[:12],
            "blank_fill_plan": blank_fill_plan,
            "required_blank_fill_targets": (blank_fill_plan.get("required_targets") or [])[:12] if blank_fill_plan else [],
            "advisory_blank_fill_targets": (blank_fill_plan.get("advisory_targets") or [])[:12] if blank_fill_plan else [],
            "blank_fill_required": bool(blank_fill_plan.get("required_targets") if blank_fill_plan else []),
            "hint": (
                "Keep the fixed canvas and current composition. Repair the specific column, "
                "section, or source-flow unit named in issues: shorten low-value text in earlier "
                "siblings if a lower section is squeezed; enlarge source figures/tables or add "
                "concise source-backed local readouts/native rows where a column or section is "
                "blank; and keep each image/table plus its text as one normal-flow DOM unit. "
                "Do not rewrite the whole poster, create empty placeholder boxes, or hide overflow."
            ),
        },
    )


def _blank_fill_plan_from_editorial_issues(
    issues: list[dict[str, Any]],
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    cw = max(1, _safe_int(canvas.get("w_px"), default=1))
    ch = max(1, _safe_int(canvas.get("h_px"), default=1))
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        target = _blank_fill_target_from_editorial_issue(issue, soup, bboxes, cw=cw, ch=ch)
        if not target:
            continue
        issue["blank_fill_target"] = target
        for key in (
            "target_kind",
            "insert_selector",
            "insert_position",
            "tail_gap_confidence",
            "content_bottom_source",
            "visible_text_bottom_px",
            "block_content_bottom_px",
            "required_co_repair_eligible",
            "promotion",
            "blank_fill_severity",
            "side_lane_bottom_px",
            "side_content_bottom_px",
            "lane_tail_gap_px",
            "blank_bbox_canvas",
            "visual_salience_score",
            "side_text_coverage_ratio",
            "required_min_side_text_coverage_ratio",
            "coverage_gap",
            "local_word_count",
            "required_min_words",
            "words_to_add_min",
            "words_to_add_max",
            "target_line_count",
            "remaining_safe_words",
            "safe_word_budget",
            "over_readout_budget",
            "allowed_filler_block_ids",
            "content_requirements",
            "primary_repair_action",
            "safe_primary_repair_action",
            "required_repair_mode",
            "prose_fill_required",
            "compact_rebalance_required",
            "allowed_selectors",
            "forbidden_selectors",
            "preserve_selectors",
            "visual_salience_level",
        ):
            if target.get(key) not in (None, "", [], {}):
                issue[key] = target.get(key)
        targets.append(target)
    if not targets:
        return {}
    required_targets = _required_blank_fill_targets({"targets": targets})
    required_keys = {_blank_fill_target_key(target) for target in required_targets if isinstance(target, dict)}
    _finalize_advisory_blank_fill_targets(targets, required_keys)
    advisory_targets = [
        target for target in sorted(targets, key=_blank_fill_target_salience_sort_key)
        if isinstance(target, dict) and _blank_fill_target_key(target) not in required_keys
    ]
    suppressed_targets = [
        target for target in advisory_targets
        if isinstance(target, dict)
        and (
            target.get("required_co_repair_eligible") is False
            or str(target.get("promotion") or target.get("blank_fill_severity") or "") == "advisory"
        )
    ]
    return {
        "version": 1,
        "normalization_version": 1,
        "blank_fill_required": bool(required_targets),
        "required_target_count": len(required_targets),
        "advisory_target_count": len(advisory_targets),
        "suppressed_target_count": len(suppressed_targets),
        "targets": targets[:12],
        "required_targets": required_targets[:12],
        "advisory_targets": advisory_targets[:12],
        "suppressed_targets": suppressed_targets[:12],
        "instructions": (
            "Repair each target locally. Add concise source-backed facts only when prose_fill_required is true; "
            "otherwise use compact native rows, readout rebalance, stacking, or local section-height reduction. "
            "Do not invent facts, resize the canvas, change global columns, or solve blank lanes by merely enlarging images."
        ),
    }


def _blank_fill_plan_from_source_visual_issues(
    issues: list[dict[str, Any]],
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    cw = max(1, _safe_int(canvas.get("w_px"), default=1))
    ch = max(1, _safe_int(canvas.get("h_px"), default=1))
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        target = _blank_fill_target_from_source_visual_issue(issue, soup, bboxes, cw=cw, ch=ch)
        if not target:
            continue
        issue["blank_fill_target"] = target
        if str(target.get("promotion") or target.get("blank_fill_severity") or "") == "required":
            issue["severity"] = "required"
            issue["soft_finalizable"] = False
            issue["blocks_soft_accept"] = True
            issue["blank_fill_required"] = True
        for key in (
            "target_kind",
            "target_scope",
            "insert_selector",
            "insert_position",
            "required_co_repair_eligible",
            "promotion",
            "blank_fill_severity",
            "blank_bbox_canvas",
            "visual_salience_score",
            "side_text_coverage_ratio",
            "required_min_side_text_coverage_ratio",
            "coverage_gap",
            "local_word_count",
            "required_min_words",
            "words_to_add_min",
            "words_to_add_max",
            "target_line_count",
            "remaining_safe_words",
            "safe_word_budget",
            "over_readout_budget",
            "allowed_filler_block_ids",
            "content_requirements",
            "safe_primary_repair_action",
            "required_repair_mode",
            "prose_fill_required",
            "compact_rebalance_required",
            "allowed_selectors",
            "forbidden_selectors",
            "preserve_selectors",
            "preserve_current_visual_size",
            "required_dom_shape",
            "visual_salience_level",
        ):
            if target.get(key) not in (None, "", [], {}):
                issue[key] = target.get(key)
        targets.append(target)
    if not targets:
        return {}
    required_targets = _required_blank_fill_targets({"targets": targets})
    required_keys = {_blank_fill_target_key(target) for target in required_targets if isinstance(target, dict)}
    _finalize_advisory_blank_fill_targets(targets, required_keys)
    advisory_targets = [
        target for target in sorted(targets, key=_blank_fill_target_salience_sort_key)
        if isinstance(target, dict) and _blank_fill_target_key(target) not in required_keys
    ]
    suppressed_targets = [
        target for target in advisory_targets
        if isinstance(target, dict)
        and (
            target.get("required_co_repair_eligible") is False
            or str(target.get("promotion") or target.get("blank_fill_severity") or "") == "advisory"
        )
    ]
    return {
        "version": 1,
        "normalization_version": 1,
        "blank_fill_required": bool(required_targets),
        "required_target_count": len(required_targets),
        "advisory_target_count": len(advisory_targets),
        "suppressed_target_count": len(suppressed_targets),
        "targets": targets[:12],
        "required_targets": required_targets[:12],
        "advisory_targets": advisory_targets[:12],
        "suppressed_targets": suppressed_targets[:12],
        "instructions": (
            "Repair source-side blankness locally. Create or fix the direct-child source-flow unit, "
            "preserve readable visual size, then fill the same unit with compact paper-backed readout, "
            "native rows, or local rebalance. Do not resize the canvas, change global columns, "
            "or solve the issue by merely enlarging the source image."
        ),
    }


def _blank_fill_target_from_source_visual_issue(
    issue: dict[str, Any],
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    *,
    cw: int,
    ch: int,
) -> dict[str, Any] | None:
    target_problem = str(issue.get("target_problem") or "")
    failure_kind = str(issue.get("failure_kind") or "")
    acceptance_mode = str(issue.get("acceptance_mode") or "")
    if (
        target_problem != "readable_visual_flow_underfilled"
        and failure_kind not in {"source_visual_flow_underfilled", "source_visual_sidecar_underfilled"}
        and acceptance_mode != "same_flow_fill"
    ):
        return None
    same_flow = issue.get("same_flow_fill_metrics") if isinstance(issue.get("same_flow_fill_metrics"), dict) else {}
    if same_flow.get("same_flow_fill_pass") is True:
        return None
    source_id = str(issue.get("source_id") or "").strip()
    panel_id = str(issue.get("panel_id") or "").strip()
    asset_block_id = str(issue.get("asset_block_id") or issue.get("block_id") or "").strip()
    flow_unit_id = str(issue.get("flow_unit_id") or same_flow.get("flow_unit_id") or "").strip()
    panel = soup.find(attrs={"data-block-id": panel_id}) if panel_id else None
    asset = soup.find(attrs={"data-block-id": asset_block_id}) if asset_block_id else None
    if not isinstance(asset, Tag) and source_id:
        asset = soup.find(attrs={"data-source-id": source_id}) or soup.find(attrs={"data-layer-id": source_id})
    if not isinstance(panel, Tag) and isinstance(asset, Tag):
        panel = _nearest_source_wrap_panel(asset)
        panel_id = str(panel.get("data-block-id") or panel.get("data-slot-id") or "") if isinstance(panel, Tag) else panel_id
    if not isinstance(panel, Tag):
        return None
    unit = soup.find(attrs={"data-block-id": flow_unit_id}) if flow_unit_id else None
    if not isinstance(unit, Tag) or not _is_source_flow_unit(unit):
        unit = None
        flow_unit_id = ""
    panel_bbox = _bbox_only(_bbox_for_tag(panel, bboxes))
    if not panel_bbox:
        return None
    asset_bbox = _bbox_only(_bbox_for_tag(asset, bboxes)) if isinstance(asset, Tag) else None
    if not asset_bbox and asset_block_id:
        asset_bbox = _bbox_for_visual_target(asset_block_id, bboxes)
    if not asset_bbox:
        return None
    unit_bbox = _bbox_only(_bbox_for_tag(unit, bboxes)) if isinstance(unit, Tag) else None
    fill_bbox = unit_bbox or panel_bbox
    blank_box = _source_flow_blank_side_bbox(fill_bbox, asset_bbox, cw=cw, ch=ch)
    if not blank_box:
        blank_box = _clip_bbox_to_canvas(fill_bbox, cw=cw, ch=ch)
    section = _nearest_ancestor_with_class(panel, {"poster-section"}) or panel
    column = _nearest_ancestor_with_class(panel, {"poster-column"})
    section_id = _section_label(section) if isinstance(section, Tag) else panel_id
    column_id = _column_label(column) if isinstance(column, Tag) else ""
    local_words = max(
        0,
        _safe_int(
            same_flow.get("local_word_count")
            if same_flow.get("local_word_count") not in (None, "", [], {})
            else issue.get("local_word_count"),
            default=0,
        ),
    )
    required_words = max(
        _safe_int(
            same_flow.get("required_min_words")
            if same_flow.get("required_min_words") not in (None, "", [], {})
            else issue.get("required_min_words"),
            default=0,
        ),
        _MIN_SOURCE_VISUAL_SIDE_TEXT_WORDS,
    )
    coverage = max(
        0.0,
        _safe_float(
            same_flow.get("side_text_coverage_ratio")
            if same_flow.get("side_text_coverage_ratio") not in (None, "", [], {})
            else issue.get("side_text_coverage_ratio"),
            default=0.0,
        ),
    )
    required_coverage = _safe_float(
        same_flow.get("required_min_side_text_coverage_ratio")
        if same_flow.get("required_min_side_text_coverage_ratio") not in (None, "", [], {})
        else issue.get("required_min_side_text_coverage_ratio"),
        default=_MIN_SOURCE_VISUAL_SIDE_TEXT_COVERAGE_RATIO,
    )
    coverage_gap = max(0.0, required_coverage - coverage)
    coverage_words = int(math.ceil(coverage_gap * max(local_words / max(coverage, 0.05), 80)))
    words_min = _clamp_int(max(required_words - local_words, coverage_words, 8), 8, 32)
    words_max = words_min + 12
    remaining_safe_words = max(0, _MAX_SOURCE_FLOW_EXPLANATION_WORDS - local_words - 8)
    over_readout_budget = remaining_safe_words < 8 or words_min > remaining_safe_words
    line_h = _median_body_line_height_px(unit or panel, bboxes)
    required_gap = max(90, int(round(line_h * 3.5)))
    visual_salience_score = _blank_fill_visual_salience_score(
        "source_flow_side_lane",
        blank_box,
        cw=cw,
        ch=ch,
        usable_blank_px=max(_safe_int(blank_box.get("h"), default=0), required_gap),
        over_readout_budget=over_readout_budget,
        required_eligible=True,
    )
    probe = {
        "target_kind": "source_flow_side_lane",
        "visual_salience_score": visual_salience_score,
        "blank_bbox_canvas": blank_box,
    }
    visually_obvious = _blank_fill_target_visually_obvious(probe, kind="source_flow_side_lane")
    compact_rebalance_required = over_readout_budget and visually_obvious
    blank_fill_required = visually_obvious
    target_scope = "existing_source_flow_unit" if flow_unit_id else "create_direct_child_source_flow_unit"
    readout = unit.select_one(".source-readout,.figure-readout,.local-readout,[data-role='source-readout']") if isinstance(unit, Tag) else None
    if flow_unit_id:
        insert_selector = f'{_selector_for_block_id(flow_unit_id)} .source-readout' if isinstance(readout, Tag) else _selector_for_block_id(flow_unit_id)
        insert_position = "append_child" if isinstance(readout, Tag) else "append_direct_child"
    else:
        insert_selector = _selector_for_block_id(panel_id)
        insert_position = "create_direct_child_source_flow_unit"
    allowed_filler_block_ids = (
        _source_visual_allowed_filler_block_ids(panel, asset)[:8]
        if isinstance(asset, Tag)
        else []
    )
    target_ids = _unique_nonempty([flow_unit_id, asset_block_id, panel_id, section_id, column_id, source_id])
    allowed_selectors = _selectors_for_ids([value for value in (flow_unit_id, panel_id, section_id) if value])
    if source_id:
        allowed_selectors.append(f'[data-source-id="{_css_attr_value(source_id)}"], [data-layer-id="{_css_attr_value(source_id)}"]')
    required_repair_mode = (
        "compact_rebalance_source_flow"
        if compact_rebalance_required else
        "create_direct_child_source_flow_unit_and_fill_blank_lane"
        if target_scope == "create_direct_child_source_flow_unit" and blank_fill_required else
        "prose_or_native_flow_fill"
        if blank_fill_required else
        "advisory"
    )
    return {
        "target_kind": "source_flow_side_lane",
        "target_scope": target_scope,
        "source_id": source_id,
        "flow_unit_id": flow_unit_id,
        "asset_block_id": asset_block_id,
        "section_id": section_id,
        "panel_id": panel_id,
        "column_id": column_id,
        "target_block_ids": target_ids,
        "insert_selector": insert_selector,
        "insert_position": insert_position,
        "allowed_selectors": allowed_selectors,
        "forbidden_selectors": [
            ".poster-columns",
            ".poster-column",
            ".poster-header",
            "[data-panel-role=\"identity_header\"]",
        ],
        "preserve_selectors": _selectors_for_ids([value for value in (flow_unit_id, asset_block_id, panel_id) if value]) + (
            [f'[data-source-id="{_css_attr_value(source_id)}"], [data-layer-id="{_css_attr_value(source_id)}"]'] if source_id else []
        ),
        "blank_bbox_canvas": blank_box,
        "visual_salience_score": visual_salience_score,
        "visual_salience_level": _blank_fill_visual_salience_level(visual_salience_score),
        "side_text_coverage_ratio": round(coverage, 3),
        "required_min_side_text_coverage_ratio": round(required_coverage, 3),
        "coverage_gap": round(coverage_gap, 3),
        "local_word_count": local_words,
        "required_min_words": required_words,
        "words_to_add_min": words_min,
        "words_to_add_max": words_max,
        "remaining_safe_words": remaining_safe_words,
        "safe_word_budget": remaining_safe_words,
        "over_readout_budget": over_readout_budget,
        "required_blank_fill_gap_px": required_gap,
        "blank_fill_severity": "required" if blank_fill_required else "advisory",
        "promotion": "required" if blank_fill_required else "advisory",
        "required_repair_mode": required_repair_mode,
        "required_repair_modes": (
            [
                "compact_existing_readout",
                "rebalance_native_rows",
                "stack_asset_and_readout",
                "reduce_flow_unit_or_section_height",
            ]
            if compact_rebalance_required else
            ["create_direct_child_source_flow_unit_and_fill_blank_lane", "append_direct_sibling_source_readout", "add_native_metric_rows"]
            if blank_fill_required else
            ["advisory"]
        ),
        "prose_fill_required": bool(blank_fill_required and not compact_rebalance_required),
        "compact_rebalance_required": bool(compact_rebalance_required),
        "target_line_count": _clamp_int(int(math.ceil(words_min / 8.0)), 1, 5),
        "allowed_filler_block_ids": allowed_filler_block_ids,
        "content_requirements": [
            "Use paper facts, benchmark numbers, mechanism notes, limitations, or takeaways.",
            "Keep the content as direct sibling readout/native rows inside the same source-flow unit.",
            "Do not use decorative filler or invented claims.",
        ],
        "primary_repair_action": (
            "compact_existing_readout_rebalance_native_rows_or_stack_asset_and_readout"
            if compact_rebalance_required else
            "create_direct_child_source_flow_unit_and_fill_blank_lane"
            if target_scope == "create_direct_child_source_flow_unit" else
            "append_direct_sibling_source_readout_or_move_allowed_filler_into_flow_unit"
        ),
        "safe_primary_repair_action": (
            "compact_existing_readout_rebalance_native_rows_or_reduce_flow_unit_height"
            if over_readout_budget else
            "create_direct_child_source_flow_unit_and_fill_blank_lane"
            if target_scope == "create_direct_child_source_flow_unit" else
            "append_direct_sibling_source_readout"
        ),
        "required_co_repair_eligible": blank_fill_required,
        "preserve_current_visual_size": True,
        "required_dom_shape": (
            "direct-child .figure-flow-unit/.source-flow-unit containing the source visual plus direct "
            "p/ul/table/div readout/native rows"
        ),
    }


def _blank_fill_target_from_editorial_issue(
    issue: dict[str, Any],
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    *,
    cw: int,
    ch: int,
) -> dict[str, Any] | None:
    issue_id = str(issue.get("id") or "")
    if issue_id in {
        "editorial_source_flow_local_readout_thin",
        "editorial_source_flow_wrap_side_underfilled",
        "editorial_source_flow_side_lane_tail_blank",
    }:
        return _blank_fill_source_flow_target(issue, soup, bboxes, cw=cw, ch=ch)
    if issue_id == "editorial_section_tail_blank":
        return _blank_fill_section_tail_target(issue, soup, bboxes, cw=cw, ch=ch)
    if issue_id == "editorial_section_internal_gap_blank":
        return _blank_fill_section_internal_gap_target(issue, soup, bboxes, cw=cw, ch=ch)
    if issue_id == "editorial_column_bottom_underfilled":
        return _blank_fill_column_bottom_target(issue, soup, bboxes, cw=cw, ch=ch)
    return None


def _blank_fill_source_flow_target(
    issue: dict[str, Any],
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    *,
    cw: int,
    ch: int,
) -> dict[str, Any] | None:
    flow_unit_id = str(issue.get("flow_unit_id") or "").strip()
    if not flow_unit_id:
        return None
    unit = soup.find(attrs={"data-block-id": flow_unit_id})
    if not isinstance(unit, Tag):
        return None
    unit_bbox = _bbox_for_tag(unit, bboxes)
    if not unit_bbox:
        return None
    asset_block_id = str(issue.get("asset_block_id") or "").strip()
    asset = soup.find(attrs={"data-block-id": asset_block_id}) if asset_block_id else None
    source_id = str(issue.get("source_id") or "").strip()
    if not isinstance(asset, Tag) and source_id:
        asset = unit.find(attrs={"data-source-id": source_id}) or unit.find(attrs={"data-layer-id": source_id})
    asset_bbox = _bbox_for_tag(asset, bboxes) if isinstance(asset, Tag) else None
    if not asset_bbox and asset_block_id:
        asset_bbox = _bbox_for_visual_target(asset_block_id, bboxes)
    if not asset_bbox:
        return None
    blank_box = _source_flow_blank_side_bbox(unit_bbox, asset_bbox, cw=cw, ch=ch)
    if not blank_box:
        blank_box = _clip_bbox_to_canvas(unit_bbox, cw=cw, ch=ch)
    section = _nearest_ancestor_with_class(unit, {"poster-section"})
    column = _nearest_ancestor_with_class(unit, {"poster-column"})
    section_id = _section_label(section) if isinstance(section, Tag) else str(issue.get("section_id") or "")
    column_id = _column_label(column) if isinstance(column, Tag) else str(issue.get("column_id") or "")
    local_words = max(0, _safe_int(issue.get("local_word_count"), default=0))
    required_words = max(
        _safe_int(issue.get("required_min_words"), default=0),
        _MIN_SOURCE_VISUAL_SIDE_TEXT_WORDS,
    )
    coverage = max(0.0, _safe_float(issue.get("side_text_coverage_ratio"), default=0.0))
    required_coverage = _safe_float(
        issue.get("required_min_side_text_coverage_ratio"),
        default=_MIN_SOURCE_VISUAL_SIDE_TEXT_COVERAGE_RATIO,
    )
    coverage_gap = max(0.0, required_coverage - coverage)
    coverage_words = int(math.ceil(coverage_gap * max(local_words / max(coverage, 0.05), 80)))
    lane_tail_words = _safe_int(issue.get("words_to_add_min"), default=0)
    words_min = _clamp_int(max(required_words - local_words, coverage_words, lane_tail_words, 8), 8, 32)
    words_max = words_min + 12
    remaining_safe_words = max(0, _MAX_SOURCE_FLOW_EXPLANATION_WORDS - local_words - 8)
    over_readout_budget = remaining_safe_words < 8 or words_min > remaining_safe_words
    line_h = _median_body_line_height_px(unit, bboxes)
    lane_tail_gap = _safe_int(issue.get("lane_tail_gap_px"), default=0)
    required_tail_gap = max(90, int(round(line_h * 3.5)))
    near_required_tail_gap = max(1, int(math.floor(required_tail_gap * 0.85)))
    target_kind = (
        "source_flow_side_lane_tail"
        if str(issue.get("id") or "") == "editorial_source_flow_side_lane_tail_blank"
        else "source_flow_side_lane"
    )
    tail_gap_is_required_size = target_kind != "source_flow_side_lane_tail" or lane_tail_gap >= near_required_tail_gap
    visual_salience_score = _blank_fill_visual_salience_score(
        target_kind,
        blank_box,
        cw=cw,
        ch=ch,
        usable_blank_px=lane_tail_gap,
        over_readout_budget=over_readout_budget,
        required_eligible=tail_gap_is_required_size,
    )
    visual_target_probe = {
        "target_kind": target_kind,
        "lane_tail_gap_px": lane_tail_gap,
        "required_blank_fill_gap_px": required_tail_gap,
        "visual_salience_score": visual_salience_score,
    }
    visually_obvious = _blank_fill_target_visually_obvious(visual_target_probe, kind=target_kind)
    compact_rebalance_required = (
        over_readout_budget
        and tail_gap_is_required_size
        and visually_obvious
    )
    blank_fill_required = tail_gap_is_required_size and visually_obvious and (not over_readout_budget or compact_rebalance_required)
    required_repair_mode = (
        "compact_rebalance_source_flow"
        if compact_rebalance_required else
        "prose_or_native_flow_fill"
        if blank_fill_required else
        "advisory"
    )
    readout = unit.select_one(".source-readout,.figure-readout,.local-readout,[data-role='source-readout']")
    insert_selector = f'{_selector_for_block_id(flow_unit_id)} .source-readout' if isinstance(readout, Tag) else _selector_for_block_id(flow_unit_id)
    insert_position = "append_child" if isinstance(readout, Tag) else "append_direct_child"
    allowed_filler_block_ids: list[str] = []
    if isinstance(section, Tag) and isinstance(asset, Tag):
        allowed_filler_block_ids = [
            block_id
            for block_id in _source_visual_allowed_filler_block_ids(section, asset)
            if block_id not in {flow_unit_id, asset_block_id}
        ][:8]
    target_ids = _unique_nonempty([flow_unit_id, asset_block_id, section_id, column_id, source_id])
    allowed_selectors = _selectors_for_ids([value for value in (flow_unit_id, section_id) if value])
    if source_id:
        allowed_selectors.append(f'[data-source-id="{_css_attr_value(source_id)}"], [data-layer-id="{_css_attr_value(source_id)}"]')
    return {
        "target_kind": target_kind,
        "source_id": source_id,
        "flow_unit_id": flow_unit_id,
        "asset_block_id": asset_block_id,
        "section_id": section_id,
        "column_id": column_id,
        "target_block_ids": target_ids,
        "insert_selector": insert_selector,
        "insert_position": insert_position,
        "allowed_selectors": allowed_selectors,
        "forbidden_selectors": [
            ".poster-columns",
            ".poster-column",
            ".poster-header",
            "[data-panel-role=\"identity_header\"]",
        ],
        "preserve_selectors": _selectors_for_ids([value for value in (flow_unit_id, asset_block_id) if value]) + (
            [f'[data-source-id="{_css_attr_value(source_id)}"], [data-layer-id="{_css_attr_value(source_id)}"]'] if source_id else []
        ),
        "blank_bbox_canvas": blank_box,
        "visual_salience_score": visual_salience_score,
        "visual_salience_level": _blank_fill_visual_salience_level(visual_salience_score),
        "side_text_coverage_ratio": round(coverage, 3),
        "required_min_side_text_coverage_ratio": round(required_coverage, 3),
        "coverage_gap": round(coverage_gap, 3),
        "side_lane_bottom_px": issue.get("side_lane_bottom_px"),
        "side_content_bottom_px": issue.get("side_content_bottom_px"),
        "lane_tail_gap_px": issue.get("lane_tail_gap_px"),
        "local_word_count": local_words,
        "required_min_words": required_words,
        "words_to_add_min": words_min,
        "words_to_add_max": words_max,
        "remaining_safe_words": remaining_safe_words,
        "safe_word_budget": remaining_safe_words,
        "over_readout_budget": over_readout_budget,
        "required_blank_fill_gap_px": required_tail_gap if target_kind == "source_flow_side_lane_tail" else None,
        "blank_fill_severity": "required" if blank_fill_required else "advisory",
        "required_repair_mode": required_repair_mode,
        "required_repair_modes": (
            [
                "compact_existing_readout",
                "rebalance_native_rows",
                "stack_asset_and_readout",
                "reduce_flow_unit_or_section_height",
            ]
            if compact_rebalance_required else
            ["append_direct_sibling_source_readout", "add_native_metric_rows"]
            if blank_fill_required else
            ["advisory"]
        ),
        "prose_fill_required": bool(blank_fill_required and not compact_rebalance_required),
        "compact_rebalance_required": bool(compact_rebalance_required),
        "target_line_count": _clamp_int(int(math.ceil(words_min / 8.0)), 1, 5),
        "allowed_filler_block_ids": allowed_filler_block_ids,
        "content_requirements": [
            "Use paper facts, benchmark numbers, mechanism notes, limitations, or takeaways.",
            "Keep the content as direct sibling readout/native rows inside the same source-flow unit.",
            "Do not use decorative filler or invented claims.",
        ],
        "primary_repair_action": (
            "compact_existing_readout_rebalance_native_rows_or_stack_asset_and_readout"
            if compact_rebalance_required else
            "append_direct_sibling_source_readout"
            if not allowed_filler_block_ids else
            "append_direct_sibling_source_readout_or_move_allowed_filler_into_flow_unit"
        ),
        "safe_primary_repair_action": (
            "compact_existing_readout_rebalance_native_rows_or_reduce_flow_unit_height"
            if over_readout_budget else
            "append_direct_sibling_source_readout"
        ),
        "required_co_repair_eligible": blank_fill_required,
        "promotion": "required" if blank_fill_required else "advisory",
        "preserve_current_visual_size": True,
        "required_dom_shape": (
            "direct-child .figure-flow-unit/.source-flow-unit containing source visual plus direct "
            "p/ul/table/div readout/native rows"
        ),
    }


def _source_flow_blank_side_bbox(
    unit_bbox: dict[str, int],
    asset_bbox: dict[str, int],
    *,
    cw: int,
    ch: int,
) -> dict[str, int] | None:
    unit_x = int(unit_bbox.get("x") or 0)
    unit_y = int(unit_bbox.get("y") or 0)
    unit_w = int(unit_bbox.get("w") or 0)
    unit_h = int(unit_bbox.get("h") or 0)
    asset_x = int(asset_bbox.get("x") or 0)
    asset_y = int(asset_bbox.get("y") or 0)
    asset_w = int(asset_bbox.get("w") or 0)
    asset_h = int(asset_bbox.get("h") or 0)
    if unit_w <= 0 or unit_h <= 0 or asset_w <= 0 or asset_h <= 0:
        return None
    unit_center = unit_x + unit_w / 2.0
    asset_center = asset_x + asset_w / 2.0
    if asset_center >= unit_center:
        raw = {"x": unit_x, "y": asset_y, "w": max(0, asset_x - unit_x), "h": asset_h}
    else:
        raw = {
            "x": asset_x + asset_w,
            "y": asset_y,
            "w": max(0, unit_x + unit_w - (asset_x + asset_w)),
            "h": asset_h,
        }
    clipped = _clip_bbox_to_canvas(raw, cw=cw, ch=ch)
    if clipped and clipped["w"] * clipped["h"] >= 80:
        return clipped
    return None


def _blank_fill_section_tail_target(
    issue: dict[str, Any],
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    *,
    cw: int,
    ch: int,
) -> dict[str, Any] | None:
    section_id = str(issue.get("section_id") or "").strip()
    if not section_id:
        return None
    section = soup.find(attrs={"data-block-id": section_id})
    if not isinstance(section, Tag):
        return None
    section_bbox = _bbox_for_tag(section, bboxes)
    if not section_bbox:
        return None
    content_bottom = _safe_int(issue.get("content_bottom_px"), default=int(section_bbox["y"] + section_bbox["h"]))
    section_bottom = _safe_int(issue.get("section_bottom_px"), default=int(section_bbox["y"] + section_bbox["h"]))
    tail_gap = max(0, _safe_int(issue.get("tail_gap_px"), default=section_bottom - content_bottom))
    allowed_gap = max(0, _safe_int(issue.get("required_max_gap_px"), default=0))
    usable_blank = max(0, tail_gap - allowed_gap)
    if usable_blank <= 0:
        return None
    line_h = _median_body_line_height_px(section, bboxes)
    visible_gap_floor = max(40, int(round(line_h * 2.0)))
    if tail_gap < visible_gap_floor:
        return None
    line_count = _clamp_int(int(math.floor(usable_blank / max(line_h, 12))), 1, 5)
    words_min = _clamp_int(line_count * 8, 10, 32)
    blank_box = _clip_bbox_to_canvas({
        "x": int(section_bbox["x"]),
        "y": content_bottom,
        "w": int(section_bbox["w"]),
        "h": max(1, section_bottom - content_bottom),
    }, cw=cw, ch=ch)
    column = _nearest_ancestor_with_class(section, {"poster-column"})
    title_text = str(issue.get("section_title") or _section_heading_text(section) or "")
    low_value_tail = bool(re.search(
        r"\b(?:references?|code|limitations?|limits?|takeaways?|caveats?|conclusion|implications?)\b",
        title_text,
        flags=re.I,
    ))
    confidence = str(issue.get("tail_gap_confidence") or "medium")
    required_blank_gap = max(60, int(round(line_h * 3.0)))
    extreme_low_value_gap = max(140, int(round(line_h * 5.0)))
    geometry_required_candidate = confidence == "high" and tail_gap >= required_blank_gap
    visual_salience_score = _blank_fill_visual_salience_score(
        "section_tail_blank",
        blank_box,
        cw=cw,
        ch=ch,
        usable_blank_px=usable_blank,
        required_eligible=geometry_required_candidate,
    )
    visual_target_probe = {
        "target_kind": "section_tail_blank",
        "tail_gap_confidence": confidence,
        "tail_gap_px": tail_gap,
        "required_blank_fill_gap_px": required_blank_gap,
        "visual_salience_score": visual_salience_score,
    }
    visually_obvious = _blank_fill_target_visually_obvious(visual_target_probe, kind="section_tail_blank")
    required_eligible = bool(
        visually_obvious
        and (not low_value_tail or usable_blank >= extreme_low_value_gap or visual_salience_score >= 0.72)
    )
    remaining_safe_words = 24
    over_readout_budget = words_min > remaining_safe_words
    compact_rebalance_required = required_eligible and over_readout_budget
    primary_action = "add_native_metric_rows"
    insert_position = "append_child"
    required_repair_mode = "add_native_metric_rows"
    if compact_rebalance_required:
        primary_action = "rebalance_section_height_add_native_metric_rows_or_reduce_section_height"
        required_repair_mode = "compact_rebalance_section_tail"
    if not required_eligible and low_value_tail:
        primary_action = "redistribute_row_height_to_source_section"
        required_repair_mode = "advisory_redistribute_row_height"
        insert_position = "none"
    insert_host = section.select_one(".section-body,.panel-body,.content")
    insert_selector = (
        f'{_selector_for_block_id(section_id)} .section-body'
        if isinstance(insert_host, Tag) and "section-body" in _class_tokens(insert_host)
        else _selector_for_block_id(section_id)
    )
    return {
        "target_kind": "section_tail_blank",
        "section_id": section_id,
        "column_id": _column_label(column) if isinstance(column, Tag) else str(issue.get("column_id") or ""),
        "target_block_ids": _unique_nonempty([section_id, str(issue.get("column_id") or "")]),
        "insert_selector": insert_selector,
        "insert_position": insert_position,
        "allowed_selectors": _selectors_for_ids([section_id]),
        "forbidden_selectors": [".poster-columns", ".poster-header", "[data-panel-role=\"identity_header\"]"],
        "preserve_selectors": _selectors_for_ids([section_id]),
        "blank_bbox_canvas": blank_box,
        "visual_salience_score": visual_salience_score,
        "visual_salience_level": _blank_fill_visual_salience_level(visual_salience_score),
        "tail_gap_px": tail_gap,
        "required_max_gap_px": allowed_gap,
        "usable_blank_px": usable_blank,
        "required_blank_fill_gap_px": required_blank_gap,
        "blank_fill_severity": "required" if required_eligible else "advisory",
        "required_repair_mode": required_repair_mode if required_eligible else "advisory",
        "required_repair_modes": (
            [
                "add_native_metric_rows",
                "rebalance_section_height",
                "reduce_section_height_to_visual_section",
            ]
            if required_eligible and compact_rebalance_required else
            ["add_native_metric_rows"]
            if required_eligible else
            ["advisory"]
        ),
        "prose_fill_required": bool(required_eligible and not compact_rebalance_required and words_min <= 24),
        "compact_rebalance_required": bool(required_eligible and compact_rebalance_required),
        "tail_gap_confidence": confidence,
        "content_bottom_source": issue.get("content_bottom_source"),
        "visible_text_bottom_px": issue.get("visible_text_bottom_px"),
        "block_content_bottom_px": issue.get("block_content_bottom_px"),
        "words_to_add_min": words_min,
        "words_to_add_max": words_min + 12,
        "remaining_safe_words": remaining_safe_words,
        "safe_word_budget": remaining_safe_words,
        "over_readout_budget": over_readout_budget,
        "target_line_count": line_count,
        "allowed_filler_block_ids": [],
        "content_requirements": [
            "Use concise source-backed section details, benchmark facts, mechanism notes, or limitation/takeaway bullets.",
            "Prefer native rows/readout over decorative filler.",
        ],
        "primary_repair_action": primary_action,
        "safe_primary_repair_action": primary_action,
        "required_co_repair_eligible": required_eligible,
        "promotion": "required" if required_eligible else "advisory",
    }


def _blank_fill_section_internal_gap_target(
    issue: dict[str, Any],
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    *,
    cw: int,
    ch: int,
) -> dict[str, Any] | None:
    section_id = str(issue.get("section_id") or "").strip()
    if not section_id:
        return None
    section = soup.find(attrs={"data-block-id": section_id})
    if not isinstance(section, Tag):
        return None
    section_bbox = _bbox_for_tag(section, bboxes)
    if not section_bbox:
        return None
    gap = max(0, _safe_int(issue.get("internal_gap_px"), default=0))
    allowed_gap = max(0, _safe_int(issue.get("required_max_gap_px"), default=0))
    usable_blank = max(0, gap - allowed_gap)
    if usable_blank <= 0:
        return None
    line_h = _median_body_line_height_px(section, bboxes)
    line_count = _clamp_int(int(math.floor(usable_blank / max(line_h, 12))), 1, 5)
    words_min = _clamp_int(line_count * 8, 10, 32)
    blank_box = issue.get("blank_bbox_canvas") if isinstance(issue.get("blank_bbox_canvas"), dict) else None
    if not blank_box:
        blank_box = {
            "x": int(section_bbox["x"]),
            "y": _safe_int(issue.get("upper_content_bottom_px"), default=int(section_bbox["y"])),
            "w": int(section_bbox["w"]),
            "h": gap,
        }
    blank_box = _clip_bbox_to_canvas(blank_box, cw=cw, ch=ch)
    if not blank_box:
        return None
    column = _nearest_ancestor_with_class(section, {"poster-column"})
    confidence = str(issue.get("tail_gap_confidence") or "high")
    required_blank_gap = max(84, int(round(line_h * 4.0)))
    required_eligible = confidence == "high" and gap >= required_blank_gap
    visual_salience_score = _blank_fill_visual_salience_score(
        "section_internal_gap_blank",
        blank_box,
        cw=cw,
        ch=ch,
        usable_blank_px=usable_blank,
        required_eligible=required_eligible,
    )
    visual_probe = {
        "target_kind": "section_internal_gap_blank",
        "tail_gap_confidence": confidence,
        "internal_gap_px": gap,
        "required_blank_fill_gap_px": required_blank_gap,
        "visual_salience_score": visual_salience_score,
    }
    visually_obvious = _blank_fill_target_visually_obvious(visual_probe, kind="section_internal_gap_blank")
    required_eligible = bool(required_eligible and visually_obvious)
    remaining_safe_words = 24
    over_readout_budget = words_min > remaining_safe_words
    compact_rebalance_required = required_eligible and over_readout_budget
    lower_block_id = str(issue.get("lower_block_id") or "").strip()
    insert_host = section.select_one(".section-body,.panel-body,.content")
    insert_selector = (
        f'{_selector_for_block_id(section_id)} .section-body'
        if isinstance(insert_host, Tag) and "section-body" in _class_tokens(insert_host)
        else _selector_for_block_id(section_id)
    )
    primary_action = "add_native_metric_rows"
    required_repair_mode = "add_native_metric_rows"
    if compact_rebalance_required:
        primary_action = "rebalance_internal_gap_add_native_rows_or_reduce_section_height"
        required_repair_mode = "compact_rebalance_section_internal_gap"
    return {
        "target_kind": "section_internal_gap_blank",
        "section_id": section_id,
        "column_id": _column_label(column) if isinstance(column, Tag) else str(issue.get("column_id") or ""),
        "target_block_ids": _unique_nonempty([section_id, str(issue.get("column_id") or ""), lower_block_id]),
        "insert_selector": insert_selector,
        "insert_position": "insert_before_lower_note" if lower_block_id else "append_child",
        "allowed_selectors": _selectors_for_ids([section_id]),
        "forbidden_selectors": [".poster-columns", ".poster-header", "[data-panel-role=\"identity_header\"]"],
        "preserve_selectors": _selectors_for_ids([value for value in (section_id, lower_block_id) if value]),
        "blank_bbox_canvas": blank_box,
        "visual_salience_score": visual_salience_score,
        "visual_salience_level": _blank_fill_visual_salience_level(visual_salience_score),
        "internal_gap_px": gap,
        "tail_gap_px": gap,
        "required_max_gap_px": allowed_gap,
        "usable_blank_px": usable_blank,
        "required_blank_fill_gap_px": required_blank_gap,
        "blank_fill_severity": "required" if required_eligible else "advisory",
        "required_repair_mode": required_repair_mode if required_eligible else "advisory",
        "required_repair_modes": (
            [
                "add_native_metric_rows",
                "rebalance_section_height",
                "reduce_section_height_to_visual_section",
            ]
            if required_eligible else
            ["advisory"]
        ),
        "prose_fill_required": bool(required_eligible and not compact_rebalance_required and words_min <= 24),
        "compact_rebalance_required": bool(compact_rebalance_required),
        "tail_gap_confidence": confidence,
        "content_bottom_source": issue.get("content_bottom_source"),
        "upper_content_bottom_px": issue.get("upper_content_bottom_px"),
        "lower_content_top_px": issue.get("lower_content_top_px"),
        "lower_content_bottom_px": issue.get("lower_content_bottom_px"),
        "lower_block_id": lower_block_id,
        "words_to_add_min": words_min,
        "words_to_add_max": words_min + 12,
        "remaining_safe_words": remaining_safe_words,
        "safe_word_budget": remaining_safe_words,
        "over_readout_budget": over_readout_budget,
        "target_line_count": line_count,
        "allowed_filler_block_ids": [],
        "content_requirements": [
            "Use concise visual interpretation, compact comparison table rows, method notes, ablation or limitation notes, source-grounded bullets, or a takeaway sentence.",
            "Fill the internal section gap before the lower note/footer; do not append a long paragraph after it.",
        ],
        "primary_repair_action": primary_action,
        "safe_primary_repair_action": primary_action,
        "required_co_repair_eligible": required_eligible,
        "promotion": "required" if required_eligible else "advisory",
    }


def _blank_fill_column_bottom_target(
    issue: dict[str, Any],
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    *,
    cw: int,
    ch: int,
) -> dict[str, Any] | None:
    last_section_id = str(issue.get("last_section") or "").strip()
    if not last_section_id:
        return None
    section = soup.find(attrs={"data-block-id": last_section_id})
    if not isinstance(section, Tag):
        return None
    section_bbox = _bbox_for_tag(section, bboxes)
    if not section_bbox:
        return None
    column_id = str(issue.get("column_id") or "").strip()
    column = soup.find(attrs={"data-column-id": column_id}) if column_id else None
    column_bbox = _bbox_for_tag(column, bboxes) if isinstance(column, Tag) else None
    bottom_gap = max(0, _safe_int(issue.get("bottom_gap_px"), default=0))
    allowed_gap = max(0, _safe_int(issue.get("required_max_gap_px"), default=0))
    usable_blank = max(0, bottom_gap - allowed_gap)
    if usable_blank <= 0:
        return None
    preferred_section = _preferred_column_blank_section(section, bboxes, cw=cw, ch=ch)
    if isinstance(preferred_section, Tag) and preferred_section is not section:
        preferred_id = str(preferred_section.get("data-block-id") or "").strip()
        preferred_bbox = _bbox_for_tag(preferred_section, bboxes)
        if preferred_id and preferred_bbox:
            section = preferred_section
            section_bbox = preferred_bbox
            last_section_id = preferred_id
    line_h = _median_body_line_height_px(section, bboxes)
    line_count = _clamp_int(int(math.floor(usable_blank / max(line_h, 12))), 1, 5)
    words_min = _clamp_int(line_count * 8, 10, 32)
    section_bottom = int(section_bbox["y"] + section_bbox["h"])
    raw_h = bottom_gap
    if column_bbox:
        raw_h = max(raw_h, int(column_bbox["y"] + column_bbox["h"]) - section_bottom)
    blank_box = _clip_bbox_to_canvas({
        "x": int(section_bbox["x"]),
        "y": section_bottom,
        "w": int(section_bbox["w"]),
        "h": max(1, raw_h),
    }, cw=cw, ch=ch)
    visual_salience_score = _blank_fill_visual_salience_score(
        "column_bottom_blank",
        blank_box,
        cw=cw,
        ch=ch,
        usable_blank_px=usable_blank,
        required_eligible=False,
    )
    return {
        "target_kind": "column_bottom_blank",
        "section_id": last_section_id,
        "column_id": column_id,
        "target_block_ids": _unique_nonempty([last_section_id, column_id]),
        "insert_selector": _selector_for_block_id(last_section_id),
        "insert_position": "append_child",
        "allowed_selectors": _selectors_for_ids([last_section_id]),
        "forbidden_selectors": [".poster-columns", ".poster-header", "[data-panel-role=\"identity_header\"]"],
        "preserve_selectors": _selectors_for_ids([last_section_id]),
        "blank_bbox_canvas": blank_box,
        "visual_salience_score": visual_salience_score,
        "visual_salience_level": _blank_fill_visual_salience_level(visual_salience_score),
        "tail_gap_px": bottom_gap,
        "required_max_gap_px": allowed_gap,
        "usable_blank_px": usable_blank,
        "words_to_add_min": words_min,
        "words_to_add_max": words_min + 12,
        "remaining_safe_words": 0,
        "safe_word_budget": 0,
        "over_readout_budget": False,
        "target_line_count": line_count,
        "allowed_filler_block_ids": [],
        "content_requirements": [
            "Use concise source-backed rows/readout in the last real section before changing column geometry.",
        ],
        "primary_repair_action": "redistribute_row_height_to_source_section",
        "safe_primary_repair_action": "redistribute_row_height_to_source_section",
        "required_repair_mode": "advisory",
        "prose_fill_required": False,
        "compact_rebalance_required": False,
        "blank_fill_severity": "advisory",
        "required_co_repair_eligible": False,
        "promotion": "advisory",
    }


def _preferred_column_blank_section(
    fallback_section: Tag,
    bboxes: dict[str, dict[str, int]],
    *,
    cw: int,
    ch: int,
) -> Tag | None:
    column = _nearest_ancestor_with_class(fallback_section, {"poster-column"})
    if not isinstance(column, Tag):
        return fallback_section
    best: tuple[int, Tag] | None = None
    canvas = {"w_px": cw, "h_px": ch}
    for section in _direct_editorial_sections(column):
        section_id = str(section.get("data-block-id") or "").strip()
        section_bbox = _bbox_for_tag(section, bboxes)
        if not section_id or not section_bbox:
            continue
        title_text = _section_heading_text(section)
        if re.search(r"\b(?:references?|code|takeaways?|caveats?|conclusion)\b", title_text, flags=re.I):
            continue
        content_boxes = _editorial_section_content_bboxes(section, bboxes, canvas)
        if not content_boxes:
            continue
        content_bottom = max(int(box["y"] + box["h"]) for box in content_boxes)
        gap = int(section_bbox["y"] + section_bbox["h"]) - content_bottom
        has_source = bool(section.find(attrs={"data-source-id": True}) or section.find(attrs={"data-layer-id": True}))
        score = gap + (80 if has_source else 0)
        if gap <= max(40, int(round(_median_body_line_height_px(section, bboxes) * 2.0))):
            continue
        if best is None or score > best[0]:
            best = (score, section)
    return best[1] if best else fallback_section


def _median_body_line_height_px(section: Tag, bboxes: dict[str, dict[str, int]]) -> int:
    values: list[float] = []
    for tag in section.find_all(["p", "li", "td", "th", "div"]):
        if not isinstance(tag, Tag):
            continue
        block_id = str(tag.get("data-block-id") or "").strip()
        if not block_id:
            continue
        measured = bboxes.get(block_id)
        if not isinstance(measured, dict):
            continue
        style = measured.get("_computed_style") if isinstance(measured.get("_computed_style"), dict) else {}
        line_height = _safe_float(style.get("lineHeight"), default=0.0)
        if line_height > 0:
            values.append(line_height)
            continue
        font_size = _safe_float(style.get("fontSize"), default=0.0)
        if font_size > 0:
            values.append(font_size * 1.18)
    if not values:
        return 18
    values.sort()
    return max(12, int(round(values[len(values) // 2])))


def _clamp_int(value: int | float, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(round(value))))


def _post_overflow_density_conservation_error(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
    ctx: ToolContext,
) -> ToolResultRecord | None:
    if not _editorial_flow_mode(ctx):
        return None
    baseline = ctx.state.get("paper_poster_html_locked_base_candidate")
    if not isinstance(baseline, dict):
        return None
    baseline_stage = str(baseline.get("stage") or "")
    if baseline_stage not in {
        "local_flow_overflow",
        "row_allocation_density",
        "heading_flow_overflow",
        "designer_owned_canvas_shell",
    }:
        return None
    baseline_canvas = baseline.get("canvas") if isinstance(baseline.get("canvas"), dict) else {}
    if not _same_canvas_size(canvas, baseline_canvas):
        return None
    baseline_measurement = _read_candidate_measurement(baseline)
    baseline_bboxes = baseline_measurement.get("bboxes") if isinstance(baseline_measurement, dict) else {}
    if not isinstance(baseline_bboxes, dict) or not baseline_bboxes:
        return None
    baseline_html = _read_candidate_text(baseline, "body_html")
    if not baseline_html.strip():
        return None
    baseline_soup = BeautifulSoup(baseline_html, "html.parser")
    baseline_inventory = _editorial_density_inventory(baseline_soup)
    current_inventory = _editorial_density_inventory(soup)
    if baseline_inventory.get("source_count", 0) < 1 and baseline_inventory.get("section_count", 0) < 3:
        return None

    lost_source_ids = sorted(
        set(baseline_inventory.get("source_ids") or []) - set(current_inventory.get("source_ids") or [])
    )
    lost_layer_ids = sorted(
        set(baseline_inventory.get("layer_ids") or []) - set(current_inventory.get("layer_ids") or [])
    )
    source_flow_drop = max(0, int(baseline_inventory.get("source_flow_unit_count", 0)) - int(current_inventory.get("source_flow_unit_count", 0)))
    section_drop = max(0, int(baseline_inventory.get("section_count", 0)) - int(current_inventory.get("section_count", 0)))
    baseline_words = int(baseline_inventory.get("word_count", 0))
    current_words = int(current_inventory.get("word_count", 0))
    word_loss_ratio = 0.0
    if baseline_words > 0:
        word_loss_ratio = round((baseline_words - current_words) / max(1, baseline_words), 3)

    current_fill_issues = _canvas_fill_issues(soup, bboxes, canvas)
    baseline_fill_issues = _canvas_fill_issues(baseline_soup, baseline_bboxes, baseline_canvas)
    current_fill_metrics = _fill_metrics_from_issues(current_fill_issues)
    baseline_fill_metrics = _fill_metrics_from_issues(baseline_fill_issues)
    current_editorial_fill = _editorial_flow_fill_error(
        soup,
        bboxes,
        canvas,
        _shadow_tool_context(ctx),
    )
    current_editorial_payload = (
        current_editorial_fill.payload
        if current_editorial_fill and isinstance(current_editorial_fill.payload, dict)
        else {}
    )
    current_editorial_issues = current_editorial_payload.get("issues") if current_editorial_payload else []
    current_editorial_issues = current_editorial_issues if isinstance(current_editorial_issues, list) else []

    issues: list[dict[str, Any]] = []
    required_blank_fill_issues: list[dict[str, Any]] = []
    suppressed_advisory_density_issues: list[dict[str, Any]] = []
    if lost_source_ids or lost_layer_ids:
        issues.append({
            "id": "density_source_inventory_loss",
            "lost_source_ids": lost_source_ids[:12],
            "lost_layer_ids": lost_layer_ids[:12],
            "expected": "Overflow repair must preserve source-backed evidence assets from the dense near-miss baseline.",
            "repair": "Restore the lost source figures/tables or replace only with equivalent source-backed native evidence in the same local section.",
        })
    if source_flow_drop >= 1:
        issues.append({
            "id": "density_source_flow_unit_loss",
            "baseline_source_flow_unit_count": baseline_inventory.get("source_flow_unit_count"),
            "current_source_flow_unit_count": current_inventory.get("source_flow_unit_count"),
            "repair": "Restore source-flow units removed during overflow repair; rebalance local rows instead of deleting evidence.",
        })
    if section_drop >= 2 or (section_drop >= 1 and int(current_inventory.get("section_count", 0)) < 6):
        issues.append({
            "id": "density_section_inventory_loss",
            "baseline_section_count": baseline_inventory.get("section_count"),
            "current_section_count": current_inventory.get("section_count"),
            "repair": "Do not clear overflow by deleting major sections; compact and rebalance existing sections.",
        })
    if baseline_words >= 160 and word_loss_ratio >= 0.35 and baseline_words - current_words >= 80:
        issues.append({
            "id": "density_text_inventory_loss",
            "baseline_word_count": baseline_words,
            "current_word_count": current_words,
            "word_loss_ratio": word_loss_ratio,
            "repair": "Restore concise source-backed details removed during overflow repair; trim locally rather than hollowing panels.",
        })
    for issue in current_editorial_issues[:6]:
        if not isinstance(issue, dict):
            continue
        if str(issue.get("id") or "") in {
            "editorial_section_tail_blank",
            "editorial_section_internal_gap_blank",
            "editorial_column_bottom_underfilled",
            "editorial_body_shell_underfilled",
        }:
            enriched = dict(issue)
            enriched["id"] = f"density_{issue.get('id')}"
            enriched["repair"] = issue.get("repair") or (
                "Restore local density with source-backed readouts/native rows or rebalance section height."
            )
            target = issue.get("blank_fill_target") if isinstance(issue.get("blank_fill_target"), dict) else {}
            required = bool(_required_blank_fill_targets({"targets": [target]})) if target else False
            enriched["density_blank_fill_required"] = required
            if required:
                required_blank_fill_issues.append(enriched)
                issues.append(enriched)
                break
            suppressed = {
                **enriched,
                "density_blank_fill_required": False,
                "suppressed_reason": (
                    "blank_fill_target_not_required"
                    if target else
                    "blank_fill_target_unresolved"
                ),
            }
            suppressed_advisory_density_issues.append(suppressed)

    if suppressed_advisory_density_issues:
        ctx.state["paper_poster_html_post_overflow_suppressed_density_diagnostics"] = {
            "suppressed_advisory_issues": suppressed_advisory_density_issues[:8],
            "blank_fill_plan": current_editorial_payload.get("blank_fill_plan"),
        }
    if not issues:
        return None
    blank_fill_plan = current_editorial_payload.get("blank_fill_plan")
    required_blank_fill_targets = (
        _required_blank_fill_targets(blank_fill_plan)
        if isinstance(blank_fill_plan, dict)
        else []
    )
    return obs_error(
        "propose_paper_poster_html found that overflow repair cleared fit by hollowing the dense poster.",
        category="validation",
        payload={
            "issue_id": "paper_poster_html_post_overflow_density_conservation_failed",
            "repair_route": "restore_density_after_overflow_repair",
            "issues": issues[:8],
            "blank_fill_plan": blank_fill_plan if isinstance(blank_fill_plan, dict) else None,
            "required_blank_fill_targets": required_blank_fill_targets[:12],
            "blank_fill_required": bool(required_blank_fill_targets),
            "density_conservation": {
                "baseline_candidate_id": baseline.get("candidate_id"),
                "baseline_candidate_relative_dir": baseline.get("candidate_relative_dir"),
                "baseline_stage": baseline_stage,
                "baseline_inventory": baseline_inventory,
                "current_inventory": current_inventory,
                "lost_source_ids": lost_source_ids[:12],
                "lost_layer_ids": lost_layer_ids[:12],
                "source_flow_unit_drop": source_flow_drop,
                "section_drop": section_drop,
                "word_loss_ratio": word_loss_ratio,
                "baseline_fill_metrics": baseline_fill_metrics,
                "current_fill_metrics": current_fill_metrics,
                "required_blank_fill_issues": required_blank_fill_issues[:8],
                "suppressed_advisory_issues": suppressed_advisory_density_issues[:8],
                "acceptance": (
                    "Clear the prior overflow while preserving baseline source evidence, section inventory, "
                    "and local panel fill within a small tolerance."
                ),
            },
            "hint": (
                "The previous overflow repair made the poster too sparse. Restore source-backed density "
                "from the locked dense near-miss baseline, then rebalance local rows/spacing so overflow "
                "stays cleared. Do not delete source-flow units or major sections to satisfy fit."
            ),
        },
    )


def _same_canvas_size(current: dict[str, Any], baseline: dict[str, Any]) -> bool:
    return (
        _safe_int(current.get("w_px"), default=-1) == _safe_int(baseline.get("w_px"), default=-2)
        and _safe_int(current.get("h_px"), default=-1) == _safe_int(baseline.get("h_px"), default=-2)
    )


def _editorial_density_inventory(soup: BeautifulSoup) -> dict[str, Any]:
    source_ids = _unique_nonempty([
        str(tag.get("data-source-id") or "")
        for tag in soup.find_all(attrs={"data-source-id": True})
        if isinstance(tag, Tag)
    ])
    layer_ids = _unique_nonempty([
        str(tag.get("data-layer-id") or "")
        for tag in soup.find_all(attrs={"data-layer-id": True})
        if isinstance(tag, Tag)
    ])
    sections = [
        tag for tag in soup.select(".poster-section")
        if isinstance(tag, Tag)
    ]
    flow_units = [
        tag for tag in soup.select(".figure-flow-unit,.source-flow-unit")
        if isinstance(tag, Tag)
    ]
    text = " ".join(soup.get_text(" ", strip=True).split())
    return {
        "section_count": len(sections),
        "section_ids": _unique_nonempty([_section_label(section) for section in sections]),
        "source_flow_unit_count": len(flow_units),
        "source_flow_unit_ids": _unique_nonempty([
            str(unit.get("data-block-id") or unit.get("data-source-id") or "")
            for unit in flow_units
        ]),
        "source_count": len(source_ids),
        "source_ids": source_ids,
        "layer_count": len(layer_ids),
        "layer_ids": layer_ids,
        "word_count": _visible_word_count(text),
    }


def _editorial_body_target_bottom(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
) -> int:
    ch = int(canvas["h_px"])
    bottom_margin = max(18, int(round(ch * 0.015)))
    columns = soup.select_one(".poster-columns,[data-layout-region='main_panels']")
    columns_bbox = _bbox_for_tag(columns, bboxes) if isinstance(columns, Tag) else None
    if columns_bbox:
        return min(ch, max(ch - bottom_margin, int(columns_bbox["y"] + columns_bbox["h"])))
    return ch - bottom_margin


def _editorial_section_content_bboxes(
    section: Tag,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
) -> list[dict[str, Any]]:
    cw = int(canvas["w_px"])
    ch = int(canvas["h_px"])
    out: list[dict[str, Any]] = []
    for tag in section.find_all(True):
        if not isinstance(tag, Tag) or tag is section:
            continue
        if _is_editorial_column_tag(tag) or _is_editorial_section_tag(tag):
            continue
        block_id = str(tag.get("data-block-id") or "").strip()
        if not block_id:
            continue
        bbox = _bbox_only(bboxes.get(block_id))
        if not bbox:
            continue
        kind = _infer_block_kind(tag)
        if kind == "group" and not (_is_source_flow_unit(tag) or _is_meaningful_flow_content_panel(tag)):
            continue
        clipped = _clip_bbox_to_canvas(bbox, cw=cw, ch=ch)
        if clipped and clipped["w"] * clipped["h"] >= 120:
            clipped["_content_bbox_source"] = "block_bbox"
            out.append(clipped)
    out.extend(_editorial_section_visible_text_line_bboxes(section, bboxes, canvas))
    return out


def _editorial_section_internal_gap_issue(
    section: Tag,
    section_bbox: dict[str, int],
    content_boxes: list[dict[str, Any]],
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
) -> dict[str, Any] | None:
    line_h = _median_body_line_height_px(section, bboxes)
    heading_bottom = _section_heading_bottom(section, bboxes)
    lines = [
        box for box in _editorial_section_visible_text_line_bboxes(section, bboxes, canvas)
        if int(box.get("y") or 0) + int(box.get("h") or 0) > heading_bottom + 3
    ]
    if len(lines) < 3:
        return None
    lines.sort(key=lambda box: (int(box.get("y") or 0), int(box.get("x") or 0)))
    cluster_gap = max(28, int(round(line_h * 1.65)))
    clusters: list[dict[str, Any]] = []
    for line in lines:
        top = int(line["y"])
        bottom = top + int(line["h"])
        if clusters and top <= int(clusters[-1]["bottom"]) + cluster_gap:
            clusters[-1]["bottom"] = max(int(clusters[-1]["bottom"]), bottom)
            clusters[-1]["top"] = min(int(clusters[-1]["top"]), top)
            clusters[-1]["lines"].append(line)
        else:
            clusters.append({"top": top, "bottom": bottom, "lines": [line]})
    if len(clusters) < 2:
        return None
    section_bottom = int(section_bbox["y"] + section_bbox["h"])
    gap_threshold = max(84, int(round(line_h * 4.0)))
    best: tuple[int, dict[str, Any], dict[str, Any], dict[str, int]] | None = None
    for upper, lower in zip(clusters, clusters[1:]):
        upper_bottom = int(upper["bottom"])
        lower_top = int(lower["top"])
        gap = lower_top - upper_bottom
        if gap < gap_threshold:
            continue
        lower_bottom = int(lower["bottom"])
        lower_near_bottom = section_bottom - lower_bottom <= max(96, int(round(line_h * 5.0)))
        huge_gap = gap >= max(150, int(round(line_h * 7.0)))
        if not lower_near_bottom and not huge_gap:
            continue
        blank_box = _clip_bbox_to_canvas(
            {
                "x": int(section_bbox["x"]),
                "y": upper_bottom,
                "w": int(section_bbox["w"]),
                "h": gap,
            },
            cw=int(canvas["w_px"]),
            ch=int(canvas["h_px"]),
        )
        if not blank_box or _section_gap_has_visible_block_content(blank_box, content_boxes):
            continue
        if best is None or gap > best[0]:
            best = (gap, upper, lower, blank_box)
    if best is None:
        return None
    gap, upper, lower, blank_box = best
    lower_line = lower["lines"][0] if lower.get("lines") else {}
    lower_block_id = _block_id_for_text_line(section, bboxes, lower_line if isinstance(lower_line, dict) else {})
    if not lower_block_id:
        return None
    column = _nearest_ancestor_with_class(section, {"poster-column"})
    section_id = _section_label(section)
    return {
        "id": "editorial_section_internal_gap_blank",
        "section_id": section_id,
        "section_title": _section_heading_text(section),
        "column_id": _column_label(column) if isinstance(column, Tag) else "",
        "internal_gap_px": gap,
        "required_max_gap_px": gap_threshold,
        "section_bottom_px": section_bottom,
        "upper_content_bottom_px": int(upper["bottom"]),
        "lower_content_top_px": int(lower["top"]),
        "lower_content_bottom_px": int(lower["bottom"]),
        "lower_block_id": lower_block_id,
        "blank_bbox_canvas": blank_box,
        "content_bottom_source": "text_line_internal_gap",
        "tail_gap_confidence": "high",
        "has_source_asset": bool(section.find(attrs={"data-source-id": True}) or section.find(attrs={"data-layer-id": True})),
        "repair": (
            "The section has a large visible internal gap before a lower note/footer. Fill that local "
            "gap with compact paper-backed native rows, or concise bullets before the "
            "lower note, or reduce/rebalance this section height. Do not append a long paragraph after "
            "the lower note and do not change global columns."
        ),
    }


def _section_heading_bottom(section: Tag, bboxes: dict[str, dict[str, int]]) -> int:
    section_bbox = _bbox_for_tag(section, bboxes)
    fallback = int(section_bbox["y"]) if section_bbox else 0
    bottoms: list[int] = []
    for tag in section.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if not isinstance(tag, Tag):
            continue
        bbox = _bbox_for_tag(tag, bboxes)
        if bbox:
            bottoms.append(int(bbox["y"] + bbox["h"]))
    return max(bottoms) if bottoms else fallback


def _section_gap_has_visible_block_content(blank_box: dict[str, int], content_boxes: list[dict[str, Any]]) -> bool:
    blank_area = max(1.0, _bbox_area(blank_box))
    for box in content_boxes:
        if str(box.get("_content_bbox_source") or "") != "block_bbox":
            continue
        overlap = _bbox_overlap_area(blank_box, box)
        if overlap / blank_area >= 0.08:
            return True
    return False


def _block_id_for_text_line(section: Tag, bboxes: dict[str, dict[str, int]], line: dict[str, Any]) -> str:
    line_box = _bbox_only(line)
    if not line_box:
        return ""
    best: tuple[float, str] | None = None
    for tag in section.find_all(True):
        if not isinstance(tag, Tag):
            continue
        block_id = str(tag.get("data-block-id") or "").strip()
        if not block_id:
            continue
        bbox = _bbox_only(bboxes.get(block_id))
        if not bbox:
            continue
        overlap = _bbox_overlap_area(line_box, bbox)
        if overlap <= 0:
            continue
        area = _bbox_area(bbox)
        if best is None or area < best[0]:
            best = (area, block_id)
    return best[1] if best else ""


def _editorial_section_visible_text_line_bboxes(
    section: Tag,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
) -> list[dict[str, Any]]:
    section_bbox = _bbox_for_tag(section, bboxes)
    if not section_bbox:
        return []
    cw = int(canvas["w_px"])
    ch = int(canvas["h_px"])
    out: list[dict[str, Any]] = []
    seen: set[tuple[int, int, int, int]] = set()
    measured_records: list[dict[str, Any]] = []
    section_id = str(section.get("data-block-id") or "").strip()
    if section_id and isinstance(bboxes.get(section_id), dict):
        measured_records.append(bboxes[section_id])
    for tag in section.find_all(True):
        if not isinstance(tag, Tag) or tag is section:
            continue
        block_id = str(tag.get("data-block-id") or "").strip()
        if block_id and isinstance(bboxes.get(block_id), dict):
            measured_records.append(bboxes[block_id])
    for measured in measured_records:
        for key in ("_text_line_bboxes", "_raw_text_line_bboxes"):
            raw_lines = measured.get(key)
            if not isinstance(raw_lines, list):
                continue
            for raw_line in raw_lines:
                line = _bbox_only(raw_line) if isinstance(raw_line, dict) else None
                if not line:
                    continue
                clipped = _intersect_bbox(line, section_bbox)
                if not clipped:
                    continue
                clipped = _clip_bbox_to_canvas(_visible_bbox_for_overlap(clipped), cw=cw, ch=ch)
                if not clipped or clipped["w"] * clipped["h"] < 30:
                    continue
                signature = (int(clipped["x"]), int(clipped["y"]), int(clipped["w"]), int(clipped["h"]))
                if signature in seen:
                    continue
                seen.add(signature)
                clipped["_content_bbox_source"] = "text_line_union"
                out.append(clipped)
    return out


def _intersect_bbox(a: dict[str, int], b: dict[str, int]) -> dict[str, int] | None:
    ax1 = int(a.get("x") or 0)
    ay1 = int(a.get("y") or 0)
    ax2 = ax1 + int(a.get("w") or 0)
    ay2 = ay1 + int(a.get("h") or 0)
    bx1 = int(b.get("x") or 0)
    by1 = int(b.get("y") or 0)
    bx2 = bx1 + int(b.get("w") or 0)
    by2 = by1 + int(b.get("h") or 0)
    x1 = max(ax1, bx1)
    y1 = max(ay1, by1)
    x2 = min(ax2, bx2)
    y2 = min(ay2, by2)
    if x2 <= x1 or y2 <= y1:
        return None
    return {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1}


def _editorial_source_flow_readout_issues(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
    ctx: ToolContext,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for unit in soup.find_all(True):
        if not isinstance(unit, Tag) or not _is_source_flow_unit(unit) or not _source_in_editorial_flow(unit):
            continue
        unit_bbox = _bbox_for_tag(unit, bboxes)
        if not unit_bbox:
            continue
        for raw_asset in unit.find_all(["figure", "img", "table"]):
            if not isinstance(raw_asset, Tag):
                continue
            asset = _source_wrap_tag(raw_asset)
            source_id = _source_id_for_tag(asset, ctx)
            if not source_id:
                continue
            key = (source_id, str(asset.get("data-block-id") or raw_asset.get("data-block-id") or ""))
            if key in seen:
                continue
            seen.add(key)
            asset_bbox = _bbox_for_tag(asset, bboxes) or _bbox_for_tag(raw_asset, bboxes)
            if not asset_bbox:
                continue
            classes = _source_flow_asset_classes(unit, asset, raw_asset)
            width_ratio = int(asset_bbox["w"]) / float(max(1, int(unit_bbox["w"])))
            is_wide = "asset-wide" in classes or width_ratio >= 0.82
            local_words = _visible_panel_word_count(unit, exclude=asset)
            min_words = 18 if is_wide else _MIN_SOURCE_VISUAL_SIDE_TEXT_WORDS
            if _has_asset_size_class(classes, "asset-large"):
                min_words = _MIN_SOURCE_VISUAL_LARGE_SIDE_TEXT_WORDS
            if local_words < min_words:
                issues.append({
                    "id": "editorial_source_flow_local_readout_thin",
                    "source_id": source_id,
                    "flow_unit_id": str(unit.get("data-block-id") or ""),
                    "asset_block_id": str(asset.get("data-block-id") or raw_asset.get("data-block-id") or ""),
                    "local_word_count": local_words,
                    "required_min_words": min_words,
                    "asset_width_ratio": round(width_ratio, 3),
                    "classes": sorted(classes),
                    "repair": (
                        "This source image/table is not supported by enough local readout text "
                        "inside its own flow unit. Add concise source-backed explanation, labels, "
                        "or a compact native/result row in the same source-flow unit."
                    ),
                })
                continue
            if is_wide or width_ratio < 0.36 or width_ratio > 0.78:
                continue
            text_boxes = _source_flow_text_bboxes(unit, asset, bboxes, canvas)
            side_coverage = _source_flow_side_text_coverage(unit_bbox, asset_bbox, text_boxes)
            if side_coverage < _MIN_SOURCE_VISUAL_SIDE_TEXT_COVERAGE_RATIO:
                issues.append({
                    "id": "editorial_source_flow_wrap_side_underfilled",
                    "source_id": source_id,
                    "flow_unit_id": str(unit.get("data-block-id") or ""),
                    "asset_block_id": str(asset.get("data-block-id") or raw_asset.get("data-block-id") or ""),
                    "side_text_coverage_ratio": round(side_coverage, 3),
                    "required_min_side_text_coverage_ratio": _MIN_SOURCE_VISUAL_SIDE_TEXT_COVERAGE_RATIO,
                    "asset_width_ratio": round(width_ratio, 3),
                    "local_word_count": local_words,
                    "repair": (
                        "The text exists but does not actually use the side wrap space beside "
                        "the floated asset. Either make the asset full-width/stacked with its "
                        "readout below, or add direct sibling source-backed prose/native rows "
                        "that cover the side of that one asset."
                    ),
                })
                continue
            lane_tail_issue = _source_flow_side_lane_tail_blank_issue(
                unit,
                asset,
                raw_asset,
                unit_bbox,
                asset_bbox,
                text_boxes,
                bboxes,
                canvas,
                source_id=source_id,
                local_words=local_words,
                width_ratio=width_ratio,
            )
            if lane_tail_issue:
                issues.append(lane_tail_issue)
    return issues


def _source_flow_side_lane_tail_blank_issue(
    unit: Tag,
    asset: Tag,
    raw_asset: Tag,
    unit_bbox: dict[str, int],
    asset_bbox: dict[str, int],
    text_boxes: list[dict[str, int]],
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
    *,
    source_id: str,
    local_words: int,
    width_ratio: float,
) -> dict[str, Any] | None:
    if width_ratio < 0.30 or width_ratio > 0.78 or not text_boxes:
        return None
    cw = int(canvas["w_px"])
    ch = int(canvas["h_px"])
    blank_box = _source_flow_blank_side_bbox(unit_bbox, asset_bbox, cw=cw, ch=ch)
    if not blank_box:
        return None
    side_x1 = int(blank_box["x"])
    side_x2 = side_x1 + int(blank_box["w"])
    side_content_bottoms: list[int] = []
    for box in text_boxes:
        box_x = int(box["x"])
        box_w = int(box["w"])
        box_center = box_x + box_w / 2.0
        if side_x1 - 4 <= box_center <= side_x2 + 4:
            side_content_bottoms.append(int(box["y"] + box["h"]))
    if not side_content_bottoms:
        return None
    side_content_bottom = max(side_content_bottoms)
    side_lane_bottom = int(blank_box["y"] + blank_box["h"])
    gap = side_lane_bottom - side_content_bottom
    line_h = _median_body_line_height_px(unit, bboxes)
    required_max_gap = max(40, int(round(line_h * 2.0)))
    if gap <= required_max_gap:
        return None
    line_count = _clamp_int(int(math.floor((gap - required_max_gap) / max(line_h, 12))), 1, 5)
    words_min = _clamp_int(line_count * 8, 8, 32)
    section = _nearest_ancestor_with_class(unit, {"poster-section"})
    column = _nearest_ancestor_with_class(unit, {"poster-column"})
    asset_block_id = str(asset.get("data-block-id") or raw_asset.get("data-block-id") or "")
    return {
        "id": "editorial_source_flow_side_lane_tail_blank",
        "source_id": source_id,
        "flow_unit_id": str(unit.get("data-block-id") or ""),
        "asset_block_id": asset_block_id,
        "section_id": _section_label(section) if isinstance(section, Tag) else "",
        "column_id": _column_label(column) if isinstance(column, Tag) else "",
        "side_lane_bottom_px": side_lane_bottom,
        "side_content_bottom_px": side_content_bottom,
        "lane_tail_gap_px": gap,
        "required_max_gap_px": required_max_gap,
        "local_word_count": local_words,
        "asset_width_ratio": round(width_ratio, 3),
        "words_to_add_min": words_min,
        "words_to_add_max": words_min + 12,
        "target_line_count": line_count,
        "repair": (
            "The source-flow unit is visually tall because of the source asset, but the opposite "
            "readout/table lane ends early. Fill that same lane with source-backed readout, metric "
            "chips, native rows, or mechanism bullets without resizing the source visual."
        ),
    }


def _has_asset_size_class(classes: set[str], token: str) -> bool:
    token = token.strip().lower()
    return token in classes or any(cls.startswith(f"{token}-") for cls in classes)


def _source_flow_asset_classes(unit: Tag, asset: Tag, raw_asset: Tag) -> set[str]:
    classes: set[str] = set()
    for tag in (unit, asset, raw_asset):
        if isinstance(tag, Tag):
            classes.update(_class_tokens(tag))
    classes.update(_source_asset_ancestor_classes(asset, stop=unit))
    if raw_asset is not asset:
        classes.update(_source_asset_ancestor_classes(raw_asset, stop=unit))
    return classes


def _source_asset_ancestor_classes(tag: Tag, *, stop: Tag | None = None) -> set[str]:
    classes: set[str] = set()
    node = tag.parent if isinstance(tag, Tag) else None
    while isinstance(node, Tag) and node is not stop:
        if _is_source_flow_unit(node) or _is_editorial_section_tag(node) or _is_editorial_column_tag(node):
            break
        if str(node.name or "").lower() in {"main", "body", "html"}:
            break
        classes.update(_class_tokens(node))
        node = node.parent if isinstance(node.parent, Tag) else None
    return classes


def _source_flow_text_bboxes(
    unit: Tag,
    asset: Tag,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
) -> list[dict[str, int]]:
    cw = int(canvas["w_px"])
    ch = int(canvas["h_px"])
    out: list[dict[str, int]] = []
    for tag in unit.find_all(True):
        if not isinstance(tag, Tag) or tag is asset or _is_ancestor(asset, tag):
            continue
        block_id = str(tag.get("data-block-id") or "").strip()
        if not block_id:
            continue
        if _infer_block_kind(tag) not in {"text", "caption", "quote", "metric", "table"}:
            continue
        if _infer_block_kind(tag) != "table" and _visible_word_count(tag.get_text(" ", strip=True)) < 2:
            continue
        measured = bboxes.get(block_id)
        if not isinstance(measured, dict):
            continue
        line_boxes = measured.get("_text_line_bboxes")
        if isinstance(line_boxes, list) and line_boxes:
            appended_lines = False
            for raw_line in line_boxes:
                line = _bbox_only(raw_line) if isinstance(raw_line, dict) else None
                if not line:
                    continue
                clipped_line = _clip_bbox_to_canvas(_visible_bbox_for_overlap(line), cw=cw, ch=ch)
                if clipped_line and clipped_line["w"] * clipped_line["h"] >= 30:
                    out.append(clipped_line)
                    appended_lines = True
            if appended_lines:
                continue
        bbox = _bbox_only(measured)
        if not bbox:
            continue
        clipped = _clip_bbox_to_canvas(_visible_bbox_for_overlap(bbox), cw=cw, ch=ch)
        if clipped and clipped["w"] * clipped["h"] >= 80:
            out.append(clipped)
    return out


def _source_flow_side_text_coverage(
    unit_bbox: dict[str, int],
    asset_bbox: dict[str, int],
    text_boxes: list[dict[str, int]],
) -> float:
    asset_x = int(asset_bbox["x"])
    asset_y = int(asset_bbox["y"])
    asset_w = int(asset_bbox["w"])
    asset_h = int(asset_bbox["h"])
    if asset_h <= 0 or not text_boxes:
        return 0.0
    asset_center = asset_x + asset_w / 2.0
    unit_center = int(unit_bbox["x"]) + int(unit_bbox["w"]) / 2.0
    asset_on_right = asset_center >= unit_center
    intervals: list[tuple[int, int]] = []
    for box in text_boxes:
        x = int(box["x"])
        y = int(box["y"])
        w = int(box["w"])
        h = int(box["h"])
        overlap_top = max(y, asset_y)
        overlap_bottom = min(y + h, asset_y + asset_h)
        if overlap_bottom - overlap_top <= 6:
            continue
        box_center = x + w / 2.0
        if asset_on_right:
            if box_center >= asset_x - 4:
                continue
        else:
            if box_center <= asset_x + asset_w + 4:
                continue
        intervals.append((overlap_top, overlap_bottom))
    if not intervals:
        return 0.0
    intervals.sort()
    merged: list[list[int]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    covered = sum(max(0, end - start) for start, end in merged)
    return covered / float(max(1, asset_h))


_TYPOGRAPHY_MIN_TIMES_NEW_ROMAN_FAMILY_RATIO = 1.0
_TYPOGRAPHY_TITLE_WEIGHT_MIN = 550
_TYPOGRAPHY_TITLE_WEIGHT_MAX = 650
_TYPOGRAPHY_HEADING_WEIGHT_MIN = 650
_TYPOGRAPHY_BODY_WEIGHT_MAX = 620
_TYPOGRAPHY_MAX_BODY_ITALIC_RATIO = 0.15
_TYPOGRAPHY_MIN_BODY_LINE_HEIGHT = 1.08
_TYPOGRAPHY_MAX_BODY_LINE_HEIGHT = 1.35
_TYPOGRAPHY_FIXED_FONT_SIZE_TOLERANCE_PX = 0.5
_TYPOGRAPHY_FIXED_FONT_SIZES_PX = {
    "title": 56.0,
    "identity_meta": 28.0,
    "section_heading": 36.0,
    "body": 24.0,
    "readout": 24.0,
    "table_text": 24.0,
    "caption": 20.0,
    "label": 20.0,
}
_TYPOGRAPHY_BODY_LIKE_ROLES = {"body", "readout", "caption", "table_text"}
_EDITORIAL_LEAD_KEY_MAX_WORDS = 5
_EDITORIAL_LEAD_KEY_MIN_BODY_WORDS = 80
_EDITORIAL_LEAD_KEY_MIN_BODY_ITEMS = 4
_EDITORIAL_LEAD_KEY_SCATTERED_STRONG_COUNT = 8
_EDITORIAL_LEAD_KEY_STRONG_WORD_RATIO_MAX = 0.35
_EDITORIAL_LEAD_KEY_BODY_TAGS = {
    "p", "li", "td", "th", "blockquote",
}


def _active_paper_typography_contract(ctx: ToolContext) -> dict[str, Any]:
    reference = _active_reference_style_contract(ctx)
    reference_typography = (
        reference.get("typography_contract")
        if isinstance(reference, dict) and isinstance(reference.get("typography_contract"), dict)
        else None
    )
    if isinstance(reference_typography, dict) and reference_typography:
        return dict(reference_typography)
    return {
        "source": "default_academic_contract",
        "family_category": "times_new_roman",
        "font_family": '"Times New Roman", Times, Georgia, serif',
        "primary_font_family": "Times New Roman",
        "title_font_size_px": 56,
        "identity_rows_font_size_px": 28,
        "section_heading_font_size_px": 36,
        "body_font_size_px": 24,
        "readout_font_size_px": 24,
        "table_text_font_size_px": 24,
        "caption_font_size_px": 20,
        "label_font_size_px": 20,
        "font_size_tolerance_px": _TYPOGRAPHY_FIXED_FONT_SIZE_TOLERANCE_PX,
        "title_weight_min": _TYPOGRAPHY_TITLE_WEIGHT_MIN,
        "title_weight_max": _TYPOGRAPHY_TITLE_WEIGHT_MAX,
        "heading_weight_min": _TYPOGRAPHY_HEADING_WEIGHT_MIN,
        "body_weight_max": _TYPOGRAPHY_BODY_WEIGHT_MAX,
        "max_body_italic_ratio": _TYPOGRAPHY_MAX_BODY_ITALIC_RATIO,
    }


def _typography_role_sizes(contract: dict[str, Any]) -> dict[str, float]:
    return {
        "title": _safe_float(contract.get("title_font_size_px")) or 56.0,
        "identity_meta": _safe_float(contract.get("identity_rows_font_size_px")) or 28.0,
        "section_heading": _safe_float(contract.get("section_heading_font_size_px")) or 36.0,
        "subsection_heading": (
            _safe_float(contract.get("subsection_heading_font_size_px"))
            or _safe_float(contract.get("body_font_size_px"))
            or 24.0
        ),
        "body": _safe_float(contract.get("body_font_size_px")) or 24.0,
        "readout": _safe_float(contract.get("readout_font_size_px")) or 24.0,
        "table_text": _safe_float(contract.get("table_text_font_size_px")) or 24.0,
        "caption": _safe_float(contract.get("caption_font_size_px") or contract.get("caption_label_font_size_px")) or 20.0,
        "label": _safe_float(contract.get("label_font_size_px") or contract.get("caption_label_font_size_px")) or 20.0,
        "lead_band": _safe_float(contract.get("lead_band_font_size_px")) or 38.0,
    }


def _typography_family_matches(value: Any, family_category: str) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    first = raw.split(",")[0].strip().strip("'\"").lower()
    if family_category == "times_new_roman":
        return _is_times_new_roman_family(value)
    if family_category == "sans_serif":
        return first in {
            "arial", "helvetica neue", "helvetica", "inter", "roboto", "open sans",
            "noto sans", "noto sans sc", "sans-serif", "system-ui",
        } or "sans" in first
    return _is_academic_serif_family(value)


def _paper_poster_typography_contract_error(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    ctx: ToolContext,
) -> ToolResultRecord | None:
    if not _dogfood_dense_mode(ctx):
        return None
    if not _editorial_flow_mode(ctx) and soup.select_one(".paper-poster,.editorial-poster") is None:
        return None
    samples = _paper_typography_samples(soup, bboxes)
    if not samples:
        return None

    typography_contract = _active_paper_typography_contract(ctx)
    expected_font_sizes = _typography_role_sizes(typography_contract)
    expected_font_family = str(
        typography_contract.get("font_family")
        or typography_contract.get("primary_font_family")
        or '"Times New Roman", Times, Georgia, serif'
    )
    expected_family_category = str(typography_contract.get("family_category") or "serif")
    font_size_tolerance = _safe_float(typography_contract.get("font_size_tolerance_px")) or 0.5
    title_weight_min = _safe_float(typography_contract.get("title_weight_min")) or _TYPOGRAPHY_TITLE_WEIGHT_MIN
    title_weight_max = _safe_float(typography_contract.get("title_weight_max")) or _TYPOGRAPHY_TITLE_WEIGHT_MAX
    heading_weight_min = _safe_float(typography_contract.get("heading_weight_min")) or _TYPOGRAPHY_HEADING_WEIGHT_MIN
    body_weight_max = _safe_float(typography_contract.get("body_weight_max")) or _TYPOGRAPHY_BODY_WEIGHT_MAX
    max_body_italic_ratio = _safe_float(typography_contract.get("max_body_italic_ratio")) or _TYPOGRAPHY_MAX_BODY_ITALIC_RATIO

    meaningful = [item for item in samples if item["role"] != "metric_value"]
    if not meaningful:
        return None
    serif_ok = [item for item in meaningful if _is_academic_serif_family(item.get("actual_font_family"))]
    serif_ratio = round(len(serif_ok) / max(1, len(meaningful)), 3)
    times_new_roman_ok = [
        item for item in meaningful if _is_times_new_roman_family(item.get("actual_font_family"))
    ]
    times_new_roman_ratio_raw = len(times_new_roman_ok) / max(1, len(meaningful))
    times_new_roman_ratio = round(times_new_roman_ratio_raw, 3)
    family_match = [
        item for item in meaningful
        if _typography_family_matches(item.get("actual_font_family"), expected_family_category)
    ]
    family_match_ratio_raw = len(family_match) / max(1, len(meaningful))
    family_match_ratio = round(family_match_ratio_raw, 3)

    font_sizes_by_role: dict[str, list[float]] = {}
    weights_by_role: dict[str, list[float]] = {}
    body_like: list[dict[str, Any]] = []
    for item in samples:
        role = str(item.get("role") or "")
        size = _safe_float(item.get("actual_font_size_px"))
        weight = _font_weight_number(item.get("actual_font_weight"))
        if size > 0:
            font_sizes_by_role.setdefault(role, []).append(size)
        if weight > 0:
            weights_by_role.setdefault(role, []).append(weight)
        if role in _TYPOGRAPHY_BODY_LIKE_ROLES:
            body_like.append(item)

    title_median = _median_numeric(font_sizes_by_role.get("title", []))
    heading_median = _median_numeric(font_sizes_by_role.get("section_heading", []))
    body_median_size = _median_numeric(
        font_sizes_by_role.get("body", [])
        + font_sizes_by_role.get("readout", [])
        + font_sizes_by_role.get("table_text", [])
    )
    readout_median_size = _median_numeric(font_sizes_by_role.get("readout", []))
    table_text_median_size = _median_numeric(font_sizes_by_role.get("table_text", []))
    caption_label_median_size = _median_numeric(
        font_sizes_by_role.get("caption", [])
        + font_sizes_by_role.get("label", [])
    )
    caption_median_size = _median_numeric(font_sizes_by_role.get("caption", []))
    label_median_size = _median_numeric(font_sizes_by_role.get("label", []))
    title_weight_median = _median_numeric(weights_by_role.get("title", []))
    heading_weight_median = _median_numeric(weights_by_role.get("section_heading", []))
    body_weight_median = _median_numeric(
        weights_by_role.get("body", [])
        + weights_by_role.get("readout", [])
        + weights_by_role.get("caption", [])
        + weights_by_role.get("table_text", [])
    )
    italic_body_ratio = round(
        sum(1 for item in body_like if str(item.get("actual_font_style") or "").lower().startswith("italic"))
        / max(1, len(body_like)),
        3,
    )
    font_size_levels = sorted({
        int(round(_safe_float(item.get("actual_font_size_px"))))
        for item in samples
        if _safe_float(item.get("actual_font_size_px")) > 0
    })
    aggregate = {
        "serif_family_ratio": serif_ratio,
        "font_family_match_ratio": family_match_ratio,
        "font_family_match_ratio_required": 1.0,
        "expected_font_family": expected_font_family,
        "expected_font_family_category": expected_family_category,
        "times_new_roman_family_ratio": times_new_roman_ratio,
        "times_new_roman_family_ratio_required": (
            _TYPOGRAPHY_MIN_TIMES_NEW_ROMAN_FAMILY_RATIO
            if expected_family_category in {"serif", "times_new_roman"}
            and "times new roman" in expected_font_family.lower()
            else 0.0
        ),
        "font_size_levels": font_size_levels,
        "expected_font_sizes_px": expected_font_sizes,
        "font_size_tolerance_px": font_size_tolerance,
        "title_size_median": round(title_median, 2) if title_median else 0,
        "section_heading_size_median": round(heading_median, 2) if heading_median else 0,
        "body_size_median": round(body_median_size, 2) if body_median_size else 0,
        "readout_size_median": round(readout_median_size, 2) if readout_median_size else 0,
        "table_text_size_median": round(table_text_median_size, 2) if table_text_median_size else 0,
        "caption_label_size_median": round(caption_label_median_size, 2) if caption_label_median_size else 0,
        "caption_size_median": round(caption_median_size, 2) if caption_median_size else 0,
        "label_size_median": round(label_median_size, 2) if label_median_size else 0,
        "title_weight_median": round(title_weight_median, 2) if title_weight_median else 0,
        "title_weight_min": title_weight_min,
        "title_weight_max": title_weight_max,
        "section_heading_weight_median": round(heading_weight_median, 2) if heading_weight_median else 0,
        "section_heading_weight_min": heading_weight_min,
        "heading_weight_median": round(max(title_weight_median, heading_weight_median), 2)
        if (title_weight_median or heading_weight_median)
        else 0,
        "body_weight_median": round(body_weight_median, 2) if body_weight_median else 0,
        "italic_body_ratio": italic_body_ratio,
    }

    issues: list[dict[str, Any]] = []
    if family_match_ratio_raw < 1.0:
        for item in [
            sample for sample in meaningful
            if not _typography_family_matches(sample.get("actual_font_family"), expected_family_category)
        ][:6]:
            issues.append(_typography_issue(
                item,
                (
                    "font_family_not_times_new_roman"
                    if expected_family_category == "times_new_roman"
                    else "font_family_not_active_contract"
                ),
                f"Use font-family: {expected_font_family} on the poster root and inherited text.",
                aggregate,
            ))

    fixed_size_roles_seen: set[str] = set()
    for item in samples:
        role = str(item.get("role") or "")
        expected_size = expected_font_sizes.get(role)
        actual_size = _safe_float(item.get("actual_font_size_px"))
        if expected_size is None or actual_size <= 0:
            continue
        if abs(actual_size - expected_size) <= font_size_tolerance:
            continue
        if role in fixed_size_roles_seen:
            continue
        fixed_size_roles_seen.add(role)
        issue = _typography_issue(
            item,
            "font_size_not_fixed_role_size",
            (
                "Use the active run's fixed role sizes from the reference/default typography contract: "
                + ", ".join(f"{key} {value:g}px" for key, value in expected_font_sizes.items())
                + "."
            ),
            aggregate,
        )
        issue.update({
            "expected_font_size_px": expected_size,
            "font_size_delta_px": round(actual_size - expected_size, 2),
            "font_size_tolerance_px": font_size_tolerance,
        })
        issues.append(issue)

    if (title_weight_median and title_weight_median < title_weight_min) or (
        heading_weight_median and heading_weight_median < heading_weight_min
    ):
        representative = _first_typography_sample(samples, {"title", "section_heading"}) or samples[0]
        issues.append(_typography_issue(
            representative,
            "heading_weight_too_light",
            f"Use the active typography weights: title >= {title_weight_min:g} and section headings >= {heading_weight_min:g}.",
            aggregate,
        ))
    if title_weight_median and title_weight_median > title_weight_max:
        representative = _first_typography_sample(samples, {"title"}) or samples[0]
        issues.append(_typography_issue(
            representative,
            "title_weight_too_heavy",
            f"Keep title weight <= {title_weight_max:g} for the active typography contract.",
            aggregate,
        ))

    if body_weight_median and body_weight_median > body_weight_max:
        representative = _first_typography_sample(body_like, _TYPOGRAPHY_BODY_LIKE_ROLES) or samples[0]
        issues.append(_typography_issue(
            representative,
            "body_weight_too_heavy",
            "Keep body, readout, and table text regular weight; avoid broad bold body copy.",
            aggregate,
        ))

    if italic_body_ratio > max_body_italic_ratio:
        representative = next(
            (item for item in body_like if str(item.get("actual_font_style") or "").lower().startswith("italic")),
            body_like[0] if body_like else samples[0],
        )
        issues.append(_typography_issue(
            representative,
            "body_italic_overuse",
            f"Use italics sparingly; body/readout italic ratio must stay <= {max_body_italic_ratio:.2f}.",
            aggregate,
        ))

    hard_line_height = []
    near_miss_line_height = []
    for item in body_like:
        if item.get("actual_line_height") in (None, ""):
            continue
        ratio = _safe_float(item.get("actual_line_height"))
        if ratio < 1.04 or ratio > 1.45:
            hard_line_height.append(item)
        elif ratio < _TYPOGRAPHY_MIN_BODY_LINE_HEIGHT or ratio > _TYPOGRAPHY_MAX_BODY_LINE_HEIGHT:
            near_miss_line_height.append(item)
    if hard_line_height:
        issues.append(_typography_issue(
            hard_line_height[0],
            "body_line_height_unsafe",
            "Keep body/readout/table line-height ratio in the readable 1.08..1.35 range.",
            aggregate,
            severity="hard",
            soft_finalizable=False,
        ))
    elif near_miss_line_height:
        ctx.state["paper_poster_html_typography_near_miss"] = {
            "issues": [
                _typography_issue(
                    near_miss_line_height[0],
                    "body_line_height_unsafe",
                    "Keep body/readout/table line-height ratio in the readable 1.08..1.35 range.",
                    aggregate,
                    severity="near_miss",
                    soft_finalizable=True,
                )
            ],
            **aggregate,
        }

    if not issues:
        return None
    size_summary = ", ".join(
        f"{role} {size:g}px" for role, size in expected_font_sizes.items()
    )
    return obs_error(
        "propose_paper_poster_html found typography that does not match the active poster contract.",
        category="validation",
        payload={
            "issue_id": "paper_poster_html_typography_contract_failed",
            "repair_route": "revise_typography_system",
            "issues": issues[:12],
            **aggregate,
            "hint": (
                f"Set the poster root to font-family: {expected_font_family}; use fixed role sizes {size_summary}; "
                "keep headings at the active contract weights and body/readouts regular; "
                "use only sparse sentence-start lead-key emphasis; avoid broad bold or italic body copy."
            ),
        },
    )


def _paper_typography_samples(soup: BeautifulSoup, bboxes: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        block_id = str(tag.get("data-block-id") or "").strip()
        if not block_id or block_id not in bboxes:
            continue
        if _skip_typography_tag(tag):
            continue
        kind = _infer_block_kind(tag)
        if kind not in {"text", "caption", "quote", "metric", "table"}:
            continue
        text = tag.get_text(" ", strip=True)
        if not text:
            continue
        role = _paper_typography_role(tag, kind)
        if role in _TYPOGRAPHY_BODY_LIKE_ROLES and _is_short_inline_lead_key_tag(tag):
            role = "lead_key"
        words = len(text.split())
        if words < 2 and role not in {
            "title", "section_heading", "subsection_heading", "label", "metric_value",
        }:
            continue
        measured = bboxes.get(block_id) if isinstance(bboxes.get(block_id), dict) else {}
        style = measured.get("_computed_style") if isinstance(measured.get("_computed_style"), dict) else {}
        samples.append({
            "role": role,
            "block_id": block_id,
            "actual_font_family": _font_family_for_tag(tag, bboxes),
            "actual_font_size_px": style.get("font_size_px"),
            "actual_font_weight": style.get("font_weight"),
            "actual_font_style": style.get("font_style"),
            "actual_line_height": style.get("line_height"),
            "sample_text": text[:160],
        })
    return samples


def _skip_typography_tag(tag: Tag) -> bool:
    name = str(tag.name or "").lower()
    if name in {"script", "style", "template", "img", "svg", "canvas", "code", "kbd", "samp", "var"}:
        return True
    role_blob = _semantic_role_blob(tag, include_ancestors=True)
    skip_tokens = (
        "katex", "math", "mathjax", "mjx-", "logo", "badge", "qr",
        "barcode", "source-figure", "flow-asset", "paper-visual",
    )
    return any(token in role_blob for token in skip_tokens)


def _paper_typography_role(tag: Tag, kind: str) -> str:
    name = str(tag.name or "").lower()
    role_blob = _semantic_role_blob(tag, include_ancestors=True)
    text = tag.get_text("", strip=True)
    if "lead-band" in role_blob or "lead_band" in role_blob or "reference-lead-band" in role_blob:
        return "lead_band"
    if (
        "poster-title" in role_blob
        or "poster_title" in role_blob
        or "paper-title" in role_blob
        or "paper_title" in role_blob
        or (name == "h1" and _typography_tag_in_identity_header(tag))
    ):
        return "title"
    if any(
        token in role_blob
        for token in ("subsection-heading", "subsection-title", "inline-colored-label")
    ):
        return "subsection_heading"
    if name in {"h1", "h2"} or "section-heading" in role_blob or "section-title" in role_blob:
        return "section_heading"
    if name in {"h3", "h4", "h5", "h6"}:
        return "label"
    if any(token in role_blob for token in ("authors", "affiliation", "venue", "identity", "meta-line", "project-link")):
        return "identity_meta"
    if kind == "table" or name in {"table", "thead", "tbody", "tr", "td", "th"} or "table" in role_blob:
        return "table_text"
    if kind == "caption" or "caption" in role_blob or name == "figcaption":
        return "caption"
    if any(token in role_blob for token in ("label", "axis", "legend")):
        return "label"
    if any(token in role_blob for token in ("readout", "takeaway", "note", "interpret")):
        return "readout"
    if kind == "metric" or re.fullmatch(r"[\d.,%+/-]+", text or ""):
        if any(token in role_blob for token in ("label", "axis", "legend")):
            return "label"
        return "metric_value"
    if any(token in role_blob for token in ("metric-label", "stat-label", "result-label")):
        return "label"
    if any(token in role_blob for token in ("metric-value", "stat-value", "result-value")):
        return "metric_value"
    return "body"


def _typography_tag_in_identity_header(tag: Tag) -> bool:
    node: Tag | None = tag
    while isinstance(node, Tag):
        if _is_identity_header_container(node):
            return True
        classes = _class_tokens(node)
        if classes & {"poster-header", "identity-header", "paper-header", "title-meta", "title-cluster"}:
            return True
        name = str(node.name or "").lower()
        if name in {"main", "body", "html"}:
            break
        node = node.parent if isinstance(node.parent, Tag) else None
    return False


def _font_family_for_tag(tag: Tag, bboxes: dict[str, dict[str, int]]) -> str:
    node: Tag | None = tag
    while isinstance(node, Tag):
        block_id = str(node.get("data-block-id") or "").strip()
        measured = bboxes.get(block_id) if block_id and isinstance(bboxes.get(block_id), dict) else None
        style = measured.get("_computed_style") if isinstance(measured, dict) and isinstance(measured.get("_computed_style"), dict) else {}
        family = str(style.get("font_family") or "").strip()
        if family:
            return family
        node = node.parent if isinstance(node.parent, Tag) else None
    return ""


def _is_academic_serif_family(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return True
    first = raw.split(",")[0].strip().strip("'\"").lower()
    return first in {"times new roman", "times", "georgia", "serif"} or "times new roman" in first


def _is_times_new_roman_family(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    first = raw.split(",")[0].strip().strip("'\"").lower()
    return first == "times new roman" or "times new roman" in first


def _font_weight_number(value: Any) -> float:
    raw = str(value or "").strip().lower()
    if not raw:
        return 0.0
    if raw == "normal":
        return 400.0
    if raw == "bold":
        return 700.0
    return _safe_float(raw)


def _median_numeric(values: list[float]) -> float:
    ordered = sorted(float(value) for value in values if float(value) > 0)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _first_typography_sample(samples: list[dict[str, Any]], roles: set[str]) -> dict[str, Any] | None:
    return next((item for item in samples if str(item.get("role") or "") in roles), None)


def _typography_issue(
    sample: dict[str, Any],
    failure_kind: str,
    expected: str,
    aggregate: dict[str, Any],
    *,
    severity: str = "hard",
    soft_finalizable: bool = False,
) -> dict[str, Any]:
    return {
        "failure_kind": failure_kind,
        "role": sample.get("role"),
        "block_id": sample.get("block_id"),
        "actual_font_family": sample.get("actual_font_family"),
        "actual_font_size_px": sample.get("actual_font_size_px"),
        "actual_font_weight": sample.get("actual_font_weight"),
        "actual_line_height": sample.get("actual_line_height"),
        "expected": expected,
        "sample_text": sample.get("sample_text"),
        "severity": severity,
        "soft_finalizable": soft_finalizable,
        **aggregate,
    }


def _paper_poster_editorial_lead_key_diagnostics(
    soup: BeautifulSoup,
    ctx: ToolContext,
) -> list[dict[str, Any]]:
    if not _dogfood_dense_mode(ctx):
        return []
    if not _editorial_flow_mode(ctx) and soup.select_one(".paper-poster,.editorial-poster") is None:
        return []

    body_items = _editorial_lead_key_body_items(soup)
    body_word_count = sum(_visible_word_count(item.get_text(" ", strip=True)) for item in body_items)
    if body_word_count < _EDITORIAL_LEAD_KEY_MIN_BODY_WORDS or len(body_items) < _EDITORIAL_LEAD_KEY_MIN_BODY_ITEMS:
        return []

    lead_keys: list[dict[str, Any]] = []
    scattered_strongs: list[dict[str, Any]] = []
    mostly_strong_items: list[dict[str, Any]] = []
    strong_word_count = 0
    strong_count = 0
    for item in body_items:
        item_words = _visible_word_count(item.get_text(" ", strip=True))
        item_strong_words = 0
        for detail in _editorial_strong_details_for_body_item(item):
            strong_count += 1
            word_count = _safe_int(detail.get("word_count"))
            strong_word_count += word_count
            item_strong_words += word_count
            if detail.get("is_short_leading"):
                lead_keys.append(detail)
            elif not detail.get("is_leading"):
                scattered_strongs.append(detail)
        if item_words >= 8 and item_strong_words / max(1, item_words) >= 0.65:
            mostly_strong_items.append({
                "container": _tag_label(item),
                "word_count": item_words,
                "strong_word_count": item_strong_words,
                "strong_word_ratio": round(item_strong_words / max(1, item_words), 3),
                "sample_text": item.get_text(" ", strip=True)[:180],
            })

    strong_word_ratio = round(strong_word_count / max(1, body_word_count), 3)
    diagnostics: list[dict[str, Any]] = []
    if not lead_keys:
        diagnostics.append({
            "issue_id": "paper_poster_editorial_lead_keys_missing",
            "diagnostic_only": True,
            "severity": "advisory",
            "body_item_count": len(body_items),
            "body_word_count": body_word_count,
            "lead_key_count": 0,
            "hint": (
                "Dense editorial paper posters should use sparse short sentence-start "
                '<strong class="lead-key">...</strong> phrases to lead key claims. '
                "Keep them inline and brief; do not bold whole paragraphs."
            ),
        })

    if (
        mostly_strong_items
        or len(scattered_strongs) >= _EDITORIAL_LEAD_KEY_SCATTERED_STRONG_COUNT
        or (
            strong_count >= _EDITORIAL_LEAD_KEY_SCATTERED_STRONG_COUNT
            and strong_word_ratio > _EDITORIAL_LEAD_KEY_STRONG_WORD_RATIO_MAX
        )
    ):
        diagnostics.append({
            "issue_id": "paper_poster_editorial_lead_keys_overused",
            "diagnostic_only": True,
            "severity": "advisory",
            "body_item_count": len(body_items),
            "body_word_count": body_word_count,
            "strong_count": strong_count,
            "scattered_strong_count": len(scattered_strongs),
            "strong_word_count": strong_word_count,
            "strong_word_ratio": strong_word_ratio,
            "mostly_strong_items": mostly_strong_items[:6],
            "scattered_strongs": scattered_strongs[:8],
            "hint": (
                "Use lead-key emphasis only for short sentence-start phrases. "
                "Mostly-bold body blocks and scattered mid-sentence bold fragments should be returned to regular weight; "
                "the typography gate still rejects broad bold body copy through body_weight_too_heavy."
            ),
        })
    return diagnostics


def _record_editorial_lead_key_diagnostics(
    ctx: ToolContext,
    diagnostics: list[dict[str, Any]],
) -> None:
    if diagnostics:
        ctx.state["paper_poster_html_editorial_lead_key_diagnostics"] = diagnostics
        log(
            "paper_poster_html.editorial_lead_key_diagnostic",
            issue_count=len(diagnostics),
            issue_ids=[str(item.get("issue_id") or "") for item in diagnostics],
            first_issues=diagnostics[:3],
        )
    else:
        ctx.state.pop("paper_poster_html_editorial_lead_key_diagnostics", None)


def _attach_editorial_lead_key_diagnostics(
    result: ToolResultRecord,
    diagnostics: list[dict[str, Any]],
) -> None:
    if not diagnostics or not isinstance(result.payload, dict):
        return
    result.payload["paper_poster_html_editorial_lead_key_diagnostics"] = diagnostics


def _editorial_lead_key_body_items(soup: BeautifulSoup) -> list[Tag]:
    items: list[Tag] = []
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        if not _is_editorial_lead_key_body_item(tag):
            continue
        if _visible_word_count(tag.get_text(" ", strip=True)) < 6:
            continue
        if _has_editorial_lead_key_body_descendant(tag):
            continue
        items.append(tag)
    return items[:120]


def _has_editorial_lead_key_body_descendant(tag: Tag) -> bool:
    for descendant in tag.find_all(True):
        if not isinstance(descendant, Tag):
            continue
        if _is_editorial_lead_key_body_item(descendant) and _visible_word_count(descendant.get_text(" ", strip=True)) >= 6:
            return True
    return False


def _is_editorial_lead_key_body_item(tag: Tag) -> bool:
    if _skip_typography_tag(tag):
        return False
    name = str(tag.name or "").lower()
    if name in {"h1", "h2", "h3", "h4", "h5", "h6", "header"}:
        return False
    kind = _infer_block_kind(tag)
    role = _paper_typography_role(tag, kind)
    if role not in _TYPOGRAPHY_BODY_LIKE_ROLES:
        return False
    role_blob = _semantic_role_blob(tag, include_ancestors=True)
    if any(
        token in role_blob
        for token in (
            "identity",
            "poster-header",
            "title",
            "heading",
            "caption",
            "formula",
            "equation",
            "math",
            "katex",
            "source-asset",
            "flow-asset",
            "source-frame",
            "figure-frame",
            "legend",
        )
    ):
        return False
    if name in _EDITORIAL_LEAD_KEY_BODY_TAGS:
        return True
    if name not in {"div", "span"}:
        return False
    role_blob = _semantic_role_blob(tag, include_ancestors=False)
    return any(
        token in role_blob
        for token in (
            "body", "prose", "readout", "takeaway", "note", "interpret", "caption", "table",
        )
    )


def _editorial_strong_details_for_body_item(item: Tag) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    item_words = _visible_word_count(item.get_text(" ", strip=True))
    for tag in item.find_all(True):
        if not isinstance(tag, Tag):
            continue
        if not _is_editorial_lead_key_emphasis_tag(tag):
            continue
        if _has_editorial_lead_key_emphasis_ancestor(tag, stop=item):
            continue
        detail = _editorial_lead_key_detail_for_emphasis(item, tag, item_words=item_words)
        if detail:
            details.append(detail)
    return details


def _has_editorial_lead_key_emphasis_ancestor(tag: Tag, *, stop: Tag) -> bool:
    node = tag.parent
    while isinstance(node, Tag) and node is not stop:
        if _is_editorial_lead_key_emphasis_tag(node):
            return True
        node = node.parent
    return False


def _is_short_inline_lead_key_tag(tag: Tag) -> bool:
    if not _is_editorial_lead_key_emphasis_tag(tag):
        return False
    container = _nearest_editorial_lead_key_body_item(tag)
    if container is None:
        return False
    detail = _editorial_lead_key_detail_for_emphasis(container, tag)
    return bool(detail and detail.get("is_short_leading"))


def _nearest_editorial_lead_key_body_item(tag: Tag) -> Tag | None:
    node = tag.parent
    while isinstance(node, Tag):
        if _is_editorial_lead_key_body_item(node):
            return node
        node = node.parent
    return None


def _editorial_lead_key_detail_for_emphasis(
    container: Tag,
    emphasis: Tag,
    *,
    item_words: int | None = None,
) -> dict[str, Any] | None:
    if emphasis is container:
        return None
    text = emphasis.get_text(" ", strip=True)
    word_count = _visible_word_count(text)
    if word_count <= 0:
        return None
    container_words = item_words if item_words is not None else _visible_word_count(container.get_text(" ", strip=True))
    if container_words <= 0:
        return None
    preceding_text = _text_before_descendant(container, emphasis)
    is_leading = _visible_word_count(preceding_text) == 0
    is_short = (
        word_count <= _EDITORIAL_LEAD_KEY_MAX_WORDS
        and word_count < container_words
        and word_count / max(1, container_words) <= 0.4
    )
    classes = _class_tokens(emphasis)
    has_lead_key_class = "lead-key" in classes or "lead_key" in classes
    name = str(emphasis.name or "").lower()
    return {
        "tag": name,
        "container": _tag_label(container),
        "has_lead_key_class": has_lead_key_class,
        "is_leading": is_leading,
        "is_short_leading": bool(is_leading and is_short and (has_lead_key_class or name in {"strong", "b"})),
        "word_count": word_count,
        "container_word_count": container_words,
        "sample_text": text[:120],
        "container_sample_text": container.get_text(" ", strip=True)[:180],
    }


def _is_editorial_lead_key_emphasis_tag(tag: Tag) -> bool:
    name = str(tag.name or "").lower()
    classes = _class_tokens(tag)
    return name in {"strong", "b"} or "lead-key" in classes or "lead_key" in classes


def _text_before_descendant(container: Tag, target: Tag) -> str:
    chunks: list[str] = []
    found = False

    def walk(node: Any) -> None:
        nonlocal found
        if found:
            return
        if node is target:
            found = True
            return
        if isinstance(node, NavigableString):
            chunks.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        for child in node.children:
            walk(child)
            if found:
                return

    walk(container)
    return " ".join(" ".join(chunks).split())


def _source_visual_size_error(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    ctx: ToolContext,
) -> ToolResultRecord | None:
    if not _dogfood_dense_mode(ctx):
        return None
    required = set(_source_flow_required_ids(ctx))
    required.update(source_id for source_id in _placed_source_ids(soup, ctx) if _is_source_visual_id(source_id))
    if not required:
        return None
    issues: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw_tag in soup.find_all(["figure", "img", "table"]):
        if not isinstance(raw_tag, Tag):
            continue
        tag = _source_wrap_tag(raw_tag)
        if id(tag) in seen:
            continue
        source_id = (
            _source_id_for_tag(tag, ctx)
            or _source_id_for_tag(raw_tag, ctx)
            or _source_id_for_tag_or_descendant(tag, ctx)
        )
        if source_id not in required:
            continue
        if source_id.startswith("ingest_table_") and not _is_bound_source_table_crop_tag(tag, ctx):
            continue
        panel = _nearest_source_wrap_panel(tag)
        if panel is None:
            continue
        if _is_identity_header_source_asset(tag, panel, source_id, ctx):
            seen.add(id(tag))
            continue
        seen.add(id(tag))
        block_id = str(tag.get("data-block-id") or raw_tag.get("data-block-id") or "").strip()
        panel_id = str(panel.get("data-block-id") or panel.get("data-slot-id") or "").strip()
        source_bbox = _bbox_only(_bbox_for_tag(tag, bboxes)) or _bbox_only(_bbox_for_tag(raw_tag, bboxes))
        panel_measurement = _bbox_for_tag(panel, bboxes)
        panel_bbox = _bbox_only(panel_measurement)
        if source_bbox is None or panel_bbox is None:
            continue
        panel_w = max(1, int(panel_bbox.get("w") or 0))
        panel_h = max(1, int(panel_bbox.get("h") or 0))
        panel_metrics = (
            panel_measurement.get("_layout_metrics")
            if isinstance(panel_measurement, dict) and isinstance(panel_measurement.get("_layout_metrics"), dict)
            else {}
        )
        panel_client_h = _safe_float(panel_metrics.get("client_height_px") or panel_h)
        panel_scroll_h = _safe_float(panel_metrics.get("scroll_height_px") or panel_client_h)
        panel_scroll_overflow = max(0, int(round(panel_scroll_h - panel_client_h)))
        source_w = max(0, int(source_bbox.get("w") or 0))
        source_h = max(0, int(source_bbox.get("h") or 0))
        width_ratio = source_w / panel_w
        classes = _source_visual_class_tokens(tag, raw_tag)
        source_area_ratio = (source_w * source_h) / max(1, panel_w * panel_h)
        intrinsic_aspect = _source_aspect_for_id(source_id, ctx, src=_source_visual_src_for_tag(tag, raw_tag))
        contain_metrics = _source_visual_contain_fit_metrics(
            source_w,
            source_h,
            intrinsic_aspect,
        )
        min_height, height_basis = _source_visual_min_height_requirement(
            tag,
            source_id,
            classes,
            panel_width_px=panel_w,
            intrinsic_aspect=intrinsic_aspect,
        )
        min_area_ratio = _source_visual_min_area_ratio(tag, source_id, classes)
        min_width_ratio = _source_visual_min_width_ratio(tag, source_id, classes)
        rendered_height = (
            int(contain_metrics.get("object_fit_rendered_height_px") or source_h)
            if contain_metrics
            else source_h
        )
        contain_fill_ok = True
        if contain_metrics:
            contain_fill_ok = (
                contain_metrics["object_fit_width_fill_ratio"] >= _MIN_SOURCE_VISUAL_OBJECT_FIT_FILL_RATIO
                and contain_metrics["object_fit_height_fill_ratio"] >= _MIN_SOURCE_VISUAL_OBJECT_FIT_FILL_RATIO
                and contain_metrics["object_fit_area_fill_ratio"] >= _MIN_SOURCE_VISUAL_OBJECT_FIT_AREA_RATIO
            )
        size_reasons: list[str] = []
        if width_ratio < min_width_ratio:
            size_reasons.append("width_ratio_low")
        if rendered_height < min_height:
            size_reasons.append("height_low")
        if source_area_ratio < min_area_ratio:
            size_reasons.append("area_ratio_low")
        if not contain_fill_ok:
            size_reasons.extend(["contain_wrapper_underfilled", "wrapper_aspect_mismatch"])
        same_flow_fill = _source_visual_same_flow_fill_metrics(
            tag,
            raw_tag,
            panel,
            source_bbox,
            bboxes,
            ctx,
        )
        wide_or_table = _source_visual_is_wide_or_table(tag, source_id, classes)
        width_only_readable = (
            size_reasons == ["width_ratio_low"]
            and not wide_or_table
            and source_w >= _MIN_SOURCE_VISUAL_READABLE_WIDTH_PX
            and rendered_height >= min_height
            and source_area_ratio >= min_area_ratio
            and contain_fill_ok
            and width_ratio >= _MIN_SOURCE_VISUAL_FLOW_FILL_MIN_WIDTH_RATIO
        )
        sidecar_issue = _source_visual_sidecar_fill_issue(
            tag,
            raw_tag,
            panel,
            source_bbox,
            bboxes,
            ctx,
        )
        if width_only_readable and same_flow_fill.get("same_flow_fill_pass") and not sidecar_issue:
            continue
        size_ok = (
            width_ratio >= min_width_ratio
            and rendered_height >= min_height
            and source_area_ratio >= min_area_ratio
            and contain_fill_ok
        )
        if size_ok and not sidecar_issue:
            continue
        sidecar_reasons = (
            sidecar_issue.get("reasons") if isinstance(sidecar_issue, dict) else []
        )
        reasons = [*size_reasons, *[str(reason) for reason in sidecar_reasons]]
        failure_kind = "source_visual_too_small"
        if sidecar_issue and not size_reasons:
            failure_kind = str(sidecar_issue.get("failure_kind") or "source_visual_sidecar_underfilled")
        elif width_only_readable:
            failure_kind = "source_visual_flow_underfilled"
        issue = {
            "failure_kind": failure_kind,
            "source_id": source_id,
            "block_id": block_id,
            "panel_id": panel_id,
            "actual_wrapper_label": _tag_label(tag),
            "source_width_px": source_w,
            "source_height_px": source_h,
            "panel_width_px": panel_w,
            "panel_height_px": panel_h,
            "panel_client_height_px": round(panel_client_h, 2),
            "panel_scroll_height_px": round(panel_scroll_h, 2),
            "panel_scroll_overflow_px": panel_scroll_overflow,
            "source_panel_width_ratio": round(width_ratio, 3),
            "source_panel_area_ratio": round(source_area_ratio, 3),
            "required_panel_width_ratio": min_width_ratio,
            "required_source_height_px": min_height,
            "rendered_source_height_px": rendered_height,
            "height_basis": height_basis,
            "required_source_area_ratio": min_area_ratio,
            "required_object_fit_fill_ratio": _MIN_SOURCE_VISUAL_OBJECT_FIT_FILL_RATIO,
            "required_object_fit_area_ratio": _MIN_SOURCE_VISUAL_OBJECT_FIT_AREA_RATIO,
            "reasons": reasons,
            "classes": sorted(classes),
            "width_only_readable_visual": width_only_readable,
            "same_flow_fill_metrics": same_flow_fill,
            "allowed_filler_block_ids": _source_visual_allowed_filler_block_ids(panel, tag),
        }
        if sidecar_issue:
            for key, value in sidecar_issue.items():
                if key not in {"failure_kind", "reasons"}:
                    issue[key] = value
        if contain_metrics:
            issue.update(contain_metrics)
            if "contain_wrapper_underfilled" in reasons:
                issue["failure_kind"] = "contain_wrapper_underfilled"
        issue.update(_source_visual_repair_contract(issue))
        issues.append({
            **issue,
        })
    if not issues:
        return None
    blank_fill_plan = _blank_fill_plan_from_source_visual_issues(issues, soup, bboxes, _default_canvas(ctx))
    required_blank_fill_targets = (
        _required_blank_fill_targets(blank_fill_plan)
        if isinstance(blank_fill_plan, dict) and blank_fill_plan
        else []
    )
    hard_issues = [
        issue for issue in issues
        if issue.get("blocks_soft_accept") is True
        or str(issue.get("severity") or "hard") not in {"advisory", "near_miss", "polish"}
        or issue.get("soft_finalizable") is not True
    ]
    soft_finalizable = bool(issues) and not hard_issues
    return obs_error(
        "propose_paper_poster_html found source figures/tables rendered too small for a conference poster.",
        category="validation",
        payload={
            "issue_id": "paper_poster_html_source_visual_too_small",
            "repair_route": "use_large_source_asset_flow_units",
            "severity": "near_miss" if soft_finalizable else "hard",
            "soft_finalizable": soft_finalizable,
            "blocks_soft_accept": not soft_finalizable,
            "near_miss_issue_count": len(issues) - len(hard_issues),
            "hard_issue_count": len(hard_issues),
            "issues": issues[:12],
            "blank_fill_plan": blank_fill_plan if blank_fill_plan else None,
            "required_blank_fill_targets": required_blank_fill_targets[:12],
            "blank_fill_required": bool(required_blank_fill_targets),
            "required_co_repair": (
                {
                    "blank_fill": {
                        **blank_fill_plan,
                        "targets": required_blank_fill_targets[:12],
                        "required_targets": required_blank_fill_targets[:12],
                        "blank_fill_required": True,
                    },
                    "reason": "readable source visual has required same-flow blank fill targets",
                }
                if blank_fill_plan and required_blank_fill_targets else None
            ),
            "hint": (
                "Make paper figures and source table crops the panel subjects while preserving the "
                "current composition. Do not fix canvas overflow by turning source visuals into tiny "
                "strips or by placing them in wide/short empty wrappers. Restore readable visual heights "
                "and wrapper aspect first, then shorten low-value local prose and reduce section gaps/padding "
                "to fit. Use `.asset-medium` around 48-58% width for "
                "secondary support visuals, `.asset-large` around 60-68% for primary wrapped figures, "
                "and real `.asset-wide`/table layouts at panel width, not centered narrow strips. If a "
                "floated source visual has only a short side readout, make it full-width/stacked or add "
                "direct source-backed text/native rows in the same flow unit; do not leave a blank "
                "sidecar lane."
            ),
        },
    )


def _source_visual_repair_contract(issue: dict[str, Any]) -> dict[str, Any]:
    failure_kind = str(issue.get("failure_kind") or "")
    reasons = {str(reason) for reason in (issue.get("reasons") or [])}
    same_flow_fill = (
        issue.get("same_flow_fill_metrics")
        if isinstance(issue.get("same_flow_fill_metrics"), dict)
        else {}
    )
    width_gap = max(
        0.0,
        _safe_float(issue.get("required_panel_width_ratio")) - _safe_float(issue.get("source_panel_width_ratio")),
    )
    height_gap = max(
        0.0,
        _safe_float(issue.get("required_source_height_px")) - _safe_float(issue.get("rendered_source_height_px")),
    )
    area_gap = max(
        0.0,
        _safe_float(issue.get("required_source_area_ratio")) - _safe_float(issue.get("source_panel_area_ratio")),
    )
    if issue.get("object_fit_width_fill_ratio") in (None, "") and issue.get("object_fit_area_fill_ratio") in (None, ""):
        object_fit_gap = 0.0
    else:
        object_fit_gap = max(
            0.0,
            max(
                _safe_float(issue.get("required_object_fit_fill_ratio")) - _safe_float(issue.get("object_fit_width_fill_ratio")),
                _safe_float(issue.get("required_object_fit_area_ratio")) - _safe_float(issue.get("object_fit_area_fill_ratio")),
            ),
        )
    threshold_gap_is_minor = (
        width_gap <= 0.03
        and height_gap <= 6
        and area_gap <= 0.015
        and object_fit_gap <= 0.03
    )
    target_problem = "true_too_small_visual"
    repair_intent = "restore_readable_source_visual_size"
    primary_action = "resize_source_visual_to_required_ratios"
    recommended_first_action = "Restore the source visual's required width/area/object-fit ratios while preserving source aspect ratio."
    acceptance_mode = "hard_visual_readability"
    same_flow_fill_required = False
    do_not_fix_by = ["shrinking_body_type", "cropping_source_visual", "oversized_empty_wrapper"]
    severity = "hard"
    soft_finalizable = False
    blocks_soft_accept = True
    if failure_kind == "source_visual_sidecar_underfilled" or reasons & {"side_readout_too_thin", "side_text_coverage_low"}:
        target_problem = "blank_sidecar_lane"
        repair_intent = "fill_source_flow_sidecar_with_evidence"
        primary_action = "fill_same_source_flow_unit_with_source_backed_readout"
        acceptance_mode = "same_flow_fill"
        same_flow_fill_required = True
        recommended_first_action = (
            "Add concise source-backed local readout/native rows as direct siblings in the same source-flow-unit, "
            "or make the asset stacked/full-width and place the readout below it."
        )
        do_not_fix_by.extend(["tiny_css_width_nudge_only", "widen_empty_wrapper", "global_column_or_row_rewrite"])
    elif failure_kind == "contain_wrapper_underfilled" or "contain_wrapper_underfilled" in reasons:
        contain_only_reasons = reasons <= {"contain_wrapper_underfilled", "wrapper_aspect_mismatch"}
        readable_wrapper_geometry = (
            _safe_float(issue.get("source_width_px"), default=0.0) >= _MIN_SOURCE_VISUAL_READABLE_WIDTH_PX
            and _safe_float(issue.get("rendered_source_height_px"), default=0.0) >= _safe_float(issue.get("required_source_height_px"), default=0.0)
            and _safe_float(issue.get("source_panel_area_ratio"), default=0.0) >= _safe_float(issue.get("required_source_area_ratio"), default=0.0)
            and _safe_float(issue.get("source_panel_width_ratio"), default=0.0) >= _MIN_SOURCE_VISUAL_FLOW_FILL_MIN_WIDTH_RATIO
            and _safe_int(issue.get("panel_scroll_overflow_px"), default=0) <= 0
        )
        object_fit_area = _safe_float(issue.get("object_fit_area_fill_ratio"), default=1.0)
        wrapper_polish_not_obvious_shell = object_fit_area >= _OBVIOUS_SOURCE_VISUAL_OBJECT_FIT_AREA_FAILURE_RATIO
        if contain_only_reasons and readable_wrapper_geometry and wrapper_polish_not_obvious_shell:
            target_problem = "readable_visual_wrapper_polish"
            repair_intent = "local_wrapper_polish_or_accept"
            primary_action = "preserve_readable_visual_and_reduce_empty_wrapper_if_safe"
            acceptance_mode = "wrapper_polish"
            recommended_first_action = (
                "Preserve the current readable source visual. If already touching this local unit, reduce the "
                "empty wrapper shell with a scoped aspect/spacing repair; otherwise treat it as polish."
            )
            do_not_fix_by.extend(["global_column_or_row_rewrite", "widen_empty_wrapper", "shrinking_body_type"])
        else:
            target_problem = "blank_wrapper_shell"
            repair_intent = "repair_blank_source_visual_wrapper_shell"
            primary_action = "repair_wrapper_aspect_or_fill_with_local_readout"
            acceptance_mode = "wrapper_fill"
            recommended_first_action = (
                "Eliminate the empty source wrapper shell by matching wrapper aspect to the source asset or by using "
                "a narrower flow unit with source-backed local readout filling the adjacent lane."
            )
            do_not_fix_by.extend(["tiny_css_width_nudge_only", "widen_empty_wrapper"])
    elif bool(issue.get("width_only_readable_visual")):
        target_problem = "readable_visual_flow_underfilled"
        repair_intent = "fill_readable_source_flow_blankness"
        acceptance_mode = "same_flow_fill"
        same_flow_fill_required = True
        severity = "near_miss"
        soft_finalizable = True
        blocks_soft_accept = False
        primary_action = (
            "fill_same_source_flow_unit_with_source_backed_readout"
            if same_flow_fill.get("same_flow_fill_available")
            else "create_direct_child_source_flow_unit_and_fill_blank_lane"
        )
        recommended_first_action = (
            "Preserve the current readable source visual size and fill the blank space with concise "
            "source-backed readout, native mini table rows, or mechanism bullets as direct "
            "siblings inside the same source-flow-unit."
        )
        do_not_fix_by.extend([
            "tiny_css_width_nudge_only",
            "global_column_or_row_rewrite",
            "changing_poster_columns",
            "moving_source_visual_to_unrelated_section",
        ])
    elif threshold_gap_is_minor:
        target_problem = "minor_geometry_gap"
        repair_intent = "resolve_minor_source_visual_threshold_gap"
        primary_action = "make_one_local_composition_fix_not_width_nudge"
        acceptance_mode = "local_composition"
        severity = "near_miss"
        soft_finalizable = True
        blocks_soft_accept = False
        recommended_first_action = "Prefer one local composition fix that also preserves density over a tiny CSS width nudge."
        do_not_fix_by.extend(["tiny_css_width_nudge_only", "global_column_or_row_rewrite"])
    target_scope = (
        "create_direct_child_source_flow_unit"
        if same_flow_fill_required and not same_flow_fill.get("same_flow_fill_available")
        else "existing_source_flow_unit"
        if same_flow_fill_required
        else "source_visual_geometry"
    )
    target_ids = _source_visual_target_block_ids(issue)
    allowed_selectors = _source_visual_allowed_selectors(issue)
    required_dom_shape = (
        "direct-child .figure-flow-unit/.source-flow-unit containing the source visual plus direct "
        "p/ul/table/div readout or compact comparison table rows"
    )
    must_not_regress_geometry = {
        key: issue.get(key)
        for key in (
            "source_width_px",
            "source_height_px",
            "source_panel_width_ratio",
            "source_panel_area_ratio",
            "rendered_source_height_px",
            "object_fit_width_fill_ratio",
            "object_fit_height_fill_ratio",
            "object_fit_area_fill_ratio",
        )
        if issue.get(key) not in (None, "", [], {})
    }
    same_flow_fill_targets: dict[str, Any] = {}
    for key in (
        "required_min_words",
        "required_min_side_text_coverage_ratio",
        "local_word_count",
        "side_text_coverage_ratio",
        "side_text_coverage_required",
        "words_pass",
        "side_text_coverage_pass",
    ):
        value = same_flow_fill.get(key) if key in same_flow_fill else issue.get(key)
        if value not in (None, "", [], {}):
            same_flow_fill_targets[key] = value
    allowed_filler_block_ids = _unique_nonempty([
        str(value)
        for value in (issue.get("allowed_filler_block_ids") or [])
        if str(value or "").strip()
    ])
    if allowed_filler_block_ids:
        same_flow_fill_targets["allowed_filler_block_ids"] = allowed_filler_block_ids
    result = {
        "target_problem": target_problem,
        "repair_intent": repair_intent,
        "primary_repair_action": primary_action,
        "recommended_first_action": recommended_first_action,
        "acceptance_mode": acceptance_mode,
        "target_scope": target_scope,
        "preserve_current_visual_size": bool(same_flow_fill_required),
        "required_dom_shape": required_dom_shape if same_flow_fill_required else None,
        "same_flow_fill_required": same_flow_fill_required,
        "same_flow_fill_metrics": same_flow_fill,
        "same_flow_fill_targets": same_flow_fill_targets if same_flow_fill_required else {},
        "must_not_regress_geometry": must_not_regress_geometry,
        "source_id": issue.get("source_id"),
        "panel_id": issue.get("panel_id"),
        "flow_unit_id": same_flow_fill.get("flow_unit_id") or issue.get("flow_unit_id"),
        "asset_block_id": same_flow_fill.get("asset_block_id") or issue.get("asset_block_id") or issue.get("block_id"),
        "allowed_filler_block_ids": allowed_filler_block_ids,
        "readable_visual_geometry": {
            "width_only_readable_visual": bool(issue.get("width_only_readable_visual")),
            "source_width_px": issue.get("source_width_px"),
            "min_readable_width_px": _MIN_SOURCE_VISUAL_READABLE_WIDTH_PX,
            "source_panel_width_ratio": issue.get("source_panel_width_ratio"),
            "required_panel_width_ratio": issue.get("required_panel_width_ratio"),
            "source_panel_area_ratio": issue.get("source_panel_area_ratio"),
            "required_source_area_ratio": issue.get("required_source_area_ratio"),
            "rendered_source_height_px": issue.get("rendered_source_height_px"),
            "required_source_height_px": issue.get("required_source_height_px"),
            "object_fit_area_fill_ratio": issue.get("object_fit_area_fill_ratio"),
            "object_fit_area_obvious_failure_threshold": _OBVIOUS_SOURCE_VISUAL_OBJECT_FIT_AREA_FAILURE_RATIO,
        },
        "target_block_ids": target_ids,
        "allowed_selectors": allowed_selectors,
        "forbidden_selectors": [
            ".poster-columns",
            ".poster-column",
            ".poster-header",
            '[data-panel-role="identity_header"]',
        ],
        "threshold_gap": {
            "panel_width_ratio": round(width_gap, 3),
            "source_height_px": round(height_gap, 3),
            "source_area_ratio": round(area_gap, 3),
            "object_fit_ratio": round(object_fit_gap, 3),
        },
        "threshold_gap_is_minor": threshold_gap_is_minor,
        "do_not_fix_by": do_not_fix_by,
        "severity": severity,
        "soft_finalizable": soft_finalizable,
        "blocks_soft_accept": blocks_soft_accept,
    }
    if target_problem == "readable_visual_wrapper_polish":
        result.update({
            "severity": "near_miss",
            "soft_finalizable": True,
            "blocks_soft_accept": False,
        })
    return result


def _source_visual_target_block_ids(issue: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    same_flow_fill = (
        issue.get("same_flow_fill_metrics")
        if isinstance(issue.get("same_flow_fill_metrics"), dict)
        else {}
    )
    if issue.get("same_flow_fill_required") and not same_flow_fill.get("same_flow_fill_available"):
        for key in ("panel_id",):
            value = str(issue.get(key) or "").strip()
            if value:
                ids.append(value)
        return _unique_nonempty(ids)
    for key in ("flow_unit_id", "asset_block_id", "block_id", "panel_id"):
        value = str(same_flow_fill.get(key) or issue.get(key) or "").strip()
        if value:
            ids.append(value)
    return _unique_nonempty(ids)


def _source_visual_allowed_selectors(issue: dict[str, Any]) -> list[str]:
    selectors: list[str] = []
    same_flow_fill = (
        issue.get("same_flow_fill_metrics")
        if isinstance(issue.get("same_flow_fill_metrics"), dict)
        else {}
    )
    if issue.get("same_flow_fill_required") and not same_flow_fill.get("same_flow_fill_available"):
        panel_id = str(issue.get("panel_id") or "").strip()
        source_id = str(issue.get("source_id") or "").strip()
        if panel_id:
            selectors.append(f'[data-block-id="{panel_id}"]')
        if source_id:
            selectors.append(f'[data-source-id="{source_id}"]')
        return _unique_nonempty(selectors)
    for key in ("flow_unit_id", "asset_block_id", "block_id", "panel_id"):
        value = str(same_flow_fill.get(key) or issue.get(key) or "").strip()
        if value:
            selectors.append(f'[data-block-id="{value}"]')
    return _unique_nonempty(selectors)


def _source_visual_allowed_filler_block_ids(panel: Tag, source_tag: Tag) -> list[str]:
    ids: list[str] = []
    source_ids = {
        str(source_tag.get("data-block-id") or "").strip(),
        str(source_tag.get("data-source-id") or "").strip(),
        str(source_tag.get("data-layer-id") or "").strip(),
    }
    for tag in panel.find_all(True):
        if not isinstance(tag, Tag) or tag is source_tag:
            continue
        block_id = str(tag.get("data-block-id") or "").strip()
        if not block_id or block_id in source_ids:
            continue
        if tag.find(attrs={"data-block-id": str(source_tag.get("data-block-id") or "")}):
            continue
        classes = _class_tokens(tag)
        name = str(tag.name or "").lower()
        if (
            name in {"p", "ul", "ol", "table"}
            or classes & {"readout", "small-readout", "metric-strip", "metric", "process-row", "native-table", "formula"}
        ):
            ids.append(block_id)
    return _unique_nonempty(ids)


def _source_visual_is_wide_or_table(tag: Tag, source_id: str, classes: set[str]) -> bool:
    return (
        str(tag.get("data-block-kind") or "").strip().lower() == "table"
        or str(source_id or "").startswith("ingest_table_")
        or "source-table" in classes
        or _has_asset_size_class(classes, "asset-wide")
    )


def _source_visual_same_flow_fill_metrics(
    tag: Tag,
    raw_tag: Tag,
    panel: Tag,
    source_bbox: dict[str, int],
    bboxes: dict[str, dict[str, int]],
    ctx: ToolContext,
) -> dict[str, Any]:
    unit = _source_flow_unit_for_tag(tag, panel)
    if not isinstance(unit, Tag) or not _is_source_flow_unit(unit):
        return {
            "same_flow_fill_available": False,
            "same_flow_fill_pass": False,
            "reason": "missing_source_flow_unit",
        }
    unit_bbox = _bbox_only(_bbox_for_tag(unit, bboxes))
    if not unit_bbox:
        return {
            "same_flow_fill_available": False,
            "same_flow_fill_pass": False,
            "flow_unit_id": str(unit.get("data-block-id") or ""),
            "reason": "missing_source_flow_unit_bbox",
        }
    unit_w = max(1, int(unit_bbox.get("w") or 0))
    source_w = max(0, int(source_bbox.get("w") or 0))
    width_ratio = source_w / float(unit_w)
    classes = _source_flow_asset_classes(unit, tag, raw_tag)
    is_wide = _has_asset_size_class(classes, "asset-wide") or width_ratio >= 0.82
    min_words = 18 if is_wide else _MIN_SOURCE_VISUAL_SIDE_TEXT_WORDS
    if _has_asset_size_class(classes, "asset-large"):
        min_words = _MIN_SOURCE_VISUAL_LARGE_SIDE_TEXT_WORDS
    local_words = _visible_panel_word_count(unit, exclude=tag)
    side_coverage_required = (
        not is_wide
        and _MIN_SOURCE_VISUAL_FLOW_FILL_MIN_WIDTH_RATIO <= width_ratio <= 0.78
    )
    text_boxes = _source_flow_text_bboxes(unit, tag, bboxes, _default_canvas(ctx))
    side_coverage = _source_flow_side_text_coverage(unit_bbox, source_bbox, text_boxes)
    words_pass = local_words >= min_words
    side_pass = (
        not side_coverage_required
        or side_coverage >= _MIN_SOURCE_VISUAL_SIDE_TEXT_COVERAGE_RATIO
    )
    return {
        "same_flow_fill_available": True,
        "same_flow_fill_pass": bool(words_pass and side_pass),
        "flow_unit_id": str(unit.get("data-block-id") or ""),
        "asset_block_id": str(tag.get("data-block-id") or raw_tag.get("data-block-id") or ""),
        "flow_unit_width_px": int(unit_bbox.get("w") or 0),
        "flow_unit_height_px": int(unit_bbox.get("h") or 0),
        "source_flow_width_ratio": round(width_ratio, 3),
        "local_word_count": local_words,
        "required_min_words": min_words,
        "side_text_coverage_ratio": round(side_coverage, 3),
        "required_min_side_text_coverage_ratio": _MIN_SOURCE_VISUAL_SIDE_TEXT_COVERAGE_RATIO,
        "side_text_coverage_required": side_coverage_required,
        "words_pass": words_pass,
        "side_text_coverage_pass": side_pass,
    }


def _source_visual_sidecar_fill_issue(
    tag: Tag,
    raw_tag: Tag,
    panel: Tag,
    source_bbox: dict[str, int],
    bboxes: dict[str, dict[str, int]],
    ctx: ToolContext,
) -> dict[str, Any] | None:
    if not _source_in_editorial_flow(tag):
        return None
    unit = _source_flow_unit_for_tag(tag, panel)
    if not isinstance(unit, Tag) or not _is_source_flow_unit(unit):
        return None
    unit_bbox = _bbox_only(_bbox_for_tag(unit, bboxes))
    if not unit_bbox:
        return None
    unit_w = max(1, int(unit_bbox.get("w") or 0))
    source_w = max(0, int(source_bbox.get("w") or 0))
    width_ratio = source_w / float(unit_w)
    classes = _source_flow_asset_classes(unit, tag, raw_tag)
    is_wide = _has_asset_size_class(classes, "asset-wide") or width_ratio >= 0.82
    if is_wide or width_ratio < 0.36 or width_ratio > 0.78:
        return None
    local_words = _visible_panel_word_count(unit, exclude=tag)
    min_words = (
        _MIN_SOURCE_VISUAL_LARGE_SIDE_TEXT_WORDS
        if _has_asset_size_class(classes, "asset-large")
        else _MIN_SOURCE_VISUAL_SIDE_TEXT_WORDS
    )
    text_boxes = _source_flow_text_bboxes(unit, tag, bboxes, _default_canvas(ctx))
    side_coverage = _source_flow_side_text_coverage(unit_bbox, source_bbox, text_boxes)
    if (
        local_words >= min_words
        and side_coverage >= _MIN_SOURCE_VISUAL_SIDE_TEXT_COVERAGE_RATIO
    ):
        return None
    reasons: list[str] = []
    if local_words < min_words:
        reasons.append("side_readout_too_thin")
    if side_coverage < _MIN_SOURCE_VISUAL_SIDE_TEXT_COVERAGE_RATIO:
        reasons.append("side_text_coverage_low")
    return {
        "failure_kind": "source_visual_sidecar_underfilled",
        "flow_unit_id": str(unit.get("data-block-id") or ""),
        "flow_unit_width_px": int(unit_bbox.get("w") or 0),
        "flow_unit_height_px": int(unit_bbox.get("h") or 0),
        "source_flow_width_ratio": round(width_ratio, 3),
        "side_text_coverage_ratio": round(side_coverage, 3),
        "required_min_side_text_coverage_ratio": _MIN_SOURCE_VISUAL_SIDE_TEXT_COVERAGE_RATIO,
        "local_word_count": local_words,
        "required_min_words": min_words,
        "blank_sidecar_height_ratio": round(max(0.0, 1.0 - side_coverage), 3),
        "recommended_layout": (
            "Use asset-wide/stacked with float:none and readout below when local readout is short; "
            "only use a 36-78% floated source visual when direct sibling source-backed text/native "
            "rows cover the side lane."
        ),
        "reasons": reasons,
        "repair": (
            "Do not leave a half-panel blank lane beside a source figure/table. Either make the "
            "asset full-width/stacked with its readout below, or keep the float and add direct "
            "sibling source-backed prose/native rows until the side text visibly fills the lane."
        ),
    }


def _source_visual_min_width_ratio(tag: Tag, source_id: str, classes: set[str]) -> float:
    is_table = (
        str(tag.get("data-block-kind") or "").strip().lower() == "table"
        or str(source_id or "").startswith("ingest_table_")
        or "source-table" in classes
    )
    if _has_asset_size_class(classes, "asset-wide") or is_table:
        return _MIN_SOURCE_VISUAL_WIDE_PANEL_WIDTH_RATIO
    if _has_asset_size_class(classes, "asset-large"):
        return _MIN_SOURCE_VISUAL_LARGE_PANEL_WIDTH_RATIO
    return _MIN_SOURCE_VISUAL_PANEL_WIDTH_RATIO


def _source_visual_min_height_requirement(
    tag: Tag,
    source_id: str,
    classes: set[str],
    *,
    panel_width_px: int = 0,
    intrinsic_aspect: float = 0.0,
) -> tuple[int, str]:
    if panel_width_px > 0 and intrinsic_aspect >= 5.0:
        return (
            _clamp_int((panel_width_px / intrinsic_aspect) * 0.9, 96, 150),
            "ultrawide_aspect",
        )
    if panel_width_px > 0 and intrinsic_aspect >= 3.2:
        return (
            _clamp_int((panel_width_px / intrinsic_aspect) * 0.9, 120, 190),
            "wide_aspect",
        )
    is_table = (
        str(tag.get("data-block-kind") or "").strip().lower() == "table"
        or str(source_id or "").startswith("ingest_table_")
        or "source-table" in classes
    )
    if _has_asset_size_class(classes, "asset-wide") or is_table:
        return 210, "wide_class"
    if _has_asset_size_class(classes, "asset-large"):
        return 180, "large_class"
    if _has_asset_size_class(classes, "asset-medium"):
        return 140, "medium_class"
    return 130, "default_class"


def _clamp_int(value: float, lower: int, upper: int) -> int:
    return int(round(max(lower, min(upper, value))))


def _source_visual_min_area_ratio(tag: Tag, source_id: str, classes: set[str]) -> float:
    is_table = (
        str(tag.get("data-block-kind") or "").strip().lower() == "table"
        or str(source_id or "").startswith("ingest_table_")
        or "source-table" in classes
    )
    if _has_asset_size_class(classes, "asset-wide") or is_table:
        return 0.08
    if _has_asset_size_class(classes, "asset-large"):
        return 0.055
    if _has_asset_size_class(classes, "asset-medium"):
        return 0.035
    return 0.03


def _source_visual_class_tokens(tag: Tag, raw_tag: Tag) -> set[str]:
    classes = set(_class_tokens(tag))
    classes.update(_source_asset_ancestor_classes(tag))
    if raw_tag is not tag:
        classes.update(_class_tokens(raw_tag))
        classes.update(_source_asset_ancestor_classes(raw_tag))
    for child in tag.find_all(["img", "table"]):
        if isinstance(child, Tag):
            classes.update(_class_tokens(child))
            classes.update(_source_asset_ancestor_classes(child, stop=tag))
    return classes


def _source_id_for_tag_or_descendant(tag: Tag, ctx: ToolContext | None = None) -> str:
    source_id = _source_id_for_tag(tag, ctx)
    if source_id:
        return source_id
    for child in tag.find_all(["img", "table"]):
        if not isinstance(child, Tag):
            continue
        source_id = _source_id_for_tag(child, ctx)
        if source_id:
            return source_id
    return ""


def _source_visual_src_for_tag(tag: Tag, raw_tag: Tag) -> str:
    candidates: list[Tag] = []
    if isinstance(raw_tag, Tag):
        candidates.append(raw_tag)
    if raw_tag is not tag and isinstance(tag, Tag):
        candidates.append(tag)
    if isinstance(tag, Tag):
        candidates.extend(child for child in tag.find_all("img") if isinstance(child, Tag))
    for candidate in candidates:
        src = str(candidate.get("src") or "").strip()
        if src:
            return src
    return ""


def _source_visual_contain_fit_metrics(width_px: int, height_px: int, aspect: float) -> dict[str, Any] | None:
    if width_px <= 0 or height_px <= 0 or aspect <= 0:
        return None
    wrapper_aspect = width_px / float(height_px)
    if wrapper_aspect > aspect:
        rendered_height = float(height_px)
        rendered_width = rendered_height * aspect
    else:
        rendered_width = float(width_px)
        rendered_height = rendered_width / aspect
    width_fill = rendered_width / float(width_px)
    height_fill = rendered_height / float(height_px)
    area_fill = (rendered_width * rendered_height) / float(max(1, width_px * height_px))
    return {
        "intrinsic_aspect_ratio": round(aspect, 3),
        "wrapper_aspect_ratio": round(wrapper_aspect, 3),
        "object_fit_rendered_width_px": int(round(rendered_width)),
        "object_fit_rendered_height_px": int(round(rendered_height)),
        "object_fit_width_fill_ratio": round(width_fill, 3),
        "object_fit_height_fill_ratio": round(height_fill, 3),
        "object_fit_area_fill_ratio": round(area_fill, 3),
        "suggested_match_aspect_width_px": int(round(height_px * aspect)),
        "suggested_match_aspect_height_px": int(round(width_px / aspect)),
    }


def _class_tokens(tag: Tag) -> set[str]:
    classes = tag.get("class")
    if isinstance(classes, list):
        return {str(cls).strip().lower() for cls in classes if str(cls).strip()}
    return {item.strip().lower() for item in str(classes or "").split() if item.strip()}


def _source_wrap_tag(tag: Tag) -> Tag:
    if str(tag.name or "").lower() in {"figure", "table"}:
        return tag
    parent = tag.parent
    while isinstance(parent, Tag):
        name = str(parent.name or "").lower()
        if name == "figure":
            return parent
        if name in {"main", "body", "html"}:
            break
        parent = parent.parent
    return tag


def _nearest_source_wrap_panel(tag: Tag) -> Tag | None:
    candidates: list[Tag] = []
    parent: Tag | None = tag
    while isinstance(parent, Tag):
        name = str(parent.name or "").lower()
        if _source_wrap_panel_candidate(parent):
            candidates.append(parent)
        if name in {"main", "body", "html"}:
            break
        parent = parent.parent
    if not candidates:
        return None
    if _source_in_editorial_flow(tag):
        for candidate in candidates:
            if _is_editorial_section_tag(candidate):
                return candidate
    for candidate in reversed(candidates):
        if _is_outer_source_panel_root(candidate):
            return candidate
    return candidates[-1]


def _source_wrap_panel_candidate(tag: Tag) -> bool:
    name = str(tag.name or "").lower()
    classes = {str(cls) for cls in (tag.get("class") or [])}
    return bool(
        tag.get("data-panel-role")
        or tag.get("data-slot-id")
        or "flow-panel" in classes
        or (name in {"article", "section", "aside"} and tag.get("data-block-id"))
    )


def _is_outer_source_panel_root(tag: Tag) -> bool:
    name = str(tag.name or "").lower()
    classes = {str(cls) for cls in (tag.get("class") or [])}
    if name not in {"article", "section", "aside", "div"}:
        return False
    if not ({"flow-panel", "panel"} & classes or tag.get("data-panel-role") or tag.get("data-slot-id")):
        return False
    parent = tag.parent
    while isinstance(parent, Tag):
        parent_classes = {str(cls) for cls in (parent.get("class") or [])}
        if "poster-grid" in parent_classes or str(parent.get("data-layout-region") or "") == "main_panels":
            return True
        if str(parent.name or "").lower() in {"main", "body", "html"}:
            return False
        if _source_wrap_panel_candidate(parent):
            return False
        parent = parent.parent
    return False


def _source_visual_for_word_exclusion(root: Tag, source_id: str) -> Tag | None:
    if not source_id:
        return None
    for name in ("figure", "table", "img"):
        found = root.find(name, attrs={"data-source-id": source_id})
        if isinstance(found, Tag):
            return _source_wrap_tag(found)
    found = root.find(attrs={"data-source-id": source_id})
    if not isinstance(found, Tag):
        return None
    if _is_source_flow_unit(found):
        nested = found.find(["figure", "table", "img"], attrs={"data-source-id": source_id})
        if isinstance(nested, Tag):
            return _source_wrap_tag(nested)
        return None
    return _source_wrap_tag(found)


def _visible_panel_word_count(panel: Tag, *, exclude: Tag) -> int:
    clone = BeautifulSoup(str(panel), "html.parser")
    exclude_id = str(exclude.get("data-block-id") or "").strip()
    if exclude_id:
        doomed = clone.find(attrs={"data-block-id": exclude_id})
        if isinstance(doomed, Tag):
            doomed.decompose()
    else:
        source_id = str(exclude.get("data-source-id") or "").strip()
        if not source_id:
            img = exclude.find("img")
            if isinstance(img, Tag):
                source_id = str(img.get("data-source-id") or "").strip()
        if source_id:
            doomed = _source_visual_for_word_exclusion(clone, source_id)
            if isinstance(doomed, Tag):
                doomed.decompose()
    text = clone.get_text(" ", strip=True)
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’.-]*", text))


def _first_img_src(tag: Tag) -> str:
    if str(tag.name or "").lower() == "img":
        return str(tag.get("src") or "")
    img = tag.find("img")
    return str(img.get("src") or "") if isinstance(img, Tag) else ""


def _tag_wrap_evidence(tag: Tag, css: str) -> list[str]:
    evidence: list[str] = []
    style_chain: list[str] = []
    class_chain: list[str] = []
    node: Tag | None = tag
    while isinstance(node, Tag):
        style_chain.append(str(node.get("style") or ""))
        class_chain.extend(str(cls).strip() for cls in (node.get("class") or []) if str(cls).strip())
        if node.get("data-panel-role") or str(node.name or "").lower() in {"main", "body", "html"}:
            break
        node = node.parent
    inline_style = " ".join(style_chain)
    if re.search(r"(?:^|;)\s*float\s*:\s*(?:left|right)\b", inline_style, re.I):
        evidence.append("inline_float")
    if re.search(r"(?:^|;)\s*shape-outside\s*:", inline_style, re.I):
        evidence.append("inline_shape_outside")

    css_text = str(css or "")
    for cls in dict.fromkeys(class_chain):
        if cls.lower() in {"asset-wide", "asset-large", "asset-medium"}:
            evidence.append(f"class_large_asset_flow:{cls}")
        escaped = re.escape(cls)
        if re.search(rf"\.{escaped}[^{{}}]*\{{[^{{}}]*\bfloat\s*:\s*(?:left|right)\b", css_text, re.I | re.S):
            evidence.append(f"class_float:{cls}")
        if re.search(rf"\.{escaped}[^{{}}]*\{{[^{{}}]*\bshape-outside\s*:", css_text, re.I | re.S):
            evidence.append(f"class_shape_outside:{cls}")
    return evidence


def _visible_figcaption_text(tag: Tag) -> str:
    captions: list[str] = []
    if str(tag.name or "").lower() == "figcaption":
        captions.append(tag.get_text(" ", strip=True))
    for caption in tag.find_all("figcaption", recursive=True):
        if isinstance(caption, Tag):
            captions.append(caption.get_text(" ", strip=True))
    return " ".join(part for part in captions if part).strip()


def _visible_source_caption_text(tag: Tag) -> str:
    captions = [_visible_figcaption_text(tag)]
    if str(tag.name or "").lower() == "figure":
        for child in tag.find_all(True, recursive=False):
            if not isinstance(child, Tag) or str(child.name or "").lower() == "figcaption":
                continue
            if _is_caption_like_source_child(child) or _looks_like_visible_paper_caption(child):
                captions.append(child.get_text(" ", strip=True))
    for sibling in _nearby_source_caption_siblings(tag):
        if _is_caption_like_source_child(sibling) or _looks_like_visible_paper_caption(sibling):
            captions.append(sibling.get_text(" ", strip=True))
    return " ".join(part for part in captions if part).strip()


def _is_caption_like_source_child(tag: Tag) -> bool:
    role_blob = " ".join(
        str(tag.get(key) or "")
        for key in ("class", "role", "data-role", "data-block-kind", "data-slot-kind", "data-layer-role")
    ).lower()
    return "caption" in role_blob


def _looks_like_visible_paper_caption(tag: Tag) -> bool:
    text = tag.get_text(" ", strip=True)
    if not text:
        return False
    if not _VISIBLE_SOURCE_CAPTION_PREFIX_RE.search(text):
        return False
    name = str(tag.name or "").lower()
    if name in {"h1", "h2", "h3", "h4", "header"}:
        return False
    return True


def _nearby_source_caption_siblings(tag: Tag) -> list[Tag]:
    parent = tag.parent
    if not isinstance(parent, Tag):
        return []
    siblings: list[Tag] = []
    for sibling in tag.next_siblings:
        if isinstance(sibling, NavigableString):
            if str(sibling).strip():
                break
            continue
        if not isinstance(sibling, Tag):
            continue
        name = str(sibling.name or "").lower()
        if name in {"figure", "table", "img", "section", "article"}:
            break
        siblings.append(sibling)
        if len(siblings) >= 2:
            break
    return siblings


_SEPARATE_SOURCE_LAYOUT_CLASS_TOKENS = (
    "media-grid",
    "media_top",
    "media-top",
    "media-row",
    "media_row",
    "media-stack",
    "media_stack",
    "side-stack",
    "side_stack",
    "support-strip",
    "support_strip",
    "figure-strip",
    "figure_strip",
    "two-up",
    "two_up",
    "split-media",
    "split_media",
    "image-text",
    "image_text",
    "text-media",
    "text_media",
    "visual-grid",
    "visual_grid",
    "figure-grid",
    "figure_grid",
    "analysis-grid",
    "analysis_grid",
    "support-figs",
    "support_figs",
)


def _source_separate_layout_evidence(tag: Tag, css: str, panel: Tag) -> list[str]:
    evidence: list[str] = []
    css_text = str(css or "")
    node = tag.parent
    while isinstance(node, Tag) and node is not panel:
        if _is_source_flow_unit(node) or _is_editorial_section_tag(node):
            break
        if _is_pure_source_visual_shell(node):
            node = node.parent
            continue
        classes = [str(cls).strip() for cls in (node.get("class") or []) if str(cls).strip()]
        for cls in classes:
            class_key = cls.lower()
            if any(token in class_key for token in _SEPARATE_SOURCE_LAYOUT_CLASS_TOKENS):
                evidence.append(f"class:{cls}")
            if _css_class_declares_display_layout(cls, css_text):
                evidence.append(f"class_display:{cls}")
        style = str(node.get("style") or "")
        if re.search(r"(?:^|;)\s*display\s*:\s*(?:grid|inline-grid|flex|inline-flex)\b", style, re.I):
            evidence.append("inline_display_layout")
        node = node.parent
    return list(dict.fromkeys(evidence))


def _nearest_separate_source_layout_wrapper(tag: Tag, css: str, panel: Tag) -> Tag | None:
    css_text = str(css or "")
    node = tag.parent
    while isinstance(node, Tag) and node is not panel:
        if _is_source_flow_unit(node) or _is_editorial_section_tag(node):
            break
        if _is_pure_source_visual_shell(node):
            node = node.parent
            continue
        classes = [str(cls).strip() for cls in (node.get("class") or []) if str(cls).strip()]
        if any(
            any(token in cls.lower() for token in _SEPARATE_SOURCE_LAYOUT_CLASS_TOKENS)
            or _css_class_declares_display_layout(cls, css_text)
            for cls in classes
        ):
            return node
        style = str(node.get("style") or "")
        if re.search(r"(?:^|;)\s*display\s*:\s*(?:grid|inline-grid|flex|inline-flex)\b", style, re.I):
            return node
        node = node.parent
    return None


def _css_class_declares_display_layout(class_name: str, css: str) -> bool:
    return bool(_css_class_display_layout_evidence(class_name, css))


def _css_class_display_layout_evidence(class_name: str, css: str) -> list[dict[str, Any]]:
    if not class_name or not css:
        return []
    evidence: list[dict[str, Any]] = []
    css_text = re.sub(r"/\*.*?\*/", "", str(css), flags=re.S)
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css_text, re.S):
        selector_text = match.group(1).strip()
        declarations = match.group(2)
        display_match = re.search(
            r"(?:^|;)\s*display\s*:\s*(grid|inline-grid|flex|inline-flex)\b",
            declarations,
            re.I,
        )
        if not display_match:
            continue
        display = display_match.group(1).lower()
        for selector in _split_css_selector_list(selector_text):
            final_compound = _css_selector_final_compound(selector)
            if not _css_compound_has_class(final_compound, class_name):
                continue
            evidence.append({
                "matched_selector": selector.strip(),
                "matched_display": display,
                "matched_class": class_name,
                "selector_scope": "self",
            })
    return evidence


def _split_css_selector_list(selector_text: str) -> list[str]:
    selectors: list[str] = []
    start = 0
    paren_depth = 0
    bracket_depth = 0
    quote: str | None = None
    for idx, char in enumerate(selector_text):
        if quote:
            if char == quote:
                quote = None
            elif char == "\\":
                continue
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char == "(":
            paren_depth += 1
        elif char == ")" and paren_depth:
            paren_depth -= 1
        elif char == "[":
            bracket_depth += 1
        elif char == "]" and bracket_depth:
            bracket_depth -= 1
        elif char == "," and not paren_depth and not bracket_depth:
            selectors.append(selector_text[start:idx].strip())
            start = idx + 1
    selectors.append(selector_text[start:].strip())
    return [selector for selector in selectors if selector]


def _css_selector_final_compound(selector: str) -> str:
    stripped = selector.strip()
    if not stripped:
        return ""
    paren_depth = 0
    bracket_depth = 0
    quote: str | None = None
    idx = len(stripped) - 1
    while idx >= 0:
        char = stripped[idx]
        if quote:
            if char == quote:
                quote = None
            idx -= 1
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == ")":
            paren_depth += 1
        elif char == "(" and paren_depth:
            paren_depth -= 1
        elif char == "]":
            bracket_depth += 1
        elif char == "[" and bracket_depth:
            bracket_depth -= 1
        elif not paren_depth and not bracket_depth:
            if char in {">", "+", "~"}:
                return stripped[idx + 1:].strip()
            if char.isspace():
                return stripped[idx + 1:].strip()
        idx -= 1
    return stripped


def _css_compound_has_class(compound: str, class_name: str) -> bool:
    if not compound or not class_name:
        return False
    escaped = re.escape(class_name)
    return bool(re.search(rf"\.{escaped}(?![A-Za-z0-9_-])", compound))


def _numeric_claim_provenance_error(soup: BeautifulSoup, ctx: ToolContext) -> ToolResultRecord | None:
    source_text = _ingested_raw_text(ctx)
    if not source_text:
        return None
    normalized_source = _normalize_claim_text(source_text)
    issues: list[dict[str, Any]] = []
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        if _infer_block_kind(tag) not in {"text", "caption", "quote", "metric"}:
            continue
        text = tag.get_text(" ", strip=True)
        unverified = _unverified_risky_numeric_tokens(text, normalized_source)
        if not unverified:
            continue
        issues.append({
            "block_id": str(tag.get("data-block-id") or "").strip(),
            "text_excerpt": text[:260],
            "unverified_numeric_tokens": unverified[:8],
        })
    if not issues:
        return None
    return obs_error(
        "propose_paper_poster_html found numeric comparative claims that are not directly supported by ingested paper text.",
        category="validation",
        payload={
            "issue_id": "paper_poster_html_unverified_numeric_claim",
            "repair_route": "remove_or_source_numeric_claims",
            "issues": issues[:8],
            "hint": (
                "Keep numeric performance/compute/comparison claims only when the exact number appears in the paper text. "
                "Otherwise remove the number, soften the statement, or replace it with a paper-backed qualitative claim."
            ),
        },
    )


def _ingested_raw_text(ctx: ToolContext) -> str:
    if not isinstance(ctx.state, dict):
        return ""
    pieces: list[str] = []
    for item in ctx.state.get("ingested") or []:
        if isinstance(item, dict):
            raw = str(item.get("raw_text") or item.get("text") or "").strip()
            if raw:
                pieces.append(raw)
    manifest = ctx.state.get("manifest")
    if isinstance(manifest, dict):
        raw = str(manifest.get("raw_text") or "").strip()
        if raw:
            pieces.append(raw)
    return "\n".join(pieces)


def _unverified_risky_numeric_tokens(text: str, normalized_source: str) -> list[str]:
    value = str(text or "").strip()
    if not value or not normalized_source:
        return []
    lower = value.lower()
    if not _has_risky_numeric_claim_cue(lower):
        return []
    unverified: list[str] = []
    for match in re.finditer(
        r"\b\d+(?:\.\d+)?(?:\s*[-\u2013\u2014]\s*\d+(?:\.\d+)?)?\s*(?:x|\u00d7|%|percent|points?|pts?|b|m|k|t|gb|mb|tokens?|parameters?|params?)?\b",
        lower,
    ):
        token = match.group(0).strip()
        if not _substantive_numeric_claim_token(token):
            continue
        normalized_token = _normalize_claim_text(token).strip()
        if not normalized_token:
            continue
        compact_token = normalized_token.replace(" ", "")
        source_compact = normalized_source.replace(" ", "")
        if normalized_token in normalized_source or compact_token in source_compact:
            continue
        if token not in unverified:
            unverified.append(token)
    return unverified


def _has_risky_numeric_claim_cue(lower: str) -> bool:
    cues = (
        "less", "more", "fewer", "higher", "lower", "improve", "improves",
        "improved", "gain", "gains", "faster", "slower", "speed", "saves",
        "saving", "cost", "compute", "pretraining", "pre-training", "accuracy",
        "performance", "outperform", "beats", "beat", "versus", " vs ",
        "than", "reduces", "reduced", "reduction", "increases", "increased",
    )
    return any(cue in lower for cue in cues)


def _substantive_numeric_claim_token(token: str) -> bool:
    compact = _normalize_claim_text(token).replace(" ", "")
    if not compact:
        return False
    if re.fullmatch(r"(?:19|20)\d{2}", compact):
        return False
    if re.search(r"[-%x]|percent|points?|pts?|gb|mb|tokens?|parameters?|params?", compact):
        return True
    if re.search(r"\d\.\d", compact):
        return True
    digits = re.sub(r"\D+", "", compact)
    return len(digits) >= 2


def _normalize_claim_text(value: str) -> str:
    normalized = str(value or "").lower()
    normalized = normalized.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    normalized = normalized.replace("\u00d7", "x")
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"\s*-\s*", "-", normalized)
    normalized = re.sub(r"\s+([%x])", r"\1", normalized)
    return normalized.strip()


def _required_source_ids(ctx: ToolContext) -> list[str]:
    if not isinstance(ctx.state, dict):
        return []
    contract = ctx.state.get("poster_plan_contract")
    contract = contract if isinstance(contract, dict) else {}
    canonical_required = contract.get("required_source_visual_ids")
    if isinstance(canonical_required, list):
        required: list[str] = []
        for value in canonical_required:
            source_id = str(value or "").strip()
            if source_id and source_id not in required:
                required.append(source_id)
        return required
    targets = contract.get("density_targets") if isinstance(contract.get("density_targets"), dict) else {}
    min_visual_count = _safe_int(
        targets.get("min_visual_count")
        or targets.get("target_visual_count")
        or targets.get("min_selected_visual_count"),
        0,
    )
    selected_ids = _ids_from_asset_list(contract.get("selected_visuals"), keys=("layer_id", "asset_id", "source_id"))
    if min_visual_count > 0:
        required = selected_ids[:min(min_visual_count, len(selected_ids))]
    else:
        required = selected_ids
    if _editorial_flow_mode(ctx) and required:
        return required
    if _fixed_layout_slot_contract_active(ctx) and required:
        return required
    for source_id in _ids_from_asset_list(
        contract.get("storyboard_selected_assets"),
        keys=("layer_id", "asset_id", "source_id"),
    ):
        if source_id not in required:
            required.append(source_id)
    storyboard = ctx.state.get("paper_visual_storyboard")
    if isinstance(storyboard, dict):
        for source_id in _ids_from_asset_list(storyboard.get("selected_assets"), keys=("asset_id", "layer_id", "source_id")):
            if source_id not in required:
                required.append(source_id)
    return required


def _ids_from_asset_list(value: Any, *, keys: tuple[str, ...]) -> list[str]:
    ids: list[str] = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        for key in keys:
            source_id = str(item.get(key) or "").strip()
            if source_id:
                if source_id not in ids:
                    ids.append(source_id)
                break
    return ids


def _placed_source_ids(
    soup: BeautifulSoup,
    ctx: ToolContext | None = None,
    *,
    bboxes: dict[str, dict[str, Any]] | None = None,
    canvas: dict[str, Any] | None = None,
) -> set[str]:
    placed: set[str] = set()
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        source_id = _source_id_for_tag(tag, ctx)
        if source_id:
            if _tag_or_ancestor_hidden(tag):
                continue
            if source_id.startswith("ingest_table_") and not _is_bound_source_table_crop_tag(tag, ctx):
                continue
            if not source_id.startswith("ingest_table_") and not _tag_has_matching_source_crop_ref(
                tag,
                source_id,
                ctx,
                include_descendants=True,
            ):
                continue
            if bboxes is not None and not _tag_has_rendered_source_crop(
                tag,
                source_id,
                ctx,
                bboxes,
                canvas or {},
            ):
                continue
            placed.add(source_id)
    return placed


def _tag_or_ancestor_hidden(tag: Tag) -> bool:
    current: Tag | None = tag
    while isinstance(current, Tag):
        if current.has_attr("hidden") or str(current.get("aria-hidden") or "").lower() == "true":
            return True
        style = _inline_style_declarations(current)
        if style.get("display", "").lower() == "none" or style.get("visibility", "").lower() == "hidden":
            return True
        try:
            if float(style.get("opacity", "1")) <= 0.001:
                return True
        except ValueError:
            pass
        current = current.parent if isinstance(current.parent, Tag) else None
    return False


def _inline_style_declarations(tag: Tag) -> dict[str, str]:
    declarations: dict[str, str] = {}
    for item in str(tag.get("style") or "").split(";"):
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        declarations[key.strip().lower()] = value.strip()
    return declarations


def _tag_has_rendered_source_crop(
    tag: Tag,
    source_id: str,
    ctx: ToolContext | None,
    bboxes: dict[str, dict[str, Any]],
    canvas: dict[str, Any],
) -> bool:
    candidates: list[Tag] = []
    if str(tag.name or "").lower() in {"img", "object", "embed"}:
        candidates.append(tag)
    candidates.extend(
        child
        for child in tag.find_all(["img", "object", "embed"])
        if isinstance(child, Tag)
    )
    canvas_w = max(1.0, _safe_float(canvas.get("w_px"), 1.0))
    canvas_h = max(1.0, _safe_float(canvas.get("h_px"), 1.0))
    for candidate in candidates:
        if _tag_or_ancestor_hidden(candidate):
            continue
        if not _tag_has_matching_source_crop_ref(
            candidate,
            source_id,
            ctx,
            include_descendants=False,
        ):
            continue
        bbox = _bbox_only(_bbox_for_tag(candidate, bboxes))
        if bbox is None:
            continue
        x = _safe_float(bbox.get("x"), 0.0)
        y = _safe_float(bbox.get("y"), 0.0)
        width = _safe_float(bbox.get("w"), 0.0)
        height = _safe_float(bbox.get("h"), 0.0)
        if width <= 1.0 or height <= 1.0:
            continue
        if x + width <= 0 or y + height <= 0 or x >= canvas_w or y >= canvas_h:
            continue
        return True
    return False


def _apply_layout_slot_source_visuals(soup: BeautifulSoup, ctx: ToolContext) -> list[dict[str, Any]]:
    contract = ctx.state.get("poster_plan_contract") if isinstance(ctx.state, dict) else None
    if not isinstance(contract, dict):
        return []
    layout = contract.get("layout_slot_contract") if isinstance(contract.get("layout_slot_contract"), dict) else {}
    if _designer_owned_css_token(layout.get("mode")):
        return []
    slots = [slot for slot in (layout.get("slots") or []) if isinstance(slot, dict)]
    if not slots:
        return []

    repairs: list[dict[str, Any]] = []
    used = {
        str(tag.get("data-block-id") or "").strip()
        for tag in soup.find_all(True)
        if isinstance(tag, Tag) and str(tag.get("data-block-id") or "").strip()
    }
    placed = _placed_source_ids(soup, ctx)
    for slot in slots:
        visual_ids = [
            str(source_id).strip()
            for source_id in (slot.get("visual_ids") or [])
            if str(source_id).strip()
        ]
        if not visual_ids:
            continue
        slot_tags = _layout_slot_tags(soup, slot)
        if not slot_tags:
            continue
        source_lane = _source_lane_contract(slot)
        for slot_tag in slot_tags:
            lane_tag = _source_lane_tag_for_slot(soup, slot_tag, source_lane, used)
            if lane_tag is None:
                continue
            for source_id in visual_ids:
                if source_id in placed:
                    continue
                image_id = _unique_block_id(
                    _safe_block_id(f"visual_{source_id}", "visual"),
                    used,
                )
                img = soup.new_tag("img")
                img["data-block-id"] = image_id
                img["data-block-kind"] = "image"
                img["data-source-id"] = source_id
                img["data-layer-id"] = source_id
                img["alt"] = f"Source visual {source_id}"
                src_path = _source_path_for_id(source_id, ctx)
                img["src"] = src_path or f"{{{{layer:{source_id}}}}}"
                img["class"] = "auto-source-visual"
                img["style"] = (
                    "position:absolute;left:0;top:0;width:100%;height:100%;"
                    "object-fit:contain;object-position:center center;display:block;"
                )
                lane_tag.insert(0, img)
                used.add(image_id)
                placed.add(source_id)
                repairs.append({
                    "slot_id": str(slot.get("slot_id") or ""),
                    "lane_id": str(source_lane.get("lane_id") or source_lane.get("data_lane") or "source"),
                    "source_id": source_id,
                    "block_id": image_id,
                })
    return repairs


def _layout_slot_tags(soup: BeautifulSoup, slot: dict[str, Any]) -> list[Tag]:
    slot_id = str(slot.get("slot_id") or "").strip()
    slot_key = _safe_block_id(slot_id, "slot")
    if not slot_id:
        return []
    return [
        tag for tag in soup.find_all(True)
        if isinstance(tag, Tag)
        and (
            str(tag.get("data-slot-id") or "").strip() == slot_id
            or _panel_role_key(tag) == slot_key
        )
    ]


def _source_lane_contract(slot: dict[str, Any]) -> dict[str, Any]:
    for lane in slot.get("child_lanes") or []:
        if not isinstance(lane, dict):
            continue
        lane_id = str(lane.get("lane_id") or lane.get("data_lane") or "").strip()
        if lane_id == "source" or lane.get("visual_ids"):
            return lane
    return {}


def _source_lane_tag_for_slot(
    soup: BeautifulSoup,
    slot_tag: Tag,
    source_lane: dict[str, Any],
    used: set[str],
) -> Tag | None:
    lane_id = str(source_lane.get("data_lane") or source_lane.get("lane_id") or "source").strip()
    lane_tag = slot_tag.find(attrs={"data-lane": lane_id})
    if isinstance(lane_tag, Tag):
        return lane_tag
    if not source_lane:
        return None
    lane_bbox = _as_int_bbox(source_lane.get("bbox"))
    lane_tag = soup.new_tag("div")
    lane_tag["data-lane"] = lane_id
    lane_tag["data-block-id"] = _unique_block_id(
        _safe_block_id(f"panel_{lane_id}_lane", "panel"),
        used,
    )
    lane_tag["data-block-kind"] = "group"
    lane_tag["class"] = "fixed-lane auto-source-lane"
    if lane_bbox:
        _merge_position_style(lane_tag, lane_bbox)
    slot_tag.append(lane_tag)
    used.add(str(lane_tag.get("data-block-id") or ""))
    return lane_tag


def _source_path_for_id(source_id: str, ctx: ToolContext) -> str:
    key = str(source_id or "").strip()
    if not key or not isinstance(ctx.state, dict):
        return ""
    rendered = ctx.state.get("rendered_layers")
    if isinstance(rendered, dict) and isinstance(rendered.get(key), dict):
        src = str((rendered[key] or {}).get("src_path") or "").strip()
        if src:
            path = Path(src)
            if not path.is_absolute():
                run_relative = ctx.run_dir / src
                path = run_relative if run_relative.exists() else path
            try:
                return str(path.resolve())
            except OSError:
                return src
    provenance = ctx.state.get("paper_visual_provenance")
    if isinstance(provenance, dict):
        for asset in provenance.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            asset_id = str(
                asset.get("layer_id")
                or asset.get("asset_id")
                or asset.get("source_id")
                or ""
            ).strip()
            if asset_id != key:
                continue
            for field in ("src_path", "output_file", "local_asset_path", "path"):
                src = str(asset.get(field) or "").strip()
                if src:
                    candidate = Path(src)
                    if not candidate.is_absolute():
                        candidate = ctx.run_dir / src
                    return str(candidate)
    return ""


def _apply_layout_slot_contract_to_dom(soup: BeautifulSoup, ctx: ToolContext) -> list[dict[str, Any]]:
    contract = ctx.state.get("poster_plan_contract") if isinstance(ctx.state, dict) else None
    if not isinstance(contract, dict):
        return []
    layout = contract.get("layout_slot_contract") if isinstance(contract.get("layout_slot_contract"), dict) else {}
    if _designer_owned_css_token(layout.get("mode")):
        return []
    slots = [slot for slot in (layout.get("slots") or []) if isinstance(slot, dict)]
    if not slots:
        return []
    repairs: list[dict[str, Any]] = []
    fixed_slot_contract = _fixed_layout_slot_contract_active(ctx)
    for slot in slots:
        slot_id = str(slot.get("slot_id") or "").strip()
        if not slot_id:
            continue
        slot_bbox = _as_int_bbox(slot.get("bbox"))
        if not slot_bbox:
            continue
        slot_key = _safe_block_id(slot_id, "slot")
        slot_tags = [
            tag for tag in soup.find_all(True)
            if isinstance(tag, Tag)
            and (
                str(tag.get("data-slot-id") or "").strip() == slot_id
                or _panel_role_key(tag) == slot_key
            )
        ]
        for slot_tag in slot_tags:
            _merge_position_style(slot_tag, slot_bbox)
            repairs.append({"slot_id": slot_id, "target": "slot", "bbox": slot_bbox})
            repairs.extend(_hoist_nested_lane_tags_to_slot(slot_tag, slot_id=slot_id))
            if fixed_slot_contract:
                continue
            for lane in slot.get("child_lanes") or []:
                if not isinstance(lane, dict):
                    continue
                lane_id = str(lane.get("data_lane") or lane.get("lane_id") or "").strip()
                lane_bbox = _as_int_bbox(lane.get("bbox"))
                if not lane_id or not lane_bbox:
                    continue
                lane_tags = [
                    tag for tag in slot_tag.find_all(True)
                    if isinstance(tag, Tag)
                    and str(tag.get("data-lane") or "").strip() == lane_id
                ]
                for lane_tag in lane_tags:
                    _merge_position_style(lane_tag, lane_bbox)
                    repairs.append({
                        "slot_id": slot_id,
                        "lane_id": lane_id,
                        "target": "lane",
                        "bbox": lane_bbox,
                    })
    return repairs


def _hoist_nested_lane_tags_to_slot(slot_tag: Tag, *, slot_id: str) -> list[dict[str, Any]]:
    """Keep fixed slot lanes in slot coordinates, not nested source coordinates."""
    repairs: list[dict[str, Any]] = []
    for lane_tag in list(slot_tag.find_all(attrs={"data-lane": True})):
        if not isinstance(lane_tag, Tag):
            continue
        lane_id = str(lane_tag.get("data-lane") or "").strip()
        if not lane_id:
            continue
        ancestor = _nearest_lane_ancestor(lane_tag, stop=slot_tag)
        if ancestor is None:
            continue
        if not _should_hoist_nested_lane_tag(lane_tag):
            continue
        block_id = str(lane_tag.get("data-block-id") or "").strip()
        lane_tag.extract()
        slot_tag.append(lane_tag)
        repairs.append({
            "slot_id": slot_id,
            "lane_id": lane_id,
            "block_id": block_id,
            "target": "nested_lane_hoist",
            "from_lane": str(ancestor.get("data-lane") or "").strip(),
        })
    return repairs


def _nearest_lane_ancestor(tag: Tag, *, stop: Tag) -> Tag | None:
    parent = tag.parent
    while isinstance(parent, Tag) and parent is not stop:
        if str(parent.get("data-lane") or "").strip():
            return parent
        parent = parent.parent
    return None


def _should_hoist_nested_lane_tag(tag: Tag) -> bool:
    name = str(tag.name or "").lower()
    if name not in {
        "figcaption", "figure", "div", "section", "article", "aside",
        "table", "blockquote", "p", "ul", "ol",
    }:
        return False
    return bool(str(tag.get("data-block-id") or "").strip())


def _panel_role_key(tag: Tag) -> str:
    value = str(tag.get("data-panel-role") or "").strip()
    if not value:
        return ""
    return _safe_block_id(value, "slot")


def _as_int_bbox(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    try:
        bbox = {key: int(round(float(value[key]))) for key in ("x", "y", "w", "h")}
    except Exception:
        return None
    if bbox["w"] <= 0 or bbox["h"] <= 0:
        return None
    return bbox


def _merge_position_style(
    tag: Tag,
    bbox: dict[str, int],
    *,
    extra_rules: list[str] | None = None,
) -> None:
    existing = str(tag.get("style") or "")
    stripped = re.sub(
        (
            r"(?:^|;)\s*(?:position|left|top|width|height|box-sizing|overflow|"
            r"object-fit|object-position|grid-area|grid-row|grid-column|"
            r"align-self|justify-self)\s*:[^;]*"
        ),
        "",
        existing,
        flags=re.IGNORECASE,
    ).strip()
    rules = [
        "position:absolute",
        f"left:{bbox['x']}px",
        f"top:{bbox['y']}px",
        f"width:{bbox['w']}px",
        f"height:{bbox['h']}px",
        "box-sizing:border-box",
        "overflow:hidden",
        "grid-area:auto",
    ]
    parts = [stripped.rstrip(";")] if stripped else []
    parts.extend(rules)
    if extra_rules:
        parts.extend(extra_rules)
    tag["style"] = ";".join(part for part in parts if part) + ";"


def _normalize_fixed_lane_child_flow(soup: BeautifulSoup) -> list[dict[str, Any]]:
    repairs: list[dict[str, Any]] = []
    for lane_tag in soup.find_all(attrs={"data-lane": True}):
        if not isinstance(lane_tag, Tag):
            continue
        lane_id = str(lane_tag.get("data-lane") or "").strip()
        for tag in lane_tag.find_all(True):
            if not isinstance(tag, Tag):
                continue
            block_id = str(tag.get("data-block-id") or "").strip()
            kind = _infer_block_kind(tag)
            if not block_id or not _should_skip_fixed_lane_child_geometry(tag, kind):
                continue
            style = str(tag.get("style") or "")
            if not style or not re.search(r"(?:^|;)\s*(?:position|left|top|right|bottom|transform|translate)\s*:", style, flags=re.IGNORECASE):
                continue
            stripped = re.sub(
                r"(?:^|;)\s*(?:position|left|top|right|bottom|width|height|transform|translate)\s*:[^;]*",
                "",
                style,
                flags=re.IGNORECASE,
            ).strip().strip(";")
            if stripped:
                tag["style"] = stripped + ";"
            else:
                tag.attrs.pop("style", None)
            repairs.append({"lane_id": lane_id, "block_id": block_id, "kind": kind})
    return repairs


def _measure_dom_bboxes(
    body_html: str,
    css: str,
    *,
    canvas: dict[str, Any],
    ctx: ToolContext,
    candidate: dict[str, Any] | None = None,
    stage: str = "measure",
    root_shell: dict[str, Any] | None = None,
) -> dict[str, dict[str, int]]:
    ctx.state.pop("paper_poster_html_computed_style_measurements", None)
    ctx.state.pop("paper_poster_html_reference_rule_measurements", None)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001 - optional compiler assist
        log("paper_poster_html.measure_unavailable", error=f"{type(exc).__name__}: {exc}")
        return {}
    cw = int(canvas["w_px"])
    ch = int(canvas["h_px"])
    temp_dir = ctx.run_dir / "html_first"
    temp_dir.mkdir(parents=True, exist_ok=True)
    candidate_dir = Path(str(candidate.get("candidate_dir") or "")) if isinstance(candidate, dict) else None
    if candidate_dir:
        candidate_dir.mkdir(parents=True, exist_ok=True)
    html_path = temp_dir / "measure.html"
    main_open = _paper_poster_main_open_tag(root_shell)
    katex_block = (
        inline_katex_bundle(ctx.settings.repo_root, root_selector=".paper-poster")
        if has_tex_math(body_html)
        else ""
    )
    measure_html = (
        "<!doctype html><html><head><meta charset=\"utf-8\"><style>"
        f"{_measurement_base_css(cw, ch)}\n{css}\n{_root_canvas_lock_css(cw, ch)}\n"
        f"</style>{katex_block}</head><body>"
        f"{main_open}{body_html}</main>"
        "</body></html>",
    )
    html_text = measure_html[0]
    html_path.write_text(
        html_text,
        encoding="utf-8",
    )
    candidate_html_path = None
    if candidate_dir:
        candidate_html_path = candidate_dir / "measure.html"
        try:
            candidate_html_path.write_text(html_text, encoding="utf-8")
            candidate["measure_html"] = str(candidate_html_path)
            candidate["measure_html_relative"] = _relative_to_run_dir(ctx, candidate_html_path)
        except OSError as exc:
            log("paper_poster_html.candidate_measure_write_failed", error=f"{type(exc).__name__}: {exc}")
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            except Exception:
                browser = p.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": cw, "height": ch}, device_scale_factor=1)
            page.goto(html_path.resolve().as_uri(), wait_until="load", timeout=15000)
            wait_for_autodesign_math(page, timeout_ms=3000)
            data = page.evaluate(
                """() => {
                  const root = document.querySelector('.paper-poster');
                  if (!root) return [];
                  const rr = root.getBoundingClientRect();
                  const clippedRect = (el, r) => {
                    let left = r.left;
                    let top = r.top;
                    let right = r.right;
                    let bottom = r.bottom;
                    let node = el.parentElement;
                    while (node) {
                      const cs = getComputedStyle(node);
                      const overflow = `${cs.overflow} ${cs.overflowX} ${cs.overflowY}`.toLowerCase();
                      if (
                        node === root ||
                        node.hasAttribute('data-lane') ||
                        /(hidden|clip|scroll|auto)/.test(overflow)
                      ) {
                        const cr = node.getBoundingClientRect();
                        left = Math.max(left, cr.left);
                        top = Math.max(top, cr.top);
                        right = Math.min(right, cr.right);
                        bottom = Math.min(bottom, cr.bottom);
                      }
                      if (node === root) break;
                      node = node.parentElement;
                    }
                    if (right < left) right = left;
                    if (bottom < top) bottom = top;
                    return {left, top, right, bottom, width: right - left, height: bottom - top};
                  };
                  const textLineRects = (el, mode = 'visible') => {
                    const rects = [];
                    const walker = document.createTreeWalker(
                      el,
                      NodeFilter.SHOW_TEXT,
                      {
                        acceptNode(node) {
                          if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
                          const parent = node.parentElement;
                          if (!parent) return NodeFilter.FILTER_REJECT;
                          const tag = parent.tagName ? parent.tagName.toLowerCase() : '';
                          if (['script', 'style', 'template'].includes(tag)) return NodeFilter.FILTER_REJECT;
                          const cs = getComputedStyle(parent);
                          if (cs.display === 'none' || cs.visibility === 'hidden') return NodeFilter.FILTER_REJECT;
                          return NodeFilter.FILTER_ACCEPT;
                        }
                      }
                    );
                    let node;
                    while ((node = walker.nextNode()) && rects.length < 160) {
                      const range = document.createRange();
                      range.selectNodeContents(node);
                      for (const r of Array.from(range.getClientRects())) {
	                        if (r.width > 1 && r.height > 1) {
	                          const visible = clippedRect(node.parentElement || el, r);
	                          const chosen = mode === 'raw' ? r : visible;
	                          if (chosen.width > 1 && chosen.height > 1) {
	                            rects.push({
	                              x: chosen.left - rr.left,
	                              y: chosen.top - rr.top,
	                              w: chosen.width,
	                              h: chosen.height
	                            });
	                          }
	                        }
                        if (rects.length >= 160) break;
                      }
                      range.detach();
                    }
                    return rects;
                  };
                  const tokenList = (el) => {
                    if (!el) return [];
                    const out = [];
                    const pushTokens = (value) => {
                      String(value || '').toLowerCase().split(/[^a-z0-9_-]+/).forEach(token => {
                        if (token) out.push(token);
                      });
                    };
                    pushTokens(el.className && typeof el.className === 'string' ? el.className : '');
                    pushTokens(el.getAttribute('data-block-kind'));
                    pushTokens(el.getAttribute('data-role'));
                    pushTokens(el.getAttribute('role'));
                    return Array.from(new Set(out));
                  };
                  const allowedMathTokens = new Set([
                    'formula', 'math-block', 'mathblock', 'equation', 'equation-block',
                    'display-math', 'math-display', 'proof', 'derivation'
                  ]);
                  const narrowMathTokens = new Set([
                    'metric', 'metrics', 'metric-card', 'metric-chip', 'setup-metric',
                    'stage', 'pipeline-stage', 'step', 'chip', 'badge', 'pill', 'stat',
                    'stats', 'kpi', 'score', 'scorecard', 'result-card', 'result-chip',
                    'takeaway-row', 'arch-row'
                  ]);
                  const hasAnyToken = (el, tokenSet) => tokenList(el).some(token => tokenSet.has(token));
                  const rectPayload = (r) => ({
                    x: r.left - rr.left,
                    y: r.top - rr.top,
                    w: r.width,
                    h: r.height
                  });
                  const elementPayload = (el) => {
                    if (!el) return {};
                    return {
                      tag: el.tagName ? el.tagName.toLowerCase() : '',
                      className: typeof el.className === 'string' ? el.className : '',
                      blockId: el.getAttribute('data-block-id') || '',
                      sectionId: el.getAttribute('data-section-id') || '',
                      blockKind: el.getAttribute('data-block-kind') || '',
                      role: el.getAttribute('data-role') || el.getAttribute('role') || '',
                      tokens: tokenList(el).slice(0, 16)
                    };
                  };
                  const nearestMathContainer = (mathEl) => {
                    let node = mathEl.parentElement;
                    let nearestBlockId = '';
                    let nearestSectionId = '';
                    let allowed = null;
                    let narrow = null;
                    let tableCell = null;
                    let semantic = null;
                    const chain = [];
                    while (node) {
                      const tag = node.tagName ? node.tagName.toLowerCase() : '';
                      if (chain.length < 10) chain.push(elementPayload(node));
                      if (!nearestBlockId && node.getAttribute('data-block-id')) {
                        nearestBlockId = node.getAttribute('data-block-id') || '';
                      }
                      if (!nearestSectionId && node.getAttribute('data-section-id')) {
                        nearestSectionId = node.getAttribute('data-section-id') || '';
                      }
                      if (!nearestSectionId && node.getAttribute('data-panel-role')) {
                        nearestSectionId = node.getAttribute('data-panel-role') || '';
                      }
                      if (!allowed && hasAnyToken(node, allowedMathTokens)) allowed = node;
                      if (!narrow && hasAnyToken(node, narrowMathTokens)) narrow = node;
                      if (!tableCell && (tag === 'td' || tag === 'th')) tableCell = node;
                      if (
                        !semantic &&
                        (hasAnyToken(node, narrowMathTokens) ||
                         hasAnyToken(node, allowedMathTokens) ||
                         tag === 'td' || tag === 'th' ||
                         node.getAttribute('data-block-id') ||
                         node.getAttribute('data-panel-role') ||
                         node === root)
                      ) {
                        semantic = node;
                      }
                      if (node === root) break;
                      node = node.parentElement;
                    }
                    const container = narrow || tableCell || semantic || mathEl.parentElement || mathEl;
                    const cr = container.getBoundingClientRect();
                    return {
                      container: elementPayload(container),
                      containerRect: rectPayload(cr),
                      nearestBlockId,
                      nearestSectionId,
                      allowedContainer: allowed ? elementPayload(allowed) : null,
                      narrowContainer: narrow ? elementPayload(narrow) : null,
                      tableCell: tableCell ? elementPayload(tableCell) : null,
                      chain
                    };
                  };
                  const mathLayoutItems = () => {
                    const items = [];
                    const mathNodes = Array.from(root.querySelectorAll('.katex')).filter(el => {
                      const parentMath = el.parentElement ? el.parentElement.closest('.katex') : null;
                      return parentMath !== el && !parentMath;
                    });
                    mathNodes.slice(0, 120).forEach((mathEl, index) => {
                      const raw = mathEl.getBoundingClientRect();
                      if (raw.width < 1 || raw.height < 1) return;
                      const rects = Array.from(mathEl.getClientRects())
                        .filter(r => r.width > 1 && r.height > 1)
                        .slice(0, 16)
                        .map(rectPayload);
                      const context = nearestMathContainer(mathEl);
                      const displayWrapper = mathEl.closest('.katex-display');
                      const annotation = mathEl.querySelector('annotation[encoding="application/x-tex"]');
                      const mathText = annotation ? (annotation.textContent || '') : (mathEl.textContent || '');
                      items.push({
                        index,
                        mathText: mathText.replace(/\\s+/g, ' ').trim().slice(0, 120),
                        rect: rectPayload(raw),
                        lineRects: rects,
                        lineRectCount: rects.length,
                        isDisplay: Boolean(displayWrapper),
                        context
                      });
                    });
                    return items;
                  };
                  const referenceRuleDiagnostics = () => {
                    const ruleStyle = (el, pseudo = null) => {
                      const cs = getComputedStyle(el, pseudo);
                      const raw = el.getBoundingClientRect();
                      return {
                        id: el.getAttribute('data-block-id') || '',
                        tagName: (el.tagName || '').toLowerCase(),
                        text: (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 120),
                        pseudo: pseudo || '',
                        rendered: Boolean(el.getClientRects().length && raw.width > 0 && raw.height > 0),
                        display: cs.display,
                        visibility: cs.visibility,
                        opacity: cs.opacity,
                        content: cs.content,
                        x: raw.left - rr.left,
                        y: raw.top - rr.top,
                        w: raw.width,
                        h: raw.height,
                        backgroundColor: cs.backgroundColor,
                        boxShadow: cs.boxShadow,
                        outlineWidth: cs.outlineWidth,
                        outlineStyle: cs.outlineStyle,
                        outlineColor: cs.outlineColor,
                        borderTopWidth: cs.borderTopWidth,
                        borderTopStyle: cs.borderTopStyle,
                        borderTopColor: cs.borderTopColor,
                        borderBottomWidth: cs.borderBottomWidth,
                        borderBottomStyle: cs.borderBottomStyle,
                        borderBottomColor: cs.borderBottomColor,
                        borderRightWidth: cs.borderRightWidth,
                        borderRightStyle: cs.borderRightStyle,
                        borderRightColor: cs.borderRightColor,
                        borderLeftWidth: cs.borderLeftWidth,
                        borderLeftStyle: cs.borderLeftStyle,
                        borderLeftColor: cs.borderLeftColor
                      };
                    };
                    const tableRows = Array.from(root.querySelectorAll('table tbody tr, table tbody th, table tbody td'))
                      .slice(0, 240)
                      .map(el => ruleStyle(el));
                    const formulaPseudos = [];
                    const formulaContainers = Array.from(root.querySelectorAll('.formula,.formula-slot,.math-block,.equation-block,[data-block-kind="formula"],[data-role*="formula"]'))
                      .slice(0, 80)
                    formulaContainers.forEach(el => {
                        formulaPseudos.push(ruleStyle(el, '::before'));
                        formulaPseudos.push(ruleStyle(el, '::after'));
                      });
                    const formulaRules = formulaContainers
                      .flatMap(el => Array.from(el.querySelectorAll('hr')))
                      .slice(0, 80)
                      .map(el => ruleStyle(el));
                    const headerPseudos = [];
                    const headerNodes = [root, ...Array.from(root.querySelectorAll('.poster-header,.identity-header,[data-panel-role="identity_header"]'))];
                    headerNodes.slice(0, 12).forEach(el => {
                      headerPseudos.push(ruleStyle(el, '::before'));
                      headerPseudos.push(ruleStyle(el, '::after'));
                    });
                    const contentRegionPseudos = [];
                    const contentRegionNodes = Array.from(root.querySelectorAll('.poster-section,.poster-column,[data-style-role="section"],[data-style-role="column"]'));
                    contentRegionNodes.slice(0, 80).forEach(el => {
                      contentRegionPseudos.push(ruleStyle(el, '::before'));
                      contentRegionPseudos.push(ruleStyle(el, '::after'));
                    });
                    const chromeNodes = Array.from(root.querySelectorAll('.reference-chrome *,[data-style-role="chrome-layer"] *'))
                      .slice(0, 120)
                      .flatMap(el => [ruleStyle(el), ruleStyle(el, '::before'), ruleStyle(el, '::after')]);
                    const contentRegions = Array.from(root.querySelectorAll('.poster-section,[data-style-role="section"]'))
                      .slice(0, 120)
                      .map(el => ruleStyle(el));
                    return {tableRows, formulaPseudos, formulaRules, headerPseudos, contentRegionPseudos, chromeNodes, contentRegions};
                  };
                  const targets = [document.body, root];
                  targets.push(...Array.from(root.querySelectorAll('[data-block-id]')));
                  const targetPayloads = targets.map(el => {
                    const raw = el.getBoundingClientRect();
                    const visible = clippedRect(el, raw);
                    const cs = getComputedStyle(el);
                    return {
                      id: el === document.body ? '__paper_poster_body__' : (el === root ? '__paper_poster_root__' : (el.getAttribute('data-block-id') || '')),
                      rendered: Boolean(el.getClientRects().length && raw.width > 0 && raw.height > 0),
                      x: raw.left - rr.left,
                      y: raw.top - rr.top,
                      w: raw.width,
                      h: raw.height,
                      visibleX: visible.left - rr.left,
                      visibleY: visible.top - rr.top,
                      visibleW: visible.width,
                      visibleH: visible.height,
                      display: getComputedStyle(el).display,
                      visibility: getComputedStyle(el).visibility,
                      fontFamily: cs.fontFamily,
                      fontSize: cs.fontSize,
                      fontWeight: cs.fontWeight,
                      fontStyle: cs.fontStyle,
                      lineHeight: cs.lineHeight,
                      color: cs.color,
                      backgroundColor: cs.backgroundColor,
                      textAlign: cs.textAlign,
                      alignItems: cs.alignItems,
                      justifyContent: cs.justifyContent,
                      justifyItems: cs.justifyItems,
                      flexDirection: cs.flexDirection,
                      boxShadow: cs.boxShadow,
                      outlineWidth: cs.outlineWidth,
                      outlineStyle: cs.outlineStyle,
                      outlineColor: cs.outlineColor,
                      whiteSpace: cs.whiteSpace,
                      overflow: cs.overflow,
                      overflowX: cs.overflowX,
                      overflowY: cs.overflowY,
                      paddingTop: cs.paddingTop,
                      paddingRight: cs.paddingRight,
                      paddingBottom: cs.paddingBottom,
                      paddingLeft: cs.paddingLeft,
                      boxSizing: cs.boxSizing,
                      gridTemplateRows: cs.gridTemplateRows,
                      gridTemplateColumns: cs.gridTemplateColumns,
                      borderTopWidth: cs.borderTopWidth,
                      borderTopStyle: cs.borderTopStyle,
                      borderTopColor: cs.borderTopColor,
                      borderRightWidth: cs.borderRightWidth,
                      borderRightStyle: cs.borderRightStyle,
                      borderRightColor: cs.borderRightColor,
                      borderBottomWidth: cs.borderBottomWidth,
                      borderBottomStyle: cs.borderBottomStyle,
                      borderBottomColor: cs.borderBottomColor,
                      borderLeftWidth: cs.borderLeftWidth,
                      borderLeftStyle: cs.borderLeftStyle,
                      borderLeftColor: cs.borderLeftColor,
                      scrollWidth: el.scrollWidth,
                      scrollHeight: el.scrollHeight,
                      clientWidth: el.clientWidth,
                      clientHeight: el.clientHeight,
	                      textLineRects: textLineRects(el),
	                      rawTextLineRects: textLineRects(el, 'raw')
	                    };
                  });
                  const mathItems = mathLayoutItems();
                  const ruleDiagnostics = referenceRuleDiagnostics();
                  return {
                    targets: targetPayloads,
                    mathLayout: {
                      version: 1,
                      itemCount: mathItems.length,
                      items: mathItems
                    },
                    referenceRuleDiagnostics: ruleDiagnostics
                  };
                }"""
            )
            if candidate_dir:
                preview_path = candidate_dir / "preview.png"
                try:
                    page.locator(".paper-poster").screenshot(path=str(preview_path), timeout=5000)
                    candidate["preview_png"] = str(preview_path)
                    candidate["preview_png_relative"] = _relative_to_run_dir(ctx, preview_path)
                except Exception as exc:  # noqa: BLE001 - screenshot is diagnostic only
                    log("paper_poster_html.candidate_preview_failed", error=f"{type(exc).__name__}: {exc}")
            browser.close()
    except Exception as exc:  # noqa: BLE001 - fallback still gives validation signal
        log("paper_poster_html.measure_failed", error=f"{type(exc).__name__}: {exc}")
        return {}
    measured: dict[str, dict[str, int]] = {}
    computed_style_measurements: dict[str, dict[str, Any]] = {}
    target_data = data.get("targets") if isinstance(data, dict) else data
    math_layout = data.get("mathLayout") if isinstance(data, dict) else None
    reference_rule_diagnostics = data.get("referenceRuleDiagnostics") if isinstance(data, dict) else None
    ctx.state["paper_poster_html_reference_rule_measurements"] = (
        reference_rule_diagnostics if isinstance(reference_rule_diagnostics, dict) else {}
    )
    if isinstance(candidate, dict) and isinstance(math_layout, dict):
        candidate["math_layout_diagnostics"] = math_layout
    for item in target_data if isinstance(target_data, list) else []:
        if not isinstance(item, dict):
            continue
        block_id = str(item.get("id") or "").strip()
        computed = _computed_style_for_measurement(item)
        if block_id and computed:
            computed_style_measurements[block_id] = computed
        if not block_id or str(item.get("display") or "") == "none" or str(item.get("visibility") or "") == "hidden":
            continue
        bbox = _coerce_bbox(item, canvas)
        if bbox and bbox["w"] > 1 and bbox["h"] > 1:
            visible_bbox = _coerce_bbox(
                {
                    "x": item.get("visibleX"),
                    "y": item.get("visibleY"),
                    "w": item.get("visibleW"),
                    "h": item.get("visibleH"),
                },
                canvas,
            )
            if visible_bbox:
                bbox["_visible_bbox"] = visible_bbox
            if computed:
                bbox["_computed_style"] = computed
            layout_metrics = _layout_metrics_for_measurement(item)
            if layout_metrics:
                bbox["_layout_metrics"] = layout_metrics
            line_bboxes = _text_line_bboxes_for_measurement(item, canvas)
            if line_bboxes:
                bbox["_text_line_bboxes"] = line_bboxes
            raw_line_bboxes = _text_line_bboxes_for_measurement({"textLineRects": item.get("rawTextLineRects")}, canvas)
            if raw_line_bboxes:
                bbox["_raw_text_line_bboxes"] = raw_line_bboxes
            measured[block_id] = bbox
    ctx.state["paper_poster_html_computed_style_measurements"] = computed_style_measurements
    if candidate_dir:
        measurement_path = candidate_dir / "measurement.json"
        try:
            measurement_path.write_text(
                json.dumps(
                    {
                        "stage": stage,
                        "canvas": canvas,
                        "block_count": len(measured),
                        "bboxes": measured,
                        "math_layout": math_layout if isinstance(math_layout, dict) else {},
                        "reference_rule_diagnostics": (
                            reference_rule_diagnostics if isinstance(reference_rule_diagnostics, dict) else {}
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            candidate["measurement_json"] = str(measurement_path)
            candidate["measurement_json_relative"] = _relative_to_run_dir(ctx, measurement_path)
            _write_candidate_manifest(ctx, candidate)
        except OSError as exc:
            log("paper_poster_html.candidate_measurement_write_failed", error=f"{type(exc).__name__}: {exc}")
    return measured


def _narrow_math_container_error(
    candidate: dict[str, Any] | None,
    canvas: dict[str, Any],
    ctx: ToolContext,
) -> ToolResultRecord | None:
    if not isinstance(candidate, dict):
        return None
    diagnostics = candidate.get("math_layout_diagnostics")
    if not isinstance(diagnostics, dict):
        return None
    raw_items = diagnostics.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        ctx.state.pop("paper_poster_html_narrow_math_container", None)
        return None
    cw = max(1, _safe_int(canvas.get("w_px"), 0))
    narrow_threshold = max(180.0, min(360.0, float(cw) * 0.13))
    issues: list[dict[str, Any]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        context = raw_item.get("context") if isinstance(raw_item.get("context"), dict) else {}
        container = context.get("container") if isinstance(context.get("container"), dict) else {}
        container_rect = context.get("containerRect") if isinstance(context.get("containerRect"), dict) else {}
        narrow_container = context.get("narrowContainer") if isinstance(context.get("narrowContainer"), dict) else None
        allowed_container = context.get("allowedContainer") if isinstance(context.get("allowedContainer"), dict) else None
        table_cell = context.get("tableCell") if isinstance(context.get("tableCell"), dict) else None
        if allowed_container:
            continue
        container_w = _safe_float(container_rect.get("w"))
        if container_w <= 0:
            continue
        math_text = str(raw_item.get("mathText") or "").strip()
        compact_math_len = len(re.sub(r"\s+", "", math_text))
        line_rects = raw_item.get("lineRects") if isinstance(raw_item.get("lineRects"), list) else []
        line_rect_count = max(_safe_int(raw_item.get("lineRectCount")), len(line_rects))
        rect = raw_item.get("rect") if isinstance(raw_item.get("rect"), dict) else {}
        math_w = _safe_float(rect.get("w"))
        fragmented = _math_layout_is_fragmented(line_rects, math_w)
        is_display = bool(raw_item.get("isDisplay"))
        has_narrow_container = bool(narrow_container)
        if table_cell and not has_narrow_container and not fragmented:
            continue
        narrow_info_cell = (
            has_narrow_container
            and container_w < narrow_threshold
            and (compact_math_len >= 3 or line_rect_count > 1 or is_display)
        )
        fragmented_inline = (
            not has_narrow_container
            and container_w < narrow_threshold
            and compact_math_len >= 5
            and fragmented
        )
        if not narrow_info_cell and not fragmented_inline:
            continue
        reason = "narrow_info_cell_formula" if narrow_info_cell else "fragmented_inline_math"
        issue: dict[str, Any] = {
            "severity_reason": reason,
            "math_text": math_text[:100],
            "container_width_px": round(container_w, 2),
            "narrow_threshold_px": round(narrow_threshold, 2),
            "container": _math_layout_element_summary(container),
            "narrow_container": _math_layout_element_summary(narrow_container) if narrow_container else {},
            "nearest_block_id": str(context.get("nearestBlockId") or ""),
            "nearest_section_id": str(context.get("nearestSectionId") or ""),
            "math_bbox": _math_layout_rect_summary(rect),
            "line_rect_count": line_rect_count,
            "line_rects": [_math_layout_rect_summary(item) for item in line_rects[:6] if isinstance(item, dict)],
            "repair": (
                "Replace this narrow-cell TeX with short plain text, then move the full equation "
                "to a dedicated `.formula` / `.math-block` element or a wide native table row."
            ),
        }
        issues.append(issue)
    if not issues:
        ctx.state.pop("paper_poster_html_narrow_math_container", None)
        return None
    ctx.state["paper_poster_html_narrow_math_container"] = {
        "issue_count": len(issues),
        "issues": issues[:12],
        "narrow_threshold_px": round(narrow_threshold, 2),
    }
    return obs_error(
        "propose_paper_poster_html found TeX math placed inside a narrow metric/stage/chip container.",
        category="validation",
        payload={
            "issue_id": "paper_poster_html_narrow_math_container",
            "repair_route": "move_math_out_of_narrow_info_cells",
            "issues": issues[:12],
            "hint": (
                "Do not put TeX/KaTeX formulas inside narrow metric cards, pipeline stages, chips, badges, "
                "or other compact information cells. Use plain-text labels or values in those cells, and move "
                "the full equation into a wide `.formula`, `.math-block`, `[data-block-kind=\"formula\"]`, "
                "or sufficiently wide native table row. Do not solve this by shrinking the formula font."
            ),
        },
    )


def _math_layout_is_fragmented(line_rects: list[Any], math_width: float) -> bool:
    rects = [item for item in line_rects if isinstance(item, dict)]
    if len(rects) <= 1:
        return False
    widths = [_safe_float(item.get("w")) for item in rects]
    widths = [width for width in widths if width > 1]
    if len(widths) <= 1:
        return False
    if math_width > 0 and sum(widths) <= math_width * 1.15:
        return False
    tiny_limit = max(10.0, min(24.0, max(widths) * 0.25))
    return sum(1 for width in widths if width <= tiny_limit) >= 1 or len(widths) >= 3


def _math_layout_element_summary(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("tag", "className", "blockId", "sectionId", "blockKind", "role"):
        raw = str(value.get(key) or "").strip()
        if raw:
            out[key] = raw[:160]
    tokens = value.get("tokens")
    if isinstance(tokens, list):
        out["tokens"] = [str(token)[:80] for token in tokens[:12] if str(token).strip()]
    return out


def _math_layout_rect_summary(value: dict[str, Any] | None) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {
        "x": round(_safe_float(value.get("x")), 2),
        "y": round(_safe_float(value.get("y")), 2),
        "w": round(_safe_float(value.get("w")), 2),
        "h": round(_safe_float(value.get("h")), 2),
    }


def _measurement_base_css(cw: int, ch: int) -> str:
    return (
        "html,body{margin:0;padding:0;background:#fff;}"
        "*{box-sizing:border-box;}"
        f".paper-poster{{position:relative;width:{cw}px;height:{ch}px;overflow:hidden;"
        "background:var(--poster-bg,#FFFFFF);color:var(--poster-text,#111827);font-family:Inter,Arial,sans-serif;}"
        ".paper-poster img{display:block;max-width:100%;height:auto;}"
        ".paper-poster table{border-collapse:collapse;width:100%;max-width:100%;table-layout:fixed;}"
        ".paper-poster [data-block-kind=\"table\"]>table{height:100%;}"
        ".paper-poster th,.paper-poster td{overflow-wrap:anywhere;text-align:left;}"
    )


def _root_canvas_lock_css(cw: int, ch: int) -> str:
    return (
        ".paper-poster{"
        f"position:relative!important;width:{cw}px!important;height:{ch}px!important;"
        "min-width:0!important;min-height:0!important;max-width:none!important;"
        "max-height:none!important;overflow:hidden!important;box-sizing:border-box!important;"
        "}"
    )


def _local_repair_hint(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
    *,
    shell_error: ToolResultRecord | None = None,
) -> str:
    cw = int(canvas.get("w_px") or 0)
    ch = int(canvas.get("h_px") or 0)
    hints: list[str] = []

    if shell_error and isinstance(shell_error.payload, dict):
        issue_id = str(shell_error.payload.get("issue_id") or "")
        if issue_id == "paper_poster_html_narrow_math_container":
            issues = shell_error.payload.get("issues") if isinstance(shell_error.payload.get("issues"), list) else []
            targets: list[str] = []
            for issue in issues[:4]:
                if not isinstance(issue, dict):
                    continue
                target = str(issue.get("nearest_block_id") or issue.get("nearest_section_id") or "").strip()
                math_text = str(issue.get("math_text") or "").strip()
                if target or math_text:
                    targets.append(
                        f"{f'`{target}` ' if target else ''}{f'({math_text[:48]})' if math_text else ''}".strip()
                    )
            hints.append(
                "Move TeX math out of narrow metric/stage/chip cells. Keep those cells to plain-text labels or values, "
                "and place the full equation in a wide `.formula`, `.math-block`, or `[data-block-kind=\"formula\"]` block."
                + (f" Targets: {', '.join(targets)}." if targets else "")
            )
            return " ".join(hints)[:1200]

    header = soup.select_one(".poster-header,[data-panel-role='identity_header'],[data-role='poster-header']")
    columns = soup.select_one(".poster-columns,.columns,[data-role='poster-columns']")
    header_bbox = _bbox_for_tag(header, bboxes) if isinstance(header, Tag) else None
    columns_bbox = _bbox_for_tag(columns, bboxes) if isinstance(columns, Tag) else None
    if header_bbox and columns_bbox:
        gap = int(columns_bbox["y"] - (header_bbox["y"] + header_bbox["h"]))
        if gap < 10:
            hints.append(
                f"Header/body gap is {gap}px; keep the header compact and move `.poster-columns` below it without changing canvas size."
            )

    column_tags = [
        tag for tag in soup.select(".poster-column,[data-column-id]")
        if isinstance(tag, Tag)
    ]
    for column in column_tags[:4]:
        column_bbox = _bbox_for_tag(column, bboxes)
        if not column_bbox:
            continue
        sections = [
            child for child in column.find_all(recursive=False)
            if isinstance(child, Tag) and (
                "poster-section" in _class_tokens(child)
                or str(child.get("data-block-kind") or "").lower() in {"panel", "section"}
            )
        ]
        section_boxes = [
            (section, _bbox_for_tag(section, bboxes))
            for section in sections
        ]
        valid_section_boxes = [
            (section, bbox) for section, bbox in section_boxes
            if bbox
        ]
        for section, bbox in valid_section_boxes:
            bottom = int(bbox["y"] + bbox["h"])
            if bottom > ch + 8 or int(bbox["y"]) >= ch:
                label = _section_label(section)
                hints.append(
                    f"Section `{label}` is out of canvas by {max(0, bottom - ch)}px; shorten text in earlier sibling sections and reduce only local image/table max-height."
                )
        if valid_section_boxes:
            last_bottom = max(int(bbox["y"] + bbox["h"]) for _, bbox in valid_section_boxes)
            column_bottom = min(ch, int(column_bbox["y"] + column_bbox["h"]))
            gap = column_bottom - last_bottom
            if gap > max(140, int(ch * 0.10)):
                label = _section_label(valid_section_boxes[-1][0])
                hints.append(
                    f"Column `{_column_label(column)}` underfills its bottom by {gap}px after `{label}`; expand existing source-backed flow or add one concise source-backed section, not a global rescale."
                )

    native_table = _native_benchmark_table_policy_error(soup, None)
    if native_table and isinstance(native_table.payload, dict):
        hints.append(str(native_table.payload.get("hint") or "Replace large native benchmark/results tables with bound ingest_table_* source evidence."))

    if shell_error and isinstance(shell_error.payload, dict):
        issues = shell_error.payload.get("issues")
        if isinstance(issues, list) and issues:
            issue_id = str(shell_error.payload.get("issue_id") or "")
            valid_issues = [issue for issue in issues if isinstance(issue, dict)]
            if issue_id == "paper_poster_html_row_allocation_density_regression":
                max_scroll = _local_flow_overflow_px(valid_issues)
                targets: list[str] = []
                for issue in sorted(
                    valid_issues,
                    key=lambda item: _safe_int(
                        (item.get("scroll_overflow_px") or {}).get("bottom")
                        if isinstance(item.get("scroll_overflow_px"), dict) else
                        item.get("bottom_overflow_px")
                    ),
                    reverse=True,
                )[:5]:
                    target = str(
                        issue.get("section_id")
                        or issue.get("flow_unit_id")
                        or issue.get("overflow_block_id")
                        or issue.get("container_id")
                        or ""
                    )
                    scroll = issue.get("scroll_overflow_px") if isinstance(issue.get("scroll_overflow_px"), dict) else {}
                    bottom_scroll = _safe_int(issue.get("bottom_overflow_px"), default=_safe_int(scroll.get("bottom")))
                    if target:
                        targets.append(f"`{target}` {bottom_scroll}px")
                target_text = ", ".join(targets)
                hints.append(
                    f"Global row allocation overflow: max {max_scroll}px across {len(valid_issues)} measured containers/sections"
                    f"{f' ({target_text})' if target_text else ''}. Rebalance all listed columns/section rows and clear every "
                    "scroll_overflow_px.bottom before local micro-trims; do not patch only one named section."
                )
                return " ".join(hints)[:1200]
            first = next(
                (
                    issue for issue in issues
                    if isinstance(issue, dict)
                    and (
                        issue.get("section_id")
                        or issue.get("flow_unit_id")
                        or issue.get("overflow_block_id")
                    )
                ),
                issues[0] if isinstance(issues[0], dict) else {},
            )
            section_id = str(first.get("section_id") or "")
            flow_unit_id = str(first.get("flow_unit_id") or "")
            overflow_block_id = str(first.get("overflow_block_id") or "")
            block_id = str(first.get("block_id") or first.get("container_id") or first.get("id") or "")
            scroll = first.get("scroll_overflow_px") if isinstance(first.get("scroll_overflow_px"), dict) else {}
            bottom_scroll = _safe_int(scroll.get("bottom"))
            if section_id or flow_unit_id or overflow_block_id:
                target = section_id or flow_unit_id or overflow_block_id
                hints.append(
                    f"Local flow overflow is in `{target}`"
                    f"{f' (`{flow_unit_id}`)' if flow_unit_id and flow_unit_id != target else ''}"
                    f" by {bottom_scroll}px; patch that section/flow unit before changing global layout."
                )
            elif block_id:
                hints.append(f"Measured shell issue is `{block_id}`; repair that local block before changing global layout.")

    if not hints:
        hints.append(
            f"Keep the {cw}x{ch} canvas fixed; make local repairs only: shorten dense text, lower local max-height, or expand underfilled section flow."
        )
    return " ".join(hints)[:1200]


def _bbox_for_tag(tag: Tag | None, bboxes: dict[str, dict[str, int]]) -> dict[str, int] | None:
    if not isinstance(tag, Tag):
        return None
    block_id = str(tag.get("data-block-id") or "").strip()
    if block_id and isinstance(bboxes.get(block_id), dict):
        return bboxes[block_id]
    for child in tag.find_all(True):
        if not isinstance(child, Tag):
            continue
        child_id = str(child.get("data-block-id") or "").strip()
        if child_id and isinstance(bboxes.get(child_id), dict):
            return bboxes[child_id]
    return None


def _column_label(tag: Tag) -> str:
    for key in ("data-column-id", "data-block-id", "data-panel-role", "data-role"):
        value = str(tag.get(key) or "").strip()
        if value:
            return value
    classes = sorted(_class_tokens(tag))
    return ".".join(classes[:2]) or "poster-column"


def _section_label(tag: Tag) -> str:
    for key in ("data-section-id", "data-block-id", "data-panel-role", "data-role"):
        value = str(tag.get(key) or "").strip()
        if value:
            return value
    heading = tag.find(["h1", "h2", "h3", "h4"])
    if isinstance(heading, Tag):
        text = heading.get_text(" ", strip=True)
        if text:
            return text[:80]
    return "poster-section"


def _fallback_dom_bboxes(soup: BeautifulSoup, canvas: dict[str, Any]) -> dict[str, dict[str, int]]:
    cw = int(canvas["w_px"])
    ch = int(canvas["h_px"])
    tags = [tag for tag in soup.find_all(True) if isinstance(tag, Tag) and tag.get("data-block-id")]
    bboxes: dict[str, dict[str, int]] = {}
    y = 48
    margin = 48
    width = max(1, cw - margin * 2)
    for tag in tags:
        block_id = str(tag.get("data-block-id") or "")
        bbox = _bbox_from_tag_attrs(tag, canvas)
        if bbox is None:
            kind = _infer_block_kind(tag)
            text_words = len(tag.get_text(" ", strip=True).split())
            height = 180 if kind in {"image", "table", "group"} else max(56, min(180, 28 + text_words * 5))
            bbox = {"x": margin, "y": y, "w": width, "h": height}
            y = min(ch - margin, y + height + 24)
        bboxes[block_id] = bbox
    return bboxes


def _bbox_from_tag_attrs(tag: Tag, canvas: dict[str, Any]) -> dict[str, int] | None:
    data_bbox = str(tag.get("data-bbox") or "").strip()
    if data_bbox:
        parsed = _parse_data_bbox(data_bbox, canvas)
        if parsed:
            return parsed
    style = str(tag.get("style") or "")
    return _parse_style_bbox(style, canvas)


def _parse_data_bbox(value: str, canvas: dict[str, Any]) -> dict[str, int] | None:
    try:
        if value.startswith("{"):
            return _coerce_bbox(json.loads(value), canvas)
        parts = [float(part.strip()) for part in re.split(r"[, ]+", value) if part.strip()]
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if len(parts) != 4:
        return None
    return _coerce_bbox({"x": parts[0], "y": parts[1], "w": parts[2], "h": parts[3]}, canvas)


def _parse_style_bbox(style: str, canvas: dict[str, Any]) -> dict[str, int] | None:
    values: dict[str, float] = {}
    for prop, key in (("left", "x"), ("top", "y"), ("width", "w"), ("height", "h")):
        match = re.search(rf"(?:^|;)\s*{prop}\s*:\s*([^;]+)", style, flags=re.IGNORECASE)
        if not match:
            continue
        parsed = _css_length_to_px(match.group(1), canvas, axis="x" if key in {"x", "w"} else "y")
        if parsed is not None:
            values[key] = parsed
    if {"x", "y", "w", "h"} <= set(values):
        return _coerce_bbox(values, canvas)
    return None


def _css_length_to_px(value: str, canvas: dict[str, Any], *, axis: str) -> float | None:
    raw = str(value or "").strip()
    match = re.match(r"(-?\d+(?:\.\d+)?)\s*(px|%)?$", raw)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2) or "px"
    if unit == "%":
        base = float(canvas["w_px"] if axis == "x" else canvas["h_px"])
        return base * number / 100.0
    return number


def _coerce_bbox(value: dict[str, Any], canvas: dict[str, Any]) -> dict[str, int] | None:
    try:
        x = int(round(float(value.get("x") or value.get("left") or 0)))
        y = int(round(float(value.get("y") or value.get("top") or 0)))
        w = int(round(float(value.get("w") or value.get("width") or 0)))
        h = int(round(float(value.get("h") or value.get("height") or 0)))
    except (TypeError, ValueError):
        return None
    cw = int(canvas["w_px"])
    ch = int(canvas["h_px"])
    x = max(-cw, min(cw * 2, x))
    y = max(-ch, min(ch * 2, y))
    w = max(1, min(cw * 2, w))
    h = max(1, min(ch * 2, h))
    return {"x": x, "y": y, "w": w, "h": h}


def _text_line_bboxes_for_measurement(item: dict[str, Any], canvas: dict[str, Any]) -> list[dict[str, int]]:
    raw_rects = item.get("textLineRects")
    if not isinstance(raw_rects, list):
        return []
    out: list[dict[str, int]] = []
    seen: set[tuple[int, int, int, int]] = set()
    for raw_rect in raw_rects[:180]:
        if not isinstance(raw_rect, dict):
            continue
        bbox = _coerce_bbox(raw_rect, canvas)
        if not bbox or bbox["w"] * bbox["h"] < 20:
            continue
        key = (bbox["x"], bbox["y"], bbox["w"], bbox["h"])
        if key in seen:
            continue
        seen.add(key)
        out.append(bbox)
    return out


def _computed_style_for_measurement(item: dict[str, Any]) -> dict[str, Any]:
    style: dict[str, Any] = {}
    if "rendered" in item:
        style["rendered"] = bool(item.get("rendered"))
    font_size = _css_px_value(str(item.get("fontSize") or ""))
    if font_size is not None:
        style["font_size_px"] = font_size
    line_height = _css_px_value(str(item.get("lineHeight") or ""))
    if line_height is not None:
        style["line_height"] = round(line_height / max(1.0, font_size or line_height), 3)
    font_weight = str(item.get("fontWeight") or "").strip()
    if font_weight:
        style["font_weight"] = font_weight
    font_family = str(item.get("fontFamily") or "").strip()
    if font_family:
        style["font_family"] = font_family
    font_style = str(item.get("fontStyle") or "").strip().lower()
    if font_style:
        style["font_style"] = font_style
    color = str(item.get("color") or "").strip()
    if color:
        style["fill"] = color
    background_color = str(item.get("backgroundColor") or "").strip().lower()
    if background_color:
        style["background_color"] = background_color
    opacity = str(item.get("opacity") or "").strip()
    if opacity:
        try:
            style["opacity"] = float(opacity)
        except ValueError:
            pass
    box_shadow = str(item.get("boxShadow") or "").strip().lower()
    if box_shadow:
        style["box_shadow"] = box_shadow
    outline_width = _css_px_value(str(item.get("outlineWidth") or ""))
    if outline_width is not None:
        style["outline_width_px"] = outline_width
    outline_style = str(item.get("outlineStyle") or "").strip().lower()
    if outline_style:
        style["outline_style"] = outline_style
    outline_color = str(item.get("outlineColor") or "").strip().lower()
    if outline_color:
        style["outline_color"] = outline_color
    text_align = str(item.get("textAlign") or "").strip()
    if text_align == "start":
        text_align = "left"
    elif text_align == "end":
        text_align = "right"
    if text_align:
        if text_align in {"left", "center", "right"}:
            style["align"] = text_align
    for source_key, out_key in (
        ("paddingTop", "padding_top_px"),
        ("paddingRight", "padding_right_px"),
        ("paddingBottom", "padding_bottom_px"),
        ("paddingLeft", "padding_left_px"),
    ):
        parsed = _css_px_value(str(item.get(source_key) or ""))
        if parsed is not None:
            style[out_key] = parsed
    for side in ("Top", "Right", "Bottom", "Left"):
        side_key = side.lower()
        width = _css_px_value(str(item.get(f"border{side}Width") or ""))
        if width is not None:
            style[f"border_{side_key}_width_px"] = width
        border_style = str(item.get(f"border{side}Style") or "").strip().lower()
        if border_style:
            style[f"border_{side_key}_style"] = border_style
        border_color = str(item.get(f"border{side}Color") or "").strip().lower()
        if border_color:
            style[f"border_{side_key}_color"] = border_color
    for source_key, out_key in (
        ("boxSizing", "box_sizing"),
        ("display", "display"),
        ("visibility", "visibility"),
        ("alignItems", "align_items"),
        ("justifyContent", "justify_content"),
        ("justifyItems", "justify_items"),
        ("flexDirection", "flex_direction"),
        ("whiteSpace", "white_space"),
        ("overflow", "overflow"),
        ("overflowX", "overflow_x"),
        ("overflowY", "overflow_y"),
        ("gridTemplateRows", "grid_template_rows"),
        ("gridTemplateColumns", "grid_template_columns"),
    ):
        raw = str(item.get(source_key) or "").strip()
        if raw:
            style[out_key] = raw
    return style


def _layout_metrics_for_measurement(item: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for source_key, out_key in (
        ("scrollWidth", "scroll_width_px"),
        ("scrollHeight", "scroll_height_px"),
        ("clientWidth", "client_width_px"),
        ("clientHeight", "client_height_px"),
    ):
        try:
            metrics[out_key] = float(item.get(source_key) or 0.0)
        except (TypeError, ValueError):
            continue
    white_space = str(item.get("whiteSpace") or "").strip()
    if white_space:
        metrics["white_space"] = white_space
    return {key: value for key, value in metrics.items() if value}


def _text_clipping_issues(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        kind = _infer_block_kind(tag)
        if kind not in {"text", "caption", "quote", "metric"}:
            continue
        block_id = str(tag.get("data-block-id") or "").strip()
        bbox = bboxes.get(block_id)
        if not block_id or not isinstance(bbox, dict):
            continue
        layout_metrics = bbox.get("_layout_metrics") if isinstance(bbox.get("_layout_metrics"), dict) else {}
        if not layout_metrics:
            continue
        scroll_width = _safe_float(layout_metrics.get("scroll_width_px"))
        scroll_height = _safe_float(layout_metrics.get("scroll_height_px"))
        client_width = _safe_float(layout_metrics.get("client_width_px"))
        client_height = _safe_float(layout_metrics.get("client_height_px"))
        width_clipped = scroll_width > client_width + 1.0 if client_width > 0 else False
        height_clipped = scroll_height > client_height + 1.0 if client_height > 0 else False
        if not width_clipped and not height_clipped:
            continue
        current_width = max(client_width, float(bbox.get("w") or 0))
        current_height = max(client_height, float(bbox.get("h") or 0))
        axes: list[str] = []
        if width_clipped:
            axes.append("x")
        if height_clipped:
            axes.append("y")
        issue: dict[str, Any] = {
            "block_id": block_id,
            "kind": kind,
            "text_word_count": len(tag.get_text(" ", strip=True).split()),
            "clip_axes": axes,
            "bbox": _bbox_only(bbox) or {},
            "measured_metrics": {
                "scroll_width_px": round(scroll_width, 2),
                "client_width_px": round(client_width, 2),
                "scroll_height_px": round(scroll_height, 2),
                "client_height_px": round(client_height, 2),
            },
            "measured_overflow_px": {
                "x": round(max(0.0, scroll_width - client_width), 2) if width_clipped else 0,
                "y": round(max(0.0, scroll_height - client_height), 2) if height_clipped else 0,
            },
            "effective_client_width_px": round(current_width, 2),
            "effective_client_height_px": round(current_height, 2),
            "effective_overflow_px": {
                "x": round(max(0.0, scroll_width - current_width), 2) if width_clipped else 0,
                "y": round(max(0.0, scroll_height - current_height), 2) if height_clipped else 0,
            },
        }
        core_lane = _core_text_lane(tag)
        if core_lane:
            issue["core_lane"] = core_lane
        white_space = str(layout_metrics.get("white_space") or "").strip()
        if white_space:
            issue["white_space"] = white_space
        issues.append(issue)
    return issues


def _severe_text_clipping_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    severe: list[dict[str, Any]] = []
    for issue in issues:
        overflow = issue.get("effective_overflow_px") if isinstance(issue.get("effective_overflow_px"), dict) else {}
        width_overflow = _safe_float(overflow.get("x"))
        height_overflow = _safe_float(overflow.get("y"))
        effective_width = max(1.0, _safe_float(issue.get("effective_client_width_px"), 1.0))
        effective_height = max(1.0, _safe_float(issue.get("effective_client_height_px"), 1.0))
        core_lane = str(issue.get("core_lane") or "")
        width_limit = max(12.0, effective_width * (0.025 if core_lane else 0.05))
        height_limit = max(14.0, effective_height * (0.04 if core_lane else 0.08))
        if height_overflow > height_limit:
            severe.append({
                **issue,
                "severity_reason": "core_lane_text_height_clipping" if core_lane else "text_height_clipping",
                "max_tolerated_overflow_px": {"x": round(width_limit, 2), "y": round(height_limit, 2)},
            })
        elif width_overflow > width_limit:
            severe.append({
                **issue,
                "severity_reason": "core_lane_text_width_clipping" if core_lane else "text_width_clipping",
                "max_tolerated_overflow_px": {"x": round(width_limit, 2), "y": round(height_limit, 2)},
            })
    return severe


def _css_px_value(value: str) -> float | None:
    match = re.match(r"(-?\d+(?:\.\d+)?)px$", str(value or "").strip())
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _bbox_only(value: dict[str, Any] | None) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    try:
        return {
            "x": int(value.get("x") or 0),
            "y": int(value.get("y") or 0),
            "w": int(value.get("w") or 0),
            "h": int(value.get("h") or 0),
        }
    except (TypeError, ValueError):
        return None


def _bbox_boundary_issues(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
) -> list[dict[str, Any]]:
    cw = int(canvas["w_px"])
    ch = int(canvas["h_px"])
    tolerance = 3
    issues: list[dict[str, Any]] = []
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        block_id = str(tag.get("data-block-id") or "").strip()
        bbox = bboxes.get(block_id)
        if not block_id or not bbox:
            continue
        visible_bbox = bbox.get("_visible_bbox") if isinstance(bbox.get("_visible_bbox"), dict) else None
        if visible_bbox and (
            _safe_int(visible_bbox.get("w")) <= 1
            or _safe_int(visible_bbox.get("h")) <= 1
        ):
            continue
        x = int(bbox.get("x") or 0)
        y = int(bbox.get("y") or 0)
        w = int(bbox.get("w") or 0)
        h = int(bbox.get("h") or 0)
        if x < -tolerance or y < -tolerance or x + w > cw + tolerance or y + h > ch + tolerance:
            issues.append({
                "block_id": block_id,
                "kind": _infer_block_kind(tag),
                "bbox": {"x": x, "y": y, "w": w, "h": h},
                "raw_bbox": _bbox_only(bbox),
            })
    return issues


def _expanded_canvas_for_boundary_issues(
    issues: list[dict[str, Any]],
    canvas: dict[str, Any],
    ctx: ToolContext,
) -> dict[str, Any] | None:
    if not issues or not _canvas_auto_expand_enabled(ctx):
        return None
    cw = max(1, int(canvas["w_px"]))
    ch = max(1, int(canvas["h_px"]))
    pad = max(24, round(min(cw, ch) * 0.02))
    right = cw
    bottom = ch
    left_overflow = 0
    top_overflow = 0
    for issue in issues:
        bbox = issue.get("bbox") if isinstance(issue.get("bbox"), dict) else {}
        x = int(bbox.get("x") or 0)
        y = int(bbox.get("y") or 0)
        w = int(bbox.get("w") or 0)
        h = int(bbox.get("h") or 0)
        left_overflow = max(left_overflow, max(0, -x))
        top_overflow = max(top_overflow, max(0, -y))
        right = max(right, x + w + pad)
        bottom = max(bottom, y + h + pad)
    if left_overflow or top_overflow:
        return None
    max_w = round(cw * 1.35)
    max_h = round(ch * 1.35)
    new_w = min(max_w, max(cw, right))
    new_h = min(max_h, max(ch, bottom))
    if new_w <= cw and new_h <= ch:
        return None
    if new_w < right or new_h < bottom:
        return None
    expanded = dict(canvas)
    expanded["w_px"] = int(new_w)
    expanded["h_px"] = int(new_h)
    expanded["aspect_ratio"] = _aspect_ratio_label(expanded)
    expanded["canvas_auto_expand_reason"] = "measured_blocks_exceeded_soft_canvas"
    return expanded


def _canvas_auto_expand_enabled(ctx: ToolContext) -> bool:
    raw = (
        os.getenv("AUTODESIGN_POSTER_CANVAS_AUTO_EXPAND", "")
        or os.getenv("DESIGN_ANYTHING_POSTER_CANVAS_AUTO_EXPAND", "")
        or os.getenv("POSTER_CANVAS_AUTO_EXPAND", "")
    ).strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    plan = ctx.state.get("canvas_plan") if isinstance(ctx.state, dict) else None
    if not isinstance(plan, dict):
        return False
    if (
        str(plan.get("preset_id") or "") == "cvpr-landscape"
        or ((plan.get("body_grid") or {}).get("family") if isinstance(plan.get("body_grid"), dict) else None) == "cvpr_3col"
    ):
        return False
    if str(plan.get("lock_level") or "").lower() == "hard":
        return False
    return _normalized_canvas_record(plan.get("canvas")) is not None


def _severe_boundary_issues(
    issues: list[dict[str, Any]],
    canvas: dict[str, Any],
) -> list[dict[str, Any]]:
    cw = max(1, int(canvas["w_px"]))
    ch = max(1, int(canvas["h_px"]))
    max_x_overflow = max(24, int(round(cw * 0.025)))
    max_y_overflow = max(24, int(round(ch * 0.04)))
    severe: list[dict[str, Any]] = []
    for issue in issues:
        bbox = issue.get("bbox") if isinstance(issue.get("bbox"), dict) else {}
        x = int(bbox.get("x") or 0)
        y = int(bbox.get("y") or 0)
        w = int(bbox.get("w") or 0)
        h = int(bbox.get("h") or 0)
        overflow_left = max(0, -x)
        overflow_top = max(0, -y)
        overflow_right = max(0, x + w - cw)
        overflow_bottom = max(0, y + h - ch)
        if (
            overflow_left > max_x_overflow
            or overflow_right > max_x_overflow
            or overflow_top > max_y_overflow
            or overflow_bottom > max_y_overflow
        ):
            severe.append({
                **issue,
                "overflow_px": {
                    "left": overflow_left,
                    "right": overflow_right,
                    "top": overflow_top,
                    "bottom": overflow_bottom,
                },
                "max_tolerated_overflow_px": {
                    "x": max_x_overflow,
                    "y": max_y_overflow,
                },
            })
    return severe


def _expand_text_bboxes_for_fit(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
) -> None:
    ch = int(canvas["h_px"])
    text_tags = [
        tag for tag in soup.find_all(True)
        if (
            isinstance(tag, Tag)
            and (kind := _infer_block_kind(tag)) in {"text", "caption", "quote"}
            and not _should_skip_fixed_lane_child_geometry(tag, kind)
        )
    ]
    text_tags.sort(key=lambda tag: int((bboxes.get(str(tag.get("data-block-id") or "")) or {}).get("y") or 0))
    for tag in text_tags:
        if not isinstance(tag, Tag) or _infer_block_kind(tag) not in {"text", "caption", "quote"}:
            continue
        block_id = str(tag.get("data-block-id") or "").strip()
        bbox = bboxes.get(block_id)
        if not block_id or not isinstance(bbox, dict):
            continue
        text = tag.get_text(" ", strip=True)
        if not text:
            continue
        computed = bbox.get("_computed_style") if isinstance(bbox.get("_computed_style"), dict) else {}
        layout_metrics = bbox.get("_layout_metrics") if isinstance(bbox.get("_layout_metrics"), dict) else {}
        font_size = float(computed.get("font_size_px") or 17.0)
        line_height_ratio = float(computed.get("line_height") or 1.18)
        line_height = line_height_ratio * max(1.0, font_size)
        width = max(1.0, float(bbox.get("w") or 1))
        chars_per_line = max(8, int(width / max(5.0, font_size * 0.56)))
        line_count = max(1, (len(text) + chars_per_line - 1) // chars_per_line)
        scroll_width = _safe_float(layout_metrics.get("scroll_width_px"))
        scroll_height = _safe_float(layout_metrics.get("scroll_height_px"))
        if scroll_width > width * 1.03:
            line_count = max(line_count, int((scroll_width + width - 1) // width))
        if _tag_is_title_like(tag) and len(text.split()) > 8:
            line_count = max(2, line_count)
        required_h = int(round(line_count * line_height + max(4.0, font_size * 0.35)))
        if scroll_height > 0:
            required_h = max(required_h, int(round(scroll_height + max(4.0, font_size * 0.25))))
        available_h, parent_bottom = _text_fit_available_height(tag, bbox, bboxes, canvas, font_size)
        if required_h > available_h and _tag_is_title_like(tag) and font_size > 28:
            scale = max(0.72, min(1.0, available_h / float(max(1, required_h))))
            fit_font = max(28, int(round(font_size * scale)))
            if fit_font < font_size:
                bbox["_fit_font_size_px"] = fit_font
                font_size = float(fit_font)
                line_height = line_height_ratio * max(1.0, font_size)
                chars_per_line = max(8, int(width / max(5.0, font_size * 0.56)))
                line_count = max(1, (len(text) + chars_per_line - 1) // chars_per_line)
                if _tag_is_title_like(tag) and len(text.split()) > 8:
                    line_count = max(2, line_count)
                required_h = int(round(line_count * line_height + max(4.0, font_size * 0.35)))
        old_h = int(bbox.get("h") or 0)
        if required_h > old_h:
            new_h = min(max(required_h, old_h), available_h)
            bbox["h"] = new_h
            _shift_later_sibling_bboxes(
                tag,
                bboxes,
                delta=new_h - old_h,
                old_bottom=int(bbox.get("y") or 0) + old_h,
                max_bottom=parent_bottom,
            )


def _text_fit_available_height(
    tag: Tag,
    bbox: dict[str, Any],
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
    font_size: float,
) -> tuple[int, int | None]:
    y = int(bbox.get("y") or 0)
    ch = int(canvas["h_px"])
    canvas_available = max(1, ch - y - 4)
    parent_id = _nearest_parent_block_id(tag)
    parent_bbox = bboxes.get(parent_id) if parent_id else None
    if not isinstance(parent_bbox, dict):
        return canvas_available, None
    parent_y = int(parent_bbox.get("y") or 0)
    parent_h = int(parent_bbox.get("h") or 0)
    if parent_h <= 0 or parent_y > y + 2:
        return canvas_available, None
    padding = max(4, int(round(font_size * 0.45)))
    parent_bottom = parent_y + parent_h - padding
    if parent_bottom <= y:
        return canvas_available, None
    return max(1, min(canvas_available, parent_bottom - y)), parent_bottom


def _tag_is_title_like(tag: Tag) -> bool:
    name = str(tag.name or "").lower()
    role_blob = " ".join(
        str(tag.get(key) or "")
        for key in ("data-block-id", "data-role", "role", "class", "data-panel-role")
    ).lower()
    return name == "h1" or "poster_title" in role_blob or "title" in role_blob


def _semantic_role_blob(tag: Tag, *, include_ancestors: bool = False) -> str:
    parts: list[str] = []
    node: Tag | None = tag
    depth = 0
    while isinstance(node, Tag):
        parts.append(str(node.name or ""))
        for key in ("id", "data-block-id", "data-role", "role", "class", "data-panel-role"):
            parts.append(str(node.get(key) or ""))
        if not include_ancestors:
            break
        node = node.parent if isinstance(node.parent, Tag) else None
        depth += 1
        if depth >= 4:
            break
    return " ".join(parts).replace("_", "-").lower()


def _core_text_lane(tag: Tag) -> str:
    name = str(tag.name or "").lower()
    role_blob = _semantic_role_blob(tag, include_ancestors=True)
    if name == "h1" or any(
        token in role_blob
        for token in ("poster-title", "poster title", "main-title", "main title", "title-strip")
    ):
        return "title"
    if any(token in role_blob for token in ("author", "authors", "byline")):
        return "authors_byline"
    if "thesis" in role_blob:
        return "thesis"
    if "abstract" in role_blob:
        return "abstract"
    if "header" in role_blob:
        return "header"
    if "footer" in role_blob:
        return "footer"
    if (
        "panel-title" in role_blob
        or ("panel" in role_blob and "title" in role_blob)
        or name in {"h2", "h3", "h4"}
    ):
        return "panel_title"
    if "title" in role_blob:
        return "title"
    if any(
        token in role_blob
        for token in (
            "body-lane", "body lane", "body-copy", "body copy", "body-prose",
            "body prose", "main-copy", "main copy", "main-text", "main text",
            "prose", "paragraph",
        )
    ):
        return "body_prose"
    return ""


def _shift_later_sibling_bboxes(
    tag: Tag,
    bboxes: dict[str, dict[str, int]],
    *,
    delta: int,
    old_bottom: int,
    max_bottom: int | None = None,
) -> None:
    if delta <= 0:
        return
    parent_id = _nearest_parent_block_id(tag)
    if not parent_id:
        return
    block_id = str(tag.get("data-block-id") or "").strip()
    for other in tag.parent.find_all(True, recursive=False) if isinstance(tag.parent, Tag) else []:
        if not isinstance(other, Tag) or other is tag:
            continue
        other_id = str(other.get("data-block-id") or "").strip()
        other_bbox = bboxes.get(other_id)
        if not other_id or other_id == block_id or not isinstance(other_bbox, dict):
            continue
        if _nearest_parent_block_id(other) != parent_id:
            continue
        if int(other_bbox.get("y") or 0) >= old_bottom - 2:
            shift_delta = delta
            if max_bottom is not None:
                other_bottom = int(other_bbox.get("y") or 0) + int(other_bbox.get("h") or 0)
                shift_delta = min(delta, max(0, max_bottom - other_bottom))
            if shift_delta > 0:
                _shift_tag_subtree_bboxes(other, bboxes, delta=shift_delta)


def _shift_tag_subtree_bboxes(tag: Tag, bboxes: dict[str, dict[str, int]], *, delta: int) -> None:
    _move_tag_subtree_bboxes(tag, bboxes, dx=0, dy=delta)


def _move_tag_subtree_bboxes(tag: Tag, bboxes: dict[str, dict[str, int]], *, dx: int, dy: int) -> None:
    for node in [tag, *tag.find_all(True)]:
        if not isinstance(node, Tag):
            continue
        block_id = str(node.get("data-block-id") or "").strip()
        bbox = bboxes.get(block_id)
        if block_id and isinstance(bbox, dict):
            bbox["x"] = int(bbox.get("x") or 0) + dx
            bbox["y"] = int(bbox.get("y") or 0) + dy


def _text_overlap_issues(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    text_tags: list[tuple[Tag, str, dict[str, int], int, str]] = []
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        block_id = str(tag.get("data-block-id") or "").strip()
        bbox = bboxes.get(block_id)
        if not block_id or not bbox or _infer_block_kind(tag) not in {"text", "caption", "quote", "metric"}:
            continue
        overlap_bbox = _visible_bbox_for_overlap(bbox)
        if _safe_int(overlap_bbox.get("w")) <= 1 or _safe_int(overlap_bbox.get("h")) <= 1:
            continue
        words = len(tag.get_text(" ", strip=True).split())
        if words < 3:
            continue
        kind = _infer_block_kind(tag)
        if _should_skip_fixed_lane_child_geometry(tag, kind):
            continue
        text_tags.append((tag, block_id, overlap_bbox, words, _core_text_lane(tag)))

    issues: list[dict[str, Any]] = []
    for idx, (left_tag, left_id, left_bbox, left_words, left_core_lane) in enumerate(text_tags):
        for right_tag, right_id, right_bbox, right_words, right_core_lane in text_tags[idx + 1:]:
            if _is_ancestor(left_tag, right_tag) or _is_ancestor(right_tag, left_tag):
                continue
            overlap = _bbox_overlap_area(left_bbox, right_bbox)
            if overlap <= 0:
                continue
            smaller = min(_bbox_plain_area(left_bbox), _bbox_plain_area(right_bbox))
            ratio = overlap / max(1, smaller)
            if left_core_lane or right_core_lane:
                if overlap < 320 and ratio < 0.03:
                    continue
            elif overlap < 1200 and ratio < 0.20:
                continue
            issue = {
                "left_block_id": left_id,
                "right_block_id": right_id,
                "left_word_count": left_words,
                "right_word_count": right_words,
                "overlap_area_px": round(overlap, 2),
                "overlap_ratio_of_smaller": round(ratio, 4),
                "left_bbox": left_bbox,
                "right_bbox": right_bbox,
            }
            if left_core_lane:
                issue["left_core_lane"] = left_core_lane
            if right_core_lane:
                issue["right_core_lane"] = right_core_lane
            issues.append(issue)
    return issues


def _visible_bbox_for_overlap(bbox: dict[str, Any]) -> dict[str, int]:
    visible = bbox.get("_visible_bbox")
    if isinstance(visible, dict):
        try:
            x = int(round(float(visible.get("x") or 0)))
            y = int(round(float(visible.get("y") or 0)))
            w = int(round(float(visible.get("w") or 0)))
            h = int(round(float(visible.get("h") or 0)))
        except (TypeError, ValueError):
            return bbox
        if w > 0 and h > 0:
            return {"x": x, "y": y, "w": w, "h": h}
    return bbox


def _severe_text_overlap_issues(overlap_issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    severe: list[dict[str, Any]] = []
    for issue in overlap_issues:
        if issue.get("left_core_lane") or issue.get("right_core_lane"):
            severe.append({
                **issue,
                "severity_reason": "core_lane_overlap",
            })
            continue
        ratio = _safe_float(issue.get("overlap_ratio_of_smaller"))
        area = _safe_float(issue.get("overlap_area_px"))
        min_words = min(
            _safe_int(issue.get("left_word_count")),
            _safe_int(issue.get("right_word_count")),
        )
        if min_words >= 5 and ratio >= 0.30 and area >= 8000:
            severe.append(issue)
        elif min_words >= 3 and ratio >= 0.55 and area >= 3000:
            severe.append(issue)
    return severe


def _resolve_severe_text_overlaps(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
) -> list[dict[str, Any]]:
    cw = int(canvas["w_px"])
    ch = int(canvas["h_px"])
    tag_by_id = {
        str(tag.get("data-block-id") or ""): tag
        for tag in soup.find_all(True)
        if isinstance(tag, Tag) and str(tag.get("data-block-id") or "").strip()
    }
    repairs: list[dict[str, Any]] = []
    for _ in range(3):
        severe = _severe_text_overlap_issues(_text_overlap_issues(soup, bboxes))
        if not severe:
            break
        changed = False
        for issue in severe[:12]:
            left_id = str(issue.get("left_block_id") or "")
            right_id = str(issue.get("right_block_id") or "")
            left_bbox = bboxes.get(left_id)
            right_bbox = bboxes.get(right_id)
            if not isinstance(left_bbox, dict) or not isinstance(right_bbox, dict):
                continue
            left_tag = tag_by_id.get(left_id)
            right_tag = tag_by_id.get(right_id)
            if not isinstance(left_tag, Tag) or not isinstance(right_tag, Tag):
                continue
            moving_id, moving_tag, moving_bbox, anchor_id, anchor_bbox = _choose_text_overlap_moving_tag(
                left_id=left_id,
                left_tag=left_tag,
                left_bbox=left_bbox,
                left_words=_safe_int(issue.get("left_word_count")),
                right_id=right_id,
                right_tag=right_tag,
                right_bbox=right_bbox,
                right_words=_safe_int(issue.get("right_word_count")),
            )
            anchor_tag = tag_by_id.get(anchor_id)
            if (
                not isinstance(anchor_tag, Tag)
                or not _is_leaf_text_repair_candidate(moving_tag)
                or _nearest_parent_block_id(moving_tag) != _nearest_parent_block_id(anchor_tag)
            ):
                continue
            parent_id = _nearest_parent_block_id(moving_tag)
            parent_bbox = bboxes.get(parent_id)
            if not isinstance(parent_bbox, dict):
                continue
            moving_bbox = bboxes.get(moving_id)
            if not isinstance(moving_bbox, dict) or not isinstance(moving_tag, Tag):
                continue
            next_bbox = _best_text_overlap_candidate_bbox(
                soup,
                bboxes,
                moving_id=moving_id,
                moving_tag=moving_tag,
                moving_bbox=moving_bbox,
                anchor_bbox=anchor_bbox,
                parent_bbox=parent_bbox,
                canvas_w=cw,
                canvas_h=ch,
            )
            if next_bbox is None:
                continue
            old_bbox = _bbox_only(moving_bbox)
            if old_bbox is None:
                continue
            dx = int(next_bbox["x"]) - old_bbox["x"]
            dy = int(next_bbox["y"]) - old_bbox["y"]
            if dx == 0 and dy == 0:
                continue
            before_bboxes = {key: dict(value) for key, value in bboxes.items()}
            before_score = _text_overlap_repair_score(_text_overlap_issues(soup, bboxes))
            before_boundary = len(_severe_boundary_issues(_bbox_boundary_issues(soup, bboxes, canvas), canvas))
            _move_tag_subtree_bboxes(moving_tag, bboxes, dx=dx, dy=dy)
            after_score = _text_overlap_repair_score(_text_overlap_issues(soup, bboxes))
            after_boundary = len(_severe_boundary_issues(_bbox_boundary_issues(soup, bboxes, canvas), canvas))
            if (
                after_score >= before_score
                or after_boundary > before_boundary
                or not _tag_subtree_inside_bbox(moving_tag, bboxes, parent_bbox)
            ):
                bboxes.clear()
                bboxes.update({key: dict(value) for key, value in before_bboxes.items()})
                continue
            repairs.append({
                "block_id": moving_id,
                "delta_x": dx,
                "delta_y": dy,
                "away_from": anchor_id,
                "old_bbox": old_bbox,
                "new_bbox": next_bbox,
                "anchor_bbox": _bbox_only(anchor_bbox),
            })
            changed = True
        if not changed:
            break
    return repairs


def _text_overlap_repair_score(overlap_issues: list[dict[str, Any]]) -> tuple[int, int, float]:
    severe = _severe_text_overlap_issues(overlap_issues)
    weighted = 0.0
    for issue in overlap_issues:
        weighted += _safe_float(issue.get("overlap_area_px")) * (
            1.0 + _safe_float(issue.get("overlap_ratio_of_smaller")) * 3.0
        )
    return (len(severe), len(overlap_issues), round(weighted, 3))


def _is_leaf_text_repair_candidate(tag: Tag) -> bool:
    if _infer_block_kind(tag) not in {"text", "caption", "quote", "metric"}:
        return False
    if any(
        isinstance(child, Tag) and str(child.get("data-block-id") or "").strip()
        for child in tag.find_all(True)
    ):
        return False
    role_blob = " ".join(
        str(tag.get(key) or "")
        for key in ("data-block-id", "data-role", "role", "class", "data-panel-role")
    ).lower()
    if _tag_is_title_like(tag) or any(
        token in role_blob
        for token in ("title", "author", "byline", "venue", "abstract", "header", "footer")
    ):
        return False
    return True


def _tag_subtree_inside_bbox(
    tag: Tag,
    bboxes: dict[str, dict[str, int]],
    parent_bbox: dict[str, int],
    *,
    tolerance: int = 2,
) -> bool:
    px = int(parent_bbox.get("x") or 0)
    py = int(parent_bbox.get("y") or 0)
    pr = px + int(parent_bbox.get("w") or 0)
    pb = py + int(parent_bbox.get("h") or 0)
    for node in [tag, *tag.find_all(True)]:
        if not isinstance(node, Tag):
            continue
        block_id = str(node.get("data-block-id") or "").strip()
        bbox = bboxes.get(block_id)
        if not block_id or not isinstance(bbox, dict):
            continue
        x = int(bbox.get("x") or 0)
        y = int(bbox.get("y") or 0)
        r = x + int(bbox.get("w") or 0)
        b = y + int(bbox.get("h") or 0)
        if x < px - tolerance or y < py - tolerance or r > pr + tolerance or b > pb + tolerance:
            return False
    return True


def _choose_text_overlap_moving_tag(
    *,
    left_id: str,
    left_tag: Tag,
    left_bbox: dict[str, int],
    left_words: int,
    right_id: str,
    right_tag: Tag,
    right_bbox: dict[str, int],
    right_words: int,
) -> tuple[str, Tag, dict[str, int], str, dict[str, int]]:
    left_score = _text_move_resistance(left_tag, left_bbox, left_words)
    right_score = _text_move_resistance(right_tag, right_bbox, right_words)
    if left_score <= right_score - 12:
        return left_id, left_tag, left_bbox, right_id, right_bbox
    if right_score <= left_score - 12:
        return right_id, right_tag, right_bbox, left_id, left_bbox
    if (left_bbox["y"], left_bbox["x"]) <= (right_bbox["y"], right_bbox["x"]):
        return right_id, right_tag, right_bbox, left_id, left_bbox
    return left_id, left_tag, left_bbox, right_id, right_bbox


def _text_move_resistance(tag: Tag, bbox: dict[str, int], word_count: int) -> float:
    role_blob = " ".join(
        str(tag.get(key) or "")
        for key in ("data-block-id", "data-role", "role", "class", "data-panel-role")
    ).lower()
    area = max(1, int(bbox.get("w") or 0) * int(bbox.get("h") or 0))
    score = float(max(0, word_count)) * 2.0 + min(120.0, area / 8000.0)
    if _tag_is_title_like(tag):
        score += 180.0
    if any(token in role_blob for token in ("author", "byline", "venue", "thesis", "abstract", "summary")):
        score += 70.0
    if any(token in role_blob for token in ("caption", "badge", "pill", "tag", "eyebrow", "metric", "stamp")):
        score -= 35.0
    if _infer_block_kind(tag) == "metric":
        score -= 45.0
    return score


def _best_text_overlap_candidate_bbox(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    *,
    moving_id: str,
    moving_tag: Tag,
    moving_bbox: dict[str, int],
    anchor_bbox: dict[str, int],
    parent_bbox: dict[str, int],
    canvas_w: int,
    canvas_h: int,
) -> dict[str, int] | None:
    w = int(moving_bbox.get("w") or 0)
    h = int(moving_bbox.get("h") or 0)
    if w <= 0 or h <= 0 or w >= canvas_w or h >= canvas_h:
        return None
    gutter = max(12, int(round(max(canvas_w, canvas_h) * 0.006)))
    margin = max(4, gutter)
    px = int(parent_bbox.get("x") or 0)
    py = int(parent_bbox.get("y") or 0)
    pw = int(parent_bbox.get("w") or 0)
    ph = int(parent_bbox.get("h") or 0)
    if pw <= w + margin * 2 or ph <= h + margin * 2:
        return None
    min_x = max(margin, px + margin)
    min_y = max(margin, py + margin)
    max_x = min(canvas_w - w - margin, px + pw - w - margin)
    max_y = min(canvas_h - h - margin, py + ph - h - margin)
    if max_x < min_x or max_y < min_y:
        return None
    start_x = int(moving_bbox.get("x") or 0)
    start_y = int(moving_bbox.get("y") or 0)
    current = {
        "x": min(max(min_x, start_x), max_x),
        "y": min(max(min_y, start_y), max_y),
        "w": w,
        "h": h,
    }
    current_score = _text_overlap_score_for_candidate(
        soup,
        bboxes,
        moving_id=moving_id,
        moving_tag=moving_tag,
        candidate_bbox=current,
    )

    anchor_x = int(anchor_bbox.get("x") or 0)
    anchor_y = int(anchor_bbox.get("y") or 0)
    anchor_w = int(anchor_bbox.get("w") or 0)
    anchor_h = int(anchor_bbox.get("h") or 0)
    raw_xs = [
        start_x,
        anchor_x,
        anchor_x + anchor_w + gutter,
        anchor_x - w - gutter,
        min_x,
        max_x,
    ]
    raw_ys = [
        anchor_y + anchor_h + gutter,
        anchor_y - h - gutter,
        start_y,
        min_y,
        max_y,
    ]
    if max_x > min_x:
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            raw_xs.append(int(round(min_x + (max_x - min_x) * frac)))
    if max_y > min_y:
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            raw_ys.append(int(round(min_y + (max_y - min_y) * frac)))

    best: tuple[int, float, int, dict[str, int]] | None = None
    seen: set[tuple[int, int]] = set()
    for raw_x in raw_xs:
        for raw_y in raw_ys:
            x = min(max(min_x, int(raw_x)), max_x)
            y = min(max(min_y, int(raw_y)), max_y)
            if (x, y) in seen:
                continue
            seen.add((x, y))
            candidate = {"x": x, "y": y, "w": w, "h": h}
            blockers, weighted_area = _text_overlap_score_for_candidate(
                soup,
                bboxes,
                moving_id=moving_id,
                moving_tag=moving_tag,
                candidate_bbox=candidate,
            )
            distance = abs(x - start_x) + abs(y - start_y)
            ranked = (blockers, weighted_area, distance, candidate)
            if best is None or ranked[:3] < best[:3]:
                best = ranked
    if best is None:
        return None
    best_blockers, best_area, _distance, best_bbox = best
    current_blockers, current_area = current_score
    if best_blockers < current_blockers or best_area < current_area * 0.90:
        return best_bbox
    return None


def _text_overlap_score_for_candidate(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    *,
    moving_id: str,
    moving_tag: Tag,
    candidate_bbox: dict[str, int],
) -> tuple[int, float]:
    candidate_area = max(1, int(candidate_bbox.get("w") or 0) * int(candidate_bbox.get("h") or 0))
    blockers = 0
    weighted_area = 0.0
    moving_core_lane = _core_text_lane(moving_tag)
    for other in soup.find_all(True):
        if not isinstance(other, Tag):
            continue
        other_id = str(other.get("data-block-id") or "").strip()
        if not other_id or other_id == moving_id:
            continue
        if _is_ancestor(moving_tag, other) or _is_ancestor(other, moving_tag):
            continue
        if _infer_block_kind(other) not in {"text", "caption", "quote", "metric"}:
            continue
        if len(other.get_text(" ", strip=True).split()) < 3:
            continue
        other_bbox = bboxes.get(other_id)
        if not isinstance(other_bbox, dict):
            continue
        overlap = _bbox_overlap_area(candidate_bbox, other_bbox)
        if overlap <= 0:
            continue
        other_area = max(1, int(other_bbox.get("w") or 0) * int(other_bbox.get("h") or 0))
        ratio = overlap / max(1, min(candidate_area, other_area))
        other_core_lane = _core_text_lane(other)
        if moving_core_lane or other_core_lane:
            if overlap < 320 and ratio < 0.03:
                continue
        elif overlap < 1200 and ratio < 0.18:
            continue
        if ratio < (0.03 if moving_core_lane or other_core_lane else 0.08):
            continue
        blockers += 1
        weighted_area += float(overlap) * (1.0 + ratio * 3.0)
    return blockers, weighted_area


def _canvas_fill_issues(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
) -> list[dict[str, Any]]:
    cw = int(canvas["w_px"])
    ch = int(canvas["h_px"])
    content_boxes = _content_fill_bboxes(soup, bboxes, canvas)
    if not content_boxes:
        return [{
            "id": "paper_poster_html_no_measurable_content",
            "message": "No measurable source/text content blocks were found.",
        }]

    bottom_px = max(float(bbox["y"] + bbox["h"]) for bbox in content_boxes)
    bottom_ratio = round(bottom_px / float(max(1, ch)), 4)
    lower_quarter_coverage = _band_grid_coverage(
        content_boxes,
        cw=cw,
        ch=ch,
        y0=ch * 0.75,
        y1=ch,
    )
    lower_half_coverage = _band_grid_coverage(
        content_boxes,
        cw=cw,
        ch=ch,
        y0=ch * 0.50,
        y1=ch,
    )
    middle_lower_coverage = _band_grid_coverage(
        content_boxes,
        cw=cw,
        ch=ch,
        y0=ch * 0.42,
        y1=ch * 0.74,
    )
    is_portrait = ch > cw
    min_bottom_ratio = 0.92 if is_portrait else 0.90
    min_lower_quarter = 0.10 if is_portrait else 0.12
    min_lower_half = 0.18 if is_portrait else 0.22
    min_middle_lower = 0.16 if is_portrait else 0.18
    metrics = {
        "content_bottom_ratio": bottom_ratio,
        "content_bottom_px": round(bottom_px, 2),
        "lower_quarter_content_coverage": lower_quarter_coverage,
        "lower_half_content_coverage": lower_half_coverage,
        "middle_lower_content_coverage": middle_lower_coverage,
        "min_content_bottom_ratio": min_bottom_ratio,
        "min_lower_quarter_content_coverage": min_lower_quarter,
        "min_lower_half_content_coverage": min_lower_half,
        "min_middle_lower_content_coverage": min_middle_lower,
    }
    issues: list[dict[str, Any]] = []
    if bottom_ratio < min_bottom_ratio:
        issues.append({
            "id": "paper_poster_html_content_stops_before_bottom",
            "message": "Dense poster content stops too far above the bottom edge.",
            **metrics,
        })
    if lower_quarter_coverage < min_lower_quarter:
        issues.append({
            "id": "paper_poster_html_lower_quarter_sparse",
            "message": "The lower quarter has too little text/figure/table content.",
            **metrics,
        })
    if lower_half_coverage < min_lower_half:
        issues.append({
            "id": "paper_poster_html_lower_half_sparse",
            "message": "The lower half is underfilled compared with dense reference posters.",
            **metrics,
        })
    if middle_lower_coverage < min_middle_lower:
        issues.append({
            "id": "paper_poster_html_middle_lower_sparse",
            "message": "The middle-lower body has a large blank run despite dense reference posters.",
            **metrics,
        })
    if (
        len(issues) == 1
        and issues[0].get("id") == "paper_poster_html_content_stops_before_bottom"
        and bottom_ratio >= min_bottom_ratio - 0.035
        and lower_quarter_coverage >= min_lower_quarter * 2.0
        and lower_half_coverage >= min_lower_half * 2.0
        and middle_lower_coverage >= min_middle_lower * 2.0
    ):
        return [{
            **issues[0],
            "near_threshold_bottom_margin_warn": True,
        }]
    return issues


def _severe_canvas_fill_issues(
    issues: list[dict[str, Any]],
    canvas: dict[str, Any],
) -> list[dict[str, Any]]:
    if not issues:
        return []
    ch = max(1, int(canvas["h_px"]))
    cw = max(1, int(canvas["w_px"]))
    is_portrait = ch > cw
    severe_bottom = 0.84 if is_portrait else 0.82
    severe_lower_quarter = 0.045 if is_portrait else 0.055
    severe_lower_half = 0.115 if is_portrait else 0.13
    severe_middle_lower = 0.085 if is_portrait else 0.10
    severe: list[dict[str, Any]] = []
    for issue in issues:
        bottom_ratio = _safe_float(issue.get("content_bottom_ratio"))
        lower_quarter = _safe_float(issue.get("lower_quarter_content_coverage"))
        lower_half = _safe_float(issue.get("lower_half_content_coverage"))
        middle_lower = _safe_float(issue.get("middle_lower_content_coverage"))
        if (
            bottom_ratio < severe_bottom
            or lower_quarter < severe_lower_quarter
            or lower_half < severe_lower_half
            or middle_lower < severe_middle_lower
        ):
            severe.append({
                **issue,
                "severe_thresholds": {
                    "content_bottom_ratio": severe_bottom,
                    "lower_quarter_content_coverage": severe_lower_quarter,
                    "lower_half_content_coverage": severe_lower_half,
                    "middle_lower_content_coverage": severe_middle_lower,
                },
            })
    return severe


def _content_fill_bboxes(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    canvas: dict[str, Any],
) -> list[dict[str, int]]:
    out: list[dict[str, int]] = []
    cw = int(canvas["w_px"])
    ch = int(canvas["h_px"])
    canvas_area = max(1, cw * ch)
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        block_id = str(tag.get("data-block-id") or "").strip()
        bbox = _bbox_only(bboxes.get(block_id))
        if not block_id or bbox is None:
            continue
        kind = _infer_block_kind(tag)
        if kind in {"group", "shape"}:
            if _is_meaningful_flow_content_panel(tag):
                clipped = _clip_bbox_to_canvas(bbox, cw=cw, ch=ch)
                if clipped and clipped["w"] * clipped["h"] >= 120:
                    out.append(clipped)
            continue
        role_blob = " ".join(
            str(tag.get(key) or "")
            for key in ("data-role", "role", "class", "data-panel-role")
        ).lower()
        area = max(0, bbox["w"]) * max(0, bbox["h"])
        if "logo" in role_blob and bbox["y"] < ch * 0.18 and area < canvas_area * 0.02:
            continue
        if kind in {"text", "caption", "quote", "metric"}:
            words = len(tag.get_text(" ", strip=True).split())
            if words < 2 and kind != "metric":
                continue
        clipped = _clip_bbox_to_canvas(bbox, cw=cw, ch=ch)
        if clipped and clipped["w"] * clipped["h"] >= 120:
            out.append(clipped)
    return out


def _is_meaningful_flow_content_panel(tag: Tag) -> bool:
    if not isinstance(tag, Tag):
        return False
    raw_classes = tag.get("class") or []
    class_tokens = {
        str(token).strip()
        for token in (
            raw_classes if isinstance(raw_classes, list) else str(raw_classes).split()
        )
        if str(token).strip()
    }
    role_blob = " ".join(
        str(tag.get(key) or "")
        for key in ("data-layout-mode", "data-panel-role", "data-slot-id", "class")
    ).lower()
    if (
        "flow-panel" not in class_tokens
        and "poster-header" not in class_tokens
        and "panel-flow" not in role_blob
    ):
        return False
    if "identity_header" in role_blob or str(tag.name).lower() == "header":
        return False
    has_source = bool(tag.find(attrs={"data-layer-id": True}) or tag.find(attrs={"data-source-id": True}))
    words = len(tag.get_text(" ", strip=True).split())
    native_units = len(tag.find_all(["table", "figure", "ul", "ol"]))
    return has_source and (words >= 18 or native_units >= 2)


def _clip_bbox_to_canvas(bbox: dict[str, int], *, cw: int, ch: int) -> dict[str, int] | None:
    x1 = max(0, int(bbox.get("x") or 0))
    y1 = max(0, int(bbox.get("y") or 0))
    x2 = min(cw, int(bbox.get("x") or 0) + int(bbox.get("w") or 0))
    y2 = min(ch, int(bbox.get("y") or 0) + int(bbox.get("h") or 0))
    if x2 <= x1 or y2 <= y1:
        return None
    return {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1}


def _band_grid_coverage(
    bboxes: list[dict[str, int]],
    *,
    cw: int,
    ch: int,
    y0: float,
    y1: float,
) -> float:
    band_top = max(0.0, min(float(ch), y0))
    band_bottom = max(band_top + 1.0, min(float(ch), y1))
    cols = 48
    rows = 12
    marked = 0
    total = cols * rows
    cell_w = float(cw) / cols
    cell_h = (band_bottom - band_top) / rows
    for row in range(rows):
        cy = band_top + row * cell_h
        for col in range(cols):
            cx = col * cell_w
            cell = {"x": cx, "y": cy, "w": cell_w, "h": cell_h}
            cell_area = max(1.0, cell_w * cell_h)
            if any(_bbox_overlap_area(bbox, cell) >= cell_area * 0.03 for bbox in bboxes):
                marked += 1
    return round(marked / float(max(1, total)), 4)


def _is_ancestor(parent: Tag, child: Tag) -> bool:
    node = child.parent
    while isinstance(node, Tag):
        if node is parent:
            return True
        node = node.parent
    return False


def _bbox_overlap_area(left: dict[str, int], right: dict[str, int]) -> float:
    x1 = max(float(left.get("x") or 0), float(right.get("x") or 0))
    y1 = max(float(left.get("y") or 0), float(right.get("y") or 0))
    x2 = min(float(left.get("x") or 0) + float(left.get("w") or 0), float(right.get("x") or 0) + float(right.get("w") or 0))
    y2 = min(float(left.get("y") or 0) + float(left.get("h") or 0), float(right.get("y") or 0) + float(right.get("h") or 0))
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _bbox_plain_area(bbox: dict[str, int]) -> float:
    return max(1.0, float(bbox.get("w") or 0) * float(bbox.get("h") or 0))


def _compiled_geometry_css(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    *,
    fixed_slot_contract: bool = False,
) -> str:
    if not bboxes:
        return ""
    lines = [
        "/* HTML-first compiler: measured auditable block geometry. */",
        ".paper-poster [data-block-id]{box-sizing:border-box;}",
    ]
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        block_id = str(tag.get("data-block-id") or "").strip()
        bbox = bboxes.get(block_id)
        if not block_id or not bbox:
            continue
        kind = _infer_block_kind(tag)
        parent_id = _nearest_parent_block_id(tag)
        if fixed_slot_contract and parent_id:
            continue
        if _should_skip_fixed_lane_child_geometry(tag, kind):
            continue
        parent_bbox = bboxes.get(parent_id) if parent_id else None
        if parent_bbox:
            rel = {
                "x": bbox["x"] - parent_bbox["x"],
                "y": bbox["y"] - parent_bbox["y"],
                "w": bbox["w"],
                "h": bbox["h"],
            }
            selector = f'.paper-poster [data-block-id="{parent_id}"] [data-block-id="{block_id}"]'
        else:
            rel = bbox
            selector = f'.paper-poster [data-block-id="{block_id}"]'
        overflow = "visible" if kind == "group" else "hidden"
        extra_css = _compiled_text_box_css(tag, bbox, kind)
        if kind == "table":
            extra_css += _compiled_table_box_css(tag)
        lines.append(
            f'{selector}{{position:absolute!important;left:{rel["x"]}px!important;top:{rel["y"]}px!important;'
            f'width:{rel["w"]}px!important;height:{rel["h"]}px!important;right:auto!important;bottom:auto!important;'
            f'transform:none!important;translate:none!important;overflow:{overflow}!important;'
            "grid-column:auto!important;grid-row:auto!important;"
            f"align-self:auto!important;justify-self:auto!important;margin:0!important;{extra_css}}}"
        )
        if kind == "table" and str(getattr(tag, "name", "") or "").lower() != "table":
            lines.append(
                f"{selector}>table{{width:100%!important;height:100%!important;max-width:100%!important;"
                "table-layout:fixed!important;border-collapse:collapse!important;}}"
            )
            lines.append(
                f"{selector}>table th,{selector}>table td{{overflow-wrap:anywhere!important;}}"
            )
    return "\n".join(lines)


def _should_skip_fixed_lane_child_geometry(tag: Tag, kind: str) -> bool:
    if kind in {"image", "table", "chart", "embed"}:
        return False
    parent = tag.parent
    while isinstance(parent, Tag):
        if str(parent.get("data-lane") or "").strip():
            return True
        parent = parent.parent
    return False


def _compiled_table_box_css(tag: Tag) -> str:
    pieces = [
        "max-width:100%!important;",
        "table-layout:fixed!important;",
        "border-collapse:collapse!important;",
        "overflow-wrap:anywhere!important;",
    ]
    if str(getattr(tag, "name", "") or "").lower() == "table":
        pieces.append("display:table!important;")
    return "".join(pieces)


def _compiled_text_box_css(tag: Tag, bbox: dict[str, Any], kind: str) -> str:
    if kind not in {"text", "caption", "quote", "metric"}:
        return ""
    pieces = [
        "white-space:normal!important;",
        "overflow-wrap:anywhere!important;",
        "word-break:normal!important;",
        "hyphens:auto;",
    ]
    fit_font = _safe_int(bbox.get("_fit_font_size_px"))
    if fit_font > 0:
        computed = bbox.get("_computed_style") if isinstance(bbox.get("_computed_style"), dict) else {}
        line_height = _safe_float(computed.get("line_height"), 1.16)
        pieces.append(f"font-size:{fit_font}px!important;")
        pieces.append(f"line-height:{max(1.05, line_height):.3g}!important;")
    if _tag_is_title_like(tag):
        pieces.append("text-wrap:balance;")
    return "".join(pieces)


def _nearest_parent_block_id(tag: Tag) -> str:
    parent = tag.parent
    while isinstance(parent, Tag):
        block_id = str(parent.get("data-block-id") or "").strip()
        if block_id:
            return block_id
        parent = parent.parent
    return ""


def _compile_blocks_from_dom(
    soup: BeautifulSoup,
    bboxes: dict[str, dict[str, int]],
    ctx: ToolContext,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        block_id = str(tag.get("data-block-id") or "").strip()
        if not block_id:
            continue
        kind = _infer_block_kind(tag)
        measured_bbox = bboxes.get(block_id)
        bbox = _bbox_only(measured_bbox) or _bbox_from_tag_attrs(tag, _default_canvas(ctx))
        if bbox is None:
            continue
        role = _infer_block_role(tag, kind, ctx)
        source_id = _source_id_for_tag(tag, ctx)
        style = _style_for_tag(tag)
        if isinstance(measured_bbox, dict) and isinstance(measured_bbox.get("_computed_style"), dict):
            style = {**measured_bbox["_computed_style"], **style}
        block: dict[str, Any] = {
            "block_id": block_id,
            "kind": kind,
            "role": role,
            "bbox": bbox,
            "editable": kind not in {"shape"},
            "style": style,
        }
        if isinstance(measured_bbox, dict):
            visible_bbox = _bbox_only(
                measured_bbox.get("_visible_bbox")
                if isinstance(measured_bbox.get("_visible_bbox"), dict)
                else None
            )
            if visible_bbox is not None and visible_bbox != bbox:
                block["provenance"] = {"visible_bbox": visible_bbox}
        parent_id = _nearest_parent_block_id(tag)
        if parent_id:
            block["parent_id"] = parent_id
        panel_role = _infer_panel_role(tag)
        slot_id = _infer_slot_id(tag) or panel_role
        if panel_role:
            block["panel_role"] = panel_role
        if slot_id:
            block["slot_id"] = slot_id
        if kind in {"text", "caption", "metric", "quote"}:
            block["text"] = tag.get_text(" ", strip=True)
        if kind == "table":
            headers, rows = _table_data(tag)
            if headers:
                block["headers"] = headers
            if rows:
                block["rows"] = rows
            block["text"] = tag.get_text(" ", strip=True)
        if kind in {"image", "chart", "embed", "table"}:
            _attach_source_binding(block, tag, source_id, ctx)
        caption = str(tag.get("data-caption") or "").strip()
        if caption:
            block["caption"] = caption
        flow_summary = _flow_panel_descendant_summary(tag)
        if flow_summary:
            block["flow_panel_summary"] = flow_summary
        blocks.append(block)
    return blocks


def _flow_panel_descendant_summary(tag: Tag) -> dict[str, Any]:
    if not _is_meaningful_flow_content_panel(tag):
        return {}
    modes: set[str] = set()
    if tag.find("table") or tag.find(attrs={"data-source-id": re.compile(r"table", re.I)}):
        modes.add("table")
    if (
        tag.find("figure")
        or tag.find("img")
        or tag.find(attrs={"data-layer-id": True})
        or tag.find(attrs={"data-source-id": True})
    ):
        modes.add("visual")
    native_count = 0
    caption_words = 0
    for child in tag.find_all(True):
        name = str(child.name or "").lower()
        class_blob = " ".join(str(token) for token in (child.get("class") or [])).lower()
        role_blob = " ".join(
            str(child.get(key) or "")
            for key in ("data-role", "role", "data-panel-role", "data-slot-id")
        ).lower()
        text = child.get_text(" ", strip=True)
        words = len(text.split())
        if name in {"figcaption", "caption"} or any(
            token in class_blob or token in role_blob
            for token in ("caption", "takeaway", "readout", "note", "interpret")
        ):
            caption_words += words
        if name == "table" or any(
            token in class_blob or token in role_blob
            for token in ("result-band", "metric-row", "pipeline", "stage", "formula", "benchmark")
        ):
            native_count += 1
    word_count = len(tag.get_text(" ", strip=True).split())
    if word_count >= 8:
        modes.add("source_text")
    if caption_words >= 5:
        modes.add("caption_takeaway")
    if native_count:
        modes.add("native")
    if len(modes) < 2 and word_count < 18:
        return {}
    return {
        "modes": sorted(modes),
        "word_count": word_count,
        "caption_word_count": caption_words,
        "native_unit_count": native_count,
    }


def _default_canvas(ctx: ToolContext) -> dict[str, Any]:
    return _canvas_for_html_first({}, ctx)


def _infer_block_kind(tag: Tag) -> str:
    explicit = str(tag.get("data-block-kind") or tag.get("data-kind") or "").strip().lower()
    name = str(tag.name or "").lower()
    if name not in {"img", "table"} and _is_panel_flow_root(tag):
        return "group"
    shape_text_kind = _text_bearing_shape_kind(tag, explicit)
    if shape_text_kind:
        return shape_text_kind
    if explicit in _VALID_BLOCK_KINDS:
        return explicit
    role_blob = " ".join(
        str(tag.get(key) or "")
        for key in ("data-role", "role", "class", "data-panel-role")
    ).lower()
    if name == "img":
        return "image"
    if name == "table":
        return "table"
    if name in {"ul", "ol"}:
        return "group"
    if name == "figcaption":
        return "caption"
    if name == "blockquote":
        return "quote"
    source_blob = " ".join(
        str(tag.get(key) or "")
        for key in ("data-source-id", "data-layer-id", "data-asset-id", "data-visual-id")
    ).lower()
    if source_blob and (
        name in {"figure", "picture"}
        or "ingest_fig_" in source_blob
        or "ingest_table_" in source_blob
        or "ingest_img_" in source_blob
        or any(token in role_blob for token in ("figure", "visual", "image", "chart", "evidence", "table"))
    ):
        return "table" if "ingest_table_" in source_blob or "table" in role_blob else "image"
    if _is_structural_text_container(tag):
        return "group"
    if str(tag.get("data-source-id") or tag.get("data-layer-id") or tag.get("data-asset-id") or "").strip():
        if any(token in role_blob for token in ("figure", "visual", "image", "chart", "evidence", "table")):
            return "table" if "table" in role_blob else "image"
    if name in _TEXT_TAGS:
        if "caption" in role_blob:
            return "caption"
        if "metric" in role_blob or re.fullmatch(r"[\d.,%+/-]+", tag.get_text("", strip=True) or ""):
            return "metric"
        return "text"
    leaf_text_kind = _leaf_text_block_kind(tag, role_blob)
    if leaf_text_kind:
        return leaf_text_kind
    return "group"


def _is_structural_text_container(tag: Tag) -> bool:
    name = str(tag.name or "").lower()
    if name not in {"div", "section", "article", "aside", "header", "footer", "figure"}:
        return False
    if str(tag.get("data-lane") or "").strip():
        return any(isinstance(child, Tag) for child in tag.find_all(True, recursive=False))
    structural_children = {
        "p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
        "table", "figure", "figcaption", "blockquote", "img",
    }
    return sum(
        1
        for child in tag.find_all(True, recursive=False)
        if isinstance(child, Tag) and str(child.name or "").lower() in structural_children
    ) >= 2


def _leaf_text_block_kind(tag: Tag, role_blob: str) -> str:
    name = str(tag.name or "").lower()
    if name not in {"div", "section", "article", "aside"}:
        return ""
    if any(
        isinstance(child, Tag) and str(child.get("data-block-id") or "").strip()
        for child in tag.find_all(True)
    ):
        return ""
    text = tag.get_text(" ", strip=True)
    word_count = len(re.findall(r"[A-Za-z0-9][A-Za-z0-9_+./:%-]*", text or ""))
    if word_count < 2:
        return ""
    if "caption" in role_blob:
        return "caption"
    if any(token in role_blob for token in ("metric", "stat", "score", "value", "formula")):
        return "metric"
    return "text"


def _text_bearing_shape_kind(tag: Tag, explicit_kind: str) -> str:
    if explicit_kind != "shape":
        return ""
    name = str(tag.name or "").lower()
    if name in {"img", "svg", "canvas", "table", "thead", "tbody", "tfoot", "tr"}:
        return ""
    if any(
        isinstance(child, Tag) and str(child.get("data-block-id") or "").strip()
        for child in tag.find_all(True)
    ):
        return ""
    text = tag.get_text(" ", strip=True)
    word_count = len(re.findall(r"[A-Za-z0-9][A-Za-z0-9_+./:%-]*", text or ""))
    if word_count < 2:
        return ""
    role_blob = " ".join(
        str(tag.get(key) or "")
        for key in ("data-role", "role", "class", "data-panel-role", "data-block-id")
    ).lower()
    decorative_tokens = (
        "background", "divider", "rule", "separator", "frame", "border",
        "logo", "icon", "panel-bar", "panel-title", "panel-num", "kicker",
    )
    if word_count <= 6 and any(token in role_blob for token in decorative_tokens):
        return ""
    if "caption" in role_blob or name == "figcaption":
        return "caption"
    if any(token in role_blob for token in ("metric", "stat", "score", "value", "formula")):
        return "metric"
    textual_tokens = (
        "result", "band", "takeaway", "citation", "provenance", "footer",
        "body", "text", "claim", "summary", "analysis", "limitation",
        "future", "bullet", "tag", "badge", "callout", "note", "label",
    )
    if str(tag.get("contenteditable") or "").lower() == "true":
        return "text"
    if name in _TEXT_TAGS or any(token in role_blob for token in textual_tokens):
        return "text"
    if word_count >= 8:
        return "text"
    return ""


def _infer_block_role(tag: Tag, kind: str, ctx: ToolContext | None = None) -> str:
    for key in ("data-role", "role", "data-panel-role"):
        value = str(tag.get(key) or "").strip()
        if value:
            return value
    classes = tag.get("class") or []
    if isinstance(classes, list) and classes:
        role = " ".join(str(item) for item in classes[:4])
        if role:
            return role
    if kind in {"image", "table", "chart", "embed"} and _source_id_for_tag(tag, ctx):
        return "source_visual local_evidence"
    return kind


def _infer_panel_role(tag: Tag) -> str:
    value = str(tag.get("data-panel-role") or "").strip()
    if value:
        return _safe_block_id(value, "panel")
    role = str(tag.get("data-role") or tag.get("role") or "").lower()
    if "panel" in role:
        return _safe_block_id(role.replace("panel", ""), "panel")
    return ""


def _infer_slot_id(tag: Tag) -> str:
    for key in ("data-slot-id", "slot-id", "data-slot"):
        value = str(tag.get(key) or "").strip()
        if value:
            return _safe_block_id(value, "slot")
    return ""


def _native_benchmark_table_policy_error(
    soup: BeautifulSoup,
    ctx: ToolContext | None,
) -> ToolResultRecord | None:
    available_source_table_ids = _available_ingest_table_source_ids(ctx)
    if not available_source_table_ids:
        return None
    bound_source_tables = _bound_source_table_tags(soup, ctx)
    native_tables = [
        table for table in soup.find_all("table")
        if isinstance(table, Tag) and _looks_like_full_native_benchmark_table(table, ctx)
    ]
    if not native_tables:
        return None
    if bound_source_tables:
        duplicate_native_tables = [
            table for table in native_tables
            if _native_table_duplicates_bound_source_crop(table, bound_source_tables, ctx)
        ]
        if not duplicate_native_tables:
            return None
        issues = [
            {
                "table_index": idx,
                "block_id": str(table.get("data-block-id") or ""),
                "row_count": len(table.find_all("tr")),
                "column_count": _native_table_column_count(table),
                "text_sample": table.get_text(" ", strip=True)[:180],
            }
            for idx, table in enumerate(duplicate_native_tables[:4], start=1)
        ]
        return obs_error(
            "propose_paper_poster_html found a large native benchmark/results table duplicating a bound source table crop.",
            category="validation",
            payload={
                "issue_id": "paper_poster_html_native_benchmark_table_duplicates_source",
                "repair_route": "compress_duplicate_native_table_summary",
                "available_source_table_ids": available_source_table_ids[:8],
                "bound_source_table_count": len(bound_source_tables),
                "issues": issues,
                "hint": (
                    "Keep the paper's bound ingest_table_* original PDF table crop as source evidence. "
                    "Delete the duplicate full native reconstruction, or compress it into a compact "
                    "comparison table or concise visual interpretation."
                ),
            },
        )
    issues = [
        {
            "table_index": idx,
            "block_id": str(table.get("data-block-id") or ""),
            "row_count": len(table.find_all("tr")),
            "column_count": _native_table_column_count(table),
            "text_sample": table.get_text(" ", strip=True)[:180],
        }
        for idx, table in enumerate(native_tables[:4], start=1)
    ]
    return obs_error(
        "propose_paper_poster_html found a native benchmark/results table replacing an available source table crop.",
        category="validation",
        payload={
            "issue_id": "paper_poster_html_native_benchmark_table_replaced_source",
            "repair_route": "use_bound_ingest_table_source_asset",
            "available_source_table_ids": available_source_table_ids[:8],
            "issues": issues,
            "hint": (
                "Use the paper's bound ingest_table_* original PDF table crop as the main benchmark/results evidence, "
                "for example a `.figure-flow-unit.source-table.asset-wide` with `data-source-id`. "
                "Keep native tables only for compact summaries such as model framing, training stages, "
                "strategy taxonomy, or short readout rows."
            ),
        },
    )


def _available_ingest_table_source_ids(ctx: ToolContext | None) -> list[str]:
    if ctx is None or not isinstance(ctx.state, dict):
        return []
    found: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        match = re.search(r"(ingest_table_[A-Za-z0-9_-]+)", text)
        if not match:
            return
        source_id = match.group(1)
        if source_id not in found:
            found.append(source_id)

    provenance = ctx.state.get("paper_visual_provenance")
    if isinstance(provenance, dict):
        for asset in provenance.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            for key in ("asset_id", "layer_id", "source_id", "output_file", "path"):
                add(asset.get(key))
    rendered = ctx.state.get("rendered_layers")
    if isinstance(rendered, dict):
        for key, value in rendered.items():
            add(key)
            if isinstance(value, dict):
                for nested_key in ("layer_id", "source_id", "path", "src_path"):
                    add(value.get(nested_key))
    for state_key in ("selected_visuals", "layout_selected_assets", "poster_selected_visuals"):
        value = ctx.state.get(state_key)
        if isinstance(value, dict):
            for key in value:
                add(key)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    for key in ("asset_id", "source_id", "layer_id"):
                        add(item.get(key))
                else:
                    add(item)
    try:
        for path in (ctx.run_dir / "layers").glob("*ingest_table*"):
            add(path.name)
    except OSError:
        pass
    return found


def _bound_source_table_tags(soup: BeautifulSoup, ctx: ToolContext | None) -> list[Tag]:
    out: list[Tag] = []
    seen: set[int] = set()
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        if id(tag) in seen:
            continue
        if _is_bound_source_table_crop_tag(tag, ctx):
            seen.add(id(tag))
            out.append(tag)
    return out


def _native_table_duplicates_bound_source_crop(
    table: Tag,
    bound_source_tables: list[Tag],
    ctx: ToolContext | None,
) -> bool:
    table_source_id = _source_id_for_tag(table, ctx)
    if table_source_id.startswith("ingest_table_"):
        return True
    table_section = _nearest_table_policy_section(table)
    table_text = table.get_text(" ", strip=True)
    for bound in bound_source_tables:
        if table_section is not None and table_section is _nearest_table_policy_section(bound):
            return True
        source_id = _source_id_for_tag(bound, ctx) or _source_id_for_tag_or_descendant(bound, ctx)
        if source_id and _native_table_overlaps_source_table_metadata(table_text, source_id, ctx):
            return True
    return False


def _nearest_table_policy_section(tag: Tag) -> Tag | None:
    parent = tag.parent
    while isinstance(parent, Tag):
        name = str(parent.name or "").lower()
        classes = set(_class_tokens(parent))
        if (
            name in {"section", "article", "aside"}
            or parent.get("data-panel-role")
            or parent.get("data-slot-id")
            or classes.intersection({"poster-section", "panel", "flow-panel", "source-flow-unit", "figure-flow-unit"})
        ):
            return parent
        parent = parent.parent
    return None


def _native_table_overlaps_source_table_metadata(
    table_text: str,
    source_id: str,
    ctx: ToolContext | None,
) -> bool:
    if ctx is None or not isinstance(ctx.state, dict):
        return False
    metadata_parts: list[str] = []
    rendered = ctx.state.get("rendered_layers")
    rec = rendered.get(source_id) if isinstance(rendered, dict) else None
    if isinstance(rec, dict):
        metadata_parts.extend([
            str(rec.get("caption") or ""),
            str(rec.get("caption_short") or ""),
            str(rec.get("title") or ""),
            " ".join(str(item) for item in rec.get("headers") or []),
        ])
    provenance = ctx.state.get("paper_visual_provenance")
    if isinstance(provenance, dict):
        for asset in provenance.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            asset_id = str(asset.get("asset_id") or asset.get("layer_id") or asset.get("source_id") or "")
            if asset_id != source_id:
                continue
            table_meta = asset.get("table_metadata") if isinstance(asset.get("table_metadata"), dict) else {}
            metadata_parts.extend([
                str(asset.get("caption_short") or ""),
                str(asset.get("caption_full") or ""),
                str(table_meta.get("title") or ""),
                " ".join(str(item) for item in table_meta.get("headers") or []),
            ])
    metadata = " ".join(part for part in metadata_parts if part).lower()
    if not metadata:
        return False
    stop = {
        "table", "figure", "results", "result", "benchmark", "score", "metric",
        "model", "method", "source", "paper", "caption", "comparison",
    }
    meta_tokens = {
        token for token in re.findall(r"[a-z][a-z0-9_-]{2,}", metadata)
        if token not in stop
    }
    if not meta_tokens:
        return False
    table_tokens = set(re.findall(r"[a-z][a-z0-9_-]{2,}", table_text.lower()))
    return len(meta_tokens.intersection(table_tokens)) >= 2


def _is_bound_source_table_crop_tag(tag: Tag, ctx: ToolContext | None) -> bool:
    source_id = _source_id_for_tag(tag, ctx) or _source_id_for_tag_or_descendant(tag, ctx)
    if not source_id.startswith("ingest_table_"):
        return False
    name = str(tag.name or "").lower()
    if name == "table":
        return False
    if name == "img":
        return _tag_has_matching_source_crop_ref(tag, source_id, ctx, include_descendants=False)
    if name not in {"figure", "picture", "div", "section", "article", "aside"}:
        return False
    return _tag_has_matching_source_crop_ref(tag, source_id, ctx, include_descendants=True)


def _tag_has_matching_source_crop_ref(
    tag: Tag,
    source_id: str,
    ctx: ToolContext | None,
    *,
    include_descendants: bool,
) -> bool:
    tags: list[Tag] = []
    if str(tag.name or "").lower() in {"img", "object", "embed"}:
        tags.append(tag)
    if include_descendants:
        tags.extend(child for child in tag.find_all(["img", "object", "embed"]) if isinstance(child, Tag))
    for candidate in tags:
        if _tag_or_ancestor_hidden(candidate):
            continue
        refs: list[str] = []
        name = str(candidate.name or "").lower()
        keys = ("src", "data-src", "data-crop-src", "data-image-src")
        if name in {"object", "embed"}:
            keys = ("data", "src", "data-src", "data-crop-src", "data-image-src")
        for key in keys:
            raw = str(candidate.get(key) or "").strip()
            if raw:
                refs.append(raw)
        if name == "img":
            refs.extend(_srcset_urls(str(candidate.get("srcset") or "")))
        if any(_source_crop_ref_matches_source_id(ref, source_id, ctx) for ref in refs):
            return True
    return False


def _source_crop_ref_matches_source_id(ref: str, source_id: str, ctx: ToolContext | None) -> bool:
    raw = str(ref or "").strip()
    if not raw:
        return False
    canonical_id = _canonical_source_id(raw, ctx, allow_plain=False)
    if canonical_id == source_id:
        return True
    if ctx is not None and _source_crop_ref_matches_registered_source_path(raw, source_id, ctx):
        return True
    return False


def _source_crop_ref_matches_registered_source_path(ref: str, source_id: str, ctx: ToolContext) -> bool:
    source_path = _source_path_for_id(source_id, ctx)
    if not source_path:
        return False
    return _asset_ref_matches_local_path(ref, source_path, ctx)


def _looks_like_full_native_benchmark_table(table: Tag, ctx: ToolContext | None) -> bool:
    source_id = _source_id_for_tag(table, ctx)
    source_bound_native = source_id.startswith("ingest_table_")
    blob = " ".join(
        [
            " ".join(_class_tokens(table)),
            str(table.get("data-role") or ""),
            str(table.get("role") or ""),
            str(table.get("data-block-id") or ""),
            table.get_text(" ", strip=True),
        ]
    ).lower()
    if any(token in blob for token in ("training stage", "pre-training", "mid-training", "sft")):
        return False
    benchmark_tokens = (
        "benchmark", "results", "leaderboard", "performance", "evaluation",
        "metric", "score", "understanding", "generation", "mmmu", "mathvista",
        "ocrbench", "docvqa", "mmstar", "geneval", "dpg", "longcat-next",
    )
    row_count = len(table.find_all("tr"))
    col_count = _native_table_column_count(table)
    numeric_count = len(re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?(?:\s*%)?(?![A-Za-z])", blob))
    if not source_bound_native and not any(token in blob for token in benchmark_tokens):
        return False
    if source_bound_native and row_count >= 4 and col_count >= 3 and numeric_count >= 4:
        return True
    if row_count >= 5 and col_count >= 3 and numeric_count >= 4:
        return True
    return row_count >= 6 or col_count >= 7 or (col_count >= 4 and numeric_count >= 10)


def _native_table_column_count(table: Tag) -> int:
    max_cols = 0
    for row in table.find_all("tr"):
        if not isinstance(row, Tag):
            continue
        count = 0
        for cell in row.find_all(["th", "td"], recursive=False):
            try:
                count += max(1, int(cell.get("colspan") or 1))
            except Exception:
                count += 1
        max_cols = max(max_cols, count)
    return max_cols


def _source_id_for_tag(tag: Tag, ctx: ToolContext | None = None) -> str:
    fallback_ids: list[str] = []
    for key in ("data-source-id", "data-layer-id", "data-asset-id", "data-visual-id"):
        source_id = _canonical_source_id(str(tag.get(key) or "").strip(), ctx)
        if source_id:
            if source_id not in fallback_ids:
                fallback_ids.append(source_id)
            if _source_id_exists(source_id, ctx):
                return source_id
    if str(tag.name or "").lower() == "img":
        parent = tag.parent
        while isinstance(parent, Tag):
            for key in ("data-source-id", "data-layer-id", "data-asset-id"):
                source_id = _canonical_source_id(str(parent.get(key) or "").strip(), ctx)
                if source_id:
                    if source_id not in fallback_ids:
                        fallback_ids.append(source_id)
                    if _source_id_exists(source_id, ctx):
                        return source_id
            parent = parent.parent
    if fallback_ids:
        return fallback_ids[0]
    src = str(tag.get("src") or "").strip()
    return _canonical_source_id(src, ctx, allow_plain=False)


def _canonical_source_id(
    value: str,
    ctx: ToolContext | None = None,
    *,
    allow_plain: bool = True,
) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    match = re.fullmatch(r"\{\{\s*(?:layer|asset)\s*:\s*([^{}]+?)\s*\}\}", raw)
    if match:
        return match.group(1).strip()
    match = re.fullmatch(r"(?:layer|asset)\s*:\s*(.+)", raw)
    if match:
        return match.group(1).strip()
    match = re.fullmatch(r"\{\{\s*(ingest_(?:fig|table)_[A-Za-z0-9_-]+)\s*\}\}", raw)
    if match:
        source_id = match.group(1).strip()
        if _source_id_exists(source_id, ctx):
            return source_id
        return ""
    if raw.startswith("{{") and raw.endswith("}}"):
        return ""
    if not allow_plain:
        return ""
    return raw


def _source_id_exists(source_id: str, ctx: ToolContext | None) -> bool:
    key = str(source_id or "").strip()
    if not key or ctx is None or not isinstance(ctx.state, dict):
        return False
    rendered = ctx.state.get("rendered_layers")
    if isinstance(rendered, dict) and isinstance(rendered.get(key), dict):
        return True
    provenance = ctx.state.get("paper_visual_provenance")
    if isinstance(provenance, dict):
        for asset in provenance.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            asset_id = str(
                asset.get("layer_id")
                or asset.get("asset_id")
                or asset.get("source_id")
                or ""
            ).strip()
            if asset_id == key:
                return True
    return False


def _style_for_tag(tag: Tag) -> dict[str, Any]:
    style = str(tag.get("style") or "")
    out: dict[str, Any] = {}
    for css_prop, key in (
        ("font-size", "font_size_px"),
        ("font-weight", "font_weight"),
        ("line-height", "line_height"),
        ("color", "fill"),
        ("text-align", "align"),
        ("background", "background"),
    ):
        match = re.search(rf"(?:^|;)\s*{css_prop}\s*:\s*([^;]+)", style, flags=re.IGNORECASE)
        if match:
            out[key] = match.group(1).strip()
    return out


def _table_data(tag: Tag) -> tuple[list[str], list[list[str]]]:
    headers = [cell.get_text(" ", strip=True) for cell in tag.find_all("th")]
    rows: list[list[str]] = []
    for tr in tag.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["td", "th"])]
        if cells:
            rows.append(cells)
    if headers and rows and rows[0] == headers:
        rows = rows[1:]
    return headers, rows


def _attach_source_binding(block: dict[str, Any], tag: Tag, source_id: str, ctx: ToolContext) -> None:
    if source_id:
        block["source_id"] = source_id
        block["layer_id"] = source_id
        block["source"] = "paper_visual"
    if source_id.startswith("ingest_table_") and _is_bound_source_table_crop_tag(tag, ctx):
        block["dom_bound_source_crop"] = True
        block["table_visual_source"] = "original_pdf_crop"
    src = str(tag.get("src") or "").strip()
    if src and not src.startswith(("http://", "https://", "//", "data:", "javascript:", "file:")):
        block["src_path"] = src
    rendered = ctx.state.get("rendered_layers") if isinstance(ctx.state, dict) else {}
    if source_id and isinstance(rendered, dict) and isinstance(rendered.get(source_id), dict):
        rec = rendered[source_id]
        if rec.get("src_path"):
            block["src_path"] = str(rec.get("src_path"))
        if rec.get("caption") and not block.get("caption"):
            block["caption"] = str(rec.get("caption"))
        if rec.get("image_size") and not block.get("image_size"):
            block["image_size"] = rec.get("image_size")


def _build_design_spec(
    args: dict[str, Any],
    ctx: ToolContext,
    *,
    canvas: dict[str, Any],
    body_html: str,
    css: str,
    blocks: list[dict[str, Any]],
    designer_owned_css: bool = False,
    root_shell: dict[str, Any] | None = None,
) -> dict[str, Any]:
    brief_obj = ctx.state.get("poster_content_brief") if isinstance(ctx.state.get("poster_content_brief"), dict) else {}
    contract = ctx.state.get("poster_plan_contract") if isinstance(ctx.state.get("poster_plan_contract"), dict) else {}
    fallback_color_system = _active_paper_poster_color_system(args, ctx, brief_obj=brief_obj, contract=contract)
    color_system = _authored_selected_color_system(body_html, css, fallback_color_system)
    color_palette = _color_system_allowed_hexes(color_system)
    title = str(args.get("title") or brief_obj.get("title") or contract.get("title") or "Paper poster").strip()
    archetype = str(args.get("archetype") or contract.get("layout_archetype") or "html_first_dense_paper_poster")
    return {
        "brief": str(args.get("brief") or ctx.state.get("run_brief") or title),
        "artifact_type": "poster",
        "visual_profile": str(args.get("visual_profile") or "tech-utility"),
        "canvas": canvas,
        "palette": (
            color_palette
            or (args.get("palette") if isinstance(args.get("palette"), list) else [])
            or ["#FFFFFF", "#21181B", "#C1121F", "#F7DEE1"]
        ),
        "color_system": color_system,
        "typography": args.get("typography") if isinstance(args.get("typography"), dict) else {
            "title_font": "Inter",
            "body_font": "Inter",
        },
        "mood": args.get("mood") if isinstance(args.get("mood"), list) else ["academic", "dense", "source-backed"],
        "composition_notes": (
            "HTML-first paper poster compiled from constrained model-authored "
            "DOM/CSS; editable blocks and bboxes were inferred by the runtime."
        ),
        "layer_graph": [],
        "html_artifact": {
            "title": title,
            "target": "poster",
            "theme": _paper_poster_theme(color_system),
            "frames": [{
                "frame_id": "poster_canvas",
                "kind": "canvas",
                "role": "academic_paper_poster",
                "layout": archetype,
                "render_mode": "authored_html",
                "poster_size": _cvpr_poster_size_metadata(),
                "layout_plan": _layout_plan_from_blocks(archetype, blocks),
                "authored_body_html": body_html,
                "authored_css": css,
                "style": _frame_style_for_designer_owned_css(designer_owned_css, root_shell=root_shell),
                "blocks": blocks,
            }],
        },
    }


def _authored_selected_color_system(
    body_html: str,
    css: str,
    fallback_color_system: dict[str, Any],
) -> dict[str, Any]:
    if academic_color_system_from_palette_id is None:
        return fallback_color_system
    try:
        soup = BeautifulSoup(body_html or "", "html.parser")
    except Exception:
        return fallback_color_system
    palette_tag = soup.select_one(".paper-poster[data-palette-id]")
    if not isinstance(palette_tag, Tag):
        return fallback_color_system
    palette_id = str(palette_tag.get("data-palette-id") or "").strip()
    if not palette_id:
        return fallback_color_system
    try:
        selected = academic_color_system_from_palette_id(
            palette_id,
            selection_reason=f"designer selected {palette_id} palette in authored HTML",
        )
    except Exception:
        selected = {}
    selected = _normalize_color_system(selected)
    if not selected:
        return fallback_color_system
    authored_vars = _authored_poster_css_variables(soup, css)
    expected_vars = selected.get("css_variables") if isinstance(selected.get("css_variables"), dict) else {}
    if not expected_vars:
        return fallback_color_system
    for css_var, expected in expected_vars.items():
        actual = authored_vars.get(css_var)
        if _normalize_color_hex(actual) != _normalize_color_hex(expected):
            return fallback_color_system
    return selected


def _authored_palette_diagnostics(
    body_html: str,
    css: str,
    fallback_color_system: dict[str, Any],
) -> list[dict[str, Any]]:
    if academic_color_system_from_palette_id is None:
        return []
    try:
        soup = BeautifulSoup(body_html or "", "html.parser")
    except Exception:
        return []
    root = soup.select_one(".paper-poster")
    fallback = _normalize_color_system(fallback_color_system)
    diagnostics: list[dict[str, Any]] = []
    palette_id = ""
    selected: dict[str, Any] = fallback
    if isinstance(root, Tag):
        palette_id = str(root.get("data-palette-id") or "").strip()
    if not palette_id:
        diagnostics.append({
            "issue_id": "paper_poster_html_palette_id_missing",
            "diagnostic_only": True,
            "severity": "advisory",
            "palette_id": fallback.get("palette_id"),
            "hint": "Set data-palette-id on .paper-poster so the authored CSS palette is auditable.",
        })
    else:
        try:
            selected = (
                fallback
                if palette_id == str(fallback.get("palette_id") or "")
                else _normalize_color_system(academic_color_system_from_palette_id(
                    palette_id,
                    selection_reason=f"designer selected {palette_id} palette in authored HTML",
                ))
            )
        except Exception:
            selected = {}
        if not selected:
            selected = fallback
            diagnostics.append({
                "issue_id": "paper_poster_html_palette_css_variable_mismatch",
                "diagnostic_only": True,
                "severity": "advisory",
                "palette_id": palette_id,
                "hint": "The authored data-palette-id is not a known academic palette id.",
            })
    expected_vars = selected.get("css_variables") if isinstance(selected.get("css_variables"), dict) else {}
    if expected_vars:
        foundational_values, potential_override_values = (
            _authored_poster_css_variable_channels(soup, css)
        )
        mismatches = []
        for css_var, expected in expected_vars.items():
            actual_values = foundational_values.get(css_var) or []
            override_values = potential_override_values.get(css_var) or []
            normalized_values = [_normalize_color_hex(value) for value in actual_values]
            normalized_overrides = [
                _normalize_color_hex(value)
                for value in override_values
            ]
            expected_normalized = _normalize_color_hex(expected)
            if (
                not normalized_values
                or any(value != expected_normalized for value in normalized_values)
                or any(value != expected_normalized for value in normalized_overrides)
            ):
                mismatches.append({
                    "css_variable": css_var,
                    "expected": expected_normalized,
                    "actual": (
                        normalized_values[-1] or None
                        if normalized_values
                        else None
                    ),
                    "actual_values": [
                        normalized or str(raw).strip()
                        for raw, normalized in zip(actual_values, normalized_values)
                    ],
                    "potential_override_values": [
                        normalized or str(raw).strip()
                        for raw, normalized in zip(
                            override_values,
                            normalized_overrides,
                        )
                    ],
                })
        if palette_id and mismatches:
            diagnostics.append({
                "issue_id": "paper_poster_html_palette_css_variable_mismatch",
                "diagnostic_only": True,
                "severity": "advisory",
                "palette_id": palette_id,
                "mismatches": mismatches[:8],
                "hint": "Define the chosen palette's exact --poster-* CSS variables on .paper-poster.",
            })
    allowed = set(_color_system_allowed_hexes(selected))
    if allowed:
        extras = sorted(
            color
            for color in _authored_palette_colors(soup, css)
            if _css_color_rgb_identity(color) not in allowed
        )
        if extras:
            shell_extra_colors, source_visual_extra_colors = _authored_palette_color_scopes(
                soup,
                css,
                set(extras),
            )
            diagnostics.append({
                "issue_id": "paper_poster_html_palette_extra_authored_hex",
                "diagnostic_only": True,
                "severity": "advisory",
                "palette_id": selected.get("palette_id") or palette_id or fallback.get("palette_id"),
                "extra_hexes": extras[:12],
                "extra_colors": extras[:12],
                "shell_extra_hexes": sorted(shell_extra_colors)[:12],
                "shell_extra_colors": sorted(shell_extra_colors)[:12],
                "source_visual_extra_hexes": sorted(source_visual_extra_colors)[:12],
                "source_visual_extra_colors": sorted(source_visual_extra_colors)[:12],
                "hint": "Use selected palette variables for authored shell colors; source figures/tables keep original colors.",
            })
    return diagnostics


def authored_palette_diagnostics(
    body_html: str,
    css: str,
    required_color_system: dict[str, Any],
    *,
    require_selected: bool = False,
) -> list[dict[str, Any]]:
    diagnostics = _authored_palette_diagnostics(
        body_html,
        css,
        required_color_system,
    )
    if not require_selected:
        return diagnostics
    soup = BeautifulSoup(body_html or "", "html.parser")
    root = soup.select_one(".paper-poster")
    actual_id = (
        str(root.get("data-palette-id") or "").strip()
        if isinstance(root, Tag)
        else ""
    )
    required_id = str(required_color_system.get("palette_id") or "").strip()
    if required_id and actual_id != required_id:
        diagnostics.insert(0, {
            "issue_id": "paper_poster_html_required_palette_mismatch",
            "severity": "error",
            "required_palette_id": required_id,
            "actual_palette_id": actual_id,
            "hint": "Use the user-selected palette id and its exact CSS variables.",
        })
    return diagnostics


def _authored_reference_style_diagnostics(
    body_html: str,
    css: str,
    reference_style_contract: Any,
) -> list[dict[str, Any]]:
    if not isinstance(reference_style_contract, dict) or not reference_style_contract:
        return []
    expected = reference_style_contract.get("required_root_attributes")
    if not isinstance(expected, dict) or not expected:
        return []
    try:
        soup = BeautifulSoup(body_html or "", "html.parser")
    except Exception:
        return []
    root = soup.select_one(".paper-poster")
    if not isinstance(root, Tag):
        return []
    mismatches = []
    for attribute, expected_value in expected.items():
        actual_value = str(root.get(str(attribute)) or "").strip()
        if actual_value != str(expected_value):
            mismatches.append({
                "attribute": str(attribute),
                "expected": str(expected_value),
                "actual": actual_value,
            })
    diagnostics: list[dict[str, Any]] = []
    if mismatches:
        diagnostics.append({
            "issue_id": "paper_poster_html_reference_style_attribute_mismatch",
            "diagnostic_only": True,
            "severity": "advisory",
            "style_reference_id": reference_style_contract.get("style_reference_id"),
            "mismatches": mismatches[:8],
            "hint": "Apply the reference style contract's required data attributes on .paper-poster.",
        })

    tokens = reference_style_contract.get("style_tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    lead_band = tokens.get("lead_band") if isinstance(tokens.get("lead_band"), dict) else {}
    if lead_band.get("present") and not soup.select_one(
        "[data-style-role='lead-band'],[data-reference-role='lead-band'],.reference-lead-band,.lead-band"
    ):
        diagnostics.append({
            "issue_id": "paper_poster_html_reference_lead_band_missing",
            "diagnostic_only": True,
            "severity": "advisory",
            "style_reference_id": reference_style_contract.get("style_reference_id"),
            "hint": "Add the reference-defined full-width target-paper lead band immediately below, but outside, the identity header.",
        })

    typography = reference_style_contract.get("typography_contract")
    typography = typography if isinstance(typography, dict) else {}
    if str(typography.get("family_category") or "") == "sans_serif" and "times new roman" in str(css or "").lower():
        diagnostics.append({
            "issue_id": "paper_poster_html_reference_default_typography_leakage",
            "diagnostic_only": True,
            "severity": "advisory",
            "style_reference_id": reference_style_contract.get("style_reference_id"),
            "hint": "Remove the default Times New Roman skin and use the reference-owned sans-serif typography contract.",
        })

    structure = tokens.get("section_structure") if isinstance(tokens.get("section_structure"), dict) else {}
    css_text = str(css or "")
    if str(structure.get("inter_section_dividers") or "") == "none" and re.search(
        r"\.poster-section[^{}]*\{[^{}]*border-(?:top|bottom)\s*:", css_text, flags=re.I | re.S
    ):
        diagnostics.append({
            "issue_id": "paper_poster_html_reference_section_divider_leakage",
            "diagnostic_only": True,
            "severity": "advisory",
            "style_reference_id": reference_style_contract.get("style_reference_id"),
            "hint": "The reference has no inter-section divider lines; remove default poster-section top/bottom borders while keeping only the reference heading treatment.",
        })
    if str(structure.get("vertical_accent_rules") or "") == "none" and re.search(
        r"(?:lead-key|metric|readout|callout)[^{}]*\{[^{}]*border-left\s*:", css_text, flags=re.I | re.S
    ):
        diagnostics.append({
            "issue_id": "paper_poster_html_reference_vertical_rule_leakage",
            "diagnostic_only": True,
            "severity": "advisory",
            "style_reference_id": reference_style_contract.get("style_reference_id"),
            "hint": "Remove colored side stems and vertical accent rules that are absent from the reference poster.",
        })
    return diagnostics


def _authored_poster_css_variables(soup: BeautifulSoup, css: str) -> dict[str, str]:
    return {
        name: values[-1]
        for name, values in _authored_poster_css_variable_values(soup, css).items()
        if values
    }


def _authored_poster_css_variable_values(
    soup: BeautifulSoup,
    css: str,
) -> dict[str, list[str]]:
    foundational, _ = _authored_poster_css_variable_channels(soup, css)
    return foundational


def _authored_poster_css_variable_channels(
    soup: BeautifulSoup,
    css: str,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    root = soup.select_one(".paper-poster")
    if not isinstance(root, Tag):
        return {}, {}
    foundational: dict[str, list[str]] = {}
    potential_overrides: dict[str, list[str]] = {}
    for css_text, source_channel in _authored_stylesheet_sources(soup, css):
        if not css_text.strip():
            continue
        try:
            transform_stylesheet_declaration_values(css_text, {})
        except ValueError:
            continue
        for selector_text, declaration_text, rule_channel in _iter_css_qualified_rules(
            css_text,
            channel=source_channel,
        ):
            if not _selector_matches_root(soup, root, selector_text):
                continue
            _append_poster_variable_declarations(
                (
                    foundational
                    if rule_channel == "foundational"
                    else potential_overrides
                ),
                _parse_css_declaration_list(declaration_text),
            )
    inline_style = str(root.get("style") or "")
    if inline_style.strip():
        try:
            transform_declaration_list_values(inline_style, {})
        except ValueError:
            pass
        else:
            _append_poster_variable_declarations(
                foundational,
                _parse_css_declaration_list(inline_style),
            )
    return foundational, potential_overrides


def _append_poster_variable_declarations(
    out: dict[str, list[str]],
    declarations: list[tuple[str, str]],
) -> None:
    for raw_name, raw_value in declarations:
        name = raw_name.strip()
        if not re.fullmatch(r"--poster-[a-z-]+", name):
            continue
        value = re.sub(r"\s*!important\s*$", "", raw_value, flags=re.I).strip()
        out.setdefault(name, []).append(value)


def _selector_matches_root(
    soup: BeautifulSoup,
    root: Tag,
    selector_text: str,
) -> bool:
    for selector in _split_css_selector_list(selector_text):
        try:
            if any(candidate is root for candidate in soup.select(selector)):
                return True
        except Exception:
            continue
    return False


def _split_css_selector_list(selector_text: str) -> list[str]:
    return [
        selector_text[start:end].strip()
        for start, end in _split_css_top_level_ranges(selector_text, ",")
        if selector_text[start:end].strip()
    ]


def _iter_css_qualified_rules(
    css: str,
    *,
    channel: str,
    _parent_selector: str | None = None,
) -> list[tuple[str, str, str]]:
    rules: list[tuple[str, str, str]] = []
    index = 0
    while index < len(css):
        index = _skip_css_trivia(css, index)
        if index >= len(css):
            break
        prelude_start = index
        terminator_index, terminator = _find_css_top_level_terminator(
            css,
            index,
            {";", "{"},
        )
        if terminator is None:
            break
        if terminator == ";":
            index = terminator_index + 1
            continue
        block_end = _find_css_matching_brace(css, terminator_index)
        if block_end is None:
            break
        prelude = css[prelude_start:terminator_index].strip()
        block = css[terminator_index + 1:block_end]
        if prelude.startswith("@"):
            at_name_match = re.match(r"@([a-zA-Z-]+)", prelude)
            at_name = at_name_match.group(1).lower() if at_name_match else ""
            condition = (
                prelude[at_name_match.end():].strip()
                if at_name_match
                else ""
            )
            if at_name == "media" and _css_media_is_explicit_not_all(condition):
                index = block_end + 1
                continue
            nested_channel = (
                channel
                if at_name in {"layer", "scope"}
                else "potential"
            )
            nested_parent = _parent_selector
            if at_name == "scope":
                scope_selector = _css_scope_root_selector(condition)
                if scope_selector is not None:
                    nested_parent = _combine_css_nested_selector(
                        _parent_selector,
                        scope_selector,
                    )
            elif at_name.endswith("keyframes"):
                nested_parent = ""
            if _parse_css_declaration_list(block):
                rules.append((nested_parent or "", block, nested_channel))
            rules.extend(_iter_css_qualified_rules(
                block,
                channel=nested_channel,
                _parent_selector=nested_parent,
            ))
        elif prelude:
            selector = _combine_css_nested_selector(_parent_selector, prelude)
            rules.append((selector, block, channel))
            rules.extend(_iter_css_qualified_rules(
                block,
                channel=channel,
                _parent_selector=selector,
            ))
        index = block_end + 1
    return rules


def _combine_css_nested_selector(
    parent_selector: str | None,
    child_selector: str,
) -> str:
    child = str(child_selector or "").strip()
    if parent_selector is None:
        return child
    parent = str(parent_selector or "").strip()
    if not parent or not child:
        return ""
    combined: list[str] = []
    for parent_item in _split_css_selector_list(parent):
        for child_item in _split_css_selector_list(child):
            if "&" in child_item:
                combined.append(child_item.replace("&", parent_item))
            elif re.search(r"(?<![\w-]):scope(?![\w-])", child_item, flags=re.I):
                combined.append(re.sub(
                    r"(?<![\w-]):scope(?![\w-])",
                    lambda _match: parent_item,
                    child_item,
                    flags=re.I,
                ))
            else:
                combined.append(f"{parent_item} {child_item}")
    return ", ".join(combined)


def _css_scope_root_selector(condition: str) -> str | None:
    source = str(condition or "").strip()
    if not source or source.casefold().startswith("to "):
        return None
    if not source.startswith("("):
        return ""
    close_index = _find_css_matching_parenthesis(source, 0)
    if close_index is None:
        return ""
    return source[1:close_index].strip()


def _parse_css_declaration_list(css: str) -> list[tuple[str, str]]:
    declarations: list[tuple[str, str]] = []
    for start, end in _split_css_top_level_ranges(css, ";"):
        source = css[start:end].strip()
        if not source or "{" in source or "}" in source:
            continue
        colon_index, colon = _find_css_top_level_terminator(source, 0, {":"})
        if colon != ":":
            continue
        name = re.sub(r"/\*.*?\*/", "", source[:colon_index], flags=re.S).strip()
        value = re.sub(r"/\*.*?\*/", "", source[colon_index + 1:], flags=re.S).strip()
        if name and value:
            declarations.append((name, value))
    return declarations


def _split_css_top_level_ranges(css: str, delimiter: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start = 0
    index = 0
    closers: list[str] = []
    while index < len(css):
        if css.startswith("/*", index):
            comment_end = css.find("*/", index + 2)
            index = len(css) if comment_end < 0 else comment_end + 2
            continue
        char = css[index]
        if char in {'"', "'"}:
            index = _skip_css_string(css, index)
            continue
        if char in "([{":
            closers.append({"(": ")", "[": "]", "{": "}"}[char])
        elif closers and char == closers[-1]:
            closers.pop()
        elif not closers and char == delimiter:
            ranges.append((start, index))
            start = index + 1
        index += 1
    ranges.append((start, len(css)))
    return ranges


def _find_css_top_level_terminator(
    css: str,
    start: int,
    terminators: set[str],
) -> tuple[int, str | None]:
    index = start
    closers: list[str] = []
    while index < len(css):
        if css.startswith("/*", index):
            comment_end = css.find("*/", index + 2)
            index = len(css) if comment_end < 0 else comment_end + 2
            continue
        char = css[index]
        if char in {'"', "'"}:
            index = _skip_css_string(css, index)
            continue
        if not closers and char in terminators:
            return index, char
        if char in "([":
            closers.append({"(": ")", "[": "]"}[char])
        elif closers and char == closers[-1]:
            closers.pop()
        index += 1
    return len(css), None


def _find_css_matching_brace(css: str, opening_index: int) -> int | None:
    depth = 1
    index = opening_index + 1
    while index < len(css):
        if css.startswith("/*", index):
            comment_end = css.find("*/", index + 2)
            index = len(css) if comment_end < 0 else comment_end + 2
            continue
        char = css[index]
        if char in {'"', "'"}:
            index = _skip_css_string(css, index)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _skip_css_trivia(css: str, index: int) -> int:
    while index < len(css):
        if css[index].isspace():
            index += 1
        elif css.startswith("/*", index):
            comment_end = css.find("*/", index + 2)
            index = len(css) if comment_end < 0 else comment_end + 2
        else:
            break
    return index


def _skip_css_string(css: str, index: int) -> int:
    quote = css[index]
    index += 1
    while index < len(css):
        if css[index] == "\\":
            index += 2
            continue
        if css[index] == quote:
            return index + 1
        index += 1
    return index


def _authored_palette_colors(soup: BeautifulSoup, css: str) -> set[str]:
    out: set[str] = set()
    for css_text, _ in _authored_stylesheet_sources(soup, css):
        for _, declaration_text, _ in _iter_css_qualified_rules(
            css_text,
            channel="foundational",
        ):
            out.update(_css_declaration_colors(declaration_text))
    for tag in soup.find_all(True):
        out.update(_css_declaration_colors(str(tag.get("style") or "")))
        out.update(_svg_presentation_colors(tag))
    return out


def _authored_palette_color_scopes(
    soup: BeautifulSoup,
    css: str,
    extra_colors: set[str],
) -> tuple[set[str], set[str]]:
    source_visual_colors: set[str] = set()
    shell_colors: set[str] = set()
    classified_colors: set[str] = set()
    for css_text, _ in _authored_stylesheet_sources(soup, css):
        for selector_text, declaration_text, _ in _iter_css_qualified_rules(
            css_text,
            channel="foundational",
        ):
            colors = _css_declaration_colors(declaration_text) & extra_colors
            if not colors:
                continue
            classified_colors.update(colors)
            if _css_rule_is_source_visual_scoped(soup, selector_text):
                source_visual_colors.update(colors)
            else:
                shell_colors.update(colors)
    for tag in soup.find_all(True):
        colors = (
            _css_declaration_colors(str(tag.get("style") or ""))
            | _svg_presentation_colors(tag)
        ) & extra_colors
        if not colors:
            continue
        classified_colors.update(colors)
        if _tag_is_in_source_visual(tag):
            source_visual_colors.update(colors)
        else:
            shell_colors.update(colors)
    shell_colors.update(extra_colors - classified_colors)
    return shell_colors, source_visual_colors - shell_colors


def _css_declaration_colors(declaration_text: str) -> set[str]:
    out: set[str] = set()
    declarations = _parse_css_declaration_list(declaration_text)
    registered_color_initial = any(
        str(property_name or "").strip().casefold() == "syntax"
        and re.search(r"<\s*color\s*>", str(value or ""), flags=re.I)
        for property_name, value in declarations
    )
    for property_name, value in declarations:
        if (
            _css_property_may_contain_color(property_name)
            or (
                registered_color_initial
                and str(property_name or "").strip().casefold() == "initial-value"
            )
        ):
            out.update(_css_value_colors(value))
    return out


def _css_property_may_contain_color(property_name: str) -> bool:
    name = str(property_name or "").strip().casefold()
    if name.startswith("--") or "color" in name:
        return True
    normalized_name = re.sub(r"^-(?:webkit|moz|ms|o)-", "", name)
    property_prefixes = (
        "background",
        "border",
        "box-shadow",
        "caret",
        "column-rule",
        "fill",
        "filter",
        "content",
        "list-style",
        "mask",
        "outline",
        "scrollbar",
        "shape-outside",
        "stroke",
        "text-decoration",
        "text-emphasis",
        "text-shadow",
        "text-stroke",
    )
    return (
        name.startswith("-webkit-box-reflect")
        or name.startswith(property_prefixes)
        or normalized_name.startswith(property_prefixes)
    )


def _svg_presentation_colors(tag: Tag) -> set[str]:
    out: set[str] = set()
    for attribute in _SVG_PRESENTATION_COLOR_ATTRIBUTES:
        raw = tag.get(attribute)
        if raw is not None:
            out.update(_css_value_colors(str(raw)))
    return out


def _css_value_colors(value: str) -> set[str]:
    text = str(value or "")
    out: set[str] = set()
    index = 0
    while index < len(text):
        if text.startswith("/*", index):
            comment_end = text.find("*/", index + 2)
            index = len(text) if comment_end < 0 else comment_end + 2
            continue
        char = text[index]
        if char in {'"', "'"}:
            index = _skip_css_string(text, index)
            continue
        if char == "#":
            match = re.match(
                r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})(?![0-9a-fA-F])",
                text[index:],
            )
            if match:
                normalized = _normalize_css_hex_color(match.group(0))
                if normalized:
                    out.add(normalized)
                index += len(match.group(0))
                continue
        name_match = re.match(r"[-_a-zA-Z][-_a-zA-Z0-9]*", text[index:])
        if not name_match:
            index += 1
            continue
        name = name_match.group(0)
        index += len(name)
        if index < len(text) and text[index] == "(":
            close_index = _find_css_matching_parenthesis(text, index)
            if close_index is None:
                break
            arguments = text[index + 1:close_index]
            if name.casefold() != "url":
                normalized = _normalize_css_color_function(name, arguments)
                if normalized:
                    out.add(normalized)
                elif name.casefold() in _CSS_ABSOLUTE_COLOR_FUNCTIONS:
                    out.add(_unresolved_css_color_function_token(name, arguments))
                else:
                    out.update(_css_value_colors(arguments))
            index = close_index + 1
            continue
        normalized = _normalize_css_named_color(name)
        if normalized:
            out.add(normalized)
    return out


def _find_css_matching_parenthesis(text: str, opening_index: int) -> int | None:
    depth = 1
    index = opening_index + 1
    while index < len(text):
        if text.startswith("/*", index):
            comment_end = text.find("*/", index + 2)
            index = len(text) if comment_end < 0 else comment_end + 2
            continue
        if text[index] in {'"', "'"}:
            index = _skip_css_string(text, index)
            continue
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _normalize_css_hex_color(value: str) -> str:
    token = str(value or "").strip().lstrip("#")
    if len(token) in {3, 4}:
        token = "".join(character * 2 for character in token)
    if len(token) not in {6, 8} or not re.fullmatch(r"[0-9a-fA-F]+", token):
        return ""
    red = int(token[0:2], 16)
    green = int(token[2:4], 16)
    blue = int(token[4:6], 16)
    alpha = int(token[6:8], 16) if len(token) == 8 else 255
    return _normalized_rgba_color(red, green, blue, alpha)


def _normalize_css_named_color(value: str) -> str:
    token = str(value or "").strip().casefold()
    if not token or token in _CSS_NON_COLOR_KEYWORDS:
        return ""
    try:
        red, green, blue, alpha = ImageColor.getcolor(token, "RGBA")
    except ValueError:
        return ""
    return _normalized_rgba_color(red, green, blue, alpha)


def _normalize_css_color_function(name: str, arguments: str) -> str:
    function_name = str(name or "").casefold()
    if function_name not in {"rgb", "rgba", "hsl", "hsla"}:
        return ""
    channels, alpha_token = _css_color_function_channels(arguments)
    if len(channels) != 3:
        return ""
    alpha = _parse_css_alpha_channel(alpha_token) if alpha_token is not None else 255
    if alpha is None:
        return ""
    if function_name in {"rgb", "rgba"}:
        rgb = [_parse_css_rgb_channel(channel) for channel in channels]
        if any(channel is None for channel in rgb):
            return ""
        return _normalized_rgba_color(int(rgb[0]), int(rgb[1]), int(rgb[2]), alpha)
    hue = _parse_css_hue(channels[0])
    saturation = _parse_css_percentage(channels[1])
    lightness = _parse_css_percentage(channels[2])
    if hue is None or saturation is None or lightness is None:
        return ""
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return _normalized_rgba_color(
        _round_css_channel(red * 255),
        _round_css_channel(green * 255),
        _round_css_channel(blue * 255),
        alpha,
    )


def _unresolved_css_color_function_token(name: str, arguments: str) -> str:
    function_name = str(name or "").strip().casefold()
    normalized_arguments = re.sub(
        r"\s+",
        " ",
        re.sub(r"/\*.*?\*/", " ", str(arguments or ""), flags=re.S),
    ).strip()
    return f"css-color:{function_name}({normalized_arguments})"


def _css_color_function_channels(arguments: str) -> tuple[list[str], str | None]:
    source = re.sub(r"/\*.*?\*/", " ", str(arguments or ""), flags=re.S).strip()
    if "," in source:
        parts = [
            source[start:end].strip()
            for start, end in _split_css_top_level_ranges(source, ",")
        ]
        if len(parts) == 4:
            return parts[:3], parts[3]
        return parts, None
    slash_ranges = _split_css_top_level_ranges(source, "/")
    if len(slash_ranges) > 2:
        return [], None
    channel_source = source[slash_ranges[0][0]:slash_ranges[0][1]].strip()
    channels = channel_source.split()
    alpha = (
        source[slash_ranges[1][0]:slash_ranges[1][1]].strip()
        if len(slash_ranges) == 2
        else None
    )
    return channels, alpha


def _parse_css_rgb_channel(value: str) -> int | None:
    token = str(value or "").strip()
    try:
        if token.endswith("%"):
            number = float(token[:-1]) * 255 / 100
        else:
            number = float(token)
    except ValueError:
        return None
    return _round_css_channel(max(0.0, min(255.0, number)))


def _parse_css_alpha_channel(value: str | None) -> int | None:
    token = str(value or "").strip()
    try:
        if token.endswith("%"):
            number = float(token[:-1]) / 100
        else:
            number = float(token)
    except ValueError:
        return None
    return _round_css_channel(max(0.0, min(1.0, number)) * 255)


def _parse_css_hue(value: str) -> float | None:
    token = str(value or "").strip().casefold()
    units = (
        ("turn", 360.0),
        ("grad", 0.9),
        ("rad", 180.0 / math.pi),
        ("deg", 1.0),
    )
    multiplier = 1.0
    for suffix, factor in units:
        if token.endswith(suffix):
            token = token[:-len(suffix)].strip()
            multiplier = factor
            break
    try:
        degrees = float(token) * multiplier
    except ValueError:
        return None
    return (degrees % 360.0) / 360.0


def _parse_css_percentage(value: str) -> float | None:
    token = str(value or "").strip()
    if not token.endswith("%"):
        return None
    try:
        number = float(token[:-1]) / 100
    except ValueError:
        return None
    return max(0.0, min(1.0, number))


def _round_css_channel(value: float) -> int:
    return int(math.floor(float(value) + 0.5))


def _normalized_rgba_color(red: int, green: int, blue: int, alpha: int) -> str:
    if alpha <= 0:
        return ""
    rgb = f"#{red:02X}{green:02X}{blue:02X}"
    return rgb if alpha >= 255 else f"{rgb}{alpha:02X}"


def _css_color_rgb_identity(color: str) -> str:
    token = str(color or "").strip().upper()
    if re.fullmatch(r"#[0-9A-F]{8}", token):
        return token[:7]
    return token


def _css_rule_is_source_visual_scoped(
    soup: BeautifulSoup,
    selector_text: str,
) -> bool:
    matched_any = False
    for selector in _split_css_selector_list(selector_text):
        selector = _static_css_selector_for_provenance(selector)
        if not selector:
            return False
        try:
            matches = list(soup.select(selector))
        except Exception:
            return False
        if not matches:
            return False
        matched_any = True
        if any(not _tag_is_in_source_visual(tag) for tag in matches):
            return False
    return matched_any


def _static_css_selector_for_provenance(selector: str) -> str:
    reduced, safe, _changed = _reduce_css_selector_for_provenance(selector)
    if not safe:
        return ""
    return re.sub(r"\s+", " ", reduced).strip()


def _reduce_css_selector_for_provenance(selector: str) -> tuple[str, bool, bool]:
    text = str(selector or "")
    out: list[str] = []
    index = 0
    bracket_depth = 0
    changed = False
    while index < len(text):
        if text.startswith("/*", index):
            comment_end = text.find("*/", index + 2)
            if comment_end < 0:
                return "", False, changed
            index = comment_end + 2
            continue
        char = text[index]
        if char in {'"', "'"}:
            string_end = _skip_css_string(text, index)
            if string_end > len(text) or text[string_end - 1:string_end] != char:
                return "", False, changed
            out.append(text[index:string_end])
            index = string_end
            continue
        if char == "[":
            bracket_depth += 1
        elif char == "]":
            if not bracket_depth:
                return "", False, changed
            bracket_depth -= 1
        if char != ":" or bracket_depth:
            out.append(char)
            index += 1
            continue
        pseudo_element = text.startswith("::", index)
        name_start = index + (2 if pseudo_element else 1)
        name_match = re.match(r"[-_a-zA-Z][-_a-zA-Z0-9]*", text[name_start:])
        if not name_match:
            out.append(char)
            index += 1
            continue
        pseudo_name = name_match.group(0).casefold()
        name_end = name_start + len(name_match.group(0))
        if (
            not pseudo_element
            and pseudo_name in {"is", "where", "not", "has"}
            and name_end < len(text)
            and text[name_end] == "("
        ):
            close_index = _find_css_matching_parenthesis(text, name_end)
            if close_index is None:
                return "", False, changed
            arguments = text[name_end + 1:close_index]
            reduced_function, safe, function_changed = (
                _reduce_css_functional_pseudo_for_provenance(
                    pseudo_name,
                    arguments,
                )
            )
            if not safe:
                return "", False, changed
            out.append(reduced_function)
            changed = changed or function_changed
            index = close_index + 1
            continue
        removable = (
            pseudo_element
            or pseudo_name in _CSS_DYNAMIC_PSEUDO_CLASSES
            or pseudo_name in _CSS_LEGACY_PSEUDO_ELEMENTS
        )
        if removable:
            changed = True
            index = name_end
            if index < len(text) and text[index] == "(":
                close_index = _find_css_matching_parenthesis(text, index)
                if close_index is None:
                    return "", False, changed
                index = close_index + 1
            continue
        if name_end < len(text) and text[name_end] == "(":
            close_index = _find_css_matching_parenthesis(text, name_end)
            if close_index is None:
                return "", False, changed
            out.append(text[index:close_index + 1])
            index = close_index + 1
            continue
        out.append(text[index:name_end])
        index = name_end
    if bracket_depth:
        return "", False, changed
    return "".join(out), True, changed


def _reduce_css_functional_pseudo_for_provenance(
    pseudo_name: str,
    arguments: str,
) -> tuple[str, bool, bool]:
    argument_ranges = _split_css_top_level_ranges(arguments, ",")
    reduced_arguments: list[str] = []
    changed = False
    for start, end in argument_ranges:
        argument = arguments[start:end].strip()
        if not argument:
            return "", False, changed
        reduced, safe, argument_changed = _reduce_css_selector_for_provenance(
            argument
        )
        if not safe:
            return "", False, changed
        reduced_arguments.append(reduced.strip())
        changed = changed or argument_changed
    if not reduced_arguments:
        return "", False, changed
    if pseudo_name == "not" and changed:
        return "", True, True
    if any(not argument for argument in reduced_arguments):
        return "", True, True
    reduced = f":{pseudo_name}({', '.join(reduced_arguments)})"
    return reduced, True, changed


def _tag_is_in_source_visual(tag: Tag) -> bool:
    current: Tag | None = tag
    while isinstance(current, Tag):
        source_id = str(current.get("data-source-id") or "").strip()
        block_kind = str(current.get("data-block-kind") or "").strip().casefold()
        if source_id and (
            current.name in {"img", "picture", "figure", "svg", "canvas", "table"}
            or block_kind in {"image", "chart", "embed", "table"}
        ):
            return True
        current = current.parent if isinstance(current.parent, Tag) else None
    return False


def _authored_stylesheet_sources(
    soup: BeautifulSoup,
    css: str,
) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = [(str(css or ""), "foundational")]
    for style_tag in soup.find_all("style"):
        media = str(style_tag.get("media") or "").strip()
        if media and _css_media_is_explicit_not_all(media):
            continue
        candidates.append((
            style_tag.get_text("\n", strip=False),
            (
                "foundational"
                if not media or _css_media_is_unconditional_all(media)
                else "potential"
            ),
        ))

    sources: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for candidate, channel in candidates:
        key = (candidate, channel)
        if not candidate.strip() or key in seen:
            continue
        seen.add(key)
        sources.append(key)
    return sources


def _css_media_is_explicit_not_all(media: str) -> bool:
    return _normalized_css_media_queries(media) == ["not all"]


def _css_media_is_unconditional_all(media: str) -> bool:
    return _normalized_css_media_queries(media) == ["all"]


def _normalized_css_media_queries(media: str) -> list[str]:
    stripped = re.sub(r"/\*.*?\*/", " ", str(media or ""), flags=re.S)
    return [
        re.sub(r"\s+", " ", stripped[start:end]).strip().casefold()
        for start, end in _split_css_top_level_ranges(stripped, ",")
        if stripped[start:end].strip()
    ]


def _normalize_color_hex(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text if re.fullmatch(r"#[0-9A-F]{6}", text) else ""


def _active_paper_poster_color_system(
    args: dict[str, Any],
    ctx: ToolContext,
    *,
    brief_obj: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    raw_brief = str(
        ctx.state.get("raw_user_brief")
        or args.get("brief")
        or ctx.state.get("run_brief")
        or ""
    )
    for source in (
        ctx.state.get("poster_content_brief"),
        ctx.state.get("poster_plan_contract"),
    ):
        if not isinstance(source, dict):
            continue
        required = _normalize_color_system(source.get("required_color_system"))
        if required:
            return required
    reference = ctx.state.get("reference_style_contract")
    if isinstance(reference, dict):
        reference_color = _normalize_color_system(reference.get("color_system"))
        if reference_color:
            return reference_color
    if active_academic_color_system is not None:
        try:
            active = active_academic_color_system(
                brief_obj,
                contract,
                args,
                raw_brief=raw_brief,
                manifest=brief_obj,
            )
        except Exception:
            active = {}
        normalized = _normalize_color_system(active)
        if normalized:
            return normalized
    if select_academic_color_system is None:
        return {}
    try:
        selected = select_academic_color_system(
            raw_brief=raw_brief,
            manifest=brief_obj,
        )
    except Exception:
        return {}
    return _normalize_color_system(selected)


def _paper_poster_theme(color_system: dict[str, Any]) -> dict[str, Any]:
    if not color_system:
        return {}
    theme: dict[str, Any] = {"color_system": color_system}
    palette_id = color_system.get("palette_id")
    if palette_id:
        theme["palette_id"] = palette_id
    palette = _color_system_allowed_hexes(color_system)
    if palette:
        theme["palette"] = palette
    roles = color_system.get("roles") if isinstance(color_system.get("roles"), dict) else {}
    if roles.get("background"):
        theme["background"] = roles["background"]
    if roles.get("accent"):
        theme["accent"] = roles["accent"]
    return theme


def _normalize_color_system(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized = dict(value)
    allowed_hexes = _color_system_allowed_hexes(normalized)
    if not allowed_hexes:
        return {}
    normalized["allowed_hexes"] = allowed_hexes
    return normalized


def _color_system_allowed_hexes(color_system: dict[str, Any]) -> list[str]:
    if not isinstance(color_system, dict):
        return []
    values = color_system.get("allowed_hexes") if isinstance(color_system.get("allowed_hexes"), list) else []
    if not values:
        css_variables = color_system.get("css_variables") if isinstance(color_system.get("css_variables"), dict) else {}
        values = list(css_variables.values())
    if not values:
        roles = color_system.get("roles") if isinstance(color_system.get("roles"), dict) else {}
        values = list(roles.values())
    out: list[str] = []
    for value in values:
        text = str(value or "").strip().upper()
        if re.fullmatch(r"#[0-9A-F]{6}", text) and text not in out:
            out.append(text)
    return out


def _frame_style_for_designer_owned_css(designer_owned_css: bool, *, root_shell: dict[str, Any] | None = None) -> dict[str, Any]:
    style: dict[str, Any] = {}
    if not designer_owned_css:
        if root_shell:
            style["root_shell"] = root_shell
        return style
    style.update({
        "layout_mode": "designer_owned_css",
        "compiler_mode": "designer_owned_css",
        "designer_owned_css": True,
    })
    if root_shell:
        style["root_shell"] = root_shell
    return style


def _layout_plan_from_blocks(archetype: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    slots = []
    for block in _main_layout_slot_blocks(blocks):
        bbox = block.get("bbox") if isinstance(block.get("bbox"), dict) else None
        if not bbox:
            continue
        visual_ids = _visual_ids_inside_slot(block, blocks)
        text_words = _text_words_inside_slot(block, blocks)
        panel_job = _panel_job_for_block(block, visual_ids)
        slots.append({
            "slot_id": block.get("slot_id") or block.get("block_id"),
            "role": block.get("role") or block.get("panel_role") or "panel",
            "bbox": bbox,
            "required": True,
            "content_policy": "dense source-backed text plus adjacent source visual/table when available",
            "panel_job": panel_job,
            "text_budget": _text_budget_label(text_words),
            "visual_ids": visual_ids,
            "space_fill_policy": _space_fill_policy_for_block(block, visual_ids),
        })
    return {
        "archetype": archetype,
        "margin_px": 48,
        "gutter_px": 24,
        "slots": slots[:12],
        "main_slot_count": len(slots),
        "notes": "Compiled from HTML-first DOM main panels; title/header/footer strips are excluded from the main-slot count.",
    }


def _main_layout_slot_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {
        str(block.get("block_id") or ""): block
        for block in blocks
        if str(block.get("block_id") or "").strip()
    }
    candidates = [
        block for block in blocks
        if _is_layout_slot_block(block) and not _is_meta_layout_slot(block)
    ]
    candidates = [
        block for block in candidates
        if not _has_layout_slot_ancestor(block, by_id)
    ]
    candidates.sort(key=lambda item: (
        _safe_float((item.get("bbox") or {}).get("y")),
        _safe_float((item.get("bbox") or {}).get("x")),
    ))
    return candidates


def _has_layout_slot_ancestor(block: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> bool:
    parent_id = str(block.get("parent_id") or "").strip()
    seen: set[str] = set()
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        parent = by_id.get(parent_id)
        if not isinstance(parent, dict):
            return False
        if _is_layout_slot_block(parent) and not _is_meta_layout_slot(parent):
            return True
        parent_id = str(parent.get("parent_id") or "").strip()
    return False


def _is_layout_slot_block(block: dict[str, Any]) -> bool:
    if block.get("kind") != "group":
        return False
    role_blob = " ".join(
        str(block.get(key) or "")
        for key in ("role", "panel_role", "slot_id")
    ).lower()
    bbox = block.get("bbox") if isinstance(block.get("bbox"), dict) else {}
    area = int(bbox.get("w") or 0) * int(bbox.get("h") or 0)
    if area < 24_000:
        return False
    if _is_decorative_layout_shell(block):
        return False
    if _is_internal_layout_lane(block):
        return False
    if block.get("panel_role") or block.get("slot_id"):
        return True
    return any(
        token in role_blob
        for token in (
            "panel", "header", "footer", "title", "method", "evidence",
            "result", "benchmark", "analysis", "limit", "takeaway",
            "provenance", "pipeline", "model", "visual",
        )
    )


def _is_decorative_layout_shell(block: dict[str, Any]) -> bool:
    role_blob = " ".join(
        str(block.get(key) or "")
        for key in ("block_id", "role", "panel_role", "slot_id")
    ).lower()
    return any(
        token in role_blob
        for token in (
            "background", "backdrop", "canvas-bg", "canvas_bg", "grid-bg",
            "grid_bg", "poster-bg", "poster_bg", "paper-poster", "root",
            "scaffold", "decor", "texture", "watermark",
        )
    )


def _is_internal_layout_lane(block: dict[str, Any]) -> bool:
    role_blob = " ".join(
        str(block.get(key) or "")
        for key in ("block_id", "role", "panel_role", "slot_id")
    ).lower()
    return any(
        token in role_blob
        for token in (
            "panel-body", "body lane", "content lane", "lane ", " lane",
            "bullets", "bullet-list", "finding-list", "result-band",
            "metric-row", "metric-grid", "chips", "caption", "figure-box",
            "image-wrap", "table-wrap", "mini-card", "card-grid",
            "flow-row", "flow-box", "support-figs", "analysis-grid",
        )
    )


def _is_meta_layout_slot(block: dict[str, Any]) -> bool:
    role_blob = " ".join(
        str(block.get(key) or "")
        for key in ("block_id", "role", "panel_role", "slot_id")
    ).lower()
    return any(
        token in role_blob
        for token in (
            "title", "header", "footer", "citation", "contact", "identity",
            "meta", "provenance", "watermark", "logo", "abstract-strip",
        )
    )


def _visual_ids_inside_slot(slot: dict[str, Any], blocks: list[dict[str, Any]]) -> list[str]:
    slot_bbox = slot.get("bbox") if isinstance(slot.get("bbox"), dict) else None
    if not slot_bbox:
        return []
    ids: list[str] = []
    for block in blocks:
        if block is slot or block.get("kind") not in {"image", "chart", "embed", "table"}:
            continue
        source_id = str(block.get("source_id") or block.get("layer_id") or "").strip()
        bbox = block.get("bbox") if isinstance(block.get("bbox"), dict) else None
        if source_id and bbox and _bbox_center_inside(bbox, slot_bbox) and source_id not in ids:
            ids.append(source_id)
    return ids


def _text_words_inside_slot(slot: dict[str, Any], blocks: list[dict[str, Any]]) -> int:
    slot_bbox = slot.get("bbox") if isinstance(slot.get("bbox"), dict) else None
    if not slot_bbox:
        return 0
    words = 0
    for block in blocks:
        if block is slot or block.get("kind") not in {"text", "caption", "quote", "metric"}:
            continue
        bbox = block.get("bbox") if isinstance(block.get("bbox"), dict) else None
        if bbox and _bbox_center_inside(bbox, slot_bbox):
            words += len(str(block.get("text") or "").split())
    return words


def _bbox_center_inside(inner: dict[str, Any], outer: dict[str, Any]) -> bool:
    cx = float(inner.get("x") or 0) + float(inner.get("w") or 0) / 2.0
    cy = float(inner.get("y") or 0) + float(inner.get("h") or 0) / 2.0
    ox = float(outer.get("x") or 0)
    oy = float(outer.get("y") or 0)
    ow = float(outer.get("w") or 0)
    oh = float(outer.get("h") or 0)
    return ox <= cx <= ox + ow and oy <= cy <= oy + oh


def _panel_job_for_block(block: dict[str, Any], visual_ids: list[str]) -> str:
    role_blob = " ".join(
        str(block.get(key) or "")
        for key in ("role", "panel_role", "slot_id", "block_id")
    ).lower()
    if any(token in role_blob for token in ("title", "header", "identity", "meta")):
        return "title_meta"
    if any(token in role_blob for token in ("method", "pipeline", "architecture", "model")):
        return "method"
    if any(token in role_blob for token in ("result", "benchmark", "table", "evidence", "visual")):
        return "main_evidence"
    if any(token in role_blob for token in ("analysis", "ablation", "qualitative", "interpret")):
        return "supporting_analysis"
    if any(token in role_blob for token in ("limit", "future")):
        return "limitations_future"
    if any(token in role_blob for token in ("takeaway", "conclusion", "implication")):
        return "synthesis_takeaway"
    if any(token in role_blob for token in ("footer", "provenance", "citation", "contact")):
        return "metadata_citation_line"
    return "mixed_text_evidence" if visual_ids else "dense_text_synthesis"


def _text_budget_label(word_count: int) -> str:
    if word_count <= 0:
        return "visual/native unit with compact caption"
    if word_count <= 35:
        return "compact 10-35 words"
    if word_count <= 80:
        return "dense 35-80 words"
    return "split or compress; current slot is over 80 words"


def _space_fill_policy_for_block(block: dict[str, Any], visual_ids: list[str]) -> str:
    if visual_ids:
        return "source visual plus nearby editable caption, claim, and interpretation bullets"
    panel_job = _panel_job_for_block(block, visual_ids)
    if panel_job in {"title_meta", "metadata_citation_line"}:
        return "thin compact band; do not steal evidence area"
    return "fill spare area with source-backed bullets, native cards, formulas, or table rows"


def _contract_main_panel_count_range(ctx: ToolContext) -> tuple[int, int, int]:
    contract = ctx.state.get("poster_plan_contract") if isinstance(ctx.state, dict) else None
    if isinstance(contract, dict):
        targets = contract.get("layout_storyboard_targets") if isinstance(contract.get("layout_storyboard_targets"), dict) else {}
        if not targets and isinstance(contract.get("density_targets"), dict):
            targets = contract.get("density_targets") or {}
        count_contract = (
            targets.get("main_panel_count_contract")
            if isinstance(targets.get("main_panel_count_contract"), dict)
            else {}
        )
        min_panels = max(1, _safe_int(count_contract.get("min"), 6))
        target_panels = max(min_panels, _safe_int(count_contract.get("target"), min_panels))
        max_panels = max(target_panels, _safe_int(count_contract.get("max"), target_panels))
        return min_panels, target_panels, max_panels
    return 6, 6, 6


def _dogfood_panel_content_plan_issues(
    args: dict[str, Any],
    blocks: list[dict[str, Any]],
    canvas: dict[str, Any],
    ctx: ToolContext,
) -> list[dict[str, Any]]:
    if not _dogfood_dense_mode(ctx):
        return []
    if _editorial_flow_mode(ctx):
        return []
    slots = [
        slot for slot in _main_layout_slot_blocks(blocks)
        if _slot_area_ratio(slot, canvas) >= 0.04
    ]
    issues: list[dict[str, Any]] = []
    landscape = _safe_float(canvas.get("w_px")) >= _safe_float(canvas.get("h_px"))
    if not slots:
        return [{
            "id": "main_panel_slots_missing",
            "message": "Dense dogfood poster has no measurable large main panels.",
            "repair": "Author the contract-defined visible group/article panel roots with data-slot-id/data-panel-role.",
        }]
    min_panels, target_panels, max_panels = _contract_main_panel_count_range(ctx)
    if landscape and len(slots) < min_panels:
        issues.append({
            "id": "main_panel_count_low",
            "main_panel_count": len(slots),
            "target_range": [min_panels, max_panels],
            "repair": "Split the storyboard into the contract-defined substantive main panels, excluding title/header/footer strips.",
        })
    if landscape and len(slots) > max_panels:
        issues.append({
            "id": "main_panel_count_high",
            "main_panel_count": len(slots),
            "target_range": [min_panels, max_panels],
            "target_main_panels": target_panels,
            "counted_main_panels": _main_panel_debug_summary(slots),
            "repair": "Merge extra top-level section shells into internal lanes, native rows, captions, or takeaways; automatic paper posters use the fixed CVPR three-column grid with exactly six main panels.",
        })

    plan_entries = _normalize_panel_content_plan(args.get("panel_content_plan"))
    if not plan_entries:
        sparse_slots: list[dict[str, Any]] = []
        for slot in slots:
            children = _slot_child_blocks(slot, blocks)
            actual_modes = _slot_actual_content_modes(children)
            actual_words = _slot_actual_word_count(children)
            if not _slot_has_enough_material_to_defer(
                actual_modes=actual_modes,
                actual_words=actual_words,
                plan_modes=actual_modes,
            ):
                sparse_slots.append({
                    "slot_id": str(slot.get("slot_id") or slot.get("block_id") or "").strip(),
                    "panel_role": str(slot.get("panel_role") or slot.get("role") or "").strip(),
                    "actual_modes": sorted(actual_modes),
                    "actual_word_count": actual_words,
                })
        issues.append({
            "id": "panel_content_plan_missing",
            "main_panel_count": len(slots),
            "sparse_slots": sparse_slots[:8],
            "repair": "Submit panel_content_plan with one entry per large main panel before the authored HTML.",
        })
        return issues

    plan_index: dict[str, dict[str, Any]] = {}
    for entry in plan_entries:
        for key in _panel_plan_keys(entry):
            plan_index.setdefault(key, entry)

    for slot in slots:
        slot_id = str(slot.get("slot_id") or slot.get("block_id") or "").strip()
        panel_role = str(slot.get("panel_role") or slot.get("role") or "").strip()
        keys = {_safe_block_id(slot_id, ""), _safe_block_id(panel_role, "")}
        keys.discard("")
        entry = next((plan_index.get(key) for key in keys if key in plan_index), None)
        children = _slot_child_blocks(slot, blocks)
        actual_modes = _slot_actual_content_modes(children)
        actual_words = _slot_actual_word_count(children)
        plan_modes = _panel_plan_content_modes(entry or {})
        has_deferrable_material = _slot_has_enough_material_to_defer(
            actual_modes=actual_modes,
            actual_words=actual_words,
            plan_modes=plan_modes or actual_modes,
        )
        if not entry:
            issues.append({
                "id": "panel_content_plan_entry_missing",
                "slot_id": slot_id,
                "panel_role": panel_role,
                "actual_modes": sorted(actual_modes),
                "actual_word_count": actual_words,
                "repair": "Add a panel_content_plan entry matching this required slot_id or panel_role.",
            })
            continue
        if len(plan_modes) < 2 and not has_deferrable_material:
            issues.append({
                "id": "panel_content_plan_modes_low",
                "slot_id": slot_id,
                "panel_role": panel_role,
                "declared_modes": sorted(plan_modes),
                "repair": "Declare at least two real content modes: source text, visual, native table/result, or caption/takeaway.",
            })
        if len(actual_modes) < 2 and not has_deferrable_material:
            issues.append({
                "id": "panel_actual_content_modes_low",
                "slot_id": slot_id,
                "panel_role": panel_role,
                "actual_modes": sorted(actual_modes),
                "actual_word_count": actual_words,
                "declared_modes": sorted(plan_modes),
                "repair": "Fill the panel with at least two visible content modes, not just an outer box or one prose/image lane.",
            })
        target_words = _safe_int(entry.get("target_words"), 0)
        if target_words and target_words < (45 if actual_modes & {"visual", "table"} else 80):
            issues.append({
                "id": "panel_content_plan_word_budget_low",
                "slot_id": slot_id,
                "target_words": target_words,
                "repair": "Raise the panel word budget or use a real native table/result unit plus caption/takeaway.",
            })
        if not (actual_modes & {"visual", "table"}) and actual_words < 70:
            issues.append({
                "id": "panel_actual_word_budget_low",
                "slot_id": slot_id,
                "actual_word_count": actual_words,
                "repair": "A text-only large panel needs enough source-backed copy to earn its area.",
            })
    return issues


def _dogfood_panel_density_issues(
    blocks: list[dict[str, Any]],
    canvas: dict[str, Any],
    ctx: ToolContext,
) -> list[dict[str, Any]]:
    if not _dogfood_dense_mode(ctx):
        return []
    if _editorial_flow_mode(ctx):
        return []
    slots = [
        slot for slot in _main_layout_slot_blocks(blocks)
        if _slot_area_ratio(slot, canvas) >= 0.04
    ]
    issues: list[dict[str, Any]] = []
    for slot in slots:
        slot_bbox = slot.get("bbox") if isinstance(slot.get("bbox"), dict) else {}
        slot_w = _safe_float(slot_bbox.get("w"))
        slot_h = _safe_float(slot_bbox.get("h"))
        if slot_w < 160 or slot_h < 120:
            continue
        slot_id = str(slot.get("slot_id") or slot.get("block_id") or "").strip()
        panel_role = str(slot.get("panel_role") or slot.get("role") or "").strip()
        children = _slot_meaningful_content_blocks(slot, blocks)
        actual_modes = _slot_actual_content_modes(children)
        actual_words = _slot_actual_word_count(children)
        if not children:
            issues.append({
                "id": "panel_effective_content_missing",
                "slot_id": slot_id,
                "panel_role": panel_role,
                "repair": (
                    "Fill this large panel with measured text, table rows, source figure notes, "
                    "metrics, captions, or takeaways before resubmitting authored HTML."
                ),
            })
            continue
        metrics = _slot_density_metrics(slot_bbox, [block["bbox"] for block in children if isinstance(block.get("bbox"), dict)])
        if not metrics:
            continue
        if (
            metrics["vertical_fill_ratio"] < 0.52
            and metrics["content_grid_coverage"] < 0.22
        ) or metrics["horizontal_fill_ratio"] < 0.54:
            issues.append({
                "id": "panel_effective_fill_low",
                "slot_id": slot_id,
                "panel_role": panel_role,
                "actual_modes": sorted(actual_modes),
                "actual_word_count": actual_words,
                **metrics,
                "repair": (
                    "Grow or split this panel's real content lanes so measured text/table/figure "
                    "boxes occupy the card, not just a small corner. Add source-backed bullets, "
                    "native result rows, a figure-reading note, or a compact takeaway band."
                ),
            })
        if (
            metrics["trailing_blank_ratio"] > 0.30
            and metrics["bottom_band_coverage"] < 0.12
            and metrics["content_bottom_ratio"] < 0.78
        ):
            issues.append({
                "id": "panel_trailing_blank_band",
                "slot_id": slot_id,
                "panel_role": panel_role,
                "actual_modes": sorted(actual_modes),
                "actual_word_count": actual_words,
                **metrics,
                "repair": (
                    "Use the empty lower band inside this panel for paper-grounded content: "
                    "concise visual interpretation, a compact comparison table, method notes, "
                    "ablation or limitation notes, source-grounded bullets, or a takeaway sentence. "
                    "Do not leave a tall card with only top-loaded content."
                ),
            })
    return issues


def _slot_meaningful_content_blocks(slot: dict[str, Any], blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in _slot_child_blocks(slot, blocks):
        if _is_meaningful_panel_content_block(block):
            out.append(block)
    return out


def _is_meaningful_panel_content_block(block: dict[str, Any]) -> bool:
    kind = str(block.get("kind") or "").lower()
    if kind == "shape":
        return False
    if kind == "flow_summary":
        return len(block.get("flow_modes") or []) >= 2 or int(_safe_float(block.get("flow_word_count"))) >= 18
    bbox = block.get("bbox") if isinstance(block.get("bbox"), dict) else {}
    if _bbox_area(bbox) < 180:
        return False
    role_blob = " ".join(
        str(block.get(key) or "")
        for key in ("block_id", "role", "panel_role", "slot_id", "caption", "text")
    ).lower()
    if any(token in role_blob for token in ("background", "backdrop", "decor", "watermark", "border", "divider")):
        return False
    if kind in {"image", "chart", "embed", "table", "caption", "quote", "metric"}:
        return True
    words = len(str(block.get("text") or block.get("caption") or "").split())
    if kind == "text":
        return words >= 2
    if kind == "group":
        return words >= 8 and any(
            token in role_blob
            for token in ("formula", "model", "pipeline", "result", "benchmark", "ablation", "metric", "takeaway")
        )
    return False


def _slot_density_metrics(
    slot_bbox: dict[str, Any],
    content_bboxes: list[dict[str, Any]],
) -> dict[str, Any]:
    clipped = [
        clipped_box for bbox in content_bboxes
        if (clipped_box := _clip_bbox_to_rect(bbox, slot_bbox)) is not None
    ]
    if not clipped:
        return {}
    sx = _safe_float(slot_bbox.get("x"))
    sy = _safe_float(slot_bbox.get("y"))
    sw = max(1.0, _safe_float(slot_bbox.get("w")))
    sh = max(1.0, _safe_float(slot_bbox.get("h")))
    left = min(_safe_float(bbox.get("x")) for bbox in clipped)
    top = min(_safe_float(bbox.get("y")) for bbox in clipped)
    right = max(_safe_float(bbox.get("x")) + _safe_float(bbox.get("w")) for bbox in clipped)
    bottom = max(_safe_float(bbox.get("y")) + _safe_float(bbox.get("h")) for bbox in clipped)
    return {
        "horizontal_fill_ratio": round((right - left) / sw, 3),
        "vertical_fill_ratio": round((bottom - top) / sh, 3),
        "content_bottom_ratio": round((bottom - sy) / sh, 3),
        "trailing_blank_ratio": round(max(0.0, (sy + sh) - bottom) / sh, 3),
        "content_grid_coverage": _slot_grid_coverage(clipped, slot_bbox, rows=10, cols=10),
        "bottom_band_coverage": _slot_grid_coverage(
            clipped,
            {
                "x": sx,
                "y": sy + sh * 0.66,
                "w": sw,
                "h": sh * 0.34,
            },
            rows=4,
            cols=10,
        ),
        "content_bbox": {
            "x": round(left, 2),
            "y": round(top, 2),
            "w": round(right - left, 2),
            "h": round(bottom - top, 2),
        },
    }


def _slot_grid_coverage(
    bboxes: list[dict[str, Any]],
    area_bbox: dict[str, Any],
    *,
    rows: int,
    cols: int,
) -> float:
    ax = _safe_float(area_bbox.get("x"))
    ay = _safe_float(area_bbox.get("y"))
    aw = max(1.0, _safe_float(area_bbox.get("w")))
    ah = max(1.0, _safe_float(area_bbox.get("h")))
    cell_w = aw / max(1, cols)
    cell_h = ah / max(1, rows)
    marked = 0
    total = max(1, rows * cols)
    for row in range(rows):
        for col in range(cols):
            cell = {
                "x": ax + col * cell_w,
                "y": ay + row * cell_h,
                "w": cell_w,
                "h": cell_h,
            }
            cell_area = max(1.0, cell_w * cell_h)
            if any(_bbox_overlap_area(cell, bbox) / cell_area >= 0.08 for bbox in bboxes):
                marked += 1
    return round(marked / total, 4)


def _clip_bbox_to_rect(bbox: dict[str, Any], rect: dict[str, Any]) -> dict[str, float] | None:
    x1 = max(_safe_float(rect.get("x")), _safe_float(bbox.get("x")))
    y1 = max(_safe_float(rect.get("y")), _safe_float(bbox.get("y")))
    x2 = min(
        _safe_float(rect.get("x")) + _safe_float(rect.get("w")),
        _safe_float(bbox.get("x")) + _safe_float(bbox.get("w")),
    )
    y2 = min(
        _safe_float(rect.get("y")) + _safe_float(rect.get("h")),
        _safe_float(bbox.get("y")) + _safe_float(bbox.get("h")),
    )
    if x2 <= x1 or y2 <= y1:
        return None
    return {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1}


def _main_panel_debug_summary(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for slot in slots[:16]:
        bbox = slot.get("bbox") if isinstance(slot.get("bbox"), dict) else {}
        out.append({
            "block_id": slot.get("block_id"),
            "slot_id": slot.get("slot_id"),
            "panel_role": slot.get("panel_role") or slot.get("role"),
            "parent_id": slot.get("parent_id"),
            "bbox": {
                "x": bbox.get("x"),
                "y": bbox.get("y"),
                "w": bbox.get("w"),
                "h": bbox.get("h"),
            },
        })
    return out


def _slot_has_enough_material_to_defer(
    *,
    actual_modes: set[str],
    actual_words: int,
    plan_modes: set[str],
) -> bool:
    if len(actual_modes) >= 2:
        return True
    if actual_modes & {"visual", "table"} and actual_words >= 45:
        return True
    if "source_text" in actual_modes and actual_words >= 80 and len(plan_modes) >= 2:
        return True
    if "native" in actual_modes and actual_words >= 60 and len(plan_modes) >= 2:
        return True
    return False


def _dogfood_benchmark_table_issues(
    blocks: list[dict[str, Any]],
    ctx: ToolContext,
) -> list[dict[str, Any]]:
    if not _dogfood_dense_mode(ctx):
        return []
    if _editorial_flow_mode(ctx):
        return []
    table_blocks = [block for block in blocks if str(block.get("kind") or "").lower() == "table"]
    result_tables = [block for block in table_blocks if _is_result_or_benchmark_block(block)]
    source_table_blocks = [
        block for block in blocks
        if _is_bound_source_table_block(block)
    ]
    result_band_units = _quantitative_result_band_unit_count(blocks)
    if source_table_blocks:
        return []
    if not result_tables:
        return [{
            "id": "benchmark_table_missing",
            "table_count": len(table_blocks),
            "source_table_count": len(source_table_blocks),
            "result_band_unit_count": result_band_units,
            "repair": "Use a bound ingest_table_* source table crop as the main benchmark/result evidence, with a nearby concise paper-grounded readout. Do not add a horizontal big-number metric/result band.",
        }]
    row_counts = [_table_body_row_count(block) for block in result_tables]
    max_rows = max(row_counts or [0])
    total_rows = sum(row_counts)
    if (
        max_rows >= 4
        or (len(result_tables) >= 2 and total_rows >= 6)
        or (max_rows >= 3 and result_band_units >= 4)
    ):
        return []
    if max_rows < 4:
        return [{
            "id": "benchmark_table_rows_low",
            "max_body_rows": max_rows,
            "total_body_rows": total_rows,
            "table_count": len(table_blocks),
            "result_table_count": len(result_tables),
            "result_band_unit_count": result_band_units,
            "repair": (
                "Use a bound ingest_table_* source crop for the main benchmark/result evidence, "
                "then add a small native summary table or concise paper-grounded readout only if needed. "
                "Do not add a horizontal big-number metric/result band."
            ),
        }]
    return []


def _normalize_panel_content_plan(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        for key in ("panels", "slots", "items", "entries"):
            value = raw.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if any(key in raw for key in ("slot_id", "panel_role", "claim", "target_words")):
            return [raw]
        return []
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _editorial_panel_content_plan_error(args: dict[str, Any], ctx: ToolContext) -> ToolResultRecord | None:
    if not _editorial_flow_mode(ctx):
        return None
    if not _normalize_panel_content_plan(args.get("panel_content_plan")):
        return None
    return obs_error(
        "conference_editorial_flow must not use legacy panel_content_plan.",
        category="validation",
        payload={
            "issue_id": "paper_poster_html_editorial_panel_content_plan_forbidden",
            "repair_route": "rewrite_as_three_column_editorial_flow",
            "hint": (
                "Use the editorial_column_plan contract and .poster-column/.poster-section DOM. "
                "Do not submit legacy slot/panel_content_plan data."
            ),
        },
    )


def _panel_plan_keys(entry: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for key in ("slot_id", "panel_role", "role", "panel_id", "block_id"):
        value = str(entry.get(key) or "").strip()
        if value:
            keys.add(_safe_block_id(value, ""))
    return {key for key in keys if key}


def _panel_plan_content_modes(entry: dict[str, Any]) -> set[str]:
    modes: set[str] = set()
    if not entry:
        return modes
    if str(entry.get("claim") or "").strip() or _as_nonempty_list(entry.get("text_units")):
        modes.add("source_text")
    if _safe_int(entry.get("target_words"), 0) >= 45:
        modes.add("source_text")
    if _as_nonempty_list(entry.get("source_refs")) or _as_nonempty_list(entry.get("evidence_refs")):
        modes.add("source_text")
    if _as_nonempty_list(entry.get("visual_plan")) or _as_nonempty_list(entry.get("visual_ids")):
        modes.add("visual")
    native_units = _as_nonempty_list(entry.get("native_units"))
    if native_units:
        native_blob = " ".join(str(item).lower() for item in native_units)
        if any(token in native_blob for token in ("table", "benchmark", "leaderboard", "ablation", "row", "result")):
            modes.add("table")
        else:
            modes.add("native")
    if (
        str(entry.get("local_explanation_plan") or "").strip()
        or str(entry.get("caption_plan") or "").strip()
        or str(entry.get("takeaway") or "").strip()
    ):
        modes.add("caption_takeaway")
    declared = entry.get("content_modes") or entry.get("modes")
    for item in _as_nonempty_list(declared):
        blob = str(item).lower()
        if "visual" in blob or "figure" in blob or "image" in blob:
            modes.add("visual")
        elif "table" in blob or "benchmark" in blob or "result" in blob:
            modes.add("table")
        elif "caption" in blob or "takeaway" in blob:
            modes.add("caption_takeaway")
        elif "text" in blob or "source" in blob:
            modes.add("source_text")
    return modes


def _quantitative_result_band_unit_count(blocks: list[dict[str, Any]]) -> int:
    units = 0
    for block in blocks:
        kind = str(block.get("kind") or "").lower()
        if kind == "table":
            continue
        role_blob = " ".join(
            str(block.get(key) or "")
            for key in ("block_id", "role", "panel_role", "slot_id", "caption", "text")
        ).lower()
        if not any(
            token in role_blob
            for token in (
                "result", "benchmark", "ablation", "comparison", "performance",
                "accuracy", "score", "metric", "stat", "result-band", "result_band",
            )
        ):
            continue
        text = str(block.get("text") or block.get("caption") or "")
        if re.search(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?\s*(?:%|pts?|x|m|b|k)?(?![A-Za-z])", text, flags=re.IGNORECASE):
            units += 1
    return min(12, units)


def _as_nonempty_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return [item for item in value if str(item or "").strip()]
    if isinstance(value, dict):
        return [value] if any(str(item or "").strip() for item in value.values()) else []
    if str(value or "").strip():
        return [value]
    return []


def _slot_area_ratio(slot: dict[str, Any], canvas: dict[str, Any]) -> float:
    area = _bbox_area(slot.get("bbox") if isinstance(slot.get("bbox"), dict) else {})
    canvas_area = max(1.0, _safe_float(canvas.get("w_px")) * _safe_float(canvas.get("h_px")))
    return area / canvas_area


def _bbox_area(bbox: dict[str, Any]) -> float:
    return max(0.0, _safe_float(bbox.get("w")) * _safe_float(bbox.get("h")))


def _slot_child_blocks(slot: dict[str, Any], blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    slot_bbox = slot.get("bbox") if isinstance(slot.get("bbox"), dict) else None
    if not slot_bbox:
        return []
    out: list[dict[str, Any]] = []
    flow_summary = slot.get("flow_panel_summary") if isinstance(slot.get("flow_panel_summary"), dict) else {}
    if flow_summary:
        out.append({
            "block_id": f"{slot.get('block_id') or slot.get('slot_id') or 'panel'}__flow_content",
            "kind": "flow_summary",
            "role": slot.get("role"),
            "panel_role": slot.get("panel_role"),
            "slot_id": slot.get("slot_id"),
            "bbox": slot_bbox,
            "flow_modes": list(flow_summary.get("modes") or []),
            "flow_word_count": int(_safe_float(flow_summary.get("word_count"))),
            "text": " ".join(["flow"] * max(0, min(500, int(_safe_float(flow_summary.get("word_count")))))),
        })
    for block in blocks:
        if block is slot:
            continue
        bbox = block.get("bbox") if isinstance(block.get("bbox"), dict) else None
        if bbox and _bbox_center_inside(bbox, slot_bbox):
            out.append(block)
    return out


def _slot_actual_content_modes(blocks: list[dict[str, Any]]) -> set[str]:
    modes: set[str] = set()
    for block in blocks:
        kind = str(block.get("kind") or "").lower()
        if kind == "flow_summary":
            modes.update(str(mode) for mode in (block.get("flow_modes") or []) if str(mode).strip())
            continue
        role_blob = " ".join(
            str(block.get(key) or "")
            for key in ("role", "panel_role", "slot_id", "block_id", "caption")
        ).lower()
        words = len(str(block.get("text") or block.get("caption") or "").split())
        if kind in {"image", "chart", "embed"}:
            modes.add("visual")
        elif kind == "table":
            modes.add("table")
        elif kind == "group" and words >= 10 and any(
            token in role_blob
            for token in ("card", "pipeline", "formula", "model", "ablation", "result", "benchmark", "metric")
        ):
            modes.add("native")
        elif kind in {"text", "quote", "metric"} and words >= 8:
            modes.add("source_text")
        elif kind == "caption" and words >= 5:
            modes.add("caption_takeaway")
        if any(token in role_blob for token in ("caption", "takeaway", "reading", "interpret", "limitation")) and words >= 5:
            modes.add("caption_takeaway")
        if str(block.get("source_id") or block.get("layer_id") or "").strip() and kind in {"image", "chart", "embed", "table"}:
            modes.add("visual" if kind != "table" else "table")
    return modes


def _slot_actual_word_count(blocks: list[dict[str, Any]]) -> int:
    flow_words = sum(
        max(0, int(_safe_float(block.get("flow_word_count"))))
        for block in blocks
        if str(block.get("kind") or "").lower() == "flow_summary"
    )
    return flow_words + sum(
        len(str(block.get("text") or block.get("caption") or "").split())
        for block in blocks
        if str(block.get("kind") or "").lower() in {"text", "caption", "metric", "quote"}
    )


def _is_result_or_benchmark_block(block: dict[str, Any]) -> bool:
    blob = " ".join(
        str(block.get(key) or "")
        for key in ("block_id", "role", "panel_role", "slot_id", "caption", "text")
    ).lower()
    if any(token in blob for token in (
        "result", "results", "benchmark", "leaderboard", "ablation", "comparison",
        "performance", "evaluation", "imagenet", "accuracy", "score",
    )):
        return True
    if str(block.get("kind") or "").lower() != "table":
        return False
    row_count = _table_body_row_count(block)
    numeric_cell_count = _table_numeric_cell_count(block)
    return row_count >= 3 and numeric_cell_count >= 4


def _is_bound_source_table_block(block: dict[str, Any]) -> bool:
    source_blob = " ".join(
        str(block.get(key) or "")
        for key in (
            "source_id", "layer_id", "src_path",
        )
    ).lower()
    if "ingest_table_" not in source_blob:
        return False
    has_crop_visual = bool(block.get("src_path") or block.get("image_size"))
    if not block.get("dom_bound_source_crop"):
        return False
    if str(block.get("table_visual_source") or "").lower() == "original_pdf_crop" and has_crop_visual:
        return True
    if _table_body_row_count(block) > 0 or block.get("headers"):
        return False
    return has_crop_visual


def _table_numeric_cell_count(block: dict[str, Any]) -> int:
    rows = block.get("rows") if isinstance(block.get("rows"), list) else []
    values: list[Any] = []
    for row in rows:
        if isinstance(row, dict):
            values.extend(row.values())
        elif isinstance(row, list):
            values.extend(row)
        else:
            values.append(row)
    if not values:
        values = re.split(r"\s+", str(block.get("text") or ""))
    count = 0
    for value in values:
        text = str(value or "").strip()
        if re.search(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?\s*(?:%|pts?|x|×|m|b|k)?(?![A-Za-z])", text, flags=re.IGNORECASE):
            count += 1
    return count


def _table_body_row_count(block: dict[str, Any]) -> int:
    rows = block.get("rows") if isinstance(block.get("rows"), list) else []
    if rows:
        return len(rows)
    text = str(block.get("text") or "")
    return max(0, text.count("\n") - 1)


def _join_css(authored_css: str, geometry_css: str, *, canvas: dict[str, Any] | None = None) -> str:
    root_css = ""
    if isinstance(canvas, dict):
        try:
            root_css = _root_canvas_lock_css(int(canvas["w_px"]), int(canvas["h_px"]))
        except (KeyError, TypeError, ValueError):
            root_css = ""
    return "\n\n".join(part for part in (authored_css.strip(), root_css, geometry_css.strip()) if part)
