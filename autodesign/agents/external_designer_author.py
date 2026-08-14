"""External designer-author adapter for paper-poster HTML authoring.

This is an experimental replacement for the paper-poster authoring pass:
the external subprocess writes standalone `poster.html`, while AutoDesign owns
paper ingest, input staging, preview capture, and final artifact promotion.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import mimetypes
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from bs4 import BeautifulSoup, Tag
from PIL import Image, ImageOps

from ..config import authoring_max_attempts_for, harness_subprocess_env
from ..designer import invoke_designer_tool
from ..schema import (
    ArtifactType,
    AttemptCandidate,
    CompositionArtifacts,
    DesignSpec,
    ToolResultRecord,
)
from ..skills.registry import SkillBundle, SkillRegistry
from ..tools import ToolContext
from ..util.browser_render import screenshot_html
from ..util.css_declaration_transform import (
    find_declaration_list_hash_tokens,
    find_stylesheet_hash_tokens,
    transform_declaration_list_values,
    transform_stylesheet_declaration_values,
)
from ..util.design_feedback import blocking_design_findings
from ..util.io import atomic_write_json, sha256_file
from ..util.logging import log
from ..attempt_candidates import (
    assert_promotion_run_unchanged,
    capture_attempt_candidate,
    is_browser_preview_resource_path,
    load_attempt_candidate,
)
from ..candidate_assessment import assess_delivery_issues
from ..attempt_selection import (
    assert_promotion_allowed,
    leased_promotion_tool_context,
    normal_promotion_lease,
    promotion_requested_run_dir,
    promote_pending_selection,
)
from ..util.math_typesetting import ensure_poster_katex_document, inline_katex_bundle
from ..util.poster_typesetting import apply_poster_typesetting_patch, revert_poster_typesetting_patch
from .external_author_process import (
    ExternalAuthorProcessRequest,
    context_attempt_selection_callback,
    context_cancellation_callback,
    context_cancellation_checkpoint,
    context_cancellation_token,
    derive_external_author_sensitive_values,
    run_external_author_process,
)
from .atomic_artifact_promotion import (
    publish_artifact_directory,
    recover_artifact_promotion,
)
from .reference_style_agent import ReferenceStyleAgentError, prepare_reference_style_contract

try:  # Worker A helper; keep staging usable while palette work lands in parallel.
    from ..util.academic_palette import (
        active_academic_color_system,
        academic_color_system_options,
        explicit_academic_color_system,
        rank_academic_color_system_options,
        select_academic_color_system,
    )
except Exception:  # pragma: no cover - supports partially landed worktrees
    active_academic_color_system = None
    academic_color_system_options = None
    explicit_academic_color_system = None
    rank_academic_color_system_options = None
    select_academic_color_system = None


_REQUIRED_INPUT_FILES = (
    "poster_content_brief.json",
    "poster_plan_contract.json",
    "paper_memory.json",
)
_OPTIONAL_INPUT_FILES = (
    "paper_visual_provenance.json",
    "paper_visual_storyboard.json",
    "poster_contract_preflight.json",
    "paper_memory.md",
    "paper_memory_dossier.json",
    "paper_memory_dossier.md",
    "canvas_plan.json",
    "reference_style_contract.json",
)
_INPUT_DIRS = (
    "paper_evidence_packs",
)
_OPTIONAL_INPUT_DIRS = (
    "layers",
)
_DEFAULT_POSTER_STABLE_S = 8.0
_MAX_CONSECUTIVE_NOOP_REPAIRS = 2
_MAX_CONSECUTIVE_NO_OUTPUT_RETRIES = 3
_REPAIR_CHANGED_STABLE_GRACE_S = 45.0
_REPAIR_IDENTICAL_STABLE_GRACE_S = 60.0
_SOFT_ACCEPT_MIN_ATTEMPT = 5
_SOURCE_WRAP_SOFT_LOCAL_WORD_LIMIT = 154
_BEST_CANDIDATE_FALLBACK_MIN_SCORE = 650
_ACADEMIC_TABLE_AUTHORING_CONTRACT = (
    "- Paper source tables use original PDF table crops, just like paper source "
    "figures use original paper figure crops. Selected, required, or placed "
    "`ingest_table_*` evidence must appear as the original source crop with "
    "matching `data-source-id` and `data-layer-id`, preferably on a "
    "`data-block-kind=\"table\"` figure/source-flow wrapper. Do not replace "
    "paper source tables with a native reconstruction, reduced native table, or "
    "re-rendered table image. Native tables summarize, not duplicate full "
    "source tables: use them only for compact benchmark readouts, method "
    "taxonomies, training-stage summaries, limitations evidence, metric "
    "comparisons, and other distilled poster content. "
    "- Author native HTML tables with an academic booktabs-style treatment: "
    "avoid boxed full-cell grids, heavy outer frames, double rules, default "
    "`th, td { border: 1px solid ... }` grids, saturated dark headers, and "
    "decorative zebra striping. Prefer light or white headers, thin horizontal "
    "top/header/bottom rules, optional very light row separators, and one muted "
    "emphasis row/cell/column. Poster-native summary/readout tables default to "
    "all-left alignment: left-align every `th` and `td`, including numeric "
    "values. Do not right-align or decimal-align numeric columns. Use "
    "all-center alignment only for short pure symbol/numeric matrices with no "
    "prose cells, no method/dataset row labels, no sentence fragments, and "
    "uniformly short values; apply it with explicit cell-level CSS such as "
    "`.all-center-matrix th, .all-center-matrix td { text-align: center; }`. "
    "Do not show the same paper table as both an original source crop and a "
    "full native reconstruction; if a source crop is placed, any nearby native "
    "table must be a smaller distilled summary/readout."
)
_SOURCE_FLOW_LIST_GUTTER_CONTRACT = (
    "- Floated source-flow lists must reserve a marker gutter. If a direct "
    "sibling `ul` or `ol` sits in a `.source-flow-unit` / `.figure-flow-unit` "
    "beside a `float-left` or `float-right` source asset, style that list with "
    "`display: flow-root`, `padding-inline-start: 1.25em` or more, "
    "`list-style-position: outside`, and `li { padding-inline-start: .25em }` "
    "or an equivalent safe gutter. Do not use `padding: 0`, `padding-left: 0`, "
    "negative text indents, or absolutely positioned custom bullets for those "
    "source-flow lists. If the text lane is too narrow for the bullet gutter, "
    "switch the source asset to a stacked/full-width flow instead of compressing "
    "the list."
)
_ACADEMIC_COLOR_AUTHORING_CONTRACT = (
    "- Choose exactly one academic palette from the curated `color_system_options`, "
    "unless `author_quick_brief.md` marks a required palette from the user "
    "prompt. Use `recommended_color_system` / `color_system` as the default "
    "random palette assignment for this poster; do not rerank palettes by paper "
    "domain, source figure/table color harmony, or institution/company/school "
    "color associations. Do not search the web, fetch logos, "
    "look up official brand hexes, add logo/icon marks, or make a branded "
    "poster. On `.paper-poster`, set `data-palette-id=\"<palette_id>\"` and "
    "define the exact `--poster-*` CSS variables for the one chosen palette. "
    "Use palette color sparingly. The identity header uses the fixed "
    "white/near-white treatment with a single top accent rule only; do "
    "not use bottom header rules, filled title bands, four-sided outlines, or "
    "mixed header styles for new paper posters. Use the primary color as compact filled section "
    "heading bands with white header text; primary may also appear in thin "
    "dividers and a few lead-key accents. "
    "Keep most "
    "poster surfaces white or neutral: panel interiors, section bodies, source "
    "wrappers, ordinary readouts, and native table cells must not become tinted "
    "palette boxes. Do not use secondary tints as large panel fills, table "
    "zebra rows, or boxed callout backgrounds. Do not mix header treatments, "
    "palettes, invent extra decorative "
    "colors, use gradients, add heavy colored borders, or return to "
    "default AI purple/indigo accents. Source figures and source table crops "
    "keep their original paper colors."
)
_EDITORIAL_LEAD_KEY_AUTHORING_CONTRACT = (
    "- Editorial lead-key emphasis: keep body, local readout, table-prose, "
    "caption, ordinary bullet, and table-cell parent text at font-weight 400. "
    "In motivation, method, results, analysis, limitations, takeaways, and "
    "source readouts, start many important paragraphs or bullets with one short "
    "inline lead phrase such as `<strong class=\"lead-key\">Training signal:</strong> ...` "
    "or `<strong class=\"lead-key\">Evidence:</strong> ...`. The lead phrase "
    "should usually be 2-5 words, with one-word academic labels such as "
    "`Problem:` or `Risk:` allowed when natural. Style only `strong.lead-key` "
    "as inline semibold/bold. Do not bold entire sentences, bullets, paragraphs, "
    "local readouts, table rows/cells, or card bodies; do not scatter many bold "
    "keywords through the same sentence; do not turn lead phrases into chips, "
    "badges, pills, all-caps labels, or section decorations; and do not put "
    "`data-block-id` on inline emphasis spans. Header identity rows, captions, "
    "formula cells, and source figure/table crops do not need lead-key emphasis."
)
_ACADEMIC_TYPOGRAPHY_FIXED_VALUES = {
    "poster_root": {
        "font_family": '"Times New Roman", Times, Georgia, serif',
    },
    "title": {
        "font_size_px": 56,
        "line_height": 1.08,
        "font_weight": 600,
    },
    "identity_rows": {
        "font_size_px": 28,
        "line_height": 1.16,
        "font_weight": 400,
    },
    "section_heading": {
        "font_size_px": 36,
        "line_height": 1.10,
        "font_weight": 700,
    },
    "body": {
        "font_size_px": 24,
        "line_height": 1.18,
        "font_weight": 400,
    },
    "readout": {
        "font_size_px": 24,
        "line_height": 1.18,
        "font_weight": 400,
    },
    "table_text": {
        "font_size_px": 24,
        "line_height": 1.18,
        "font_weight": 400,
    },
    "caption_label": {
        "font_size_px": 20,
        "line_height": 1.18,
        "font_weight": 400,
    },
    "font_size_tolerance_px": 0.5,
}
_ACADEMIC_TYPOGRAPHY_AUTHORING_CONTRACT = (
    '- Fixed typography values: `.paper-poster` / poster root uses `font-family: "Times New Roman", Times, Georgia, serif`; '
    "title 56px/1.08/600; author and affiliation identity rows 28px/1.16/400; "
    "major section headings 36px/1.10/700; body, local readouts, and table prose 24px/1.18/400; "
    "captions and labels 20px/1.18/400. Preserve these fixed values unless repair_context shows actual visible overflow or clipping in that local lane."
)


def _academic_typography_fixed_values() -> dict[str, dict[str, Any]]:
    return copy.deepcopy(_ACADEMIC_TYPOGRAPHY_FIXED_VALUES)


@dataclass(frozen=True)
class _InvocationResult:
    status: str
    reason: str
    returncode: int | None = None
    timed_out: bool = False
    elapsed_s: float = 0.0
    poster_sha256: str = ""


class ExternalDesignerAuthor:
    """Run a local coding-agent command as the paper-poster author."""

    def __init__(self, settings: Any, system_prompt: str):
        self.settings = settings
        self.system_prompt = system_prompt
        self._total_in = 0
        self._total_out = 0
        self._total_cache_read = 0
        self._total_cache_create = 0

    def run(self, brief: str, ctx: ToolContext) -> None:
        context_cancellation_checkpoint(ctx, "external_poster.before_start")
        if promote_pending_selection(ctx) != "none":
            return
        _recover_poster_final_promotion(ctx.run_dir / "final")
        context_cancellation_checkpoint(ctx, "external_poster.after_recovery")
        ctx.state.pop("designer_api_error", None)
        harness = str(getattr(self.settings, "designer_author_harness", "custom") or "custom").strip()
        command = str(getattr(self.settings, "designer_author_cmd", "") or "").strip()
        timeout_s = max(1, int(getattr(self.settings, "designer_author_timeout_s", 1800) or 1800))
        max_attempts = authoring_max_attempts_for(self.settings, "poster")
        poster_stable_s = max(
            0.25,
            float(getattr(self.settings, "designer_author_poster_stable_s", _DEFAULT_POSTER_STABLE_S) or _DEFAULT_POSTER_STABLE_S),
        )
        designer_author_dir = ctx.run_dir / "designer_author"
        context_cancellation_checkpoint(ctx, "external_poster.before_author_dir")
        designer_author_dir.mkdir(parents=True, exist_ok=True)
        context_cancellation_checkpoint(ctx, "external_poster.after_author_dir")
        log(
            "designer_author.start",
            mode="external",
            attempt_dir=str(designer_author_dir),
            timeout_s=timeout_s,
            max_attempts=max_attempts,
            attempt_budget=max_attempts,
            repair_until_pass=True,
            harness=harness or "custom",
            command_configured=bool(command),
        )

        if not command:
            self._fail(
                ctx,
                "missing_designer_author_cmd",
                "external designer author requires AUTODESIGN_DESIGNER_AUTHOR_CMD, "
                "--designer-author-cmd, or a supported --designer-author-harness",
                designer_author_dir,
            )
            return

        resume_pre = (
            ctx.state.get("designer_author_resume")
            if isinstance(ctx.state.get("designer_author_resume"), dict)
            else None
        )
        reference_path = str(ctx.state.get("reference_poster_path") or "").strip()
        if reference_path:
            try:
                prepare_reference_style_contract(
                    ctx,
                    Path(reference_path),
                    command=command,
                    harness=harness,
                    model_hint=str(getattr(self.settings, "designer_author_model", "") or ""),
                    timeout_s=min(timeout_s, 900),
                    page_index=max(0, int(ctx.state.get("reference_page_index") or 0)),
                )
            except (OSError, ReferenceStyleAgentError, ValueError) as exc:
                self._fail(
                    ctx,
                    "reference_style_analysis_failed",
                    str(exc),
                    designer_author_dir,
                )
                return

        if not self._ensure_ingested(ctx):
            return
        context_cancellation_checkpoint(ctx, "external_poster.after_ingest")

        # Consume resume pre-seed (set by runner._run_inner when --resume-run
        # is in effect). This lets the loop continue from attempt N+1 with the
        # last validation_feedback packet as its repair driver.
        ctx.state.pop("designer_author_resume", None)

        repair_feedback: dict[str, Any] | None = None
        previous_attempt_dir: Path | None = None
        consecutive_noop_repairs = 0
        consecutive_no_output_retries = 0
        if isinstance(resume_pre, dict):
            repair_feedback = (
                resume_pre.get("repair_feedback")
                if isinstance(resume_pre.get("repair_feedback"), dict)
                else None
            )
            prev = resume_pre.get("previous_attempt_dir")
            if prev:
                previous_attempt_dir = Path(prev)
            log(
                "designer_author.resume",
                mode="external",
                source_run_dir=resume_pre.get("source_run_dir"),
                prior_attempts=int(resume_pre.get("prior_attempts") or 0),
                incremental_max_attempts=max_attempts,
                repair_seed=repair_feedback is not None,
            )

        # `max_attempts` is per-call (fresh runs: absolute cap; resumed runs:
        # incremental budget). `absolute_attempt_budget` is the cumulative
        # cap shown in logs so telemetry remains honest across resumes.
        prior_attempts_before = int(ctx.state.get("designer_author_attempts") or 0)
        absolute_attempt_budget = prior_attempts_before + max_attempts
        ctx.state["designer_author_attempt_budget"] = absolute_attempt_budget
        for loop_index in range(1, max_attempts + 1):
            context_cancellation_checkpoint(ctx, "external_poster.before_attempt")
            if promote_pending_selection(ctx) != "none":
                return
            context_cancellation_checkpoint(ctx, "external_poster.after_selection_check")
            attempt_dir = self._next_attempt_dir(ctx)
            attempt_index = int(ctx.state.get("designer_author_attempts") or 0)
            has_more_attempts_in_call = loop_index < max_attempts
            log(
                "designer_author.attempt_start",
                mode="external",
                attempt=attempt_index,
                max_attempts=absolute_attempt_budget,
                attempt_budget=absolute_attempt_budget,
                call_budget=max_attempts,
                call_index=loop_index,
                attempt_dir=str(attempt_dir),
                repair=repair_feedback is not None,
            )

            context_cancellation_checkpoint(ctx, "external_poster.before_staging")
            stage_ok = self._stage_inputs(
                ctx,
                brief=brief,
                attempt_dir=attempt_dir,
                repair_feedback=repair_feedback,
                previous_attempt_dir=previous_attempt_dir,
            )
            if not stage_ok:
                return
            context_cancellation_checkpoint(ctx, "external_poster.after_staging")

            prompt = self._build_prompt(
                ctx,
                brief=brief,
                attempt_dir=attempt_dir,
                repair_feedback=repair_feedback,
                attempt_index=attempt_index,
                max_attempts=max_attempts,
            )
            prompt_path = attempt_dir / "designer_author_prompt.md"
            context_cancellation_checkpoint(ctx, "external_poster.before_prompt_write")
            prompt_path.write_text(prompt, encoding="utf-8")
            context_cancellation_checkpoint(ctx, "external_poster.after_prompt_write")

            previous_poster_sha = ""
            if repair_feedback is not None and previous_attempt_dir is not None:
                previous_poster_path = previous_attempt_dir / "poster.html"
                if previous_poster_path.exists():
                    previous_poster_sha = sha256_file(previous_poster_path)

            invocation = self._invoke_author_command(
                command,
                prompt=prompt,
                attempt_dir=attempt_dir,
                timeout_s=timeout_s,
                poster_stable_s=poster_stable_s,
                previous_poster_sha256=previous_poster_sha,
                run_id=ctx.run_id,
                attempt=attempt_index,
                ctx=ctx,
            )
            context_cancellation_checkpoint(ctx, "external_poster.after_author_process")
            invocation_payload = asdict(invocation)
            _log_designer_author_agent_output(
                attempt_dir=attempt_dir,
                attempt_index=attempt_index,
                max_attempts=max_attempts,
                invocation=invocation_payload,
            )
            ctx.state["designer_author_invocation"] = invocation_payload
            attempt_records = list(ctx.state.get("designer_author_attempt_records") or [])
            attempt_records.append({
                "attempt": attempt_index,
                "attempt_dir": str(attempt_dir),
                "invocation": invocation_payload,
            })
            ctx.state["designer_author_attempt_records"] = attempt_records
            poster_path = attempt_dir / "poster.html"
            if invocation.status == "selected":
                promote_pending_selection(ctx)
                return
            if invocation.status != "ok" or not poster_path.exists():
                # 快速空退(进程退出但没写 poster.html)重试;超时不重试
                # (timeout 已烧掉整段预算,重试代价太高)。
                is_retryable_no_output = (
                    not invocation.timed_out and not poster_path.exists()
                )
                if is_retryable_no_output:
                    consecutive_no_output_retries += 1
                if (
                    is_retryable_no_output
                    and has_more_attempts_in_call
                    and consecutive_no_output_retries < _MAX_CONSECUTIVE_NO_OUTPUT_RETRIES
                ):
                    # 结构化留痕(仅记录,不改 repair 基准):
                    feedback = _command_no_output_feedback(
                        attempt_dir=attempt_dir,
                        attempt_index=attempt_index,
                        invocation=invocation_payload,
                    )
                    _record_feedback(ctx, attempt_dir, feedback)
                    log(
                        "designer_author.retry",
                        mode="external",
                        attempt=attempt_index,
                        next_attempt=attempt_index + 1,
                        issue_id="designer_author_no_output",
                        error_category="command_no_output",
                        consecutive_no_output_retries=consecutive_no_output_retries,
                    )
                    # 保持 repair_feedback / previous_attempt_dir 不变:
                    # - repair 轮空退 → 下一轮仍从上一个好 poster 继续修
                    # - 初稿空退(两者为 None)→ 下一轮重新画
                    continue
                if self._try_promote_delivery_fallbacks(
                    ctx,
                    attempt_index=attempt_index,
                    attempt_dir=attempt_dir,
                    last_feedback=repair_feedback,
                    source_reason=invocation.reason or "designer_author_no_output",
                    source_message="external designer author did not produce poster.html",
                ):
                    return
                self._fail(
                    ctx,
                    invocation.reason or "designer_author_no_output",
                    "external designer author did not produce poster.html",
                    attempt_dir,
                )
                return

            # 成功产出 poster 后归零连续空退计数,避免跨轮误累计。
            consecutive_no_output_retries = 0

            if repair_feedback is not None and previous_attempt_dir is not None:
                previous_poster_path = previous_attempt_dir / "poster.html"
                if (
                    previous_poster_path.exists()
                    and invocation.poster_sha256
                    and invocation.poster_sha256 == sha256_file(previous_poster_path)
                ):
                    feedback = _noop_repair_feedback(
                        previous_feedback=repair_feedback,
                        attempt_index=attempt_index,
                        attempt_dir=attempt_dir,
                    )
                    _record_feedback(ctx, attempt_dir, feedback)
                    capture_poster_attempt_candidate(
                        ctx=ctx,
                        attempt_dir=attempt_dir,
                        attempt=attempt_index,
                        max_attempts=absolute_attempt_budget,
                        diagnostics={
                            **feedback,
                            "candidate_safety_state": "blocked",
                        },
                    )
                    if promote_pending_selection(ctx) != "none":
                        return
                    consecutive_noop_repairs += 1
                    if consecutive_noop_repairs >= _MAX_CONSECUTIVE_NOOP_REPAIRS:
                        if self._try_promote_delivery_fallbacks(
                            ctx,
                            attempt_index=attempt_index,
                            attempt_dir=attempt_dir,
                            last_feedback=feedback,
                            source_reason="designer_author_repair_noop",
                            source_message="external designer author repair attempts produced identical poster.html repeatedly",
                        ):
                            return
                        self._fail(
                            ctx,
                            "designer_author_repair_noop",
                            "external designer author repair attempts produced identical poster.html repeatedly",
                            attempt_dir,
                            payload=feedback,
                        )
                        return
                    if has_more_attempts_in_call:
                        repair_feedback = feedback
                        previous_attempt_dir = attempt_dir
                        log(
                            "designer_author.retry",
                            mode="external",
                            attempt=attempt_index,
                            next_attempt=attempt_index + 1,
                            issue_id="designer_author_repair_noop",
                            error_category="validation",
                        )
                        continue
                    if self._try_promote_delivery_fallbacks(
                        ctx,
                        attempt_index=attempt_index,
                        attempt_dir=attempt_dir,
                        last_feedback=feedback,
                        source_reason="designer_author_repair_noop",
                        source_message="external designer author repair attempt produced identical poster.html",
                    ):
                        return
                    self._fail(
                        ctx,
                        "designer_author_repair_noop",
                        "external designer author repair attempt produced identical poster.html",
                        attempt_dir,
                        payload=feedback,
                    )
                    return

                consecutive_noop_repairs = 0
                scope_feedback = _local_repair_scope_violation_feedback(
                    previous_feedback=repair_feedback,
                    previous_poster_path=previous_poster_path,
                    poster_path=poster_path,
                    attempt_index=attempt_index,
                    attempt_dir=attempt_dir,
                )
                if scope_feedback is not None:
                    _record_feedback(ctx, attempt_dir, scope_feedback)
                    capture_poster_attempt_candidate(
                        ctx=ctx,
                        attempt_dir=attempt_dir,
                        attempt=attempt_index,
                        max_attempts=absolute_attempt_budget,
                        diagnostics={
                            **scope_feedback,
                            "candidate_safety_state": "blocked",
                        },
                    )
                    if promote_pending_selection(ctx) != "none":
                        return
                    if has_more_attempts_in_call:
                        repair_feedback = scope_feedback
                        log(
                            "designer_author.retry",
                            mode="external",
                            attempt=attempt_index,
                            next_attempt=attempt_index + 1,
                            issue_id="designer_author_local_repair_scope_violation",
                            error_category="validation",
                            stage="local_repair_scope_guard",
                        )
                        continue
                    if self._try_promote_delivery_fallbacks(
                        ctx,
                        attempt_index=attempt_index,
                        attempt_dir=attempt_dir,
                        last_feedback=scope_feedback,
                        source_reason="designer_author_local_repair_scope_violation",
                        source_message="external designer author repair changed content outside its local repair scope",
                    ):
                        return
                    self._fail(
                        ctx,
                        "designer_author_local_repair_scope_violation",
                        "external designer author repair changed content outside its local repair scope",
                        attempt_dir,
                        payload=scope_feedback,
                    )
                    return

            validation_feedback = self._direct_final_validation_feedback(
                ctx,
                attempt_index=attempt_index,
                attempt_dir=attempt_dir,
                poster_path=poster_path,
            )
            context_cancellation_checkpoint(ctx, "external_poster.after_validation")
            if validation_feedback is not None:
                validation_feedback = _source_visual_repair_regression_feedback(repair_feedback, validation_feedback)
                consecutive_noop_repairs = 0
                _record_feedback(ctx, attempt_dir, validation_feedback)
                if _auto_repair_source_flow_list_gutter(ctx, attempt_dir, poster_path, validation_feedback):
                    validation_feedback = self._direct_final_validation_feedback(
                        ctx,
                        attempt_index=attempt_index,
                        attempt_dir=attempt_dir,
                        poster_path=poster_path,
                    )
                    if validation_feedback is None:
                        self._apply_typesetting_with_validation(
                            ctx,
                            attempt_index=attempt_index,
                            attempt_dir=attempt_dir,
                            poster_path=poster_path,
                            phase="direct_final_auto_repair",
                        )
                        outcome = self._promote_direct_final_after_critic(
                            ctx,
                            attempt_index=attempt_index,
                            attempt_dir=attempt_dir,
                            poster_path=poster_path,
                            poster_sha256=sha256_file(poster_path),
                            has_more_attempts_in_call=has_more_attempts_in_call,
                            phase="direct_final_auto_repair",
                        )
                        if outcome.get("action") == "retry":
                            repair_feedback = outcome.get("feedback")
                            previous_attempt_dir = attempt_dir
                            continue
                        return
                    validation_feedback = _source_visual_repair_regression_feedback(repair_feedback, validation_feedback)
                    _record_feedback(ctx, attempt_dir, validation_feedback)
                if _auto_repair_typography_line_height(ctx, attempt_dir, poster_path, validation_feedback):
                    validation_feedback = self._direct_final_validation_feedback(
                        ctx,
                        attempt_index=attempt_index,
                        attempt_dir=attempt_dir,
                        poster_path=poster_path,
                    )
                    if validation_feedback is None:
                        self._apply_typesetting_with_validation(
                            ctx,
                            attempt_index=attempt_index,
                            attempt_dir=attempt_dir,
                            poster_path=poster_path,
                            phase="direct_final_auto_repair",
                        )
                        outcome = self._promote_direct_final_after_critic(
                            ctx,
                            attempt_index=attempt_index,
                            attempt_dir=attempt_dir,
                            poster_path=poster_path,
                            poster_sha256=sha256_file(poster_path),
                            has_more_attempts_in_call=has_more_attempts_in_call,
                            phase="direct_final_auto_repair",
                        )
                        if outcome.get("action") == "retry":
                            repair_feedback = outcome.get("feedback")
                            previous_attempt_dir = attempt_dir
                            continue
                        return
                    validation_feedback = _source_visual_repair_regression_feedback(repair_feedback, validation_feedback)
                    _record_feedback(ctx, attempt_dir, validation_feedback)
                soft_acceptance = _soft_finalizable_direct_validation_feedback(
                    validation_feedback,
                    attempt_index,
                    max_attempts=max_attempts,
                )
                capture_poster_attempt_candidate(
                    ctx=ctx,
                    attempt_dir=attempt_dir,
                    attempt=attempt_index,
                    max_attempts=absolute_attempt_budget,
                    diagnostics={
                        **validation_feedback,
                        "candidate_safety_state": (
                            "ready_with_warnings" if soft_acceptance else "blocked"
                        ),
                    },
                )
                if promote_pending_selection(ctx) != "none":
                    return
                if soft_acceptance:
                    ctx.state["designer_author_soft_acceptance"] = soft_acceptance
                    log(
                        "designer_author.direct_final_soft_accept",
                        mode="external",
                        attempt=attempt_index,
                        attempt_dir=str(attempt_dir),
                        issue_id=soft_acceptance.get("issue_id"),
                        reason=soft_acceptance.get("reason"),
                    )
                    self._apply_typesetting_with_validation(
                        ctx,
                        attempt_index=attempt_index,
                        attempt_dir=attempt_dir,
                        poster_path=poster_path,
                        phase="soft_accept",
                    )
                    self._promote_direct_final(
                        ctx,
                        attempt_index=attempt_index,
                        attempt_dir=attempt_dir,
                        poster_path=poster_path,
                        poster_sha256=sha256_file(poster_path),
                        acceptance_path="soft_accept",
                    )
                    ctx.state["finalize_notes"] = ""
                    if isinstance(ctx.state.get("designer_author_result"), dict):
                        ctx.state["designer_author_result"]["soft_acceptance"] = soft_acceptance
                    return
                issue_id = str(
                    validation_feedback.get("summary", {}).get("issue_id")
                    or validation_feedback.get("payload", {}).get("issue_id")
                    or "paper_poster_html_direct_final_validation_failed"
                )
                if has_more_attempts_in_call:
                    validation_feedback = self._merge_attempt_level_critic_review(
                        ctx,
                        attempt_index=attempt_index,
                        attempt_dir=attempt_dir,
                        poster_path=poster_path,
                        validation_feedback=validation_feedback,
                    )
                    _record_feedback(ctx, attempt_dir, validation_feedback)
                    repair_feedback = validation_feedback
                    previous_attempt_dir = attempt_dir
                    summary = validation_feedback.get("summary", {})
                    log(
                        "designer_author.retry",
                        mode="external",
                        attempt=attempt_index,
                        next_attempt=attempt_index + 1,
                        stage=summary.get("validation_stage") or "direct_final_preflight",
                        issue_id=issue_id,
                        error_category="validation",
                    )
                    continue
                if self._try_promote_delivery_fallbacks(
                    ctx,
                    attempt_index=attempt_index,
                    attempt_dir=attempt_dir,
                    last_feedback=validation_feedback,
                    source_reason=issue_id,
                    source_message=(
                        validation_feedback.get("error_message")
                        or "external designer author direct-final poster failed HTML-first preflight"
                    ),
                ):
                    return
                self._fail(
                    ctx,
                    issue_id,
                    validation_feedback.get("error_message")
                    or "external designer author direct-final poster failed HTML-first preflight",
                    attempt_dir,
                    payload=validation_feedback,
                )
                return

            self._apply_typesetting_with_validation(
                ctx,
                attempt_index=attempt_index,
                attempt_dir=attempt_dir,
                poster_path=poster_path,
                phase="direct_final",
            )
            math_record = ensure_poster_katex_document(
                poster_path,
                ctx.settings.repo_root,
                root_selector=".paper-poster",
            )
            if math_record.get("detected"):
                ctx.state["designer_author_math_typesetting"] = math_record
            outcome = self._promote_direct_final_after_critic(
                ctx,
                attempt_index=attempt_index,
                attempt_dir=attempt_dir,
                poster_path=poster_path,
                poster_sha256=sha256_file(poster_path),
                has_more_attempts_in_call=has_more_attempts_in_call,
                phase="direct_final",
            )
            if outcome.get("action") == "retry":
                repair_feedback = outcome.get("feedback")
                previous_attempt_dir = attempt_dir
                continue
            return

    def _direct_final_validation_feedback(
        self,
        ctx: ToolContext,
        *,
        attempt_index: int,
        attempt_dir: Path,
        poster_path: Path,
    ) -> dict[str, Any] | None:
        """Run the standalone poster through the normal HTML-first preflight."""
        from ..tools.propose_paper_poster_html import propose_paper_poster_html

        html_text = poster_path.read_text(encoding="utf-8", errors="replace")
        canvas = {
            **_direct_canvas(ctx),
            "dpi": 150,
            "aspect_ratio": "2:1",
            "color_mode": "RGB",
        }
        had_design_spec = "design_spec" in ctx.state
        previous_design_spec = ctx.state.get("design_spec")
        try:
            result = propose_paper_poster_html(
                {
                    "html": html_text,
                    "canvas": canvas,
                    "designer_owned_css": True,
                    "browser_flow": "editorial_flow",
                },
                ctx=ctx,
            )
            validated_design_spec = copy.deepcopy(ctx.state.get("design_spec"))
            latest_candidate = copy.deepcopy(ctx.state.get("paper_poster_html_latest_candidate") or {})
        finally:
            if had_design_spec:
                ctx.state["design_spec"] = previous_design_spec
            else:
                ctx.state.pop("design_spec", None)

        if result.status == "ok":
            candidate_relative_dir = None
            candidate_preview_relative = None
            candidate_measurement_relative = None
            if isinstance(latest_candidate, dict):
                candidate_relative_dir = latest_candidate.get("candidate_relative_dir")
                candidate_preview_relative = (
                    latest_candidate.get("preview_png_relative")
                    or latest_candidate.get("candidate_preview_png_relative")
                )
                candidate_measurement_relative = (
                    latest_candidate.get("measurement_json_relative")
                    or latest_candidate.get("candidate_measurement_json_relative")
                )
            ctx.state["designer_author_direct_final_critic_candidate"] = {
                "attempt": attempt_index,
                "attempt_dir": str(attempt_dir),
                "poster_path": str(poster_path),
                "poster_relative_path": _run_relative_path(ctx, poster_path),
                "design_spec": validated_design_spec,
                "candidate": latest_candidate if isinstance(latest_candidate, dict) else {},
                "candidate_relative_dir": candidate_relative_dir,
                "candidate_preview_png_relative": candidate_preview_relative,
                "candidate_measurement_json_relative": candidate_measurement_relative,
            }
            log(
                "designer_author.direct_final_validation_pass",
                mode="external",
                attempt=attempt_index,
                attempt_dir=str(attempt_dir),
            )
            return None

        payload = dict(result.payload or {})
        payload.update({
            "direct_final_preflight": True,
            "validation_stage": payload.get("stage") or payload.get("repair_route") or "",
        })
        patched = ToolResultRecord(
            status=result.status,
            error_message=result.error_message,
            error_category=result.error_category,
            payload=payload,
        )
        feedback = _validation_feedback(
            tool_name="propose_paper_poster_html",
            result=patched,
            attempt_index=attempt_index,
            attempt_dir=attempt_dir,
        )
        log(
            "designer_author.direct_final_validation_block",
            mode="external",
            attempt=attempt_index,
            attempt_dir=str(attempt_dir),
            issue_id=payload.get("issue_id"),
            issue_count=len(payload.get("issues") or []),
            candidate_relative_dir=payload.get("candidate_relative_dir"),
        )
        return feedback

    def _apply_typesetting_with_validation(
        self,
        ctx: ToolContext,
        *,
        attempt_index: int,
        attempt_dir: Path,
        poster_path: Path,
        phase: str,
    ) -> dict[str, Any] | None:
        """Apply final rhythm CSS only when the patched HTML still validates."""

        record_path = attempt_dir / f"auto_typesetting_{phase}.json"
        backup_path = attempt_dir / f"poster_before_typesetting_{phase}.html"
        record = apply_poster_typesetting_patch(
            poster_path,
            record_path=record_path,
            backup_path=backup_path,
            phase=phase,
        )
        if not record.get("applied"):
            return record

        state_snapshot = copy.deepcopy(ctx.state)
        feedback = self._direct_final_validation_feedback(
            ctx,
            attempt_index=attempt_index,
            attempt_dir=attempt_dir,
            poster_path=poster_path,
        )
        if feedback is None:
            ctx.state["designer_author_typesetting"] = record
            log(
                "designer_author.typesetting_pass",
                mode="external",
                attempt=attempt_index,
                attempt_dir=str(attempt_dir),
                phase=phase,
                profile=record.get("profile"),
            )
            return record

        summary = feedback.get("summary") if isinstance(feedback.get("summary"), dict) else {}
        payload = feedback.get("payload") if isinstance(feedback.get("payload"), dict) else {}
        issue_id = str(
            summary.get("issue_id")
            or payload.get("issue_id")
            or "paper_poster_html_typesetting_validation_failed"
        )
        reverted = revert_poster_typesetting_patch(record)
        if reverted:
            ctx.state.clear()
            ctx.state.update(state_snapshot)
        record.update({
            "reverted": reverted,
            "revert_issue_id": issue_id,
            "revert_reason": "typesetting_patch_failed_validation",
        })
        atomic_write_json(record_path, record)
        log(
            "designer_author.typesetting_reverted",
            mode="external",
            attempt=attempt_index,
            attempt_dir=str(attempt_dir),
            phase=phase,
            issue_id=issue_id,
            reverted=reverted,
        )
        return None

    def _promote_direct_final_after_critic(
        self,
        ctx: ToolContext,
        *,
        attempt_index: int,
        attempt_dir: Path,
        poster_path: Path,
        poster_sha256: str,
        has_more_attempts_in_call: bool,
        phase: str,
    ) -> dict[str, Any]:
        candidate = capture_poster_attempt_candidate(
            ctx=ctx,
            attempt_dir=attempt_dir,
            attempt=attempt_index,
            max_attempts=max(
                attempt_index,
                int(ctx.state.get("designer_author_attempt_budget") or attempt_index),
            ),
            diagnostics={
                "accepted": True,
                "candidate_safety_state": "ready",
                "phase": phase,
            },
        )
        if promote_pending_selection(ctx) != "none":
            return {"action": "done", "candidate_id": candidate.candidate_id}
        if not has_more_attempts_in_call:
            log(
                "designer_author.critic_skip",
                mode="external",
                attempt=attempt_index,
                attempt_dir=str(attempt_dir),
                phase=phase,
                reason="final_attempt_no_repair_budget",
            )
            self._promote_direct_final(
                ctx,
                attempt_index=attempt_index,
                attempt_dir=attempt_dir,
                poster_path=poster_path,
                poster_sha256=poster_sha256,
                acceptance_path="critic_skipped_final_attempt",
            )
            return {"action": "done"}

        ctx.state.pop("designer_author_direct_final_acceptance_path", None)
        feedback = self._direct_final_critic_feedback(
            ctx,
            attempt_index=attempt_index,
            attempt_dir=attempt_dir,
            poster_path=poster_path,
            phase=phase,
        )
        if feedback is None:
            acceptance_path = str(
                ctx.state.pop("designer_author_direct_final_acceptance_path", "")
                or "critic_pass"
            )
            self._promote_direct_final(
                ctx,
                attempt_index=attempt_index,
                attempt_dir=attempt_dir,
                poster_path=poster_path,
                poster_sha256=poster_sha256,
                acceptance_path=acceptance_path,
            )
            return {"action": "done"}

        if _critic_feedback_is_infrastructure_failure(feedback):
            log(
                "designer_author.critic_skip",
                mode="external",
                attempt=attempt_index,
                attempt_dir=str(attempt_dir),
                phase=phase,
                reason="critic_infrastructure_failure",
            )
            self._promote_direct_final(
                ctx,
                attempt_index=attempt_index,
                attempt_dir=attempt_dir,
                poster_path=poster_path,
                poster_sha256=poster_sha256,
                acceptance_path="critic_skipped_infrastructure_failure",
            )
            return {"action": "done"}

        _record_feedback(ctx, attempt_dir, feedback)
        summary = feedback.get("summary") if isinstance(feedback.get("summary"), dict) else {}
        payload = feedback.get("payload") if isinstance(feedback.get("payload"), dict) else {}
        issue_id = str(
            summary.get("issue_id")
            or payload.get("issue_id")
            or "designer_author_critic_feedback"
        )
        if has_more_attempts_in_call and _critic_feedback_should_retry(feedback):
            log(
                "designer_author.retry",
                mode="external",
                attempt=attempt_index,
                next_attempt=attempt_index + 1,
                stage="critic_vision_review",
                issue_id=issue_id,
                error_category="critic",
            )
            return {"action": "retry", "feedback": feedback}

        if _critic_feedback_blocks_delivery_fallback(feedback):
            self._fail(
                ctx,
                issue_id,
                feedback.get("error_message") or "external designer author poster failed critic vision review",
                attempt_dir,
                payload=feedback,
            )
            return {"action": "done"}

        if self._try_promote_delivery_fallbacks(
            ctx,
            attempt_index=attempt_index,
            attempt_dir=attempt_dir,
            last_feedback=feedback,
            source_reason=issue_id,
            source_message=feedback.get("error_message")
            or "external designer author poster failed critic vision review",
        ):
            return {"action": "done"}
        self._fail(
            ctx,
            issue_id,
            feedback.get("error_message") or "external designer author poster failed critic vision review",
            attempt_dir,
            payload=feedback,
        )
        return {"action": "done"}

    def _direct_final_critic_feedback(
        self,
        ctx: ToolContext,
        *,
        attempt_index: int,
        attempt_dir: Path,
        poster_path: Path,
        phase: str,
    ) -> dict[str, Any] | None:
        """Run CriticAgent on a deterministic-valid direct-final candidate."""
        max_critique_iters = _safe_int(getattr(ctx.settings, "max_critique_iters", 0), default=0)
        if max_critique_iters <= 0:
            ctx.state["designer_author_direct_final_acceptance_path"] = "critic_skipped_disabled"
            log(
                "designer_author.critic_skip",
                mode="external",
                attempt=attempt_index,
                attempt_dir=str(attempt_dir),
                reason="max_critique_iters_disabled",
            )
            return None

        candidate = ctx.state.get("designer_author_direct_final_critic_candidate")
        candidate = candidate if isinstance(candidate, dict) else {}
        critic_count = len(ctx.state.get("critique_results") or [])
        if critic_count >= max_critique_iters:
            ctx.state["designer_author_direct_final_acceptance_path"] = "critic_skipped_max_iters"
            log(
                "designer_author.critic_skip",
                mode="external",
                attempt=attempt_index,
                attempt_dir=str(attempt_dir),
                reason="max_critique_iters_reached",
                critic_count=critic_count,
                max_critique_iters=max_critique_iters,
            )
            return None

        design_spec = candidate.get("design_spec")
        if design_spec is None:
            return _critic_infrastructure_feedback(
                attempt_index=attempt_index,
                attempt_dir=attempt_dir,
                message="critic gate could not find the validated DesignSpec from deterministic preflight",
                candidate=candidate,
            )

        canvas = _direct_canvas(ctx)
        preview_path = attempt_dir / f"critic_preview_{phase}.png"
        preview_warnings: list[str] = []
        try:
            preview = _render_direct_preview(
                html_path=poster_path,
                preview_path=preview_path,
                canvas=canvas,
                ctx=ctx,
            )
            preview_warnings.extend(list(getattr(preview, "warnings", []) or []))
        except Exception as exc:  # noqa: BLE001
            preview_warnings.append(f"direct_preview_render_error:{type(exc).__name__}: {exc}")
        if not preview_path.exists():
            candidate_preview = _candidate_preview_path(ctx, candidate)
            if candidate_preview and candidate_preview.exists():
                shutil.copy2(candidate_preview, preview_path)
                preview_warnings.append("critic_reused_candidate_preview")
        if not preview_path.exists():
            return _critic_infrastructure_feedback(
                attempt_index=attempt_index,
                attempt_dir=attempt_dir,
                message="critic gate could not render or locate a preview image for the deterministic-valid poster",
                candidate=candidate,
                preview_warnings=preview_warnings,
            )

        clean_feedback = _critic_clean_design_feedback(ctx, attempt_index=attempt_index)
        composite_payload = {
            "artifact_type": "poster",
            "iteration": _safe_int(ctx.state.get("composite_iter"), default=0) + 1,
            "render_mode": "designer_author_critic_candidate",
            "designer_author_critic_candidate": True,
            "designer_author_attempt": attempt_index,
            "designer_author_attempt_dir": str(attempt_dir),
            "preview_relative_path": _run_relative_path(ctx, preview_path),
            "html_relative_path": _run_relative_path(ctx, poster_path),
            "preview_sha256": sha256_file(preview_path),
            "html_sha256": sha256_file(poster_path),
            "canvas": canvas,
            "frame_render_warnings": preview_warnings,
            "design_feedback": clean_feedback,
        }

        scratch_state_keys = (
            "design_spec",
            "composition",
            "last_design_feedback",
            "last_composite_payload",
            "last_critique_preview_sha256",
            "last_critique_composite_iteration",
            "last_critique_spec_revision",
        )
        state_snapshot = {
            key: copy.deepcopy(ctx.state.get(key))
            for key in scratch_state_keys
            if key in ctx.state
        }
        missing_keys = {
            key for key in scratch_state_keys
            if key not in ctx.state
        }
        try:
            ctx.state["design_spec"] = design_spec
            ctx.state["composition"] = CompositionArtifacts(
                html_path=str(poster_path),
                preview_path=str(preview_path),
                layer_manifest=[{
                    "layer_id": "designer_author_raw_html",
                    "kind": "html",
                    "name": "External designer-author poster",
                }],
            )
            ctx.state["last_design_feedback"] = clean_feedback
            ctx.state["last_composite_payload"] = composite_payload
            from ..tools.critique_tool import critique

            result = critique({"preview_path": str(preview_path)}, ctx=ctx)
        finally:
            for key in missing_keys:
                ctx.state.pop(key, None)
            for key, value in state_snapshot.items():
                ctx.state[key] = value

        if result.status != "ok":
            return _critic_infrastructure_feedback(
                attempt_index=attempt_index,
                attempt_dir=attempt_dir,
                message=result.error_message or "critic gate failed before producing a critique report",
                candidate=candidate,
                preview_path=preview_path,
                preview_warnings=preview_warnings,
                result_payload=result.payload or {},
            )

        report = dict(result.payload or {})
        verdict = str(report.get("verdict") or "").lower()
        if verdict == "pass":
            ctx.state["designer_author_direct_final_acceptance_path"] = "critic_pass"
            log(
                "designer_author.critic_pass",
                mode="external",
                attempt=attempt_index,
                attempt_dir=str(attempt_dir),
                score=report.get("score"),
                preview_relative_path=_run_relative_path(ctx, preview_path),
            )
            return None
        if _critic_report_is_infrastructure_failure(report):
            return _critic_infrastructure_feedback(
                attempt_index=attempt_index,
                attempt_dir=attempt_dir,
                message=str(report.get("summary") or "critic infrastructure failure"),
                candidate=candidate,
                preview_path=preview_path,
                preview_warnings=preview_warnings,
                result_payload=report,
            )
        feedback = _critic_report_repair_feedback(
            report=report,
            attempt_index=attempt_index,
            attempt_dir=attempt_dir,
            candidate=candidate,
            preview_path=preview_path,
            preview_warnings=preview_warnings,
        )
        log(
            "designer_author.critic_block",
            mode="external",
            attempt=attempt_index,
            attempt_dir=str(attempt_dir),
            verdict=verdict,
            score=report.get("score"),
            issue_count=len(report.get("issues") or []),
        )
        return feedback

    def _merge_attempt_level_critic_review(
        self,
        ctx: ToolContext,
        *,
        attempt_index: int,
        attempt_dir: Path,
        poster_path: Path,
        validation_feedback: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach an advisory critic review to deterministic repair feedback.

        Deterministic preflight remains the primary gate. The critic is allowed
        to help the next external attempt with visual/narrative repair context,
        but critic infrastructure failures or budget exhaustion must not replace
        the concrete validator issue that already explains why this attempt
        cannot be promoted.
        """

        critic_feedback = self._attempt_level_critic_feedback(
            ctx,
            attempt_index=attempt_index,
            attempt_dir=attempt_dir,
            poster_path=poster_path,
            validation_feedback=validation_feedback,
        )
        if critic_feedback is None:
            return validation_feedback
        return _merge_attempt_level_critic_feedback(validation_feedback, critic_feedback)

    def _attempt_level_critic_feedback(
        self,
        ctx: ToolContext,
        *,
        attempt_index: int,
        attempt_dir: Path,
        poster_path: Path,
        validation_feedback: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Run CriticAgent as an attempt-level advisor after preflight failed."""

        max_critique_iters = _safe_int(getattr(ctx.settings, "max_critique_iters", 0), default=0)
        if max_critique_iters <= 0:
            return None
        critic_count = len(ctx.state.get("critique_results") or [])
        if critic_count >= max_critique_iters:
            log(
                "designer_author.attempt_critic_skip",
                mode="external",
                attempt=attempt_index,
                attempt_dir=str(attempt_dir),
                reason="max_critique_iters_reached",
                critic_count=critic_count,
                max_critique_iters=max_critique_iters,
            )
            return None

        canvas = _direct_canvas(ctx)
        design_spec = _attempt_critic_design_spec(ctx, canvas)
        preview_path = attempt_dir / f"critic_preview_attempt_{attempt_index:02d}.png"
        preview_warnings: list[str] = []
        try:
            preview = _render_direct_preview(
                html_path=poster_path,
                preview_path=preview_path,
                canvas=canvas,
                ctx=ctx,
            )
            preview_warnings.extend(list(getattr(preview, "warnings", []) or []))
        except Exception as exc:  # noqa: BLE001
            preview_warnings.append(f"attempt_critic_preview_render_error:{type(exc).__name__}: {exc}")
        if not preview_path.exists():
            log(
                "designer_author.attempt_critic_skip",
                mode="external",
                attempt=attempt_index,
                attempt_dir=str(attempt_dir),
                reason="preview_unavailable",
                preview_warnings=preview_warnings[:4],
            )
            return None

        design_feedback = _attempt_critic_design_feedback(
            ctx,
            validation_feedback=validation_feedback,
            attempt_index=attempt_index,
        )
        composite_payload = {
            "artifact_type": "poster",
            "iteration": _safe_int(ctx.state.get("composite_iter"), default=0) + 1,
            "render_mode": "designer_author_attempt_critic_candidate",
            "designer_author_attempt_critic_candidate": True,
            "designer_author_attempt": attempt_index,
            "designer_author_attempt_dir": str(attempt_dir),
            "preview_relative_path": _run_relative_path(ctx, preview_path),
            "html_relative_path": _run_relative_path(ctx, poster_path),
            "preview_sha256": sha256_file(preview_path),
            "html_sha256": sha256_file(poster_path),
            "canvas": canvas,
            "frame_render_warnings": preview_warnings,
            "design_feedback": design_feedback,
        }
        scratch_state_keys = (
            "design_spec",
            "composition",
            "last_design_feedback",
            "last_composite_payload",
            "last_critique_preview_sha256",
            "last_critique_composite_iteration",
            "last_critique_spec_revision",
        )
        state_snapshot = {
            key: copy.deepcopy(ctx.state.get(key))
            for key in scratch_state_keys
            if key in ctx.state
        }
        missing_keys = {
            key for key in scratch_state_keys
            if key not in ctx.state
        }
        try:
            ctx.state["design_spec"] = design_spec
            ctx.state["composition"] = CompositionArtifacts(
                html_path=str(poster_path),
                preview_path=str(preview_path),
                layer_manifest=[{
                    "layer_id": "designer_author_raw_html",
                    "kind": "html",
                    "name": "External designer-author poster attempt",
                }],
            )
            ctx.state["last_design_feedback"] = design_feedback
            ctx.state["last_composite_payload"] = composite_payload
            from ..tools.critique_tool import critique

            result = critique({"preview_path": str(preview_path)}, ctx=ctx)
        finally:
            for key in missing_keys:
                ctx.state.pop(key, None)
            for key, value in state_snapshot.items():
                ctx.state[key] = value

        if result.status != "ok":
            log(
                "designer_author.attempt_critic_skip",
                mode="external",
                attempt=attempt_index,
                attempt_dir=str(attempt_dir),
                reason="critic_unavailable",
                error=result.error_message,
            )
            return None

        report = dict(result.payload or {})
        verdict = str(report.get("verdict") or "").lower()
        if verdict == "pass":
            log(
                "designer_author.attempt_critic_pass",
                mode="external",
                attempt=attempt_index,
                attempt_dir=str(attempt_dir),
                score=report.get("score"),
                preview_relative_path=_run_relative_path(ctx, preview_path),
            )
            return None
        if _critic_report_is_infrastructure_failure(report):
            log(
                "designer_author.attempt_critic_skip",
                mode="external",
                attempt=attempt_index,
                attempt_dir=str(attempt_dir),
                reason="critic_report_infrastructure_failure",
                verdict=verdict,
                score=report.get("score"),
            )
            return None
        feedback = _critic_report_repair_feedback(
            report=report,
            attempt_index=attempt_index,
            attempt_dir=attempt_dir,
            candidate=_candidate_from_validation_feedback(validation_feedback),
            preview_path=preview_path,
            preview_warnings=preview_warnings,
            deterministic_valid=False,
        )
        log(
            "designer_author.attempt_critic_feedback",
            mode="external",
            attempt=attempt_index,
            attempt_dir=str(attempt_dir),
            verdict=verdict,
            score=report.get("score"),
            issue_count=len(report.get("issues") or []),
        )
        return feedback

    def _promote_direct_final(
        self,
        ctx: ToolContext,
        *,
        attempt_index: int,
        attempt_dir: Path,
        poster_path: Path,
        poster_sha256: str,
        acceptance_path: str = "clean_preflight",
        quality_status: str = "ready",
        quality_diagnostics: list[str] | None = None,
        selection_owned: bool = False,
        _normal_lease_owned: bool = False,
        _requested_run_dir: Path | None = None,
        _promotion_candidate_id: str | None = None,
    ) -> None:
        """Promote the external author's standalone HTML without html_first import."""
        if not selection_owned and not _normal_lease_owned:
            original_run_dir = Path(ctx.run_dir)
            with normal_promotion_lease(
                run_dir=original_run_dir,
                candidate_id=f"untracked-poster-attempt-{attempt_index}",
                expected_run_identity=ctx.run_directory_identity,
            ) as leased_run_dir:
                with leased_promotion_tool_context(ctx, leased_run_dir):
                    return self._promote_direct_final(
                        ctx,
                        attempt_index=attempt_index,
                        attempt_dir=(
                            leased_run_dir
                            / attempt_dir.relative_to(original_run_dir)
                        ),
                        poster_path=(
                            leased_run_dir
                            / poster_path.relative_to(original_run_dir)
                        ),
                        poster_sha256=poster_sha256,
                        acceptance_path=acceptance_path,
                        quality_status=quality_status,
                        quality_diagnostics=quality_diagnostics,
                        selection_owned=False,
                        _normal_lease_owned=True,
                        _requested_run_dir=original_run_dir,
                        _promotion_candidate_id=_promotion_candidate_id,
                    )
        def checkpoint(phase: str) -> None:
            context_cancellation_checkpoint(ctx, phase)
            assert_promotion_run_unchanged()
        checkpoint("external_poster.promotion.start")
        promotion_candidate_id = _promotion_candidate_id
        if promotion_candidate_id is None:
            try:
                promotion_candidate_id = load_attempt_candidate(
                    ctx.run_dir,
                    attempt_index,
                ).candidate_id
            except ValueError:
                promotion_candidate_id = f"untracked-poster-attempt-{attempt_index}"
        assert_promotion_allowed(
            run_dir=ctx.run_dir,
            candidate_id=promotion_candidate_id,
        )
        checkpoint("external_poster.promotion.before_typesetting")
        existing_math_record = ctx.state.get("designer_author_math_typesetting")
        math_record = ensure_poster_katex_document(
            poster_path,
            ctx.settings.repo_root,
            root_selector=".paper-poster",
        )
        if math_record.get("detected"):
            if math_record.get("applied") or not isinstance(existing_math_record, dict):
                ctx.state["designer_author_math_typesetting"] = math_record
            if math_record.get("applied"):
                poster_sha256 = sha256_file(poster_path)
                log(
                    "designer_author.math_typesetting",
                    mode="external",
                    attempt=attempt_index,
                    attempt_dir=str(attempt_dir),
                    applied=True,
                )
        checkpoint("external_poster.promotion.after_typesetting")

        iter_num = ctx.next_composite_iter()
        iter_dir = ctx.run_dir / "composites" / f"iter_{iter_num:02d}"
        final_dir = ctx.run_dir / "final"
        checkpoint("external_poster.promotion.before_iter_directory")
        iter_dir.mkdir(parents=True, exist_ok=True)
        checkpoint("external_poster.promotion.after_iter_directory")

        iter_html = iter_dir / "poster.html"
        final_html = final_dir / "poster.html"
        shutil.copy2(poster_path, iter_html)
        for dirname in ("layers", "assets"):
            _copytree_replace(attempt_dir / dirname, iter_dir / dirname)
        checkpoint("external_poster.promotion.after_iter_copy")
        _rewrite_run_local_asset_refs(
            iter_html,
            ctx.run_dir,
            additional_run_dirs=(
                (_requested_run_dir,) if _requested_run_dir is not None else ()
            ),
        )
        layer_asset_resolution = _resolve_layer_asset_placeholders(iter_html)
        iter_inline_assets = _inline_local_assets(iter_html)
        checkpoint("external_poster.promotion.after_asset_inline")

        canvas = _direct_canvas(ctx)
        header_guard = _maybe_repair_collapsed_poster_header(iter_html, canvas)
        checkpoint("external_poster.promotion.after_header_guard")
        preview_path = iter_dir / "preview.png"
        preview = _render_direct_preview(
            html_path=iter_html,
            preview_path=preview_path,
            canvas=canvas,
            ctx=ctx,
        )
        checkpoint("external_poster.promotion.after_preview")

        manifest = {
            "artifact_type": "poster",
            "render_mode": "designer_author_raw_html",
            "acceptance_path": acceptance_path,
            "source": "external_designer_author",
            "attempt": attempt_index,
            "attempt_dir": str(attempt_dir),
            "source_poster": str(poster_path),
            "source_html_sha256": poster_sha256 or sha256_file(poster_path),
            "html_sha256": sha256_file(iter_html),
            "quality_status": (
                quality_status
                if quality_status in {"ready", "ready_with_warnings"}
                else "ready"
            ),
            "quality_diagnostics": list(quality_diagnostics or []),
            "standalone_assets": iter_inline_assets,
            "layer_asset_resolution": layer_asset_resolution,
            "canvas": canvas,
            "preview": {
                "path": str(preview_path) if preview_path.exists() else "",
                "backend": preview.backend,
                "warnings": preview.warnings,
                "scale": preview.scale,
                "width_px": preview.width_px,
                "height_px": preview.height_px,
            },
        }
        typesetting_record = ctx.state.get("designer_author_typesetting")
        if isinstance(typesetting_record, dict):
            manifest["typesetting"] = typesetting_record
        math_record = ctx.state.get("designer_author_math_typesetting")
        if isinstance(math_record, dict) and math_record.get("detected"):
            manifest["math_typesetting"] = math_record
        if header_guard:
            manifest["header_collapse_guard"] = header_guard
        if _html_contains_layer_placeholders(iter_html):
            manifest["unresolved_layer_placeholders"] = True
            log(
                "designer_author.layer_placeholder_unresolved",
                mode="external",
                attempt=attempt_index,
                attempt_dir=str(attempt_dir),
                html=str(iter_html),
                **layer_asset_resolution,
            )
        checkpoint("external_poster.promotion.before_iter_manifest")
        atomic_write_json(iter_dir / "designer_author_direct_manifest.json", manifest)
        checkpoint("external_poster.promotion.before_final_staging")
        staging_dir = Path(
            tempfile.mkdtemp(prefix=".poster-final-staging-", dir=ctx.run_dir)
        )
        try:
            checkpoint("external_poster.promotion.after_final_staging")
            shutil.copytree(iter_dir, staging_dir, dirs_exist_ok=True)
            checkpoint("external_poster.promotion.after_final_copy")
            atomic_write_json(
                staging_dir / "designer_author_direct_manifest.json",
                manifest,
            )
            checkpoint("external_poster.promotion.after_final_manifest")
            checkpoint("external_poster.promotion.before_final_publish")
            _publish_poster_final(staging_dir, final_dir, checkpoint)
        except BaseException:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise
        final_preview = final_dir / "preview.png"

        checkpoint("external_poster.promotion.before_state")
        ctx.state["composition"] = CompositionArtifacts(
            html_path=str(iter_html),
            preview_path=str(preview_path) if preview_path.exists() else None,
            layer_manifest=[{
                "layer_id": "designer_author_raw_html",
                "kind": "html",
                "name": "External designer-author poster",
            }],
        )
        payload: dict[str, Any] = {
            "artifact_type": "poster",
            "iteration": iter_num,
            "render_mode": "designer_author_raw_html",
            "acceptance_path": acceptance_path,
            "designer_author_direct_final": True,
            "designer_author_attempt": attempt_index,
            "designer_author_attempt_dir": str(attempt_dir),
            "preview_relative_path": f"composites/iter_{iter_num:02d}/preview.png" if preview_path.exists() else None,
            "html_relative_path": f"composites/iter_{iter_num:02d}/poster.html",
            "html_sha256": sha256_file(iter_html),
            "preview_sha256": sha256_file(preview_path) if preview_path.exists() else None,
            "canvas": canvas,
            "frame_render_backend": preview.backend,
            "frame_render_warnings": preview.warnings,
        }
        ctx.state["last_composite_payload"] = payload
        ctx.state["designer_author_direct_final"] = manifest
        ctx.state["finalized"] = True
        ctx.state["finalize_notes"] = "External designer author standalone HTML promoted directly."
        ctx.state["designer_author_result"] = {
            "status": "ok",
            "mode": "direct_final",
            "acceptance_path": acceptance_path,
            "attempt": attempt_index,
            "attempt_dir": str(attempt_dir),
            "poster_sha256": poster_sha256 or sha256_file(poster_path),
            "final_html": str(final_html),
            "final_preview": str(final_preview) if final_preview.exists() else "",
            "standalone_assets": iter_inline_assets,
            "quality_status": manifest["quality_status"],
            "quality_diagnostics": manifest["quality_diagnostics"],
        }
        if isinstance(typesetting_record, dict):
            ctx.state["designer_author_result"]["typesetting"] = typesetting_record
        log(
            "designer_author.direct_final",
            mode="external",
            attempt=attempt_index,
            attempt_dir=str(attempt_dir),
            html=str(final_html),
            preview=str(final_preview) if final_preview.exists() else "",
            preview_backend=preview.backend,
            preview_warnings=preview.warnings,
        )

    def _try_promote_best_candidate_fallback(
        self,
        ctx: ToolContext,
        *,
        attempt_index: int,
        attempt_dir: Path,
        last_feedback: dict[str, Any] | None,
        source_reason: str,
        source_message: str,
    ) -> bool:
        candidates = _best_candidate_fallback_candidates(ctx, last_feedback=last_feedback)
        rejected: list[dict[str, Any]] = []
        for candidate in candidates:
            acceptance = _best_candidate_fallback_acceptance(ctx, candidate, last_feedback)
            if not acceptance.get("accepted"):
                rejected.append({
                    "candidate_id": candidate.get("candidate_id"),
                    "candidate_score": candidate.get("candidate_score"),
                    "reason": acceptance.get("reason") or "rejected",
                    "details": acceptance.get("details") or {},
                })
                continue
            self._promote_html_first_candidate_fallback(
                ctx,
                attempt_index=attempt_index,
                attempt_dir=attempt_dir,
                candidate=candidate,
                acceptance=acceptance,
                rejected_candidates=rejected,
                source_reason=source_reason,
                source_message=source_message,
                last_feedback=last_feedback,
            )
            return True
        if rejected:
            ctx.state["designer_author_best_candidate_fallback_rejected"] = rejected[:8]
            log(
                "designer_author.best_candidate_fallback_rejected",
                mode="external",
                attempt=attempt_index,
                attempt_dir=str(attempt_dir),
                rejected_count=len(rejected),
                first_rejection=rejected[0],
                source_reason=source_reason,
            )
        return False

    def _try_promote_delivery_fallbacks(
        self,
        ctx: ToolContext,
        *,
        attempt_index: int,
        attempt_dir: Path,
        last_feedback: dict[str, Any] | None,
        source_reason: str,
        source_message: str,
    ) -> bool:
        if self._try_promote_best_candidate_fallback(
            ctx,
            attempt_index=attempt_index,
            attempt_dir=attempt_dir,
            last_feedback=last_feedback,
            source_reason=source_reason,
            source_message=source_message,
        ):
            return True
        return self._try_promote_best_available_artifact_fallback(
            ctx,
            attempt_index=attempt_index,
            attempt_dir=attempt_dir,
            last_feedback=last_feedback,
            source_reason=source_reason,
            source_message=source_message,
        )

    def _try_promote_best_available_artifact_fallback(
        self,
        ctx: ToolContext,
        *,
        attempt_index: int,
        attempt_dir: Path,
        last_feedback: dict[str, Any] | None,
        source_reason: str,
        source_message: str,
    ) -> bool:
        candidates = _best_available_artifact_fallback_candidates(ctx, last_feedback=last_feedback)
        rejected: list[dict[str, Any]] = []
        for candidate in candidates:
            candidate_dir = Path(str(candidate.get("_candidate_dir_abs") or ""))
            poster_path = Path(str(candidate.get("_measure_html_abs") or ""))
            candidate_attempt = (
                _fallback_candidate_numeric_suffix(candidate)
                or attempt_index
            )
            try:
                fresh_feedback = self._direct_final_validation_feedback(
                    ctx,
                    attempt_index=candidate_attempt,
                    attempt_dir=candidate_dir,
                    poster_path=poster_path,
                )
            except Exception as exc:  # noqa: BLE001
                rejected.append({
                    "candidate_id": candidate.get("candidate_id"),
                    "candidate_score": candidate.get("candidate_score"),
                    "reason": "poster_preflight_failed",
                    "details": {
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                })
                continue
            fresh_payload: dict[str, Any] = {}
            if isinstance(fresh_feedback, dict):
                payload = fresh_feedback.get("payload")
                summary = fresh_feedback.get("summary")
                if isinstance(payload, dict) and payload:
                    fresh_payload = copy.deepcopy(payload)
                elif isinstance(summary, dict):
                    fresh_payload = copy.deepcopy(summary)
            validated_candidate = {
                **candidate,
                "payload": fresh_payload,
            }
            acceptance = _best_available_artifact_fallback_acceptance(
                ctx,
                validated_candidate,
                None,
            )
            if not acceptance.get("accepted"):
                rejected.append({
                    "candidate_id": candidate.get("candidate_id"),
                    "candidate_score": candidate.get("candidate_score"),
                    "reason": acceptance.get("reason") or "rejected",
                    "details": acceptance.get("details") or {},
                })
                continue
            usable_rejected = list(ctx.state.get("designer_author_best_candidate_fallback_rejected") or [])
            self._promote_html_first_candidate_fallback(
                ctx,
                attempt_index=attempt_index,
                attempt_dir=attempt_dir,
                candidate=validated_candidate,
                acceptance=acceptance,
                rejected_candidates=rejected,
                source_reason=source_reason,
                source_message=source_message,
                last_feedback=fresh_feedback,
                fallback_kind="best_available_artifact",
                rejected_usable_candidates=usable_rejected,
            )
            if ctx.state.get("finalized"):
                return True
            rejected.append({
                "candidate_id": candidate.get("candidate_id"),
                "candidate_score": candidate.get("candidate_score"),
                "reason": "preview_render_failed",
                "details": {"candidate": candidate.get("candidate_id")},
            })
        if rejected:
            ctx.state["designer_author_best_available_artifact_fallback_rejected"] = rejected[:8]
            log(
                "designer_author.best_available_artifact_fallback_rejected",
                mode="external",
                attempt=attempt_index,
                attempt_dir=str(attempt_dir),
                rejected_count=len(rejected),
                first_rejection=rejected[0],
                source_reason=source_reason,
            )
        return False

    def _promote_html_first_candidate_fallback(
        self,
        ctx: ToolContext,
        *,
        attempt_index: int,
        attempt_dir: Path,
        candidate: dict[str, Any],
        acceptance: dict[str, Any],
        rejected_candidates: list[dict[str, Any]],
        source_reason: str,
        source_message: str,
        last_feedback: dict[str, Any] | None,
        fallback_kind: str = "best_candidate",
        rejected_usable_candidates: list[dict[str, Any]] | None = None,
        _normal_lease_owned: bool = False,
        _requested_run_dir: Path | None = None,
    ) -> bool:
        if not _normal_lease_owned:
            original_run_dir = Path(ctx.run_dir)
            promotion_candidate_id = str(
                candidate.get("candidate_id")
                or f"untracked-poster-attempt-{attempt_index}"
            )
            with normal_promotion_lease(
                run_dir=original_run_dir,
                candidate_id=promotion_candidate_id,
                expected_run_identity=ctx.run_directory_identity,
            ) as leased_run_dir:
                remapped_candidate = dict(candidate)
                for key in (
                    "_candidate_dir_abs",
                    "_measure_html_abs",
                    "_preview_png_abs",
                ):
                    raw_path = str(remapped_candidate.get(key) or "")
                    if raw_path:
                        remapped_candidate[key] = os.fspath(
                            leased_run_dir
                            / Path(raw_path).relative_to(original_run_dir)
                        )
                with leased_promotion_tool_context(ctx, leased_run_dir):
                    return self._promote_html_first_candidate_fallback(
                        ctx,
                        attempt_index=attempt_index,
                        attempt_dir=(
                            leased_run_dir
                            / attempt_dir.relative_to(original_run_dir)
                        ),
                        candidate=remapped_candidate,
                        acceptance=acceptance,
                        rejected_candidates=rejected_candidates,
                        source_reason=source_reason,
                        source_message=source_message,
                        last_feedback=last_feedback,
                        fallback_kind=fallback_kind,
                        rejected_usable_candidates=rejected_usable_candidates,
                        _normal_lease_owned=True,
                        _requested_run_dir=original_run_dir,
                    )
        def checkpoint(phase: str) -> None:
            context_cancellation_checkpoint(ctx, phase)
            assert_promotion_run_unchanged()
        checkpoint("external_poster.fallback.start")
        promotion_candidate_id = str(
            candidate.get("candidate_id")
            or f"untracked-poster-attempt-{attempt_index}"
        )
        candidate_dir = Path(str(candidate.get("_candidate_dir_abs") or ""))
        measure_html = Path(str(candidate.get("_measure_html_abs") or ""))
        preview_src = Path(str(candidate.get("_preview_png_abs") or ""))
        iter_num = ctx.next_composite_iter()
        iter_dir = ctx.run_dir / "composites" / f"iter_{iter_num:02d}"
        final_dir = ctx.run_dir / "final"
        iter_dir.mkdir(parents=True, exist_ok=True)
        checkpoint("external_poster.fallback.after_iter_directory")
        is_available_fallback = fallback_kind == "best_available_artifact"
        manifest_name = (
            "designer_author_best_available_artifact_fallback.json"
            if is_available_fallback
            else "designer_author_best_candidate_fallback.json"
        )
        render_mode = (
            "designer_author_best_available_artifact_fallback"
            if is_available_fallback
            else "designer_author_best_candidate_fallback"
        )
        result_mode = "best_available_artifact_fallback" if is_available_fallback else "best_candidate_fallback"
        event_name = (
            "designer_author.best_available_artifact_fallback_final"
            if is_available_fallback
            else "designer_author.best_candidate_fallback_final"
        )
        layer_name = (
            "External designer-author best available artifact fallback"
            if is_available_fallback
            else "External designer-author best candidate fallback"
        )

        iter_html = iter_dir / "poster.html"
        shutil.copy2(measure_html, iter_html)
        _rewrite_run_local_asset_refs(
            iter_html,
            ctx.run_dir,
            additional_run_dirs=(
                (_requested_run_dir,) if _requested_run_dir is not None else ()
            ),
        )
        math_record = ensure_poster_katex_document(
            iter_html,
            ctx.settings.repo_root,
            root_selector=".paper-poster",
        )

        for dirname in ("layers", "assets"):
            _copytree_replace(ctx.run_dir / dirname, iter_dir / dirname)
            _copytree_merge(candidate_dir / dirname, iter_dir / dirname)
        checkpoint("external_poster.fallback.after_asset_copy")

        canvas = _direct_canvas(ctx)
        before_typesetting_fit = _poster_root_scroll_metrics(iter_html, canvas)
        layer_asset_resolution = _resolve_layer_asset_placeholders(iter_html)
        iter_inline_assets = _inline_local_assets(iter_html)
        typesetting_record = apply_poster_typesetting_patch(
            iter_html,
            record_path=iter_dir / "auto_typesetting_fallback.json",
            backup_path=iter_dir / "poster_before_typesetting.html",
            phase=fallback_kind,
        )
        if typesetting_record.get("applied"):
            after_typesetting_fit = _poster_root_scroll_metrics(iter_html, canvas)
            if _typesetting_fit_regressed(before_typesetting_fit, after_typesetting_fit, canvas):
                reverted = revert_poster_typesetting_patch(typesetting_record)
                typesetting_record.update({
                    "reverted": reverted,
                    "revert_reason": "fallback_typesetting_worsened_root_overflow",
                    "before_fit": before_typesetting_fit,
                    "after_fit": after_typesetting_fit,
                })
                atomic_write_json(iter_dir / "auto_typesetting_fallback.json", typesetting_record)
                log(
                    "designer_author.typesetting_fallback_reverted",
                    mode="external",
                    attempt=attempt_index,
                    attempt_dir=str(attempt_dir),
                    fallback_kind=fallback_kind,
                    reverted=reverted,
                    before_fit=before_typesetting_fit,
                    after_fit=after_typesetting_fit,
                )
            else:
                typesetting_record.update({
                    "before_fit": before_typesetting_fit,
                    "after_fit": after_typesetting_fit,
                })
                atomic_write_json(iter_dir / "auto_typesetting_fallback.json", typesetting_record)
                log(
                    "designer_author.typesetting_fallback_applied",
                    mode="external",
                    attempt=attempt_index,
                    attempt_dir=str(attempt_dir),
                    fallback_kind=fallback_kind,
                    profile=typesetting_record.get("profile"),
                    before_fit=before_typesetting_fit,
                    after_fit=after_typesetting_fit,
                )
        header_guard = _maybe_repair_collapsed_poster_header(iter_html, canvas)
        checkpoint("external_poster.fallback.after_typesetting")

        preview_path = iter_dir / "preview.png"
        preview = _render_direct_preview(
            html_path=iter_html,
            preview_path=preview_path,
            canvas=canvas,
            ctx=ctx,
        )
        preview_warnings = list(getattr(preview, "warnings", []) or [])
        if not preview_path.exists() and preview_src.exists():
            shutil.copy2(preview_src, preview_path)
            preview_warnings.append("fallback_reused_candidate_preview")
        checkpoint("external_poster.fallback.after_preview")
        if is_available_fallback and not preview_path.exists():
            return False

        remaining_issue_ids = _fallback_remaining_issue_ids(candidate, last_feedback)
        fallback_manifest = {
            "artifact_type": "poster",
            "render_mode": render_mode,
            "source": "external_designer_author",
            "attempt": attempt_index,
            "attempt_dir": str(attempt_dir),
            "source_reason": source_reason,
            "source_message": source_message,
            "candidate": _fallback_candidate_summary(candidate),
            "acceptance": acceptance,
            "rejected_candidates": rejected_candidates[:8],
            "rejected_usable_candidates": (rejected_usable_candidates or [])[:8],
            "remaining_issue_ids": remaining_issue_ids,
            "remaining_hard_issue_ids": (
                list(acceptance.get("remaining_hard_issue_ids") or [])
                if is_available_fallback
                else []
            ),
            "last_feedback_summary": _feedback_summary_for_manifest(last_feedback),
            "standalone_assets": iter_inline_assets,
            "layer_asset_resolution": layer_asset_resolution,
            "canvas": canvas,
            "preview": {
                "path": str(preview_path) if preview_path.exists() else "",
                "backend": getattr(preview, "backend", "unknown"),
                "warnings": preview_warnings,
                "scale": getattr(preview, "scale", None),
                "width_px": getattr(preview, "width_px", None),
                "height_px": getattr(preview, "height_px", None),
            },
        }
        if typesetting_record:
            fallback_manifest["typesetting"] = typesetting_record
        if math_record.get("detected"):
            fallback_manifest["math_typesetting"] = math_record
        if header_guard:
            fallback_manifest["header_collapse_guard"] = header_guard
        if is_available_fallback:
            fallback_manifest["quality_status"] = str(
                acceptance.get("quality_status") or "ready_with_warnings"
            )
            fallback_manifest["quality_diagnostics"] = list(
                acceptance.get("quality_diagnostics") or []
            )
            fallback_manifest["best_available_artifact_fallback"] = True
        else:
            fallback_manifest["quality_status"] = "ready_with_warnings"
            fallback_manifest["quality_diagnostics"] = remaining_issue_ids
        fallback_manifest["html_sha256"] = sha256_file(iter_html)
        if _html_contains_layer_placeholders(iter_html):
            fallback_manifest["unresolved_layer_placeholders"] = True
            log(
                "designer_author.layer_placeholder_unresolved",
                mode="external",
                attempt=attempt_index,
                attempt_dir=str(attempt_dir),
                fallback_kind=fallback_kind,
                html=str(iter_html),
                **layer_asset_resolution,
            )
        atomic_write_json(iter_dir / manifest_name, fallback_manifest)
        checkpoint("external_poster.fallback.after_manifest")

        checkpoint("external_poster.fallback.before_final_staging")
        staging_dir = Path(
            tempfile.mkdtemp(prefix=".poster-final-staging-", dir=ctx.run_dir)
        )
        try:
            shutil.copytree(iter_dir, staging_dir, dirs_exist_ok=True)
            checkpoint("external_poster.fallback.after_final_staging")
            publish_artifact_directory(
                staging_dir,
                final_dir,
                artifact_name="poster",
                post_publish=lambda: checkpoint(
                    "external_poster.fallback.after_final_publish"
                ),
            )
        except BaseException:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

        final_html = final_dir / "poster.html"
        final_preview = final_dir / "preview.png"
        checkpoint("external_poster.fallback.before_state")

        ctx.state["composition"] = CompositionArtifacts(
            html_path=str(iter_html),
            preview_path=str(preview_path) if preview_path.exists() else str(final_preview) if final_preview.exists() else None,
            layer_manifest=[{
                "layer_id": render_mode,
                "kind": "html",
                "name": layer_name,
            }],
        )
        payload: dict[str, Any] = {
            "artifact_type": "poster",
            "iteration": iter_num,
            "render_mode": render_mode,
            "designer_author_best_candidate_fallback": not is_available_fallback,
            "designer_author_best_available_artifact_fallback": is_available_fallback,
            "designer_author_attempt": attempt_index,
            "designer_author_attempt_dir": str(attempt_dir),
            "candidate_id": candidate.get("candidate_id"),
            "candidate_score": candidate.get("candidate_score"),
            "preview_relative_path": f"composites/iter_{iter_num:02d}/preview.png" if preview_path.exists() else None,
            "html_relative_path": f"composites/iter_{iter_num:02d}/poster.html",
            "html_sha256": sha256_file(iter_html),
            "preview_sha256": sha256_file(preview_path) if preview_path.exists() else None,
            "canvas": canvas,
            "frame_render_backend": getattr(preview, "backend", "unknown"),
            "frame_render_warnings": preview_warnings,
        }
        ctx.state["last_composite_payload"] = payload
        if is_available_fallback:
            ctx.state["designer_author_best_available_artifact_fallback"] = fallback_manifest
        else:
            ctx.state["designer_author_best_candidate_fallback"] = fallback_manifest
        ctx.state["finalized"] = True
        ctx.state["finalize_notes"] = (
            "External designer author finalized the best safe artifact with quality warnings."
            if is_available_fallback
            else "External designer author finalized best usable candidate with remaining near-miss diagnostics."
        )
        ctx.state["designer_author_result"] = {
            "status": "ok",
            "mode": result_mode,
            "fallback_finalized": True,
            "best_available_artifact_fallback": is_available_fallback,
            "attempt": attempt_index,
            "attempt_dir": str(attempt_dir),
            "candidate_id": candidate.get("candidate_id"),
            "candidate_score": candidate.get("candidate_score"),
            "final_html": str(final_html),
            "final_preview": str(final_preview) if final_preview.exists() else "",
            "remaining_issue_ids": remaining_issue_ids,
            "remaining_hard_issue_ids": [],
            "quality_status": fallback_manifest["quality_status"],
            "quality_diagnostics": fallback_manifest["quality_diagnostics"],
            "rejected_usable_candidates": (rejected_usable_candidates or [])[:8],
            "acceptance": acceptance,
            "standalone_assets": iter_inline_assets,
        }
        log(
            event_name,
            mode="external",
            attempt=attempt_index,
            attempt_dir=str(attempt_dir),
            candidate_id=candidate.get("candidate_id"),
            candidate_score=candidate.get("candidate_score"),
            source_issue_id=source_reason,
            acceptance_reason=acceptance.get("reason"),
            remaining_issue_ids=remaining_issue_ids,
            remaining_hard_issue_ids=fallback_manifest["remaining_hard_issue_ids"],
            html=str(final_html),
            preview=str(final_preview) if final_preview.exists() else "",
        )
        return True

    @property
    def token_totals(self) -> tuple[int, int]:
        return self._total_in, self._total_out

    @property
    def cache_totals(self) -> tuple[int, int]:
        return self._total_cache_read, self._total_cache_create

    def _next_attempt_dir(self, ctx: ToolContext) -> Path:
        attempt = int(ctx.state.get("designer_author_attempts") or 0) + 1
        ctx.state["designer_author_attempts"] = attempt
        attempt_dir = ctx.run_dir / "designer_author" / f"attempt_{attempt:02d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        return attempt_dir

    def _ensure_ingested(self, ctx: ToolContext) -> bool:
        if _has_author_context(ctx):
            return True
        switch_result = invoke_designer_tool("switch_artifact_type", {"type": "poster"}, ctx)
        if switch_result.status == "error":
            self._fail_from_tool(ctx, "switch_artifact_type", switch_result, ctx.run_dir / "designer_author")
            return False

        attachments = [str(path) for path in (ctx.state.get("attachments") or [])]
        if not attachments and not ctx.state.get("reuse_ingest_run"):
            self._fail(
                ctx,
                "designer_author_missing_ingest_input",
                "external designer author only supports paper posters with attachments or --reuse-ingest-run",
                ctx.run_dir / "designer_author",
            )
            return False
        ingest_result = invoke_designer_tool("ingest_document", {"file_paths": attachments}, ctx)
        if ingest_result.status == "error":
            self._fail_from_tool(ctx, "ingest_document", ingest_result, ctx.run_dir / "designer_author")
            return False
        if not _has_author_context(ctx):
            self._fail(
                ctx,
                "designer_author_missing_ingest_context",
                "ingest_document completed but did not produce poster_content_brief/poster_plan_contract/paper_memory context",
                ctx.run_dir / "designer_author",
            )
            return False
        return True

    def _stage_inputs(
        self,
        ctx: ToolContext,
        *,
        brief: str,
        attempt_dir: Path,
        repair_feedback: dict[str, Any] | None = None,
        previous_attempt_dir: Path | None = None,
    ) -> bool:
        copied: list[str] = []
        missing_required: list[str] = []
        missing_optional: list[str] = []

        for name in _REQUIRED_INPUT_FILES:
            if _copy_run_file(ctx.run_dir / name, attempt_dir / name):
                copied.append(name)
            else:
                missing_required.append(name)
        for name in _OPTIONAL_INPUT_FILES:
            if _copy_run_file(ctx.run_dir / name, attempt_dir / name):
                copied.append(name)
            else:
                missing_optional.append(name)
        for name in _INPUT_DIRS:
            src = ctx.run_dir / name
            dst = attempt_dir / name
            if src.exists() and src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
                copied.append(f"{name}/")
            else:
                missing_optional.append(f"{name}/")
        for name in _OPTIONAL_INPUT_DIRS:
            src = ctx.run_dir / name
            dst = attempt_dir / name
            if src.exists() and src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
                copied.append(f"{name}/")
            else:
                missing_optional.append(f"{name}/")

        reference_files = _stage_reference_style_inputs(ctx, attempt_dir)
        copied.extend(reference_files)
        runtime_skills = _stage_runtime_skills(
            ctx,
            attempt_dir,
            stage="repair" if repair_feedback is not None else "plan",
        )
        copied.extend(runtime_skills["files"])

        color_context = _synchronize_staged_color_system(ctx, attempt_dir, brief)
        color_system = (
            color_context.get("recommended_color_system")
            if isinstance(color_context.get("recommended_color_system"), dict)
            else color_context.get("color_system")
        )

        if _write_author_quick_brief(ctx, attempt_dir, brief):
            copied.append("author_quick_brief.md")

        repair_inputs: list[str] = []
        if repair_feedback is not None:
            if previous_attempt_dir is not None:
                if _copy_run_file(previous_attempt_dir / "poster.html", attempt_dir / "previous_poster.html"):
                    repair_inputs.append("previous_poster.html")
                if _copy_run_file(previous_attempt_dir / "panel_content_plan.json", attempt_dir / "previous_panel_content_plan.json"):
                    repair_inputs.append("previous_panel_content_plan.json")
            repair_inputs.extend(_copy_candidate_feedback_files(ctx, attempt_dir, repair_feedback))
            visual_packet_inputs = _stage_attempt_visual_repair_packet(attempt_dir, repair_feedback)
            repair_inputs.extend(name for name in visual_packet_inputs if name not in repair_inputs)
            repair_context = _build_repair_context(ctx, attempt_dir, repair_feedback)
            if repair_context:
                summary = repair_feedback.setdefault("summary", {})
                if isinstance(summary, dict):
                    summary["repair_context"] = repair_context
                    if isinstance(repair_context.get("global_overflow_repair_plan"), dict):
                        summary["global_overflow_repair_plan"] = repair_context["global_overflow_repair_plan"]
                payload = repair_feedback.setdefault("payload", {})
                if isinstance(payload, dict):
                    payload["repair_context"] = repair_context
                    if isinstance(repair_context.get("global_overflow_repair_plan"), dict):
                        payload["global_overflow_repair_plan"] = repair_context["global_overflow_repair_plan"]
                atomic_write_json(attempt_dir / "repair_context.json", repair_context)
                repair_inputs.append("repair_context.json")
            atomic_write_json(attempt_dir / "validation_feedback.json", repair_feedback)
            atomic_write_json(attempt_dir / "previous_validation_error.json", repair_feedback)
            repair_inputs.extend(["validation_feedback.json", "previous_validation_error.json"])

        author_visible_brief = _author_visible_brief(ctx, brief)
        manifest = {
            "version": 1,
            "run_id": ctx.run_id,
            "brief": author_visible_brief,
            "raw_user_brief": ctx.state.get("raw_user_brief"),
            "canvas_plan": ctx.state.get("canvas_plan"),
            "color_system": color_system,
            "recommended_color_system": color_system,
            "required_color_system": color_context.get("required_color_system") or {},
            "color_system_options": color_context.get("color_system_options") or [],
            "institution_color_signals": color_context.get("institution_color_signals") or {},
            "aesthetic_contract": color_context.get("aesthetic_contract") or {},
            "attachments": ctx.state.get("attachments") or [],
            "reference_style_contract": (
                color_context.get("reference_style_contract")
                or ctx.state.get("reference_style_contract")
                or {}
            ),
            "style_reference_id": _reference_style_id(ctx),
            "reference_style_files": reference_files,
            "reference_images": [
                name for name in reference_files if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
            ],
            "runtime_skills": runtime_skills["catalog"],
            "copied": copied,
            "repair_inputs": repair_inputs,
            "must_read_first": _author_must_read_first(repair_inputs, copied),
            "must_read_first_images": _author_must_read_first_images(attempt_dir),
            "missing_required": missing_required,
            "missing_optional": missing_optional,
            "output_contract": {
                "poster_html": "poster.html",
                "done_marker": "designer_author_done.json",
            },
            "forbidden_outputs": ["panel_content_plan.json"],
        }
        atomic_write_json(attempt_dir / "author_input_manifest.json", manifest)
        if missing_required:
            self._fail(
                ctx,
                "designer_author_missing_required_inputs",
                f"external designer author staging is missing required inputs: {', '.join(missing_required)}",
                attempt_dir,
            )
            return False
        return True

    def _build_prompt(
        self,
        ctx: ToolContext,
        *,
        brief: str,
        attempt_dir: Path,
        repair_feedback: dict[str, Any] | None = None,
        attempt_index: int = 1,
        max_attempts: int = 1,
    ) -> str:
        model_hint = str(getattr(self.settings, "designer_author_model", "") or "").strip()
        model_line = f"\nModel hint from AutoDesign: {model_hint}\n" if model_hint else ""
        repair_block = _format_repair_prompt_block(
            repair_feedback,
            attempt_index=attempt_index,
            max_attempts=max_attempts,
            reference_style=_reference_style_contract(ctx),
        )
        content = _state_or_attempt_json(ctx, "poster_content_brief", attempt_dir / "poster_content_brief.json")
        contract = _state_or_attempt_json(ctx, "poster_plan_contract", attempt_dir / "poster_plan_contract.json")
        recommended_color = _active_color_system(ctx, content, contract, brief)
        required_color = _required_color_system(ctx, content, contract, brief)
        structured_selection = bool(_structured_selected_color_system(ctx))
        palette_heading = (
            "Required palette"
            if structured_selection
            else "Palette options and recommendation"
        )
        color_options = _color_system_options(content, contract, raw_brief=str(ctx.state.get("raw_user_brief") or brief or ""))
        institution_signals = _institution_color_signals_from_sources(content, contract)
        color_block = _format_color_system_prompt_block(
            recommended_color,
            options=color_options,
            required=required_color,
            institution_signals=institution_signals,
            reference_style=_reference_style_contract(ctx),
            structured_selection=structured_selection,
        )
        visual_treatment_block = "\n".join(_aesthetic_contract_lines(content, contract))
        reference_style_block = _reference_style_prompt_block(ctx)
        identity_layout_block = "\n".join(_identity_header_authoring_contract(ctx))
        typography_authoring_contract = _typography_authoring_contract(ctx)
        table_authoring_contract = _table_authoring_contract(ctx)
        formula_authoring_contract = _formula_authoring_contract(ctx)
        reference_layout_authoring_contract = _reference_layout_authoring_contract(ctx)
        required_source_ids = [
            str(value or "").strip()
            for value in (contract.get("required_source_visual_ids") or [])
            if str(value or "").strip()
        ]
        required_source_instruction = (
            "- REQUIRED SOURCE VISUALS FOR ATTEMPT 1: place every one of these IDs in poster.html: "
            + ", ".join(required_source_ids)
            + ". These are hard evidence requirements, not optional suggestions."
            if required_source_ids
            else "- No eligible required source visual IDs are present; do not revive rejected paper crops."
        )
        multi_image_instruction = (
            "- Use a visual-first composition: multiple original paper figures/tables must occupy substantive body "
            "regions and carry the scientific story; do not replace their allocated space with prose."
            if len(required_source_ids) >= 2
            else ""
        )
        author_visible_brief = _author_visible_brief(ctx, brief)
        runtime_skill_instruction = _author_runtime_skill_instruction(attempt_dir)
        active_canvas = _active_canvas_contract(ctx)
        canvas_label = f"{active_canvas['w_px']}x{active_canvas['h_px']}"
        board_requirement = (
            f"- Reference-reconstructed fixed board: {canvas_label}, {active_canvas['aspect_ratio']} aspect, "
            "with the reference-owned body regions and geometry. Rebuild the visual shell from the reference blueprint; "
            "the active canvas contract supplies geometry only, not the default AutoDesign poster appearance."
            if _reference_style_contract(ctx)
            else "- CVPR-style fixed landscape board: 84in x 42in, 2:1 aspect, three human conference-poster columns."
        )
        python_path = sys.executable
        return f"""You are authoring the initial HTML/CSS for an academic paper poster in AutoDesign.

Headless execution contract:
- This is an execution task, not a planning task.
- Do not enter plan mode. Do not call EnterPlanMode, ExitPlanMode, AskUserQuestion, or TodoWrite.
- Do not delegate to Explore/subagents.
- Do not stop after writing an implementation plan, research summary, or repair plan.
- You are already approved to edit files in this directory.
- You must directly create or update poster.html in the current directory.
- When finished, write designer_author_done.json.
- If you cannot complete every repair, still write poster.html with the best valid attempt you can produce, then write designer_author_done.json describing the limitation.

Work only inside this directory:
{attempt_dir}
{model_line}
User prompt:
{author_visible_brief}

{palette_heading}:
{color_block}

Visual treatment:
{visual_treatment_block}

Reference poster style:
{reference_style_block}

AutoDesign has already extracted the paper content and source assets. Read only the compact task files first:
- {runtime_skill_instruction}
- author_input_manifest.json
- author_quick_brief.md
- poster_content_brief.json
- poster_plan_contract.json
- paper_visual_provenance.json and paper_visual_storyboard.json when present
- paper_evidence_packs/*.md when present
- paper_memory_dossier.md/json only if a needed method/result/limitation is absent from the evidence packs
- layers/ for source figures/tables/images when present
- reference_style_contract.json, reference_style_blueprint.html, and reference_poster/reference.png when present; these are style-only inputs and never paper evidence

Do not read paper_memory.json or paper_memory.md end-to-end during the first pass. They are fallback sources only.

Output exactly these files:
- poster.html
- designer_author_done.json
Do not write panel_content_plan.json. AutoDesign will validate poster.html with the same HTML-first harness before publishing the standalone file.

If the user prompt contains older AutoDesign planner-tool instructions such as propose_paper_poster_html, composite, or finalize, treat them as internal context only. Your external-author contract is just the two output files above.

Execution discipline:
- Prefer direct file authoring over exploratory analysis. Read the required JSON/Markdown context, then write the output files.
- If you need Python, use this interpreter exactly: {python_path}
- Do not install packages, create virtual environments, or run setup commands.
- Initial drafts can be authored directly. On repair attempts, read repair_context.json first, then visual_repair_packet.md/json and the current candidate primary images. Use validation_feedback.json only as fallback detail, and treat secondary/locked-base images as advisory/reference unless repair_context.json says otherwise. Do not install packages or run heavy browser QA; AutoDesign will capture a preview from the standalone HTML after you exit.

Poster requirements:
{board_requirement}
- Header identity area is limited to exactly these three visible paper-identity rows: paper title, author list, and school/institution/company names.
{identity_layout_block}
- Do not add a fourth header/meta/subtitle row or side identity rail. Do not put any other visible content in the header: no logos, image badges, icons, QR codes, venue/year text, conference names, arXiv/archive labels, citation/contact text, project/code/resource links, topic badges, method slogans, contribution bullets, benchmark claims, source figures/tables, body evidence, captions, or explanatory prose. If venue, project, code, resource, citation, or contact fields are available, omit them from the header; users can add them after export.
- Do not put visible process labels such as "Paper poster", "authored HTML", "source-backed", or "no generated evidence imagery" anywhere.
- Use only claims grounded in paper_memory, paper_memory_dossier, and poster_content_brief.
- Source figures/tables/images are optional only when none are available. Place selected, primary, or required source assets when poster_plan_contract, paper_visual_provenance, paper_visual_storyboard, or layers/ identify them. For `ingest_table_*`, place the original PDF table crop as source evidence; do not substitute a native table.
{required_source_instruction}
{multi_image_instruction}
- This task must work for text-only coding harnesses. Do not require multimodal image inspection to complete the poster; use JSON/Markdown context as the primary source of truth, and treat local image files as optional evidence.
- No generated, stock, remote, data:, file:, or external images.
- Every placed source figure/table/image must carry data-source-id and data-layer-id matching the source asset id. If there are no source figure/table/image assets, use native HTML tables, compact comparison boards, equations, timelines, and dense text panels instead.
{_color_authoring_contract(ctx)}
{reference_layout_authoring_contract}
{table_authoring_contract}
{formula_authoring_contract}
- For paper equations, write TeX source inside `\\(...\\)` for inline math or `\\[...\\]` for display math, preferably inside `.formula`, `.math-block`, or `[data-block-kind="formula"]` elements. AutoDesign injects offline KaTeX after validation; do not add MathJax, KaTeX, scripts, CDN links, or hand-built MathML. Use TeX operators such as `\\lt`, `\\le`, and `\\sqrt{{d_k}}` instead of raw `<` or Unicode/plain approximations.
- Do not put TeX/KaTeX formulas inside narrow metric cards, pipeline stages, chips, badges, or compact KPI cells. Use plain-text labels/values there and move full equations into a wide formula block or sufficiently wide native table row.
- Use semantic panels with stable data-block-id and data-panel-role.
- Each major panel should combine dense paper-grounded text with at least one source visual, native table, equation block, mechanism flow, comparison board, or compact local explanation. Do not satisfy this with a horizontal metric/KPI band of standalone big-number-plus-label tiles; state headline numbers inside running prose or a distilled native table row instead.
- When a source visual/table is floated left or right, the other lane must be filled inside the same direct-child `.figure-flow-unit` / `.source-flow-unit` with source-backed readout, native rows, or mechanism bullets. If there is not enough local evidence to fill the side lane, use a stacked/full-width source-flow unit instead of leaving half a section blank.
- Keep each source asset and its local readout/native rows in one direct-child source-flow unit; do not split the image into one wrapper and place the relevant text/native rows elsewhere in the panel.
{_SOURCE_FLOW_LIST_GUTTER_CONTRACT}
- Preserve source aspect ratio with object-fit: contain. Do not stretch, cover-crop, or center a small figure in a mostly empty panel. Match each source wrapper's aspect to the source asset; do not put a 1.5-2.0 aspect figure in a full-width short white shell that leaves large side gutters.
{typography_authoring_contract}
{_EDITORIAL_LEAD_KEY_AUTHORING_CONTRACT}
- Keep final visible text as poster copy only. No planning notes, lane names, provenance rows, or instructions.
- No scripts, remote assets, @import, event handlers, iframes, or unsafe URLs.

{repair_block}
When done, write designer_author_done.json with a short JSON summary, then exit.
"""

    def _invoke_author_command(
        self,
        command: str,
        *,
        prompt: str,
        attempt_dir: Path,
        timeout_s: int,
        poster_stable_s: float,
        previous_poster_sha256: str = "",
        run_id: str = "",
        attempt: int = 0,
        ctx: ToolContext | None = None,
    ) -> _InvocationResult:
        checkpoint = lambda phase: context_cancellation_checkpoint(ctx, phase)
        checkpoint("external_poster.process.before_command_parse")
        poster_path = attempt_dir / "poster.html"
        done_marker = attempt_dir / "designer_author_done.json"
        stdout_path = attempt_dir / ".designer_author_log.stdout.tmp"
        stderr_path = attempt_dir / ".designer_author_log.stderr.tmp"
        try:
            done_marker.unlink()
        except OSError:
            pass

        try:
            cmd = shlex.split(command)
        except ValueError as exc:
            _write_process_log(
                attempt_dir,
                cmd=[command],
                returncode=None,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                reason="designer_author_command_parse_error",
                error=str(exc),
            )
            return _InvocationResult(status="error", reason="designer_author_command_parse_error")
        if not cmd:
            return _InvocationResult(status="error", reason="designer_author_empty_command")

        start = time.monotonic()
        deadline = start + timeout_s
        stable_seen_at: float | None = None
        identical_seen_at: float | None = None
        changed_repair_seen_at: float | None = None
        last_poster_sig: tuple[int, int] | None = None
        last_wait_log = start
        last_identical_wait_log = start
        env = harness_subprocess_env(
            os.environ,
            harness=str(getattr(self.settings, "designer_author_harness", "") or ""),
            api_key=getattr(self.settings, "harness_api_key", None),
        )
        author_python = (
            env.get("AUTODESIGN_AUTHOR_PYTHON", "").strip()
            or env.get("DESIGN_ANYTHING_AUTHOR_PYTHON", "").strip()
            or sys.executable
        )
        env["AUTODESIGN_AUTHOR_PYTHON"] = author_python
        env.setdefault("DESIGN_ANYTHING_AUTHOR_PYTHON", author_python)
        sensitive_values = derive_external_author_sensitive_values(
            cmd,
            env,
            (str(getattr(self.settings, "harness_api_key", None) or ""),),
        )

        def completion_requested() -> str | None:
            nonlocal stable_seen_at
            nonlocal identical_seen_at
            nonlocal changed_repair_seen_at
            nonlocal last_poster_sig
            nonlocal last_wait_log
            nonlocal last_identical_wait_log

            if done_marker.exists() and poster_path.exists():
                time.sleep(0.25)
                return "done_marker"
            poster_sig = _file_signature(poster_path)
            now = time.monotonic()
            if poster_sig is not None:
                if poster_sig != last_poster_sig:
                    last_poster_sig = poster_sig
                    stable_seen_at = now
                    identical_seen_at = None
                    changed_repair_seen_at = None
                elif stable_seen_at is not None and now - stable_seen_at >= poster_stable_s:
                    if (
                        previous_poster_sha256
                        and sha256_file(poster_path) == previous_poster_sha256
                    ):
                        if identical_seen_at is None:
                            identical_seen_at = now
                        if now - last_identical_wait_log >= 15:
                            last_identical_wait_log = now
                            log(
                                "designer_author.identical_repair_wait",
                                mode="external",
                                elapsed_s=round(now - start, 1),
                                identical_for_s=round(now - identical_seen_at, 1),
                                deadline_s=round(max(0.0, deadline - now), 1),
                            )
                        return None
                    if previous_poster_sha256:
                        if changed_repair_seen_at is None:
                            changed_repair_seen_at = now
                        changed_repair_grace_s = _REPAIR_CHANGED_STABLE_GRACE_S
                        if now - changed_repair_seen_at < changed_repair_grace_s:
                            return None
                        log(
                            "designer_author.repair_changed_stable_without_done_marker",
                            mode="external",
                            elapsed_s=round(now - start, 1),
                            changed_for_s=round(now - changed_repair_seen_at, 1),
                            stable_for_s=round(now - stable_seen_at, 1),
                            grace_s=changed_repair_grace_s,
                            deadline_s=round(max(0.0, deadline - now), 1),
                        )
                        return "poster_changed_stable_without_done_marker"
                    return "poster_stable_without_done_marker"
            if now - last_wait_log >= 15:
                last_wait_log = now
                log(
                    "designer_author.wait",
                    mode="external",
                    elapsed_s=round(now - start, 1),
                    poster_exists=poster_sig is not None,
                    done_marker=done_marker.exists(),
                )
            return None

        process_result = run_external_author_process(
            ExternalAuthorProcessRequest(
                run_id=run_id or f"poster:{attempt_dir.resolve()}",
                attempt=attempt,
                command=cmd,
                cwd=attempt_dir,
                prompt=prompt,
                timeout_s=timeout_s,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                env=env,
                completion_requested=completion_requested,
                interruption_requested=context_cancellation_callback(ctx),
                selection_requested=context_attempt_selection_callback(ctx),
                poll_interval_s=0.25,
                run_dir=getattr(ctx, "run_dir", attempt_dir),
                cancellation_token=context_cancellation_token(ctx),
                sensitive_values=sensitive_values,
            )
        )
        checkpoint("external_poster.process.after_process")
        returncode = process_result.returncode
        timed_out = process_result.timed_out
        reason = process_result.reason
        elapsed = process_result.elapsed_s
        checkpoint("external_poster.process.before_output_hash")
        poster_sha = sha256_file(poster_path) if poster_path.exists() else ""
        checkpoint("external_poster.process.after_output_hash")
        ok_reasons = {
            "done_marker",
            "poster_stable_without_done_marker",
            "poster_changed_stable_without_done_marker",
        }
        status = (
            "selected"
            if process_result.status == "selected"
            else (
                "ok"
                if poster_path.exists()
                and not timed_out
                and (returncode in (0, None) or reason in ok_reasons)
                else "error"
            )
        )
        if timed_out:
            status = "error"
        if status != "ok" and not reason:
            reason = "designer_author_failed"
        checkpoint("external_poster.process.before_log_write")
        _write_process_log(
            attempt_dir,
            cmd=cmd,
            returncode=returncode,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            reason=reason,
            timeout=timed_out,
            timeout_s=timeout_s,
            elapsed_s=elapsed,
            poster_sha256=poster_sha,
            sensitive_values=sensitive_values,
        )
        checkpoint("external_poster.process.after_log_write")
        if status == "selected":
            return _InvocationResult(
                status="selected",
                reason="attempt_selected",
                returncode=returncode,
                timed_out=False,
                elapsed_s=elapsed,
                poster_sha256=poster_sha,
            )
        return _InvocationResult(
            status=status,
            reason=reason if status == "ok" else f"designer_author_{reason}",
            returncode=returncode,
            timed_out=timed_out,
            elapsed_s=elapsed,
            poster_sha256=poster_sha,
        )

    def _fail_from_tool(
        self,
        ctx: ToolContext,
        tool_name: str,
        result: ToolResultRecord,
        attempt_dir: Path,
    ) -> None:
        reason = f"designer_author_{tool_name}_error"
        message = result.error_message or f"{tool_name} failed"
        self._fail(ctx, reason, message, attempt_dir, payload=result.payload)

    def _fail(
        self,
        ctx: ToolContext,
        reason: str,
        message: str,
        attempt_dir: Path,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        ctx.state["designer_contract_abort"] = True
        ctx.state["designer_api_error"] = {
            "type": "external_designer_author",
            "reason": reason,
            "message": message,
        }
        ctx.state["designer_author_result"] = {
            "status": "error",
            "reason": reason,
            "message": message,
            "attempt_dir": str(attempt_dir),
            "payload": payload or {},
        }
        log(
            "designer_author.fail",
            mode="external",
            reason=reason,
            message=message[:800],
            attempt_dir=str(attempt_dir),
        )


def _has_author_context(ctx: ToolContext) -> bool:
    state = ctx.state
    return bool(
        state.get("poster_content_brief")
        and state.get("poster_plan_contract")
        and state.get("paper_memory")
    )


def _best_candidate_fallback_candidates(
    ctx: ToolContext,
    *,
    last_feedback: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in (
        ctx.state.get("paper_poster_html_best_candidate"),
        ctx.state.get("paper_poster_html_locked_base_candidate"),
    ):
        candidate = _load_fallback_candidate_manifest(ctx, record)
        if candidate and str(candidate.get("candidate_id") or "") not in seen:
            seen.add(str(candidate.get("candidate_id") or ""))
            candidates.append(candidate)
    candidates_dir = ctx.run_dir / "html_first" / "candidates"
    for manifest_path in sorted(candidates_dir.glob("candidate_*/manifest.json")):
        candidate = _load_fallback_candidate_manifest(ctx, {"manifest_path": str(manifest_path)})
        candidate_id = str(candidate.get("candidate_id") or manifest_path.parent.name) if candidate else ""
        if candidate and candidate_id not in seen:
            seen.add(candidate_id)
            candidates.append(candidate)
    root_candidate = _load_root_html_first_fallback_candidate(ctx, last_feedback)
    if root_candidate and str(root_candidate.get("candidate_id") or "") not in seen:
        candidates.append(root_candidate)
    candidates.sort(key=_fallback_candidate_priority_key, reverse=True)
    return candidates


def _load_root_html_first_fallback_candidate(
    ctx: ToolContext,
    last_feedback: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Synthesize a fallback candidate from run-level html_first/measure.html.

    Some failed external-author runs have a renderable HTML-first measure file
    but no candidate manifest/preview bundle. This is lower priority than real
    candidate manifests and still goes through the same eligibility checks.
    """
    html_first_dir = ctx.run_dir / "html_first"
    measure_html = html_first_dir / "measure.html"
    if not measure_html.exists() or not measure_html.is_file():
        return None
    payload = last_feedback.get("payload") if isinstance(last_feedback, dict) and isinstance(last_feedback.get("payload"), dict) else {}
    summary = last_feedback.get("summary") if isinstance(last_feedback, dict) and isinstance(last_feedback.get("summary"), dict) else {}
    score = _safe_int(payload.get("candidate_score") or summary.get("candidate_score"), default=0)
    candidate: dict[str, Any] = {
        "candidate_id": "html_first_root",
        "candidate_relative_dir": "html_first",
        "status": "validation_error",
        "stage": payload.get("validation_stage") or payload.get("stage") or summary.get("validation_stage") or "",
        "candidate_score": max(_BEST_CANDIDATE_FALLBACK_MIN_SCORE, score - 5) if score else _BEST_CANDIDATE_FALLBACK_MIN_SCORE,
        "candidate_score_reasons": ["root_html_first_measure_fallback"],
        "measure_html": "html_first/measure.html",
        "payload": dict(payload),
        "_manifest_path_abs": "",
        "_candidate_dir_abs": str(html_first_dir),
        "_measure_html_abs": str(measure_html),
        "_preview_png_abs": str(html_first_dir / "preview.png"),
        "_measurement_json_abs": str(html_first_dir / "measurement.json"),
        "_body_html_abs": str(html_first_dir / "body.html"),
        "_style_css_abs": str(html_first_dir / "style.css"),
        "_measure_only_fallback": True,
        "_allow_system_katex_in_measure": True,
    }
    return candidate


def _best_available_artifact_fallback_candidates(
    ctx: ToolContext,
    *,
    last_feedback: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_html: set[str] = set()

    def add(candidate: dict[str, Any] | None) -> None:
        if not candidate:
            return
        html_path = Path(str(candidate.get("_measure_html_abs") or ""))
        key = str(html_path.resolve()) if html_path.exists() else str(candidate.get("candidate_id") or "")
        if not key or key in seen_html:
            return
        seen_html.add(key)
        candidates.append(candidate)

    for candidate in _best_candidate_fallback_candidates(ctx, last_feedback=last_feedback):
        add(candidate)
    for candidate in _attempt_poster_fallback_candidates(ctx):
        add(candidate)
    candidates.sort(key=_fallback_candidate_priority_key, reverse=True)
    return candidates


def _attempt_poster_fallback_candidates(ctx: ToolContext) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for attempt_dir in sorted((ctx.run_dir / "designer_author").glob("attempt_*")):
        if not attempt_dir.is_dir():
            continue
        poster_html = attempt_dir / "poster.html"
        if not poster_html.exists() or not poster_html.is_file():
            continue
        match = re.search(r"attempt_(\d+)$", attempt_dir.name)
        attempt_index = _safe_int(match.group(1) if match else "", default=0)
        feedback = _read_optional_json(attempt_dir / "validation_feedback.json")
        payload = feedback.get("payload") if isinstance(feedback, dict) and isinstance(feedback.get("payload"), dict) else {}
        summary = feedback.get("summary") if isinstance(feedback, dict) and isinstance(feedback.get("summary"), dict) else {}
        score = _safe_int(payload.get("candidate_score") or summary.get("candidate_score"), default=0)
        if score <= 0:
            score = max(100, 300 + attempt_index)
        candidate_id = f"planner_attempt_{attempt_index:02d}_poster" if attempt_index else f"planner_{attempt_dir.name}_poster"
        candidates.append({
            "candidate_id": candidate_id,
            "candidate_relative_dir": str(attempt_dir.relative_to(ctx.run_dir)),
            "status": "validation_error" if payload else "attempt_output",
            "stage": payload.get("validation_stage") or payload.get("stage") or summary.get("validation_stage") or "",
            "candidate_score": score,
            "candidate_score_reasons": ["planner_attempt_poster_html_fallback"],
            "measure_html": str(poster_html.relative_to(ctx.run_dir)),
            "payload": dict(payload),
            "_manifest_path_abs": "",
            "_candidate_dir_abs": str(attempt_dir),
            "_measure_html_abs": str(poster_html),
            "_preview_png_abs": str(attempt_dir / "preview.png"),
            "_measurement_json_abs": str(attempt_dir / "measurement.json"),
            "_body_html_abs": "",
            "_style_css_abs": "",
            "_measure_only_fallback": True,
            "_fallback_recency_rank": attempt_index,
        })
    return candidates


def _fallback_candidate_priority_key(candidate: dict[str, Any]) -> tuple[int, int, int, int]:
    """Rank delivery candidates by product usefulness, then score, then recency."""
    return (
        1 if _fallback_candidate_deterministic_accepted(candidate) else 0,
        _safe_int(candidate.get("candidate_score"), default=-10_000),
        max(
            _safe_int(candidate.get("_fallback_recency_rank"), default=0),
            _fallback_candidate_numeric_suffix(candidate),
        ),
        0 if candidate.get("_measure_only_fallback") is True else 1,
    )


def _fallback_candidate_numeric_suffix(candidate: dict[str, Any]) -> int:
    values = (
        str(candidate.get("candidate_id") or ""),
        str(candidate.get("candidate_relative_dir") or ""),
        str(candidate.get("_candidate_dir_abs") or ""),
    )
    best = 0
    for value in values:
        for match in re.finditer(r"(?:candidate|attempt)_(\d+)", value):
            best = max(best, _safe_int(match.group(1), default=0))
    return best


def _fallback_candidate_deterministic_accepted(candidate: dict[str, Any]) -> bool:
    if str(candidate.get("status") or "").lower() == "accepted":
        return True
    reasons = candidate.get("candidate_score_reasons")
    if isinstance(reasons, list) and any(str(item) == "accepted_design_spec" for item in reasons):
        return True
    payload = candidate.get("payload")
    if isinstance(payload, dict):
        return str(payload.get("status") or "").lower() == "accepted"
    return False


def _load_fallback_candidate_manifest(ctx: ToolContext, record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    raw_manifest_path = str(record.get("manifest_path") or "")
    manifest_path = Path(raw_manifest_path) if raw_manifest_path else None
    if manifest_path is None:
        candidate_rel = str(record.get("candidate_relative_dir") or "")
        candidate_id = str(record.get("candidate_id") or "")
        if candidate_rel:
            manifest_path = ctx.run_dir / candidate_rel / "manifest.json"
        elif candidate_id:
            manifest_path = ctx.run_dir / "html_first" / "candidates" / candidate_id / "manifest.json"
    if manifest_path is None or not manifest_path.exists():
        return None
    try:
        candidate = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(candidate, dict):
        return None
    candidate["_manifest_path_abs"] = str(manifest_path)
    candidate_dir = _candidate_dir_for_manifest(ctx, candidate, manifest_path)
    candidate["_candidate_dir_abs"] = str(candidate_dir)
    for key, filename in (
        ("measure_html", "measure.html"),
        ("preview_png", "preview.png"),
        ("measurement_json", "measurement.json"),
        ("body_html", "body.html"),
        ("style_css", "style.css"),
    ):
        candidate[f"_{key}_abs"] = str(_candidate_file_for_manifest(ctx, candidate, candidate_dir, key, filename))
    return candidate


def _candidate_dir_for_manifest(ctx: ToolContext, candidate: dict[str, Any], manifest_path: Path) -> Path:
    for key in ("candidate_relative_dir", "candidate_dir"):
        raw = str(candidate.get(key) or "")
        if not raw:
            continue
        path = Path(raw)
        if path.is_absolute():
            return path
        return ctx.run_dir / path
    return manifest_path.parent


def _candidate_file_for_manifest(
    ctx: ToolContext,
    candidate: dict[str, Any],
    candidate_dir: Path,
    key: str,
    filename: str,
) -> Path:
    raw = str(candidate.get(key) or "")
    if raw:
        path = Path(raw)
        if path.is_absolute():
            return path
        run_path = ctx.run_dir / path
        if run_path.exists():
            return run_path
        return candidate_dir / path
    return candidate_dir / filename


def _best_candidate_fallback_acceptance(
    ctx: ToolContext,
    candidate: dict[str, Any],
    last_feedback: dict[str, Any] | None,
) -> dict[str, Any]:
    missing = _fallback_candidate_missing_files(candidate)
    if missing:
        return {"accepted": False, "reason": "missing_candidate_files", "details": {"missing": missing}}
    score = _safe_int(candidate.get("candidate_score"), default=-10_000)
    missing_assets = _fallback_candidate_missing_asset_refs(candidate)
    if missing_assets:
        return {"accepted": False, "reason": "missing_candidate_assets", "details": {"missing_assets": missing_assets[:8]}}
    unsafe_html = _fallback_candidate_unsafe_html(candidate, repo_root=ctx.settings.repo_root)
    if unsafe_html:
        return {"accepted": False, "reason": "unsafe_candidate_html", "details": {"unsafe": unsafe_html[:8]}}
    current_remaining_issue_ids = _fallback_remaining_issue_ids(candidate, last_feedback)
    if (
        _reference_style_contract(ctx)
        and "paper_poster_html_reference_style_contract_failed" in current_remaining_issue_ids
    ):
        return {
            "accepted": False,
            "reason": "current_reference_style_hard_issue",
            "details": {"issue_id": "paper_poster_html_reference_style_contract_failed"},
        }
    if _fallback_candidate_deterministic_accepted(candidate):
        return {
            "accepted": True,
            "reason": "deterministic_accepted_candidate_fallback",
            "candidate_score": score,
            "issue_id": "",
            "remaining_issue_ids": current_remaining_issue_ids,
        }
    if score < _BEST_CANDIDATE_FALLBACK_MIN_SCORE:
        return {
            "accepted": False,
            "reason": "candidate_score_below_fallback_threshold",
            "details": {"candidate_score": score, "min_score": _BEST_CANDIDATE_FALLBACK_MIN_SCORE},
        }
    payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
    primary_issue_id = str(payload.get("primary_blocking_issue_id") or payload.get("issue_id") or "")
    if not _fallback_primary_issue_acceptable(payload):
        return {
            "accepted": False,
            "reason": "primary_issue_not_fallback_eligible",
            "details": {"issue_id": primary_issue_id},
        }
    secondary_blocker = _fallback_secondary_blocker(payload)
    if secondary_blocker:
        return {"accepted": False, "reason": "hard_secondary_diagnostic", "details": secondary_blocker}
    return {
        "accepted": True,
        "reason": "best_candidate_usable_fallback",
        "candidate_score": score,
        "issue_id": primary_issue_id,
        "remaining_issue_ids": current_remaining_issue_ids,
    }


def _best_available_artifact_fallback_acceptance(
    ctx: ToolContext,
    candidate: dict[str, Any],
    last_feedback: dict[str, Any] | None,
) -> dict[str, Any]:
    missing = _fallback_candidate_missing_files(candidate)
    if missing:
        return {"accepted": False, "reason": "missing_artifact_files", "details": {"missing": missing}}
    unsafe_html = _fallback_candidate_unsafe_html(candidate, repo_root=ctx.settings.repo_root)
    if unsafe_html:
        return {"accepted": False, "reason": "unsafe_artifact_html", "details": {"unsafe": unsafe_html[:8]}}
    if not _fallback_candidate_has_poster_root(candidate):
        return {"accepted": False, "reason": "missing_poster_root", "details": {"candidate_id": candidate.get("candidate_id")}}
    missing_assets = _fallback_candidate_missing_asset_refs(candidate)
    if missing_assets:
        return {"accepted": False, "reason": "missing_or_remote_artifact_assets", "details": {"missing_assets": missing_assets[:8]}}
    delivery_assessment = assess_delivery_issues(
        "poster",
        _fallback_delivery_issues(candidate, last_feedback),
    )
    if delivery_assessment.safety_state == "blocked":
        return {
            "accepted": False,
            "reason": "hard_delivery_issue",
            "details": {
                "issue_ids": [
                    issue.issue_id
                    for issue in delivery_assessment.hard_blockers
                ],
            },
        }
    score = _safe_int(candidate.get("candidate_score"), default=0)
    payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
    primary_issue_id = str(payload.get("primary_blocking_issue_id") or payload.get("issue_id") or "")
    hard_issue_ids: list[str] = []
    if (
        _reference_style_contract(ctx)
        and "paper_poster_html_reference_style_contract_failed" in hard_issue_ids
    ):
        return {
            "accepted": False,
            "reason": "current_reference_style_hard_issue",
            "details": {"issue_id": "paper_poster_html_reference_style_contract_failed"},
        }
    return {
        "accepted": True,
        "reason": "best_available_artifact_fallback",
        "candidate_score": score,
        "issue_id": primary_issue_id,
        "remaining_issue_ids": _fallback_remaining_issue_ids(candidate, last_feedback),
        "remaining_hard_issue_ids": hard_issue_ids,
        "quality_status": delivery_assessment.safety_state,
        "quality_diagnostics": [
            issue.issue_id
            for issue in delivery_assessment.quality_diagnostics
        ],
    }


def _fallback_delivery_issues(
    candidate: dict[str, Any],
    last_feedback: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for payload in _fallback_issue_payloads(candidate, last_feedback):
        primary_issue_id = str(
            payload.get("primary_blocking_issue_id")
            or payload.get("issue_id")
            or ""
        )
        if primary_issue_id:
            primary = {**payload, "issue_id": primary_issue_id}
            key = (primary_issue_id, str(primary.get("message") or ""))
            if key not in seen:
                seen.add(key)
                issues.append(primary)
        for field in ("issues", "secondary_gate_issues", "findings"):
            values = payload.get(field)
            if not isinstance(values, list):
                continue
            for raw_issue in values:
                if not isinstance(raw_issue, dict):
                    continue
                issue_id = str(
                    raw_issue.get("issue_id")
                    or raw_issue.get("id")
                    or primary_issue_id
                    or "unknown_delivery_issue"
                )
                key = (issue_id, str(raw_issue.get("message") or ""))
                if key not in seen:
                    seen.add(key)
                    issues.append({**raw_issue, "issue_id": issue_id})
    return issues


def _fallback_candidate_missing_files(candidate: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    keys = ("measure_html",) if candidate.get("_measure_only_fallback") is True else (
        "measure_html",
        "preview_png",
        "measurement_json",
        "body_html",
        "style_css",
    )
    for key in keys:
        path = Path(str(candidate.get(f"_{key}_abs") or ""))
        if not path.exists() or not path.is_file():
            missing.append(key)
    return missing


def _fallback_candidate_unsafe_html(candidate: dict[str, Any], *, repo_root: Path | str | None = None) -> list[str]:
    system_katex_script_hashes = _system_katex_script_hashes(repo_root)
    if candidate.get("_measure_only_fallback") is not True:
        unsafe: list[str] = []
        body_path = Path(str(candidate.get("_body_html_abs") or ""))
        style_path = Path(str(candidate.get("_style_css_abs") or ""))
        measure_path = Path(str(candidate.get("_measure_html_abs") or ""))
        if body_path.exists():
            unsafe.extend(_fallback_html_file_unsafe(body_path, label="body_html", allow_system_katex=False))
        if style_path.exists():
            try:
                style_text = style_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                unsafe.append("style_css_unreadable")
            else:
                if _css_has_unsafe_url(style_text):
                    unsafe.append("style_css[url]")
        if measure_path.exists():
            unsafe.extend(
                _fallback_html_file_unsafe(
                    measure_path,
                    label="measure_html",
                    allow_system_katex=True,
                    system_katex_script_hashes=system_katex_script_hashes,
                    check_css_urls=False,
                )
            )
        return unsafe
    html_path = Path(str(candidate.get("_measure_html_abs") or ""))
    allow_system_katex = candidate.get("_allow_system_katex_in_measure") is True
    return _fallback_html_file_unsafe(
        html_path,
        label="measure_html",
        allow_system_katex=allow_system_katex,
        system_katex_script_hashes=system_katex_script_hashes,
    )


def _fallback_html_file_unsafe(
    html_path: Path,
    *,
    label: str,
    allow_system_katex: bool,
    system_katex_script_hashes: set[str] | None = None,
    check_css_urls: bool = True,
) -> list[str]:
    if not html_path.exists():
        return [f"{label}_missing"]
    try:
        soup = BeautifulSoup(html_path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    except OSError:
        return [f"{label}_unreadable"]
    unsafe: list[str] = []
    for tag in soup.find_all(["script", "iframe"]):
        if tag.name == "script" and allow_system_katex and _is_allowed_system_katex_script(
            tag,
            system_katex_script_hashes or set(),
        ):
            continue
        unsafe.append(f"{label}:<{tag.name}>")
    for tag in soup.find_all(True):
        for attr, raw in list(tag.attrs.items()):
            attr_name = str(attr).lower()
            if attr_name.startswith("on"):
                unsafe.append(f"{label}:{tag.name}[{attr_name}]")
                continue
            if attr_name not in {"href", "src", "xlink:href", "poster", "action"}:
                continue
            values = raw if isinstance(raw, list) else [raw]
            for value in values:
                text = str(value or "").strip()
                if re.match(r"^(?:javascript:)", text, flags=re.IGNORECASE):
                    unsafe.append(f"{label}:{tag.name}[{attr_name}]={text[:80]}")
        inline_style = str(tag.get("style") or "")
        if check_css_urls and _css_has_unsafe_url(inline_style):
            unsafe.append(f"{label}:{tag.name}[style_url]")
    if check_css_urls:
        for style in soup.find_all("style"):
            allow_data_font = allow_system_katex and _is_system_katex_tag(style)
            if _css_has_unsafe_url(style.get_text(), allow_data_font=allow_data_font):
                unsafe.append(f"{label}:style[url]")
    return unsafe


def _is_system_katex_tag(tag: Any) -> bool:
    return any(
        str(tag.get(marker) or "").strip().lower() in {"1", "true", "yes"}
        for marker in ("data-autodesign-katex", "data-designanything-katex")
    )


def _is_allowed_system_katex_script(tag: Any, system_katex_script_hashes: set[str]) -> bool:
    if not _is_system_katex_tag(tag) or not system_katex_script_hashes:
        return False
    text = tag.string if tag.string is not None else tag.get_text()
    digest = hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()
    return digest in system_katex_script_hashes


_SYSTEM_KATEX_SCRIPT_HASH_CACHE: dict[str, set[str]] = {}


def _system_katex_script_hashes(repo_root: Path | str | None) -> set[str]:
    if repo_root is None:
        return set()
    key = str(Path(repo_root))
    cached = _SYSTEM_KATEX_SCRIPT_HASH_CACHE.get(key)
    if cached is not None:
        return cached
    block = inline_katex_bundle(Path(repo_root), root_selector=".paper-poster")
    hashes: set[str] = set()
    if block:
        soup = BeautifulSoup(block, "html.parser")
        for tag in soup.find_all("script"):
            if not _is_system_katex_tag(tag):
                continue
            text = tag.string if tag.string is not None else tag.get_text()
            script_text = str(text or "")
            for variant in (
                script_text,
                script_text.replace("__autoDesignMath", "__designAnythingMath"),
            ):
                hashes.add(hashlib.sha256(variant.encode("utf-8")).hexdigest())
    _SYSTEM_KATEX_SCRIPT_HASH_CACHE[key] = hashes
    return hashes


def _css_has_unsafe_url(css_text: str, *, allow_data_font: bool = False) -> bool:
    for match in _CSS_IMPORT_RE.finditer(css_text or ""):
        value = (match.group(2) or match.group(3) or "").strip().strip("'\"")
        if allow_data_font and value.lower().startswith("data:font/"):
            continue
        if re.match(r"^(?://|https?:|data:|file:|blob:|javascript:)", value, flags=re.IGNORECASE):
            return True
    for match in _CSS_URL_RE.finditer(css_text or ""):
        value = match.group(2).strip().strip("'\"")
        if allow_data_font and value.lower().startswith("data:font/"):
            continue
        if re.match(r"^(?://|https?:|data:|file:|blob:|javascript:)", value, flags=re.IGNORECASE):
            return True
    return False


def _fallback_candidate_has_poster_root(candidate: dict[str, Any]) -> bool:
    html_path = Path(str(candidate.get("_measure_html_abs") or ""))
    if not html_path.exists():
        return False
    try:
        soup = BeautifulSoup(html_path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    except OSError:
        return False
    if soup.select_one(".paper-poster, .poster, [data-artifact-type='poster'], [data-render-mode='authored_html']"):
        return True
    body = soup.body
    if body is None:
        return False
    body_tokens = " ".join(str(value).lower() for value in (body.get("class") or []))
    return "poster" in body_tokens or str(body.get("data-artifact-type") or "").lower() == "poster"


def _fallback_candidate_missing_asset_refs(candidate: dict[str, Any]) -> list[str]:
    html_path = Path(str(candidate.get("_measure_html_abs") or ""))
    candidate_dir = Path(str(candidate.get("_candidate_dir_abs") or ""))
    if not html_path.exists():
        return ["measure_html"]
    try:
        soup = BeautifulSoup(html_path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    except OSError:
        return ["measure_html"]
    missing: list[str] = []
    attrs = ("src", "href", "xlink:href", "poster", "srcset")
    for tag in soup.find_all(True):
        for attr in attrs:
            value = str(tag.get(attr) or "").strip()
            if not value:
                continue
            is_asset_ref = tag.name in {"img", "source", "image", "video"} or (
                tag.name == "link" and "stylesheet" in {str(item).lower() for item in tag.get("rel") or []}
            )
            refs = _split_srcset_refs(value) if attr == "srcset" else [value]
            for ref in refs:
                if is_asset_ref and re.match(r"^(?://|https?:|data:|file:|blob:|javascript:)", ref, flags=re.IGNORECASE):
                    missing.append(ref)
                    continue
                if _LOCAL_ASSET_SKIP_RE.match(ref):
                    continue
                asset_path = _fallback_asset_ref_path(ref, html_path.parent, candidate_dir)
                if asset_path is not None and not asset_path.exists():
                    missing.append(ref)
    return missing


def _split_srcset_refs(value: str) -> list[str]:
    refs: list[str] = []
    for part in (value or "").split(","):
        first = part.strip().split(None, 1)[0] if part.strip() else ""
        if first:
            refs.append(first)
    return refs


def _fallback_asset_ref_path(value: str, base_dir: Path, candidate_dir: Path) -> Path | None:
    ref = (value or "").strip().strip("'\"")
    if not ref or ref.startswith("#") or ref.startswith("data:") or re.match(r"^[a-z]+:", ref, flags=re.IGNORECASE):
        return None
    path_part = re.split(r"[?#]", ref, maxsplit=1)[0]
    if not path_part:
        return None
    path = Path(unquote(path_part))
    if path.is_absolute():
        return path
    for base in (base_dir, candidate_dir):
        candidate = base / path
        if candidate.exists():
            return candidate
    return base_dir / path


def _fallback_primary_issue_acceptable(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    issue_id = str(payload.get("primary_blocking_issue_id") or payload.get("issue_id") or "")
    hard_denied = {
        "paper_poster_html_visible_pipeline_text",
        "paper_poster_html_unverified_numeric_claim",
        "paper_poster_html_identity_header_only_failed",
        "paper_poster_html_unsafe_external_asset_ref",
        "paper_poster_html_severe_text_clipping",
        "paper_poster_html_severe_text_overlap",
        "paper_poster_html_source_required_assets_missing",
        "designer_author_local_repair_scope_violation",
        "paper_poster_html_designer_flow_canvas_overflow",
        "paper_poster_html_root_wrapper_padding_overflow",
        "paper_poster_html_row_allocation_density_regression",
        "paper_poster_html_post_overflow_density_conservation_failed",
        "paper_poster_html_editorial_flow_shape_failed",
        "paper_poster_html_reference_style_contract_failed",
        "paper_poster_html_panel_flow_shape_failed",
        "paper_poster_html_source_coverage_failed",
    }
    if issue_id in hard_denied:
        return False
    if _feedback_has_required_blank_fill(payload, payload):
        return False
    if issue_id == "paper_poster_html_local_flow_overflow":
        return _fallback_local_flow_near_miss(payload) or _fallback_local_flow_usable_delivery(payload)
    if issue_id == "paper_poster_html_editorial_flow_fill_failed":
        return not _feedback_has_required_blank_fill(payload, payload)
    if issue_id == "paper_poster_html_typography_contract_failed":
        return _typography_feedback_soft_finalizable(payload, payload)
    if issue_id == "paper_poster_html_source_wrap_missing":
        return _source_wrap_feedback_soft_finalizable(payload, payload)
    if issue_id == "paper_poster_html_source_visual_too_small":
        return _source_visual_feedback_soft_finalizable(payload, payload) or _fallback_source_visual_polish(payload)
    if issue_id == "paper_poster_html_source_visual_repair_regression":
        return _fallback_source_visual_polish(payload) and not _feedback_has_required_blank_fill(payload, payload)
    return False


def _fallback_local_flow_near_miss(payload: dict[str, Any]) -> bool:
    issues = _feedback_issues(payload, payload)
    if not issues:
        return False
    for issue in issues:
        if issue.get("visible_overflow") is True:
            return False
        if str(issue.get("severity") or "") not in {"near_miss", "advisory", "polish"}:
            return False
        if issue.get("soft_finalizable") is False or issue.get("blocks_soft_accept") is True:
            return False
        if _scroll_bottom_overflow_px(issue) > 16:
            return False
    return True


def _fallback_local_flow_usable_delivery(payload: dict[str, Any]) -> bool:
    """Allow high-scoring fallback delivery for small non-visible local overflow.

    This intentionally does not relax the validator or normal soft-accept path.
    It only applies after the repair loop would otherwise fail, and it rejects
    any visible overflow, clipping/overlap evidence, required blank-fill, or
    large residual scroll overflow.
    """
    if _feedback_has_required_blank_fill(payload, payload):
        return False
    issues = _feedback_issues(payload, payload)
    if not issues:
        return False
    if _safe_int(payload.get("hard_issue_count"), default=0) > len(issues):
        return False
    max_bottom = 0
    for issue in issues:
        if issue.get("visible_overflow") is True or issue.get("blocks_soft_accept") is True:
            return False
        if any(
            issue.get(key) is True
            for key in (
                "visible_overlap",
                "has_visible_overlap",
                "text_overlap",
                "clipping",
                "has_clipping",
                "visible_clipping",
                "canvas_overflow",
            )
        ):
            return False
        max_bottom = max(max_bottom, _scroll_bottom_overflow_px(issue))
    return max_bottom <= 32


def _fallback_source_visual_polish(payload: dict[str, Any]) -> bool:
    issues = _feedback_issues(payload, payload)
    if not issues:
        return False
    for issue in issues:
        target_problem = str(issue.get("target_problem") or "")
        if target_problem not in {"readable_visual_wrapper_polish", "blank_wrapper_shell", "minor_geometry_gap"}:
            return False
        geometry = issue.get("readable_visual_geometry") if isinstance(issue.get("readable_visual_geometry"), dict) else {}
        same_flow = issue.get("same_flow_fill_metrics") if isinstance(issue.get("same_flow_fill_metrics"), dict) else {}
        if _safe_int(geometry.get("source_width_px"), default=0) < _safe_int(geometry.get("min_readable_width_px"), default=1):
            return False
        if _safe_float(geometry.get("source_panel_area_ratio"), default=0.0) < _safe_float(geometry.get("required_source_area_ratio"), default=0.03):
            return False
        if _safe_int(geometry.get("rendered_source_height_px"), default=0) < _safe_int(geometry.get("required_source_height_px"), default=1):
            return False
        if same_flow and same_flow.get("same_flow_fill_pass") is False:
            return False
    return True


def _fallback_secondary_blocker(payload: dict[str, Any]) -> dict[str, Any] | None:
    diagnostics = payload.get("secondary_gate_issues")
    if not isinstance(diagnostics, list):
        return None
    hard_secondary_issue_ids = {
        "paper_poster_html_visible_pipeline_text",
        "paper_poster_html_unverified_numeric_claim",
        "paper_poster_html_identity_header_only_failed",
        "paper_poster_html_unsafe_external_asset_ref",
        "paper_poster_html_severe_text_clipping",
        "paper_poster_html_severe_text_overlap",
        "paper_poster_html_source_required_assets_missing",
        "designer_author_local_repair_scope_violation",
        "paper_poster_html_designer_flow_canvas_overflow",
        "paper_poster_html_row_allocation_density_regression",
        "paper_poster_html_post_overflow_density_conservation_failed",
        "paper_poster_html_reference_style_contract_failed",
    }
    for diagnostic in diagnostics[:12]:
        if not isinstance(diagnostic, dict):
            continue
        issue_id = str(diagnostic.get("issue_id") or "")
        if issue_id in hard_secondary_issue_ids:
            return {"issue_id": issue_id}
        if _feedback_has_required_blank_fill(diagnostic, diagnostic):
            return {"issue_id": issue_id, "reason": "required_blank_fill"}
        if issue_id == "paper_poster_html_source_visual_too_small":
            if _source_visual_feedback_soft_finalizable(diagnostic, diagnostic) or _fallback_source_visual_polish(diagnostic):
                continue
            return {"issue_id": issue_id, "reason": "hard_source_visual_secondary"}
        if issue_id == "paper_poster_html_source_wrap_missing":
            if _source_wrap_feedback_soft_finalizable(diagnostic, diagnostic):
                continue
            # Source-wrap secondaries are retained as diagnostics for fallback; they often lag behind
            # the primary visual near-miss and should not alone prevent a usable poster.
            continue
        if issue_id == "paper_poster_html_editorial_flow_fill_failed":
            continue
        if diagnostic.get("blocks_soft_accept") is True:
            return {"issue_id": issue_id, "reason": "blocks_soft_accept"}
    return None


def _fallback_remaining_issue_ids(candidate: dict[str, Any], last_feedback: dict[str, Any] | None) -> list[str]:
    issue_ids: list[str] = []
    payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
    _append_feedback_issue_ids(issue_ids, payload)
    if isinstance(last_feedback, dict):
        summary = last_feedback.get("summary") if isinstance(last_feedback.get("summary"), dict) else {}
        last_payload = last_feedback.get("payload") if isinstance(last_feedback.get("payload"), dict) else last_feedback
        _append_feedback_issue_ids(issue_ids, summary)
        if isinstance(last_payload, dict):
            _append_feedback_issue_ids(issue_ids, last_payload)
    unique: list[str] = []
    seen: set[str] = set()
    for issue_id in issue_ids:
        if issue_id and issue_id not in seen:
            seen.add(issue_id)
            unique.append(issue_id)
    return unique[:12]


def _fallback_hard_issue_ids(candidate: dict[str, Any], last_feedback: dict[str, Any] | None) -> list[str]:
    hard: list[str] = []
    for payload in _fallback_issue_payloads(candidate, last_feedback):
        if not isinstance(payload, dict):
            continue
        primary = str(payload.get("primary_blocking_issue_id") or payload.get("issue_id") or "")
        if primary:
            hard.append(primary)
        for issue in payload.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            issue_id = str(issue.get("issue_id") or payload.get("issue_id") or "")
            if not issue_id:
                continue
            severity = str(issue.get("severity") or "").lower()
            if severity in {"hard", "required", "blocking", "error"} or issue.get("blocks_soft_accept") is True:
                hard.append(issue_id)
        for diagnostic in payload.get("secondary_gate_issues") or []:
            if not isinstance(diagnostic, dict):
                continue
            issue_id = str(diagnostic.get("issue_id") or "")
            if not issue_id:
                continue
            severity = str(diagnostic.get("severity") or "").lower()
            if severity in {"hard", "required", "blocking", "error"} or diagnostic.get("blocks_soft_accept") is True:
                hard.append(issue_id)
    unique: list[str] = []
    seen: set[str] = set()
    for issue_id in hard:
        if issue_id and issue_id not in seen:
            seen.add(issue_id)
            unique.append(issue_id)
    return unique[:16]


def _fallback_issue_payloads(candidate: dict[str, Any], last_feedback: dict[str, Any] | None) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    candidate_payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
    if candidate_payload:
        payloads.append(candidate_payload)
    if not candidate_payload and isinstance(last_feedback, dict):
        summary = last_feedback.get("summary") if isinstance(last_feedback.get("summary"), dict) else {}
        payload = last_feedback.get("payload") if isinstance(last_feedback.get("payload"), dict) else last_feedback
        if summary:
            payloads.append(summary)
        if isinstance(payload, dict) and payload:
            payloads.append(payload)
    return payloads


def _append_feedback_issue_ids(issue_ids: list[str], payload: dict[str, Any]) -> None:
    for key in ("primary_blocking_issue_id", "issue_id"):
        value = str(payload.get(key) or "")
        if value:
            issue_ids.append(value)
    for issue in payload.get("issues") or []:
        if isinstance(issue, dict) and issue.get("issue_id"):
            issue_ids.append(str(issue.get("issue_id")))
    for diagnostic in payload.get("secondary_gate_issues") or []:
        if isinstance(diagnostic, dict) and diagnostic.get("issue_id"):
            issue_ids.append(str(diagnostic.get("issue_id")))


def _fallback_candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
    return {
        "candidate_id": candidate.get("candidate_id"),
        "candidate_relative_dir": candidate.get("candidate_relative_dir"),
        "status": candidate.get("status"),
        "stage": candidate.get("stage"),
        "candidate_score": candidate.get("candidate_score"),
        "candidate_score_reasons": candidate.get("candidate_score_reasons") or [],
        "issue_id": payload.get("issue_id"),
        "primary_blocking_issue_id": payload.get("primary_blocking_issue_id"),
        "preview_png": candidate.get("preview_png"),
        "measurement_json": candidate.get("measurement_json"),
    }


def _feedback_summary_for_manifest(feedback: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(feedback, dict):
        return {}
    summary = feedback.get("summary") if isinstance(feedback.get("summary"), dict) else {}
    payload = feedback.get("payload") if isinstance(feedback.get("payload"), dict) else feedback
    compact = {
        "issue_id": summary.get("issue_id") or payload.get("issue_id"),
        "primary_blocking_issue_id": payload.get("primary_blocking_issue_id"),
        "repair_route": summary.get("repair_route") or payload.get("repair_route"),
        "candidate_id": summary.get("candidate_id") or payload.get("candidate_id"),
        "candidate_score": summary.get("candidate_score") or payload.get("candidate_score"),
    }
    for key in (
        "feedback_tool",
        "critic_verdict",
        "critic_score",
        "critic_summary",
    ):
        value = summary.get(key) if key in summary else payload.get(key)
        if value not in (None, "", [], {}):
            compact[key] = value
    dimension_scores = payload.get("dimension_scores")
    if isinstance(dimension_scores, dict) and dimension_scores:
        compact["dimension_scores"] = dimension_scores
    return compact


def _rewrite_run_local_asset_refs(
    html_path: Path,
    run_dir: Path,
    *,
    additional_run_dirs: tuple[Path, ...] = (),
) -> None:
    try:
        text = html_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    # Promotion leases expose the same run through a stable filesystem path
    # (for example /.vol/...) while authored HTML still contains the canonical
    # display path. Normalize both identities before standalone asset inlining.
    run_prefixes: set[str] = set()
    for candidate in (run_dir, *additional_run_dirs):
        run_prefixes.add(str(candidate).rstrip("/"))
        run_prefixes.add(str(candidate.resolve()).rstrip("/"))
    for run_prefix in sorted(filter(None, run_prefixes), key=len, reverse=True):
        text = text.replace(run_prefix + "/", "")
        text = text.replace(run_prefix, ".")
    html_path.write_text(text, encoding="utf-8")


_LAYER_PLACEHOLDER_RE = re.compile(r"\{\{\s*(layer|asset)\s*:\s*([^{}]+?)\s*\}\}")


def _resolve_layer_asset_placeholders(html_path: Path) -> dict[str, Any]:
    """Rewrite external-author layer placeholders to local asset paths.

    External coding agents are instructed to use ``{{layer:ingest_fig_01}}`` for
    source crops. The direct-final path publishes authored HTML without the
    html-first renderer, so resolve those aliases before local-asset inlining.
    """
    result: dict[str, Any] = {
        "enabled": True,
        "resolved_count": 0,
        "missing_count": 0,
        "placeholders": [],
    }
    try:
        text = html_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        result.update({"enabled": False, "error": str(exc)})
        return result

    cache: dict[str, str | None] = {}

    def repl(match: re.Match[str]) -> str:
        kind = match.group(1)
        key = match.group(2).strip()
        cache_key = f"{kind}:{key}"
        if cache_key not in cache:
            cache[cache_key] = _layer_asset_relative_path(html_path.parent, key, kind=kind)
        relative = cache[cache_key]
        if relative:
            result["resolved_count"] += 1
            result["placeholders"].append({"kind": kind, "key": key, "path": relative})
            return relative
        result["missing_count"] += 1
        result["placeholders"].append({"kind": kind, "key": key, "missing": True})
        return match.group(0)

    rewritten = _LAYER_PLACEHOLDER_RE.sub(repl, text)
    if rewritten != text:
        html_path.write_text(rewritten, encoding="utf-8")
    return result


def _html_contains_layer_placeholders(html_path: Path) -> bool:
    try:
        text = html_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(_LAYER_PLACEHOLDER_RE.search(text))


def _layer_asset_relative_path(base_dir: Path, key: str, *, kind: str) -> str | None:
    clean_key = str(key or "").strip()
    if not clean_key:
        return None
    asset_dirs = ("layers", "assets") if kind == "layer" else ("assets", "layers")
    stems = [f"img_{clean_key}", clean_key]
    exts = (".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif")
    candidates: list[Path] = []
    for dirname in asset_dirs:
        root = base_dir / dirname
        if not root.is_dir():
            continue
        for stem in stems:
            candidates.extend(root.glob(f"{stem}.*"))
        for ext in exts:
            for stem in stems:
                direct = root / f"{stem}{ext}"
                if direct.exists():
                    candidates.insert(0, direct)
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        try:
            return resolved.relative_to(base_dir.resolve()).as_posix()
        except ValueError:
            continue
    return None


def _copy_run_file(src: Path, dst: Path) -> bool:
    if not src.exists() or not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _stage_reference_style_inputs(ctx: ToolContext, attempt_dir: Path) -> list[str]:
    """Stage only the safe preview/metadata, never the original reference file."""

    selected = _structured_selected_color_system(ctx)
    reference = _reference_style_contract(ctx)
    contract_path = attempt_dir / "reference_style_contract.json"
    if reference and not contract_path.exists():
        atomic_write_json(contract_path, reference)
    staged: list[str] = ["reference_style_contract.json"] if contract_path.exists() else []
    blueprint_source = Path(
        ctx.state.get("reference_style_blueprint_path")
        or (ctx.run_dir / "reference_style_blueprint.html")
    )
    if blueprint_source.exists() and blueprint_source.is_file():
        blueprint_target = attempt_dir / "reference_style_blueprint.html"
        if selected and reference:
            _write_selected_reference_blueprint(
                blueprint_source,
                blueprint_target,
                reference=reference,
                selected=selected,
            )
        else:
            shutil.copy2(blueprint_source, blueprint_target)
        staged.append("reference_style_blueprint.html")
    source_dir = ctx.run_dir / "reference_poster"
    if not source_dir.exists():
        return staged
    target_dir = attempt_dir / "reference_poster"
    for name in ("reference.png",):
        source = source_dir / name
        if not source.exists() or not source.is_file():
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / name
        if selected and reference:
            if not _write_neutral_reference_preview(source, target):
                continue
        else:
            shutil.copy2(source, target)
        staged.append(f"reference_poster/{name}")
    return staged


def _write_selected_reference_blueprint(
    source: Path,
    target: Path,
    *,
    reference: dict[str, Any],
    selected: dict[str, Any],
) -> None:
    soup = BeautifulSoup(source.read_text(encoding="utf-8"), "html.parser")
    replacements = _reference_color_variable_replacements(reference)
    declarations = _selected_palette_css_declarations(selected)
    style_tags = [tag for tag in soup.find_all("style") if isinstance(tag, Tag)]
    if not style_tags:
        head = soup.head or soup.new_tag("head")
        if soup.head is None:
            soup.insert(0, head)
        style_tags = [soup.new_tag("style")]
        head.append(style_tags[0])
    inline_style_tags = [tag for tag in soup.find_all(style=True) if isinstance(tag, Tag)]
    css_blocks = [
        ("stylesheet", style.get_text("\n", strip=False))
        for style in style_tags
    ]
    css_blocks.extend(
        ("declarations", str(tag.get("style") or ""))
        for tag in inline_style_tags
    )
    transformed = _transform_reference_css_blocks(css_blocks, replacements)
    stylesheets = transformed[:len(style_tags)]
    for index, (style, css) in enumerate(zip(style_tags, stylesheets, strict=True)):
        style.string = (declarations + "\n" + css) if index == 0 else css
    inline_styles = transformed[len(style_tags):]
    for tag, css in zip(inline_style_tags, inline_styles, strict=True):
        tag["style"] = css
    staged_blueprint = str(soup)
    forbidden = _remaining_reference_palette_tokens(
        staged_blueprint,
        reference=reference,
        selected=selected,
    )
    if forbidden:
        rendered = ", ".join(forbidden)
        raise ValueError(
            "selected reference blueprint staging left forbidden reference palette "
            f"tokens: {rendered}; remove them from selectors, strings, URLs, or HTML "
            "attributes because only CSS declaration values may be remapped"
        )
    target.write_text(staged_blueprint, encoding="utf-8")


def _reference_color_variable_replacements(reference: dict[str, Any]) -> dict[str, str]:
    color_system = reference.get("color_system") if isinstance(reference.get("color_system"), dict) else {}
    roles = color_system.get("roles") if isinstance(color_system.get("roles"), dict) else {}
    role_variables = (
        ("primary", "--poster-primary"),
        ("bar", "--poster-bar"),
        ("accent", "--poster-accent"),
        ("secondary", "--poster-secondary"),
        ("text", "--poster-text"),
        ("ink", "--poster-text"),
        ("header_text", "--poster-header-text"),
        ("section_heading_text", "--poster-header-text"),
        ("on_primary", "--poster-header-text"),
        ("background", "--poster-bg"),
    )
    replacements: dict[str, str] = {}
    for role, variable in role_variables:
        value = str(roles.get(role) or "").strip()
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
            replacements.setdefault(value.upper(), f"var({variable})")
    for value in color_system.get("allowed_hexes") or []:
        color = str(value or "").strip()
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            replacements.setdefault(color.upper(), "var(--poster-accent)")
    return replacements


def _selected_palette_css_declarations(selected: dict[str, Any]) -> str:
    css_variables = selected.get("css_variables") if isinstance(selected.get("css_variables"), dict) else {}
    lines = [
        f"  {name}: {value};"
        for name, value in css_variables.items()
        if str(name).startswith("--poster-")
        and re.fullmatch(r"#[0-9A-Fa-f]{6}", str(value or ""))
    ]
    return ":root {\n" + "\n".join(lines) + "\n}"


def _transform_reference_css_blocks(
    blocks: list[tuple[str, str]],
    replacements: dict[str, str],
) -> list[str]:
    transformed: list[str] = []
    for mode, css in blocks:
        if mode == "stylesheet":
            transformed.append(
                transform_stylesheet_declaration_values(css, replacements)
            )
        elif mode == "declarations":
            transformed.append(transform_declaration_list_values(css, replacements))
        else:
            raise ValueError(f"unsupported reference CSS parse mode: {mode!r}")
    return transformed


def _remaining_reference_palette_tokens(
    blueprint: str,
    *,
    reference: dict[str, Any],
    selected: dict[str, Any],
) -> list[str]:
    reference_colors = reference.get("color_system")
    reference_colors = reference_colors if isinstance(reference_colors, dict) else {}
    selected_hexes = {
        str(value).upper()
        for value in selected.get("allowed_hexes") or []
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", str(value or ""))
    }
    selected_variables = selected.get("css_variables")
    if isinstance(selected_variables, dict):
        selected_hexes.update(
            str(value).upper()
            for value in selected_variables.values()
            if re.fullmatch(r"#[0-9A-Fa-f]{6}", str(value or ""))
        )
    forbidden = {
        value for value in _reference_color_variable_replacements(reference)
        if value not in selected_hexes
    }
    reference_palette_id = str(reference_colors.get("palette_id") or "").strip()
    selected_palette_id = str(selected.get("palette_id") or "").strip()
    if reference_palette_id and reference_palette_id != selected_palette_id:
        forbidden.add(reference_palette_id)
    soup = BeautifulSoup(blueprint, "html.parser")
    style_reference_id = str(reference.get("style_reference_id") or "").strip()
    if style_reference_id and style_reference_id == reference_palette_id:
        for tag in soup.find_all(attrs={"data-reference-style-id": True}):
            if str(tag.get("data-reference-style-id") or "").strip() == style_reference_id:
                del tag["data-reference-style-id"]
    casefolded = str(soup).casefold()
    remaining = {
        token for token in forbidden
        if token.casefold() in casefolded
    }
    semantic_hashes: set[str] = set()
    for style in soup.find_all("style"):
        semantic_hashes.update(
            find_stylesheet_hash_tokens(style.get_text("\n", strip=False))
        )
    for tag in soup.find_all(style=True):
        if isinstance(tag, Tag):
            semantic_hashes.update(
                find_declaration_list_hash_tokens(str(tag.get("style") or ""))
            )
    remaining.update(
        token for token in forbidden
        if token.startswith("#") and token.upper() in semantic_hashes
    )
    return sorted(remaining)


def _write_neutral_reference_preview(source: Path, target: Path) -> bool:
    try:
        with Image.open(source) as image:
            neutral = ImageOps.grayscale(ImageOps.exif_transpose(image).convert("RGB"))
            neutral.save(target, format="PNG")
    except (OSError, ValueError):
        return False
    return True


def _stage_runtime_skills(ctx: ToolContext, attempt_dir: Path, *, stage: str) -> dict[str, Any]:
    """Stage only hash-verified, active-stage runtime skill content."""

    staged_dir = attempt_dir / "runtime_skills"
    snapshot_root = ctx.run_dir / "runtime_skills"
    snapshot_path = snapshot_root / "snapshot.json"
    if snapshot_root.exists():
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("runtime skill snapshot is missing or unreadable") from exc
        if not isinstance(snapshot, dict) or int(snapshot.get("version") or 0) != 2:
            raise ValueError("runtime skill snapshot must use version 2")
        selected = snapshot.get("selected")
        runtime_state = snapshot.get("runtime_state")
        if not isinstance(selected, list) or not isinstance(runtime_state, dict):
            raise ValueError("runtime skill snapshot is missing selected/runtime_state data")
        bundle = SkillBundle.from_runtime_state(runtime_state)
        selected_ids = [
            str(item.get("id") or "")
            for item in selected
            if isinstance(item, dict) and str(item.get("id") or "")
        ]
        if set(bundle.ids) != set(selected_ids):
            raise ValueError("runtime skill snapshot cannot reconstruct every selected pack")

        entries: list[dict[str, Any]] = []
        for item in selected:
            if not isinstance(item, dict):
                raise ValueError("runtime skill snapshot contains an invalid selected entry")
            skill_id = str(item.get("id") or "").strip()
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", skill_id)
            expected_pack_path = f"packs/{safe_name}"
            if not skill_id or str(item.get("pack_path") or "") != expected_pack_path:
                raise ValueError(f"unsafe runtime skill snapshot pack path for {skill_id!r}")
            pack = bundle.get(skill_id)
            if pack is None:
                raise ValueError(f"runtime skill snapshot pack is unavailable: {skill_id}")
            canonical_root = (snapshot_root / expected_pack_path).resolve()
            if pack.root.resolve() != canonical_root:
                raise ValueError(f"runtime skill snapshot root mismatch: {skill_id}")
            if not pack.verify_integrity():
                raise ValueError(f"runtime skill snapshot hash mismatch: {skill_id}")
            if stage not in pack.manifest.stages:
                continue
            skeleton = pack.render(stage)
            if not skeleton:
                raise ValueError(f"runtime skill stage skeleton is missing: {skill_id}:{stage}")

            target_root = staged_dir / expected_pack_path
            target_root.mkdir(parents=True, exist_ok=True)
            (target_root / "SKILL.md").write_text(skeleton.rstrip() + "\n", encoding="utf-8")
            resources: list[dict[str, str]] = []
            for resource in pack.manifest.resources:
                if stage not in resource.stages:
                    continue
                content = pack.read_resource(resource.id, stage)
                if content is None:
                    raise ValueError(
                        f"runtime skill resource hash mismatch: {skill_id}:{resource.id}"
                    )
                relative = Path(resource.path)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError(f"unsafe runtime skill resource path: {resource.path}")
                target = (target_root / relative).resolve()
                try:
                    target.relative_to(target_root.resolve())
                except ValueError as exc:
                    raise ValueError(
                        f"runtime skill resource escapes staged pack: {resource.path}"
                    ) from exc
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                resources.append({
                    "id": resource.id,
                    "path": str((Path(expected_pack_path) / relative).as_posix()),
                    "description": resource.description,
                    "when_to_read": resource.when_to_read,
                    "media_type": resource.media_type,
                })
            entries.append({
                "id": skill_id,
                "path": str((Path(expected_pack_path) / "SKILL.md").as_posix()),
                "resources": resources,
            })
        return _write_staged_runtime_skill_index(
            attempt_dir,
            staged_dir,
            stage=stage,
            entries=entries,
            source="run_snapshot",
        )

    skill_ids = ctx.state.get("direct_runtime_skill_ids")
    if not isinstance(skill_ids, list) or not skill_ids:
        return {"files": [], "catalog": {"stage": stage, "available": False}}
    registry = SkillRegistry.load(Path(getattr(ctx.settings, "skills_dir", "") or ""))
    entries: list[dict[str, Any]] = []
    for value in skill_ids:
        skill_id = str(value or "").strip()
        if not skill_id:
            continue
        pack = registry.get(skill_id)
        if pack is None:
            raise ValueError(f"requested direct runtime skill is invalid or missing: {skill_id}")
        if stage not in pack.manifest.stages:
            continue
        if pack.manifest.manifest_version == 2 and not pack.verify_integrity():
            raise ValueError(f"requested direct runtime skill failed integrity checks: {skill_id}")
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", skill_id)
        pack_path = f"packs/{safe_name}"
        target_root = staged_dir / pack_path
        target_root.mkdir(parents=True, exist_ok=True)
        skeleton = pack.render(stage)
        if not skeleton:
            raise ValueError(f"requested direct runtime skill stage is missing: {skill_id}:{stage}")
        (target_root / "SKILL.md").write_text(skeleton.rstrip() + "\n", encoding="utf-8")
        resources: list[dict[str, str]] = []
        for resource in pack.manifest.resources:
            if stage not in resource.stages:
                continue
            content = pack.read_resource(resource.id, stage)
            if content is None:
                raise ValueError(f"requested direct runtime resource is invalid: {skill_id}:{resource.id}")
            target = target_root / resource.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            resources.append({
                "id": resource.id,
                "path": str((Path(pack_path) / resource.path).as_posix()),
                "description": resource.description,
                "when_to_read": resource.when_to_read,
                "media_type": resource.media_type,
            })
        entries.append({
            "id": skill_id,
            "path": str((Path(pack_path) / "SKILL.md").as_posix()),
            "resources": resources,
        })
    return _write_staged_runtime_skill_index(
        attempt_dir,
        staged_dir,
        stage=stage,
        entries=entries,
        source="direct_registry",
    )


def _write_staged_runtime_skill_index(
    attempt_dir: Path,
    staged_dir: Path,
    *,
    stage: str,
    entries: list[dict[str, Any]],
    source: str,
) -> dict[str, Any]:
    if not entries:
        return {"files": [], "catalog": {"stage": stage, "available": False}}
    index = [
        "# Runtime Skills",
        "",
        f"Read this index first. Active stage: {stage}.",
        "Resources are staged files; read only those needed for the current task.",
        "",
    ]
    for entry in entries:
        index.append(f"- `{entry['path']}`: {entry['id']} compact operating guidance.")
        index.extend(
            f"  - `{resource['path']}`: {resource['description']} {resource['when_to_read']}".rstrip()
            for resource in entry["resources"]
        )
    staged_dir.mkdir(parents=True, exist_ok=True)
    (staged_dir / "index.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    files = sorted(
        str(path.relative_to(attempt_dir))
        for path in staged_dir.rglob("*")
        if path.is_file()
    )
    return {
        "files": files,
        "catalog": {
            "stage": stage,
            "index": "runtime_skills/index.md",
            "skills": entries,
            "source": source,
        },
    }


def _author_runtime_skill_instruction(attempt_dir: Path) -> str:
    if (attempt_dir / "runtime_skills" / "index.md").is_file():
        return "runtime_skills/index.md first, then only the listed compact skills/resources that apply"
    return "author_input_manifest.json first; no runtime-skills snapshot is available for this historical run"


def _copytree_replace(src: Path, dst: Path) -> None:
    if not src.exists() or not src.is_dir():
        return
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)


def _publish_poster_final(
    staging_dir: Path,
    final_dir: Path,
    checkpoint: Any,
) -> None:
    """Install a validated poster tree and roll it back unless cancel confirms it."""
    publish_artifact_directory(
        staging_dir,
        final_dir,
        artifact_name="poster",
        post_publish=lambda: checkpoint(
            "external_poster.promotion.after_final_publish"
        ),
    )


def _recover_poster_final_promotion(final_dir: Path) -> None:
    recover_artifact_promotion(
        final_dir,
        artifact_name="poster",
        trust_legacy_journal=True,
    )


def _copytree_merge(src: Path, dst: Path) -> None:
    if not src.exists() or not src.is_dir():
        return
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)


def _inline_local_assets(html_path: Path) -> dict[str, Any]:
    """Rewrite local asset references to data URIs so poster.html is portable."""
    result: dict[str, Any] = {
        "enabled": True,
        "inlined_count": 0,
        "stylesheet_count": 0,
        "missing_count": 0,
        "skipped_count": 0,
    }
    try:
        soup = BeautifulSoup(html_path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    except OSError as e:
        result.update({"enabled": False, "error": str(e)})
        return result

    def replace_attr(tag: Any, attr: str, *, base_dir: Path | None = None) -> None:
        value = str(tag.get(attr) or "").strip()
        if not value:
            return
        data_uri, status = _asset_ref_to_data_uri(
            value,
            base_dir=base_dir or html_path.parent,
        )
        if status == "inlined" and data_uri:
            tag[attr] = data_uri
            result["inlined_count"] += 1
        elif status == "missing":
            result["missing_count"] += 1
        elif status == "skipped":
            result["skipped_count"] += 1

    for tag in soup.find_all(["img", "source"]):
        replace_attr(tag, "src")
    for tag in soup.find_all(["video"]):
        replace_attr(tag, "src")
        replace_attr(tag, "poster")
    for tag in soup.find_all(["image"]):
        replace_attr(tag, "href")
        replace_attr(tag, "xlink:href")
        replace_attr(tag, "src")

    for tag in list(soup.find_all("link")):
        rel = tag.get("rel") or []
        rel_tokens = {str(item).lower() for item in rel} if isinstance(rel, list) else {str(rel).lower()}
        if "stylesheet" not in rel_tokens:
            continue
        href = str(tag.get("href") or "").strip()
        css_path = _local_asset_path(href, base_dir=html_path.parent)
        if css_path is None:
            result["skipped_count"] += 1
            continue
        if not css_path.exists() or not css_path.is_file():
            result["missing_count"] += 1
            continue
        try:
            css_text = css_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            result["missing_count"] += 1
            continue
        css_text, css_stats = _inline_css_urls(css_text, base_dir=css_path.parent)
        style = soup.new_tag("style")
        style["data-od-inlined-stylesheet"] = href
        style.string = css_text
        tag.replace_with(style)
        result["stylesheet_count"] += 1
        result["inlined_count"] += css_stats["inlined_count"]
        result["missing_count"] += css_stats["missing_count"]
        result["skipped_count"] += css_stats["skipped_count"]

    for style in soup.find_all("style"):
        css_text = style.get_text()
        rewritten, css_stats = _inline_css_urls(css_text, base_dir=html_path.parent)
        if rewritten != css_text:
            style.string = rewritten
        result["inlined_count"] += css_stats["inlined_count"]
        result["missing_count"] += css_stats["missing_count"]
        result["skipped_count"] += css_stats["skipped_count"]

    for tag in soup.find_all(True):
        inline_style = tag.get("style")
        if not inline_style:
            continue
        rewritten, css_stats = _inline_css_urls(str(inline_style), base_dir=html_path.parent)
        if rewritten != inline_style:
            tag["style"] = rewritten
        result["inlined_count"] += css_stats["inlined_count"]
        result["missing_count"] += css_stats["missing_count"]
        result["skipped_count"] += css_stats["skipped_count"]

    html_path.write_text(str(soup), encoding="utf-8")
    return result


_LOCAL_ASSET_SKIP_RE = re.compile(
    r"^(?:data:|https?:|blob:|javascript:|mailto:|tel:|#)",
    flags=re.IGNORECASE,
)
_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^)'\"]+)\1\s*\)")
_CSS_IMPORT_RE = re.compile(
    r"@import\s+(?:url\(\s*(['\"]?)([^)'\"\s]+)\1\s*\)|(['\"][^'\"]+['\"]))",
    flags=re.IGNORECASE,
)


def _inline_css_urls(css_text: str, *, base_dir: Path) -> tuple[str, dict[str, int]]:
    stats = {"inlined_count": 0, "missing_count": 0, "skipped_count": 0}

    def repl(match: re.Match[str]) -> str:
        quote = match.group(1) or ""
        value = match.group(2).strip()
        data_uri, status = _asset_ref_to_data_uri(value, base_dir=base_dir)
        if status == "inlined" and data_uri:
            stats["inlined_count"] += 1
            return f"url({quote}{data_uri}{quote})"
        if status == "missing":
            stats["missing_count"] += 1
        elif status == "skipped":
            stats["skipped_count"] += 1
        return match.group(0)

    return _CSS_URL_RE.sub(repl, css_text), stats


def _asset_ref_to_data_uri(value: str, *, base_dir: Path) -> tuple[str | None, str]:
    path = _local_asset_path(value, base_dir=base_dir)
    if path is None:
        return None, "skipped"
    if not path.exists() or not path.is_file():
        return None, "missing"
    try:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None, "missing"
    return f"data:{mime};base64,{payload}", "inlined"


def _local_asset_path(value: str, *, base_dir: Path) -> Path | None:
    ref = (value or "").strip().strip("'\"")
    if not ref or _LOCAL_ASSET_SKIP_RE.match(ref) or ref.startswith("/"):
        return None
    path_part = re.split(r"[?#]", ref, maxsplit=1)[0]
    if not path_part:
        return None
    try:
        return (base_dir / unquote(path_part)).resolve()
    except OSError:
        return None


def _direct_canvas(ctx: ToolContext) -> dict[str, int]:
    plan = ctx.state.get("canvas_plan") if isinstance(ctx.state.get("canvas_plan"), dict) else {}
    canvas = plan.get("canvas") if isinstance(plan.get("canvas"), dict) else {}
    return {
        "w_px": max(1, _safe_int(canvas.get("w_px"), default=3072)),
        "h_px": max(1, _safe_int(canvas.get("h_px"), default=1536)),
    }


def _active_canvas_contract(ctx: ToolContext) -> dict[str, Any]:
    plan = ctx.state.get("canvas_plan") if isinstance(ctx.state.get("canvas_plan"), dict) else {}
    canvas = plan.get("canvas") if isinstance(plan.get("canvas"), dict) else {}
    width = max(1, _safe_int(canvas.get("w_px"), default=3072))
    height = max(1, _safe_int(canvas.get("h_px"), default=1536))
    return {
        "preset_id": str(plan.get("preset_id") or ""),
        "w_px": width,
        "h_px": height,
        "dpi": max(1, _safe_int(canvas.get("dpi"), default=96)),
        "aspect_ratio": str(canvas.get("aspect_ratio") or f"{width}:{height}"),
        "color_mode": str(canvas.get("color_mode") or "RGB"),
    }


def _poster_root_scroll_metrics(html_path: Path, canvas: dict[str, int]) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001 - optional guard
        return {"available": False, "warning": f"playwright_unavailable: {type(exc).__name__}: {exc}"}
    cw = max(1, _safe_int(canvas.get("w_px"), default=3072))
    ch = max(1, _safe_int(canvas.get("h_px"), default=1536))
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            except Exception:
                browser = p.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": cw, "height": ch}, device_scale_factor=1)
            page.goto(html_path.resolve().as_uri(), wait_until="load", timeout=15000)
            metrics = page.evaluate(
                """() => {
                  const root = document.querySelector('.paper-poster');
                  if (!root) return {available: true, missingRoot: true};
                  return {
                    available: true,
                    missingRoot: false,
                    clientWidth: root.clientWidth || 0,
                    clientHeight: root.clientHeight || 0,
                    scrollWidth: root.scrollWidth || 0,
                    scrollHeight: root.scrollHeight || 0
                  };
                }"""
            )
            browser.close()
            return metrics if isinstance(metrics, dict) else {"available": False, "warning": "invalid_metrics"}
    except Exception as exc:  # noqa: BLE001 - guard should never break fallback promotion
        return {"available": False, "warning": f"measure_failed: {type(exc).__name__}: {exc}"}


def _typesetting_fit_regressed(
    before: dict[str, Any],
    after: dict[str, Any],
    canvas: dict[str, int],
) -> bool:
    if not before.get("available") or not after.get("available"):
        return False
    if before.get("missingRoot") or after.get("missingRoot"):
        return False
    cw = max(1, _safe_int(canvas.get("w_px"), default=3072))
    ch = max(1, _safe_int(canvas.get("h_px"), default=1536))
    before_overflow = max(
        0,
        _safe_int(before.get("scrollWidth")) - cw,
        _safe_int(before.get("scrollHeight")) - ch,
    )
    after_overflow = max(
        0,
        _safe_int(after.get("scrollWidth")) - cw,
        _safe_int(after.get("scrollHeight")) - ch,
    )
    if before_overflow <= 1 and after_overflow > 1:
        return True
    return after_overflow > before_overflow + 4


def _render_direct_preview(
    *,
    html_path: Path,
    preview_path: Path,
    canvas: dict[str, int],
    ctx: ToolContext,
):
    last_result = None
    for selector in (".paper-poster", ".poster", "main", None):
        result = screenshot_html(
            html_path,
            preview_path,
            viewport_width=int(canvas["w_px"]),
            viewport_height=int(canvas["h_px"]),
            selector=selector,
            full_page=False,
            max_edge=getattr(ctx.settings, "poster_preview_max_edge", 2048),
            timeout_ms=30_000,
        )
        last_result = result
        if result.ok and preview_path.exists():
            return result
    return last_result


_HEADER_COLLAPSE_GUARD_MARKER = "data-autodesign-header-collapse-guard"
_LEGACY_HEADER_COLLAPSE_GUARD_MARKER = "data-designanything-header-collapse-guard"
_HEADER_COLLAPSE_GUARD_CSS = (
    f'<style {_HEADER_COLLAPSE_GUARD_MARKER}="1">'
    ".paper-poster{display:flex!important;flex-direction:column!important;}"
    ".paper-poster>.poster-header,.paper-poster>header{flex:0 0 auto!important;}"
    ".paper-poster>.poster-body,.paper-poster>.poster-columns,.paper-poster>.poster-grid"
    "{flex:1 1 auto!important;min-height:0!important;}"
    "</style>"
)


def _maybe_repair_collapsed_poster_header(html_path: Path, canvas: dict[str, int]) -> dict[str, Any] | None:
    """Repair a starved poster-header grid row in a finalized composite.

    In a fixed-height `display:grid; grid-template-rows:auto minmax(0,1fr)` root the
    `auto` header track can resolve to ~0 when large header assets are inlined.
    The header content is intact but its row collapses, so the
    body paints over it and the poster looks header-less. Detect that specific collapse
    — a grid root whose header box is far shorter than the header's own content — and,
    only then, inject a scoped flex-column override so the header keeps its content
    height. Healthy posters (header rendered at its content height, including ones with
    explicit grid header rows) never match the condition and are left untouched.

    Returns a small record when it patches `html_path`, else None.
    """
    cw = max(1, int(canvas.get("w_px") or 3072))
    ch = max(1, int(canvas.get("h_px") or 1536))
    try:
        html = html_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if any(marker in html for marker in (
        _HEADER_COLLAPSE_GUARD_MARKER,
        _LEGACY_HEADER_COLLAPSE_GUARD_MARKER,
    )):
        return None
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001 - optional dependency guard
        return None
    metrics: dict[str, Any] | None = None
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            except Exception:  # noqa: BLE001
                browser = p.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": cw, "height": ch}, device_scale_factor=1)
            page.goto(html_path.resolve().as_uri(), wait_until="load", timeout=20_000)
            try:
                page.wait_for_function(
                    "Array.from(document.images).every(im=>im.complete && im.naturalHeight>0)",
                    timeout=5_000,
                )
            except Exception:  # noqa: BLE001 - proceed on slow/missing assets
                pass
            metrics = page.evaluate(
                """() => {
                  const root = document.querySelector('.paper-poster');
                  const header = document.querySelector('.poster-header, header');
                  if (!root || !header) return null;
                  const cs = getComputedStyle(root);
                  return {
                    display: cs.display,
                    boxHeight: header.getBoundingClientRect().height,
                    contentHeight: header.scrollHeight,
                  };
                }"""
            )
            browser.close()
    except Exception:  # noqa: BLE001 - never let preview repair break promotion
        return None
    if not isinstance(metrics, dict):
        return None
    display = str(metrics.get("display") or "")
    box = float(metrics.get("boxHeight") or 0.0)
    content = float(metrics.get("contentHeight") or 0.0)
    # Only act on a genuine grid-row collapse: a grid root whose substantial header
    # content is starved into a far shorter box. Conservative ratio avoids false fires.
    if "grid" not in display or content < 100.0 or box >= content * 0.6:
        return None
    if "</head>" in html:
        patched = html.replace("</head>", _HEADER_COLLAPSE_GUARD_CSS + "</head>", 1)
    else:
        patched = re.sub(r"(<body[^>]*>)", r"\1" + _HEADER_COLLAPSE_GUARD_CSS, html, count=1)
        if patched == html:
            patched = _HEADER_COLLAPSE_GUARD_CSS + html
    try:
        html_path.write_text(patched, encoding="utf-8")
    except OSError:
        return None
    record = {"applied": True, "box_height_px": round(box), "content_height_px": round(content)}
    log(
        "designer_author.header_collapse_guard",
        mode="external",
        html=str(html_path),
        box_height_px=record["box_height_px"],
        content_height_px=record["content_height_px"],
    )
    return record


def capture_poster_attempt_candidate(
    *,
    ctx: ToolContext,
    attempt: int,
    max_attempts: int,
    attempt_dir: Path,
    diagnostics: dict[str, Any],
) -> AttemptCandidate:
    poster_path = attempt_dir / "poster.html"
    validation_path = attempt_dir / "attempt_candidate_validation.json"
    atomic_write_json(validation_path, diagnostics)
    preview_path = attempt_dir / "attempt_preview.png"
    preview_html = attempt_dir / ".attempt_preview_materialized.html"
    try:
        shutil.copy2(poster_path, preview_html)
        ensure_poster_katex_document(
            preview_html,
            Path(
                getattr(
                    ctx.settings,
                    "repo_root",
                    Path(__file__).resolve().parents[2],
                )
            ),
            root_selector=".paper-poster",
        )
        preview = _render_direct_preview(
            html_path=preview_html,
            preview_path=preview_path,
            canvas=_direct_canvas(ctx),
            ctx=ctx,
        )
        if not preview_path.exists():
            log(
                "designer_author.attempt_preview_error",
                mode="external",
                attempt=attempt,
                max_attempts=max_attempts,
                attempt_dir=str(attempt_dir),
                warnings=getattr(preview, "warnings", []) if preview is not None else [],
                error="preview image was not produced",
            )
    except Exception as exc:  # noqa: BLE001
        log(
            "designer_author.attempt_preview_error",
            mode="external",
            attempt=attempt,
            max_attempts=max_attempts,
            attempt_dir=str(attempt_dir),
            error=f"{type(exc).__name__}: {exc}"[:300],
        )
    finally:
        preview_html.unlink(missing_ok=True)

    raw_findings: list[Any] = []
    for key in ("findings", "issues"):
        values = diagnostics.get(key)
        if isinstance(values, list):
            raw_findings.extend(values)
    payload = diagnostics.get("payload")
    if isinstance(payload, dict):
        for key in ("findings", "issues"):
            values = payload.get(key)
            if isinstance(values, list):
                raw_findings.extend(values)
    summary = diagnostics.get("summary")
    if isinstance(summary, dict) and summary.get("issue_id"):
        raw_findings.append(summary)
    delivery_issues: list[dict[str, Any]] = []
    for item in raw_findings:
        if isinstance(item, dict):
            delivery_issues.append(item)
        else:
            delivery_issues.append({
                "issue_id": "poster_validation",
                "message": str(item),
            })
    declared_safety_state = str(
        diagnostics.get("candidate_safety_state") or "blocked"
    )
    if not delivery_issues and declared_safety_state != "ready":
        delivery_issues.append({
            "issue_id": "poster_validation",
            "message": "Poster requires validation or repair before delivery.",
        })
    assessment = assess_delivery_issues("poster", delivery_issues)

    dependencies: list[str] = []
    for dirname in ("layers", "assets"):
        root = attempt_dir / dirname
        if root.is_dir():
            dependencies.extend(
                path.resolve().relative_to(attempt_dir.resolve()).as_posix()
                for path in sorted(root.rglob("*"))
                if path.is_file()
            )
    browser_resource_paths = [
        path for path in dependencies if is_browser_preview_resource_path(path)
    ]
    done_marker = attempt_dir / "designer_author_done.json"
    if done_marker.is_file():
        dependencies.append("designer_author_done.json")
    candidate = capture_attempt_candidate(
        run_dir=ctx.run_dir,
        attempt_dir=attempt_dir,
        artifact_type="poster",
        attempt=attempt,
        max_attempts=max_attempts,
        source_path="poster.html",
        dependency_paths=sorted(set(dependencies)),
        preview_paths=["attempt_preview.png"] if preview_path.is_file() else [],
        validation_summary_path=validation_path.name,
        safety_state=assessment.safety_state,
        hard_blockers=list(assessment.hard_blockers),
        warnings=list(assessment.quality_diagnostics),
        browser_resource_paths=browser_resource_paths,
    )
    log(
        "attempt_candidate.available",
        run_id=ctx.run_id,
        artifact_type="poster",
        attempt=attempt,
        max_attempts=max_attempts,
        candidate_id=candidate.candidate_id,
        safety_state=candidate.safety_state,
    )
    return candidate


def promote_selected_attempt(
    ctx: ToolContext,
    candidate: AttemptCandidate,
    *,
    validate_for_delivery: bool = True,
) -> None:
    checkpoint = lambda phase: context_cancellation_checkpoint(ctx, phase)
    checkpoint("external_poster.selected_promotion.start")
    snapshot_poster = ctx.run_dir / candidate.source_relative_path
    work_dir = (
        ctx.run_dir / "attempt_selection_work" / candidate.candidate_id
    )
    checkpoint("external_poster.selected_promotion.before_work_cleanup")
    shutil.rmtree(work_dir, ignore_errors=True)
    checkpoint("external_poster.selected_promotion.before_work_copy")
    shutil.copytree(snapshot_poster.parent, work_dir)
    checkpoint("external_poster.selected_promotion.after_work_copy")
    poster_path = work_dir / snapshot_poster.name
    requested_run_dir = promotion_requested_run_dir(ctx)
    _rewrite_run_local_asset_refs(
        poster_path,
        ctx.run_dir,
        additional_run_dirs=(
            (requested_run_dir,) if requested_run_dir is not None else ()
        ),
    )
    _resolve_layer_asset_placeholders(poster_path)
    author = ExternalDesignerAuthor(ctx.settings, "")
    if validate_for_delivery:
        fresh_feedback = author._direct_final_validation_feedback(
            ctx,
            attempt_index=candidate.attempt,
            attempt_dir=work_dir,
            poster_path=poster_path,
        )
        fresh_payload: dict[str, Any] = {}
        if isinstance(fresh_feedback, dict):
            payload = fresh_feedback.get("payload")
            summary = fresh_feedback.get("summary")
            if isinstance(payload, dict) and payload:
                fresh_payload = copy.deepcopy(payload)
            elif isinstance(summary, dict):
                fresh_payload = copy.deepcopy(summary)
        assessment = assess_delivery_issues(
            "poster",
            _fallback_delivery_issues({"payload": fresh_payload}, None),
        )
        if assessment.safety_state == "blocked":
            raise ValueError("selected Poster candidate failed fresh validation")
        quality_status = assessment.safety_state
        quality_diagnostics = [
            issue.issue_id
            for issue in assessment.quality_diagnostics
        ]
    else:
        quality_status = candidate.safety_state
        quality_diagnostics = [issue.issue_id for issue in candidate.warnings]
    author._promote_direct_final(
        ctx,
        attempt_index=candidate.attempt,
        attempt_dir=work_dir,
        poster_path=poster_path,
        poster_sha256=sha256_file(poster_path),
        acceptance_path="user_selected_attempt",
        quality_status=quality_status,
        quality_diagnostics=quality_diagnostics,
        selection_owned=True,
        _requested_run_dir=requested_run_dir,
        _promotion_candidate_id=candidate.candidate_id,
    )


def _log_designer_author_agent_output(
    *,
    attempt_dir: Path,
    attempt_index: int,
    max_attempts: int,
    invocation: dict[str, Any],
) -> None:
    done_summary = _json_summary_excerpt(attempt_dir / "designer_author_done.json")
    stdout_excerpt = _tail_text_excerpt(attempt_dir / ".designer_author_log.stdout.tmp", limit=1800)
    stderr_excerpt = _tail_text_excerpt(attempt_dir / ".designer_author_log.stderr.tmp", limit=900)
    if not done_summary and not stdout_excerpt and not stderr_excerpt:
        return
    log(
        "designer_author.agent_output",
        mode="external",
        attempt=attempt_index,
        max_attempts=max_attempts,
        attempt_dir=str(attempt_dir),
        status=invocation.get("status"),
        reason=invocation.get("reason"),
        elapsed_s=invocation.get("elapsed_s"),
        done_summary=done_summary,
        stdout_excerpt=stdout_excerpt,
        stderr_excerpt=stderr_excerpt,
    )


def _write_author_quick_brief(ctx: ToolContext, attempt_dir: Path, brief: str) -> bool:
    content = _state_or_attempt_json(ctx, "poster_content_brief", attempt_dir / "poster_content_brief.json")
    contract = _state_or_attempt_json(ctx, "poster_plan_contract", attempt_dir / "poster_plan_contract.json")
    storyboard = _state_or_attempt_json(ctx, "paper_visual_storyboard", attempt_dir / "paper_visual_storyboard.json")

    author_visible_brief = _author_visible_brief(ctx, brief)
    lines: list[str] = [
        "# AutoDesign External Author Quick Brief",
        "",
        "Use this compact brief first. Open the larger JSON files only to verify exact ids or recover missing wording.",
        "",
        "## User Prompt",
        author_visible_brief.strip(),
        "",
        "## Paper Identity",
    ]
    title = _first_text(content, "title", "paper_title") or _first_text(contract, "title", "paper_title")
    authors = content.get("authors")
    affiliations = content.get("affiliations") or content.get("institutions")
    if title:
        lines.append(f"- Title: {title}")
    if isinstance(authors, list) and authors:
        lines.append("- Authors: " + ", ".join(str(item) for item in authors[:12] if str(item).strip()))
    if isinstance(affiliations, list) and affiliations:
        lines.append("- Affiliations: " + ", ".join(str(item) for item in affiliations[:8] if str(item).strip()))

    lines.extend(["", "## Required Structure"])
    slots = _required_slot_ids(contract)
    if slots:
        if _reference_style_contract(ctx):
            lines.append("- Required paper topics to keep as nested subsection/content obligations: " + ", ".join(slots))
        else:
            lines.append("- Required slots: " + ", ".join(slots))
    canvas = _state_or_attempt_json(ctx, "canvas_plan", attempt_dir / "canvas_plan.json")
    if canvas:
        nested_canvas = canvas.get("canvas") if isinstance(canvas.get("canvas"), dict) else {}
        lines.append(
            "- Canvas: "
            + json.dumps(
                {
                    "preset_id": canvas.get("preset_id"),
                    **{
                        key: nested_canvas.get(key)
                        for key in ("w_px", "h_px", "dpi", "aspect_ratio", "color_mode")
                        if key in nested_canvas
                    },
                },
                ensure_ascii=False,
            )
        )

    required_visual_lines = _required_source_visual_brief_lines(contract)
    if required_visual_lines:
        lines.extend(["", "## Required Source Visuals for Attempt 1"])
        lines.extend(required_visual_lines)
        if len(required_visual_lines) >= 2:
            lines.append(
                "- Visual-first composition: place all required paper visuals as substantive body regions and "
                "let multiple original figures/tables carry the story; do not replace their space with prose."
            )
    else:
        lines.extend(["", "## Selected Source Visuals"])
        visual_lines = _selected_visual_brief_lines(contract, storyboard)
        if visual_lines:
            lines.extend(visual_lines)
        else:
            lines.append("- No eligible selected source visuals were summarized; inspect paper_visual_provenance.json.")

    high_priority_lines = _high_priority_visual_brief_lines(contract, storyboard)
    if high_priority_lines:
        lines.extend(["", "## High-Priority Source Visuals"])
        lines.extend(high_priority_lines)

    supplemental_visual_lines = _supplemental_native_visual_brief_lines(contract)
    if supplemental_visual_lines:
        lines.extend(["", "## Supplemental Native Visual Tasks"])
        lines.extend(supplemental_visual_lines)

    lines.extend([
        "",
        "## Nested Paper Topics (not top-level sections)"
        if _reference_style_contract(ctx)
        else "## Section Copy Targets",
    ])
    section_lines = _section_brief_lines(content, contract)
    if section_lines:
        lines.extend(section_lines)
    else:
        lines.append("- Use poster_content_brief.json and paper_evidence_packs/*.md for source-backed claims.")

    color_system = _active_color_system(ctx, content, contract, brief)
    required_color_system = _required_color_system(ctx, content, contract, brief)
    structured_selection = bool(_structured_selected_color_system(ctx))
    color_options = _color_system_options(content, contract, raw_brief=str(ctx.state.get("raw_user_brief") or brief or ""))
    institution_signals = _institution_color_signals_from_sources(content, contract)
    reference_style = _reference_style_contract(ctx)
    lines.extend(["", "## Color System"])
    lines.extend(_color_system_brief_lines(
        color_system,
        options=color_options,
        required=required_color_system,
        institution_signals=institution_signals,
        reference_style=reference_style,
        structured_selection=structured_selection,
    ))

    lines.extend(["", "## Visual Treatment"])
    lines.extend(_aesthetic_contract_lines(content, contract))

    if reference_style:
        lines.extend(["", "## Reference Style"])
        lines.extend(_reference_style_brief_lines(
            _reference_style_for_author(ctx),
            structured_selection=structured_selection,
        ))
        lines.extend(["", "## Reference Layout"])
        lines.append(_reference_layout_authoring_contract(ctx))

    lines.extend([
        "",
        "## Hard Gates",
        (
            f"- Write a complete poster.html with a .paper-poster root on the fixed "
            f"{_active_canvas_contract(ctx)['w_px']}x{_active_canvas_contract(ctx)['h_px']} active canvas."
        ),
        "- Use only local source assets from layers/. Do not use remote, data:, file:, stock, or generated images.",
        "- Header identity area is limited to exactly these three visible paper-identity rows: paper title, author list, and school/institution/company names.",
    ])
    lines.extend(_identity_header_authoring_contract(ctx))
    lines.extend([
        "- Do not add a fourth header/meta/subtitle row or side identity rail. Do not put any other visible content in the header: no logos, image badges, icons, QR codes, venue/year text, conference names, arXiv/archive labels, citation/contact text, project/code/resource links, topic badges, method slogans, contribution bullets, benchmark claims, source figures/tables, captions, or explanatory prose. If venue, project, code, resource, citation, or contact fields are available, omit them from the header; users can add them after export.",
        "- Every placed source figure/table/image must include matching data-source-id and data-layer-id.",
        "- Put each source figure/table/image in its own source-flow-unit or figure-flow-unit with local explanatory text.",
        _SOURCE_FLOW_LIST_GUTTER_CONTRACT,
        "- Selected or available ingest_table_* evidence must be placed as the original PDF table crop; compact native summaries may supplement it.",
        _color_authoring_contract(ctx),
        _typography_authoring_contract(ctx),
        _EDITORIAL_LEAD_KEY_AUTHORING_CONTRACT,
        _table_authoring_contract(ctx),
        _formula_authoring_contract(ctx),
        "- Write paper equations as TeX inside \\(...\\) or \\[...\\], preferably in .formula or math-block elements; do not add MathJax/KaTeX scripts yourself.",
        "- Keep TeX formulas out of narrow metric/stage/chip/badge cells; use plain text in those cells and place full equations in wide formula blocks or wide table rows.",
        "- Keep visible text as poster content only; no process labels, planning notes, or provenance boilerplate.",
        "- Write designer_author_done.json when poster.html is complete.",
        "",
    ])
    (attempt_dir / "author_quick_brief.md").write_text("\n".join(lines), encoding="utf-8")
    return True


def _synchronize_staged_color_system(ctx: ToolContext, attempt_dir: Path, brief: str) -> dict[str, Any]:
    content_path = attempt_dir / "poster_content_brief.json"
    contract_path = attempt_dir / "poster_plan_contract.json"
    content = _state_or_attempt_json(ctx, "poster_content_brief", content_path)
    contract = _state_or_attempt_json(ctx, "poster_plan_contract", contract_path)
    recommended_color_system = _active_color_system(ctx, content, contract, brief)
    if not recommended_color_system:
        return {}
    required_color_system = _required_color_system(ctx, content, contract, brief)
    structured_color_system = _structured_selected_color_system(ctx)
    structured_selection = bool(structured_color_system)
    options = _color_system_options(content, contract, raw_brief=str(ctx.state.get("raw_user_brief") or brief or ""))
    reference_color_system = _reference_color_system(ctx)
    if reference_color_system:
        recommended_color_system = _reference_scoped_color_system(recommended_color_system)
        required_color_system = _reference_scoped_color_system(required_color_system)
        reference_color_system = _reference_scoped_color_system(reference_color_system)
        if structured_selection:
            options = [recommended_color_system]
        else:
            options = []
            seen_reference_options: set[str] = set()
            for option in (required_color_system, recommended_color_system, reference_color_system):
                option_id = str(option.get("palette_id") or "") if isinstance(option, dict) else ""
                if option_id and option_id not in seen_reference_options:
                    options.append(option)
                    seen_reference_options.add(option_id)
    elif structured_selection:
        options = [recommended_color_system]
    signals = _institution_color_signals_from_sources(content, contract)
    aesthetic_contract = _default_aesthetic_contract()
    if isinstance(content.get("aesthetic_contract"), dict):
        aesthetic_contract = dict(content.get("aesthetic_contract") or {})
    if isinstance(contract.get("aesthetic_contract"), dict):
        aesthetic_contract = dict(contract.get("aesthetic_contract") or {})
    author_reference_style = _reference_style_for_author(ctx)
    if isinstance(author_reference_style.get("aesthetic_contract"), dict):
        aesthetic_contract = {
            **aesthetic_contract,
            **dict(author_reference_style.get("aesthetic_contract") or {}),
        }
        active_palette_id = str(recommended_color_system.get("palette_id") or "")
        aesthetic_contract["palette_usage_policy"] = (
            f"Use the active run-scoped palette `{active_palette_id}` and its exact CSS variables inside the reference-owned visual grammar. "
            "Source figures and source table crops keep their original paper colors."
        )
    if author_reference_style:
        atomic_write_json(
            attempt_dir / "reference_style_contract.json",
            author_reference_style,
        )
    reference_typography = _reference_typography_contract(ctx)
    reference_layout = _reference_layout_contract(ctx)
    if content:
        content = _scrub_stale_logo_policy(content, background_key="background_contract")
        content = {
            **content,
            "color_system": recommended_color_system,
            "recommended_color_system": recommended_color_system,
            "color_system_options": options,
            "institution_color_signals": signals,
            "aesthetic_contract": aesthetic_contract,
            "reference_style_id": _reference_style_id(ctx),
        }
        if reference_typography:
            content_typography = dict(content.get("typography_contract") or {})
            content_typography.pop("times_new_roman_family_ratio_required", None)
            content["typography_contract"] = {**content_typography, **reference_typography}
        if reference_layout:
            content = _apply_reference_layout_to_content(content, reference_layout)
        if required_color_system:
            content["required_color_system"] = required_color_system
        ctx.state["poster_content_brief"] = content
        atomic_write_json(content_path, content)
    if contract:
        contract = _scrub_stale_logo_policy(contract, background_key="background_policy")
        contract = {
            **contract,
            "color_system": recommended_color_system,
            "recommended_color_system": recommended_color_system,
            "color_system_options": options,
            "institution_color_signals": signals,
            "aesthetic_contract": aesthetic_contract,
            "reference_style_id": _reference_style_id(ctx),
        }
        if reference_typography:
            contract_typography = dict(contract.get("typography_targets") or {})
            for key in ("times_new_roman_family_ratio_required", "allowed_serif_fallbacks"):
                contract_typography.pop(key, None)
            contract["typography_targets"] = {**contract_typography, **reference_typography}
        if reference_layout:
            contract = _apply_reference_layout_to_contract(contract, reference_layout)
            contract["authored_html_skeleton"] = _reference_authored_html_skeleton(
                ctx,
                blueprint_path=attempt_dir / "reference_style_blueprint.html",
            )
        if required_color_system:
            contract["required_color_system"] = required_color_system
        ctx.state["poster_plan_contract"] = contract
        atomic_write_json(contract_path, contract)
    return {
        "color_system": recommended_color_system,
        "recommended_color_system": recommended_color_system,
        "required_color_system": required_color_system,
        "color_system_options": options,
        "institution_color_signals": signals,
        "aesthetic_contract": aesthetic_contract,
        "reference_style_contract": author_reference_style,
        "typography_contract": reference_typography,
        "reference_layout_contract": reference_layout,
    }


def _reference_scoped_color_system(value: Any) -> dict[str, Any]:
    color = dict(value) if isinstance(value, dict) else {}
    if not color:
        return {}
    color["usage_contract"] = (
        "Use these colors inside the active reference poster's visual grammar. The reference contract owns header, "
        "section, table, formula, surface, and spacing treatment; do not infer the normal AutoDesign top rule, "
        "filled section bars, dividers, or panel styling from this palette object."
    )
    return color


def _scrub_stale_logo_policy(data: dict[str, Any], *, background_key: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        return data
    background = data.get(background_key)
    if not isinstance(background, dict) or "logo_policy" not in background:
        return data
    return {
        **data,
        background_key: {
            key: value
            for key, value in background.items()
            if key != "logo_policy"
        },
    }


def _structured_selected_color_system(ctx: ToolContext) -> dict[str, Any]:
    return _normalize_author_color_system(
        (ctx.state or {}).get("required_color_system")
    )


def _active_color_system(
    ctx: ToolContext,
    content: dict[str, Any],
    contract: dict[str, Any],
    brief: str,
) -> dict[str, Any]:
    structured = _structured_selected_color_system(ctx)
    if structured:
        return structured
    raw_brief = str(ctx.state.get("raw_user_brief") or brief or "")
    if explicit_academic_color_system is not None:
        try:
            explicit = explicit_academic_color_system(raw_brief=raw_brief)
        except Exception:
            explicit = {}
        normalized = _normalize_author_color_system(explicit)
        if normalized:
            return normalized
    reference_color = _reference_color_system(ctx)
    if reference_color:
        return reference_color
    existing = _existing_author_color_system(content, contract)
    ranked_options = _canonical_author_color_options(content, contract, raw_brief=raw_brief)
    candidate = ranked_options[0] if ranked_options else {}
    if existing:
        if _should_override_existing_color_system(existing, candidate, ranked_options, raw_brief):
            return candidate
        return existing
    normalized_candidate = _normalize_author_color_system(candidate)
    if normalized_candidate and int(normalized_candidate.get("selection_score") or 0) > 0:
        return normalized_candidate
    if active_academic_color_system is not None:
        try:
            active = active_academic_color_system(
                content,
                contract,
                raw_brief=raw_brief,
                manifest=_author_color_selection_manifest(content, contract),
            )
        except Exception:
            active = {}
        normalized = _normalize_author_color_system(active)
        if normalized:
            return normalized
    if select_academic_color_system is not None:
        try:
            selected = select_academic_color_system(
                raw_brief=raw_brief,
                manifest=_author_color_selection_manifest(content, contract),
            )
        except Exception:
            selected = {}
        normalized = _normalize_author_color_system(selected)
        if normalized:
            return normalized
    if normalized_candidate:
        return normalized_candidate
    return _fallback_author_color_system()


def _required_color_system(
    ctx: ToolContext,
    content: dict[str, Any],
    contract: dict[str, Any],
    brief: str,
) -> dict[str, Any]:
    structured = _structured_selected_color_system(ctx)
    if structured:
        return structured
    raw_brief = str(ctx.state.get("raw_user_brief") or brief or "")
    if explicit_academic_color_system is not None:
        try:
            explicit = explicit_academic_color_system(raw_brief=raw_brief)
        except Exception:
            explicit = {}
        normalized = _normalize_author_color_system(explicit)
        if normalized:
            return normalized
    reference_color = _reference_color_system(ctx)
    if reference_color:
        return reference_color
    for source in (content, contract):
        required = source.get("required_color_system") if isinstance(source, dict) else None
        normalized = _normalize_author_color_system(required)
        if normalized:
            return normalized
    return {}


def _color_system_options(content: dict[str, Any], contract: dict[str, Any], *, raw_brief: str = "") -> list[dict[str, Any]]:
    canonical = _canonical_author_color_options(content, contract, raw_brief=raw_brief)
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for option in canonical:
        palette_id = str(option.get("palette_id") or "").strip()
        if palette_id and palette_id not in seen:
            merged.append(option)
            seen.add(palette_id)
    for source in (content, contract):
        raw_options = source.get("color_system_options") if isinstance(source, dict) else None
        options = _normalize_author_color_options(raw_options)
        for option in options:
            palette_id = str(option.get("palette_id") or "").strip()
            if palette_id and palette_id not in seen:
                merged.append(option)
                seen.add(palette_id)
    if merged:
        return merged
    return [_fallback_author_color_system()]


def _canonical_author_color_options(
    content: dict[str, Any],
    contract: dict[str, Any],
    *,
    raw_brief: str = "",
) -> list[dict[str, Any]]:
    manifest = _author_color_selection_manifest(content, contract)
    raw_brief = str(raw_brief or "").strip() or _first_text(manifest, "raw_user_brief", "brief", "title")
    text_units = (
        manifest.get("recommended_text_units")
        if isinstance(manifest.get("recommended_text_units"), dict)
        else None
    )
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_options(options: Any) -> None:
        for option in _normalize_author_color_options(options):
            palette_id = str(option.get("palette_id") or "").strip()
            if palette_id and palette_id not in seen:
                merged.append(option)
                seen.add(palette_id)

    if rank_academic_color_system_options is not None:
        try:
            options = rank_academic_color_system_options(
                raw_brief=raw_brief,
                manifest=manifest,
                recommended_text_units=text_units,
            )
        except Exception:
            options = []
        add_options(options)
    if academic_color_system_options is not None:
        try:
            options = academic_color_system_options()
        except Exception:
            options = []
        add_options(options)
    if merged:
        return merged
    return [_fallback_author_color_system()]


def _author_color_selection_manifest(content: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    manifest: dict[str, Any] = {}
    for source in (contract, content):
        if isinstance(source, dict):
            manifest.update(source)
    return manifest


_RAW_TONE_OR_COLOR_INTENT_RE = re.compile(
    r"\b(?:palette|color|colour|tone|warm|warmer|cool|cooler|soft|softer|muted|"
    r"restrained|formal|bright|brighter|calm|fresh|classic|less\s+blue|more\s+blue)\b|"
    r"配色|颜色|色板|色系|暖色|冷色|柔和|明亮|克制",
    re.I,
)


def _existing_author_color_system(content: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    for source in (content, contract):
        value = source.get("color_system") if isinstance(source, dict) else None
        normalized = _normalize_author_color_system(value)
        if normalized:
            return normalized
    return {}


def _should_override_existing_color_system(
    existing: dict[str, Any],
    candidate: dict[str, Any],
    ranked_options: list[dict[str, Any]],
    raw_brief: str,
) -> bool:
    candidate = _normalize_author_color_system(candidate)
    existing = _normalize_author_color_system(existing)
    candidate_id = str(candidate.get("palette_id") or "").strip()
    existing_id = str(existing.get("palette_id") or "").strip()
    if not candidate_id or not existing_id or candidate_id == existing_id:
        return False
    candidate_raw = int(candidate.get("raw_selection_score") or 0)
    candidate_total = int(candidate.get("selection_score") or 0)
    existing_ranked = _ranked_author_color_option_by_id(ranked_options, existing_id)
    existing_raw = int(existing_ranked.get("raw_selection_score") or existing.get("raw_selection_score") or 0)
    existing_total = int(existing_ranked.get("selection_score") or existing.get("selection_score") or 0)
    if candidate_raw <= 0:
        return False
    if _RAW_TONE_OR_COLOR_INTENT_RE.search(str(raw_brief or "")):
        return candidate_raw >= 3 and candidate_raw > existing_raw
    return candidate_raw >= 6 and candidate_total >= existing_total + 3


def _ranked_author_color_option_by_id(options: list[dict[str, Any]], palette_id: str) -> dict[str, Any]:
    target = str(palette_id or "").strip()
    for option in options or []:
        if str(option.get("palette_id") or "").strip() == target:
            return _normalize_author_color_system(option)
    return {}


def _normalize_author_color_options(raw_options: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_options, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_options:
        normalized = _normalize_author_color_system(item)
        palette_id = str(normalized.get("palette_id") or "").strip()
        if not normalized or not palette_id or palette_id in seen:
            continue
        seen.add(palette_id)
        out.append(normalized)
    return out


def _institution_color_signals_from_sources(
    content: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    for source in (content, contract):
        value = source.get("institution_color_signals") if isinstance(source, dict) else None
        if isinstance(value, dict) and value:
            return value
    organizations: list[str] = []
    for source in (content, contract):
        for key in ("affiliations", "institutions"):
            values = source.get(key) if isinstance(source, dict) else None
            if isinstance(values, list):
                organizations.extend(str(item).strip() for item in values if str(item or "").strip())
    organizations = list(dict.fromkeys(organizations))[:12]
    return {
        "organizations": organizations,
        "signal_strength": "strong" if len(organizations) == 1 else "mixed" if organizations else "none",
        "source": "paper_identity",
        "selection_guidance": (
            "Use institution/company/school names only as soft color associations; "
            "do not fetch logos, official brand colors, or brand assets."
        ),
    }


def _normalize_author_color_system(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized = dict(value)
    allowed = _author_color_allowed_hexes(normalized)
    if not allowed:
        return {}
    normalized["allowed_hexes"] = allowed
    return normalized


def _author_color_allowed_hexes(color_system: dict[str, Any]) -> list[str]:
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


def _fallback_author_color_system() -> dict[str, Any]:
    roles = {
        "background": "#FFFFFF",
        "text": "#21181B",
        "primary": "#C1121F",
        "secondary": "#F7DEE1",
        "accent": "#C1121F",
        "header_text": "#FFFFFF",
        "bar": "#C1121F",
    }
    css_variables = {
        "--poster-bg": roles["background"],
        "--poster-text": roles["text"],
        "--poster-primary": roles["primary"],
        "--poster-secondary": roles["secondary"],
        "--poster-accent": roles["accent"],
        "--poster-header-text": roles["header_text"],
        "--poster-bar": roles["bar"],
    }
    return {
        "version": 1,
        "palette_id": "bright_cobalt",
        "palette_name": "Cardinal Red",
        "use_when": "formal academic poster fallback",
        "selection_reason": "fallback academic palette",
        "roles": roles,
        "css_variables": css_variables,
        "allowed_hexes": list(dict.fromkeys(roles.values())),
        "usage_contract": (
            "Use the selected palette sparingly. The identity header uses the "
            "fixed white header with a single top accent rule only; keep panel "
            "interiors and table cells white or neutral. Source wrapper DOM boxes "
            "may exist for measurement, but their borders must be transparent with "
            "no visible outline or shadow."
        ),
    }


def _default_aesthetic_contract() -> dict[str, str]:
    return {
        "canvas_policy": "white or near-white academic canvas; no tinted full-board backgrounds",
        "palette_usage_policy": (
            "Use the selected palette sparingly. The identity header uses the "
            "fixed white/near-white treatment with a single top accent rule "
            "only."
        ),
        "section_surface_policy": (
            "Use compact filled primary section heading bands with white text, "
            "while keeping section bodies and panel interiors white/neutral; do "
            "not fill panels with secondary tints or wrap each panel in a colored box."
        ),
        "header_surface_policy": (
            "Use the fixed identity-header style: white/near-white header with "
            "a single top accent rule only. Do not use bottom header rules, "
            "filled title bands, four-sided outlines, or mixed header styles "
            "for new paper posters."
        ),
        "table_surface_policy": (
            "Native tables use white cells and booktabs-like horizontal rules; no "
            "decorative zebra striping or saturated headers."
        ),
            "source_wrapper_policy": "Source figure/table wrappers stay white with transparent borders only; keep the DOM box for measurement but do not show visible wrapper outlines or shadows.",
        "color_dominance_policy": "Most of the poster should read as paper content and whitespace, not color blocks.",
    }


def _reference_style_contract(ctx: ToolContext) -> dict[str, Any]:
    run_dir = getattr(ctx, "run_dir", None)
    has_reference_metadata = bool(
        ctx.state.get("reference_poster")
        or ctx.state.get("reference_poster_path")
        or (
            run_dir is not None
            and (Path(run_dir) / "reference_poster" / "reference_source_metadata.json").exists()
        )
    )
    if not has_reference_metadata:
        return {}
    value = ctx.state.get("reference_style_contract")
    if not isinstance(value, dict) or not value:
        if run_dir is None:
            return {}
        path = Path(run_dir) / "reference_style_contract.json"
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    if not isinstance(value, dict) or (
        _safe_int(value.get("version"), default=0) not in {3, 4}
        or str(value.get("transfer_mode") or "") != "reference_first_reconstruction"
        or not str(value.get("style_reference_id") or "").strip()
        or not isinstance(value.get("style_tokens"), dict)
    ):
        return {}
    ctx.state["reference_style_contract"] = value
    return value


def _author_visible_brief(ctx: ToolContext, brief: str) -> str:
    raw = str(ctx.state.get("raw_user_brief") or "").strip()
    visible = (
        raw
        if _reference_style_contract(ctx) and raw
        else str(brief or "")
    )
    return _strip_runtime_skill_context(visible)


def _strip_runtime_skill_context(value: str) -> str:
    """Keep runner-injected runtime guidance out of user-visible external prompts."""

    pattern = re.compile(
        r"(?ms)^## (?:AutoDesign|DesignAnything) Runtime Skills Context \([^\n]+\)\n.*?(?:\n---\n|\Z)"
    )
    return pattern.sub("", str(value or "")).strip()


def _reference_typography_contract(ctx: ToolContext) -> dict[str, Any]:
    reference = _reference_style_contract(ctx)
    value = reference.get("typography_contract") if isinstance(reference, dict) else None
    return dict(value) if isinstance(value, dict) and value else {}


def _reference_layout_contract(ctx: ToolContext) -> dict[str, Any]:
    reference = _reference_style_contract(ctx)
    tokens = reference.get("style_tokens") if isinstance(reference.get("style_tokens"), dict) else {}
    columns = tokens.get("column_structure") if isinstance(tokens.get("column_structure"), dict) else {}
    if not columns:
        return {}
    per_column = columns.get("major_sections_per_column")
    if not isinstance(per_column, list) or not 2 <= len(per_column) <= 6:
        return {}
    normalized = [max(1, min(3, _safe_int(item, default=1))) for item in per_column]
    return {
        "source": "reference_poster",
        "layout_mode": str(columns.get("layout_mode") or "equal_columns"),
        "region_count": len(normalized),
        "major_section_count": sum(normalized),
        "major_sections_per_column": normalized,
        "subsection_treatment": str(columns.get("subsection_treatment") or "inline_colored_label"),
        "header_treatment": copy.deepcopy(tokens.get("header_treatment") or {}),
        "lead_band": copy.deepcopy(tokens.get("lead_band") or {}),
        "section_heading_treatment": copy.deepcopy(tokens.get("section_heading_treatment") or {}),
        "section_structure": copy.deepcopy(tokens.get("section_structure") or {}),
        "layout_rhythm": copy.deepcopy(tokens.get("layout_rhythm") or {}),
        "chrome_treatment": copy.deepcopy(tokens.get("chrome_treatment") or {}),
        "table_treatment": copy.deepcopy(tokens.get("table_treatment") or {}),
        "formula_treatment": copy.deepcopy(tokens.get("formula_treatment") or {}),
        "top_level_dom_contract": (
            "Each reference-owned .poster-column body region must contain exactly the corresponding number "
            "of direct-child .poster-section elements; additional paper topics are nested subsections inside them."
        ),
    }


def _apply_reference_layout_to_content(
    content: dict[str, Any],
    reference_layout: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(content)
    updated["reference_layout_contract"] = copy.deepcopy(reference_layout)
    updated["background_contract"] = _reference_background_contract(
        content.get("background_contract"), reference_layout
    )
    updated["reference_archetype_skeleton"] = _reference_archetype_skeleton(reference_layout)
    updated["native_reference_targets"] = _reference_native_targets(
        content.get("native_reference_targets"), reference_layout
    )
    sections = content.get("sections")
    if isinstance(sections, list):
        updated["sections"] = [
            {
                **item,
                "reference_hierarchy_role": "nested_subsection_topic",
                "top_level_major_section": False,
            }
            if isinstance(item, dict) else item
            for item in sections
        ]
    for key in ("panel_plan", "editorial_column_plan"):
        updated[key] = _reference_column_plan(content.get(key), reference_layout)
    return updated


def _apply_reference_layout_to_contract(
    contract: dict[str, Any],
    reference_layout: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(contract)
    updated["reference_layout_contract"] = copy.deepcopy(reference_layout)
    updated["background_policy"] = _reference_background_contract(
        contract.get("background_policy"), reference_layout
    )
    updated["reference_archetype_skeleton"] = _reference_archetype_skeleton(reference_layout)
    updated["layout_storyboard_targets"] = _reference_layout_storyboard_targets(
        contract.get("layout_storyboard_targets"), reference_layout
    )
    if isinstance(contract.get("native_reference_targets"), dict):
        updated["native_reference_targets"] = _reference_native_targets(
            contract.get("native_reference_targets"), reference_layout
        )
    required = contract.get("required_sections")
    if isinstance(required, list):
        updated["required_sections"] = [
            {
                **item,
                "reference_hierarchy_role": "nested_subsection_topic",
                "top_level_major_section": False,
            }
            if isinstance(item, dict) else item
            for item in required
        ]
    updated["editorial_column_plan"] = _reference_column_plan(
        contract.get("editorial_column_plan"), reference_layout
    )
    flow = dict(contract.get("editorial_flow_contract") or {})
    capacity = dict(flow.get("column_capacity_contract") or {})
    per_column = list(reference_layout.get("major_sections_per_column") or [1, 1, 1])
    if all(_safe_int(item, default=1) == 1 for item in per_column):
        capacity.update({
            "typical_sections_per_column": "1",
            "section_count_range_per_column": {"min": 1, "max": 1},
            "max_sections_per_column": 1,
        })
    flow["column_capacity_contract"] = capacity
    flow["reference_layout_contract"] = copy.deepcopy(reference_layout)
    hard_rules = [
        str(item) for item in flow.get("hard_rules") or []
        if "one to three .poster-section" not in str(item).lower()
        and "dark section bars" not in str(item).lower()
    ]
    hard_rules.insert(
        0,
        "Reference layout is authoritative: use exactly one direct-child .poster-section per reference-owned "
        f"body region across {len(per_column)} regions; keep all required paper topics nested inside them."
        if all(_safe_int(item, default=1) == 1 for item in per_column)
        else f"Reference layout is authoritative: direct-child major sections per body region must be {per_column}.",
    )
    flow["hard_rules"] = hard_rules
    flow["section_seed_titles_role"] = "nested_subsection_topics"
    updated["editorial_flow_contract"] = flow
    return updated


def _reference_background_contract(value: Any, reference_layout: dict[str, Any]) -> dict[str, Any]:
    existing = dict(value) if isinstance(value, dict) else {}
    header = reference_layout.get("header_treatment") if isinstance(reference_layout.get("header_treatment"), dict) else {}
    section = reference_layout.get("section_heading_treatment") if isinstance(reference_layout.get("section_heading_treatment"), dict) else {}
    structure = reference_layout.get("section_structure") if isinstance(reference_layout.get("section_structure"), dict) else {}
    existing.update({
        "default": "reference-owned academic canvas",
        "use_generated_background": False,
        "reference_owned": True,
        "structure": (
            f"header={header.get('mode') or 'open_white'}, header_top_rule={header.get('top_rule') or 'none'}, "
            f"section_heading={section.get('mode') or 'text_only'}, inter_section_dividers={structure.get('inter_section_dividers') or 'none'}"
        ),
    })
    return existing


def _reference_archetype_skeleton(reference_layout: dict[str, Any]) -> dict[str, Any]:
    per_column = list(reference_layout.get("major_sections_per_column") or [1, 1, 1])
    region_count = len(per_column)
    layout_mode = str(reference_layout.get("layout_mode") or "equal_columns")
    total = sum(_safe_int(item, default=1) for item in per_column)
    section = reference_layout.get("section_heading_treatment") if isinstance(reference_layout.get("section_heading_treatment"), dict) else {}
    return {
        "profile": "reference_first_reconstruction",
        "reference_archetype": "user_supplied_reference_poster",
        "layout": {
            "identity_header": "Match reference_style_blueprint.html; target title, authors, and institutions only.",
            "body": (
                f"exactly {region_count} reference-owned body regions using `{layout_mode}` geometry with "
                f"direct-child major section counts {per_column}"
            ),
            "section_style": f"reference-owned `{section.get('mode') or 'text_only'}` major headings; nested topics are h3/inline labels",
        },
        "hard_constraints": {
            "column_count": region_count,
            "layout_mode": layout_mode,
            "min_sections_total": total,
            "target_sections_total": total,
            "major_sections_per_column": per_column,
            "source_assets_are_subjects": True,
            "visible_figcaption_allowed": False,
            "panel_content_plan_allowed": False,
        },
        "forbid": [
            "default AutoDesign poster skin",
            "promoting nested paper topics into extra major sections",
            "reference content, logos, QR codes, figures, or claims",
        ],
        "source_flow_unit": "Keep each target-paper source figure/table and its local readout in one editable source-flow unit.",
    }


def _reference_layout_storyboard_targets(value: Any, reference_layout: dict[str, Any]) -> dict[str, Any]:
    existing = dict(value) if isinstance(value, dict) else {}
    per_column = list(reference_layout.get("major_sections_per_column") or [1, 1, 1])
    total = sum(_safe_int(item, default=1) for item in per_column)
    section = reference_layout.get("section_heading_treatment") if isinstance(reference_layout.get("section_heading_treatment"), dict) else {}
    existing.update({
        "editorial_reference_layout": (
            f"Use the staged reference blueprint with direct-child major section counts {per_column}; "
            "all other paper topics are nested subsections in normal flow."
        ),
        "column_flow_contract": {
            "column_count": len(per_column),
            "layout_mode": str(reference_layout.get("layout_mode") or "equal_columns"),
            "min_sections_total": total,
            "target_sections_total": total,
            "major_sections_per_column": per_column,
            "section_bar_required": str(section.get("mode") or "") == "filled_band",
            "allowed_roots": ["poster-columns", "poster-column", "poster-section"],
            "disallowed_roots": ["poster-grid", "flow-panel six-pack", "fixed-lane", "panel_content_plan"],
            "repair": "Restore the exact reference-owned major-section count and keep paper topics nested inside those sections.",
        },
    })
    return existing


def _reference_native_targets(value: Any, reference_layout: dict[str, Any]) -> dict[str, Any]:
    existing = dict(value) if isinstance(value, dict) else {}
    per_column = list(reference_layout.get("major_sections_per_column") or [1, 1, 1])
    total = sum(_safe_int(item, default=1) for item in per_column)
    existing.update({
        "profile": "reference_first_reconstruction",
        "column_count": len(per_column),
        "layout_mode": str(reference_layout.get("layout_mode") or "equal_columns"),
        "min_sections_total": total,
        "target_sections_total": total,
        "major_sections_per_column": per_column,
        "section_topics_role": "nested_subsection_content",
    })
    return existing


def _reference_authored_html_skeleton(
    ctx: ToolContext,
    *,
    blueprint_path: Path | None = None,
) -> dict[str, Any]:
    path = blueprint_path or (ctx.run_dir / "reference_style_blueprint.html")
    if not path.exists():
        return {
            "version": 3,
            "source": "reference_style_blueprint.html",
            "html": "",
            "css": "",
            "instruction": "Read the staged reference_style_blueprint.html as the only visual skeleton.",
        }
    try:
        soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    except OSError:
        return {}
    css = "\n".join(tag.get_text("\n", strip=False) for tag in soup.find_all("style"))
    root = soup.select_one(".reference-style-blueprint")
    return {
        "version": 3,
        "source": "reference_style_blueprint.html",
        "html": str(root) if isinstance(root, Tag) else "",
        "css": css,
        "instruction": (
            "Use this sanitized reference-owned DOM/CSS scaffold. Replace placeholders with target-paper content, "
            "preserve the exact top-level structure, and do not import the normal AutoDesign authored skeleton."
        ),
    }


def _reference_column_plan(value: Any, reference_layout: dict[str, Any]) -> list[dict[str, Any]]:
    default_roles = (
        ("left_story", "motivation_and_context"),
        ("middle_method", "method_flow"),
        ("right_results", "results_and_analysis"),
        ("supporting_analysis", "supporting_analysis"),
        ("takeaway_region", "limitations_and_takeaways"),
        ("reference_region_6", "paper_specific_support"),
    )
    per_column = list(reference_layout.get("major_sections_per_column") or [1, 1, 1])
    defaults = default_roles[:len(per_column)]
    existing = value if isinstance(value, list) else []
    out: list[dict[str, Any]] = []
    for index, (column_id, role) in enumerate(defaults):
        source = existing[index] if index < len(existing) and isinstance(existing[index], dict) else {}
        out.append({
            **source,
            "column_id": str(source.get("column_id") or column_id),
            "role": str(source.get("role") or role),
            "major_section_count": int(per_column[index]),
            "section_targets_role": "nested_subsection_topics",
            "layout": (
                "Match this region's geometry from reference_style_blueprint.html; organize listed targets as "
                "nested subsections in normal flow."
            ),
        })
    return out


def _typography_fixed_values_from_contract(contract: dict[str, Any]) -> dict[str, Any]:
    if not contract:
        return _academic_typography_fixed_values()
    family = str(contract.get("font_family") or _ACADEMIC_TYPOGRAPHY_FIXED_VALUES["poster_root"]["font_family"])
    return {
        "poster_root": {"font_family": family},
        "title": {
            "font_size_px": _safe_int(contract.get("title_font_size_px"), default=56),
            "line_height": 1.08,
            "font_weight": _safe_int(contract.get("title_weight"), default=600),
        },
        "identity_rows": {
            "font_size_px": _safe_int(contract.get("identity_rows_font_size_px"), default=28),
            "line_height": 1.16,
            "font_weight": _safe_int(contract.get("identity_weight"), default=400),
        },
        "section_heading": {
            "font_size_px": _safe_int(contract.get("section_heading_font_size_px"), default=36),
            "line_height": 1.10,
            "font_weight": _safe_int(contract.get("section_heading_weight"), default=700),
        },
        "subsection_heading": {
            "font_size_px": _safe_int(
                contract.get("subsection_heading_font_size_px") or contract.get("body_font_size_px"),
                default=24,
            ),
            "line_height": 1.18,
            "font_weight": _safe_int(contract.get("section_heading_weight"), default=700),
        },
        "body": {
            "font_size_px": _safe_int(contract.get("body_font_size_px"), default=24),
            "line_height": 1.18,
            "font_weight": _safe_int(contract.get("body_weight"), default=400),
        },
        "readout": {
            "font_size_px": _safe_int(contract.get("readout_font_size_px"), default=24),
            "line_height": 1.18,
            "font_weight": _safe_int(contract.get("body_weight"), default=400),
        },
        "table_text": {
            "font_size_px": _safe_int(contract.get("table_text_font_size_px"), default=24),
            "line_height": 1.18,
            "font_weight": _safe_int(contract.get("body_weight"), default=400),
        },
        "caption_label": {
            "font_size_px": _safe_int(
                contract.get("caption_label_font_size_px") or contract.get("caption_font_size_px"),
                default=20,
            ),
            "line_height": 1.18,
            "font_weight": _safe_int(contract.get("body_weight"), default=400),
        },
        "lead_band": {
            "font_size_px": _safe_int(contract.get("lead_band_font_size_px"), default=38),
            "line_height": 1.12,
            "font_weight": _safe_int(contract.get("section_heading_weight"), default=700),
        },
        "font_size_tolerance_px": float(contract.get("font_size_tolerance_px") or 1.5),
    }


def _typography_required_system(ctx: ToolContext) -> dict[str, Any]:
    reference = _reference_typography_contract(ctx)
    if not reference:
        return {
            "source": "default_academic_contract",
            "primary_font_family": "Times New Roman",
            "font_family": '"Times New Roman", Times, Georgia, serif',
            "family_category": "serif",
            "allowed_fallbacks": ["Times", "Georgia", "serif"],
            "fixed_values": _academic_typography_fixed_values(),
            "font_family_match_ratio_required": 1.0,
            "times_new_roman_family_ratio_required": 1.0,
            "fixed_css_summary": (
                "title 56px/1.08/600; identity rows 28px/1.16/400; "
                "major section headings 36px/1.10/700; body/readout/table prose 24px/1.18/400; "
                "captions/labels 20px/1.18/400"
            ),
            "title_weight_min": 550,
            "title_weight_max": 650,
            "heading_weight_min": 650,
            "body_weight_max": 620,
            "max_body_italic_ratio": 0.15,
            "fixed_role_font_sizes_required": True,
        }
    fixed = _typography_fixed_values_from_contract(reference)
    family = str(reference.get("font_family") or "Arial, Helvetica, sans-serif")
    return {
        "source": "reference_poster",
        "primary_font_family": str(reference.get("primary_font_family") or family.split(",", 1)[0]).strip('" '),
        "font_family": family,
        "family_category": str(reference.get("family_category") or "sans_serif"),
        "allowed_fallbacks": [part.strip().strip('"') for part in family.split(",")[1:]],
        "fixed_values": fixed,
        "font_family_match_ratio_required": 1.0,
        "fixed_css_summary": (
            f"title {fixed['title']['font_size_px']}px/{fixed['title']['line_height']}/{fixed['title']['font_weight']}; "
            f"identity rows {fixed['identity_rows']['font_size_px']}px/{fixed['identity_rows']['line_height']}/{fixed['identity_rows']['font_weight']}; "
            f"major section headings {fixed['section_heading']['font_size_px']}px/{fixed['section_heading']['line_height']}/{fixed['section_heading']['font_weight']}; "
            f"nested subsection headings {fixed['subsection_heading']['font_size_px']}px/{fixed['subsection_heading']['line_height']}/{fixed['subsection_heading']['font_weight']}; "
            f"body/readout/table prose {fixed['body']['font_size_px']}px/{fixed['body']['line_height']}/{fixed['body']['font_weight']}; "
            f"captions/labels {fixed['caption_label']['font_size_px']}px/{fixed['caption_label']['line_height']}/{fixed['caption_label']['font_weight']}"
        ),
        "title_weight_min": _safe_int(reference.get("title_weight_min"), default=500),
        "title_weight_max": _safe_int(reference.get("title_weight_max"), default=800),
        "heading_weight_min": _safe_int(reference.get("heading_weight_min"), default=500),
        "body_weight_max": _safe_int(reference.get("body_weight_max"), default=650),
        "max_body_italic_ratio": float(reference.get("max_body_italic_ratio") or 0.15),
        "fixed_role_font_sizes_required": True,
    }


def _typography_authoring_contract(ctx: ToolContext) -> str:
    reference = _reference_typography_contract(ctx)
    if not reference:
        return _ACADEMIC_TYPOGRAPHY_AUTHORING_CONTRACT
    system = _typography_required_system(ctx)
    return (
        f"- Reference-owned typography: use `{system['font_family']}` on `.paper-poster` and inherit it through authored text; "
        f"{system['fixed_css_summary']}. This reference typography replaces the normal default typography contract for this run. "
        "Do not silently restore the default AutoDesign font or centered-title hierarchy."
    )


def _identity_header_authoring_contract(ctx: ToolContext) -> list[str]:
    reference = _reference_style_contract(ctx)
    if not reference:
        return [
            "- Place those fields as three compact centered text rows only: title line, authors line, school/institution/company line. The school/institution/company line should contain only organization names grounded in the paper, rendered as plain text only; do not invent missing organizations.",
        ]
    tokens = reference.get("style_tokens") if isinstance(reference.get("style_tokens"), dict) else {}
    header = tokens.get("header_treatment") if isinstance(tokens.get("header_treatment"), dict) else {}
    alignment = str(header.get("alignment") or "left")
    composition = str(header.get("composition") or "full_width_identity")
    lines = [
        "- Keep the three semantic identity rows (title, authors, institutions), but compose and align them according to the reference instead of the default centered header.",
        f"- Reference identity layout is `{composition}` with `{alignment}` alignment. Collapse and reflow any reference logo/QR reservation; do not leave a large empty logo rail and do not invent replacement content.",
    ]
    if str(header.get("top_rule") or "none") == "none":
        lines.append("- The reference identity area has no top rule: set header/root border-top to 0/none and do not restore the default colored top accent line.")
    return lines


def _reference_layout_authoring_contract(ctx: ToolContext) -> str:
    layout = _reference_layout_contract(ctx)
    if not layout:
        return ""
    per_column = list(layout.get("major_sections_per_column") or [1, 1, 1])
    region_count = len(per_column)
    layout_mode = str(layout.get("layout_mode") or "equal_columns")
    subsection = str(layout.get("subsection_treatment") or "inline_colored_label")
    return (
        f"- Reference-owned top-level structure: use exactly {region_count} `.poster-column` body regions with "
        f"`{layout_mode}` geometry and direct-child `.poster-section` counts `{per_column}`. "
        f"Use `{subsection}` h3/inline labels for Motivation, Method, Results, Analysis, Limitations, and other paper topics inside those major sections. "
        "Preserve the staged blueprint's region proportions/positions. Do not turn the old required_sections list "
        "into separate h2 panels, and do not add default inter-section divider lines."
    )


def _table_authoring_contract(ctx: ToolContext) -> str:
    reference = _reference_style_contract(ctx)
    if not reference:
        return _ACADEMIC_TABLE_AUTHORING_CONTRACT
    tokens = reference.get("style_tokens") if isinstance(reference.get("style_tokens"), dict) else {}
    table = tokens.get("table_treatment") if isinstance(tokens.get("table_treatment"), dict) else {}
    observed = bool(table.get("observed"))
    rule_style = str(table.get("rule_style") or "none")
    evidence_rule = (
        "- Paper source-table evidence still uses the original local PDF crop with matching data-source-id and data-layer-id; "
        "a native table may only be a smaller editable summary, not a duplicate full reconstruction. "
    )
    if not observed or rule_style == "none":
        return evidence_rule + (
            "The reference does not establish a table-rule style. Keep native summary tables open on white: no outer top/bottom frame, "
            "no row-by-row horizontal rules, no colored border, and at most one subtle header underline."
        )
    if rule_style == "minimal":
        return evidence_rule + "Use at most one subtle native-table header rule and no outer or row-by-row rules."
    return evidence_rule + f"Use the visibly observed reference `{rule_style}` rule treatment and nothing heavier."


def _formula_authoring_contract(ctx: ToolContext) -> str:
    reference = _reference_style_contract(ctx)
    if not reference:
        return "- Keep wide formula blocks readable and avoid decorative formula frames."
    tokens = reference.get("style_tokens") if isinstance(reference.get("style_tokens"), dict) else {}
    formula = tokens.get("formula_treatment") if isinstance(tokens.get("formula_treatment"), dict) else {}
    frame = str(formula.get("frame") or "none")
    background = str(formula.get("background") or "none")
    if frame == "none":
        return (
            "- Reference formula treatment is unframed: keep equations in normal content flow with no top/bottom separator rules, "
            "outline, colored side stem, or decorative formula box"
            + ("; a light reference-matched background is allowed." if background == "light" else " and no formula background panel.")
        )
    return f"- Match the reference formula treatment exactly: `{frame}` frame and `{background}` background; do not add extra rules."


def _reference_color_system(ctx: ToolContext) -> dict[str, Any]:
    reference = _reference_style_contract(ctx)
    return _normalize_author_color_system(reference.get("color_system"))


def _reference_style_for_author(ctx: ToolContext) -> dict[str, Any]:
    reference = _reference_style_contract(ctx)
    selected = _structured_selected_color_system(ctx)
    if not reference or not selected:
        return reference
    return _project_reference_style_for_selected_palette(reference, selected)


def _project_reference_style_for_selected_palette(
    reference: dict[str, Any],
    selected: dict[str, Any],
) -> dict[str, Any]:
    projected = copy.deepcopy(reference)
    tokens = (
        projected.get("style_tokens")
        if isinstance(projected.get("style_tokens"), dict)
        else {}
    )
    tokens.pop("reference_palette_roles", None)
    projected["style_tokens"] = tokens
    projected["color_system"] = _reference_scoped_color_system(selected)

    header = tokens.get("header_treatment") if isinstance(tokens.get("header_treatment"), dict) else {}
    lead = tokens.get("lead_band") if isinstance(tokens.get("lead_band"), dict) else {}
    section = tokens.get("section_heading_treatment") if isinstance(tokens.get("section_heading_treatment"), dict) else {}
    palette_id = str(selected.get("palette_id") or "")
    projected["summary"] = (
        f"Reference structure with `{header.get('mode') or 'open_white'}` header and "
        f"`{section.get('mode') or 'text_only'}` section headings; color is supplied "
        f"only by selected palette `{palette_id}`."
    )
    aesthetic_contract = (
        copy.deepcopy(projected.get("aesthetic_contract"))
        if isinstance(projected.get("aesthetic_contract"), dict)
        else {}
    )
    aesthetic_contract.update({
        "canvas_policy": (
            "Use the selected palette's --poster-bg and --poster-text variables for the "
            "reference-owned canvas treatment; do not use a decorative image or gradient background."
        ),
        "palette_usage_policy": (
            f"Use only selected palette `{palette_id}` and its exact CSS variables. "
            "Source figures and source table crops keep their original paper colors."
        ),
        "header_surface_policy": (
            f"Keep reference header mode `{header.get('mode') or 'open_white'}`, alignment "
            f"`{header.get('alignment') or 'left'}`, and rule placement "
            f"`{header.get('rule_placement') or 'none'}` while mapping all color roles to the selected palette."
        ),
        "lead_band_policy": (
            f"Keep the reference lead band {'present' if lead.get('present') else 'absent'} "
            "and preserve its geometry; any band colors come only from the selected palette."
        ),
        "section_surface_policy": (
            f"Keep reference section-heading mode `{section.get('mode') or 'text_only'}` "
            "and surface geometry while mapping every color role to the selected palette."
        ),
        "color_dominance_policy": (
            "Use the selected palette sparingly inside the reference-owned visual grammar; "
            "do not reintroduce the reference palette or add colored dashboard cards."
        ),
    })
    projected["aesthetic_contract"] = aesthetic_contract
    return projected


def _reference_style_id(ctx: ToolContext) -> str:
    return str(_reference_style_contract(ctx).get("style_reference_id") or "").strip()


def _reference_style_brief_lines(
    reference: dict[str, Any],
    *,
    structured_selection: bool = False,
) -> list[str]:
    style_id = str(reference.get("style_reference_id") or "")
    summary = str(reference.get("summary") or "").strip()
    attributes = reference.get("required_root_attributes")
    canvas = reference.get("canvas_contract") if isinstance(reference.get("canvas_contract"), dict) else {}
    width = max(1, _safe_int(canvas.get("w_px"), default=3072))
    height = max(1, _safe_int(canvas.get("h_px"), default=1536))
    style_priority = (
        "- The user-selected palette is authoritative for color. Apply the reference poster's layout, typography, spacing, section, table, formula, and surface treatment without copying its color system."
        if structured_selection
        else "- Reference styling overrides default palette, typography, title alignment/color, header composition, lead-band, section-heading, section-separation, surface, spacing, and emphasis treatment. Default poster aesthetics are fallback-only and must not leak into this run."
    )
    staged_input_guidance = (
        "- Read `reference_style_contract.json` and `reference_style_blueprint.html`, then inspect "
        "`reference_poster/reference.png`. The remapped blueprint is the primary layout/CSS "
        "scaffold; the grayscale preview is neutralized guidance only for geometry, hierarchy, "
        "spacing, and tonal relationships, not final authored surface-color guidance."
        if structured_selection
        else "- Read `reference_style_contract.json` and `reference_style_blueprint.html`, then inspect "
        "`reference_poster/reference.png`. The sanitized blueprint is the primary layout/CSS scaffold; "
        "the image is the visual fidelity target."
    )
    lines = [
        f"- Required style reference id: `{style_id}`.",
        f"- REFERENCE-FIRST RECONSTRUCTION MODE: begin from a blank {width}x{height} canvas. Do not reuse the normal AutoDesign header, section-rule, card, or typography skin and merely recolor it.",
        staged_input_guidance,
        "- Transfer style only. Never copy reference text, names, logos, icons, figures, tables, citations, or links into the target poster.",
        style_priority,
        "- Preserve only the non-visual hard gates: fixed canvas, identity-only header content, target-paper provenance, editable native text, local assets, and no overlap/scripts/remote content. Body-region count and geometry come from the reference.",
        "- Reproduce the reference's absence as carefully as its presence: when it has no inter-section lines, outer panel frames, colored side stems, or card chrome, do not add them.",
    ]
    tokens = reference.get("style_tokens") if isinstance(reference.get("style_tokens"), dict) else {}
    columns = tokens.get("column_structure") if isinstance(tokens.get("column_structure"), dict) else {}
    per_column = columns.get("major_sections_per_column")
    if isinstance(per_column, list) and 2 <= len(per_column) <= 6:
        lines.append(
            f"- Exact top-level structure: direct-child `.poster-section` counts across the {len(per_column)} "
            f"reference-owned body regions are `{per_column}`. "
            "The old paper section list is content inventory, not permission to create more major panels."
        )
    chrome = tokens.get("chrome_treatment") if isinstance(tokens.get("chrome_treatment"), dict) else {}
    if bool(chrome.get("present")):
        lines.append(
            "- Keep ornamental routes/rails only in the root-level `.reference-chrome`/`[data-style-role=chrome-layer]` "
            "behind content and confined to reference gutters or section edges. Never recreate chrome with section/column "
            "pseudo-elements and never let it cross text, figures, tables, or formulas."
        )
    table = tokens.get("table_treatment") if isinstance(tokens.get("table_treatment"), dict) else {}
    if not bool(table.get("observed")) or str(table.get("rule_style") or "none") == "none":
        lines.append("- The reference does not establish booktabs: do not add outer table rules or repeated row separators.")
    formula = tokens.get("formula_treatment") if isinstance(tokens.get("formula_treatment"), dict) else {}
    if str(formula.get("frame") or "none") == "none":
        lines.append("- Equations are unframed in this style: do not put top/bottom rules or a box around formula blocks.")
    if summary:
        lines.insert(1, f"- Visual summary: {summary}")
    if isinstance(attributes, dict) and attributes:
        rendered = " ".join(f'{key}="{value}"' for key, value in attributes.items())
        lines.append(f"- Put these attributes on `.paper-poster`: `{rendered}`.")
    return lines


def _reference_style_prompt_block(ctx: ToolContext) -> str:
    reference = _reference_style_for_author(ctx)
    if not reference:
        return "- No user reference poster was supplied. Follow the normal academic aesthetic contract."
    return "\n".join(_reference_style_brief_lines(
        reference,
        structured_selection=bool(_structured_selected_color_system(ctx)),
    ))


def _color_authoring_contract(ctx: ToolContext) -> str:
    structured = _structured_selected_color_system(ctx)
    reference = _reference_style_contract(ctx)
    if structured:
        palette_id = str(structured.get("palette_id") or "")
        css_variables = structured.get("css_variables") if isinstance(structured.get("css_variables"), dict) else {}
        reference_rule = (
            "The user-selected palette is authoritative for color. Apply the reference poster's layout, typography, spacing, section, table, formula, and surface treatment without copying its color system. "
            if reference
            else "The user-selected palette is authoritative for color. "
        )
        treatment = ""
        if reference:
            tokens = reference.get("style_tokens") if isinstance(reference.get("style_tokens"), dict) else {}
            header = tokens.get("header_treatment") if isinstance(tokens.get("header_treatment"), dict) else {}
            section = tokens.get("section_heading_treatment") if isinstance(tokens.get("section_heading_treatment"), dict) else {}
            treatment = (
                f"Apply reference header treatment `{header.get('mode') or 'top_rule_white'}` and "
                f"major section-heading treatment `{section.get('mode') or 'filled_band'}`. "
            )
        return (
            f"- {reference_rule}Use only required palette `{palette_id}`. Set "
            f"`data-palette-id=\"{palette_id}\"` on `.paper-poster` and define these exact "
            f"`--poster-*` CSS variables: {json.dumps(css_variables, ensure_ascii=False, sort_keys=True)}. "
            f"{treatment}Do not expose, choose, rerank, or mix any alternative palette. "
            "Keep source figures and source table crops in their original colors."
        )
    if not reference:
        return _ACADEMIC_COLOR_AUTHORING_CONTRACT
    color = _reference_color_system(ctx)
    palette_id = str(color.get("palette_id") or _reference_style_id(ctx))
    tokens = reference.get("style_tokens") if isinstance(reference.get("style_tokens"), dict) else {}
    header = tokens.get("header_treatment") if isinstance(tokens.get("header_treatment"), dict) else {}
    section = tokens.get("section_heading_treatment") if isinstance(tokens.get("section_heading_treatment"), dict) else {}
    return (
        "- A user reference poster is active. Unless the user prompt explicitly names a different palette, "
        f"use required palette `{palette_id}` and its exact `--poster-*` CSS variables. "
        f"Apply header treatment `{header.get('mode') or 'top_rule_white'}` and major section-heading treatment "
        f"`{section.get('mode') or 'filled_band'}` consistently across the whole poster. "
        "Do not rerank by paper domain or institution. Transfer only visual treatment: never copy reference "
        "text, logos, icons, figures, tables, links, or scientific content. Keep source figures/tables in their "
        "original colors and preserve all target-paper provenance. Typography and layout treatment come from the reference contract, not the default poster skin."
    )


def _aesthetic_contract_lines(content: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    aesthetic = (
        contract.get("aesthetic_contract")
        if isinstance(contract.get("aesthetic_contract"), dict)
        else content.get("aesthetic_contract")
        if isinstance(content.get("aesthetic_contract"), dict)
        else {}
    )
    ordered_keys = (
        "reference_priority_policy",
        "canvas_policy",
        "palette_usage_policy",
        "header_surface_policy",
        "lead_band_policy",
        "section_surface_policy",
        "section_separation_policy",
        "chrome_policy",
        "table_surface_policy",
        "source_wrapper_policy",
        "color_dominance_policy",
    )
    lines = [
        f"- {str(aesthetic.get(key)).strip()}"
        for key in ordered_keys
        if str(aesthetic.get(key) or "").strip()
    ]
    if lines:
        limit = len(ordered_keys) if aesthetic.get("reference_priority_policy") else 9
        return lines[:limit]
    fallback = _default_aesthetic_contract()
    return [
        f"- {fallback[key]}"
        for key in ordered_keys
        if key in fallback
    ]


def _color_system_brief_lines(
    color_system: dict[str, Any],
    *,
    options: list[dict[str, Any]] | None = None,
    required: dict[str, Any] | None = None,
    institution_signals: dict[str, Any] | None = None,
    reference_style: dict[str, Any] | None = None,
    structured_selection: bool = False,
) -> list[str]:
    palette_id = str(color_system.get("palette_id") or "bright_cobalt")
    palette_name = str(color_system.get("palette_name") or "Cardinal Red")
    reason = str(color_system.get("selection_reason") or "default academic palette")
    required = required if isinstance(required, dict) else {}
    options = options or []
    display_options = (
        [_normalize_author_color_system(color_system)]
        if structured_selection
        else _display_color_system_options(
            color_system,
            options,
            required=required,
            max_alternatives=5,
        )
    )
    display_options = [item for item in display_options if item]
    signals = institution_signals if isinstance(institution_signals, dict) else {}
    organizations = signals.get("organizations") if isinstance(signals.get("organizations"), list) else []
    if structured_selection:
        lines = [
            (
                "- The user-selected palette is authoritative for color. Apply the reference poster's layout, typography, spacing, section, table, formula, and surface treatment without copying its color system."
                if isinstance(reference_style, dict) and reference_style
                else "- The user-selected palette is authoritative for color; there is no palette choice or recommendation step."
            ),
            f"- Required user-selected palette: {palette_name} (`{palette_id}`)",
            f"- Selection reason: {reason}",
        ]
    elif isinstance(reference_style, dict) and reference_style:
        lines = [
            "- The active reference palette is authoritative for this run; there is no general palette-library choice step.",
            f"- Reference palette: {palette_name} (`{palette_id}`)",
            f"- Selection reason: {reason}",
        ]
    else:
        lines = [
            "- Choose exactly one academic palette before writing CSS; the staged JSON has the curated palette library.",
            f"- Recommended default: {palette_name} (`{palette_id}`)",
            f"- Recommendation reason: {reason}",
        ]
    if required and not structured_selection:
        lines.append(
            "- Required palette from user prompt: "
            f"{required.get('palette_name') or required.get('palette_id')} "
            f"(`{required.get('palette_id')}`)"
        )
    if organizations:
        lines.append("- Institution/company/school names detected, but they are not palette-selection inputs: " + ", ".join(str(item) for item in organizations[:12]))
    else:
        lines.append("- Institution/company/school names detected, but they are not palette-selection inputs: none available")
    if signals.get("selection_guidance") and not structured_selection:
        lines.append("- Ignore institution color-signal guidance for automatic palette selection; use only the random recommended default.")
    if structured_selection:
        if isinstance(reference_style, dict) and reference_style:
            lines.extend([
                "- Use the user-selected palette's exact CSS variables, but take header, section, table, formula, and surface treatment only from reference_style_contract.json; do not add the normal single-top-rule or filled-section-bar defaults.",
                "- Do not search for official colors, fetch logos, add icons, rerank by paper domain, or mix reference-palette colors into the selected palette.",
                "- Set `data-palette-id` on `.paper-poster` and define the exact `--poster-*` CSS variables for the user-selected palette.",
                "- Keep source figures and source table crops in their original colors.",
            ])
        else:
            lines.extend([
                "- Use only the user-selected palette's exact CSS variables; do not choose, recommend, stage, or mix another palette.",
                "- Do not search for official colors, fetch logos, add icons, or rerank by paper domain or institution.",
                "- Set `data-palette-id` on `.paper-poster` and define the exact `--poster-*` CSS variables for the user-selected palette.",
                "- Use the fixed identity-header treatment: white/near-white header with a single top accent rule only; no bottom header rule or side outline.",
                "- Use palette colors for compact filled section heading bands, thin dividers, and a few lead-key accents; keep panel interiors, native table cells, and ordinary readouts white or neutral.",
                "- Keep source figures and source table crops in their original colors.",
            ])
    elif isinstance(reference_style, dict) and reference_style:
        lines.extend([
                "- This palette belongs to the active reference poster. Use its exact CSS variables, but take header, section, table, formula, and surface treatment only from reference_style_contract.json; do not add the normal single-top-rule or filled-section-bar defaults.",
                "- Do not search for official colors, fetch logos, add icons, rerank by paper domain, or mix another palette into the reference style.",
                "- Set `data-palette-id` on `.paper-poster` and define the exact CSS variables for the chosen palette.",
                "- Keep source figures and source table crops in their original colors.",
        ])
    else:
        lines.extend([
            "- Use the recommended default palette unless the user prompt explicitly requires another listed palette; do not rerank by paper domain, source colors, or institution/company/school names.",
            "- Do not search for official colors, fetch logos, add icons, or mix palette colors.",
            "- Set `data-palette-id` on `.paper-poster` and define the exact CSS variables for the chosen palette.",
            "- Use the fixed identity-header treatment: white/near-white header with a single top accent rule only; no bottom header rule or side outline.",
            "- Use palette colors for compact filled section heading bands, thin dividers, and a few lead-key accents; keep panel interiors, native table cells, and ordinary readouts white or neutral. Source wrapper DOM boxes may exist for measurement, but their borders must be transparent with no visible outline or shadow.",
            "- Do not use filled title bands, four-sided outlines, mixed header treatments, body panel fills, table zebra rows, boxed callout backgrounds, or heavy colored panel borders.",
            "- Keep source figures and source table crops in their original colors.",
        ])
    if display_options:
        if structured_selection:
            lines.append("- Required palette for this attempt:")
        else:
            shown_count = len(display_options)
            total_count = len(options)
            suffix = f" (showing {shown_count} of {total_count})" if total_count > shown_count else ""
            lines.append("- Palette options for this attempt" + suffix + ":")
    for option in display_options:
        opt_id = str(option.get("palette_id") or "").strip()
        opt_name = str(option.get("palette_name") or opt_id).strip()
        use_when = str(option.get("use_when") or "").strip()
        css_vars = option.get("css_variables") if isinstance(option.get("css_variables"), dict) else {}
        allowed = option.get("allowed_hexes") if isinstance(option.get("allowed_hexes"), list) else []
        line = f"  - {opt_name} (`{opt_id}`)"
        if use_when:
            line += f": {use_when}"
        lines.append(line)
        if css_vars:
            lines.append("    CSS variables: " + json.dumps(css_vars, ensure_ascii=False, sort_keys=True))
        if allowed:
            lines.append("    Allowed authored hex colors: " + ", ".join(str(item) for item in allowed))
    return lines


def _display_color_system_options(
    color_system: dict[str, Any],
    options: list[dict[str, Any]],
    *,
    required: dict[str, Any],
    max_alternatives: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(option: dict[str, Any]) -> None:
        palette_id = str(option.get("palette_id") or "").strip()
        if not palette_id or palette_id in seen:
            return
        normalized = _normalize_author_color_system(option)
        if not normalized:
            return
        out.append(normalized)
        seen.add(palette_id)

    if required:
        add(required)
    add(color_system)
    alternatives = 0
    for option in options:
        before = len(out)
        add(option)
        if len(out) > before:
            alternatives += 1
        if alternatives >= max(0, max_alternatives):
            break
    return out


def _format_color_system_prompt_block(
    color_system: dict[str, Any],
    *,
    options: list[dict[str, Any]] | None = None,
    required: dict[str, Any] | None = None,
    institution_signals: dict[str, Any] | None = None,
    reference_style: dict[str, Any] | None = None,
    structured_selection: bool = False,
) -> str:
    return "\n".join(_color_system_brief_lines(
        color_system,
        options=options,
        required=required,
        institution_signals=institution_signals,
        reference_style=reference_style,
        structured_selection=structured_selection,
    ))


def _state_or_attempt_json(ctx: ToolContext, key: str, path: Path) -> dict[str, Any]:
    value = ctx.state.get(key)
    if isinstance(value, dict):
        return value
    loaded = _read_optional_json(path)
    return loaded if isinstance(loaded, dict) else {}


def _first_text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _required_slot_ids(contract: dict[str, Any]) -> list[str]:
    nested = contract.get("panel_content_plan_contract")
    candidates = []
    if isinstance(nested, dict):
        candidates.append(nested.get("required_slot_ids"))
    candidates.extend([
        contract.get("required_slot_ids"),
        contract.get("required_sections"),
    ])
    for value in candidates:
        if isinstance(value, list):
            out = []
            for item in value:
                if isinstance(item, str) and item.strip():
                    out.append(item.strip())
                elif isinstance(item, dict):
                    slot = _first_text(item, "slot_id", "id", "panel_role", "title")
                    if slot:
                        out.append(slot)
            if out:
                return out
    return []


def _selected_visual_brief_lines(contract: dict[str, Any], storyboard: dict[str, Any]) -> list[str]:
    visuals = contract.get("selected_visuals")
    if not isinstance(visuals, list) or not visuals:
        visuals = contract.get("storyboard_selected_assets")
    if not isinstance(visuals, list) or not visuals:
        visuals = storyboard.get("selected_assets")
    if not isinstance(visuals, list):
        return []
    lines: list[str] = []
    for item in visuals[:8]:
        if not isinstance(item, dict):
            continue
        visual_id = _first_text(item, "layer_id", "asset_id", "source_id", "rendered_layer_id", "id")
        caption = _first_text(item, "caption_short", "caption", "title", "visual_role")
        output_file = _first_text(item, "output_file", "src", "path")
        bits = [visual_id or "unknown_visual"]
        if caption:
            bits.append(caption)
        if output_file:
            bits.append(output_file)
        lines.append("- " + " | ".join(bits))
    return lines


def _required_source_visual_brief_lines(contract: dict[str, Any]) -> list[str]:
    raw_ids = contract.get("required_source_visual_ids")
    if not isinstance(raw_ids, list):
        return []
    required_ids = [str(value or "").strip() for value in raw_ids if str(value or "").strip()]
    if not required_ids:
        return []
    records = [
        item
        for item in (contract.get("selected_visuals") or [])
        if isinstance(item, dict)
    ]
    by_id = {
        _first_text(item, "layer_id", "asset_id", "source_id", "rendered_layer_id", "id"): item
        for item in records
    }
    lines: list[str] = []
    for visual_id in required_ids:
        item = by_id.get(visual_id) or {}
        caption = _first_text(item, "caption_short", "caption", "title", "visual_role")
        output_file = _first_text(item, "output_file", "src", "path")
        bits = [visual_id]
        if caption:
            bits.append(caption)
        if output_file:
            bits.append(output_file)
        lines.append("- " + " | ".join(bits))
    return lines


def _high_priority_visual_brief_lines(contract: dict[str, Any], storyboard: dict[str, Any]) -> list[str]:
    tiers = contract.get("source_asset_tiers") if isinstance(contract.get("source_asset_tiers"), dict) else {}
    visuals = tiers.get("high_priority_assets")
    if not isinstance(visuals, list) or not visuals:
        visual_ids = {
            str(item.get("asset_id") or item.get("layer_id") or "").strip()
            for item in (storyboard.get("selected_assets") or [])
            if isinstance(item, dict)
            and bool(item.get("protected_anchor"))
            and str(item.get("asset_id") or item.get("layer_id") or "").strip()
        }
        visuals = [
            item for item in (contract.get("selected_visuals") or [])
            if isinstance(item, dict)
            and str(item.get("layer_id") or item.get("asset_id") or "").strip() in visual_ids
        ]
    if not isinstance(visuals, list):
        return []
    lines: list[str] = []
    for item in visuals[:8]:
        if not isinstance(item, dict):
            continue
        visual_id = _first_text(item, "layer_id", "asset_id", "source_id", "rendered_layer_id", "id")
        caption = _first_text(item, "caption_short", "caption", "title", "visual_role")
        output_file = _first_text(item, "output_file", "src", "path")
        anchor_kind = _first_text(item, "anchor_kind")
        anchor_label = _first_text(item, "anchor_label")
        anchor_reason = _first_text(item, "anchor_reason")
        anchor = ""
        if anchor_kind or anchor_label:
            anchor = f"{(anchor_kind or 'source').title()} {anchor_label}".strip()
        bits = [visual_id or "unknown_visual"]
        if anchor:
            bits.append(anchor)
        if caption:
            bits.append(caption)
        if output_file:
            bits.append(output_file)
        if anchor_reason:
            bits.append(anchor_reason)
        lines.append("- " + " | ".join(bits))
    return lines


def _supplemental_native_visual_brief_lines(contract: dict[str, Any]) -> list[str]:
    tasks = contract.get("supplemental_native_visual_tasks")
    if not isinstance(tasks, list) or not tasks:
        visual_selection = contract.get("visual_selection") if isinstance(contract.get("visual_selection"), dict) else {}
        tasks = visual_selection.get("supplemental_native_visual_tasks")
    if not isinstance(tasks, list) or not tasks:
        return []
    lines = [
        "- Use these only as native editable HTML/SVG/table units grounded in poster_content_brief and paper evidence.",
        "- Do not substitute rejected, forbidden, or debug-only source crops to satisfy these tasks.",
    ]
    for task in tasks[:8]:
        if not isinstance(task, dict):
            continue
        task_id = _first_text(task, "task_id", "id", "kind") or "native_visual_task"
        title = _first_text(task, "title", "role", "kind")
        instruction = _first_text(task, "instruction", "description", "purpose")
        sources = task.get("source_text_roles") or task.get("source_ids") or []
        bits = [task_id]
        if title and title != task_id:
            bits.append(title)
        if instruction:
            bits.append(instruction)
        if isinstance(sources, list) and sources:
            bits.append("sources: " + ", ".join(str(item) for item in sources[:4] if str(item).strip()))
        lines.append("- " + " | ".join(bits))
    return lines


def _section_brief_lines(content: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    sections = content.get("sections")
    if not isinstance(sections, list) or not sections:
        sections = contract.get("sections")
    if not isinstance(sections, list):
        return []
    lines: list[str] = []
    for section in sections[:10]:
        if not isinstance(section, dict):
            continue
        title = _first_text(section, "title", "heading", "slot_id", "panel_role", "id") or "section"
        summary = _first_text(section, "summary", "takeaway", "text", "purpose")
        bullets = section.get("bullets")
        if isinstance(bullets, list) and bullets:
            summary = "; ".join(str(item).strip() for item in bullets[:3] if str(item).strip()) or summary
        lines.append(f"- {title}: {summary}" if summary else f"- {title}")
    return lines


def _read_optional_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _record_feedback(ctx: ToolContext, attempt_dir: Path, feedback: dict[str, Any]) -> None:
    bound_feedback = copy.deepcopy(feedback)
    poster_path = attempt_dir / "poster.html"
    invocation = _read_optional_json(attempt_dir / "designer_author_log.json")
    attempt_match = re.fullmatch(r"attempt_(\d+)", attempt_dir.name)
    if poster_path.is_file() and isinstance(invocation, dict) and attempt_match:
        bound_feedback["validated_attempt"] = int(attempt_match.group(1))
        bound_feedback["validated_attempt_dir"] = str(attempt_dir.resolve())
        bound_feedback["validated_poster_sha256"] = sha256_file(poster_path)
        bound_feedback["invocation_poster_sha256"] = str(
            invocation.get("poster_sha256") or ""
        )
    atomic_write_json(attempt_dir / "validation_feedback.json", bound_feedback)
    ctx.state["designer_author_last_feedback"] = bound_feedback
    feedback_history = list(ctx.state.get("designer_author_validation_feedback") or [])
    feedback_history.append(bound_feedback)
    ctx.state["designer_author_validation_feedback"] = feedback_history


def _run_relative_path(ctx: ToolContext, path: Path | str | None) -> str:
    if not path:
        return ""
    try:
        return str(Path(path).resolve().relative_to(ctx.run_dir.resolve()))
    except Exception:
        return str(path)


def _candidate_preview_path(ctx: ToolContext, candidate: dict[str, Any]) -> Path | None:
    for key in (
        "candidate_preview_png_relative",
        "preview_png_relative",
        "candidate_preview_png",
        "preview_png",
    ):
        raw = str(candidate.get(key) or "")
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = ctx.run_dir / path
        if path.exists():
            return path
    nested = candidate.get("candidate")
    if isinstance(nested, dict) and nested is not candidate:
        return _candidate_preview_path(ctx, nested)
    return None


def _critic_clean_design_feedback(ctx: ToolContext, *, attempt_index: int) -> dict[str, Any]:
    feedback: dict[str, Any] = {
        "artifact_type": "poster",
        "iteration": attempt_index,
        "findings": [],
        "counts": {},
        "has_blocking_findings": False,
    }
    contract = ctx.state.get("poster_plan_contract")
    if isinstance(contract, dict) and contract:
        feedback["poster_plan_contract"] = copy.deepcopy(contract)
    crits = ctx.state.get("critique_results") or []
    if crits:
        scores = getattr(crits[-1], "dimension_scores", None)
        if isinstance(crits[-1], dict):
            scores = crits[-1].get("dimension_scores")
        if isinstance(scores, dict):
            feedback["latest_critic_scorecard"] = copy.deepcopy(scores)
    return feedback


def _attempt_critic_design_spec(ctx: ToolContext, canvas: dict[str, Any]) -> DesignSpec:
    spec = ctx.state.get("design_spec")
    if isinstance(spec, DesignSpec):
        return copy.deepcopy(spec)
    if isinstance(spec, dict):
        try:
            return DesignSpec.model_validate(spec)
        except Exception:  # noqa: BLE001
            pass
    active_canvas = _active_canvas_contract(ctx)
    return DesignSpec(
        brief=str(ctx.state.get("raw_user_brief") or ctx.state.get("brief") or "Academic paper poster"),
        artifact_type=ArtifactType.POSTER,
        canvas={
            "w_px": int(canvas.get("w_px") or 3072),
            "h_px": int(canvas.get("h_px") or 1536),
            "dpi": int(canvas.get("dpi") or active_canvas["dpi"]),
            "aspect_ratio": str(canvas.get("aspect_ratio") or active_canvas["aspect_ratio"]),
            "color_mode": str(canvas.get("color_mode") or active_canvas["color_mode"]),
        },
    )


def _attempt_critic_design_feedback(
    ctx: ToolContext,
    *,
    validation_feedback: dict[str, Any],
    attempt_index: int,
) -> dict[str, Any]:
    summary = validation_feedback.get("summary") if isinstance(validation_feedback.get("summary"), dict) else {}
    payload = validation_feedback.get("payload") if isinstance(validation_feedback.get("payload"), dict) else {}
    raw_issues = summary.get("issues") or payload.get("issues") or []
    findings: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_issues[:12], start=1):
        if not isinstance(raw, dict):
            continue
        issue_id = str(raw.get("issue_id") or raw.get("id") or f"deterministic:{index}")
        message = str(
            raw.get("message")
            or raw.get("description")
            or payload.get("hint")
            or payload.get("local_repair_hint")
            or validation_feedback.get("error_message")
            or "Deterministic preflight rejected the authored poster."
        )
        findings.append({
            "id": issue_id,
            "source": "direct_final_preflight",
            "severity": "blocker",
            "artifact_type": "poster",
            "message": message,
            "target": {
                key: raw.get(key)
                for key in (
                    "section_id",
                    "container_kind",
                    "target_block_ids",
                    "target_selectors",
                    "data_block_id",
                    "data_source_id",
                    "bbox",
                    "container_bbox",
                )
                if raw.get(key) not in (None, "", [])
            },
            "evidence": copy.deepcopy(raw),
            "suggested_action": str(
                raw.get("suggested_action")
                or raw.get("fix")
                or payload.get("hint")
                or "Fix the deterministic preflight blocker before promotion."
            ),
            "repairable": True,
        })
    if not findings:
        issue_id = str(summary.get("issue_id") or payload.get("issue_id") or "direct_final_preflight")
        findings.append({
            "id": issue_id,
            "source": "direct_final_preflight",
            "severity": "blocker",
            "artifact_type": "poster",
            "message": str(
                validation_feedback.get("error_message")
                or payload.get("hint")
                or "Deterministic preflight rejected the authored poster."
            ),
            "target": {},
            "evidence": {
                "issue_id": issue_id,
                "repair_route": summary.get("repair_route") or payload.get("repair_route"),
            },
            "suggested_action": str(payload.get("hint") or "Fix the deterministic preflight blocker before promotion."),
            "repairable": True,
        })
    feedback = {
        "artifact_type": "poster",
        "iteration": attempt_index,
        "counts": {"blocker": len(findings)},
        "has_blocking_findings": True,
        "findings": findings,
    }
    contract = ctx.state.get("poster_plan_contract")
    if isinstance(contract, dict) and contract:
        feedback["poster_plan_contract"] = copy.deepcopy(contract)
    crits = ctx.state.get("critique_results") or []
    if crits:
        scores = getattr(crits[-1], "dimension_scores", None)
        if isinstance(crits[-1], dict):
            scores = crits[-1].get("dimension_scores")
        if isinstance(scores, dict):
            feedback["latest_critic_scorecard"] = copy.deepcopy(scores)
    return feedback


def _candidate_from_validation_feedback(feedback: dict[str, Any]) -> dict[str, Any]:
    summary = feedback.get("summary") if isinstance(feedback.get("summary"), dict) else {}
    payload = feedback.get("payload") if isinstance(feedback.get("payload"), dict) else {}
    keys = (
        "candidate_id",
        "candidate_relative_dir",
        "candidate_preview_png",
        "candidate_preview_png_relative",
        "candidate_measurement_json",
        "candidate_measurement_json_relative",
        "candidate_validation_overlay_png",
        "candidate_validation_overlay_png_relative",
        "candidate_score",
        "candidate_score_reasons",
    )
    return {
        key: copy.deepcopy(summary.get(key) if summary.get(key) not in (None, "") else payload.get(key))
        for key in keys
        if (summary.get(key) if summary.get(key) not in (None, "") else payload.get(key)) not in (None, "")
    }


def _merge_attempt_level_critic_feedback(
    validation_feedback: dict[str, Any],
    critic_feedback: dict[str, Any],
) -> dict[str, Any]:
    merged = copy.deepcopy(validation_feedback)
    summary = merged.setdefault("summary", {})
    payload = merged.setdefault("payload", {})
    if not isinstance(summary, dict) or not isinstance(payload, dict):
        return validation_feedback
    critic_summary = critic_feedback.get("summary") if isinstance(critic_feedback.get("summary"), dict) else {}
    critic_payload = critic_feedback.get("payload") if isinstance(critic_feedback.get("payload"), dict) else {}
    context = _post_composite_context_from_feedback(critic_summary, critic_payload)
    context.update({
        "advisory": True,
        "repair_order": "Fix the deterministic preflight blocker first, then apply this critic guidance.",
        "candidate_preview_png_relative": (
            critic_summary.get("candidate_preview_png_relative")
            or critic_payload.get("candidate_preview_png_relative")
            or ""
        ),
        "candidate_measurement_json_relative": (
            critic_summary.get("candidate_measurement_json_relative")
            or critic_payload.get("candidate_measurement_json_relative")
            or ""
        ),
    })
    summary["attempt_level_critic_feedback"] = {
        "feedback_tool": "critic",
        "critic_verdict": context.get("critic_verdict"),
        "critic_score": context.get("critic_score"),
        "critic_summary": context.get("critic_summary"),
        "issue_count": len(context.get("blocking_findings") or []),
    }
    payload["attempt_level_critic_feedback"] = context
    history = payload.get("attempt_level_critic_feedback_history")
    if not isinstance(history, list):
        history = []
    history.append(copy.deepcopy(context))
    payload["attempt_level_critic_feedback_history"] = history[-4:]
    return merged


def _critic_feedback_should_retry(feedback: dict[str, Any]) -> bool:
    summary = feedback.get("summary") if isinstance(feedback.get("summary"), dict) else {}
    payload = feedback.get("payload") if isinstance(feedback.get("payload"), dict) else {}
    if payload.get("critic_infrastructure_failure") or summary.get("critic_infrastructure_failure"):
        return False
    return str(summary.get("critic_verdict") or payload.get("critic_verdict") or "").lower() in {"revise", "fail"}


def _critic_feedback_is_infrastructure_failure(feedback: dict[str, Any]) -> bool:
    summary = feedback.get("summary") if isinstance(feedback.get("summary"), dict) else {}
    payload = feedback.get("payload") if isinstance(feedback.get("payload"), dict) else {}
    return bool(payload.get("critic_infrastructure_failure") or summary.get("critic_infrastructure_failure"))


def _critic_feedback_blocks_delivery_fallback(feedback: dict[str, Any]) -> bool:
    summary = feedback.get("summary") if isinstance(feedback.get("summary"), dict) else {}
    payload = feedback.get("payload") if isinstance(feedback.get("payload"), dict) else {}
    if (summary.get("feedback_tool") or payload.get("feedback_tool")) != "critic":
        return False
    if payload.get("critic_infrastructure_failure") or summary.get("critic_infrastructure_failure"):
        return False
    return str(summary.get("critic_verdict") or payload.get("critic_verdict") or "").lower() in {"revise", "fail"}


def _critic_infrastructure_feedback(
    *,
    attempt_index: int,
    attempt_dir: Path,
    message: str,
    candidate: dict[str, Any] | None = None,
    preview_path: Path | None = None,
    preview_warnings: list[str] | None = None,
    result_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issue_id = "designer_author_critic_unavailable"
    hint = (
        "CriticAgent could not complete a reliable poster review. This is an infrastructure failure, "
        "not a poster repair instruction; do not ask the external designer to rewrite poster.html for it."
    )
    issue = {
        "id": issue_id,
        "issue_id": issue_id,
        "source": "critic",
        "severity": "blocking",
        "category": "infrastructure",
        "message": message,
        "description": message,
        "suggested_action": "Do not edit poster.html for this critic infrastructure failure.",
        "repairable": False,
        "critic_infrastructure_failure": True,
    }
    payload = {
        "issue_id": issue_id,
        "primary_blocking_issue_id": issue_id,
        "repair_route": "critic_infrastructure_failure",
        "hint": hint,
        "issues": [issue],
        "blocking_findings": [issue],
        "feedback_tool": "critic",
        "critic_infrastructure_failure": True,
        "critic_error_message": message,
        "candidate": copy.deepcopy(candidate or {}),
        "candidate_relative_dir": (candidate or {}).get("candidate_relative_dir"),
        "candidate_preview_png_relative": _path_relative_to_attempt_or_run(preview_path, attempt_dir) if preview_path else "",
        "preview_warnings": list(preview_warnings or []),
        "critic_result_payload": copy.deepcopy(result_payload or {}),
    }
    summary = {
        "issue_id": issue_id,
        "primary_blocking_issue_id": issue_id,
        "repair_route": "critic_infrastructure_failure",
        "hint": hint,
        "issues": [issue],
        "blocking_findings": [issue],
        "feedback_tool": "critic",
        "critic_infrastructure_failure": True,
    }
    return {
        "version": 1,
        "tool": "critic",
        "attempt": attempt_index,
        "attempt_dir": str(attempt_dir),
        "status": "error",
        "error_message": message,
        "error_category": "critic",
        "summary": summary,
        "payload": payload,
    }


def _critic_report_repair_feedback(
    *,
    report: dict[str, Any],
    attempt_index: int,
    attempt_dir: Path,
    candidate: dict[str, Any],
    preview_path: Path,
    preview_warnings: list[str] | None = None,
    deterministic_valid: bool = True,
) -> dict[str, Any]:
    verdict = str(report.get("verdict") or "revise").lower()
    score = _safe_float(report.get("score"), default=0.0)
    issues = _critic_repair_issues(report)
    sanitized_report = _sanitize_critic_report_for_author(report, issues)
    critic_summary = _sanitize_critic_text_for_author(str(report.get("summary") or ""))
    hint = (
        "The deterministic preflight passed, but the vision critic found poster-quality issues. "
        "Patch only poster.html, preserve source ids/data-block-id/data-layer-id/source evidence, "
        "and address the named critic targets before the next attempt."
        if deterministic_valid else
        "The deterministic preflight still has primary blockers, and the vision critic found additional "
        "poster-quality issues from the rendered attempt preview. Fix the deterministic blocker first, "
        "then use the named critic targets as visual/narrative repair guidance for the same poster.html patch."
    )
    payload = {
        "issue_id": "designer_author_post_composite_blockers",
        "primary_blocking_issue_id": "designer_author_post_composite_blockers",
        "repair_route": "repair_authored_poster_design_feedback",
        "hint": hint,
        "issues": issues[:8],
        "blocking_findings": issues[:12],
        "feedback_tool": "critic",
        "critic_verdict": verdict,
        "critic_score": score,
        "dimension_scores": copy.deepcopy(report.get("dimension_scores") or {}),
        "review_coverage": copy.deepcopy(report.get("review_coverage") or {}),
        "critic_summary": critic_summary,
        "critique_report": sanitized_report,
        "candidate": copy.deepcopy(candidate),
        "candidate_relative_dir": candidate.get("candidate_relative_dir"),
        "candidate_preview_png_relative": _path_relative_to_attempt_or_run(preview_path, attempt_dir),
        "candidate_measurement_json_relative": candidate.get("candidate_measurement_json_relative"),
        "preview_warnings": list(preview_warnings or []),
    }
    summary = {
        "issue_id": "designer_author_post_composite_blockers",
        "primary_blocking_issue_id": "designer_author_post_composite_blockers",
        "repair_route": "repair_authored_poster_design_feedback",
        "hint": hint,
        "issues": issues[:8],
        "blocking_findings": issues[:8],
        "feedback_tool": "critic",
        "critic_verdict": verdict,
        "critic_score": score,
        "critic_summary": critic_summary,
        "dimension_scores": copy.deepcopy(report.get("dimension_scores") or {}),
        "review_coverage": copy.deepcopy(report.get("review_coverage") or {}),
        "candidate_preview_png_relative": payload["candidate_preview_png_relative"],
        "candidate_measurement_json_relative": payload["candidate_measurement_json_relative"],
    }
    return {
        "version": 1,
        "tool": "critic",
        "attempt": attempt_index,
        "attempt_dir": str(attempt_dir),
        "status": "error",
        "error_message": f"critic {verdict}: {critic_summary or 'poster-quality revisions required'}",
        "error_category": "critic",
        "summary": summary,
        "payload": payload,
    }


def _critic_repair_issues(report: dict[str, Any]) -> list[dict[str, Any]]:
    raw_issues = report.get("issues") if isinstance(report.get("issues"), list) else []
    out: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_issues[:12], start=1):
        if not isinstance(raw, dict):
            continue
        category = str(raw.get("category") or "quality")
        issue_id = str(raw.get("issue_id") or f"critic:{category}:{index}")
        description = _sanitize_critic_text_for_author(
            str(raw.get("description") or raw.get("message") or "Critic requested poster-quality repair.")
        )
        issue: dict[str, Any] = {
            "id": issue_id,
            "issue_id": issue_id,
            "source": "critic",
            "severity": str(raw.get("severity") or "high"),
            "category": category,
            "message": description,
            "description": description,
            "target": copy.deepcopy(raw.get("target") or {}),
            "evidence": copy.deepcopy(raw.get("evidence") or {}),
            "suggested_action": _sanitize_critic_text_for_author(
                str(raw.get("suggested_action") or "Patch the named poster section in poster.html.")
            ),
            "layer_ids": _sanitize_critic_json_for_author(list(raw.get("layer_ids") or [])),
            "slide_id": _sanitize_critic_json_for_author(raw.get("slide_id")),
            "confidence": raw.get("confidence"),
            "evidence_paper_anchor": _sanitize_critic_json_for_author(raw.get("evidence_paper_anchor")),
            "repair_tool": "edit_poster_html",
            "repair_route": "repair_authored_poster_design_feedback",
        }
        issue["target"] = _sanitize_critic_json_for_author(issue["target"])
        issue["evidence"] = _sanitize_critic_json_for_author(issue["evidence"])
        out.append(issue)
    if out:
        return out
    summary = _sanitize_critic_text_for_author(str(report.get("summary") or "Critic requested poster-quality revisions."))
    return [{
        "id": "critic:summary",
        "issue_id": "critic:summary",
        "source": "critic",
        "severity": "high",
        "category": "quality",
        "message": summary,
        "description": summary,
        "target": {},
        "evidence": {},
        "suggested_action": "Improve the poster according to the critic summary while preserving deterministic preflight validity.",
        "repair_tool": "edit_poster_html",
        "repair_route": "repair_authored_poster_design_feedback",
    }]


_CRITIC_AUTHOR_REPORT_DROP_KEYS = {
    "repair_tool",
    "repair_route",
    "tool",
    "tool_name",
    "planner_tool",
    "stage",
}


def _sanitize_critic_text_for_author(value: str) -> str:
    replacements = {
        "propose_design_spec": "poster.html",
        "propose_paper_poster_html": "poster.html validation",
        "apply_design_ops": "poster.html",
        "finalize": "final delivery",
    }
    text = value
    for token, replacement in replacements.items():
        text = re.sub(rf"\b{re.escape(token)}\b", replacement, text)
    return text


def _sanitize_critic_json_for_author(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_critic_text_for_author(value)
    if isinstance(value, list):
        return [_sanitize_critic_json_for_author(item) for item in value]
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if str(key) in _CRITIC_AUTHOR_REPORT_DROP_KEYS:
                continue
            clean[str(key)] = _sanitize_critic_json_for_author(item)
        return clean
    return copy.deepcopy(value)


def _sanitize_critic_report_for_author(report: dict[str, Any], issues: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "score": _safe_float(report.get("score"), default=0.0),
        "verdict": str(report.get("verdict") or ""),
        "summary": _sanitize_critic_text_for_author(str(report.get("summary") or "")),
        "dimension_scores": _sanitize_critic_json_for_author(report.get("dimension_scores") or {}),
        "review_coverage": _sanitize_critic_json_for_author(report.get("review_coverage") or {}),
        "issues": copy.deepcopy(issues[:12]),
    }


def _critic_report_is_infrastructure_failure(report: dict[str, Any]) -> bool:
    if str(report.get("verdict") or "").lower() != "fail":
        return False
    summary = str(report.get("summary") or "").lower()
    if "critic api error" in summary or "max_turns" in summary or "without report_verdict" in summary:
        return True
    for issue in report.get("issues") or []:
        if isinstance(issue, dict):
            text = " ".join(str(issue.get(key) or "") for key in ("description", "message")).lower()
            if "critic api error" in text or "max_turns" in text or "without report_verdict" in text:
                return True
    return False


def _path_relative_to_attempt_or_run(path: Path | None, attempt_dir: Path) -> str:
    if not path:
        return ""
    try:
        return str(path.resolve().relative_to(attempt_dir.parent.parent.resolve()))
    except Exception:
        try:
            return str(path.resolve().relative_to(attempt_dir.resolve()))
        except Exception:
            return str(path)


def _command_no_output_feedback(
    *,
    attempt_dir: Path,
    attempt_index: int,
    invocation: dict[str, Any],
) -> dict[str, Any]:
    stdout_excerpt = _tail_text_excerpt(attempt_dir / ".designer_author_log.stdout.tmp", limit=1200)
    stderr_excerpt = _tail_text_excerpt(attempt_dir / ".designer_author_log.stderr.tmp", limit=1600)
    combined = f"{stdout_excerpt}\n{stderr_excerpt}".lower()
    timed_out = bool(invocation.get("timed_out")) or "timeout" in combined
    issue_id = "designer_author_command_timeout" if timed_out else "designer_author_command_no_output"
    reason = str(invocation.get("reason") or "designer_author_no_output")
    hint = (
        "The external coding harness exited before writing poster.html. Start the next attempt by "
        "creating a complete fixed-canvas poster.html in the attempt directory, then fill and polish "
        "it incrementally. Do not spend a long turn printing analysis or large JSON excerpts to the "
        "terminal; read local files and write the target files directly."
    )
    if timed_out:
        hint = (
            "The external coding harness hit a provider or process timeout before writing poster.html. "
            "Keep the next attempt more compact: read the quick brief and evidence packs, avoid dumping "
            "large JSON to the terminal, and write a complete poster.html early before additional polish."
        )
    issue = {
        "id": issue_id,
        "issue_id": issue_id,
        "severity": "blocking",
        "attempt": attempt_index,
        "reason": reason,
        "message": "external designer author did not produce poster.html",
        "hint": hint,
        "stdout_excerpt": stdout_excerpt,
        "stderr_excerpt": stderr_excerpt,
    }
    return {
        "category": "process",
        "error_category": "process",
        "error_message": "external designer author did not produce poster.html",
        "summary": {
            "issue_id": issue_id,
            "primary_blocking_issue_id": issue_id,
            "repair_route": "author_command_retry",
            "hint": hint,
            "issues": [issue],
            "stdout_excerpt": stdout_excerpt,
            "stderr_excerpt": stderr_excerpt,
        },
        "payload": {
            "issue_id": issue_id,
            "primary_blocking_issue_id": issue_id,
            "repair_route": "author_command_retry",
            "hint": hint,
            "issues": [issue],
            "invocation": invocation,
            "attempt": attempt_index,
            "reason": reason,
            "stdout_excerpt": stdout_excerpt,
            "stderr_excerpt": stderr_excerpt,
        },
    }


def _soft_finalizable_direct_validation_feedback(
    validation_feedback: dict[str, Any],
    attempt_index: int,
    *,
    max_attempts: int | None = None,
) -> dict[str, Any] | None:
    summary = validation_feedback.get("summary") if isinstance(validation_feedback.get("summary"), dict) else {}
    payload = validation_feedback.get("payload") if isinstance(validation_feedback.get("payload"), dict) else {}
    issue_id = str(summary.get("issue_id") or payload.get("issue_id") or "")
    if not issue_id:
        return None
    if (
        _soft_accept_item_blocks(summary, include_hard_severity=True)
        or _soft_accept_item_blocks(payload, include_hard_severity=True)
    ):
        return None
    if _feedback_has_required_blank_fill(summary, payload):
        return None
    if _feedback_has_hard_secondary_diagnostic(payload):
        return None
    if max_attempts is not None and attempt_index < max_attempts:
        return None
    if issue_id == "paper_poster_html_editorial_flow_fill_failed":
        if max_attempts is None:
            return None
        return {
            "issue_id": issue_id,
            "reason": "advisory_blank_fill_only_after_attempt_budget",
            "attempt": attempt_index,
            "max_attempts": max_attempts,
        }
    if max_attempts is None and attempt_index < _SOFT_ACCEPT_MIN_ATTEMPT:
        return None
    if issue_id == "paper_poster_html_typography_contract_failed":
        if _typography_feedback_soft_finalizable(summary, payload):
            return {"issue_id": issue_id, "reason": "near_miss_typography", "attempt": attempt_index}
        return None
    if issue_id == "paper_poster_html_source_wrap_missing":
        if _source_wrap_feedback_soft_finalizable(summary, payload):
            return {"issue_id": issue_id, "reason": "near_miss_source_wrap_readout_length", "attempt": attempt_index}
        return None
    if issue_id == "paper_poster_html_source_visual_too_small":
        if _source_visual_feedback_soft_finalizable(summary, payload):
            return {"issue_id": issue_id, "reason": "readable_source_visual_flow_fill_polish", "attempt": attempt_index}
        return None
    if issue_id == "paper_poster_html_local_flow_overflow":
        if _local_flow_feedback_soft_finalizable(summary, payload):
            return {"issue_id": issue_id, "reason": "near_miss_local_flow_overflow", "attempt": attempt_index}
        return None
    return None


def _feedback_has_required_blank_fill(summary: dict[str, Any], payload: dict[str, Any]) -> bool:
    for source in (summary, payload):
        if not isinstance(source, dict):
            continue
        if _looks_like_blank_fill_plan(source) and _blank_fill_required_targets(source):
            return True
        explicit = source.get("required_blank_fill_targets")
        if isinstance(explicit, list) and _blank_fill_required_targets({"required_targets": explicit}):
            return True
        plan = source.get("blank_fill_plan") if isinstance(source.get("blank_fill_plan"), dict) else {}
        if _blank_fill_required_targets(plan):
            return True
        blank = source.get("blank_fill")
        if isinstance(blank, dict) and _blank_fill_required_targets(blank):
            return True
        for key in ("required_co_repair", "post_overflow_required_followup"):
            container = source.get(key)
            if not isinstance(container, dict):
                continue
            blank = container.get("blank_fill")
            if isinstance(blank, dict) and _blank_fill_required_targets(blank):
                return True
    return False


def _looks_like_blank_fill_plan(source: dict[str, Any]) -> bool:
    return any(
        key in source
        for key in (
            "blank_fill_required",
            "required_targets",
            "advisory_targets",
            "suppressed_targets",
            "required_target_count",
            "advisory_target_count",
        )
    )


def _feedback_has_hard_secondary_diagnostic(payload: dict[str, Any]) -> bool:
    diagnostics = payload.get("secondary_gate_issues")
    if not isinstance(diagnostics, list):
        return False
    hard_issue_ids = {
        "paper_poster_html_row_allocation_density_regression",
        "paper_poster_html_post_overflow_density_conservation_failed",
        "paper_poster_html_identity_header_only_failed",
        "paper_poster_html_source_required_assets_missing",
        "paper_poster_html_source_visual_repair_regression",
        "paper_poster_html_unsafe_external_asset_ref",
        "paper_poster_html_severe_text_clipping",
        "paper_poster_html_severe_text_overlap",
        "designer_author_local_repair_scope_violation",
    }
    for diagnostic in diagnostics[:12]:
        if not isinstance(diagnostic, dict):
            continue
        if diagnostic.get("blocks_soft_accept") is True:
            return True
        issue_id = str(diagnostic.get("issue_id") or "")
        if _feedback_has_required_blank_fill(diagnostic, diagnostic):
            return True
        if issue_id == "paper_poster_html_local_flow_overflow":
            if _local_flow_feedback_soft_finalizable(diagnostic, diagnostic):
                continue
            return True
        if issue_id == "paper_poster_html_source_visual_too_small":
            if _source_visual_feedback_soft_finalizable(diagnostic, diagnostic):
                continue
            return True
        if issue_id == "paper_poster_html_source_wrap_missing":
            if _source_wrap_feedback_soft_finalizable(diagnostic, diagnostic):
                continue
            return True
        if issue_id == "paper_poster_html_typography_contract_failed":
            if _typography_feedback_soft_finalizable(diagnostic, diagnostic):
                continue
            return True
        if issue_id in hard_issue_ids:
            return True
        if _secondary_diagnostic_is_soft_polish(diagnostic):
            continue
        if str(diagnostic.get("severity") or "") == "hard":
            return True
        if issue_id == "paper_poster_html_editorial_flow_fill_failed":
            if _feedback_has_required_blank_fill(diagnostic, diagnostic):
                return True
        elif issue_id:
            return True
    return False


def _secondary_diagnostic_is_soft_polish(diagnostic: dict[str, Any]) -> bool:
    if diagnostic.get("soft_finalizable") is not True:
        return False
    if diagnostic.get("blocks_soft_accept") is True:
        return False
    if diagnostic.get("visible_overflow") is True:
        return False
    if _safe_int(diagnostic.get("hard_issue_count"), default=0) > 0:
        return False
    severity = str(diagnostic.get("severity") or "")
    if severity and severity not in {"advisory", "near_miss", "polish"}:
        return False
    for issue in _feedback_issues(diagnostic, diagnostic):
        if issue.get("blocks_soft_accept") is True:
            return False
        if issue.get("visible_overflow") is True:
            return False
        issue_severity = str(issue.get("severity") or "")
        if issue_severity and issue_severity not in {"advisory", "near_miss", "polish"}:
            return False
        if issue.get("soft_finalizable") is not True:
            return False
    return True


def _feedback_issues(summary: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    issues = payload.get("issues") if isinstance(payload.get("issues"), list) else summary.get("issues")
    return [issue for issue in (issues or []) if isinstance(issue, dict)]


def _soft_accept_item_blocks(item: dict[str, Any], *, include_hard_severity: bool = False) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("blocks_soft_accept") is True:
        return True
    if item.get("soft_finalizable") is False:
        return True
    severity = str(item.get("severity") or "").strip().lower()
    return include_hard_severity and severity in {"hard", "required", "blocking", "error"}


def _soft_accept_issue_allows(issue: dict[str, Any]) -> bool:
    if _soft_accept_item_blocks(issue, include_hard_severity=True):
        return False
    severity = str(issue.get("severity") or "").strip().lower()
    return issue.get("soft_finalizable") is True or severity in {"advisory", "near_miss", "polish"}


def _typography_feedback_soft_finalizable(summary: dict[str, Any], payload: dict[str, Any]) -> bool:
    issues = _feedback_issues(summary, payload)
    if not issues:
        return False
    for issue in issues:
        if str(issue.get("failure_kind") or "") != "body_line_height_unsafe":
            return False
        if not _soft_accept_issue_allows(issue):
            return False
        ratio = _safe_float(issue.get("actual_line_height"), default=0.0)
        if not (1.04 <= ratio < 1.08 or 1.35 < ratio <= 1.45):
            return False
    return True


def _source_wrap_feedback_soft_finalizable(summary: dict[str, Any], payload: dict[str, Any]) -> bool:
    issues = _feedback_issues(summary, payload)
    if not issues:
        return False
    for issue in issues:
        if str(issue.get("failure_kind") or "") != "local_readout_too_long":
            return False
        if not _soft_accept_issue_allows(issue):
            return False
        local_words = _safe_int(issue.get("local_words"), default=999)
        if local_words > _SOURCE_WRAP_SOFT_LOCAL_WORD_LIMIT:
            return False
        if issue.get("flow_unit_violations") or issue.get("separate_layout_evidence") or issue.get("visible_figcaption"):
            return False
    return True


def _source_visual_feedback_soft_finalizable(summary: dict[str, Any], payload: dict[str, Any]) -> bool:
    issues = _feedback_issues(summary, payload)
    if not issues:
        return False
    for issue in issues:
        if not _soft_accept_issue_allows(issue):
            return False
        failure_kind = str(issue.get("failure_kind") or "")
        target_problem = str(issue.get("target_problem") or "")
        reasons = {str(reason) for reason in (issue.get("reasons") or []) if str(reason)}
        panel_overflow = _safe_int(issue.get("panel_scroll_overflow_px"), default=0)
        if target_problem == "readable_visual_wrapper_polish":
            if (
                str(issue.get("severity") or "") == "near_miss"
                and issue.get("soft_finalizable") is True
                and issue.get("blocks_soft_accept") is not True
                and panel_overflow <= 0
            ):
                continue
            return False
        if target_problem == "minor_geometry_gap" or failure_kind == "minor_geometry_gap":
            if (
                issue.get("threshold_gap_is_minor") is True
                and issue.get("acceptance_mode") in (None, "", "local_composition")
                and not (reasons & {"contain_wrapper_underfilled", "wrapper_aspect_mismatch"})
                and panel_overflow <= 0
            ):
                continue
            return False
        if target_problem in {"source_visual_flow_underfilled", "readable_visual_flow_underfilled"} or failure_kind in {
            "source_visual_flow_underfilled",
            "readable_visual_flow_underfilled",
        }:
            if (
                target_problem == "source_visual_flow_underfilled"
                or failure_kind == "source_visual_flow_underfilled"
            ) and issue.get("width_only_readable_visual") is not True:
                return False
            if (
                issue.get("width_only_readable_visual") is not False
                and issue.get("acceptance_mode") in (None, "", "same_flow_fill")
                and reasons <= {"width_ratio_low"}
                and panel_overflow <= 0
            ):
                continue
            return False
        if (
            str(issue.get("severity") or "") == "near_miss"
            and issue.get("soft_finalizable") is True
            and issue.get("blocks_soft_accept") is not True
            and panel_overflow <= 0
        ):
            continue
        return False
    return True


def _local_flow_feedback_soft_finalizable(summary: dict[str, Any], payload: dict[str, Any]) -> bool:
    for source in (summary, payload):
        if _soft_accept_item_blocks(source, include_hard_severity=True):
            return False
        if str(source.get("severity") or "near_miss") != "near_miss":
            return False
        if source.get("soft_finalizable") is False:
            return False
        if source.get("visible_overflow") is True:
            return False
        if _safe_int(source.get("hard_issue_count"), default=0) > 0:
            return False
    if summary.get("soft_finalizable") is not True and payload.get("soft_finalizable") is not True:
        return False
    issues = _feedback_issues(summary, payload)
    if not issues:
        return False
    for issue in issues:
        if not _soft_accept_issue_allows(issue):
            return False
        if str(issue.get("severity") or "") != "near_miss":
            return False
        if issue.get("visible_overflow") is True:
            return False
        if _scroll_bottom_overflow_px(issue) > 8:
            return False
    return True


def _validation_feedback(
    *,
    tool_name: str,
    result: ToolResultRecord,
    attempt_index: int,
    attempt_dir: Path,
) -> dict[str, Any]:
    payload = result.payload or {}
    summary_keys = (
        "issue_id",
        "repair_route",
        "hint",
        "local_repair_hint",
        "candidate_id",
        "candidate_relative_dir",
        "candidate_preview_png",
        "candidate_preview_png_relative",
        "candidate_measurement_json",
        "candidate_measurement_json_relative",
        "candidate_validation_overlay_png",
        "candidate_validation_overlay_png_relative",
        "candidate_visual_repair_packet_json",
        "candidate_visual_repair_packet_json_relative",
        "candidate_visual_repair_dir",
        "candidate_visual_repair_dir_relative",
        "candidate_score",
        "candidate_score_reasons",
        "locked_base_candidate_id",
        "locked_base_candidate_relative_dir",
        "locked_base_candidate_preview_png",
        "locked_base_candidate_preview_png_relative",
        "locked_base_candidate_measurement_json",
        "locked_base_candidate_measurement_json_relative",
        "locked_base_candidate_visual_repair_packet_json",
        "locked_base_candidate_visual_repair_dir",
        "locked_base_candidate_score",
        "visual_fill_feedback",
        "visual_repair_packet",
        "row_allocation_reasons",
        "primary_blocking_issue_id",
        "secondary_gate_issues",
        "all_gate_diagnostics",
        "density_conservation",
        "repair_scope",
        "target_block_ids",
        "target_selectors",
        "allowed_selectors",
        "forbidden_selectors",
        "preserve_selectors",
        "validation_stage",
    )
    summary = {key: payload.get(key) for key in summary_keys if payload.get(key) not in (None, "")}
    issues = payload.get("issues")
    if isinstance(issues, list):
        summary["issues"] = issues[:8]
    return {
        "version": 1,
        "tool": tool_name,
        "attempt": attempt_index,
        "attempt_dir": str(attempt_dir),
        "status": result.status,
        "error_message": result.error_message or "",
        "error_category": result.error_category or "",
        "summary": summary,
        "payload": payload,
    }


def _post_composite_feedback(
    *,
    ctx: ToolContext,
    tool_name: str,
    result: ToolResultRecord,
    attempt_index: int,
    attempt_dir: Path,
) -> dict[str, Any] | None:
    payload = dict(result.payload or {})
    blocking = _post_composite_blocking_findings(ctx, payload)
    if not blocking:
        return None
    hint = (
        "Composite/finalize found blocking design feedback. Use previous_poster.html as a read-only "
        "baseline when present, write the revised candidate to poster.html, and repair only the named "
        "panels/assets before re-running."
    )
    payload.update({
        "issue_id": "designer_author_post_composite_blockers",
        "repair_route": "repair_authored_poster_design_feedback",
        "hint": hint,
        "issues": blocking[:8],
        "blocking_findings": blocking[:12],
        "feedback_tool": tool_name,
    })
    summary = {
        "issue_id": "designer_author_post_composite_blockers",
        "repair_route": "repair_authored_poster_design_feedback",
        "hint": hint,
        "issues": blocking[:8],
        "blocking_findings": blocking[:8],
        "feedback_tool": tool_name,
    }
    return {
        "version": 1,
        "tool": tool_name,
        "attempt": attempt_index,
        "attempt_dir": str(attempt_dir),
        "status": result.status,
        "error_message": result.error_message or "",
        "error_category": result.error_category or "",
        "summary": summary,
        "payload": payload,
    }


def _post_composite_blocking_findings(ctx: ToolContext, payload: dict[str, Any]) -> list[dict[str, Any]]:
    del ctx
    explicit = payload.get("blocking_findings")
    if isinstance(explicit, list) and explicit:
        return [item for item in explicit if isinstance(item, dict)]
    feedback = payload.get("design_feedback")
    return blocking_design_findings(feedback)


def _active_feedback_for_repair(feedback: dict[str, Any] | None) -> dict[str, Any]:
    current = feedback if isinstance(feedback, dict) else {}
    seen: set[int] = set()
    while isinstance(current, dict) and id(current) not in seen:
        seen.add(id(current))
        summary = current.get("summary") if isinstance(current.get("summary"), dict) else {}
        payload = current.get("payload") if isinstance(current.get("payload"), dict) else {}
        issue_id = str(summary.get("issue_id") or payload.get("issue_id") or "")
        if issue_id != "designer_author_repair_noop":
            return current
        previous = payload.get("previous_feedback")
        if not isinstance(previous, dict):
            return current
        current = previous
    return current if isinstance(current, dict) else {}


def _underlying_feedback_for_visual_repair(feedback: dict[str, Any] | None) -> dict[str, Any]:
    active = _active_feedback_for_repair(feedback)
    summary = active.get("summary") if isinstance(active.get("summary"), dict) else {}
    payload = active.get("payload") if isinstance(active.get("payload"), dict) else {}
    issue_id = str(summary.get("issue_id") or payload.get("issue_id") or "")
    if issue_id != "designer_author_local_repair_scope_violation":
        return active
    previous = payload.get("previous_feedback")
    if isinstance(previous, dict):
        return _active_feedback_for_repair(previous)
    return active


def _source_visual_repair_regression_feedback(
    previous_feedback: dict[str, Any] | None,
    validation_feedback: dict[str, Any],
) -> dict[str, Any]:
    previous_active = _active_feedback_for_repair(previous_feedback)
    previous_summary = previous_active.get("summary") if isinstance(previous_active.get("summary"), dict) else {}
    previous_payload = previous_active.get("payload") if isinstance(previous_active.get("payload"), dict) else {}
    previous_issue_id = str(previous_summary.get("issue_id") or previous_payload.get("issue_id") or "")
    if previous_issue_id != "paper_poster_html_source_visual_too_small":
        return validation_feedback
    summary = validation_feedback.get("summary") if isinstance(validation_feedback.get("summary"), dict) else {}
    payload = validation_feedback.get("payload") if isinstance(validation_feedback.get("payload"), dict) else {}
    issue_id = str(summary.get("issue_id") or payload.get("issue_id") or "")
    if issue_id not in {
        "paper_poster_html_severe_text_overlap",
        "paper_poster_html_local_flow_overflow",
        "paper_poster_html_source_coverage_low",
    }:
        return validation_feedback
    previous_issues = previous_summary.get("issues") or previous_payload.get("issues") or []
    previous_issues = previous_issues if isinstance(previous_issues, list) else []
    wrapped = copy.deepcopy(validation_feedback)
    wrapped_summary = wrapped.setdefault("summary", {})
    wrapped_payload = wrapped.setdefault("payload", {})
    if not isinstance(wrapped_summary, dict) or not isinstance(wrapped_payload, dict):
        return validation_feedback
    original_summary = copy.deepcopy(summary)
    original_payload = copy.deepcopy(payload)
    wrapped_summary.update({
        "issue_id": "paper_poster_html_source_visual_repair_regression",
        "repair_route": "repair_source_visual_without_new_overlap_or_overflow",
        "primary_blocking_issue_id": "paper_poster_html_source_visual_repair_regression",
        "issues": copy.deepcopy(previous_issues),
        "source_visual_regression": {
            "new_issue_id": issue_id,
            "regression_issue_id": issue_id,
            "regression_repair_route": summary.get("repair_route") or payload.get("repair_route"),
            "regression_issues": original_summary.get("issues") or original_payload.get("issues") or [],
        },
        "hint": (
            "The previous source-visual repair cleared or changed the visual-size gate but introduced a new "
            f"{issue_id} blocker. Repair the listed source visual targets while also clearing that regression; "
            "do not hand off to a separate broad heading/body rewrite. If the previous repair used broad image "
            "resizing or global column/row changes for a readable flow-fill target, undo that strategy and fill "
            "the same source-flow unit with source-backed readout/native rows instead."
        ),
    })
    wrapped_payload.update({
        "issue_id": "paper_poster_html_source_visual_repair_regression",
        "repair_route": "repair_source_visual_without_new_overlap_or_overflow",
        "primary_blocking_issue_id": "paper_poster_html_source_visual_repair_regression",
        "issues": copy.deepcopy(previous_issues),
        "source_visual_regression": wrapped_summary["source_visual_regression"],
        "original_validation_feedback": {
            "summary": original_summary,
            "payload": original_payload,
        },
    })
    return wrapped


def _noop_repair_metadata(feedback: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(feedback, dict):
        return {}
    summary = feedback.get("summary") if isinstance(feedback.get("summary"), dict) else {}
    payload = feedback.get("payload") if isinstance(feedback.get("payload"), dict) else {}
    issue_id = str(summary.get("issue_id") or payload.get("issue_id") or "")
    if issue_id != "designer_author_repair_noop":
        return {}
    active = _active_feedback_for_repair(feedback)
    active_summary = active.get("summary") if isinstance(active.get("summary"), dict) else {}
    active_payload = active.get("payload") if isinstance(active.get("payload"), dict) else {}
    return {
        "issue_id": "designer_author_repair_noop",
        "hint": summary.get("hint") or payload.get("hint") or "",
        "previous_issue_id": (
            summary.get("previous_issue_id")
            or payload.get("original_issue_id")
            or active_summary.get("issue_id")
            or active_payload.get("issue_id")
            or ""
        ),
        "previous_repair_route": (
            payload.get("original_repair_route")
            or active_summary.get("repair_route")
            or active_payload.get("repair_route")
            or ""
        ),
    }


def _build_repair_context(ctx: ToolContext, attempt_dir: Path, feedback: dict[str, Any]) -> dict[str, Any]:
    del attempt_dir
    noop_metadata = _noop_repair_metadata(feedback)
    active_feedback = _active_feedback_for_repair(feedback)
    summary = active_feedback.get("summary") if isinstance(active_feedback.get("summary"), dict) else {}
    payload = active_feedback.get("payload") if isinstance(active_feedback.get("payload"), dict) else {}
    issue_id = str(summary.get("issue_id") or payload.get("issue_id") or "")
    issues = summary.get("issues") or payload.get("issues") or []
    issues = issues if isinstance(issues, list) else []
    visual_fill_feedback = summary.get("visual_fill_feedback") or payload.get("visual_fill_feedback") or {}
    row_allocation_reasons = summary.get("row_allocation_reasons") or payload.get("row_allocation_reasons") or []
    secondary_gate_issues = summary.get("secondary_gate_issues") or payload.get("secondary_gate_issues") or []
    all_gate_diagnostics = summary.get("all_gate_diagnostics") or payload.get("all_gate_diagnostics") or []
    density_conservation = summary.get("density_conservation") or payload.get("density_conservation") or {}
    visual_repair_packet = summary.get("visual_repair_packet") or payload.get("visual_repair_packet") or {}
    source_visual_regression = summary.get("source_visual_regression") or payload.get("source_visual_regression") or {}
    blank_fill_plan = _blank_fill_plan_from_feedback(summary, payload)
    active_primary_advisory_blank_fill_plan = _active_primary_advisory_blank_fill_plan_from_feedback(summary, payload)
    if not _blank_fill_plan_has_targets(blank_fill_plan) and _blank_fill_plan_has_targets(active_primary_advisory_blank_fill_plan):
        blank_fill_plan = active_primary_advisory_blank_fill_plan
    advisory_blank_fill_plan = _advisory_blank_fill_plan_from_feedback(summary, payload)
    if _blank_fill_plan_has_targets(active_primary_advisory_blank_fill_plan):
        active_keys = {
            _blank_fill_prompt_target_key(target)
            for target in active_primary_advisory_blank_fill_plan.get("targets") or []
            if isinstance(target, dict)
        }
        advisory_blank_fill_plan = _blank_fill_plan_without_target_keys(advisory_blank_fill_plan, active_keys)
    required_co_repair = _required_co_repair_from_feedback(summary, payload)
    post_overflow_followup = _post_overflow_followup_from_feedback(summary, payload)
    repair_route = str(summary.get("repair_route") or payload.get("repair_route") or "")
    max_overflow = _max_scroll_overflow_px(issues)
    classification = _repair_context_classification(
        issue_id=issue_id,
        repair_route=repair_route,
        issues=issues,
        max_scroll_overflow_px=max_overflow,
        visual_fill_feedback=visual_fill_feedback if isinstance(visual_fill_feedback, dict) else {},
        blank_fill_plan=blank_fill_plan if isinstance(blank_fill_plan, dict) else {},
    )
    row_allocation_context = _row_allocation_context_from_issues(issues, row_allocation_reasons)
    base_repair_scope = _repair_scope_from_feedback(summary, payload, issues, classification)
    blank_fill_context = _blank_fill_context_from_plan(blank_fill_plan)
    advisory_blank_fill_context = _blank_fill_context_from_plan(advisory_blank_fill_plan)
    blank_fill_scope = _repair_scope_from_blank_fill(blank_fill_context)
    if blank_fill_scope and (classification == "blank_fill_repair" or required_co_repair or post_overflow_followup):
        base_repair_scope = _merge_repair_scopes(base_repair_scope, blank_fill_scope)
    context_hint = str(summary.get("hint") or payload.get("hint") or "")
    context_local_hint = str(summary.get("local_repair_hint") or payload.get("local_repair_hint") or "")
    reference_style = _reference_style_contract(ctx)
    if reference_style:
        style_tokens = reference_style.get("style_tokens")
        columns = style_tokens.get("column_structure") if isinstance(style_tokens, dict) else None
        exact_counts = columns.get("major_sections_per_column") if isinstance(columns, dict) else None
        context_hint = (
            "Preserve the active reference-owned structure"
            + (f" with exact major-section counts {exact_counts}" if isinstance(exact_counts, list) else "")
            + "; ignore stale generic layout ranges."
        )
        if context_local_hint:
            context_local_hint = (
                "Use the active reference contract plus the structured issues and repair_scope below; "
                "ignore stale free-text layout guidance from earlier non-reference validation."
            )
    context = {
        "version": 1,
        "classification": classification,
        "issue_id": issue_id,
        "repair_route": repair_route,
        "max_scroll_overflow_px": max_overflow,
        "issues": issues[:8],
        "hint": context_hint,
        "local_repair_hint": context_local_hint,
        "primary_blocking_issue_id": summary.get("primary_blocking_issue_id") or payload.get("primary_blocking_issue_id") or issue_id,
        "secondary_gate_issues": secondary_gate_issues[:8] if isinstance(secondary_gate_issues, list) else [],
        "all_gate_diagnostics": all_gate_diagnostics[:12] if isinstance(all_gate_diagnostics, list) else [],
        "repair_scope": base_repair_scope,
        "current_candidate": {
            "candidate_id": summary.get("candidate_id") or payload.get("candidate_id"),
            "candidate_relative_dir": summary.get("candidate_relative_dir") or payload.get("candidate_relative_dir"),
            "preview_png": (
                summary.get("candidate_preview_png_relative")
                or summary.get("candidate_preview_png")
                or payload.get("candidate_preview_png_relative")
                or payload.get("candidate_preview_png")
            ),
            "measurement_json": (
                summary.get("candidate_measurement_json_relative")
                or summary.get("candidate_measurement_json")
                or payload.get("candidate_measurement_json_relative")
                or payload.get("candidate_measurement_json")
            ),
            "validation_overlay_png": (
                summary.get("candidate_validation_overlay_png_relative")
                or summary.get("candidate_validation_overlay_png")
                or payload.get("candidate_validation_overlay_png_relative")
                or payload.get("candidate_validation_overlay_png")
            ),
            "visual_repair_packet_json": (
                summary.get("candidate_visual_repair_packet_json_relative")
                or summary.get("candidate_visual_repair_packet_json")
                or payload.get("candidate_visual_repair_packet_json_relative")
                or payload.get("candidate_visual_repair_packet_json")
            ),
            "visual_repair_dir": (
                summary.get("candidate_visual_repair_dir_relative")
                or summary.get("candidate_visual_repair_dir")
                or payload.get("candidate_visual_repair_dir_relative")
                or payload.get("candidate_visual_repair_dir")
            ),
            "candidate_score": summary.get("candidate_score") or payload.get("candidate_score"),
            "candidate_score_reasons": summary.get("candidate_score_reasons") or payload.get("candidate_score_reasons") or [],
        },
        "locked_base_candidate": {
            "candidate_id": summary.get("locked_base_candidate_id") or payload.get("locked_base_candidate_id"),
            "candidate_relative_dir": (
                summary.get("locked_base_candidate_relative_dir")
                or payload.get("locked_base_candidate_relative_dir")
            ),
            "preview_png": (
                summary.get("locked_base_candidate_preview_png_relative")
                or summary.get("locked_base_candidate_preview_png")
                or payload.get("locked_base_candidate_preview_png_relative")
                or payload.get("locked_base_candidate_preview_png")
            ),
            "measurement_json": (
                summary.get("locked_base_candidate_measurement_json_relative")
                or summary.get("locked_base_candidate_measurement_json")
                or payload.get("locked_base_candidate_measurement_json_relative")
                or payload.get("locked_base_candidate_measurement_json")
            ),
            "visual_repair_packet_json": (
                summary.get("locked_base_candidate_visual_repair_packet_json")
                or payload.get("locked_base_candidate_visual_repair_packet_json")
            ),
            "visual_repair_dir": (
                summary.get("locked_base_candidate_visual_repair_dir")
                or payload.get("locked_base_candidate_visual_repair_dir")
            ),
            "candidate_score": summary.get("locked_base_candidate_score") or payload.get("locked_base_candidate_score"),
        },
        "row_allocation": row_allocation_context,
        "lower_band_fill": _lower_band_fill_context(visual_fill_feedback if isinstance(visual_fill_feedback, dict) else {}),
        "visual_fill_feedback": visual_fill_feedback,
    }
    if blank_fill_context:
        context["blank_fill"] = blank_fill_context
    if advisory_blank_fill_context:
        context["advisory_blank_fill"] = advisory_blank_fill_context
    if isinstance(required_co_repair, dict) and required_co_repair:
        context["required_co_repair"] = required_co_repair
    if isinstance(post_overflow_followup, dict) and post_overflow_followup:
        context["post_overflow_required_followup"] = post_overflow_followup
    if isinstance(visual_repair_packet, dict) and visual_repair_packet:
        context["visual_repair_packet"] = visual_repair_packet
    if isinstance(density_conservation, dict) and density_conservation:
        context["density_conservation"] = density_conservation
    if classification == "row_allocation_failure":
        context["global_overflow_repair_plan"] = _global_overflow_repair_plan_from_row_allocation(row_allocation_context)
    if classification == "local_flow_overflow":
        context["local_flow"] = _local_flow_context_from_issues(issues)
    if classification == "source_wrap_failure":
        context["source_wrap"] = _source_wrap_context_from_issues(issues)
    if classification == "source_visual_sizing_failure":
        context["source_visual_sizing"] = _source_visual_sizing_context_from_issues(issues)
        synthesized_blank_fill_plan = {}
        if not blank_fill_context:
            synthesized_blank_fill_plan = _blank_fill_plan_from_source_visual_sizing(context["source_visual_sizing"])
            synthesized_blank_fill_context = _blank_fill_context_from_plan(synthesized_blank_fill_plan)
            if synthesized_blank_fill_context:
                context["blank_fill"] = synthesized_blank_fill_context
                context["required_co_repair"] = {
                    **(context.get("required_co_repair") if isinstance(context.get("required_co_repair"), dict) else {}),
                    "blank_fill": synthesized_blank_fill_plan,
                    "reason": "source_visual same-flow underfill must be repaired as blank-fill in the same attempt",
                }
                blank_fill_scope = _repair_scope_from_blank_fill(synthesized_blank_fill_context)
        source_visual_scope = _repair_scope_from_source_visual_sizing(context["source_visual_sizing"])
        if source_visual_scope or blank_fill_scope:
            context["repair_scope"] = _merge_repair_scopes(source_visual_scope, blank_fill_scope)
        if isinstance(source_visual_regression, dict) and source_visual_regression:
            context["source_visual_sizing"]["post_fix_regression"] = source_visual_regression
    if classification == "typography_contract_failure":
        context["typography"] = _typography_context_from_feedback(ctx, summary, payload, issues)
    if classification == "reference_style_failure":
        context["reference_style"] = {
            "style_reference_id": _reference_style_id(ctx),
            "contract": copy.deepcopy(_reference_style_for_author(ctx)),
            "issues": copy.deepcopy(issues[:12]),
        }
    if classification == "identity_header_failure":
        context["identity_header"] = _identity_header_context_from_issues(issues)
    if classification == "heading_flow_overflow":
        context["heading_flow"] = _heading_flow_context_from_issues(issues)
    if classification == "section_content_overflow":
        context["section_content"] = _section_content_context_from_issues(issues)
    if classification == "density_conservation_failure":
        context["density_conservation"] = _density_conservation_context_from_feedback(summary, payload, issues)
    if classification == "local_repair_scope_violation":
        context["local_repair_scope_violation"] = _local_repair_scope_context_from_feedback(summary, payload)
        original_context = context["local_repair_scope_violation"].get("original_repair_context")
        if (
            isinstance(original_context, dict)
            and original_context.get("classification") == "source_visual_sizing_failure"
            and isinstance(original_context.get("source_visual_sizing"), dict)
        ):
            context["underlying_classification"] = "source_visual_sizing_failure"
            context["underlying_issue_id"] = original_context.get("issue_id")
            context["underlying_repair_route"] = original_context.get("repair_route")
            context["primary_blocking_issue_id"] = (
                original_context.get("primary_blocking_issue_id")
                or original_context.get("issue_id")
                or context.get("primary_blocking_issue_id")
            )
            context["source_visual_sizing"] = copy.deepcopy(original_context["source_visual_sizing"])
            if isinstance(original_context.get("repair_scope"), dict):
                context["repair_scope"] = copy.deepcopy(original_context["repair_scope"])
            if isinstance(original_context.get("visual_repair_packet"), dict):
                context["visual_repair_packet"] = copy.deepcopy(original_context["visual_repair_packet"])
        if (
            isinstance(original_context, dict)
            and (
                original_context.get("classification") == "blank_fill_repair"
                or isinstance(original_context.get("blank_fill"), dict)
            )
        ):
            context["underlying_classification"] = str(original_context.get("classification") or "blank_fill_repair")
            context["underlying_issue_id"] = original_context.get("issue_id")
            context["underlying_repair_route"] = original_context.get("repair_route")
            if isinstance(original_context.get("blank_fill"), dict):
                context["blank_fill"] = copy.deepcopy(original_context["blank_fill"])
            if isinstance(original_context.get("required_co_repair"), dict):
                context["required_co_repair"] = copy.deepcopy(original_context["required_co_repair"])
            if isinstance(original_context.get("repair_scope"), dict):
                context["repair_scope"] = copy.deepcopy(original_context["repair_scope"])
            if isinstance(original_context.get("visual_repair_packet"), dict):
                context["visual_repair_packet"] = copy.deepcopy(original_context["visual_repair_packet"])
    if classification == "post_composite_design_feedback":
        context["post_composite_feedback"] = _post_composite_context_from_feedback(summary, payload)
    attempt_critic_feedback = payload.get("attempt_level_critic_feedback") or summary.get("attempt_level_critic_feedback")
    if isinstance(attempt_critic_feedback, dict) and attempt_critic_feedback:
        context["attempt_level_critic_feedback"] = copy.deepcopy(attempt_critic_feedback)
    if noop_metadata:
        context["noop"] = noop_metadata
    current_score = _safe_int(context["current_candidate"].get("candidate_score"), default=0)
    locked_score = _safe_int(context["locked_base_candidate"].get("candidate_score"), default=0)
    if current_score or locked_score:
        context["current_vs_locked_base"] = {
            "candidate_score_delta": current_score - locked_score,
            "current_candidate_score": current_score,
            "locked_base_candidate_score": locked_score,
        }
    return context


def _repair_context_classification(
    *,
    issue_id: str,
    repair_route: str,
    issues: list[Any],
    max_scroll_overflow_px: int,
    visual_fill_feedback: dict[str, Any],
    blank_fill_plan: dict[str, Any],
) -> str:
    if issue_id == "paper_poster_html_root_wrapper_padding_overflow":
        return "root_wrapper_padding_overflow"
    if issue_id == "paper_poster_html_designer_flow_canvas_overflow":
        return "row_allocation_failure"
    if issue_id == "paper_poster_html_source_wrap_missing":
        return "source_wrap_failure"
    if issue_id == "paper_poster_html_source_visual_too_small":
        if _blank_fill_plan_has_targets(blank_fill_plan) and _blank_fill_plan_is_source_flow(blank_fill_plan):
            return "blank_fill_repair"
        return "source_visual_sizing_failure"
    if issue_id == "paper_poster_html_source_visual_repair_regression":
        return "source_visual_sizing_failure"
    if issue_id == "paper_poster_html_typography_contract_failed":
        return "typography_contract_failure"
    if issue_id == "paper_poster_html_reference_style_contract_failed":
        return "reference_style_failure"
    if issue_id in {
        "paper_poster_html_identity_header_only_failed",
        "paper_poster_html_unsafe_external_asset_ref",
    }:
        return "identity_header_failure"
    if issue_id == "paper_poster_html_heading_flow_overflow":
        return "heading_flow_overflow"
    if issue_id == "paper_poster_html_post_overflow_density_conservation_failed":
        return "density_conservation_failure"
    if issue_id in {
        "paper_poster_html_editorial_flow_fill_failed",
        "paper_poster_html_blank_fill_regression",
    }:
        return "blank_fill_repair"
    if issue_id == "designer_author_local_repair_scope_violation":
        return "local_repair_scope_violation"
    if issue_id == "designer_author_post_composite_blockers":
        if repair_route == "revise_typography_system" or _has_typography_feedback_issue(issues):
            return "typography_contract_failure"
        return "post_composite_design_feedback"
    if issue_id in {
        "designer_author_command_no_output",
        "designer_author_command_timeout",
    }:
        return "command_no_output"
    if issue_id == "paper_poster_html_row_allocation_density_regression":
        return "row_allocation_failure"
    if issue_id == "paper_poster_html_local_flow_overflow":
        valid_issues = [issue for issue in issues if isinstance(issue, dict)]
        if _has_severe_fill_feedback(visual_fill_feedback):
            return "row_allocation_failure"
        if _has_section_content_overflow_issue(valid_issues):
            return "section_content_overflow"
        if max_scroll_overflow_px <= 32 and len(valid_issues) <= 1:
            return "micro_overflow"
        if valid_issues:
            return "local_flow_overflow"
        return "row_allocation_failure"
    if _has_severe_fill_feedback(visual_fill_feedback):
        return "row_allocation_failure"
    return "generic_validation_failure"


def _has_section_content_overflow_issue(issues: list[dict[str, Any]]) -> bool:
    for issue in issues:
        if str(issue.get("container_kind") or "") in {"poster_section", "source_flow_unit", "figure_flow_unit"}:
            return True
        section_id = str(issue.get("section_id") or "")
        container_id = str(issue.get("container_id") or "")
        if section_id and container_id and section_id == container_id:
            return True
    return False


def _has_typography_feedback_issue(issues: list[Any]) -> bool:
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        if str(issue.get("repair_route") or "") == "revise_typography_system":
            return True
        if str(issue.get("stage") or "") == "typography_system":
            return True
        if str(issue.get("issue_id") or "") == "paper_poster_html_typography_contract_failed":
            return True
        if str(issue.get("failure_kind") or "").startswith((
            "font_",
            "body_weight",
            "body_italic",
            "body_line_height",
            "heading_weight",
        )):
            return True
    return False


def _max_scroll_overflow_px(issues: list[Any]) -> int:
    max_overflow = 0
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        scroll = issue.get("scroll_overflow_px") if isinstance(issue.get("scroll_overflow_px"), dict) else {}
        max_overflow = max(max_overflow, _safe_int(scroll.get("bottom"), default=0))
    return max_overflow


def _repair_scope_from_feedback(
    summary: dict[str, Any],
    payload: dict[str, Any],
    issues: list[Any],
    classification: str,
) -> dict[str, Any]:
    for source in (summary, payload):
        scope = source.get("repair_scope")
        if isinstance(scope, dict) and scope:
            return scope
    target_ids: list[str] = []
    allowed: list[str] = []
    forbidden: list[str] = []
    preserve: list[str] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        scope = issue.get("repair_scope") if isinstance(issue.get("repair_scope"), dict) else {}
        target_ids.extend(_string_list(scope.get("target_block_ids") or issue.get("target_block_ids")))
        allowed.extend(_string_list(scope.get("allowed_selectors") or issue.get("allowed_selectors")))
        forbidden.extend(_string_list(scope.get("forbidden_selectors") or issue.get("forbidden_selectors")))
        preserve.extend(_string_list(scope.get("preserve_selectors") or issue.get("preserve_selectors")))
    mode = {
        "heading_flow_overflow": "heading_lane",
        "section_content_overflow": "local_section_flow",
        "local_flow_overflow": "local_section_flow",
        "micro_overflow": "local_section_flow",
        "root_wrapper_padding_overflow": "root_wrapper",
        "density_conservation_failure": "density_conservation",
    }.get(classification, classification or "unspecified")
    return {
        "mode": mode,
        "target_block_ids": _unique_strings(target_ids),
        "allowed_selectors": _unique_strings(allowed),
        "forbidden_selectors": _unique_strings(forbidden),
        "preserve_selectors": _unique_strings(preserve),
    }


def _blank_fill_plan_from_feedback(summary: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    for container_key in ("required_co_repair", "post_overflow_required_followup"):
        for source in (summary, payload):
            container = source.get(container_key)
            if not isinstance(container, dict):
                continue
            blank = container.get("blank_fill")
            if isinstance(blank, dict) and _blank_fill_plan_has_targets(blank):
                required_blank = _blank_fill_required_plan(blank)
                if _blank_fill_plan_has_targets(required_blank):
                    return required_blank
    for source in (summary, payload):
        targets = source.get("required_blank_fill_targets")
        if isinstance(targets, list) and targets:
            required_blank = _blank_fill_required_plan({
                "version": 1,
                "blank_fill_required": True,
                "targets": [target for target in targets if isinstance(target, dict)][:12],
            })
            if _blank_fill_plan_has_targets(required_blank):
                return required_blank
    for source in (summary, payload):
        plan = source.get("blank_fill_plan")
        if not isinstance(plan, dict) or not _blank_fill_plan_has_targets(plan):
            continue
        required_plan = _blank_fill_required_plan(plan)
        if _blank_fill_plan_has_targets(required_plan):
            return required_plan
    return {}


def _active_primary_advisory_blank_fill_plan_from_feedback(
    summary: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    primary_issue_id = str(
        summary.get("primary_blocking_issue_id")
        or payload.get("primary_blocking_issue_id")
        or summary.get("issue_id")
        or payload.get("issue_id")
        or ""
    )
    if primary_issue_id != "paper_poster_html_editorial_flow_fill_failed":
        return {}
    if _blank_fill_plan_has_targets(_blank_fill_plan_from_feedback(summary, payload)):
        return {}
    advisory_plan = _advisory_blank_fill_plan_from_feedback(summary, payload)
    advisory_targets = (
        advisory_plan.get("targets")
        if isinstance(advisory_plan.get("targets"), list) else
        []
    )
    if not advisory_targets:
        return {}
    active_targets: list[dict[str, Any]] = []
    for target in advisory_targets[:4]:
        if not isinstance(target, dict):
            continue
        active = copy.deepcopy(target)
        active["active_primary_advisory_repair"] = True
        active["original_promotion"] = active.get("promotion")
        active["original_blank_fill_severity"] = active.get("blank_fill_severity")
        active.setdefault("safe_primary_repair_action", active.get("primary_repair_action") or "compact_local_blank_fill")
        active.setdefault("primary_repair_action", active.get("safe_primary_repair_action") or "compact_local_blank_fill")
        active.setdefault("required_repair_mode", active.get("safe_primary_repair_action") or "active_primary_local_blank_fill")
        active_targets.append(active)
    if not active_targets:
        return {}
    return {
        **advisory_plan,
        "blank_fill_required": True,
        "active_primary_advisory_repair": True,
        "context_role": "active_primary_repair",
        "visual_reference_only": False,
        "targets": active_targets,
        "required_targets": active_targets,
        "required_target_count": len(active_targets),
        "advisory_targets": [],
        "advisory_target_count": 0,
        "instructions": (
            "Active primary blank-fill repair: locally compact, rebalance, add native rows/chips, "
            "stack asset/readout, or reduce unused section height for the listed targets."
        ),
    }


def _advisory_blank_fill_plan_from_feedback(summary: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    for source in (summary, payload):
        for key in ("advisory_blank_fill_plan", "advisory_blank_fill", "blank_fill_plan"):
            plan = source.get(key)
            if not isinstance(plan, dict) or not _blank_fill_plan_has_targets(plan):
                continue
            advisory_targets = _blank_fill_advisory_targets(plan)
            if not advisory_targets:
                continue
            return {
                **plan,
                "blank_fill_required": False,
                "required_targets": [],
                "required_target_count": 0,
                "targets": advisory_targets,
                "advisory_targets": advisory_targets,
                "advisory_target_count": len(advisory_targets),
                "visual_reference_only": True,
                "context_role": "visual_reference",
            }
    return {}


def _blank_fill_plan_without_target_keys(
    plan: dict[str, Any],
    excluded_keys: set[tuple[str, str, str]],
) -> dict[str, Any]:
    if not isinstance(plan, dict) or not excluded_keys:
        return plan if isinstance(plan, dict) else {}

    def keep_targets(values: Any) -> list[dict[str, Any]]:
        return [
            target for target in (values if isinstance(values, list) else [])
            if isinstance(target, dict) and _blank_fill_prompt_target_key(target) not in excluded_keys
        ]

    targets = keep_targets(plan.get("targets"))
    advisory_targets = keep_targets(plan.get("advisory_targets"))
    if not targets and advisory_targets:
        targets = list(advisory_targets)
    if not targets:
        return {}
    return {
        **plan,
        "blank_fill_required": False,
        "required_targets": [],
        "required_target_count": 0,
        "targets": targets[:12],
        "advisory_targets": (advisory_targets or targets)[:12],
        "advisory_target_count": len(advisory_targets or targets),
        "visual_reference_only": True,
        "context_role": "visual_reference",
    }


def _required_co_repair_from_feedback(summary: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    for source in (summary, payload):
        value = source.get("required_co_repair")
        if isinstance(value, dict) and value:
            return _normalize_required_repair_container(value)
    return {}


def _post_overflow_followup_from_feedback(summary: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    for source in (summary, payload):
        value = source.get("post_overflow_required_followup")
        if isinstance(value, dict) and value:
            return _normalize_required_repair_container(value)
    return {}


def _normalize_required_repair_container(container: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(container)
    blank = normalized.get("blank_fill")
    if isinstance(blank, dict):
        required_blank = _blank_fill_required_plan(blank)
        if _blank_fill_plan_has_targets(required_blank):
            normalized["blank_fill"] = required_blank
        else:
            normalized.pop("blank_fill", None)
    return normalized if any(value not in (None, "", {}, []) for value in normalized.values()) else {}


def _blank_fill_plan_has_targets(plan: dict[str, Any]) -> bool:
    if not isinstance(plan, dict):
        return False
    for key in ("targets", "required_targets", "advisory_targets"):
        if isinstance(plan.get(key), list) and any(isinstance(target, dict) for target in plan.get(key) or []):
            return True
    return False


def _blank_fill_required_targets(plan: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(plan, dict):
        return []
    explicit = plan.get("required_targets")
    if isinstance(explicit, list) and explicit:
        return [target for target in explicit if isinstance(target, dict)][:12]
    required: list[dict[str, Any]] = []
    targets = plan.get("targets") if isinstance(plan.get("targets"), list) else []
    for target in targets:
        if isinstance(target, dict) and _blank_fill_target_is_required(target):
            required.append(target)
    required.sort(key=lambda target: -_safe_float(target.get("visual_salience_score"), default=0.0))
    return required[:4]


def _blank_fill_advisory_targets(plan: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(plan, dict):
        return []
    required_keys = {
        _blank_fill_prompt_target_key(target)
        for target in _blank_fill_required_targets(plan)
        if isinstance(target, dict)
    }
    targets: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for key in ("targets", "advisory_targets"):
        values = plan.get(key) if isinstance(plan.get(key), list) else []
        for target in values:
            if not isinstance(target, dict):
                continue
            target_key = _blank_fill_prompt_target_key(target)
            if target_key in required_keys or target_key in seen:
                continue
            if _blank_fill_target_is_required(target):
                continue
            seen.add(target_key)
            targets.append(target)
    return targets[:12]


def _blank_fill_target_is_required(target: dict[str, Any]) -> bool:
    if not isinstance(target, dict):
        return False
    kind = str(target.get("target_kind") or "")
    local_words = _safe_int(target.get("local_word_count"), default=0)
    words_min = _safe_int(target.get("words_to_add_min"), default=0)
    remaining_safe = _safe_int(
        target.get("remaining_safe_words"),
        default=max(0, 140 - local_words - 8),
    )
    visually_obvious = _blank_fill_target_visually_obvious_for_author(target, kind=kind)
    if target.get("required_co_repair_eligible") is False and not visually_obvious:
        return False
    repair_mode = str(
        target.get("required_repair_mode")
        or target.get("safe_primary_repair_action")
        or target.get("primary_repair_action")
        or ""
    )
    compact_rebalance_required = any(
        marker in repair_mode
        for marker in ("compact", "rebalance", "stack", "reduce")
    )
    over_budget = (
        bool(target.get("over_readout_budget"))
        or remaining_safe < 8
        or (words_min > 0 and words_min > remaining_safe)
    )
    if kind in {"source_flow_side_lane", "source_flow_side_lane_tail"} and over_budget and visually_obvious:
        compact_rebalance_required = True
    if kind in {"section_tail_blank", "section_internal_gap_blank"} and words_min > 24 and visually_obvious:
        compact_rebalance_required = True
    if kind in {"source_flow_side_lane", "source_flow_side_lane_tail"} and not compact_rebalance_required:
        if remaining_safe < 8 or (words_min and words_min > remaining_safe):
            return False
    if kind == "source_flow_side_lane_tail":
        lane_gap = _safe_int(target.get("lane_tail_gap_px"), default=0)
        required_gap = _safe_int(target.get("required_blank_fill_gap_px"), default=90)
        if lane_gap < required_gap:
            return False
    if kind in {"section_tail_blank", "section_internal_gap_blank"}:
        tail_gap = _safe_int(target.get("tail_gap_px") or target.get("internal_gap_px"), default=0)
        required_blank = _safe_int(target.get("required_blank_fill_gap_px"), default=60)
        if tail_gap < required_blank:
            return False
        if words_min > 24 and not compact_rebalance_required:
            return False
    promotion = str(target.get("promotion") or target.get("blank_fill_severity") or "required")
    return promotion != "advisory" or (compact_rebalance_required and visually_obvious)


def _blank_fill_target_visually_obvious_for_author(target: dict[str, Any], *, kind: str) -> bool:
    promotion = str(target.get("promotion") or target.get("blank_fill_severity") or "").strip().lower()
    if promotion == "required":
        return True
    score = _safe_float(target.get("visual_salience_score"), default=0.0)
    blank = target.get("blank_bbox_canvas") if isinstance(target.get("blank_bbox_canvas"), dict) else {}
    blank_area = _safe_int(blank.get("w"), default=0) * _safe_int(blank.get("h"), default=0)
    if kind == "source_flow_side_lane":
        return score >= 0.56 or blank_area >= 12000
    if kind == "source_flow_side_lane_tail":
        lane_gap = _safe_int(target.get("lane_tail_gap_px"), default=0)
        required_gap = _safe_int(target.get("required_blank_fill_gap_px"), default=90)
        return lane_gap >= required_gap and (score >= 0.56 or blank_area >= 12000)
    if kind in {"section_tail_blank", "section_internal_gap_blank"}:
        confidence = str(target.get("tail_gap_confidence") or "medium")
        tail_gap = _safe_int(target.get("tail_gap_px") or target.get("internal_gap_px"), default=0)
        required_gap = _safe_int(target.get("required_blank_fill_gap_px"), default=60)
        return confidence == "high" and tail_gap >= required_gap and (score >= 0.56 or blank_area >= 12000)
    return False


def _blank_fill_required_plan(plan: dict[str, Any]) -> dict[str, Any]:
    required = _blank_fill_required_targets(plan)
    if not required:
        return {}
    return {
        **plan,
        "blank_fill_required": True,
        "required_target_count": len(required),
        "targets": required,
        "required_targets": required,
    }


def _blank_fill_plan_is_source_flow(plan: dict[str, Any]) -> bool:
    targets = _blank_fill_required_targets(plan) or (plan.get("targets") if isinstance(plan.get("targets"), list) else [])
    return any(
        isinstance(target, dict)
        and str(target.get("target_kind") or "") in {"source_flow_side_lane", "source_flow_side_lane_tail"}
        for target in targets
    )


def _blank_fill_context_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    if not _blank_fill_plan_has_targets(plan):
        return {}
    targets: list[dict[str, Any]] = []
    for target in plan.get("targets") or []:
        if not isinstance(target, dict):
            continue
        targets.append({
            "target_kind": target.get("target_kind"),
            "target_scope": target.get("target_scope"),
            "source_id": target.get("source_id"),
            "flow_unit_id": target.get("flow_unit_id"),
            "asset_block_id": target.get("asset_block_id"),
            "panel_id": target.get("panel_id"),
            "section_id": target.get("section_id"),
            "column_id": target.get("column_id"),
            "target_block_ids": target.get("target_block_ids") or [],
            "insert_selector": target.get("insert_selector"),
            "insert_position": target.get("insert_position"),
            "blank_bbox_canvas": target.get("blank_bbox_canvas"),
            "side_text_coverage_ratio": target.get("side_text_coverage_ratio"),
            "required_min_side_text_coverage_ratio": target.get("required_min_side_text_coverage_ratio"),
            "coverage_gap": target.get("coverage_gap"),
            "local_word_count": target.get("local_word_count"),
            "required_min_words": target.get("required_min_words"),
            "words_to_add_min": target.get("words_to_add_min"),
            "words_to_add_max": target.get("words_to_add_max"),
            "remaining_safe_words": target.get("remaining_safe_words"),
            "over_readout_budget": target.get("over_readout_budget"),
            "blank_fill_severity": target.get("blank_fill_severity"),
            "visual_salience_score": target.get("visual_salience_score"),
            "visual_salience_level": target.get("visual_salience_level"),
            "visual_salience_rank": target.get("visual_salience_rank"),
            "required_repair_mode": target.get("required_repair_mode"),
            "required_repair_modes": target.get("required_repair_modes") or [],
            "required_repair_reason": target.get("required_repair_reason"),
            "prose_fill_required": target.get("prose_fill_required"),
            "compact_rebalance_required": target.get("compact_rebalance_required"),
            "safe_primary_repair_action": target.get("safe_primary_repair_action"),
            "target_line_count": target.get("target_line_count"),
            "tail_gap_confidence": target.get("tail_gap_confidence"),
            "content_bottom_source": target.get("content_bottom_source"),
            "visible_text_bottom_px": target.get("visible_text_bottom_px"),
            "block_content_bottom_px": target.get("block_content_bottom_px"),
            "side_lane_bottom_px": target.get("side_lane_bottom_px"),
            "side_content_bottom_px": target.get("side_content_bottom_px"),
            "lane_tail_gap_px": target.get("lane_tail_gap_px"),
            "tail_gap_px": target.get("tail_gap_px"),
            "usable_blank_px": target.get("usable_blank_px"),
            "required_blank_fill_gap_px": target.get("required_blank_fill_gap_px"),
            "required_max_gap_px": target.get("required_max_gap_px"),
            "required_co_repair_eligible": target.get("required_co_repair_eligible"),
            "promotion": target.get("promotion"),
            "allowed_filler_block_ids": target.get("allowed_filler_block_ids") or [],
            "allowed_selectors": target.get("allowed_selectors") or [],
            "forbidden_selectors": target.get("forbidden_selectors") or [],
            "preserve_selectors": target.get("preserve_selectors") or [],
            "content_requirements": target.get("content_requirements") or [],
            "primary_repair_action": target.get("primary_repair_action"),
            "required_dom_shape": target.get("required_dom_shape"),
            "preserve_current_visual_size": target.get("preserve_current_visual_size"),
            "active_primary_advisory_repair": target.get("active_primary_advisory_repair"),
            "original_promotion": target.get("original_promotion"),
            "original_blank_fill_severity": target.get("original_blank_fill_severity"),
        })
    explicit_required_keys = {
        _blank_fill_prompt_target_key(target)
        for target in (plan.get("required_targets") if isinstance(plan.get("required_targets"), list) else [])
        if isinstance(target, dict)
    }
    required_targets = (
        [
            target for target in targets
            if isinstance(target, dict) and _blank_fill_prompt_target_key(target) in explicit_required_keys
        ][:12]
        if explicit_required_keys else
        _blank_fill_required_targets({"targets": targets})
    )
    advisory_targets = [
        target for target in targets
        if isinstance(target, dict) and target not in required_targets
    ]
    suppressed_targets = plan.get("suppressed_targets") if isinstance(plan.get("suppressed_targets"), list) else []
    return {
        "version": 1,
        "normalization_version": plan.get("normalization_version") or 1,
        "blank_fill_required": bool(required_targets),
        "context_role": (
            "active_primary_repair"
            if plan.get("active_primary_advisory_repair") else
            "required_repair"
            if required_targets else
            "visual_reference"
        ),
        "visual_reference_only": not bool(required_targets),
        "active_primary_advisory_repair": bool(plan.get("active_primary_advisory_repair")),
        "targets": targets[:12],
        "required_targets": required_targets[:12],
        "advisory_targets": advisory_targets[:12],
        "suppressed_targets": [target for target in suppressed_targets if isinstance(target, dict)][:12],
        "required_target_count": len(required_targets),
        "advisory_target_count": len(advisory_targets),
        "suppressed_target_count": len([target for target in suppressed_targets if isinstance(target, dict)]),
        "instructions": plan.get("instructions") or (
            "Patch only listed required targets and fill blank source/section space with source-backed paper facts."
            if required_targets and not plan.get("active_primary_advisory_repair") else
            "Active primary blank-fill repair: patch these local targets before the next validation pass."
            if plan.get("active_primary_advisory_repair") else
            "Visual reference only: do not add large prose or change global layout for advisory blank-fill targets."
        ),
    }


def _repair_scope_from_blank_fill(blank_fill: dict[str, Any]) -> dict[str, Any]:
    targets = blank_fill.get("targets") if isinstance(blank_fill.get("targets"), list) else []
    if not targets:
        return {}
    target_block_ids: list[str] = []
    allowed_selectors: list[str] = []
    forbidden_selectors: list[str] = []
    preserve_selectors: list[str] = []
    allowed_filler_block_ids: list[str] = []
    target_source_ids: list[str] = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        target_block_ids.extend(_string_list(target.get("target_block_ids")))
        allowed_filler_block_ids.extend(_string_list(target.get("allowed_filler_block_ids")))
        target_block_ids.extend(_string_list(target.get("allowed_filler_block_ids")))
        allowed_selectors.extend(_string_list(target.get("allowed_selectors")))
        forbidden_selectors.extend(_string_list(target.get("forbidden_selectors")))
        preserve_selectors.extend(_string_list(target.get("preserve_selectors")))
        for field in ("flow_unit_id", "asset_block_id", "section_id", "column_id"):
            value = str(target.get(field) or "").strip()
            if value:
                target_block_ids.append(value)
        source_id = str(target.get("source_id") or "").strip()
        if source_id:
            target_source_ids.append(source_id)
            allowed_selectors.append(f'[data-source-id="{source_id}"]')
            allowed_selectors.append(f'[data-layer-id="{source_id}"]')
    return {
        "mode": "blank_fill_repair",
        "target_block_ids": _unique_strings(target_block_ids),
        "target_source_ids": _unique_strings(target_source_ids),
        "allowed_filler_block_ids": _unique_strings(allowed_filler_block_ids),
        "allowed_selectors": _unique_strings(allowed_selectors),
        "forbidden_selectors": _unique_strings(forbidden_selectors + [
            ".poster-columns",
            ".poster-column",
            ".poster-header",
        ]),
        "preserve_selectors": _unique_strings(preserve_selectors + [
            ".poster-columns",
            ".poster-column",
            ".poster-header",
            "[data-source-id]",
            "[data-layer-id]",
        ]),
    }


def _blank_fill_prompt_instruction(blank_fill: dict[str, Any], classification: str) -> str:
    targets = blank_fill.get("targets") if isinstance(blank_fill.get("targets"), list) else []
    required_targets = _blank_fill_required_targets(blank_fill)
    active_primary_repair = bool(blank_fill.get("active_primary_advisory_repair"))
    required_target_keys = {
        _blank_fill_prompt_target_key(target)
        for target in required_targets
        if isinstance(target, dict)
    }
    lines = [
        "- Use source-backed paper facts only: benchmark numbers, mechanism notes, limitations/takeaways, or native rows from the staged context.",
        "- Do not solve blank fill by widening the image only, shrinking global typography, changing `.poster-columns`, changing row allocation, or editing the header.",
    ]
    if active_primary_repair:
        lines.insert(0, "- Active primary blank-fill targets are the current primary blocker for this attempt. Apply a minimal local repair; they are not final validator-required hard gates.")
    elif required_targets:
        lines.insert(0, "- Required blank-fill targets are blocking repairs, not decorative polish. Fix every required target in this attempt.")
    else:
        lines.insert(0, "- Blank-fill targets here are visual-reference-only advisory context. Do not treat advisory gaps as required fixes or turn them into large text stuffing.")
    for index, target in enumerate(targets[:8], start=1):
        if not isinstance(target, dict):
            continue
        target_name = (
            target.get("flow_unit_id")
            or target.get("section_id")
            or target.get("column_id")
            or f"target_{index}"
        )
        selector = target.get("insert_selector") or target_name
        words_min = target.get("words_to_add_min")
        words_max = target.get("words_to_add_max")
        action = target.get("primary_repair_action") or "append_direct_sibling_source_readout"
        safe_action = target.get("safe_primary_repair_action") or action
        source_id = target.get("source_id") or ""
        remaining_safe = target.get("remaining_safe_words")
        over_budget = bool(target.get("over_readout_budget"))
        compact_rebalance = bool(target.get("compact_rebalance_required"))
        prose_fill = target.get("prose_fill_required")
        repair_mode = str(target.get("required_repair_mode") or "").strip()
        repair_modes = target.get("required_repair_modes") if isinstance(target.get("required_repair_modes"), list) else []
        preserve = (
            "preserve current source visual size; "
            if target.get("target_kind") in {"source_flow_side_lane", "source_flow_side_lane_tail"} else
            ""
        )
        promotion = str(target.get("promotion") or ("required" if target in required_targets else "advisory"))
        confidence = str(target.get("tail_gap_confidence") or "")
        source = str(target.get("content_bottom_source") or "")
        target_required = _blank_fill_prompt_target_key(target) in required_target_keys
        label = "required" if target_required else promotion
        if active_primary_repair and target_required:
            lines.append(
                f"- Target {index} (active primary repair): `{target_name}`"
                + (f" source_id={source_id}" if source_id else "")
                + ("; over_readout_budget=true" if over_budget else "")
                + ("; compact_rebalance_required=true" if compact_rebalance else "")
                + (f"; remaining_safe_words={remaining_safe}" if remaining_safe not in (None, "") else "")
                + f"; action={safe_action}. Patch only `{selector}` or the listed local section/source-flow unit. "
                "Prefer native rows/chips, compact local readout, stacked asset/readout, or reducing unused local height; "
                "do not change `.poster-columns`, global row allocation, section ordering, the header, or global typography."
                + (
                    f" Do not add {words_min}-{words_max} prose words unless prose_fill_required=true and the staged context has safe source-backed facts."
                    if words_min not in (None, "") or words_max not in (None, "") else
                    " Do not add a large paragraph."
                )
            )
            continue
        if not target_required:
            lines.append(
                f"- Target {index} ({label or 'advisory'} visual reference only): `{target_name}`"
                + (f" source_id={source_id}" if source_id else "")
                + ("; over_readout_budget=true" if over_budget else "")
                + ("; compact_rebalance_required=true" if compact_rebalance else "")
                + (f"; remaining_safe_words={remaining_safe}" if remaining_safe not in (None, "") else "")
                + "; advisory only. Do not add "
                + (
                    f"{words_min}-{words_max} prose words"
                    if words_min not in (None, "") or words_max not in (None, "")
                    else "new prose"
                )
                + " or broaden layout scope for this target. Do not change `.poster-columns`, global row allocation, "
                "or section ordering for this advisory reference. If you are already editing this exact unit for a required repair, "
                f"prefer tiny local compaction/rebalance via action={safe_action}; native rows/chips, stacked asset/readout, "
                "or reducing unused local section height are preferred over prose."
            )
            continue
        if compact_rebalance or prose_fill is False or any(
            marker in repair_mode for marker in ("compact", "rebalance", "stack", "reduce")
        ):
            modes_text = ", ".join(str(mode) for mode in repair_modes if str(mode).strip()) or safe_action
            lines.append(
                f"- Target {index} ({label}): `{target_name}`"
                + (f" source_id={source_id}" if source_id else "")
                + f"; required_repair_mode={repair_mode or 'compact/rebalance'}; "
                f"Do not add {words_min}-{words_max} prose words. Use compact local repair: {modes_text}. "
                f"Patch only `{selector}` or the listed target section/source-flow unit; preserve ids and current source visuals."
                + (f" remaining_safe_words={remaining_safe}" if remaining_safe not in (None, "") else "")
                + (f" confidence={confidence}" if confidence else "")
                + (f" content_bottom_source={source}" if source else "")
            )
            continue
        if action == "redistribute_row_height_to_source_section":
            lines.append(
                f"- Target {index} ({label}): `{target_name}`; action={action}; "
                "do not add a large paragraph to the last section. Rebalance row height toward a real source/evidence section."
                + (f" confidence={confidence}" if confidence else "")
                + (f" content_bottom_source={source}" if source else "")
                + "."
            )
            continue
        if over_budget:
            lines.append(
                f"- Target {index} ({label}): `{target_name}`"
                + (f" source_id={source_id}" if source_id else "")
                + f"; over_readout_budget=true; remaining_safe_words={remaining_safe}. "
                f"Do not add {words_min}-{words_max} prose words. This may still be a required repair, but the mode is compact/rebalance: "
                f"use action={safe_action}, compact existing readout into native rows, stack asset and readout, "
                "or locally reduce unused flow-unit/section height without changing global columns."
            )
            continue
        lines.append(
            f"- Target {index} ({label}): `{target_name}`"
            + (f" source_id={source_id}" if source_id else "")
            + f"; action={action}; {preserve}insert {words_min}-{words_max} source-backed words at `{selector}`."
            + (f" remaining_safe_words={remaining_safe}" if remaining_safe not in (None, "") else "")
            + (f" confidence={confidence}" if confidence else "")
            + (f" content_bottom_source={source}" if source else "")
        )
        if target.get("allowed_filler_block_ids"):
            lines.append(
                f"  Allowed filler blocks may be moved here only with id/text preserved: {target.get('allowed_filler_block_ids')}"
            )
    if classification != "blank_fill_repair" and required_targets:
        lines.append("- Complete these blank-fill co-repairs together with the primary blocker; do not defer them to another attempt.")
    if not active_primary_repair and any(
        isinstance(target, dict)
        and str(target.get("promotion") or "") == "advisory"
        and not target.get("active_primary_advisory_repair")
        for target in targets
    ):
        lines.append("- Advisory blank-fill targets are visual reference only during small typography/line-height repairs; do not add large text or make global layout edits solely for advisory targets.")
    return "\n".join(lines)


def _blank_fill_prompt_target_key(target: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(target.get("target_kind") or ""),
        str(target.get("flow_unit_id") or target.get("section_id") or target.get("column_id") or ""),
        str(target.get("source_id") or target.get("asset_block_id") or ""),
    )


def _merge_repair_scopes(*scopes: dict[str, Any]) -> dict[str, Any]:
    valid = [scope for scope in scopes if isinstance(scope, dict) and scope]
    if not valid:
        return {}
    merged = dict(valid[0])
    for scope in valid[1:]:
        for key in (
            "target_block_ids",
            "target_source_ids",
            "allowed_filler_block_ids",
            "allowed_selectors",
            "forbidden_selectors",
            "preserve_selectors",
        ):
            merged[key] = _unique_strings(_string_list(merged.get(key)) + _string_list(scope.get(key)))
        if scope.get("mode") == "blank_fill_repair":
            merged["mode"] = "blank_fill_repair" if not merged.get("mode") else f"{merged.get('mode')}+blank_fill"
    return merged


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _row_allocation_context_from_issues(issues: list[Any], reasons: Any = None) -> dict[str, Any]:
    containers: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []
    column_budget_map: dict[str, dict[str, Any]] = {}
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        bottom_overflow = _scroll_bottom_overflow_px(issue)
        section_id = issue.get("section_id")
        column_id = str(issue.get("column_id") or "")
        containers.append({
            "container_id": issue.get("container_id"),
            "client_height_px": issue.get("container_client_height_px"),
            "scroll_height_px": issue.get("container_scroll_height_px"),
            "scroll_to_client_ratio": issue.get("scroll_to_client_ratio"),
            "column_id": column_id,
        })
        sections.append({
            "section_id": section_id,
            "section_height_px": issue.get("section_height_px"),
            "column_height_px": issue.get("column_height_px"),
            "scroll_overflow_px": issue.get("scroll_overflow_px"),
        })
        target = {
            "container_kind": issue.get("container_kind"),
            "container_id": issue.get("container_id"),
            "section_id": section_id,
            "column_id": column_id,
            "flow_unit_id": issue.get("flow_unit_id"),
            "overflow_block_id": issue.get("overflow_block_id"),
            "client_height_px": issue.get("client_height_px") or issue.get("container_client_height_px"),
            "scroll_height_px": issue.get("scroll_height_px") or issue.get("container_scroll_height_px"),
            "bottom_overflow_px": bottom_overflow,
            "required_clearance_px": bottom_overflow,
            "target_max_bottom_overflow_px": 3,
            "scroll_overflow_px": issue.get("scroll_overflow_px"),
            "container_bbox": issue.get("container_bbox"),
            "section_bbox": issue.get("section_bbox"),
            "container_bottom_px": issue.get("container_bottom_px"),
            "content_bottom_px": issue.get("content_bottom_px"),
            "scroll_to_client_ratio": issue.get("scroll_to_client_ratio"),
            "allowed_actions": [
                "rebalance section row heights across the affected column",
                "shorten low-value local prose/readouts",
                "move or merge optional secondary flow content within the same column",
                "tighten local gaps/padding after row allocation is corrected",
            ],
        }
        targets.append({key: value for key, value in target.items() if value not in (None, "", {}, [])})
        if column_id:
            budget = column_budget_map.setdefault(column_id, {
                "column_id": column_id,
                "overflowing_sections": [],
                "total_bottom_overflow_px": 0,
                "max_bottom_overflow_px": 0,
            })
            if section_id and section_id not in budget["overflowing_sections"]:
                budget["overflowing_sections"].append(section_id)
            budget["total_bottom_overflow_px"] += bottom_overflow
            budget["max_bottom_overflow_px"] = max(budget["max_bottom_overflow_px"], bottom_overflow)
            if issue.get("column_height_px") not in (None, ""):
                budget["column_height_px"] = issue.get("column_height_px")
    return {
        "containers": [item for item in containers if any(value not in (None, "", {}) for value in item.values())],
        "sections": [item for item in sections if any(value not in (None, "", {}) for value in item.values())],
        "overflow_targets": _ranked_overflow_targets(targets),
        "column_budgets": sorted(
            column_budget_map.values(),
            key=lambda item: _safe_int(item.get("max_bottom_overflow_px"), default=0),
            reverse=True,
        ),
        "reasons": reasons if isinstance(reasons, list) else [],
    }


def _ranked_overflow_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(
        targets,
        key=lambda item: _safe_int(item.get("bottom_overflow_px"), default=0),
        reverse=True,
    )
    result: list[dict[str, Any]] = []
    for index, target in enumerate(ranked[:12], start=1):
        item = dict(target)
        item["rank"] = index
        result.append(item)
    return result


def _global_overflow_repair_plan_from_row_allocation(row_allocation: dict[str, Any]) -> dict[str, Any]:
    targets = row_allocation.get("overflow_targets") if isinstance(row_allocation, dict) else []
    targets = targets if isinstance(targets, list) else []
    max_bottom = 0
    for target in targets:
        if isinstance(target, dict):
            max_bottom = max(max_bottom, _safe_int(target.get("bottom_overflow_px"), default=0))
    return {
        "must_clear_all_scroll_overflow": True,
        "acceptance": {
            "max_bottom_overflow_px": 3,
            "no_residual_overflow_gt_32_px": True,
            "all_targets_must_fit": True,
            "do_not_stop_after_shell_css_only": True,
            "done_marker_required_after_all_targets_addressed": True,
        },
        "previous_max_bottom_overflow_px": max_bottom,
        "issue_count_total": len(targets),
        "targets_sorted_by_severity": targets,
        "column_budgets": row_allocation.get("column_budgets") if isinstance(row_allocation, dict) else [],
        "repair_instruction": (
            "This is a whole-poster overflow clearance task. Do not repair only local_repair_hint. "
            "Clear every target in targets_sorted_by_severity; grid shell fixes are necessary but "
            "insufficient when section content demand still exceeds allocated row height."
        ),
    }


def _scroll_bottom_overflow_px(issue: dict[str, Any]) -> int:
    scroll = issue.get("scroll_overflow_px") if isinstance(issue.get("scroll_overflow_px"), dict) else {}
    return _safe_int(issue.get("bottom_overflow_px"), default=_safe_int(scroll.get("bottom"), default=0))


def _source_wrap_context_from_issues(issues: list[Any]) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    filtered_identity_header_targets: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        if _source_wrap_issue_is_identity_header_target(issue):
            filtered_identity_header_targets.append({
                "source_id": issue.get("source_id"),
                "block_id": issue.get("block_id"),
                "panel_id": issue.get("panel_id"),
                "panel_role": issue.get("panel_role"),
                "actual_wrapper_label": issue.get("actual_wrapper_label"),
            })
            continue
        targets.append({
            "source_id": issue.get("source_id"),
            "block_id": issue.get("block_id"),
            "panel_id": issue.get("panel_id"),
            "panel_role": issue.get("panel_role"),
            "failure_kind": issue.get("failure_kind"),
            "actual_wrapper_label": issue.get("actual_wrapper_label"),
            "actual_parent_label": issue.get("actual_parent_label"),
            "flow_unit": issue.get("flow_unit"),
            "reason": issue.get("reason"),
            "expected_parent": issue.get("expected_parent") or "panel-root direct child",
            "required_dom_shape": (
                issue.get("required_dom_shape")
                or "direct-child section.figure-flow-unit containing one flow-asset and direct readout text"
            ),
            "local_words": issue.get("local_words"),
            "flow_unit_violations": issue.get("flow_unit_violations") or [],
            "flow_unit_violation_details": issue.get("flow_unit_violation_details") or [],
            "separate_layout_evidence": issue.get("separate_layout_evidence") or [],
            "visible_figcaption": issue.get("visible_figcaption"),
        })
    return {
        "required_dom_shape": (
            "Each source figure/table must be in its own direct-child .figure-flow-unit/.source-flow-unit "
            "with the visual and local source-backed readout in the same DOM flow."
        ),
        "targets": targets[:8],
        "filtered_identity_header_targets": filtered_identity_header_targets[:8],
    }


def _source_wrap_issue_is_identity_header_target(issue: dict[str, Any]) -> bool:
    panel_role = str(issue.get("panel_role") or "").strip().lower().replace("-", "_").replace(" ", "_")
    panel_id = str(issue.get("panel_id") or "").strip().lower()
    if panel_role != "identity_header" and panel_id not in {"identity_header", "title_meta"}:
        return False
    blob = " ".join(
        str(issue.get(key) or "")
        for key in (
            "source_id",
            "block_id",
            "panel_id",
            "panel_role",
            "actual_wrapper_label",
            "actual_parent_label",
        )
    ).lower().replace("-", "_")
    return any(token in blob for token in ("identity", "logo", "badge", "venue", "conference", "arxiv", "institution"))


def _source_visual_sizing_context_from_issues(issues: list[Any]) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        repair_required = _source_visual_target_is_required(issue)
        targets.append({
            "failure_kind": issue.get("failure_kind"),
            "source_id": issue.get("source_id"),
            "block_id": issue.get("block_id"),
            "panel_id": issue.get("panel_id"),
            "actual_wrapper_label": issue.get("actual_wrapper_label"),
            "target_problem": issue.get("target_problem"),
            "source_visual_required": repair_required,
            "context_role": "required_repair" if repair_required else "visual_reference",
            "visual_reference_only": not repair_required,
            "repair_intent": issue.get("repair_intent"),
            "primary_repair_action": issue.get("primary_repair_action"),
            "recommended_first_action": issue.get("recommended_first_action"),
            "acceptance_mode": issue.get("acceptance_mode"),
            "target_scope": issue.get("target_scope"),
            "preserve_current_visual_size": issue.get("preserve_current_visual_size"),
            "required_dom_shape": issue.get("required_dom_shape"),
            "same_flow_fill_required": issue.get("same_flow_fill_required"),
            "same_flow_fill_metrics": issue.get("same_flow_fill_metrics"),
            "same_flow_fill_targets": issue.get("same_flow_fill_targets"),
            "must_not_regress_geometry": issue.get("must_not_regress_geometry"),
            "readable_visual_geometry": issue.get("readable_visual_geometry"),
            "target_block_ids": issue.get("target_block_ids") or [],
            "allowed_selectors": issue.get("allowed_selectors") or [],
            "forbidden_selectors": issue.get("forbidden_selectors") or [],
            "allowed_filler_block_ids": issue.get("allowed_filler_block_ids") or [],
            "threshold_gap": issue.get("threshold_gap"),
            "threshold_gap_is_minor": issue.get("threshold_gap_is_minor"),
            "do_not_fix_by": issue.get("do_not_fix_by") or [],
            "source_width_px": issue.get("source_width_px"),
            "source_height_px": issue.get("source_height_px"),
            "panel_width_px": issue.get("panel_width_px"),
            "panel_height_px": issue.get("panel_height_px"),
            "panel_client_height_px": issue.get("panel_client_height_px"),
            "panel_scroll_height_px": issue.get("panel_scroll_height_px"),
            "panel_scroll_overflow_px": issue.get("panel_scroll_overflow_px"),
            "source_panel_width_ratio": issue.get("source_panel_width_ratio"),
            "source_panel_area_ratio": issue.get("source_panel_area_ratio"),
            "required_panel_width_ratio": issue.get("required_panel_width_ratio"),
            "required_source_height_px": issue.get("required_source_height_px"),
            "required_source_area_ratio": issue.get("required_source_area_ratio"),
            "required_object_fit_fill_ratio": issue.get("required_object_fit_fill_ratio"),
            "required_object_fit_area_ratio": issue.get("required_object_fit_area_ratio"),
            "height_px_reference": issue.get("required_source_height_px"),
            "height_px_reference_is_adaptive": bool(issue.get("required_source_height_px") not in (None, "", [], {})),
            "intrinsic_aspect_ratio": issue.get("intrinsic_aspect_ratio"),
            "wrapper_aspect_ratio": issue.get("wrapper_aspect_ratio"),
            "object_fit_rendered_width_px": issue.get("object_fit_rendered_width_px"),
            "object_fit_rendered_height_px": issue.get("object_fit_rendered_height_px"),
            "object_fit_width_fill_ratio": issue.get("object_fit_width_fill_ratio"),
            "object_fit_height_fill_ratio": issue.get("object_fit_height_fill_ratio"),
            "object_fit_area_fill_ratio": issue.get("object_fit_area_fill_ratio"),
            "suggested_match_aspect_width_px": issue.get("suggested_match_aspect_width_px"),
            "suggested_match_aspect_height_px": issue.get("suggested_match_aspect_height_px"),
            "flow_unit_id": issue.get("flow_unit_id"),
            "asset_block_id": issue.get("asset_block_id"),
            "flow_unit_width_px": issue.get("flow_unit_width_px"),
            "flow_unit_height_px": issue.get("flow_unit_height_px"),
            "source_flow_width_ratio": issue.get("source_flow_width_ratio"),
            "side_text_coverage_ratio": issue.get("side_text_coverage_ratio"),
            "required_min_side_text_coverage_ratio": issue.get("required_min_side_text_coverage_ratio"),
            "local_word_count": issue.get("local_word_count"),
            "required_min_words": issue.get("required_min_words"),
            "blank_sidecar_height_ratio": issue.get("blank_sidecar_height_ratio"),
            "recommended_layout": issue.get("recommended_layout"),
            "repair": issue.get("repair"),
            "reasons": issue.get("reasons") or [],
            "classes": issue.get("classes") or [],
        })
    targets = targets[:8]
    required_targets = [target for target in targets if target.get("source_visual_required")]
    advisory_targets = [target for target in targets if not target.get("source_visual_required")]
    return {
        "targets": targets,
        "required_targets": required_targets,
        "advisory_targets": advisory_targets,
        "required_target_count": len(required_targets),
        "advisory_target_count": len(advisory_targets),
        "context_role": "required_repair" if required_targets else "visual_reference",
        "visual_reference_only": not bool(required_targets),
        "instructions": (
            "Patch every required source-visual target locally; advisory targets are visual-reference polish only."
            if required_targets else
            "Visual reference only: do not broaden layout scope or add retries solely for advisory source-visual polish."
        ),
    }


def _blank_fill_plan_from_source_visual_sizing(source_visual_sizing: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(source_visual_sizing, dict):
        return {}
    source_targets = (
        source_visual_sizing.get("required_targets")
        if isinstance(source_visual_sizing.get("required_targets"), list) else
        []
    )
    targets: list[dict[str, Any]] = []
    for target in source_targets:
        if not isinstance(target, dict):
            continue
        if target.get("source_visual_required") is not True:
            continue
        if str(target.get("acceptance_mode") or "") != "same_flow_fill":
            continue
        target_problem = str(target.get("target_problem") or "")
        failure_kind = str(target.get("failure_kind") or "")
        if target_problem != "readable_visual_flow_underfilled" and failure_kind not in {
            "source_visual_flow_underfilled",
            "source_visual_sidecar_underfilled",
        }:
            continue
        same_flow_targets = target.get("same_flow_fill_targets") if isinstance(target.get("same_flow_fill_targets"), dict) else {}
        same_flow_metrics = target.get("same_flow_fill_metrics") if isinstance(target.get("same_flow_fill_metrics"), dict) else {}
        flow_unit_id = str(target.get("flow_unit_id") or same_flow_metrics.get("flow_unit_id") or "").strip()
        panel_id = str(target.get("panel_id") or "").strip()
        asset_block_id = str(target.get("asset_block_id") or target.get("block_id") or "").strip()
        local_words = _safe_int(
            same_flow_targets.get("local_word_count")
            if same_flow_targets.get("local_word_count") not in (None, "", [], {})
            else target.get("local_word_count"),
            default=0,
        )
        required_words = _safe_int(
            same_flow_targets.get("required_min_words")
            if same_flow_targets.get("required_min_words") not in (None, "", [], {})
            else target.get("required_min_words"),
            default=18,
        )
        coverage = _safe_float(
            same_flow_targets.get("side_text_coverage_ratio")
            if same_flow_targets.get("side_text_coverage_ratio") not in (None, "", [], {})
            else target.get("side_text_coverage_ratio"),
            default=0.0,
        )
        required_coverage = _safe_float(
            same_flow_targets.get("required_min_side_text_coverage_ratio")
            if same_flow_targets.get("required_min_side_text_coverage_ratio") not in (None, "", [], {})
            else target.get("required_min_side_text_coverage_ratio"),
            default=0.3,
        )
        words_min = max(8, min(32, max(required_words - local_words, 8)))
        remaining_safe = max(0, 140 - local_words - 8)
        over_budget = remaining_safe < 8 or words_min > remaining_safe
        target_scope = str(target.get("target_scope") or "")
        if not target_scope:
            target_scope = "existing_source_flow_unit" if flow_unit_id else "create_direct_child_source_flow_unit"
        insert_selector = (
            f'[data-block-id="{flow_unit_id}"]'
            if flow_unit_id else
            f'[data-block-id="{panel_id}"]'
            if panel_id else
            None
        )
        blank_target = {
            "target_kind": "source_flow_side_lane",
            "target_scope": target_scope,
            "source_id": target.get("source_id"),
            "flow_unit_id": flow_unit_id,
            "asset_block_id": asset_block_id,
            "panel_id": panel_id,
            "section_id": panel_id,
            "target_block_ids": _unique_strings([
                value for value in (flow_unit_id, asset_block_id, panel_id, str(target.get("source_id") or "")) if value
            ]),
            "insert_selector": insert_selector,
            "insert_position": "append_direct_child" if flow_unit_id else "create_direct_child_source_flow_unit",
            "side_text_coverage_ratio": coverage,
            "required_min_side_text_coverage_ratio": required_coverage,
            "coverage_gap": max(0.0, required_coverage - coverage),
            "local_word_count": local_words,
            "required_min_words": required_words,
            "words_to_add_min": words_min,
            "words_to_add_max": words_min + 12,
            "remaining_safe_words": remaining_safe,
            "safe_word_budget": remaining_safe,
            "over_readout_budget": over_budget,
            "required_repair_mode": (
                "compact_rebalance_source_flow"
                if over_budget else
                "create_direct_child_source_flow_unit_and_fill_blank_lane"
                if target_scope == "create_direct_child_source_flow_unit" else
                "prose_or_native_flow_fill"
            ),
            "required_repair_modes": (
                ["compact_existing_readout", "rebalance_native_rows", "stack_asset_and_readout", "reduce_flow_unit_or_section_height"]
                if over_budget else
                ["create_direct_child_source_flow_unit_and_fill_blank_lane", "append_direct_sibling_source_readout", "add_native_metric_rows"]
            ),
            "prose_fill_required": not over_budget,
            "compact_rebalance_required": over_budget,
            "allowed_filler_block_ids": target.get("allowed_filler_block_ids") or [],
            "allowed_selectors": target.get("allowed_selectors") or [],
            "forbidden_selectors": target.get("forbidden_selectors") or [],
            "preserve_selectors": [
                selector for selector in (
                    f'[data-block-id="{flow_unit_id}"]' if flow_unit_id else "",
                    f'[data-block-id="{asset_block_id}"]' if asset_block_id else "",
                    f'[data-block-id="{panel_id}"]' if panel_id else "",
                ) if selector
            ],
            "primary_repair_action": (
                "compact_existing_readout_rebalance_native_rows_or_stack_asset_and_readout"
                if over_budget else
                "create_direct_child_source_flow_unit_and_fill_blank_lane"
                if target_scope == "create_direct_child_source_flow_unit" else
                "append_direct_sibling_source_readout_or_move_allowed_filler_into_flow_unit"
            ),
            "safe_primary_repair_action": (
                "compact_existing_readout_rebalance_native_rows_or_reduce_flow_unit_height"
                if over_budget else
                "append_direct_sibling_source_readout"
            ),
            "required_co_repair_eligible": True,
            "promotion": "required",
            "blank_fill_severity": "required",
            "preserve_current_visual_size": True,
            "required_dom_shape": target.get("required_dom_shape"),
            "content_requirements": [
                "Use paper facts, compact comparison table rows, mechanism notes, or benchmark context.",
                "Keep the content as direct sibling readout/native rows inside the same source-flow unit.",
            ],
        }
        targets.append(blank_target)
    if not targets:
        return {}
    return {
        "version": 1,
        "normalization_version": 1,
        "blank_fill_required": True,
        "targets": targets[:12],
        "required_targets": targets[:12],
        "required_target_count": len(targets),
        "instructions": (
            "Required source-visual same-flow fill synthesized from source_visual_sizing targets. "
            "Preserve the readable visual size and patch only the listed local flow units/panels."
        ),
    }


def _source_visual_target_is_required(issue: dict[str, Any]) -> bool:
    if issue.get("blocks_soft_accept") is True:
        return True
    if issue.get("soft_finalizable") is False:
        return True
    severity = str(issue.get("severity") or "").strip().lower()
    if severity in {"hard", "required", "blocking", "error"}:
        return True
    if issue.get("soft_finalizable") is True and severity in {"", "advisory", "near_miss", "polish"}:
        return False
    target_problem = str(issue.get("target_problem") or "")
    failure_kind = str(issue.get("failure_kind") or "")
    if target_problem == "readable_visual_wrapper_polish" and severity in {"advisory", "polish"}:
        return False
    if (target_problem == "minor_geometry_gap" or failure_kind == "minor_geometry_gap") and issue.get("threshold_gap_is_minor") is True:
        return False
    return severity not in {"advisory", "polish"}


def _repair_scope_from_source_visual_sizing(source_visual_sizing: dict[str, Any]) -> dict[str, Any]:
    required_targets = (
        source_visual_sizing.get("required_targets")
        if isinstance(source_visual_sizing.get("required_targets"), list) else
        []
    )
    targets = required_targets or (
        source_visual_sizing.get("targets") if isinstance(source_visual_sizing.get("targets"), list) else []
    )
    target_block_ids: list[str] = []
    allowed_selectors: list[str] = []
    forbidden_selectors: list[str] = []
    target_source_ids: list[str] = []
    allowed_filler_block_ids: list[str] = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        target_block_ids.extend(_string_list(target.get("target_block_ids")))
        allowed_filler_block_ids.extend(_string_list(target.get("allowed_filler_block_ids")))
        target_block_ids.extend(_string_list(target.get("allowed_filler_block_ids")))
        allowed_selectors.extend(_string_list(target.get("allowed_selectors")))
        forbidden_selectors.extend(_string_list(target.get("forbidden_selectors")))
        for field in ("source_id", "panel_id", "flow_unit_id", "asset_block_id", "block_id"):
            value = str(target.get(field) or "").strip()
            if not value:
                continue
            if field == "source_id":
                target_source_ids.append(value)
                allowed_selectors.append(f'[data-source-id="{value}"]')
                allowed_selectors.append(f'[data-layer-id="{value}"]')
            else:
                target_block_ids.append(value)
    target_block_ids = _unique_strings(target_block_ids)
    target_source_ids = _unique_strings(target_source_ids)
    if not target_block_ids and not target_source_ids:
        return {}
    return {
        "mode": "source_visual_flow_fill",
        "target_block_ids": target_block_ids,
        "target_source_ids": target_source_ids,
        "allowed_filler_block_ids": _unique_strings(allowed_filler_block_ids),
        "allowed_selectors": _unique_strings(allowed_selectors),
        "forbidden_selectors": _unique_strings(forbidden_selectors),
        "preserve_selectors": [
            ".poster-columns",
            ".poster-column",
            ".poster-header",
            "[data-source-id]",
        ],
    }


def _typography_context_from_feedback(
    ctx: ToolContext,
    summary: dict[str, Any],
    payload: dict[str, Any],
    issues: list[Any],
) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for key in (
        "serif_family_ratio",
        "font_family_match_ratio",
        "expected_font_family",
        "expected_font_family_category",
        "font_size_levels",
        "times_new_roman_family_ratio",
        "expected_font_sizes_px",
        "font_size_tolerance_px",
        "heading_weight_median",
        "body_weight_median",
        "italic_body_ratio",
        "title_size_median",
        "section_heading_size_median",
        "body_size_median",
        "readout_size_median",
        "table_text_size_median",
        "caption_label_size_median",
        "caption_size_median",
        "label_size_median",
        "title_weight_median",
        "title_weight_min",
        "title_weight_max",
        "section_heading_weight_median",
        "section_heading_weight_min",
    ):
        value = summary.get(key) if summary.get(key) not in (None, "") else payload.get(key)
        if value not in (None, ""):
            metrics[key] = value
    targets: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        targets.append({
            "failure_kind": issue.get("failure_kind"),
            "role": issue.get("role"),
            "block_id": issue.get("block_id"),
            "actual_font_family": issue.get("actual_font_family"),
            "actual_font_size_px": issue.get("actual_font_size_px"),
            "actual_font_weight": issue.get("actual_font_weight"),
            "actual_line_height": issue.get("actual_line_height"),
            "expected": issue.get("expected"),
            "sample_text": issue.get("sample_text"),
        })
    return {
        "metrics": metrics,
        "targets": targets[:8],
        "required_system": _typography_required_system(ctx),
    }


def _heading_flow_context_from_issues(issues: list[Any]) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        targets.append({
            "failure_kind": issue.get("failure_kind"),
            "role": issue.get("role"),
            "block_id": issue.get("block_id"),
            "container_id": issue.get("container_id"),
            "client_width_px": issue.get("client_width_px"),
            "scroll_width_px": issue.get("scroll_width_px"),
            "client_height_px": issue.get("client_height_px"),
            "scroll_height_px": issue.get("scroll_height_px"),
            "bottom_overflow_px": issue.get("bottom_overflow_px"),
            "width_overflow_px": issue.get("width_overflow_px"),
            "scroll_overflow_px": issue.get("scroll_overflow_px"),
            "bbox": issue.get("bbox"),
            "actual_font_size_px": issue.get("actual_font_size_px"),
            "actual_font_weight": issue.get("actual_font_weight"),
            "actual_line_height": issue.get("actual_line_height"),
            "sample_text": issue.get("sample_text"),
            "expected": issue.get("expected"),
            "repair": issue.get("repair"),
            "repair_scope": issue.get("repair_scope"),
            "target_block_ids": issue.get("target_block_ids") or [],
            "target_selectors": issue.get("target_selectors") or [],
            "allowed_selectors": issue.get("allowed_selectors") or [],
            "forbidden_selectors": issue.get("forbidden_selectors") or [],
            "preserve_selectors": issue.get("preserve_selectors") or [],
        })
    return {
        "targets": targets[:8],
        "repair_order": [
            "compact identity/header rows locally",
            "reduce only title/heading font-size, line-height, or padding when needed",
            "allocate a little more header/heading lane height from the body grid if local compacting is insufficient",
        ],
    }


def _local_flow_context_from_issues(issues: list[Any]) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        targets.append(_local_flow_target_from_issue(issue))
    return {"targets": targets[:8]}


def _identity_header_context_from_issues(issues: list[Any]) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        targets.append({
            "id": issue.get("id"),
            "header_id": issue.get("header_id"),
            "block_id": issue.get("block_id"),
            "role": issue.get("role"),
            "word_count": issue.get("word_count"),
            "text": issue.get("text"),
            "repair": issue.get("repair"),
        })
    return {
        "targets": targets[:8],
        "allowed_header_content": [
            "paper title",
            "authors",
            "school/institution/company names",
        ],
        "forbidden_header_content": [
            "fourth header/meta/subtitle row or side identity rail",
            "summary/tagline/thesis/readout copy",
            "method/result/takeaway claims",
            "method/topic/contribution/takeaway badges",
            "logos, image badges, icons, or QR codes",
            "venue/conference/arXiv/archive metadata",
            "citation/contact text",
            "project/code/resource links",
            "explanatory captions under identity labels",
            "paper source figures/tables used as body evidence",
        ],
    }


def _section_content_context_from_issues(issues: list[Any]) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        targets.append(_local_flow_target_from_issue(issue))
    return {"targets": targets[:8]}


def _local_flow_target_from_issue(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "container_id": issue.get("container_id"),
        "container_kind": issue.get("container_kind"),
        "section_id": issue.get("section_id"),
        "column_id": issue.get("column_id"),
        "flow_unit_id": issue.get("flow_unit_id"),
        "overflow_block_id": issue.get("overflow_block_id"),
        "client_height_px": issue.get("client_height_px") or issue.get("container_client_height_px"),
        "scroll_height_px": issue.get("scroll_height_px") or issue.get("container_scroll_height_px"),
        "bottom_overflow_px": issue.get("bottom_overflow_px"),
        "scroll_overflow_px": issue.get("scroll_overflow_px"),
        "scroll_to_client_ratio": issue.get("scroll_to_client_ratio"),
        "container_bbox": issue.get("container_bbox"),
        "section_bbox": issue.get("section_bbox"),
        "same_column_sibling_section_ids": issue.get("same_column_sibling_section_ids") or [],
        "sample_text": issue.get("sample_text"),
        "repair": issue.get("repair"),
        "repair_scope": issue.get("repair_scope"),
        "target_block_ids": issue.get("target_block_ids") or [],
        "target_selectors": issue.get("target_selectors") or [],
        "allowed_selectors": issue.get("allowed_selectors") or [],
        "forbidden_selectors": issue.get("forbidden_selectors") or [],
        "preserve_selectors": issue.get("preserve_selectors") or [],
    }


def _post_composite_context_from_feedback(summary: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    findings = summary.get("blocking_findings") or payload.get("blocking_findings") or payload.get("issues") or []
    context = {
        "feedback_tool": summary.get("feedback_tool") or payload.get("feedback_tool"),
        "blocking_findings": findings[:8] if isinstance(findings, list) else [],
        "design_feedback_relative_path": payload.get("design_feedback_relative_path"),
    }
    if context.get("feedback_tool") == "critic":
        for key in ("critic_verdict", "critic_score", "critic_summary", "dimension_scores", "review_coverage"):
            value = summary.get(key) if summary.get(key) not in (None, "") else payload.get(key)
            if value not in (None, ""):
                context[key] = copy.deepcopy(value)
    return context


def _density_conservation_context_from_feedback(
    summary: dict[str, Any],
    payload: dict[str, Any],
    issues: list[Any],
) -> dict[str, Any]:
    density = summary.get("density_conservation") or payload.get("density_conservation") or {}
    return {
        **(density if isinstance(density, dict) else {}),
        "targets": issues[:8],
    }


def _local_repair_scope_context_from_feedback(summary: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    violations = summary.get("violations") or payload.get("violations") or []
    original = payload.get("original_repair_context") if isinstance(payload.get("original_repair_context"), dict) else {}
    return {
        "violations": violations[:12] if isinstance(violations, list) else [],
        "allowed_selectors": payload.get("allowed_selectors") or summary.get("allowed_selectors") or [],
        "forbidden_selectors": payload.get("forbidden_selectors") or summary.get("forbidden_selectors") or [],
        "target_block_ids": payload.get("target_block_ids") or summary.get("target_block_ids") or [],
        "original_repair_context": original,
    }


def _lower_band_fill_context(visual_fill_feedback: dict[str, Any]) -> dict[str, Any]:
    for path in (
        ("candidate", "fill_metrics"),
        ("candidate", "fill_issues"),
        ("fill_metrics",),
        ("fill_issues",),
    ):
        value: Any = visual_fill_feedback
        for key in path:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                value = None
                break
        if isinstance(value, dict):
            return {
                key: value.get(key)
                for key in (
                    "content_bottom_ratio",
                    "lower_quarter_content_coverage",
                    "lower_half_content_coverage",
                    "middle_lower_content_coverage",
                )
                if value.get(key) not in (None, "")
            }
        if isinstance(value, list):
            for issue in value:
                if isinstance(issue, dict):
                    metrics = {
                        key: issue.get(key)
                        for key in (
                            "content_bottom_ratio",
                            "lower_quarter_content_coverage",
                            "lower_half_content_coverage",
                            "middle_lower_content_coverage",
                        )
                        if issue.get(key) not in (None, "")
                    }
                    if metrics:
                        return metrics
    return {}


def _has_severe_fill_feedback(visual_fill_feedback: dict[str, Any]) -> bool:
    candidate = visual_fill_feedback.get("candidate") if isinstance(visual_fill_feedback, dict) else {}
    if isinstance(candidate, dict) and candidate.get("severe_fill_issues"):
        return True
    return bool(visual_fill_feedback.get("severe_fill_issues")) if isinstance(visual_fill_feedback, dict) else False


def _noop_repair_feedback(
    *,
    previous_feedback: dict[str, Any],
    attempt_index: int,
    attempt_dir: Path,
) -> dict[str, Any]:
    active_feedback = _active_feedback_for_repair(previous_feedback)
    previous_summary = active_feedback.get("summary")
    if not isinstance(previous_summary, dict):
        previous_summary = {}
    previous_payload = active_feedback.get("payload")
    if not isinstance(previous_payload, dict):
        previous_payload = {}
    previous_issue_id = str(previous_summary.get("issue_id") or previous_payload.get("issue_id") or "")
    previous_repair_route = str(previous_summary.get("repair_route") or previous_payload.get("repair_route") or "")
    previous_issues = previous_summary.get("issues") or previous_payload.get("issues") or []
    previous_repair_context = previous_summary.get("repair_context") or previous_payload.get("repair_context") or {}
    return {
        "version": 1,
        "tool": "external_designer_author",
        "attempt": attempt_index,
        "attempt_dir": str(attempt_dir),
        "status": "error",
        "error_message": "repair attempt produced identical poster.html",
        "error_category": "validation",
        "summary": {
            "issue_id": "designer_author_repair_noop",
            "repair_route": "author_must_apply_feedback_diff",
            "hint": (
                "The previous repair attempt wrote the same poster.html bytes. "
                "Use previous_poster.html as a read-only baseline and write a concrete local diff to poster.html."
            ),
            "previous_issue_id": previous_issue_id,
            "previous_hint": previous_summary.get("hint") or previous_summary.get("local_repair_hint") or "",
            "original_issue_id": previous_issue_id,
            "original_repair_route": previous_repair_route,
            "original_issues": previous_issues if isinstance(previous_issues, list) else [],
        },
        "payload": {
            "issue_id": "designer_author_repair_noop",
            "repair_route": "author_must_apply_feedback_diff",
            "previous_feedback": previous_feedback,
            "original_feedback": active_feedback,
            "original_issue_id": previous_issue_id,
            "original_repair_route": previous_repair_route,
            "original_issues": previous_issues if isinstance(previous_issues, list) else [],
            "original_repair_context": previous_repair_context if isinstance(previous_repair_context, dict) else {},
        },
    }


_LOCAL_REPAIR_SCOPE_CLASSIFICATIONS = {
    "micro_overflow",
    "section_content_overflow",
    "heading_flow_overflow",
    "root_wrapper_padding_overflow",
    "density_conservation_failure",
    "source_visual_sizing_failure",
    "blank_fill_repair",
}


def _local_repair_scope_violation_feedback(
    *,
    previous_feedback: dict[str, Any],
    previous_poster_path: Path,
    poster_path: Path,
    attempt_index: int,
    attempt_dir: Path,
) -> dict[str, Any] | None:
    active_feedback = _active_feedback_for_repair(previous_feedback)
    summary = active_feedback.get("summary") if isinstance(active_feedback.get("summary"), dict) else {}
    payload = active_feedback.get("payload") if isinstance(active_feedback.get("payload"), dict) else {}
    repair_context = summary.get("repair_context") or payload.get("repair_context") or {}
    if not isinstance(repair_context, dict):
        return None
    classification = str(repair_context.get("classification") or "")
    has_blank_fill_scope = _has_required_blank_fill_context(repair_context)
    if classification not in _LOCAL_REPAIR_SCOPE_CLASSIFICATIONS and not has_blank_fill_scope:
        return None
    if classification == "source_visual_sizing_failure" and not _source_visual_sizing_scope_guard_enabled(repair_context):
        return None
    repair_scope = repair_context.get("repair_scope") if isinstance(repair_context.get("repair_scope"), dict) else {}
    target_ids = _scope_target_block_ids(repair_context)
    if not target_ids and classification != "root_wrapper_padding_overflow":
        return None
    if not previous_poster_path.exists() or not poster_path.exists():
        return None
    try:
        previous_html = previous_poster_path.read_text(encoding="utf-8", errors="replace")
        current_html = poster_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    previous_soup = BeautifulSoup(previous_html, "html.parser")
    current_soup = BeautifulSoup(current_html, "html.parser")
    violations = _local_repair_scope_violations(previous_soup, current_soup, target_ids)
    if not violations:
        return None
    allowed_selectors = _string_list(repair_scope.get("allowed_selectors"))
    forbidden_selectors = _string_list(repair_scope.get("forbidden_selectors"))
    underlying_issue_id = str(repair_context.get("primary_blocking_issue_id") or repair_context.get("issue_id") or "")
    underlying_repair_route = str(repair_context.get("repair_route") or "")
    underlying_classification = str(repair_context.get("classification") or classification)
    hint = (
        "The repair changed content outside the local repair scope. Use previous_poster.html as a read-only "
        "baseline, write the revised candidate to poster.html, and patch only the listed target blocks/selectors; "
        "preserve non-target sections and source evidence."
    )
    feedback_payload = {
        "issue_id": "designer_author_local_repair_scope_violation",
        "repair_route": "author_apply_local_patch",
        "hint": hint,
        "issues": violations[:8],
        "violations": violations[:12],
        "classification": classification,
        "target_block_ids": sorted(target_ids),
        "allowed_selectors": allowed_selectors,
        "forbidden_selectors": forbidden_selectors,
        "underlying_issue_id": underlying_issue_id,
        "underlying_repair_route": underlying_repair_route,
        "underlying_classification": underlying_classification,
        "original_repair_context": repair_context,
        "previous_feedback": active_feedback,
    }
    log(
        "designer_author.local_repair_scope_violation",
        attempt=attempt_index,
        violation_count=len(violations),
        classification=classification,
        first_violations=violations[:4],
    )
    return {
        "version": 1,
        "tool": "external_designer_author",
        "attempt": attempt_index,
        "attempt_dir": str(attempt_dir),
        "status": "error",
        "error_message": "external designer author repair changed content outside its local repair scope",
        "error_category": "validation",
        "summary": {
            "issue_id": "designer_author_local_repair_scope_violation",
            "repair_route": "author_apply_local_patch",
            "hint": hint,
            "issues": violations[:8],
            "violations": violations[:8],
            "target_block_ids": sorted(target_ids),
            "allowed_selectors": allowed_selectors,
            "forbidden_selectors": forbidden_selectors,
            "underlying_issue_id": underlying_issue_id,
            "underlying_repair_route": underlying_repair_route,
            "underlying_classification": underlying_classification,
            "repair_context": {
                "version": 1,
                "classification": "local_repair_scope_violation",
                "issue_id": "designer_author_local_repair_scope_violation",
                "repair_route": "author_apply_local_patch",
                "issues": violations[:8],
                "repair_scope": repair_scope,
                "underlying_issue_id": underlying_issue_id,
                "underlying_repair_route": underlying_repair_route,
                "underlying_classification": underlying_classification,
                "local_repair_scope_violation": {
                    "violations": violations[:8],
                    "target_block_ids": sorted(target_ids),
                    "allowed_selectors": allowed_selectors,
                    "forbidden_selectors": forbidden_selectors,
                    "original_repair_context": repair_context,
                },
            },
        },
        "payload": feedback_payload,
    }


def _scope_target_block_ids(repair_context: dict[str, Any]) -> set[str]:
    ids: list[str] = []
    scope = repair_context.get("repair_scope") if isinstance(repair_context.get("repair_scope"), dict) else {}
    ids.extend(_string_list(scope.get("target_block_ids")))
    ids.extend(_string_list(scope.get("target_source_ids")))
    ids.extend(_string_list(scope.get("allowed_filler_block_ids")))
    for key in ("heading_flow", "section_content", "local_flow", "density_conservation", "source_visual_sizing", "blank_fill"):
        context = repair_context.get(key)
        if not isinstance(context, dict):
            continue
        targets = context.get("targets") if isinstance(context.get("targets"), list) else []
        for target in targets:
            if not isinstance(target, dict):
                continue
            ids.extend(_string_list(target.get("target_block_ids")))
            ids.extend(_string_list(target.get("allowed_filler_block_ids")))
            for field in (
                "block_id",
                "container_id",
                "section_id",
                "flow_unit_id",
                "overflow_block_id",
                "panel_id",
                "asset_block_id",
                "source_block_id",
                "source_id",
            ):
                value = str(target.get(field) or "").strip()
                if value:
                    ids.append(value)
    return set(_unique_strings(ids))


def _has_required_blank_fill_context(repair_context: dict[str, Any]) -> bool:
    if (
        isinstance(repair_context.get("blank_fill"), dict)
        and bool(_blank_fill_required_targets(repair_context["blank_fill"]))
    ):
        return True
    required = repair_context.get("required_co_repair")
    if isinstance(required, dict) and isinstance(required.get("blank_fill"), dict):
        return bool(_blank_fill_required_targets(required["blank_fill"]))
    followup = repair_context.get("post_overflow_required_followup")
    if isinstance(followup, dict) and isinstance(followup.get("blank_fill"), dict):
        return bool(_blank_fill_required_targets(followup["blank_fill"]))
    return False


def _source_visual_sizing_scope_guard_enabled(repair_context: dict[str, Any]) -> bool:
    source_visual = repair_context.get("source_visual_sizing")
    if not isinstance(source_visual, dict):
        return False
    required_targets = (
        source_visual.get("required_targets")
        if isinstance(source_visual.get("required_targets"), list) else
        []
    )
    targets = required_targets or (source_visual.get("targets") if isinstance(source_visual.get("targets"), list) else [])
    if not targets:
        return False
    scoped_problems = {"readable_visual_flow_underfilled", "minor_geometry_gap"}
    for target in targets:
        if not isinstance(target, dict):
            return False
        target_problem = str(target.get("target_problem") or "")
        primary_action = str(target.get("primary_repair_action") or "")
        if target_problem not in scoped_problems and primary_action != "fill_same_source_flow_unit_with_source_backed_readout":
            return False
    return True


def _local_repair_scope_violations(
    previous_soup: BeautifulSoup,
    current_soup: BeautifulSoup,
    target_ids: set[str],
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    previous_sources = _attr_values(previous_soup, "data-source-id")
    current_sources = _attr_values(current_soup, "data-source-id")
    lost_sources = sorted(previous_sources - current_sources)
    if lost_sources:
        violations.append({
            "id": "non_target_source_id_removed",
            "lost_source_ids": lost_sources[:12],
            "expected": "Local repair must preserve source evidence assets.",
        })
    previous_layers = _attr_values(previous_soup, "data-layer-id")
    current_layers = _attr_values(current_soup, "data-layer-id")
    lost_layers = sorted(previous_layers - current_layers)
    if lost_layers:
        violations.append({
            "id": "non_target_layer_id_removed",
            "lost_layer_ids": lost_layers[:12],
            "expected": "Local repair must preserve rendered layer bindings.",
        })
    previous_columns = len(previous_soup.select(".poster-column,[data-column-id]"))
    current_columns = len(current_soup.select(".poster-column,[data-column-id]"))
    if previous_columns and current_columns != previous_columns:
        violations.append({
            "id": "column_count_changed_for_local_repair",
            "previous_column_count": previous_columns,
            "current_column_count": current_columns,
        })
    previous_sections = len(previous_soup.select(".poster-section"))
    current_sections = len(current_soup.select(".poster-section"))
    if previous_sections and current_sections != previous_sections:
        violations.append({
            "id": "section_count_changed_for_local_repair",
            "previous_section_count": previous_sections,
            "current_section_count": current_sections,
        })
    previous_flow_units = _flow_unit_ids(previous_soup)
    current_flow_units = _flow_unit_ids(current_soup)
    lost_flow_units = sorted(previous_flow_units - current_flow_units - target_ids)
    if lost_flow_units:
        violations.append({
            "id": "non_target_source_flow_unit_removed",
            "lost_flow_unit_ids": lost_flow_units[:12],
        })
    previous_global_layout_css = _global_layout_css_signature(previous_soup)
    current_global_layout_css = _global_layout_css_signature(current_soup)
    if current_global_layout_css != previous_global_layout_css:
        violations.append({
            "id": "global_column_or_row_css_changed_for_local_repair",
            "expected": "Scoped local/source-flow repair must not rewrite .poster-columns or .poster-column layout CSS.",
        })
    previous_blocks = _attr_values(previous_soup, "data-block-id")
    current_blocks = _attr_values(current_soup, "data-block-id")
    lost_blocks = []
    for block_id in sorted(previous_blocks - current_blocks - target_ids):
        tag = previous_soup.find(attrs={"data-block-id": block_id})
        if isinstance(tag, Tag) and _tag_in_local_repair_scope(tag, target_ids):
            continue
        if isinstance(tag, Tag) and _tag_text_preserved_elsewhere(tag, current_soup):
            continue
        lost_blocks.append(block_id)
    if len(lost_blocks) >= 3:
        violations.append({
            "id": "non_target_block_ids_removed",
            "lost_block_ids": lost_blocks[:16],
        })
    previous_words = _outside_target_word_count(previous_soup, target_ids)
    current_words = _outside_target_word_count(current_soup, target_ids)
    if previous_words >= 120 and previous_words - current_words >= 50:
        loss_ratio = round((previous_words - current_words) / max(1, previous_words), 3)
        if loss_ratio >= 0.22:
            violations.append({
                "id": "non_target_text_removed",
                "previous_outside_target_words": previous_words,
                "current_outside_target_words": current_words,
                "word_loss_ratio": loss_ratio,
            })
    return violations


def _global_layout_css_signature(soup: BeautifulSoup) -> list[str]:
    lines: list[str] = []
    for style in soup.find_all("style"):
        text = style.get_text("\n", strip=False)
        for raw_line in text.splitlines():
            line = " ".join(raw_line.strip().split())
            if not line:
                continue
            if ".poster-columns" in line or ".poster-column" in line:
                lines.append(line)
    for tag in soup.select(".poster-columns,.poster-column"):
        style = str(tag.get("style") or "").strip()
        if not style:
            continue
        block_id = str(tag.get("data-block-id") or tag.get("data-column-id") or "").strip()
        classes = ".".join(str(cls) for cls in (tag.get("class") or []))
        lines.append(f"{block_id}:{classes}:{style}")
    return lines


def _attr_values(soup: BeautifulSoup, attr: str) -> set[str]:
    values: set[str] = set()
    for tag in soup.find_all(attrs={attr: True}):
        value = str(tag.get(attr) or "").strip()
        if value:
            values.add(value)
    return values


def _flow_unit_ids(soup: BeautifulSoup) -> set[str]:
    ids: set[str] = set()
    for tag in soup.select(".figure-flow-unit,.source-flow-unit"):
        source_id = str(tag.get("data-source-id") or "").strip()
        layer_id = str(tag.get("data-layer-id") or "").strip()
        block_id = str(tag.get("data-block-id") or "").strip()
        if source_id:
            ids.add(source_id)
        elif layer_id:
            ids.add(layer_id)
        elif block_id:
            ids.add(block_id)
    return ids


def _tag_in_local_repair_scope(tag: Tag, target_ids: set[str]) -> bool:
    node: Tag | None = tag
    while isinstance(node, Tag):
        for attr in ("data-block-id", "data-source-id", "data-layer-id"):
            value = str(node.get(attr) or "").strip()
            if value and value in target_ids:
                return True
        node = node.parent if isinstance(node.parent, Tag) else None
    return False


def _tag_text_preserved_elsewhere(tag: Tag, current_soup: BeautifulSoup) -> bool:
    text = " ".join(tag.get_text(" ", strip=True).split())
    if len(text) < 32:
        return False
    current_text = " ".join(current_soup.get_text(" ", strip=True).split())
    return text in current_text


def _outside_target_word_count(soup: BeautifulSoup, target_ids: set[str]) -> int:
    working = BeautifulSoup(str(soup), "html.parser")
    for target_id in target_ids:
        for tag in working.find_all(attrs={"data-block-id": target_id}):
            tag.decompose()
    text = " ".join(working.get_text(" ", strip=True).split())
    return len(re.findall(r"\b[\w'-]+\b", text))


def _auto_repair_identity_header_only(
    ctx: ToolContext,
    attempt_dir: Path,
    poster_path: Path,
    feedback: dict[str, Any],
) -> str:
    summary = feedback.get("summary") if isinstance(feedback.get("summary"), dict) else {}
    issue_id = str(summary.get("issue_id") or "")
    if issue_id != "paper_poster_html_identity_header_only_failed":
        return ""
    issues = summary.get("issues")
    if not isinstance(issues, list) or not issues or not poster_path.exists():
        return ""

    text = poster_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(text, "html.parser")
    patched: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        target_text = _normalize_visible_text(str(issue.get("text") or ""))
        if not target_text:
            continue
        header_id = str(issue.get("header_id") or "")
        header = soup.find(attrs={"data-block-id": header_id}) if header_id else None
        if header is None:
            header = soup.find("header")
        if header is None:
            continue
        target = _find_smallest_text_container(header, target_text)
        if target is None:
            continue
        patched.append({
            "header_id": header_id,
            "text": target_text,
            "tag": str(target.name or ""),
            "classes": [str(cls) for cls in (target.get("class") or [])],
        })
        target.decompose()
    if not patched:
        return ""

    backup_path = attempt_dir / "poster_before_auto_identity_header_repair.html"
    if not backup_path.exists():
        shutil.copy2(poster_path, backup_path)
    poster_path.write_text(str(soup), encoding="utf-8")
    repair_record = {
        "version": 1,
        "repair": "identity_header_only",
        "patched": patched,
        "backup": str(backup_path),
        "poster_sha256": sha256_file(poster_path),
    }
    atomic_write_json(attempt_dir / "auto_repair_identity_header.json", repair_record)
    ctx.state["designer_author_auto_repair"] = repair_record
    log(
        "designer_author.auto_repair",
        mode="external",
        attempt_dir=str(attempt_dir),
        repair="identity_header_only",
        patched=len(patched),
    )
    return "identity_header_only"


def _find_smallest_text_container(root: Any, target_text: str) -> Any | None:
    matches: list[Any] = []
    for tag in root.find_all(True):
        visible = _normalize_visible_text(tag.get_text(" ", strip=True))
        if visible == target_text:
            matches.append(tag)
    if matches:
        return min(matches, key=lambda tag: len(str(tag)))
    for tag in root.find_all(True):
        visible = _normalize_visible_text(tag.get_text(" ", strip=True))
        if target_text in visible:
            matches.append(tag)
    if matches:
        return min(matches, key=lambda tag: len(str(tag)))
    return None


def _normalize_visible_text(value: str) -> str:
    return " ".join(value.split())


def _auto_repair_source_wrap_missing(
    ctx: ToolContext,
    attempt_dir: Path,
    poster_path: Path,
    feedback: dict[str, Any],
) -> str:
    summary = feedback.get("summary") if isinstance(feedback.get("summary"), dict) else {}
    issue_id = str(summary.get("issue_id") or "")
    if issue_id != "paper_poster_html_source_wrap_missing":
        return ""
    issues = summary.get("issues")
    if not isinstance(issues, list) or not issues:
        return ""
    repairable = [
        issue for issue in issues
        if isinstance(issue, dict) and _source_wrap_issue_is_micro_repairable(issue)
    ]
    if not repairable:
        return _record_source_wrap_auto_repair_skip(
            ctx,
            attempt_dir,
            reason="source_wrap_requires_author_restructure",
            issues=issues,
            repairable_count=0,
        )
    if len(repairable) > 3:
        return _record_source_wrap_auto_repair_skip(
            ctx,
            attempt_dir,
            reason="too_many_source_wrap_micro_repairs",
            issues=issues,
            repairable_count=len(repairable),
        )
    if not poster_path.exists():
        return _record_source_wrap_auto_repair_skip(
            ctx,
            attempt_dir,
            reason="poster_html_missing",
            issues=issues,
            repairable_count=len(repairable),
        )

    text = poster_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(text, "html.parser")
    if soup.head is None:
        return _record_source_wrap_auto_repair_skip(
            ctx,
            attempt_dir,
            reason="html_head_missing",
            issues=issues,
            repairable_count=len(repairable),
        )

    patched: list[dict[str, Any]] = []
    for idx, issue in enumerate(repairable, start=1):
        source_id = str(issue.get("source_id") or "")
        block_id = str(issue.get("block_id") or "")
        tag = _source_wrap_target_tag(soup, source_id=source_id, block_id=block_id)
        if tag is None:
            continue
        classes = [str(cls) for cls in (tag.get("class") or [])]
        if any(cls.startswith("od-auto-source-wrap-") for cls in classes):
            continue
        repair_class = f"od-auto-source-wrap-{idx}-{_css_class_suffix(source_id or block_id)}"
        classes.append(repair_class)
        tag["class"] = classes
        patched.append({
            "source_id": source_id,
            "block_id": block_id,
            "repair_class": repair_class,
        })
    if not patched:
        return _record_source_wrap_auto_repair_skip(
            ctx,
            attempt_dir,
            reason="source_wrap_target_not_found",
            issues=issues,
            repairable_count=len(repairable),
        )

    backup_path = attempt_dir / "poster_before_auto_source_wrap_repair.html"
    if not backup_path.exists():
        shutil.copy2(poster_path, backup_path)

    style = soup.new_tag("style")
    style["data-od-auto-repair"] = "source-wrap"
    style.string = _source_wrap_repair_css(patched)
    soup.head.append(style)
    poster_path.write_text(str(soup), encoding="utf-8")
    repair_record = {
        "version": 1,
        "repair": "source_wrap_missing",
        "patched": patched,
        "backup": str(backup_path),
        "poster_sha256": sha256_file(poster_path),
    }
    atomic_write_json(attempt_dir / "auto_repair_source_wrap.json", repair_record)
    ctx.state["designer_author_auto_repair"] = repair_record
    log(
        "designer_author.auto_repair",
        mode="external",
        attempt_dir=str(attempt_dir),
        repair="source_wrap_missing",
        patched=len(patched),
        source_ids=[item.get("source_id") for item in patched],
    )
    return "source_wrap_missing"


def _record_source_wrap_auto_repair_skip(
    ctx: ToolContext,
    attempt_dir: Path,
    *,
    reason: str,
    issues: list[Any],
    repairable_count: int,
) -> str:
    source_ids = [
        str(issue.get("source_id") or "")
        for issue in issues
        if isinstance(issue, dict) and str(issue.get("source_id") or "")
    ]
    record = {
        "version": 1,
        "repair": "source_wrap_missing",
        "status": "skipped",
        "reason": reason,
        "issue_count": len(issues),
        "repairable_count": repairable_count,
        "source_ids": source_ids[:12],
    }
    atomic_write_json(attempt_dir / "auto_repair_source_wrap_skipped.json", record)
    log(
        "designer_author.auto_repair_skipped",
        mode="external",
        attempt_dir=str(attempt_dir),
        repair="source_wrap_missing",
        reason=reason,
        issue_count=len(issues),
        repairable_count=repairable_count,
        source_ids=source_ids[:8],
    )
    ctx.state["designer_author_auto_repair_skip"] = record
    return ""


def _source_wrap_issue_is_micro_repairable(issue: dict[str, Any]) -> bool:
    if issue.get("visible_figcaption"):
        return False
    if issue.get("separate_layout_evidence") or issue.get("flow_unit_violations"):
        return False
    reason = str(issue.get("reason") or "").lower()
    if "float" not in reason and "shape-outside" not in reason and "not authored" not in reason:
        return False
    flow_unit = str(issue.get("flow_unit") or "")
    if "source-flow-unit" not in flow_unit and "figure-flow-unit" not in flow_unit:
        return False
    local_words = _safe_int(issue.get("local_words"), default=0)
    return 6 <= local_words <= 140


def _source_wrap_target_tag(soup: BeautifulSoup, *, source_id: str, block_id: str) -> Any | None:
    if block_id:
        by_block = soup.find(attrs={"data-block-id": block_id})
        if by_block is not None:
            if str(by_block.name or "").lower() == "img":
                parent = by_block.find_parent(["figure", "table"])
                return parent or by_block
            return by_block
    if not source_id:
        return None
    for name in ("figure", "table", "img"):
        tag = soup.find(name, attrs={"data-source-id": source_id})
        if tag is None:
            tag = soup.find(name, attrs={"data-layer-id": source_id})
        if tag is None:
            continue
        if name == "img":
            parent = tag.find_parent(["figure", "table"])
            return parent or tag
        return tag
    return None


def _source_wrap_repair_css(patched: list[dict[str, Any]]) -> str:
    css = ["/* AutoDesign auto repair: scoped source wrap fix. */"]
    for item in patched:
        repair_class = str(item.get("repair_class") or "")
        if not repair_class:
            continue
        selector = f".{repair_class}"
        css.extend([
            f"{selector} {{",
            "  float: right !important;",
            "  width: 58% !important;",
            "  max-width: 58% !important;",
            "  max-height: 300px !important;",
            "  margin: 0 0 10px 18px !important;",
            "  shape-outside: inset(0 round 6px) !important;",
            "}",
            f"{selector} img {{",
            "  width: 100% !important;",
            "  height: auto !important;",
            "  max-height: inherit !important;",
            "  object-fit: contain !important;",
            "}",
        ])
    return "\n".join(css)


def _auto_repair_source_flow_list_gutter(
    ctx: ToolContext,
    attempt_dir: Path,
    poster_path: Path,
    feedback: dict[str, Any],
) -> bool:
    if not _feedback_has_issue_id(feedback, "paper_poster_source_flow_list_marker_gutter_low"):
        return False
    if not poster_path.exists():
        return False
    text = poster_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(text, "html.parser")
    if soup.head is None:
        return False
    existing = soup.find("style", attrs={"data-od-auto-repair": "source-flow-list-gutter"})
    if existing is not None:
        return False

    backup_path = attempt_dir / "poster_before_source_flow_list_gutter.html"
    if not backup_path.exists():
        shutil.copy2(poster_path, backup_path)
    style = soup.new_tag("style")
    style["data-od-auto-repair"] = "source-flow-list-gutter"
    style.string = _source_flow_list_gutter_repair_css()
    soup.head.append(style)
    poster_path.write_text(str(soup), encoding="utf-8")
    repair_record = {
        "version": 1,
        "repair": "source_flow_list_gutter",
        "backup": str(backup_path),
        "poster_sha256": sha256_file(poster_path),
    }
    atomic_write_json(attempt_dir / "auto_repair_source_flow_list_gutter.json", repair_record)
    ctx.state["designer_author_auto_repair"] = repair_record
    log(
        "designer_author.auto_repair",
        mode="external",
        attempt_dir=str(attempt_dir),
        repair="source_flow_list_gutter",
    )
    return True


def _source_flow_list_gutter_repair_css() -> str:
    return "\n".join([
        "/* AutoDesign auto repair: scoped source-flow list marker gutter. */",
        ".paper-poster :where(.source-flow-unit,.figure-flow-unit) > :where(ul,ol) {",
        "  display: flow-root !important;",
        "  margin: .25em 0 .35em !important;",
        "  padding-inline-start: 1.25em !important;",
        "  list-style-position: outside !important;",
        "}",
        ".paper-poster :where(.source-flow-unit,.figure-flow-unit) > ul > li,",
        ".paper-poster :where(.source-flow-unit,.figure-flow-unit) > ol > li {",
        "  padding-inline-start: .28em !important;",
        "}",
    ])


def _feedback_has_issue_id(feedback: dict[str, Any], issue_id: str) -> bool:
    containers: list[Any] = [feedback]
    for key in ("summary", "payload"):
        value = feedback.get(key) if isinstance(feedback, dict) else None
        if isinstance(value, dict):
            containers.append(value)
    for container in list(containers):
        if not isinstance(container, dict):
            continue
        if str(container.get("issue_id") or container.get("id") or "") == issue_id:
            return True
        for key in ("issues", "paper_poster_dom_findings", "poster_gate_findings_sample", "findings"):
            values = container.get(key)
            if not isinstance(values, list):
                continue
            for item in values:
                if isinstance(item, dict) and str(item.get("id") or item.get("issue_id") or "") == issue_id:
                    return True
    return False


def _auto_repair_editorial_contribution_text_only(
    ctx: ToolContext,
    attempt_dir: Path,
    poster_path: Path,
    feedback: dict[str, Any],
) -> str:
    summary = feedback.get("summary") if isinstance(feedback.get("summary"), dict) else {}
    issue_id = str(summary.get("issue_id") or "")
    if issue_id != "paper_poster_html_editorial_flow_shape_failed":
        return ""
    issues = summary.get("issues")
    if not isinstance(issues, list) or not poster_path.exists():
        return ""
    targets = [
        issue for issue in issues
        if isinstance(issue, dict) and issue.get("id") == "editorial_contribution_text_only_panel"
    ]
    if not targets:
        return ""

    text = poster_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(text, "html.parser")
    if soup.head is None:
        return ""

    patched: list[dict[str, Any]] = []
    for issue in targets:
        section_id = str(issue.get("section_id") or "").strip()
        section = soup.find(attrs={"data-block-id": section_id}) if section_id else None
        if section is None:
            section = _find_contribution_section(soup)
        if section is None:
            continue
        if section.find("table"):
            continue

        rows = _contribution_rows_from_section(section)
        if len(rows) < 2:
            continue

        table = _build_contribution_table(soup, rows, section_id or "panel_contributions")
        grid = section.select_one(".compact-grid")
        if grid is not None:
            grid.replace_with(table)
        else:
            body = section.select_one(".section-body") or section
            first_para = body.find("p")
            if first_para is not None:
                first_para.insert_after(table)
            else:
                body.insert(0, table)
        patched.append({
            "section_id": section_id or str(section.get("data-block-id") or ""),
            "row_count": len(rows),
        })

    if not patched:
        return ""

    backup_path = attempt_dir / "poster_before_auto_contribution_table_repair.html"
    if not backup_path.exists():
        shutil.copy2(poster_path, backup_path)

    style = soup.new_tag("style")
    style["data-od-auto-repair"] = "contribution-table"
    style.string = _contribution_table_repair_css()
    soup.head.append(style)
    poster_path.write_text(str(soup), encoding="utf-8")
    repair_record = {
        "version": 1,
        "repair": "editorial_contribution_text_only",
        "patched": patched,
        "backup": str(backup_path),
        "poster_sha256": sha256_file(poster_path),
    }
    atomic_write_json(attempt_dir / "auto_repair_contribution_table.json", repair_record)
    ctx.state["designer_author_auto_repair"] = repair_record
    log(
        "designer_author.auto_repair",
        mode="external",
        attempt_dir=str(attempt_dir),
        repair="editorial_contribution_text_only",
        patched=len(patched),
    )
    return "editorial_contribution_text_only"


def _find_contribution_section(soup: BeautifulSoup) -> Any | None:
    for section in soup.select(".poster-section"):
        identity = " ".join(
            str(value or "")
            for value in (
                section.get("data-block-id"),
                section.get("data-panel-role"),
                section.get_text(" ", strip=True)[:160],
            )
        )
        if "contribution" in identity.lower():
            return section
    return None


def _contribution_rows_from_section(section: Any) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for item in section.select(".principle"):
        label_tag = item.find("strong")
        label = _normalize_visible_text(label_tag.get_text(" ", strip=True) if label_tag else "")
        description = _normalize_visible_text(item.get_text(" ", strip=True))
        if label and description.startswith(label):
            description = _normalize_visible_text(description[len(label):])
        if label and description:
            rows.append((label, description))
    if rows:
        return rows

    for li in section.find_all("li"):
        text = _normalize_visible_text(li.get_text(" ", strip=True))
        if not text:
            continue
        if ":" in text:
            label, description = text.split(":", 1)
        else:
            words = text.split()
            label, description = " ".join(words[:4]), " ".join(words[4:])
        label = _normalize_visible_text(label)
        description = _normalize_visible_text(description)
        if label and description:
            rows.append((label, description))
    return rows[:6]


def _build_contribution_table(soup: BeautifulSoup, rows: list[tuple[str, str]], section_id: str) -> Any:
    table = soup.new_tag(
        "table",
        attrs={
            "class": "small-table contribution-table",
            "data-role": "native-table",
            "data-block-id": f"{section_id}_native_contribution_table",
            "aria-label": "Contribution and mechanism summary",
        },
    )
    thead = soup.new_tag("thead")
    tr = soup.new_tag("tr")
    for heading in ("Contribution", "Mechanism in the paper"):
        th = soup.new_tag("th")
        th.string = heading
        tr.append(th)
    thead.append(tr)
    tbody = soup.new_tag("tbody")
    for label, description in rows[:6]:
        tr = soup.new_tag("tr")
        th = soup.new_tag("th")
        th.string = label
        td = soup.new_tag("td")
        td.string = description
        tr.append(th)
        tr.append(td)
        tbody.append(tr)
    table.append(thead)
    table.append(tbody)
    return table


def _contribution_table_repair_css() -> str:
    return "\n".join([
        "/* AutoDesign auto repair: turn contribution prose into a native evidence unit. */",
        ".contribution-table {",
        "  margin: 8px 0 10px !important;",
        "  border-collapse: collapse !important;",
        "  width: 100% !important;",
        "}",
        ".contribution-table th, .contribution-table td {",
        "  font-size: 15px !important;",
        "  line-height: 1.16 !important;",
        "  text-align: left !important;",
        "  vertical-align: top !important;",
        "}",
        ".contribution-table tbody th {",
        "  width: 34% !important;",
        "}",
    ])


def _auto_repair_root_wrapper_padding_overflow(
    ctx: ToolContext,
    attempt_dir: Path,
    poster_path: Path,
    feedback: dict[str, Any],
) -> str:
    summary = feedback.get("summary") if isinstance(feedback.get("summary"), dict) else {}
    issue_id = str(summary.get("issue_id") or "")
    if issue_id != "paper_poster_html_root_wrapper_padding_overflow":
        return ""
    if not poster_path.exists():
        return ""

    text = poster_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(text, "html.parser")
    if soup.head is None:
        return ""
    if _has_authored_full_editorial_poster_root(soup):
        _record_root_wrapper_auto_repair_skip(
            ctx,
            attempt_dir,
            poster_path,
            reason="authored_full_editorial_root_should_normalize_transparently",
        )
        return ""

    existing = soup.find("style", attrs={"data-od-auto-repair": "root-wrapper-padding-overflow"})
    if existing is not None:
        return ""
    repair_css = _root_wrapper_padding_repair_css(feedback)
    if not repair_css:
        _record_root_wrapper_auto_repair_skip(
            ctx,
            attempt_dir,
            poster_path,
            reason="root_wrapper_payload_not_safe_for_deterministic_repair",
        )
        return ""

    backup_path = attempt_dir / "poster_before_auto_root_wrapper_repair.html"
    if not backup_path.exists():
        shutil.copy2(poster_path, backup_path)

    style = soup.new_tag("style")
    style["data-od-auto-repair"] = "root-wrapper-padding-overflow"
    style.string = repair_css
    soup.head.append(style)
    poster_path.write_text(str(soup), encoding="utf-8")
    repair_record = {
        "version": 1,
        "repair": "root_wrapper_padding_overflow",
        "backup": str(backup_path),
        "poster_sha256": sha256_file(poster_path),
    }
    atomic_write_json(attempt_dir / "auto_repair_root_wrapper_padding.json", repair_record)
    ctx.state["designer_author_auto_repair"] = repair_record
    log(
        "designer_author.auto_repair",
        mode="external",
        attempt_dir=str(attempt_dir),
        repair="root_wrapper_padding_overflow",
    )
    return "root_wrapper_padding_overflow"


def _record_root_wrapper_auto_repair_skip(
    ctx: ToolContext,
    attempt_dir: Path,
    poster_path: Path,
    *,
    reason: str,
) -> None:
    skip_record = {
        "version": 1,
        "repair": "root_wrapper_padding_overflow",
        "skipped": True,
        "reason": reason,
        "poster_sha256": sha256_file(poster_path) if poster_path.exists() else "",
    }
    atomic_write_json(attempt_dir / "auto_repair_root_wrapper_padding_skipped.json", skip_record)
    ctx.state["designer_author_auto_repair_skipped"] = skip_record
    log(
        "designer_author.auto_repair_skipped",
        mode="external",
        attempt_dir=str(attempt_dir),
        repair="root_wrapper_padding_overflow",
        reason=reason,
    )


def _root_wrapper_padding_repair_css(feedback: dict[str, Any]) -> str:
    repair = _root_wrapper_padding_repair_from_feedback(feedback)
    if not repair:
        return ""
    width_px = int(repair["width_px"])
    height_px = int(repair["height_px"])
    return "\n".join([
        "/* AutoDesign auto repair: scoped root wrapper padding fix. */",
        ".paper-poster > .editorial-poster {",
        "  grid-row: 1 / -1 !important;",
        "  grid-column: 1 / -1 !important;",
        "  align-self: start !important;",
        "  justify-self: start !important;",
        f"  width: {width_px}px !important;",
        f"  height: {height_px}px !important;",
        f"  max-width: {width_px}px !important;",
        f"  max-height: {height_px}px !important;",
        "  min-width: 0 !important;",
        "  min-height: 0 !important;",
        "  box-sizing: border-box !important;",
        "}",
    ])


def _root_wrapper_padding_repair_from_feedback(feedback: dict[str, Any]) -> dict[str, int] | None:
    summary = feedback.get("summary") if isinstance(feedback.get("summary"), dict) else {}
    payload = feedback.get("payload") if isinstance(feedback.get("payload"), dict) else {}
    issues = summary.get("issues") or payload.get("issues") or []
    if not isinstance(issues, list) or not issues:
        return None
    if not all(isinstance(issue, dict) and issue.get("id") == "root_wrapper_padding_overflow" for issue in issues):
        return None
    issue = next((item for item in issues if isinstance(item, dict)), None)
    if not issue:
        return None
    classes = {str(cls) for cls in (issue.get("classes") or [])}
    if "editorial-poster" not in classes:
        return None
    deterministic = issue.get("deterministic_repair")
    if isinstance(deterministic, dict):
        width_px = _safe_int(deterministic.get("width_px"), default=0)
        height_px = _safe_int(deterministic.get("height_px"), default=0)
    else:
        width_px = 0
        height_px = 0
    bbox = issue.get("bbox") if isinstance(issue.get("bbox"), dict) else {}
    canvas = issue.get("canvas") if isinstance(issue.get("canvas"), dict) else {}
    cw = _safe_int(canvas.get("w"), default=0)
    ch = _safe_int(canvas.get("h"), default=0)
    x = _safe_int(bbox.get("x"), default=-1)
    y = _safe_int(bbox.get("y"), default=-1)
    w = _safe_int(bbox.get("w"), default=0)
    h = _safe_int(bbox.get("h"), default=0)
    if not width_px and cw > 0 and x >= 0:
        width_px = max(1, cw - max(0, x))
    if not height_px and ch > 0 and y >= 0:
        height_px = max(1, ch - max(0, y))
    if cw <= 0 or ch <= 0 or x < 0 or y < 0 or w < int(0.94 * cw) or h < int(0.94 * ch):
        return None
    if width_px < int(0.90 * cw) or height_px < int(0.90 * ch):
        return None
    overflow = issue.get("overflow_px") if isinstance(issue.get("overflow_px"), dict) else {}
    if _safe_int(overflow.get("left"), default=0) > 0 or _safe_int(overflow.get("top"), default=0) > 0:
        return None
    computed = issue.get("computed_style") if isinstance(issue.get("computed_style"), dict) else {}
    padding_max = max(
        _safe_int(computed.get("padding_left_px"), default=0) + _safe_int(computed.get("padding_right_px"), default=0),
        _safe_int(computed.get("padding_top_px"), default=0) + _safe_int(computed.get("padding_bottom_px"), default=0),
    )
    max_overflow = max((_safe_int(overflow.get(side), default=0) for side in ("right", "bottom")), default=0)
    if overflow and max_overflow > max(96, padding_max + 8):
        return None
    scroll = issue.get("scroll_overflow_px") if isinstance(issue.get("scroll_overflow_px"), dict) else {}
    if max((_safe_int(scroll.get(side), default=0) for side in ("right", "bottom")), default=0) > 4:
        return None
    return {"width_px": width_px, "height_px": height_px}


def _has_authored_full_editorial_poster_root(soup: BeautifulSoup) -> bool:
    for root in soup.select(".paper-poster.editorial-poster"):
        classes = _html_class_tokens(root)
        if "paper-poster" not in classes or "editorial-poster" not in classes:
            continue
        if _has_direct_editorial_header_and_columns(root):
            return True
    return False


def _has_direct_editorial_header_and_columns(tag: Any) -> bool:
    direct_children = [
        child
        for child in tag.find_all(True, recursive=False)
        if hasattr(child, "attrs")
    ]
    has_direct_header = any(
        getattr(child, "name", "") == "header"
        or bool({"poster-header", "identity-header"}.intersection(_html_class_tokens(child)))
        for child in direct_children
    )
    has_direct_columns = any(
        "poster-columns" in _html_class_tokens(child)
        for child in direct_children
    )
    return bool(has_direct_header and has_direct_columns)


def _html_class_tokens(tag: Any) -> set[str]:
    return {
        str(cls).strip()
        for cls in (getattr(tag, "attrs", {}) or {}).get("class", [])
        if str(cls).strip()
    }


def _css_class_suffix(value: str) -> str:
    suffix = "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
    return suffix or "source"


def _auto_repair_typography_line_height(
    ctx: ToolContext,
    attempt_dir: Path,
    poster_path: Path,
    feedback: dict[str, Any],
) -> bool:
    summary = feedback.get("summary") if isinstance(feedback.get("summary"), dict) else {}
    payload = feedback.get("payload") if isinstance(feedback.get("payload"), dict) else {}
    attempt_index = _safe_int(feedback.get("attempt"), default=0)
    if attempt_index < _SOFT_ACCEPT_MIN_ATTEMPT:
        return False
    issue_id = str(summary.get("issue_id") or payload.get("issue_id") or "")
    if issue_id != "paper_poster_html_typography_contract_failed":
        return False
    issues = _feedback_issues(summary, payload)
    if not 1 <= len(issues) <= 2:
        return False
    targets: list[tuple[str, float, float]] = []
    seen: set[str] = set()
    for issue in issues:
        if str(issue.get("failure_kind") or "") != "body_line_height_unsafe":
            return False
        block_id = str(issue.get("block_id") or "").strip()
        ratio = _safe_float(issue.get("actual_line_height"), default=0.0)
        if not block_id or block_id in seen:
            return False
        if 0.98 <= ratio < 1.04:
            target_ratio = 1.08
        elif 1.35 < ratio <= 1.45:
            target_ratio = 1.35
        else:
            return False
        seen.add(block_id)
        targets.append((block_id, ratio, target_ratio))
    if not poster_path.exists():
        return False

    text = poster_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(text, "html.parser")
    if soup.head is None:
        return False
    for block_id, _, _ in targets:
        if len(soup.find_all(attrs={"data-block-id": block_id})) != 1:
            return False

    backup_path = attempt_dir / "poster_before_auto_typography_line_height.html"
    if not backup_path.exists():
        shutil.copy2(poster_path, backup_path)

    style = soup.new_tag("style")
    style["data-od-auto-repair"] = "typography-line-height"
    style.string = _typography_line_height_repair_css(targets)
    soup.head.append(style)
    poster_path.write_text(str(soup), encoding="utf-8")
    repair_record = {
        "version": 1,
        "repair": "typography_line_height",
        "targets": [
            {
                "block_id": block_id,
                "actual_line_height": actual,
                "target_line_height": target,
            }
            for block_id, actual, target in targets
        ],
        "backup": str(backup_path),
        "poster_sha256": sha256_file(poster_path),
    }
    atomic_write_json(attempt_dir / "auto_repair_typography_line_height.json", repair_record)
    ctx.state["designer_author_auto_repair"] = repair_record
    log(
        "designer_author.auto_repair",
        mode="external",
        attempt_dir=str(attempt_dir),
        repair="typography_line_height",
        target_count=len(targets),
    )
    return True


def _typography_line_height_repair_css(targets: list[tuple[str, float, float]]) -> str:
    css = ["/* AutoDesign auto repair: scoped typography line-height near-miss fix. */"]
    for block_id, _, target_ratio in targets:
        selector = f'[data-block-id="{_css_attr_value(block_id)}"]'
        css.append(f"{selector} {{ line-height: {target_ratio:.2f} !important; }}")
        css.append(f"{selector} th, {selector} td {{ line-height: {target_ratio:.2f} !important; }}")
    return "\n".join(css)


def _css_attr_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _should_retry_import_error(result: ToolResultRecord) -> bool:
    if result.status != "error":
        return False
    category = (result.error_category or "").strip().lower()
    payload = result.payload or {}
    return category in {"validation", "contract", "tool"} or bool(
        payload.get("issue_id")
        or payload.get("repair_route")
        or payload.get("issues")
        or payload.get("hint")
        or payload.get("local_repair_hint")
    )


def _format_repair_prompt_block(
    feedback: dict[str, Any] | None,
    *,
    attempt_index: int = 1,
    max_attempts: int = 1,
    reference_style: dict[str, Any] | None = None,
) -> str:
    if not feedback:
        return ""
    noop_metadata = _noop_repair_metadata(feedback)
    active_feedback = _active_feedback_for_repair(feedback)
    summary = active_feedback.get("summary") if isinstance(active_feedback.get("summary"), dict) else {}
    payload = active_feedback.get("payload") if isinstance(active_feedback.get("payload"), dict) else {}
    issue_id = str(summary.get("issue_id") or payload.get("issue_id") or "")
    repair_route = str(summary.get("repair_route") or payload.get("repair_route") or "")
    feedback_tool = str(summary.get("feedback_tool") or payload.get("feedback_tool") or "")
    hint = str(summary.get("hint") or summary.get("local_repair_hint") or payload.get("hint") or payload.get("local_repair_hint") or "")
    reference_active = isinstance(reference_style, dict) and bool(reference_style)
    if reference_active:
        style_tokens = reference_style.get("style_tokens")
        columns = style_tokens.get("column_structure") if isinstance(style_tokens, dict) else None
        exact_counts = columns.get("major_sections_per_column") if isinstance(columns, dict) else None
        hint = (
            "Preserve the active reference-owned structure"
            + (f" with exact major-section counts {exact_counts}" if isinstance(exact_counts, list) else "")
            + "; use repair_context.json for the current local blocker and ignore stale generic layout ranges."
        )
    issues = summary.get("issues") or payload.get("issues") or []
    repair_context = summary.get("repair_context") or payload.get("repair_context") or {}
    repair_classification = (
        str(repair_context.get("classification") or "")
        if isinstance(repair_context, dict) else
        ""
    )
    raw_repair_classification = repair_classification
    if (
        repair_classification == "local_repair_scope_violation"
        and isinstance(repair_context, dict)
        and isinstance(repair_context.get("source_visual_sizing"), dict)
        and repair_context.get("underlying_classification") == "source_visual_sizing_failure"
    ):
        repair_classification = "source_visual_sizing_failure"
    if (
        repair_classification == "local_repair_scope_violation"
        and isinstance(repair_context, dict)
        and isinstance(repair_context.get("blank_fill"), dict)
        and (
            repair_context.get("underlying_classification") == "blank_fill_repair"
            or repair_context["blank_fill"].get("targets")
        )
    ):
        repair_classification = "blank_fill_repair"
    global_overflow_plan = (
        repair_context.get("global_overflow_repair_plan")
        if isinstance(repair_context.get("global_overflow_repair_plan"), dict) else
        summary.get("global_overflow_repair_plan")
        if isinstance(summary.get("global_overflow_repair_plan"), dict) else
        payload.get("global_overflow_repair_plan")
        if isinstance(payload.get("global_overflow_repair_plan"), dict) else
        {}
    )
    row_allocation = repair_context.get("row_allocation") if isinstance(repair_context.get("row_allocation"), dict) else {}
    attempt_critic = (
        repair_context.get("attempt_level_critic_feedback")
        if isinstance(repair_context.get("attempt_level_critic_feedback"), dict) else
        {}
    )
    max_overflow = _safe_int(
        global_overflow_plan.get("previous_max_bottom_overflow_px") if isinstance(global_overflow_plan, dict) else None,
        default=_safe_int(repair_context.get("max_scroll_overflow_px") if isinstance(repair_context, dict) else None, default=_max_scroll_overflow_px(issues if isinstance(issues, list) else [])),
    )
    preview_issues = issues
    if repair_classification == "source_wrap_failure" and isinstance(repair_context, dict):
        source_wrap = repair_context.get("source_wrap")
        if isinstance(source_wrap, dict) and isinstance(source_wrap.get("targets"), list):
            preview_issues = source_wrap.get("targets") or []
    issues_preview = ""
    if isinstance(preview_issues, list) and preview_issues:
        preview_limit = 8 if repair_classification == "row_allocation_failure" else 5
        issues_preview = json.dumps(preview_issues[:preview_limit], ensure_ascii=False, indent=2)
    has_near_miss_issues = (
        any(
            isinstance(issue, dict)
            and (issue.get("soft_finalizable") is True or str(issue.get("severity") or "") == "near_miss")
            for issue in issues
        )
        if isinstance(issues, list) else
        False
    )
    global_overflow_preview = ""
    if isinstance(global_overflow_plan, dict) and global_overflow_plan:
        global_overflow_preview = json.dumps(global_overflow_plan, ensure_ascii=False, indent=2)
    elif repair_classification == "row_allocation_failure" and isinstance(row_allocation, dict):
        row_targets = row_allocation.get("overflow_targets")
        if isinstance(row_targets, list) and row_targets:
            global_overflow_preview = json.dumps({
                "targets_sorted_by_severity": row_targets[:12],
                "column_budgets": row_allocation.get("column_budgets") or [],
            }, ensure_ascii=False, indent=2)
    secondary_preview = ""
    if isinstance(repair_context, dict):
        secondary = repair_context.get("secondary_gate_issues")
        if isinstance(secondary, list) and secondary:
            secondary_preview = json.dumps(secondary[:6], ensure_ascii=False, indent=2)
    visual_packet_preview = ""
    if isinstance(repair_context, dict):
        visual_packet = repair_context.get("visual_repair_packet")
        if isinstance(visual_packet, dict) and visual_packet:
            visual_packet_preview = json.dumps({
                "image_alignment_contract": {
                    "candidate_preview.png": "full current poster context",
                    "candidate_validation_overlay.png": "primary gate overlay for primary_blocking_issue_id",
                    "visual_repair/*_crops/*.png": "issue-local crop; use image_issue_map for exact issue/target",
                    "visual_repair/*_overlays/*.png": "secondary diagnostic overlay only; advisory next-risk context",
                    "locked_base_preview.png": "dense baseline/reference when present",
                },
                "primary_blocking_issue_id": visual_packet.get("primary_blocking_issue_id"),
                "must_read_images": visual_packet.get("must_read_images"),
                "image_issue_map": (visual_packet.get("image_issue_map") or [])[:12],
                "current_candidate": visual_packet.get("current_candidate"),
                "locked_base_candidate": visual_packet.get("locked_base_candidate"),
                "secondary_diagnostics_are_advisory": visual_packet.get("secondary_diagnostics_are_advisory"),
            }, ensure_ascii=False, indent=2)
    blank_fill_preview = ""
    blank_fill_instruction = ""
    blank_fill_preview_heading = "Blank-fill required targets:"
    active_primary_blank_fill = False
    advisory_blank_fill_preview = ""
    advisory_blank_fill_instruction = ""
    advisory_blank_fill_preview_block = ""
    if isinstance(repair_context, dict):
        raw_blank_fill = repair_context.get("blank_fill") if isinstance(repair_context.get("blank_fill"), dict) else {}
        blank_fill = raw_blank_fill if raw_blank_fill and _blank_fill_required_targets(raw_blank_fill) else {}
        advisory_blank_fill = (
            repair_context.get("advisory_blank_fill")
            if isinstance(repair_context.get("advisory_blank_fill"), dict) else
            {}
        )
        if not advisory_blank_fill and raw_blank_fill and not blank_fill:
            advisory_blank_fill = raw_blank_fill
        if blank_fill and isinstance(blank_fill.get("targets"), list) and blank_fill.get("targets"):
            active_primary_blank_fill = bool(blank_fill.get("active_primary_advisory_repair"))
            if active_primary_blank_fill:
                blank_fill_preview_heading = "Blank-fill active primary repair targets:"
            blank_fill_preview = json.dumps({
                "blank_fill_required": bool(blank_fill.get("blank_fill_required")),
                "active_primary_advisory_repair": bool(blank_fill.get("active_primary_advisory_repair")),
                "context_role": blank_fill.get("context_role"),
                "required_targets": blank_fill.get("required_targets") or [],
                "suppressed_targets": blank_fill.get("suppressed_targets") or [],
                "targets": blank_fill.get("targets")[:8],
                "instructions": blank_fill.get("instructions"),
            }, ensure_ascii=False, indent=2)
            blank_fill_instruction = _blank_fill_prompt_instruction(blank_fill, repair_classification)
        if advisory_blank_fill and isinstance(advisory_blank_fill.get("targets"), list) and advisory_blank_fill.get("targets"):
            advisory_blank_fill_preview = json.dumps({
                "blank_fill_required": False,
                "visual_reference_only": True,
                "advisory_targets": advisory_blank_fill.get("advisory_targets") or advisory_blank_fill.get("targets")[:8],
                "targets": advisory_blank_fill.get("targets")[:8],
                "instructions": advisory_blank_fill.get("instructions"),
            }, ensure_ascii=False, indent=2)
            advisory_blank_fill_instruction = _blank_fill_prompt_instruction(advisory_blank_fill, repair_classification)
            if blank_fill_preview:
                advisory_blank_fill_preview_block = (
                    "\nAdvisory blank-fill visual references:\n"
                    f"{advisory_blank_fill_preview}\n"
                )
            else:
                blank_fill_preview_heading = "Blank-fill advisory visual references:"
                blank_fill_preview = advisory_blank_fill_preview
                blank_fill_instruction = advisory_blank_fill_instruction
                advisory_blank_fill_instruction = ""
    noop_instruction = ""
    if noop_metadata:
        previous_issue = str(noop_metadata.get("previous_issue_id") or issue_id or "")
        noop_instruction = (
            f"- Your last repair produced byte-identical HTML. Use previous_poster.html as a read-only baseline; "
            f"write a revised poster.html with a real local diff. Original blocking issue: {previous_issue or 'unknown'}. "
            "Keep the original structured blocker targets below in scope.\n"
        )
    has_required_blank_fill = _has_required_blank_fill_context(repair_context) if isinstance(repair_context, dict) else False
    overflow_instruction = ""
    if repair_classification == "row_allocation_failure":
        row_rebalance_scope = (
            "all reference-owned body-region section rows"
            if reference_active
            else "all three column section rows"
        )
        overflow_instruction = (
            "- This is a row allocation failure, not a typography overflow. Do not shrink body font-size, "
            "line-height, source figures/tables, or global section padding as the primary fix.\n"
            f"- This is a global overflow repair. Previous max_scroll_overflow_px={max_overflow}; "
            "the next poster.html must clear every listed row_allocation target in this attempt. "
            "Target: no listed container/section has scroll_overflow_px.bottom > 3px, and no residual "
            "overflow >32px is acceptable.\n"
            "- Restore the authored editorial rows: `.editorial-poster` must own the fixed canvas height; "
            "`.poster-columns` must live in the `minmax(0,1fr)` body row with `min-height:0`, "
            "`align-self:stretch`, `align-items:stretch`, and `height:100%`; `.poster-section` rows must "
            "receive real height instead of collapsing to auto/min-content.\n"
            f"- Do not stop after adding height:100%/minmax(0,1fr). Recompute {row_rebalance_scope}, "
            "rebalance section heights, and shorten/split/move low-value local prose across affected sections "
            "until all listed overflow is gone.\n"
            "- Read repair_context.json.global_overflow_repair_plan.targets_sorted_by_severity and patch every "
            "listed target, starting with the largest bottom_overflow_px. Treat local_repair_hint as diagnostic "
            "only for this classification; do not patch a single named section first.\n"
            "- Preserve density by filling section bottoms with source-backed paper content or by rebalancing "
            "row allocation. Repair the row/grid sizing first, then make local text edits only if a small "
            "residual overflow remains.\n"
            "- Do not write designer_author_done.json until every listed row-allocation target has an explicit "
            "content, spacing, or row-height fix; a shell CSS-only diff is not a complete repair.\n"
        )
    elif repair_classification == "source_wrap_failure":
        overflow_instruction = (
            "- This is a source figure/table DOM flow failure. Read repair_context.json.source_wrap.targets "
            "and repair each listed source_id/block_id locally.\n"
            "- Source-flow repair applies only to body evidence figures/tables. Header/title_meta identity text "
            "must remain identity-only; do not give header identity labels local readouts, captions, or "
            ".figure-flow-unit/.source-flow-unit wrappers.\n"
            "- Every source asset must live in its own direct child of the panel root: "
            "`<section class=\"figure-flow-unit\" data-source-id=\"...\" data-layer-id=\"...\">`.\n"
            "- Put exactly that asset's `<figure class=\"flow-asset ...\">` source shell and its short "
            "local h/p/list/native-summary readout inside the same flow unit. For source table targets, "
            "the shell must show the original PDF crop; native rows may only supplement it as sibling summary. Do not leave the asset in `.visual-shell`, "
            "media/text split wrappers, shared strips, grids, or a panel-wide text flow.\n"
            "- For invalid_source_flow_unit targets, inspect `flow_unit_violation_details` first. If it "
            "contains `matched_selector`, patch that exact CSS/DOM target before making broader edits.\n"
            "- The `.figure-flow-unit/.source-flow-unit` element itself must be `display: flow-root` or "
            "`display: block`; do not put grid/flex on the flow-unit selector. A pure direct child "
            "`figure.flow-asset` source shell may use its own local image layout, but source-backed "
            "readout/native rows must be direct siblings in the same flow unit.\n"
            "- If `violation_kind=source_text_split_child`, remove that mixed child wrapper or flatten it "
            "so the source visual shell and readout/native rows are direct siblings; do not rewrite "
            "unrelated sections or `.poster-columns`.\n"
            "- If stale feedback mentions an identity_header/title_meta element, preserve the identity-only header "
            "and repair only the listed body evidence targets.\n"
            "- Replace visible figcaptions with nearby readout prose. Preserve all source_id/data-layer-id "
            "values and keep one flow unit per source asset, even when several assets share a panel.\n"
        )
    elif repair_classification == "section_content_overflow":
        structure_scope = (
            "current reference-owned body-region structure"
            if reference_active
            else "current three-column structure"
        )
        overflow_instruction = (
            "- This is named poster-section content overflow. Read repair_context.json.section_content.targets "
            "and patch only the listed section_id/container_id/flow_unit_id inside its current column.\n"
            f"- Keep the {structure_scope} and section identities. Clear the section scroll overflow "
            "by shortening low-value readout prose, merging or moving compact comparison table rows within the same column, "
            "reducing only local gaps/padding, or giving the overflowing section a bit more height by taking "
            "space from a less dense neighboring section.\n"
            "- If the target is a `.source-flow-unit` or `.figure-flow-unit`, keep the valid local source-flow "
            "DOM shape while making room; do not convert it into a media/text split wrapper, shared strip, or "
            "panel-wide grid just to reduce height.\n"
            "- Do not solve this by only changing source figure size, and do not use global body font-size or "
            "line-height reductions as the main fix. If a source figure also needs to grow, first make room in "
            "the section by trimming/rebalancing the surrounding readout.\n"
            "- Do not add overflow:hidden or max-height clipping to hide the content; the visible poster must "
            "fit in normal flow.\n"
        )
    elif repair_classification == "local_flow_overflow":
        overflow_instruction = (
            "- This is local flow overflow. Read repair_context.json.local_flow.targets and "
            "repair_context.json.repair_scope before editing.\n"
            "- Patch only the listed container_id/section_id/flow_unit_id and same-column sibling sections. "
            "Do not rebuild the poster, change column count, or delete non-target source evidence.\n"
            "- Clear overflow by local prose trimming, local spacing compaction, or same-column row rebalance; "
            "then preserve panel density by keeping source-flow units and meaningful readouts visible.\n"
            + (
                "- Near-miss local overflow means minimal local adjustment only: trim or compact the named block just enough to fit. "
                "Do not delete key rows/readout, remove source evidence, or hollow out the section to clear a small overflow.\n"
                if has_near_miss_issues else
                ""
            )
        )
    elif repair_classification == "source_visual_sizing_failure":
        scope_violation_prefix = (
            "- The last attempt violated local repair scope. Treat that as a wrapper around the original "
            "source visual flow-fill task: preserve all non-target sections/source ids and apply the same-flow "
            "fill repair to the listed source_visual_sizing targets only.\n"
            if raw_repair_classification == "local_repair_scope_violation" else
            ""
        )
        overflow_instruction = (
            scope_violation_prefix +
            "- This is a source visual sizing/fill failure. Read repair_context.json.source_visual_sizing.targets "
            "for each target_problem, primary_repair_action, recommended_first_action, and do_not_fix_by list before editing. "
            "Use repair_context.json.source_visual_sizing.required_targets as the blocking contract; "
            "advisory_targets are visual-reference polish only and must not broaden the repair scope.\n"
            "- For target_problem=readable_visual_flow_underfilled, preserve the current readable source visual size. "
            "Fill the blank space inside the same `.source-flow-unit` with source-backed readout, "
            "native mini-table rows, or mechanism bullets as direct siblings of the source asset. Patch only the "
            "listed flow_unit_id/panel_id and same-section readout; do not change `.poster-columns`, column row "
            "allocation, the global grid, the header, unrelated sections, or global font sizing.\n"
            "- For target_scope=create_direct_child_source_flow_unit, create exactly one direct-child "
            "`.figure-flow-unit.source-flow-unit[data-source-id][data-layer-id]` in the target panel, keep the "
            "current source visual readable size, and place direct sibling `p`, `ul`, `table`, or `div` readout/native rows "
            "inside that same unit. If that sibling is a `ul` or `ol`, reserve a real marker gutter with "
            "`display: flow-root`, `padding-inline-start: 1.25em` or more, and `li` padding; do not use "
            "`padding: 0`, negative indents, or custom absolute bullets beside a floated asset. Do not "
            "treat the bare image as the resize target.\n"
            "- When allowed_filler_block_ids are present, you may move those existing same-panel paper-fact blocks into "
            "the target source-flow unit, but preserve their original data-block-id and visible text. Do not create `_2` "
            "replacement block ids to sidestep the existing structure.\n"
            "- Repair all readable_visual_flow_underfilled targets in one pass; do not fix one image lane and leave the "
            "same blank-lane pattern in neighboring source sections.\n"
            "- For target_problem=minor_geometry_gap, make one local composition repair in the named source-flow unit; "
            "do not solve it by tiny CSS width nudges or global layout rewrites.\n"
            "- For target_problem=readable_visual_wrapper_polish, the source visual is already readable and the wrapper issue "
            "is near-miss polish. Preserve the current visual size; only make a small scoped wrapper-aspect/spacing repair "
            "if it is safe, and do not rewrite columns, row allocation, or body content for this target.\n"
            "- For target_problem=blank_wrapper_shell or blank_sidecar_lane, first fill the visual blankness: add "
            "source-backed local readout/native rows in the same `.source-flow-unit`, or make the source visual stacked/full-width "
            "with its readout below. Do not solve these targets with a tiny CSS width nudge only.\n"
            "- For target_problem=true_too_small_visual, restore each listed source figure/table to at least the required panel "
            "width ratio, height reference, and area/object-fit ratios while preserving aspect ratio; verify the change does not create same-panel overflow.\n"
            "- If repair_context.json.source_visual_sizing.post_fix_regression is present, the previous source-visual repair "
            "introduced overlap/local overflow/source loss. Clear that regression in the same local source-flow repair; do not "
            "hand off to a broad unrelated rewrite. If the previous repair resized images or changed global columns/rows for a "
            "flow-fill target, undo that broad rewrite and fill the same source-flow unit instead.\n"
            "- In visual_repair_packet.json, `diagnostic_geometry` is what the browser measured; "
            "`required_targets` is the acceptance target. Read `acceptance_mode`: hard ratio/fill targets are gates, while "
            "`height_px_reference` / "
            "`required_source_height_px` as an adaptive diagnostic reference, not a literal pixel height "
            "to force when it would create same-panel overflow.\n"
            "- If a target includes failure_kind=contain_wrapper_underfilled, object_fit_width_fill_ratio, "
            "object_fit_area_fill_ratio, wrapper_aspect_ratio, or intrinsic_aspect_ratio, repair the wrapper "
            "aspect mismatch. Increase wrapper height, reduce wrapper width, or use a narrower float/flow unit "
            "with adjacent local readout so the source visual visibly fills the wrapper. Do not leave a large "
            "white/bordered shell around a centered image.\n"
            "- If a target includes failure_kind=source_visual_sidecar_underfilled, flow_unit_id, "
            "side_text_coverage_ratio, local_word_count, required_min_words, or recommended_layout, repair "
            "that exact source-flow unit. A 36-78% floated source visual is valid only when direct sibling "
            "source-backed prose/native rows fill the side lane; otherwise make the asset `.asset-wide` / "
            "float:none / stacked and put the readout below. Do not leave a blank sidecar lane beside a "
            "half-width figure or table.\n"
            "- First check the same panel overflow fields in each target: panel_scroll_overflow_px, "
            "panel_client_height_px, and panel_scroll_height_px. If panel_scroll_overflow_px is greater than "
            "0, clear that same-panel overflow by shortening/rebalancing local readout and cards before "
            "increasing the source visual.\n"
            "- Do not satisfy overflow by turning paper visuals into tiny strips or by placing them inside "
            "oversized empty wrappers. Prefer `.asset-medium`, `.asset-large`, or `.asset-wide` sizing on "
            "the actual source wrapper, then shorten low-value readout prose or tighten local gaps if needed. "
            "When a target's required_panel_width_ratio is high, make tables/`.asset-wide` visuals actually "
            "span the panel instead of centering a 60-72% narrow strip.\n"
            "- Keep source aspect ratio with object-fit: contain and do not crop axes, legends, labels, or "
            "table content.\n"
        )
    elif repair_classification == "blank_fill_repair":
        if active_primary_blank_fill:
            overflow_instruction = (
                "- This is an active_primary_advisory_blank_fill repair: the remaining blank-fill targets are advisory-grade "
                "for final acceptance, but they are the current primary_blocking_issue_id and must receive a concrete local repair attempt now.\n"
                "- Patch only the listed active primary targets. Prefer compact comparison table rows, source-grounded bullets, method notes, ablation or limitation notes, local flow or section-height "
                "rebalance, stacked asset/readout flow, or reducing unused local section height. Do not rewrite columns, edit the header, "
                "shrink global typography, or add a large paragraph to make the gap disappear.\n"
            )
        elif has_required_blank_fill:
            overflow_instruction = (
                "- This is a blank_fill_repair. Read repair_context.json.blank_fill.targets before editing.\n"
                "- Patch only the listed required flow_unit_id/section_id/insert_selector targets. Add or move the requested "
                "words_to_add_min..words_to_add_max source-backed words into those exact locations only when prose_fill_required is true.\n"
                "- If a target has over_readout_budget=true, compact_rebalance_required=true, or remaining_safe_words is lower than "
                "words_to_add_min, do not add more prose. Prefer compact comparison table rows, source-grounded bullets, method notes, ablation or limitation notes, local flow or section-height "
                "rebalance, stacked asset/readout flow, or reducing unused section height without changing global columns.\n"
                "- For source_flow_side_lane targets, preserve the current readable source visual size and append "
                "direct sibling readout/native rows inside the same `.figure-flow-unit/.source-flow-unit`.\n"
                "- Allowed filler blocks may be moved into the target flow unit only if their data-block-id and visible "
                "text are preserved. Do not create replacement `_2` ids to bypass the existing DOM.\n"
                "- Fix every required blank-fill target in one pass. Do not solve this by widening images only, changing "
                "`.poster-columns`, changing column row allocation, editing the header, shrinking global typography, or "
                "deleting unrelated sections/source evidence.\n"
            )
        else:
            overflow_instruction = (
                "- This validation includes blank-fill advisory visual references only. Read repair_context.json.advisory_blank_fill "
                "as visual context, not as a required prose-fill contract.\n"
                "- Do not add large prose, change `.poster-columns`, alter global row allocation, edit the header, or reorder sections "
                "solely for advisory blank-fill targets. If you make a change, keep it to tiny local compaction, native rows/chips, "
                "stacking an asset/readout inside the same source-flow unit, or reducing clearly unused local section height.\n"
            )
    elif repair_classification == "reference_style_failure":
        overflow_instruction = (
            "- This is a reference_style_failure. Read repair_context.json.reference_style.issues and the staged "
            "reference_style_contract.json/reference_style_blueprint.html before editing.\n"
            "- Patch every listed reference mismatch in one pass: restore the exact direct-child major-section count per column, "
            "merge extra topics into h3/subsection/inline-label content, and remove only the listed header/section/table/formula rules or frames.\n"
            "- Preserve all target-paper visible text, source ids, data-layer-id bindings, figures, table crops, equations, and evidence. "
            "Do not replace scientific content while repairing visual structure.\n"
            "- Do not restore the normal AutoDesign skin, default top accent rule, default filled section bars, booktabs outer rules, "
            "Times New Roman typography, or default centered header when they conflict with the active reference.\n"
        )
    elif repair_classification == "typography_contract_failure":
        typography_context = (
            repair_context.get("typography")
            if isinstance(repair_context, dict) and isinstance(repair_context.get("typography"), dict)
            else {}
        )
        required_system = (
            typography_context.get("required_system")
            if isinstance(typography_context.get("required_system"), dict)
            else {}
        )
        required_family = str(
            required_system.get("font_family")
            or required_system.get("primary_font_family")
            or '"Times New Roman", Times, Georgia, serif'
        )
        required_summary = str(required_system.get("fixed_css_summary") or "use the fixed role sizes in repair_context.json")
        overflow_instruction = (
            "- This is a typography_contract_failure / academic typography contract failure. Read repair_context.json.typography.metrics "
            "and repair_context.json.typography.targets before editing.\n"
            "- Patch CSS tokens/root typography first: `.paper-poster` or the poster root must use "
            f"`font-family: {required_family}`, then let normal text inherit it.\n"
            "- Use repair_context.json.typography.required_system.fixed_values as the fixed CSS target.\n"
            f"- Restore the active run's typography contract: {required_summary}.\n"
            f"{_EDITORIAL_LEAD_KEY_AUTHORING_CONTRACT}\n"
            "- Remove broad bold body copy and broad italics. Do not fix typography feedback by deleting "
            "paper content, shrinking source figures/tables, or restructuring layout unless a separate "
            "local overflow issue explicitly requires it.\n"
        )
    elif repair_classification == "identity_header_failure":
        overflow_instruction = (
            "- This is an identity_header_failure. Read repair_context.json.identity_header.targets before editing.\n"
            "- The header is limited to exactly these three visible paper-identity rows: paper title, author list, and school/institution/company names.\n"
            "- Place them as three compact text rows only: title line, authors line, school/institution/company line. The school/institution/company line should contain only organization names grounded in the paper, rendered as plain text only; do not invent missing organizations.\n"
            "- Do not add a fourth header/meta/subtitle row or side identity rail. Authored poster headers exclude every other visible item: logos, image badges, icons, QR codes, venue/conference/arXiv/archive metadata, citation/contact text, project/code/resource links, topic badges, method slogans, contribution bullets, benchmark claims, source figures/tables, captions, and explanatory prose.\n"
            "- If the feedback issue is paper_poster_html_unsafe_external_asset_ref, replace remote/data/file/absolute "
            "body evidence image refs with relative staged local source assets. If an unsafe image is in the header, "
            "remove it and keep paper identity as text.\n"
            "- Remove any summary, tagline, thesis, method/result/takeaway readout, and any explanatory "
            "caption under identity labels. Forbid method/topic/contribution/takeaway badges.\n"
        )
    elif repair_classification == "heading_flow_overflow":
        overflow_instruction = (
            "- This is a heading_flow_overflow / header-title lane fit failure. Read "
            "repair_context.json.heading_flow.targets before editing.\n"
            "- Patch only the named identity header, title, or section heading lane. Keep the identity header "
            "identity-only; do not move contribution/body-copy sentences back into the header.\n"
            "- Clear true visible lane overflow by compacting authors/affiliation rows, tightening local padding, "
            "or allocating a little more header/heading height from the body grid. Preserve paper-title scale; reduce "
            "title font-size only as a last resort when repair_context.json shows visible text actually overlaps or clips.\n"
            "- Do not use overflow:hidden/clip to hide clipped heading text, and do not globally shrink body "
            "prose or source figures/tables to satisfy a heading-only issue.\n"
        )
    elif repair_classification == "density_conservation_failure":
        overflow_instruction = (
            "- This is a post-overflow density conservation failure. Read "
            "repair_context.json.density_conservation before editing.\n"
            "- The previous overflow may now be cleared, but the repair hollowed panels or removed source-backed "
            "evidence. Restore the listed source ids/sections/source-flow units from the dense near-miss baseline, "
            "then keep overflow cleared through local row rebalance and concise text.\n"
            "- Do not solve by increasing canvas size, changing template, deleting sections, or leaving oversized "
            "empty wrappers around figures/tables.\n"
        )
    elif repair_classification == "local_repair_scope_violation":
        overflow_instruction = (
            "- The previous repair violated its local repair scope. Read "
            "repair_context.json.local_repair_scope_violation.violations and the embedded "
            "original_repair_context before editing.\n"
            "- Use previous_poster.html as a read-only baseline and apply the original local repair only in poster.html. Preserve non-target "
            "data-block-id, data-source-id, data-layer-id, columns, sections, and source-flow units.\n"
            "- Do not repeat the broad rewrite/delete that caused the scope violation.\n"
        )
    elif repair_classification == "post_composite_design_feedback":
        if feedback_tool == "critic":
            overflow_instruction = (
                "- The vision critic rejected a deterministic-valid poster. Read "
                "repair_context.json.post_composite_feedback.blocking_findings and patch only the named "
                "sections/assets/copy issues in poster.html.\n"
                "- Preserve source ids, data-block-id, data-layer-id, source evidence, and deterministic "
                "preflight validity. Do not use internal planner tools; only edit poster.html and local CSS/HTML.\n"
                "- Keep the current poster structure unless a critic issue explicitly asks for local rebalancing; "
                "address each finding's target and suggested action with the smallest visual/content repair.\n"
            )
        else:
            overflow_instruction = (
                "- Composite/finalize rejected the imported poster with blocking design feedback. Read "
                "repair_context.json.post_composite_feedback.blocking_findings and patch only the named "
                "panels/assets/sections.\n"
                "- Keep the current poster structure and source-backed content; repair blank bands, local "
                "overflow, clipped/undersized assets, or unreadable tables according to each finding's target "
                "and suggested action.\n"
            )
    elif repair_classification == "command_no_output":
        initial_structure = (
            "the staged reference blueprint body-region skeleton"
            if reference_active
            else "academic header, three columns, and source-backed section skeleton"
        )
        overflow_instruction = (
            "- The previous external command exited before poster.html was written. There is no previous_poster.html "
            "to patch; ignore generic diff/baseline wording for this process failure and author a fresh poster.html.\n"
            "- Keep this attempt compact enough for the coding harness/provider to finish. Read author_quick_brief.md, "
            "paper_evidence_packs/*.md, paper_visual_storyboard.json, and poster_content_brief.json; avoid printing "
            "large JSON or long analysis back to the terminal.\n"
            f"- Create poster.html early with the active canvas contract from author_quick_brief.md and {initial_structure}, then "
            "incrementally fill figures/tables/text. Write designer_author_done.json "
            "only after poster.html exists.\n"
            "- Preserve the target contract: standalone HTML, relative local assets only, no markdown-only response, "
            "and no reliance on console output as the deliverable.\n"
        )
    elif repair_classification == "root_wrapper_padding_overflow":
        overflow_instruction = (
            "- This is a root wrapper padding/box-sizing overflow. Patch wrapper CSS only; do not change "
            "poster content density, typography, figures, tables, or section allocation.\n"
            "- Use scoped wrapper sizing from repair_context.json/issues[].deterministic_repair when present: "
            "`.paper-poster > .editorial-poster` should span the root grid with `grid-row:1/-1`, use measured "
            "pixel width/height caps, and set `box-sizing:border-box`. Do not use a blind `height:100%` fix; "
            "inside a grid shell that can collapse the wrapper into the header row.\n"
        )
    elif repair_classification == "micro_overflow" or issue_id == "paper_poster_html_local_flow_overflow":
        overflow_instruction = (
            "- This is a micro local overflow repair only if the reported bottom overflow is <=32px in one "
            "named section. Patch only that section/flow unit from local_repair_hint: trim redundant prose, "
            "tighten local spacing slightly, or move optional secondary content inside the same column. "
            "Keep body type readable and do not rebuild the poster.\n"
            + (
                "- If the listed issue is a near miss, make the smallest local adjustment that clears it. "
                "Do not delete key rows/readout or hollow the section just to remove a small overflow.\n"
                if has_near_miss_issues else
                ""
            )
        )
    scope_instruction = (
        "- Prefer the smallest global reflow that clears all listed row-allocation overflow; partial overflow reduction is failure."
        if repair_classification == "row_allocation_failure" else
        "- Prefer the smallest local edit that clears the gate over broad redesign."
    )
    required_blank_fill_note = ""
    if blank_fill_instruction:
        blank_fill_heading = (
            "Required blank-fill co-repair"
            if has_required_blank_fill and repair_classification != "blank_fill_repair" else
            "Blank-fill repair targets"
            if has_required_blank_fill and repair_classification == "blank_fill_repair" else
            "Advisory blank-fill visual reference"
        )
        required_blank_fill_note = (
            f"\n{blank_fill_heading}:\n"
            f"{blank_fill_instruction}\n"
        )
    if advisory_blank_fill_instruction:
        required_blank_fill_note += (
            "\nAdvisory blank-fill visual reference:\n"
            f"{advisory_blank_fill_instruction}\n"
        )
    attempt_critic_note = ""
    if attempt_critic:
        critic_findings = attempt_critic.get("blocking_findings")
        if not isinstance(critic_findings, list):
            critic_findings = []
        critic_lines = []
        for issue in critic_findings[:4]:
            if not isinstance(issue, dict):
                continue
            target = issue.get("target") if isinstance(issue.get("target"), dict) else {}
            target_label = (
                target.get("section_id")
                or target.get("data_block_id")
                or target.get("selector")
                or issue.get("issue_id")
                or issue.get("id")
                or "poster"
            )
            critic_lines.append(
                f"- {target_label}: {issue.get('description') or issue.get('message') or issue.get('suggested_action')}"
            )
        attempt_critic_note = (
            "\nAttempt-level critic advisory:\n"
            "- A vision critic reviewed the previous attempt preview before this repair attempt. "
            "Fix the deterministic primary_blocking_issue_id first, then use this advisory to improve visual hierarchy, "
            "storytelling, evidence organization, and polish without breaking deterministic validity.\n"
            f"- Verdict: {attempt_critic.get('critic_verdict') or 'unknown'}, "
            f"score: {attempt_critic.get('critic_score') if attempt_critic.get('critic_score') is not None else 'unknown'}.\n"
            f"- Summary: {attempt_critic.get('critic_summary') or 'See repair_context.json.attempt_level_critic_feedback.'}\n"
            f"{chr(10).join(critic_lines) if critic_lines else '- See repair_context.json.attempt_level_critic_feedback.blocking_findings.'}\n"
        )
    attempt_line = (
        f"- This is repair attempt {attempt_index}; AutoDesign will keep validating and retrying until all gates pass, "
        f"subject to a safety budget of {max_attempts} attempts. Clear the current primary_blocking_issue_id completely "
        "in this attempt; do not rely on a later pass for any known required fix.\n"
        if max_attempts > 1 else
        ""
    )
    source_wrap_example = (
        """
For source figure/table wrap failures, every placed body evidence source asset needs its own local flow unit:
<section class="figure-flow-unit" data-source-id="..." data-layer-id="..."> with the visual and nearby source-backed explanatory text in the same DOM flow. Do not place all figures in one media strip separated from text.
"""
        if repair_classification == "source_wrap_failure" else
        ""
    )
    return f"""
Repair attempt:
- The previous poster was rejected by AutoDesign validation.
- Read repair_context.json first when present, then visual_repair_packet.md and visual_repair_packet.json. For row-allocation failures, the global_overflow_repair_plan is the acceptance contract.
- Inspect candidate_preview.png for the full current poster, candidate_validation_overlay.png as the primary gate overlay, and the primary candidate crops listed in visual_repair_packet.json.must_read_images before editing.
- Files listed in visual_repair_packet.json.advisory_images are secondary diagnostics only; files listed in visual_repair_packet.json.reference_images are locked-base/reference images only unless this is density/baseline restoration.
- Fix primary_blocking_issue_id first. Treat secondary diagnostics as advisory next-risk context only unless repair_context.json.required_co_repair.blank_fill or repair_context.json.post_overflow_required_followup.blank_fill marks blank-fill as required. Required blank-fill targets must be cleared in this same attempt without broadening global layout scope. If repair_context.json.blank_fill.active_primary_advisory_repair is true, those targets are active primary local repairs for this attempt, not secondary visual references. Advisory blank-fill targets outside that active plan are visual references only.
- If repair_context.json.attempt_level_critic_feedback exists, treat it as advisory third-party visual review: use it after clearing the deterministic blocker, not instead of clearing the blocker.
- Use repair_context.json for exact selectors/block ids and the visual repair packet for what the failure looks like.
- Patch every blocking target listed in the repair context, not just the first one shown in a local hint.
- A repair that only reduces max overflow but leaves any listed target over the acceptance threshold is still a failed repair.
{attempt_line.rstrip()}
- Use validation_feedback.json only for fallback raw payload details after repair_context.json and the primary visual packet.
- previous_poster.html is a read-only baseline when it exists; write the revised candidate to poster.html only.
- Do not write a byte-identical copy to poster.html while you are still planning the repair. If you need a scratch copy, use a temporary filename and write poster.html only after the local diff is applied.
- The new poster.html must contain a concrete diff from previous_poster.html that addresses the blocking feedback.
{noop_instruction}{overflow_instruction}{required_blank_fill_note}{scope_instruction}
- Write a fresh poster.html and designer_author_done.json in the current directory.
- Preserve all source ids, data-source-id, data-layer-id, data-block-id, and fixed canvas semantics.
- Validation issue_id: {issue_id or "unknown"}
- Repair route: {repair_route or "unknown"}
- Gate hint: {hint or "see validation_feedback.json"}

Global overflow repair plan:
{global_overflow_preview or "Not applicable; see repair_context.json for structured repair targets."}

Previous validation issues:
{issues_preview or "See validation_feedback.json for the full payload."}

Secondary diagnostics that may become the next blockers after this primary repair:
{secondary_preview or "None reported."}

{blank_fill_preview_heading}
{blank_fill_preview or "None."}
{advisory_blank_fill_preview_block}
{attempt_critic_note}

Visual repair packet:
{visual_packet_preview or "No visual_repair_packet.json was staged; rely on text feedback and measurement files."}
{source_wrap_example}
"""


def _copy_candidate_feedback_files(ctx: ToolContext, attempt_dir: Path, feedback: dict[str, Any]) -> list[str]:
    active_feedback = _underlying_feedback_for_visual_repair(feedback)
    summary = active_feedback.get("summary") if isinstance(active_feedback.get("summary"), dict) else {}
    payload = active_feedback.get("payload") if isinstance(active_feedback.get("payload"), dict) else {}
    copied: list[str] = []
    candidate_specs = (
        ("locked_base_candidate_relative_dir", "locked_base"),
        ("candidate_relative_dir", "candidate"),
    )
    for key, prefix in candidate_specs:
        rel = payload.get(key) or summary.get(key)
        if not isinstance(rel, str) or not rel.strip():
            continue
        src_dir = (ctx.run_dir / rel).resolve()
        try:
            src_dir.relative_to(ctx.run_dir.resolve())
        except ValueError:
            continue
        if not src_dir.exists() or not src_dir.is_dir():
            continue
        for src_name, dst_name in (
            ("body.html", f"{prefix}_body.html"),
            ("style.css", f"{prefix}_style.css"),
            ("measurement.json", f"{prefix}_measurement.json"),
            ("preview.png", f"{prefix}_preview.png"),
            ("validation_overlay.png", f"{prefix}_validation_overlay.png"),
            ("visual_repair/packet.json", f"{prefix}_visual_repair_packet.json"),
        ):
            if _copy_run_file(src_dir / src_name, attempt_dir / dst_name):
                copied.append(dst_name)
        for src_subdir, dst_subdir in (
            ("visual_repair/crops", f"visual_repair/{prefix}_crops"),
            ("visual_repair/overlays", f"visual_repair/{prefix}_overlays"),
        ):
            src = src_dir / src_subdir
            dst = attempt_dir / dst_subdir
            if src.exists() and src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
                copied.append(f"{dst_subdir}/")
    return copied


def _stage_attempt_visual_repair_packet(attempt_dir: Path, feedback: dict[str, Any]) -> list[str]:
    active_feedback = _underlying_feedback_for_visual_repair(feedback)
    summary = active_feedback.get("summary") if isinstance(active_feedback.get("summary"), dict) else {}
    payload = active_feedback.get("payload") if isinstance(active_feedback.get("payload"), dict) else {}
    current = _attempt_visual_packet_entry(attempt_dir, "candidate")
    locked = _attempt_visual_packet_entry(attempt_dir, "locked_base")
    if not current and not locked:
        return []
    required_blank_plan = _blank_fill_plan_from_feedback(summary, payload)
    required_blank_targets = _blank_fill_required_targets(required_blank_plan)
    active_primary_advisory_blank_plan = {}
    if not required_blank_targets:
        active_primary_advisory_blank_plan = _active_primary_advisory_blank_fill_plan_from_feedback(summary, payload)
        active_targets = _blank_fill_required_targets(active_primary_advisory_blank_plan)
        if active_targets:
            required_blank_plan = active_primary_advisory_blank_plan
            required_blank_targets = active_targets
    advisory_blank_plan = _advisory_blank_fill_plan_from_feedback(summary, payload)
    if active_primary_advisory_blank_plan:
        active_keys = {
            _blank_fill_prompt_target_key(target)
            for target in active_primary_advisory_blank_plan.get("targets") or []
            if isinstance(target, dict)
        }
        advisory_blank_plan = _blank_fill_plan_without_target_keys(advisory_blank_plan, active_keys)
    active_primary_blank_fill = bool(required_blank_plan.get("active_primary_advisory_repair"))
    blank_fill_instruction = (
        "If blank_fill_plan.active_primary_advisory_repair is true, inspect blank-fill crops as active primary local repair images and apply a minimal local compaction/rebalance/native-row repair; this is not a final validator-required hard gate."
        if active_primary_blank_fill else
        "If blank_fill_plan has required targets, inspect blank_fill crops as required repair images and apply each target's required_repair_mode; add words only when prose_fill_required is true."
    )
    packet = {
        "version": 1,
        "primary_blocking_issue_id": (
            summary.get("primary_blocking_issue_id")
            or payload.get("primary_blocking_issue_id")
            or summary.get("issue_id")
            or payload.get("issue_id")
        ),
        "issue_id": summary.get("issue_id") or payload.get("issue_id"),
        "repair_route": summary.get("repair_route") or payload.get("repair_route"),
        "secondary_diagnostics_are_advisory": True,
        "blank_fill_required": bool(required_blank_targets),
        "active_primary_advisory_repair": active_primary_blank_fill,
        "blank_fill_plan": required_blank_plan if required_blank_targets else {},
        "advisory_blank_fill_plan": advisory_blank_plan if advisory_blank_plan else {},
        "instructions": [
            "Inspect candidate_preview.png for the full current poster.",
            "Inspect candidate_validation_overlay.png as the primary gate overlay.",
            blank_fill_instruction,
            "If advisory_blank_fill_plan has targets, treat them as visual references only; do not add large prose or change global layout for advisory targets.",
            "Inspect visual_repair/*_overlays as secondary diagnostic overlays only.",
            "Fix primary_blocking_issue_id first.",
            "Treat secondary diagnostics as next-risk context only; do not broaden repair scope to chase them.",
            "Use repair_context.json for exact selectors/block ids and this packet for what the failure looks like.",
        ],
        "current_candidate": current,
        "locked_base_candidate": locked,
    }
    image_issue_map: list[dict[str, Any]] = []
    image_issue_map.extend(_attempt_visual_issue_map(attempt_dir, "candidate", current, packet["primary_blocking_issue_id"]))
    image_issue_map.extend(_attempt_visual_issue_map(attempt_dir, "locked_base", locked, packet["primary_blocking_issue_id"]))
    packet["image_issue_map"] = image_issue_map
    packet["must_read_images"] = [
        {
            "path": item.get("path"),
            "role": item.get("role"),
            "issue_id": item.get("issue_id"),
            "stage": item.get("stage"),
            "diagnostic_only": item.get("diagnostic_only", False),
        }
        for item in image_issue_map[:24]
        if item.get("path") and item.get("candidate") == "current_candidate" and not item.get("diagnostic_only")
    ]
    packet["advisory_images"] = [
        {
            "path": item.get("path"),
            "role": item.get("role"),
            "issue_id": item.get("issue_id"),
            "stage": item.get("stage"),
            "diagnostic_only": True,
        }
        for item in image_issue_map[:24]
        if item.get("path") and item.get("candidate") == "current_candidate" and item.get("diagnostic_only")
    ]
    packet["reference_images"] = [
        {
            "path": item.get("path"),
            "role": item.get("role"),
            "issue_id": item.get("issue_id"),
            "stage": item.get("stage"),
            "diagnostic_only": True,
        }
        for item in image_issue_map[:24]
        if item.get("path") and item.get("candidate") == "locked_base_candidate"
    ]
    packet_path = attempt_dir / "visual_repair_packet.json"
    md_path = attempt_dir / "visual_repair_packet.md"
    atomic_write_json(packet_path, packet)
    md_path.write_text(_visual_repair_packet_markdown(packet), encoding="utf-8")
    for target in (feedback, _active_feedback_for_repair(feedback), active_feedback):
        payload_obj = target.setdefault("payload", {}) if isinstance(target, dict) else {}
        if isinstance(payload_obj, dict):
            payload_obj["visual_repair_packet"] = packet
        summary_obj = target.setdefault("summary", {}) if isinstance(target, dict) else {}
        if isinstance(summary_obj, dict):
            summary_obj["visual_repair_packet"] = packet
    return ["visual_repair_packet.json", "visual_repair_packet.md"]


def _attempt_visual_packet_entry(attempt_dir: Path, prefix: str) -> dict[str, Any]:
    files = {
        "preview_png": f"{prefix}_preview.png",
        "measurement_json": f"{prefix}_measurement.json",
        "validation_overlay_png": f"{prefix}_validation_overlay.png",
        "source_packet_json": f"{prefix}_visual_repair_packet.json",
    }
    existing = {key: name for key, name in files.items() if (attempt_dir / name).exists()}
    crops_dir = Path("visual_repair") / f"{prefix}_crops"
    overlays_dir = Path("visual_repair") / f"{prefix}_overlays"
    if (attempt_dir / crops_dir).is_dir():
        existing["crops_dir"] = str(crops_dir)
        existing["crops"] = sorted(str(path.relative_to(attempt_dir)) for path in (attempt_dir / crops_dir).glob("*.png"))
    if (attempt_dir / overlays_dir).is_dir():
        existing["overlays_dir"] = str(overlays_dir)
        existing["overlays"] = sorted(str(path.relative_to(attempt_dir)) for path in (attempt_dir / overlays_dir).glob("*.png"))
    if not existing:
        return {}
    source_packet = _read_json_if_exists(attempt_dir / f"{prefix}_visual_repair_packet.json")
    if source_packet:
        existing["source_packet_summary"] = {
            "primary_issue_id": source_packet.get("primary_issue_id"),
            "validation_stage": source_packet.get("validation_stage"),
            "crop_count": len(source_packet.get("crops") or []),
            "secondary_diagnostic_count": len(source_packet.get("secondary_diagnostics") or []),
        }
    return existing


def _attempt_visual_issue_map(
    attempt_dir: Path,
    prefix: str,
    entry: dict[str, Any],
    primary_blocking_issue_id: Any,
) -> list[dict[str, Any]]:
    if not isinstance(entry, dict) or not entry:
        return []
    candidate_role = "current_candidate" if prefix == "candidate" else "locked_base_candidate"
    source_packet = _read_json_if_exists(attempt_dir / f"{prefix}_visual_repair_packet.json")
    items: list[dict[str, Any]] = []

    def add(path: Any, *, role: str, issue_id: Any = None, stage: Any = None, diagnostic_only: bool = False, **extra: Any) -> None:
        rel = str(path or "").strip()
        if not rel or not (attempt_dir / rel).exists():
            return
        item = {
            "path": rel,
            "candidate": candidate_role,
            "role": role,
            "issue_id": issue_id,
            "stage": stage,
            "diagnostic_only": diagnostic_only,
        }
        item.update({key: value for key, value in extra.items() if value not in (None, "", [], {})})
        items.append(item)

    add(
        entry.get("preview_png"),
        role="candidate_preview" if prefix == "candidate" else "locked_base_preview",
        issue_id=primary_blocking_issue_id if prefix == "candidate" else None,
        stage="full_candidate",
        diagnostic_only=(prefix != "candidate"),
    )
    add(
        entry.get("validation_overlay_png"),
        role="primary_gate_overlay" if prefix == "candidate" else "locked_base_validation_overlay",
        issue_id=primary_blocking_issue_id,
        stage=source_packet.get("validation_stage") or "primary_validation",
        diagnostic_only=(prefix != "candidate"),
    )

    crop_by_name = {
        Path(str(path)).name: str(path)
        for path in entry.get("crops") or []
        if str(path or "").strip()
    }
    for crop in source_packet.get("crops") or []:
        if not isinstance(crop, dict):
            continue
        staged = crop_by_name.get(Path(str(crop.get("relative_path") or crop.get("path") or "")).name)
        if not staged:
            continue
        add(
            staged,
            role=f"{crop.get('role') or 'candidate'}_crop",
            issue_id=crop.get("issue_id"),
            stage=crop.get("stage"),
            diagnostic_only=bool(crop.get("diagnostic_only")) or prefix != "candidate",
            failure_kind=crop.get("failure_kind"),
            target=crop.get("target"),
            diagnostic_geometry=crop.get("diagnostic_geometry"),
            required_targets=crop.get("required_targets"),
        )

    overlay_by_name = {
        Path(str(path)).name: str(path)
        for path in entry.get("overlays") or []
        if str(path or "").strip()
    }
    for overlay in source_packet.get("overlays") or []:
        if not isinstance(overlay, dict):
            continue
        staged = overlay_by_name.get(Path(str(overlay.get("relative_path") or overlay.get("path") or "")).name)
        if not staged:
            continue
        add(
            staged,
            role="secondary_diagnostic_overlay",
            issue_id=overlay.get("issue_id"),
            stage=overlay.get("stage"),
            diagnostic_only=True,
        )
    return items


def _visual_repair_packet_markdown(packet: dict[str, Any]) -> str:
    lines = [
        "# Visual Repair Packet",
        "",
        f"Primary blocker: `{packet.get('primary_blocking_issue_id') or packet.get('issue_id') or 'unknown'}`",
        "",
        "Read this before editing `poster.html`. Use `previous_poster.html` only as a read-only baseline.",
        "Fix the primary blocker first. Secondary diagnostics are advisory next-risk context only.",
        "",
    ]
    image_map = packet.get("image_issue_map") if isinstance(packet.get("image_issue_map"), list) else []
    blank_plan = packet.get("blank_fill_plan") if isinstance(packet.get("blank_fill_plan"), dict) else {}
    blank_targets = blank_plan.get("targets") if isinstance(blank_plan.get("targets"), list) else []
    active_primary_blank_fill = bool(blank_plan.get("active_primary_advisory_repair"))
    advisory_blank_plan = packet.get("advisory_blank_fill_plan") if isinstance(packet.get("advisory_blank_fill_plan"), dict) else {}
    advisory_blank_targets = (
        advisory_blank_plan.get("targets")
        if isinstance(advisory_blank_plan.get("targets"), list) else
        []
    )
    if blank_targets:
        blank_heading = (
            "## Active Primary Blank-Fill Repair Targets"
            if active_primary_blank_fill else
            "## Required Blank-Fill Targets"
        )
        blank_description = (
            "These advisory-grade targets are the current primary blocker; make a minimal local repair attempt before the next validation."
            if active_primary_blank_fill else
            "These are required repair targets, not advisory diagnostics."
        )
        lines.extend([
            blank_heading,
            "",
            blank_description,
            "",
        ])
        for index, target in enumerate(blank_targets[:8], start=1):
            if not isinstance(target, dict):
                continue
            compact_required = bool(target.get("over_readout_budget") or target.get("compact_rebalance_required"))
            repair_mode = target.get("required_repair_mode") or target.get("safe_primary_repair_action") or target.get("primary_repair_action") or ""
            if active_primary_blank_fill:
                action = str(target.get("safe_primary_repair_action") or repair_mode or "compact local blank-fill")
                words = "use local compaction/rebalance/native rows; do not add long prose"
            elif compact_required:
                action = (
                    "compact native rows/chips, rebalance local flow/section height, stack asset/readout, "
                    "or reduce unused local section height"
                )
                words = "do not add long prose"
            else:
                action = str(repair_mode or "")
                words = f"add {target.get('words_to_add_min')}-{target.get('words_to_add_max')} words"
            lines.append(
                "- Target {idx}: `{name}` {words} at `{selector}`; action={action}".format(
                    idx=index,
                    name=target.get("flow_unit_id") or target.get("section_id") or target.get("column_id") or "target",
                    words=words,
                    selector=target.get("insert_selector") or "",
                    action=action,
                )
            )
        lines.append("")
    if advisory_blank_targets:
        lines.extend([
            "## Advisory Blank-Fill Visual References",
            "",
            "These targets are visual references only. Do not add large prose or change global layout for them.",
            "",
        ])
        for index, target in enumerate(advisory_blank_targets[:8], start=1):
            if not isinstance(target, dict):
                continue
            lines.append(
                "- Reference {idx}: `{name}`; advisory only; prefer no edit unless already touching the same local unit.".format(
                    idx=index,
                    name=target.get("flow_unit_id") or target.get("section_id") or target.get("column_id") or "target",
                )
            )
        lines.append("")
    if image_map:
        lines.extend([
            "## Primary Repair Images",
            "",
            "- `candidate_validation_overlay.png` is the primary gate overlay.",
            "- Primary candidate crops are the only crop images that define the current repair scope.",
            "",
        ])
        primary_items = [
            item for item in image_map
            if isinstance(item, dict)
            and item.get("candidate") == "current_candidate"
            and not item.get("diagnostic_only")
        ]
        advisory_items = [
            item for item in image_map
            if isinstance(item, dict)
            and item.get("candidate") == "current_candidate"
            and item.get("diagnostic_only")
        ]
        reference_items = [
            item for item in image_map
            if isinstance(item, dict)
            and item.get("candidate") == "locked_base_candidate"
        ]
        for item in primary_items[:12]:
            if not isinstance(item, dict):
                continue
            lines.append(
                "- `{path}` -> role={role}, issue={issue}, stage={stage}, diagnostic_only={diag}".format(
                    path=item.get("path") or "",
                    role=item.get("role") or "",
                    issue=item.get("issue_id") or "",
                    stage=item.get("stage") or "",
                    diag=bool(item.get("diagnostic_only")),
                )
            )
        lines.append("")
        if advisory_items:
            lines.extend([
                "## Advisory / Next-Risk Images",
                "",
                "- These secondary diagnostics do not broaden the primary repair scope.",
                "",
            ])
            for item in advisory_items[:8]:
                lines.append(
                    "- `{path}` -> role={role}, issue={issue}, stage={stage}, diagnostic_only=True".format(
                        path=item.get("path") or "",
                        role=item.get("role") or "",
                        issue=item.get("issue_id") or "",
                        stage=item.get("stage") or "",
                    )
                )
            lines.append("")
        if reference_items:
            lines.extend([
                "## Reference Images",
                "",
                "- Locked-base images are reference only unless repair_context.json says this is a density/baseline restoration.",
                "",
            ])
            for item in reference_items[:8]:
                lines.append(
                    "- `{path}` -> role={role}, issue={issue}, stage={stage}, diagnostic_only=True".format(
                        path=item.get("path") or "",
                        role=item.get("role") or "",
                        issue=item.get("issue_id") or "",
                        stage=item.get("stage") or "",
                    )
                )
            lines.append("")
    for label, entry in (
        ("Current candidate", packet.get("current_candidate")),
        ("Locked base candidate", packet.get("locked_base_candidate")),
    ):
        if not isinstance(entry, dict) or not entry:
            continue
        lines.extend([f"## {label}", ""])
        for key in ("preview_png", "validation_overlay_png", "measurement_json", "source_packet_json", "crops_dir", "overlays_dir"):
            if entry.get(key):
                lines.append(f"- {key}: `{entry[key]}`")
        crops = entry.get("crops") if isinstance(entry.get("crops"), list) else []
        if crops:
            lines.append("- crops:")
            lines.extend(f"  - `{crop}`" for crop in crops[:12])
        overlays = entry.get("overlays") if isinstance(entry.get("overlays"), list) else []
        if overlays:
            lines.append("- overlays:")
            lines.extend(f"  - `{overlay}`" for overlay in overlays[:8])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _author_must_read_first(repair_inputs: list[str], copied: list[str]) -> list[str]:
    reference_inputs = [
        "reference_style_contract.json",
        "reference_style_blueprint.html",
        "reference_poster/reference.png",
    ]
    repair_priority = [
        "repair_context.json",
        "visual_repair_packet.md",
        "visual_repair_packet.json",
        "candidate_validation_overlay.png",
        "candidate_preview.png",
        "candidate_measurement.json",
        "validation_feedback.json",
    ]
    ordered = [
        "runtime_skills/index.md",
        *(repair_priority if repair_inputs else ["author_quick_brief.md", *reference_inputs]),
        *(["author_quick_brief.md", *reference_inputs] if repair_inputs else repair_priority),
        "poster_content_brief.json",
        "poster_plan_contract.json",
        "paper_visual_provenance.json",
        "paper_visual_storyboard.json",
    ]
    available = set(repair_inputs) | set(copied)
    return [name for name in ordered if name in available][:16]


def _author_must_read_first_images(attempt_dir: Path) -> list[dict[str, Any]]:
    packet = _read_json_if_exists(attempt_dir / "visual_repair_packet.json")
    images = packet.get("must_read_images") if isinstance(packet.get("must_read_images"), list) else []
    result: list[dict[str, Any]] = []
    for item in images:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path") or "").strip()
        if not rel or not (attempt_dir / rel).exists():
            continue
        result.append({
            "path": rel,
            "role": item.get("role"),
            "issue_id": item.get("issue_id"),
            "stage": item.get("stage"),
            "diagnostic_only": bool(item.get("diagnostic_only")),
        })
    return result[:24]


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return stat.st_size, stat.st_mtime_ns


def _write_process_log(
    attempt_dir: Path,
    *,
    cmd: list[str],
    returncode: int | None,
    stdout_path: Path,
    stderr_path: Path,
    reason: str,
    timeout: bool = False,
    timeout_s: int | None = None,
    elapsed_s: float = 0.0,
    poster_sha256: str = "",
    error: str = "",
    sensitive_values: Sequence[str] = (),
) -> None:
    payload = {
        "cmd": [_redact_process_log_text(part, sensitive_values) for part in cmd],
        "returncode": returncode,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "stdout": _redact_process_log_text(_read_text(stdout_path), sensitive_values),
        "stderr": _redact_process_log_text(_read_text(stderr_path), sensitive_values),
        "reason": reason,
        "timeout": timeout,
        "timeout_s": timeout_s,
        "elapsed_s": elapsed_s,
        "poster_sha256": poster_sha256,
    }
    if error:
        payload["error"] = error
    atomic_write_json(attempt_dir / "designer_author_log.json", payload)


def _redact_process_log_text(value: Any, sensitive_values: Sequence[str]) -> str:
    text = str(value or "")
    for secret in sensitive_values:
        text = text.replace(secret, "[REDACTED]")
    return text


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _tail_text_excerpt(path: Path, *, limit: int) -> str:
    text = _read_text(path).strip()
    if not text:
        return ""
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


def _json_summary_excerpt(path: Path, *, limit: int = 900) -> str:
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return _tail_text_excerpt(path, limit=limit)
    if isinstance(payload, dict):
        for key in ("summary", "message", "notes", "status", "changes"):
            value = payload.get(key)
            if value:
                return _truncate_compact_text(value, limit)
    return _truncate_compact_text(payload, limit)


def _truncate_compact_text(value: Any, limit: int) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            text = str(value)
    text = re.sub(r"\s+\n", "\n", text).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"
