"""Independent local coding-agent author for paper-to-slides HTML."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup, Tag

from ..config import authoring_max_attempts_for, harness_subprocess_env
from ..designer import invoke_designer_tool
from ..schema import AttemptCandidate, CompositionArtifacts
from ..tools import ToolContext
from ..tools._deck_preview import build_deck_preview_grid
from ..util.artifact_palette_validation import validate_artifact_palette
from ..util.artifact_browser_audit import audit_slides_html
from ..util.browser_render import screenshot_deck_slides
from ..util.deck_planner import parse_explicit_slide_count
from ..util.editable_html import (
    ensure_editable_html_contract,
    find_deck_artifact_root,
)
from ..util.io import atomic_write_json, sha256_file
from ..util.logging import log
from ..attempt_candidates import (
    assert_promotion_run_unchanged,
    capture_attempt_candidate,
    is_browser_preview_resource_path,
)
from ..candidate_assessment import assess_delivery_issues
from ..attempt_selection import (
    assert_promotion_allowed,
    leased_promotion_tool_context,
    normal_promotion_lease,
    promote_pending_selection,
    ranked_delivery_candidates,
)
from ..util.slides_visual_plan import (
    build_slides_asset_catalog,
    build_slides_visual_plan,
)
from .external_author_process import (
    ExternalAuthorProcessRequest,
    context_attempt_selection_callback,
    context_cancellation_callback,
    context_cancellation_checkpoint,
    context_cancellation_token,
    run_external_author_process,
)
from .atomic_artifact_promotion import (
    publish_artifact_directory,
    recover_artifact_promotion,
)


_STATE_INPUTS = {
    "deck_plan.json": "deck_plan",
    "paper_memory.json": "paper_memory",
    "paper_memory_dossier.json": "paper_memory_dossier",
    "paper_visual_provenance.json": "paper_visual_provenance",
}
_RUN_INPUTS = (
    "deck_plan.json",
    "paper_memory.json",
    "paper_memory.md",
    "paper_memory_dossier.json",
    "paper_memory_dossier.md",
    "paper_visual_provenance.json",
)
_RESOURCE_ATTRIBUTES = {
    "audio": ("src",),
    "embed": ("src",),
    "iframe": ("src",),
    "img": ("src", "srcset"),
    "input": ("src",),
    "link": ("href",),
    "object": ("data",),
    "script": ("src",),
    "source": ("src", "srcset"),
    "track": ("src",),
    "video": ("src", "poster"),
}


class ExternalSlidesAuthor:
    """Run a local coding-agent subprocess as a raw HTML slides author."""

    def __init__(self, settings: Any, system_prompt: str):
        self.settings = settings
        self.system_prompt = system_prompt
        self._total_in = 0
        self._total_out = 0
        self._total_cache_read = 0
        self._total_cache_create = 0

    @property
    def token_totals(self) -> tuple[int, int]:
        return self._total_in, self._total_out

    @property
    def cache_totals(self) -> tuple[int, int]:
        return self._total_cache_read, self._total_cache_create

    def run(self, brief: str, ctx: ToolContext) -> None:
        context_cancellation_checkpoint(ctx, "external_slides.before_start")
        if promote_pending_selection(ctx) != "none":
            return
        ctx.state.pop("designer_api_error", None)
        ctx.state["artifact_type"] = "deck"
        command = str(getattr(self.settings, "designer_author_cmd", "") or "").strip()
        author_root = ctx.run_dir / "slides_author"
        context_cancellation_checkpoint(ctx, "external_slides.before_author_dir")
        author_root.mkdir(parents=True, exist_ok=True)
        context_cancellation_checkpoint(ctx, "external_slides.after_author_dir")
        try:
            _recover_interrupted_promotion(ctx.run_dir / "final")
        except (OSError, ValueError) as exc:
            self._fail(
                ctx,
                "slides_author_promotion_recovery_failed",
                f"interrupted slides promotion could not be recovered: {exc}",
                author_root,
            )
            return
        if not command:
            self._fail(
                ctx,
                "missing_designer_author_cmd",
                "external slides author requires a configured local coding-agent command",
                author_root,
            )
            return
        if not self._ensure_ingested(ctx):
            return
        provenance = ctx.state.get("paper_visual_provenance")
        paper_memory = ctx.state.get("paper_memory")
        if (
            not isinstance(provenance, dict)
            or not isinstance(paper_memory, dict)
            or not Path(ctx.layers_dir).is_dir()
        ):
            self._fail(
                ctx,
                "slides_author_missing_ingest_context",
                "external slides author requires paper_memory, paper_visual_provenance, "
                "and the ingested layers directory",
                author_root,
            )
            return

        expected_slide_count = _expected_slide_count(brief, ctx)
        max_attempts = authoring_max_attempts_for(self.settings, "deck")
        prior_attempts = int(ctx.state.get("slides_author_attempts") or 0)
        absolute_attempt_budget = prior_attempts + max_attempts
        catalog = build_slides_asset_catalog(
            provenance,
            rendered_layers=ctx.state.get("rendered_layers"),
        )
        visual_plan = build_slides_visual_plan(
            provenance,
            rendered_layers=ctx.state.get("rendered_layers"),
            expected_slide_count=expected_slide_count,
            color_system=_current_slides_color_system(ctx),
            deck_plan=(
                ctx.state.get("deck_plan")
                if isinstance(ctx.state.get("deck_plan"), dict)
                else None
            ),
        )
        try:
            trusted_source_hashes = _trusted_slides_source_hashes(
                ctx,
                catalog,
                require_existing=isinstance(
                    ctx.state.get("external_author_resume"), dict
                ),
            )
        except ValueError as exc:
            self._fail(
                ctx,
                "slides_author_trusted_source_anchor_failed",
                str(exc),
                author_root,
            )
            return
        resume = ctx.state.pop("external_author_resume", None)
        resume = resume if isinstance(resume, dict) else {}
        previous_attempt_value = str(resume.get("previous_attempt_dir") or "").strip()
        previous_attempt_dir = Path(previous_attempt_value) if previous_attempt_value else None
        previous_validation = (
            resume.get("repair_feedback")
            if isinstance(resume.get("repair_feedback"), dict)
            else None
        )
        if resume:
            log(
                "slides_author.resume",
                mode="external",
                source_run_dir=resume.get("source_run_dir"),
                prior_attempts=int(resume.get("prior_attempts") or 0),
                repair_seed=previous_validation is not None,
            )
        for loop_index in range(1, max_attempts + 1):
            context_cancellation_checkpoint(ctx, "external_slides.before_attempt")
            if promote_pending_selection(ctx) != "none":
                return
            context_cancellation_checkpoint(ctx, "external_slides.after_selection_check")
            attempt_dir = self._next_attempt_dir(ctx)
            log(
                "slides_author.attempt_start",
                mode="external",
                attempt=int(ctx.state.get("slides_author_attempts") or loop_index),
                max_attempts=absolute_attempt_budget,
            )
            context_cancellation_checkpoint(ctx, "external_slides.before_staging")
            try:
                self._stage_inputs(
                    ctx,
                    brief=brief,
                    attempt_dir=attempt_dir,
                    expected_slide_count=expected_slide_count,
                    catalog=catalog,
                    visual_plan=visual_plan,
                    previous_attempt_dir=previous_attempt_dir,
                    previous_validation=previous_validation,
                )
            except (OSError, ValueError) as exc:
                self._fail(ctx, "slides_author_staging_failed", str(exc), attempt_dir)
                return
            context_cancellation_checkpoint(ctx, "external_slides.after_staging")

            prompt = self._build_prompt(
                brief=brief,
                attempt_dir=attempt_dir,
                expected_slide_count=expected_slide_count,
                has_dossier=(attempt_dir / "paper_memory_dossier.json").is_file(),
                has_runtime_skills=(attempt_dir / "runtime_skills" / "index.md").is_file(),
                repair=previous_validation is not None,
            )
            context_cancellation_checkpoint(ctx, "external_slides.before_prompt_write")
            (attempt_dir / "slides_author_prompt.md").write_text(prompt, encoding="utf-8")
            context_cancellation_checkpoint(ctx, "external_slides.after_prompt_write")
            invocation = self._invoke(
                command,
                prompt=prompt,
                attempt_dir=attempt_dir,
                run_id=ctx.run_id,
                attempt=int(
                    ctx.state.get("slides_author_attempts") or loop_index
                ),
                ctx=ctx,
            )
            context_cancellation_checkpoint(ctx, "external_slides.after_author_process")
            ctx.state["slides_author_invocation"] = invocation
            if invocation["status"] == "selected":
                promote_pending_selection(ctx)
                return
            if invocation["status"] != "ok":
                process_validation = _validation_report(
                    expected_slide_count,
                    0,
                    [_issue(str(invocation["reason"]), str(invocation.get("message") or "external slides author command failed"))],
                    0,
                    0,
                    [],
                    [],
                )
                context_cancellation_checkpoint(ctx, "external_slides.before_process_validation_write")
                atomic_write_json(attempt_dir / "slides_validation.json", process_validation)
                context_cancellation_checkpoint(ctx, "external_slides.after_process_validation_write")
                if loop_index < max_attempts:
                    previous_attempt_dir = attempt_dir
                    previous_validation = process_validation
                    context_cancellation_checkpoint(ctx, "external_slides.before_process_retry")
                    log(
                        "slides_author.process_retry",
                        attempt=loop_index,
                        next_attempt=loop_index + 1,
                        reason=invocation["reason"],
                    )
                    continue
                if self._try_promote_best_available_candidate(ctx):
                    return
                self._fail(
                    ctx,
                    str(invocation["reason"]),
                    str(invocation.get("message") or "external slides author command failed"),
                    attempt_dir,
                )
                return

            validation = _validate_slides(
                attempt_dir / "slides.html",
                attempt_dir=attempt_dir,
                expected_slide_count=expected_slide_count,
                visual_plan=visual_plan,
                catalog=catalog,
                trusted_source_hashes=trusted_source_hashes,
            )
            context_cancellation_checkpoint(ctx, "external_slides.after_validation")
            if validation["status"] == "ok":
                context_cancellation_checkpoint(ctx, "external_slides.before_browser_audit")
                browser_audit = audit_slides_html(
                    attempt_dir / "slides.html",
                    required_source_ids=validation.get("source_visual_ids") or [],
                    expected_slide_count=expected_slide_count,
                )
                context_cancellation_checkpoint(ctx, "external_slides.after_browser_audit")
                context_cancellation_checkpoint(ctx, "external_slides.before_browser_qa_write")
                atomic_write_json(attempt_dir / "slides_browser_qa.json", browser_audit)
                context_cancellation_checkpoint(ctx, "external_slides.after_browser_qa_write")
                validation = _merge_slides_browser_audit(validation, browser_audit)
            context_cancellation_checkpoint(ctx, "external_slides.before_validation_write")
            atomic_write_json(attempt_dir / "slides_validation.json", validation)
            context_cancellation_checkpoint(ctx, "external_slides.after_validation_write")
            candidate = capture_slides_attempt_candidate(
                ctx=ctx,
                attempt_dir=attempt_dir,
                attempt=int(ctx.state.get("slides_author_attempts") or loop_index),
                max_attempts=absolute_attempt_budget,
                validation=validation,
            )
            if promote_pending_selection(ctx) != "none":
                return
            if validation["status"] == "ok":
                context_cancellation_checkpoint(ctx, "external_slides.before_promotion")
                try:
                    self._promote(
                        ctx,
                        attempt_dir=attempt_dir,
                        expected_slide_count=expected_slide_count,
                        validation=validation,
                        candidate_id=candidate.candidate_id,
                    )
                except Exception as exc:
                    self._fail(
                        ctx,
                        "slides_author_promotion_failed",
                        f"accepted slides could not be rendered and promoted: {type(exc).__name__}: {exc}",
                        attempt_dir,
                    )
                return
            if _browser_audit_unavailable(validation):
                self._fail(
                    ctx,
                    "slides_browser_audit_unavailable",
                    "accepted slides HTML could not be verified in the local browser runtime",
                    attempt_dir,
                    payload=validation,
                )
                return
            if loop_index < max_attempts:
                previous_attempt_dir = attempt_dir
                previous_validation = validation
                context_cancellation_checkpoint(ctx, "external_slides.before_validation_retry")
                log(
                    "slides_author.validation_retry",
                    attempt=loop_index,
                    next_attempt=loop_index + 1,
                    issue_ids=[issue["id"] for issue in validation["issues"]],
                )
                continue
            if self._try_promote_best_available_candidate(ctx):
                return
            self._fail(
                ctx,
                "slides_author_validation_failed",
                "; ".join(issue["message"] for issue in validation["issues"]),
                attempt_dir,
                payload=validation,
            )
            return

    def _try_promote_best_available_candidate(self, ctx: ToolContext) -> bool:
        context_cancellation_checkpoint(ctx, "external_slides.fallback.start")
        if promote_pending_selection(ctx) != "none":
            return True
        candidates = ranked_delivery_candidates(ctx.run_dir, artifact_type="deck")
        if not candidates:
            return False

        for candidate in candidates:
            source_html = ctx.run_dir / candidate.source_relative_path
            attempt_dir = source_html.parent
            visual_plan = json.loads(
                (attempt_dir / "slides_visual_plan.json").read_text(encoding="utf-8")
            )
            catalog = json.loads(
                (attempt_dir / "slides_asset_catalog.json").read_text(encoding="utf-8")
            )
            prior_validation = json.loads(
                (ctx.run_dir / candidate.validation_summary_relative_path).read_text(
                    encoding="utf-8"
                )
            )
            if not all(
                isinstance(payload, dict)
                for payload in (visual_plan, catalog, prior_validation)
            ):
                continue
            expected_slide_count = int(
                prior_validation.get("expected_slide_count") or 1
            )
            trusted_hashes = _trusted_slides_source_hashes(
                ctx,
                catalog,
                require_existing=True,
            )
            validation = _validate_slides(
                source_html,
                attempt_dir=attempt_dir,
                expected_slide_count=expected_slide_count,
                visual_plan=visual_plan,
                catalog=catalog,
                trusted_source_hashes=trusted_hashes,
            )
            context_cancellation_checkpoint(ctx, "external_slides.fallback.after_static")
            browser_audit = audit_slides_html(
                source_html,
                required_source_ids=validation.get("source_visual_ids") or [],
                expected_slide_count=expected_slide_count,
            )
            validation = _merge_slides_browser_audit(validation, browser_audit)
            assessment = assess_delivery_issues(
                "deck",
                [
                    issue
                    for issue in validation.get("issues") or []
                    if isinstance(issue, dict)
                ],
            )
            if assessment.safety_state == "blocked":
                rejection = {
                    "schema_version": 1,
                    "artifact_type": "deck",
                    "candidate_id": candidate.candidate_id,
                    "source_relative_path": candidate.source_relative_path,
                    "safety_state": assessment.safety_state,
                    "hard_blockers": [
                        issue.model_dump(mode="json")
                        for issue in assessment.hard_blockers
                    ],
                    "quality_diagnostics": [
                        issue.model_dump(mode="json")
                        for issue in assessment.quality_diagnostics
                    ],
                    "fresh_validation": validation,
                }
                context_cancellation_checkpoint(
                    ctx,
                    "external_slides.fallback.before_rejection_write",
                )
                atomic_write_json(
                    ctx.run_dir / "slides_best_available_rejected.json",
                    rejection,
                )
                ctx.state["slides_best_available_rejected"] = rejection
                log(
                    "slides_author.best_available_rejected",
                    candidate_id=candidate.candidate_id,
                    hard_blocker_ids=[
                        issue.issue_id for issue in assessment.hard_blockers
                    ],
                )
                continue
            validation["delivery_assessment"] = {
                "safety_state": assessment.safety_state,
                "quality_diagnostics": [
                    item.model_dump(mode="json")
                    for item in assessment.quality_diagnostics
                ],
            }
            self._promote(
                ctx,
                attempt_dir=attempt_dir,
                expected_slide_count=expected_slide_count,
                validation=validation,
                browser_audit=browser_audit,
                candidate_id=candidate.candidate_id,
                acceptance_path="best_available_artifact_fallback",
            )
            return True
        return False

    def _ensure_ingested(self, ctx: ToolContext) -> bool:
        if _has_shared_slides_evidence(ctx):
            return True
        switch_result = invoke_designer_tool(
            "switch_artifact_type",
            {"type": "deck"},
            ctx,
        )
        if switch_result.status == "error":
            self._fail(
                ctx,
                "slides_author_switch_artifact_type_error",
                switch_result.error_message or "switch_artifact_type failed",
                ctx.run_dir / "slides_author",
                payload=switch_result.payload,
            )
            return False
        attachments = [str(path) for path in (ctx.state.get("attachments") or [])]
        if not attachments and not ctx.state.get("reuse_ingest_run"):
            self._fail(
                ctx,
                "slides_author_missing_ingest_input",
                "external slides author requires PDF attachments or a reused ingest run",
                ctx.run_dir / "slides_author",
            )
            return False
        ingest_result = invoke_designer_tool(
            "ingest_document",
            {"file_paths": attachments},
            ctx,
        )
        if ingest_result.status == "error":
            self._fail(
                ctx,
                "slides_author_ingest_document_error",
                ingest_result.error_message or "ingest_document failed",
                ctx.run_dir / "slides_author",
                payload=ingest_result.payload,
            )
            return False
        if not _has_shared_slides_evidence(ctx):
            self._fail(
                ctx,
                "slides_author_missing_ingest_context",
                "ingest_document completed without paper_memory, paper_visual_provenance, and layers",
                ctx.run_dir / "slides_author",
            )
            return False
        return True

    def _next_attempt_dir(self, ctx: ToolContext) -> Path:
        attempt = int(ctx.state.get("slides_author_attempts") or 0) + 1
        ctx.state["slides_author_attempts"] = attempt
        attempt_dir = ctx.run_dir / "slides_author" / f"attempt_{attempt:02d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        return attempt_dir

    def _stage_inputs(
        self,
        ctx: ToolContext,
        *,
        brief: str,
        attempt_dir: Path,
        expected_slide_count: int,
        catalog: dict[str, Any],
        visual_plan: dict[str, Any],
        previous_attempt_dir: Path | None,
        previous_validation: dict[str, Any] | None,
    ) -> None:
        copied: list[str] = []
        for name in _RUN_INPUTS:
            source = ctx.run_dir / name
            if source.is_file():
                shutil.copy2(source, attempt_dir / name)
                copied.append(name)
        for name, state_key in _STATE_INPUTS.items():
            target = attempt_dir / name
            value = ctx.state.get(state_key)
            if name == "deck_plan.json" and isinstance(value, dict):
                atomic_write_json(target, value)
                copied.append(name)
            elif not target.exists() and isinstance(value, dict):
                atomic_write_json(target, value)
                copied.append(name)

        evidence_packs = ctx.run_dir / "paper_evidence_packs"
        if evidence_packs.is_dir():
            shutil.copytree(
                evidence_packs,
                attempt_dir / "paper_evidence_packs",
                dirs_exist_ok=True,
            )
            copied.append("paper_evidence_packs/")

        runtime_skills = _stage_runtime_skills(
            ctx,
            attempt_dir,
            stage="repair" if previous_validation is not None else "plan",
        )
        runtime_skill_files = list(runtime_skills.get("files") or [])
        if runtime_skill_files:
            copied.extend(runtime_skill_files)

        _stage_layers(ctx, attempt_dir, catalog)
        _sync_visual_plan_paths(visual_plan, catalog)
        atomic_write_json(attempt_dir / "slides_asset_catalog.json", catalog)
        atomic_write_json(attempt_dir / "slides_visual_plan.json", visual_plan)
        repair_inputs: list[str] = []
        if previous_attempt_dir is not None and previous_validation is not None:
            previous_html = previous_attempt_dir / "slides.html"
            if previous_html.is_file():
                shutil.copy2(previous_html, attempt_dir / "previous_slides.html")
                repair_inputs.append("previous_slides.html")
            atomic_write_json(
                attempt_dir / "previous_validation_findings.json",
                _compact_validation_findings(previous_validation),
            )
            repair_inputs.append("previous_validation_findings.json")
        has_dossier = (attempt_dir / "paper_memory_dossier.json").is_file()
        active_deck_plan = (
            ctx.state.get("deck_plan")
            if isinstance(ctx.state.get("deck_plan"), dict)
            else {}
        )
        manifest = {
            "kind": "external_slides_author_input",
            "version": 2,
            "run_id": ctx.run_id,
            "brief": brief,
            "expected_slide_count": expected_slide_count,
            "deck_plan": {
                "path": "deck_plan.json",
                "talk_profile": str(active_deck_plan.get("talk_profile") or ""),
                "outline_items": len(active_deck_plan.get("outline") or []),
            },
            "output_contract": {
                "slides_html": "slides.html",
                "done_marker": "designer_author_done.json",
            },
            "must_read_first": [
                *(["runtime_skills/index.md"] if runtime_skill_files else []),
                *(["deck_plan.json"] if (attempt_dir / "deck_plan.json").is_file() else []),
                "slides_visual_plan.json",
                *(["paper_memory_dossier.json"] if has_dossier else ["paper_memory.json"]),
                *repair_inputs,
            ],
            "progressive_disclosure": {
                "full_asset_catalog": "slides_asset_catalog.json",
                "full_visual_provenance": "paper_visual_provenance.json",
                "full_paper_memory": "paper_memory.json",
                "source_layers": "layers/",
            },
            "visual_targets": dict(visual_plan.get("targets") or {}),
            "evidence_coverage": dict(visual_plan.get("evidence_coverage") or {}),
            "repair_inputs": repair_inputs,
            "runtime_skills": runtime_skill_files,
            "staged_inputs": sorted(set(copied + [
                "slides_visual_plan.json",
                "slides_asset_catalog.json",
                "layers/",
            ])),
        }
        atomic_write_json(attempt_dir / "author_input_manifest.json", manifest)

    def _build_prompt(
        self,
        *,
        brief: str,
        attempt_dir: Path,
        expected_slide_count: int,
        has_dossier: bool,
        has_runtime_skills: bool,
        repair: bool,
    ) -> str:
        evidence_instruction = (
            "Read paper_memory_dossier.json for the method/results narrative; use "
            "paper_memory.json and paper_evidence_packs/ only when more evidence is needed."
            if has_dossier
            else "No dossier is available. Build the narrative from paper_memory.json and "
            "paper_evidence_packs/ while keeping every claim source-grounded."
        )
        baseline_instruction = (
            "- Patch previous_slides.html first; do not restart the deck from scratch.\n"
            if (attempt_dir / "previous_slides.html").is_file()
            else "- No usable previous_slides.html baseline exists; rebuild slides.html from the staged evidence while correcting the process failure.\n"
        )
        repair_instruction = (
            "\nRepair contract:\n"
            f"{baseline_instruction}"
            "- Read previous_validation_findings.json and fix every listed deterministic issue.\n"
            "- Save the patched result as slides.html, then re-check the complete output contract.\n"
            if repair
            else ""
        )
        runtime_skill_instruction = (
            "Read runtime_skills/index.md first, then open only the active-stage skill and "
            "resource files listed there before editing.\n"
            if has_runtime_skills
            else ""
        )
        return f"""You are the local coding-agent author for an HTML-first academic paper deck.
Work only inside this directory: {attempt_dir}

User brief:
{brief}

Execution contract:
- Create slides.html and designer_author_done.json directly; a prose-only response is failure.
- Author exactly {expected_slide_count} slides. Each slide must be a .deck-slide element with a unique id, data-slide-role, and data-section.
- Wrap every .deck-slide in one non-body/html Deck root, preferably `<main id="deck" data-slide-count="{expected_slide_count}" data-autodesign-artifact-root="deck">`. Never place slides directly under body.
- Use one 16:9 viewport per slide at 1920x1080 CSS pixels.
- Implement keyboard navigation with ArrowLeft and ArrowRight plus stable hash/deep-link state, with no visible playback or slide-navigation controls.
- Keep content editable with native HTML text and tables. Do not rasterize slide text.
- Use original paper visuals from layers/ and identify each placement with data-source-id from slides_asset_catalog.json.
- Follow the role-specific word ranges in slides_visual_plan.json. The cover and closing may be concise; every other slide must remain above the static 30-word hard floor. Method/algorithm and results slides may carry more evidence-backed content than outline or transition slides.
- Put hidden presenter guidance in each slide's data-speaker-notes attribute using `[Sources] ... [Talk] ...`; never render those notes in the visible slide.
- Treat an eligible original paper visual, native HTML table, verifiable equation, or editable mechanism diagram as a visual evidence unit. Mark tables, equations, and diagrams with data-visual-unit plus data-evidence-ref, and keep their text and labels editable.
- Meet the three independent targets in slides_visual_plan.json: unique source visuals, source visual placements, and slides containing at least one visual evidence unit.
- A source visual may appear only once on a slide. Respect source_visual_reuse_cap, and give every placement a local, placement-specific interpretation in figcaption or .evidence-readout rather than repeating the same caption.
- Optional unmatched reserves listed in slides_visual_plan.json are shortfall-only supporting visuals; use at most two and never ground method or results claims in them.
- Use no remote assets, remote fonts, external stylesheets, iframes, embeds, network calls, or absolute/file URLs.
- Keep every local dependency inside this attempt directory. Do not use parent-directory paths.

{runtime_skill_instruction}Read author_input_manifest.json, deck_plan.json when present, and slides_visual_plan.json first. Follow the planned slide order, chapter, communication job, assertion title, scope, layout family, evidence refs, and speaker-note intent. Then inspect slides_asset_catalog.json for the full eligible corpus. {evidence_instruction} paper_visual_provenance.json and layers/ are the source of truth for original paper visuals.

Treat deck_plan.json as the narrative contract. Use slides_visual_plan.json.storyboard as its compact authoring view. Include experiment setup/evaluation protocol only when the paper supports the relevant datasets, tasks, baselines, or metrics. If an outline slot is unsupported, replace it with another source-backed point while preserving the requested count and chapter progression. State the thesis once, develop the mechanism once, separate setup from results, and reserve the closing for non-redundant takeaways. Use assertion-led titles where the paper supports a precise claim; use neutral titles for cover, outline, and chapter checkpoints.

Build a formal academic visual system: white or near-white canvas, near-black ink, neutral gray thin rules, and one restrained accent selected from the supplied color_system. Preserve palette_id and exact --poster-* compatibility variables when supplied, but do not spread every palette role across slide surfaces. Use a serif main hierarchy with sans-serif small labels, flat editorial composition, generous evidence areas, and varied layout families. Avoid gradients, dark presentation themes, card grids, nested panels, decorative chrome, and oversized sparse slogans.

The deck is visual-first: meet all independent targets in slides_visual_plan.json, include both method and results evidence when those roles are available, and prefer a real paper figure/table or an editable evidence unit over decorative imagery. Keep source ids and evidence refs in invisible metadata even when the visible slide is minimal.
{repair_instruction}

When slides.html is complete, write designer_author_done.json with a short JSON status and exit.
"""

    def _invoke(
        self,
        command: str,
        *,
        prompt: str,
        attempt_dir: Path,
        run_id: str = "",
        attempt: int = 0,
        ctx: ToolContext | None = None,
    ) -> dict[str, Any]:
        checkpoint = lambda phase: context_cancellation_checkpoint(ctx, phase)
        checkpoint("external_slides.process.before_command_parse")
        try:
            cmd = shlex.split(command)
        except ValueError as exc:
            return {
                "status": "error",
                "reason": "slides_author_command_parse_error",
                "message": str(exc),
            }
        if not cmd:
            return {
                "status": "error",
                "reason": "slides_author_empty_command",
                "message": "configured command is empty",
            }
        timeout_s = max(1, int(getattr(self.settings, "designer_author_timeout_s", 1800) or 1800))
        env = harness_subprocess_env(
            os.environ,
            harness=str(getattr(self.settings, "designer_author_harness", "") or ""),
            api_key=getattr(self.settings, "harness_api_key", None),
        )
        env.setdefault("AUTODESIGN_AUTHOR_PYTHON", sys.executable)
        harness = str(getattr(self.settings, "designer_author_harness", "") or "")
        model = str(getattr(self.settings, "designer_author_model", "") or "")
        sensitive_values = _process_sensitive_values(
            cmd,
            getattr(self.settings, "harness_api_key", None),
            env,
        )
        raw_stdout_path = attempt_dir / ".slides_author_stdout.tmp"
        raw_stderr_path = attempt_dir / ".slides_author_stderr.tmp"
        result = run_external_author_process(
            ExternalAuthorProcessRequest(
                run_id=run_id or f"slides:{attempt_dir.resolve()}",
                attempt=attempt,
                command=cmd,
                cwd=attempt_dir,
                prompt=prompt,
                timeout_s=timeout_s,
                stdout_path=raw_stdout_path,
                stderr_path=raw_stderr_path,
                env=env,
                interruption_requested=context_cancellation_callback(ctx),
                selection_requested=context_attempt_selection_callback(ctx),
                run_dir=getattr(ctx, "run_dir", attempt_dir),
                cancellation_token=context_cancellation_token(ctx),
                sensitive_values=sensitive_values,
            )
        )
        checkpoint("external_slides.process.after_process")
        checkpoint("external_slides.process.before_raw_log_cleanup")
        raw_stdout_path.unlink(missing_ok=True)
        raw_stderr_path.unlink(missing_ok=True)
        checkpoint("external_slides.process.after_raw_log_cleanup")
        checkpoint("external_slides.process.before_process_log_write")
        if result.status == "timeout":
            _write_process_log(
                attempt_dir, cmd, result.returncode, result.stdout, result.stderr,
                harness=harness,
                model=model,
                elapsed_s=result.elapsed_s,
                sensitive_values=sensitive_values,
            )
            return {
                "status": "error",
                "reason": "slides_author_timeout",
                "message": f"coding-agent command timed out after {timeout_s}s",
            }
        if result.status == "spawn_error":
            _write_process_log(
                attempt_dir, cmd, None, result.stdout, result.stderr,
                harness=harness,
                model=model,
                elapsed_s=result.elapsed_s,
                sensitive_values=sensitive_values,
            )
            return {
                "status": "error",
                "reason": "slides_author_command_error",
                "message": result.stderr or "coding-agent command failed to start",
            }
        if result.status == "selected":
            _write_process_log(
                attempt_dir, cmd, result.returncode, result.stdout, result.stderr,
                harness=harness,
                model=model,
                elapsed_s=result.elapsed_s,
                sensitive_values=sensitive_values,
            )
            return {
                "status": "selected",
                "reason": "attempt_selected",
                "message": "authoring stopped for selected attempt",
                "returncode": result.returncode,
            }
        _write_process_log(
            attempt_dir, cmd, result.returncode, result.stdout, result.stderr,
            harness=harness,
            model=model,
            elapsed_s=result.elapsed_s,
            sensitive_values=sensitive_values,
        )
        if result.returncode != 0:
            return {
                "status": "error",
                "reason": "slides_author_process_exit",
                "message": f"coding-agent command exited with status {result.returncode}",
                "returncode": result.returncode,
            }
        if not (attempt_dir / "designer_author_done.json").is_file():
            return {
                "status": "error",
                "reason": "slides_author_missing_done_marker",
                "message": "coding-agent command did not write designer_author_done.json",
                "returncode": result.returncode,
            }
        if not _contains_json_object(attempt_dir / "designer_author_done.json"):
            return {
                "status": "error",
                "reason": "slides_author_invalid_done_marker",
                "message": "designer_author_done.json must contain a JSON object",
                "returncode": result.returncode,
            }
        if not (attempt_dir / "slides.html").is_file():
            return {
                "status": "error",
                "reason": "slides_author_missing_html",
                "message": "coding-agent command did not write slides.html",
                "returncode": result.returncode,
            }
        return {"status": "ok", "reason": "process_exit", "returncode": result.returncode}

    def _promote(
        self,
        ctx: ToolContext,
        *,
        attempt_dir: Path,
        expected_slide_count: int,
        validation: dict[str, Any],
        browser_audit: dict[str, Any] | None = None,
        candidate_id: str | None = None,
        acceptance_path: str = "deterministic_validation_pass",
        selection_owned: bool = False,
        _normal_lease_owned: bool = False,
    ) -> None:
        if not selection_owned and not _normal_lease_owned:
            original_run_dir = Path(ctx.run_dir)
            with normal_promotion_lease(
                run_dir=original_run_dir,
                candidate_id=candidate_id or "untracked-slides-candidate",
                expected_run_identity=ctx.run_directory_identity,
            ) as leased_run_dir:
                with leased_promotion_tool_context(ctx, leased_run_dir):
                    return self._promote(
                        ctx,
                        attempt_dir=(
                            leased_run_dir
                            / attempt_dir.relative_to(original_run_dir)
                        ),
                        expected_slide_count=expected_slide_count,
                        validation=validation,
                        browser_audit=browser_audit,
                        candidate_id=candidate_id,
                        acceptance_path=acceptance_path,
                        selection_owned=False,
                        _normal_lease_owned=True,
                    )
        def checkpoint(phase: str) -> None:
            context_cancellation_checkpoint(ctx, phase)
            assert_promotion_run_unchanged()
        checkpoint("external_slides.promotion.start")
        if candidate_id is not None:
            assert_promotion_allowed(
                run_dir=ctx.run_dir,
                candidate_id=candidate_id,
            )
        final_dir = ctx.run_dir / "final"
        checkpoint("external_slides.promotion.before_staging")
        staging_dir = Path(
            tempfile.mkdtemp(prefix=".slides-final-staging-", dir=ctx.run_dir)
        )
        try:
            checkpoint("external_slides.promotion.after_staging")
            checkpoint("external_slides.promotion.before_copy")
            staged_html = staging_dir / "deck.html"
            shutil.copy2(attempt_dir / "slides.html", staged_html)
            editable = ensure_editable_html_contract(staged_html, "deck")
            editable_doc = BeautifulSoup(
                staged_html.read_text(encoding="utf-8"),
                "html.parser",
            )
            editable_root = find_deck_artifact_root(editable_doc)
            actual_slide_count = (
                len(editable_root.select(".deck-slide"))
                if editable_root is not None
                else 0
            )
            if (
                editable_root is None
                or actual_slide_count < 1
                or editable.text_layer_count < actual_slide_count
            ):
                raise ValueError(
                    "Deck promotion requires one editable Deck root containing all slides "
                    "and at least one editable text layer per slide"
                )
            staged_alias = staging_dir / "slides.html"
            shutil.copy2(staged_html, staged_alias)
            _copy_slides_dependency_closure(
                attempt_dir,
                staging_dir,
                attempt_dir / "slides.html",
            )
            for name in (
                "designer_author_done.json",
                "slides_visual_plan.json",
                "slides_asset_catalog.json",
                "slides_validation.json",
            ):
                shutil.copy2(attempt_dir / name, staging_dir / name)
            atomic_write_json(
                staging_dir / "slides_validation.json",
                validation,
            )
            if browser_audit is not None:
                atomic_write_json(
                    staging_dir / "slides_browser_qa.json",
                    browser_audit,
                )
            checkpoint("external_slides.promotion.after_copy")

            slides_dir = staging_dir / "slides"
            render = screenshot_deck_slides(
                staged_html,
                slides_dir,
                slide_w=1920,
                slide_h=1080,
            )
            checkpoint("external_slides.promotion.after_render")
            staged_preview = staging_dir / "preview.png"
            render_paths = [Path(path) for path in (getattr(render, "paths", None) or [])]
            if not render_paths:
                raise RuntimeError("slide renderer produced no preview images")
            build_deck_preview_grid(render_paths, staged_preview)
            checkpoint("external_slides.promotion.after_preview")
            if not staged_preview.is_file():
                raise RuntimeError("slide preview grid was not produced")
            final_html = final_dir / "deck.html"
            final_alias = final_dir / "slides.html"
            preview_path = final_dir / "preview.png"
            delivery_assessment = assess_delivery_issues(
                "deck",
                [
                    issue
                    for issue in validation.get("issues") or []
                    if isinstance(issue, dict)
                ],
            )
            if delivery_assessment.safety_state == "blocked":
                raise ValueError("blocked Deck validation cannot be promoted")
            manifest = {
                "artifact_type": "deck",
                "render_mode": "external_slides_author_raw_html",
                "source": "external_slides_author",
                "acceptance_path": acceptance_path,
                "attempt": int(ctx.state.get("slides_author_attempts") or 1),
                "attempt_dir": str(attempt_dir),
                "slide_count": actual_slide_count,
                "expected_slide_count": expected_slide_count,
                "html_sha256": sha256_file(staged_html),
                "slides_html_sha256": sha256_file(staged_alias),
                "sidecar_sha256": {
                    name: sha256_file(staging_dir / name)
                    for name in (
                        "slides_asset_catalog.json",
                        "slides_visual_plan.json",
                        "slides_validation.json",
                        "slides_browser_qa.json",
                    )
                    if (staging_dir / name).is_file()
                },
                "quality_status": delivery_assessment.safety_state,
                "quality_diagnostics": [
                    issue.issue_id
                    for issue in delivery_assessment.quality_diagnostics
                ],
                "validation": validation,
                "preview": {
                    "path": str(preview_path),
                    "backend": getattr(render, "backend", ""),
                    "warnings": list(getattr(render, "warnings", None) or []),
                    "slide_image_count": len(render_paths),
                },
            }
            checkpoint("external_slides.promotion.before_manifest")
            atomic_write_json(staging_dir / "slides_author_manifest.json", manifest)
            checkpoint("external_slides.promotion.before_publish")
            _atomic_replace_directory(
                staging_dir,
                final_dir,
                post_publish=lambda: checkpoint(
                    "external_slides.promotion.after_publish"
                ),
            )
        except BaseException:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise
        checkpoint("external_slides.promotion.before_state")
        ctx.state["composition"] = CompositionArtifacts(
            html_path=str(final_html),
            deck_html_path=str(final_html),
            preview_path=str(preview_path) if preview_path.is_file() else None,
            layer_manifest=[{
                "layer_id": "external_slides_author_raw_html",
                "kind": "html",
                "name": "External slides author HTML",
            }],
        )
        ctx.state["last_composite_payload"] = {
            "artifact_type": "deck",
            "render_mode": "external_slides_author_raw_html",
            "designer_author_direct_final": True,
            "html_relative_path": "final/deck.html",
            "preview_relative_path": "final/preview.png" if preview_path.is_file() else None,
            "html_sha256": manifest["html_sha256"],
            "n_slides": actual_slide_count,
        }
        ctx.state["finalized"] = True
        ctx.state["designer_author_direct_final"] = {
            "source": "external_slides_author",
            "artifact_type": "deck",
            "acceptance_path": acceptance_path,
        }
        ctx.state["finalize_notes"] = "External slides author standalone HTML promoted directly."
        ctx.state["slides_author_result"] = {
            "status": "ok",
            "mode": "direct_final",
            "attempt": int(ctx.state.get("slides_author_attempts") or 1),
            "attempt_dir": str(attempt_dir),
            "final_html": str(final_html),
            "final_slides_alias": str(final_alias),
            "final_preview": str(preview_path) if preview_path.is_file() else "",
            "slide_count": actual_slide_count,
        }
        log(
            "slides_author.finalized",
            mode="external",
            html=str(final_html),
            preview=str(preview_path) if preview_path.is_file() else "",
            slide_count=actual_slide_count,
        )

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
            "type": "external_slides_author",
            "reason": reason,
            "message": message,
        }
        ctx.state["slides_author_result"] = {
            "status": "error",
            "reason": reason,
            "message": message,
            "attempt_dir": str(attempt_dir),
            "payload": payload or {},
        }
        log(
            "slides_author.fail",
            mode="external",
            reason=reason,
            message=message[:800],
            attempt_dir=str(attempt_dir),
        )


def capture_slides_attempt_candidate(
    *,
    ctx: ToolContext,
    attempt_dir: Path,
    attempt: int,
    max_attempts: int,
    validation: dict[str, Any],
) -> AttemptCandidate:
    source_html = attempt_dir / "slides.html"
    preview_path = attempt_dir / "attempt_preview.png"
    preview_paths: list[str] = []
    if source_html.is_file():
        try:
            render_dir = attempt_dir / "attempt_preview_slides"
            render = screenshot_deck_slides(
                source_html,
                render_dir,
                slide_w=1920,
                slide_h=1080,
            )
            render_paths = [
                Path(path) for path in (getattr(render, "paths", None) or [])
            ]
            if render_paths:
                build_deck_preview_grid(render_paths, preview_path)
        except Exception as exc:  # noqa: BLE001
            log(
                "attempt_candidate.preview_failed",
                artifact_type="deck",
                attempt=attempt,
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
    if preview_path.is_file():
        preview_paths.append("attempt_preview.png")

    assessment = assess_delivery_issues(
        "deck",
        [
            item
            for item in validation.get("issues") or []
            if isinstance(item, dict)
        ],
    )
    root = Path(os.path.abspath(os.fspath(attempt_dir)))
    anchored_source_html = _anchored_slides_dependency_path(source_html, root)
    try:
        closure_files = _slides_dependency_closure_files(attempt_dir, source_html)
    except ValueError:
        closure_files = [source_html]
    dependencies = [
        path.relative_to(root).as_posix()
        for path in closure_files
        if path != anchored_source_html
    ]
    browser_resource_paths = [
        path for path in dependencies if is_browser_preview_resource_path(path)
    ]
    for name in (
        "designer_author_done.json",
        "slides_visual_plan.json",
        "slides_asset_catalog.json",
        "slides_browser_qa.json",
    ):
        path = attempt_dir / name
        if path.is_file() and name not in dependencies:
            dependencies.append(name)
    candidate = capture_attempt_candidate(
        run_dir=ctx.run_dir,
        attempt_dir=attempt_dir,
        artifact_type="deck",
        attempt=attempt,
        max_attempts=max_attempts,
        source_path="slides.html",
        dependency_paths=sorted(dependencies),
        preview_paths=preview_paths,
        validation_summary_path="slides_validation.json",
        safety_state=assessment.safety_state,
        hard_blockers=list(assessment.hard_blockers),
        warnings=list(assessment.quality_diagnostics),
        browser_resource_paths=browser_resource_paths,
    )
    log(
        "attempt_candidate.available",
        run_id=ctx.run_id,
        artifact_type="deck",
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
    source_html = ctx.run_dir / candidate.source_relative_path
    attempt_snapshot = source_html.parent
    browser_audit: dict[str, Any] | None = None
    if validate_for_delivery:
        visual_plan = json.loads(
            (attempt_snapshot / "slides_visual_plan.json").read_text(encoding="utf-8")
        )
        catalog = json.loads(
            (attempt_snapshot / "slides_asset_catalog.json").read_text(encoding="utf-8")
        )
        if not isinstance(visual_plan, dict) or not isinstance(catalog, dict):
            raise ValueError("selected Deck candidate metadata is invalid")
        expected_slide_count = _expected_slide_count("", ctx)
        trusted_hashes = _trusted_slides_source_hashes(
            ctx,
            catalog,
            require_existing=True,
        )
        validation = _validate_slides(
            source_html,
            attempt_dir=attempt_snapshot,
            expected_slide_count=expected_slide_count,
            visual_plan=visual_plan,
            catalog=catalog,
            trusted_source_hashes=trusted_hashes,
        )
        browser_audit = audit_slides_html(
            source_html,
            required_source_ids=validation.get("source_visual_ids") or [],
            expected_slide_count=expected_slide_count,
        )
        validation = _merge_slides_browser_audit(validation, browser_audit)
        assessment = assess_delivery_issues(
            "deck",
            [
                issue
                for issue in validation.get("issues") or []
                if isinstance(issue, dict)
            ],
        )
        if assessment.safety_state == "blocked":
            raise ValueError("selected Deck candidate failed fresh validation")
        validation["delivery_assessment"] = {
            "safety_state": assessment.safety_state,
            "quality_diagnostics": [
                issue.model_dump(mode="json")
                for issue in assessment.quality_diagnostics
            ],
        }
    else:
        validation = json.loads(
            (ctx.run_dir / candidate.validation_summary_relative_path).read_text(
                encoding="utf-8"
            )
        )
        expected_slide_count = int(validation.get("expected_slide_count") or 1)
    ExternalSlidesAuthor(ctx.settings, "")._promote(
        ctx,
        attempt_dir=attempt_snapshot,
        expected_slide_count=expected_slide_count,
        validation=validation,
        browser_audit=browser_audit,
        candidate_id=candidate.candidate_id,
        acceptance_path="user_selected_attempt",
        selection_owned=True,
    )


def _has_shared_slides_evidence(ctx: ToolContext) -> bool:
    return bool(
        isinstance(ctx.state.get("paper_memory"), dict)
        and isinstance(ctx.state.get("paper_visual_provenance"), dict)
        and Path(ctx.layers_dir).is_dir()
    )


def _compact_validation_findings(validation: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "external_slides_repair_findings",
        "version": 1,
        "expected_slide_count": validation.get("expected_slide_count"),
        "actual_slide_count": validation.get("actual_slide_count"),
        "visual_slide_count": validation.get("visual_slide_count"),
        "visual_placement_count": validation.get("visual_placement_count"),
        "issues": [
            _compact_validation_issue(issue)
            for issue in (validation.get("issues") or [])
            if isinstance(issue, dict)
        ],
    }


def _compact_validation_issue(issue: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "id": str(issue.get("id") or ""),
        "message": str(issue.get("message") or ""),
    }
    evidence = issue.get("evidence")
    if isinstance(evidence, dict) and evidence:
        compact["evidence"] = json.loads(json.dumps(evidence, default=str))
    return compact


def _expected_slide_count(brief: str, ctx: ToolContext) -> int:
    deck_plan = ctx.state.get("deck_plan")
    if (
        isinstance(deck_plan, dict)
        and str(deck_plan.get("lock_level") or "") in {"hard", "soft"}
    ):
        count = _positive_int(deck_plan.get("slide_count"))
        if count is not None:
            return count
    raw_brief = str(ctx.state.get("raw_user_brief") or brief or "")
    explicit = parse_explicit_slide_count(raw_brief)
    return explicit or 18


def _stage_layers(ctx: ToolContext, attempt_dir: Path, catalog: dict[str, Any]) -> None:
    target_dir = attempt_dir / "layers"
    target_dir.mkdir(parents=True, exist_ok=True)
    if Path(ctx.layers_dir).is_dir():
        shutil.copytree(ctx.layers_dir, target_dir, dirs_exist_ok=True)
    used_names: dict[str, Path] = {}
    for record in catalog.get("assets") or []:
        if not isinstance(record, dict):
            continue
        layer = record.get("rendered_layer") if isinstance(record.get("rendered_layer"), dict) else {}
        source = Path(str(layer.get("src_path") or ""))
        if not source.is_file():
            output_file = str((record.get("provenance") or {}).get("output_file") or "")
            candidate = (ctx.run_dir / output_file).resolve() if output_file else None
            if candidate is not None and candidate.is_file():
                source = candidate
        if not source.is_file():
            continue
        name = source.name
        prior = used_names.get(name)
        if prior is not None and prior.resolve() != source.resolve():
            name = f"{record['asset_id']}{source.suffix.lower()}"
        target = target_dir / name
        if target.exists() and sha256_file(target) != sha256_file(source):
            name = f"{record['asset_id']}{source.suffix.lower()}"
            target = target_dir / name
        used_names[name] = source
        if not target.exists() or sha256_file(target) != sha256_file(source):
            shutil.copy2(source, target)
        record["staged_path"] = f"layers/{name}"


def _trusted_slides_source_hashes(
    ctx: ToolContext,
    catalog: dict[str, Any],
    *,
    require_existing: bool = False,
    require_catalog_match: bool = True,
) -> dict[str, str]:
    anchor_path = ctx.run_dir / "slides_trusted_source_hashes.json"
    expected_ids: set[str] = set()
    declared_hashes: dict[str, str] = {}
    for record in catalog.get("assets") or []:
        if not isinstance(record, dict):
            continue
        asset_id = str(record.get("asset_id") or "").strip()
        rendered_layer = (
            record.get("rendered_layer")
            if isinstance(record.get("rendered_layer"), dict)
            else {}
        )
        provenance = (
            record.get("provenance")
            if isinstance(record.get("provenance"), dict)
            else {}
        )
        if asset_id and (
            str(rendered_layer.get("src_path") or "").strip()
            or str(provenance.get("output_file") or "").strip()
        ):
            expected_ids.add(asset_id)
            declared_hash = str(provenance.get("output_sha256") or "").strip().lower()
            if re.fullmatch(r"[0-9a-fA-F]{64}", declared_hash):
                declared_hashes[asset_id] = declared_hash
    if anchor_path.is_file():
        try:
            anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"slides trusted source hash anchor is invalid: {exc}") from exc
        hashes = anchor.get("hashes") if isinstance(anchor, dict) else None
        if (
            not isinstance(anchor, dict)
            or anchor.get("kind") != "slides_trusted_source_hashes"
            or anchor.get("version") != 1
            or not isinstance(hashes, dict)
        ):
            raise ValueError("slides trusted source hash anchor is invalid")
        trusted = {
            str(asset_id): str(value).strip().lower()
            for asset_id, value in hashes.items()
            if str(asset_id).strip() and re.fullmatch(r"[0-9a-fA-F]{64}", str(value).strip())
        }
        if require_catalog_match and expected_ids != set(trusted):
            raise ValueError(
                "slides source catalog does not match the trusted source hash anchor"
            )
        if any(trusted.get(asset_id) != value for asset_id, value in declared_hashes.items()):
            raise ValueError(
                "slides trusted source hash anchor disagrees with ingest provenance"
            )
        return trusted
    if require_existing:
        raise ValueError(
            "slides resume requires the trusted source hash anchor from the original run"
        )

    trusted: dict[str, str] = {}
    for record in catalog.get("assets") or []:
        if not isinstance(record, dict):
            continue
        asset_id = str(record.get("asset_id") or "").strip()
        rendered_layer = (
            record.get("rendered_layer")
            if isinstance(record.get("rendered_layer"), dict)
            else {}
        )
        provenance = (
            record.get("provenance")
            if isinstance(record.get("provenance"), dict)
            else {}
        )
        if not asset_id:
            continue
        rendered_source = str(rendered_layer.get("src_path") or "").strip()
        source = Path(rendered_source) if rendered_source else None
        if source is None or not source.is_file():
            output_file = str(provenance.get("output_file") or "").strip()
            source = (ctx.run_dir / output_file).resolve() if output_file else None
        if source is not None and source.is_file():
            # Snapshot actual source bytes before the coding agent can mutate staging.
            actual_hash = sha256_file(source).lower()
            declared_hash = declared_hashes.get(asset_id)
            if declared_hash and actual_hash != declared_hash:
                raise ValueError(
                    f"slides source asset {asset_id} disagrees with ingest provenance"
                )
            trusted[asset_id] = actual_hash
    if not expected_ids.issubset(trusted):
        raise ValueError("slides source assets could not be bound to trusted bytes")
    atomic_write_json(
        anchor_path,
        {
            "kind": "slides_trusted_source_hashes",
            "version": 1,
            "source": "pre_author_actual_source_bytes",
            "hashes": trusted,
        },
    )
    anchor_path.chmod(0o444)
    return trusted


def _slides_visual_matches_hash(
    attempt_dir: Path,
    source_path: str,
    expected_hash: str,
) -> bool:
    if not expected_hash:
        return False
    parsed = urlparse(unquote(source_path.strip()))
    if parsed.scheme or parsed.netloc or not parsed.path:
        return False
    root = attempt_dir.resolve()
    candidate = (root / parsed.path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return candidate.is_file() and sha256_file(candidate).lower() == expected_hash


def _sync_visual_plan_paths(
    visual_plan: dict[str, Any],
    catalog: dict[str, Any],
) -> None:
    paths = {
        str(record.get("asset_id") or ""): str(record.get("staged_path") or "")
        for record in catalog.get("assets") or []
        if isinstance(record, dict)
    }
    for record in visual_plan.get("recommended_assets") or []:
        if isinstance(record, dict):
            record["staged_path"] = paths.get(str(record.get("asset_id") or ""), "")


def _current_slides_color_system(ctx: ToolContext) -> dict[str, Any]:
    state = ctx.state if isinstance(ctx.state, dict) else {}
    candidates = [state.get("required_color_system")]
    for key in ("poster_content_brief", "poster_plan_contract"):
        container = state.get(key) if isinstance(state.get(key), dict) else {}
        candidates.extend((
            container.get("required_color_system"),
            container.get("recommended_color_system"),
            container.get("color_system"),
        ))
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate:
            return json.loads(json.dumps(candidate, default=str))
    return {}


def _validate_slides(
    html_path: Path,
    *,
    attempt_dir: Path,
    expected_slide_count: int,
    visual_plan: dict[str, Any],
    catalog: dict[str, Any],
    trusted_source_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    if not html_path.is_file():
        issues.append(_issue("missing_slides_html", "slides.html does not exist"))
        return _validation_report(expected_slide_count, 0, issues, 0, 0, [], [])
    try:
        html = html_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        issues.append(_issue("unreadable_slides_html", f"slides.html could not be read: {exc}"))
        return _validation_report(expected_slide_count, 0, issues, 0, 0, [], [])
    soup = BeautifulSoup(html, "html.parser")
    slides = list(soup.select(".deck-slide"))
    artifact_root = find_deck_artifact_root(soup)
    if artifact_root is None or any(
        artifact_root not in slide.parents for slide in slides
    ):
        issues.append(_issue(
            "missing_deck_artifact_root",
            "all .deck-slide elements must share one non-body/html Deck root",
        ))
    if len(slides) != expected_slide_count:
        issues.append(_issue(
            "slide_count_mismatch",
            f"expected {expected_slide_count} .deck-slide elements, found {len(slides)}",
        ))
    slide_ids = [str(slide.get("id") or "").strip() for slide in slides]
    if any(not slide_id for slide_id in slide_ids) or len(set(slide_ids)) != len(slide_ids):
        issues.append(_issue("invalid_slide_ids", "every slide must have a unique non-empty id"))
    empty_slides = [
        slide_ids[index] or str(index + 1)
        for index, slide in enumerate(slides)
        if not slide.get_text(" ", strip=True)
    ]
    if empty_slides:
        issues.append(_issue("empty_slide", f"slides without native text: {', '.join(empty_slides)}"))
    compact_css = re.sub(r"\s+", "", "\n".join(tag.get_text() for tag in soup.find_all("style"))).lower()
    has_ratio = "aspect-ratio:16/9" in compact_css
    has_dimensions = "width:1920px" in compact_css and "height:1080px" in compact_css
    if not has_ratio and not has_dimensions:
        issues.append(_issue(
            "missing_16_9_viewport",
            "deck CSS must define a 16:9 or 1920x1080 slide viewport",
        ))
    script_text = "\n".join(tag.get_text() for tag in soup.find_all("script"))
    if "keydown" not in script_text or "ArrowLeft" not in script_text or "ArrowRight" not in script_text:
        issues.append(_issue(
            "missing_keyboard_navigation",
            "slides must implement ArrowLeft and ArrowRight keyboard navigation",
        ))
    issues.extend(_local_closure_issues(soup, html, attempt_dir))

    catalog_by_id = {
        str(record.get("asset_id")): record
        for record in catalog.get("assets") or []
        if isinstance(record, dict)
    }
    hidden_element_ids = _hidden_element_ids(soup, attempt_dir)
    targets = visual_plan.get("targets") if isinstance(visual_plan.get("targets"), dict) else {}
    substantive_word_floor = max(1, int(targets.get("minimum_substantive_word_count") or 30))
    per_slide_metrics: list[dict[str, Any]] = []
    metrics_by_slide: dict[int, dict[str, Any]] = {}
    visual_unit_slide_ids: set[str] = set()
    native_table_count = 0
    native_equation_count = 0
    native_diagram_count = 0
    speaker_note_count = 0
    missing_speaker_notes: list[str] = []
    invalid_speaker_notes: list[str] = []
    require_speaker_notes = bool(targets.get("require_speaker_notes"))
    for index, slide in enumerate(slides):
        slide_id = slide_ids[index] or str(index + 1)
        role = _slide_role(slide, index=index, slide_count=len(slides))
        substantive = index not in {0, len(slides) - 1}
        speaker_note = str(slide.get("data-speaker-notes") or "").strip()
        if not speaker_note:
            note_node = slide.select_one("[data-speaker-notes], .speaker-notes")
            if isinstance(note_node, Tag):
                speaker_note = str(
                    note_node.get("data-speaker-notes")
                    or note_node.get_text(" ", strip=True)
                    or ""
                ).strip()
        if speaker_note:
            speaker_note_count += 1
            if "[Sources]" not in speaker_note or "[Talk]" not in speaker_note:
                invalid_speaker_notes.append(slide_id)
        elif require_speaker_notes:
            missing_speaker_notes.append(slide_id)
        tables = [
            tag for tag in _visible_tags(slide.select("table"), hidden_element_ids)
            if _has_evidence_reference(tag) and _is_substantive_native_table(tag)
        ]
        equations = [
            tag for tag in _visible_tags(
                slide.select('math, .equation, [data-visual-unit="equation"]'),
                hidden_element_ids,
            )
            if _has_evidence_reference(tag)
        ]
        diagrams = [
            tag for tag in _visible_tags(
                slide.select('.mechanism-diagram, [data-visual-unit="diagram"]'),
                hidden_element_ids,
            )
            if _has_evidence_reference(tag) and tag.get_text(" ", strip=True)
        ]
        native_table_count += len(tables)
        native_equation_count += len(equations)
        native_diagram_count += len(diagrams)
        unit_types: list[str] = []
        if tables:
            unit_types.append("native_table")
        if equations:
            unit_types.append("verifiable_equation")
        if diagrams:
            unit_types.append("editable_mechanism_diagram")
        if unit_types:
            visual_unit_slide_ids.add(slide_id)
        metric = {
            "slide_id": slide_id,
            "slide_number": index + 1,
            "role": role,
            "substantive": substantive,
            "word_count": _word_count(slide.get_text(" ", strip=True)),
            "source_visual_ids": [],
            "source_visual_placement_count": 0,
            "native_table_count": len(tables),
            "native_equation_count": len(equations),
            "native_diagram_count": len(diagrams),
            "visual_unit_types": unit_types,
            "speaker_note_present": bool(speaker_note),
        }
        per_slide_metrics.append(metric)
        metrics_by_slide[id(slide)] = metric
    if missing_speaker_notes:
        issues.append(_issue(
            "missing_speaker_notes",
            "full formal slides require data-speaker-notes on every slide; "
            f"missing: {', '.join(missing_speaker_notes)}",
        ))
    if require_speaker_notes and invalid_speaker_notes:
        issues.append(_issue(
            "invalid_speaker_note_format",
            "speaker notes must contain [Sources] and [Talk] sections; "
            f"invalid: {', '.join(invalid_speaker_notes)}",
        ))
    sparse_substantive = [
        metric for metric in per_slide_metrics
        if metric["substantive"] and metric["word_count"] < substantive_word_floor
    ]
    if sparse_substantive:
        details = ", ".join(
            f"{metric['slide_id']}={metric['word_count']}"
            for metric in sparse_substantive
        )
        issues.append(_issue(
            "insufficient_substantive_slide_words",
            f"substantive slides require at least {substantive_word_floor} words; {details}",
        ))

    used_ids: list[str] = []
    used_fingerprints: list[str] = []
    source_visual_slide_ids: set[str] = set()
    placements_by_fingerprint: dict[str, list[str]] = {}
    labels_by_fingerprint: dict[str, str] = {}
    optional_reserve_ids = set(visual_plan.get("optional_reserve_asset_ids") or [])
    for image in soup.select("img"):
        source_id = _effective_source_id(image)
        if not source_id:
            continue
        record = catalog_by_id.get(source_id)
        eligibility = record.get("eligibility") if isinstance(record, dict) else {}
        if record is None or not (
            eligibility.get("eligible") or eligibility.get("reserve")
        ):
            issues.append(_issue(
                "unknown_or_ineligible_source_visual",
                f"source visual {source_id or '<missing>'} is not eligible in the full catalog",
            ))
            continue
        src = str(image.get("src") or "").strip()
        if src != str(record.get("staged_path") or ""):
            issues.append(_issue(
                "source_visual_path_mismatch",
                f"source visual {source_id} must use catalog path {record.get('staged_path')}",
            ))
            continue
        trusted_hash = str(
            (trusted_source_hashes or {}).get(source_id) or ""
        ).strip().lower()
        if trusted_source_hashes is not None and not trusted_hash:
            issues.append(_issue(
                "source_visual_missing_trusted_hash",
                f"source visual {source_id} has no trusted source-byte hash",
            ))
            continue
        if trusted_source_hashes is not None and not _slides_visual_matches_hash(
            attempt_dir,
            src,
            trusted_hash,
        ):
            issues.append(_issue(
                "source_visual_hash_mismatch",
                f"source visual {source_id} bytes do not match the immutable ingest source",
            ))
            continue
        if id(image) in hidden_element_ids:
            issues.append(_issue(
                "source_visual_not_visible",
                f"source visual {source_id} is hidden or has zero rendered size",
            ))
            continue
        if eligibility.get("reserve"):
            if source_id not in optional_reserve_ids:
                issues.append(_issue(
                    "unapproved_unmatched_reserve",
                    f"unmatched reserve {source_id} is not approved by the optional shortfall plan",
                ))
            continue
        parent = image.find_parent(class_="deck-slide")
        if not isinstance(parent, Tag):
            issues.append(_issue(
                "source_visual_outside_slide",
                f"source visual {source_id} must be placed inside a .deck-slide element",
            ))
            continue
        slide_id = str(parent.get("id") or "")
        fingerprint = str(record.get("fingerprint") or f"asset:{source_id}")
        used_ids.append(source_id)
        used_fingerprints.append(fingerprint)
        labels_by_fingerprint.setdefault(fingerprint, source_id)
        source_visual_slide_ids.add(slide_id)
        visual_unit_slide_ids.add(slide_id)
        placements_by_fingerprint.setdefault(fingerprint, []).append(slide_id)
        metric = metrics_by_slide.get(id(parent))
        if metric is not None:
            metric["source_visual_ids"].append(source_id)
            metric["source_visual_placement_count"] += 1
            if "original_source_visual" not in metric["visual_unit_types"]:
                metric["visual_unit_types"].append("original_source_visual")
        if not _has_local_source_interpretation(image, source_id):
            issues.append(_issue(
                "source_visual_missing_local_interpretation",
                f"source visual {source_id} on {slide_id or '<unnamed slide>'} needs a local figcaption or evidence readout",
            ))

    for fingerprint, placement_slides in placements_by_fingerprint.items():
        duplicate_slides = sorted({
            slide_id for slide_id in placement_slides
            if placement_slides.count(slide_id) > 1
        })
        if duplicate_slides:
            source_id = labels_by_fingerprint[fingerprint]
            issues.append(_issue(
                "source_visual_repeated_on_same_slide",
                f"source visual {source_id} repeats on the same slide: {', '.join(duplicate_slides)}",
            ))
    reuse_cap = max(1, int(targets.get("source_visual_reuse_cap") or 1))
    for fingerprint, placement_slides in placements_by_fingerprint.items():
        if len(placement_slides) > reuse_cap:
            source_id = labels_by_fingerprint[fingerprint]
            issues.append(_issue(
                "source_visual_reuse_cap_exceeded",
                f"source visual {source_id} appears {len(placement_slides)} times; cap is {reuse_cap}",
            ))

    min_unique_sources = int(targets.get("minimum_unique_source_visual_count") or 0)
    min_placements = int(
        targets.get("minimum_source_visual_placement_count")
        or targets.get("minimum_visual_placement_count")
        or 0
    )
    min_visual_units = int(
        targets.get("minimum_visual_unit_slide_count")
        or targets.get("minimum_visual_slide_count")
        or 0
    )
    unique_source_count = len(set(used_fingerprints))
    if unique_source_count < min_unique_sources:
        issues.append(_issue(
            "insufficient_unique_source_visuals",
            f"expected at least {min_unique_sources} unique source visuals, found {unique_source_count}",
        ))
    if len(visual_unit_slide_ids) < min(min_visual_units, expected_slide_count):
        issues.append(_issue(
            "insufficient_visual_unit_slides",
            f"expected at least {min(min_visual_units, expected_slide_count)} visual-unit slides, found {len(visual_unit_slide_ids)}",
        ))
    if len(used_ids) < min_placements:
        issues.append(_issue(
            "insufficient_source_visual_placements",
            f"expected at least {min_placements} source visual placements, found {len(used_ids)}",
        ))
        issues.append(_issue(
            "insufficient_visual_placements",
            f"expected at least {min_placements} source visual placements, found {len(used_ids)}",
        ))
    coverage = visual_plan.get("evidence_coverage") if isinstance(visual_plan.get("evidence_coverage"), dict) else {}
    used_id_set = set(used_ids)
    role_asset_ids = {
        role: {
            str(source_id)
            for source_id in coverage.get(f"{role}_asset_ids", [])
            if str(source_id).strip()
        }
        for role in ("method", "results")
    }
    used_roles = {
        role for role, source_ids in role_asset_ids.items()
        if used_id_set & source_ids
    }
    planned_ids = set().union(*role_asset_ids.values())
    used_roles.update(
        _catalog_story_role(catalog_by_id[source_id])
        for source_id in used_ids
        if source_id not in planned_ids
    )
    for role in ("method", "results"):
        if coverage.get(role) and role not in used_roles:
            issues.append(_issue(
                f"missing_{role}_visual_evidence",
                f"deck does not place any eligible {role} source visual",
            ))
    palette_audit: dict[str, Any] = {}
    required_color_system = visual_plan.get("color_system")
    if (
        isinstance(required_color_system, dict)
        and required_color_system.get("palette_id")
        and isinstance(required_color_system.get("css_variables"), dict)
        and required_color_system.get("css_variables")
    ):
        palette_audit = validate_artifact_palette(
            html,
            "\n".join(tag.get_text() for tag in soup.find_all("style")),
            required_color_system,
            "slides",
        )
        issues.extend(
            _issue(
                str(finding.get("issue_id") or "slides_palette_contract_failed"),
                str(finding.get("message") or "slides palette contract failed"),
            )
            for finding in palette_audit.get("blocking_findings") or []
        )
    return _validation_report(
        expected_slide_count,
        len(slides),
        issues,
        len(source_visual_slide_ids),
        len(used_ids),
        sorted(set(used_ids)),
        sorted(used_roles),
        per_slide_metrics=per_slide_metrics,
        visual_unit_slides=len(visual_unit_slide_ids),
        native_tables=native_table_count,
        native_equations=native_equation_count,
        native_diagrams=native_diagram_count,
        speaker_notes=speaker_note_count,
        palette_audit=palette_audit.get("debug_metrics") or {},
    )


def _local_closure_issues(soup: BeautifulSoup, html: str, attempt_dir: Path) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    references: list[str] = []
    for tag_name, attributes in _RESOURCE_ATTRIBUTES.items():
        for tag in soup.find_all(tag_name):
            for attribute in attributes:
                raw = str(tag.get(attribute) or "").strip()
                if not raw:
                    continue
                if attribute == "srcset":
                    references.extend(part.strip().split()[0] for part in raw.split(",") if part.strip())
                else:
                    references.append(raw)
    for match in re.finditer(r"url\(\s*(['\"]?)(.*?)\1\s*\)", html, flags=re.IGNORECASE):
        references.append(match.group(2).strip())
    if re.search(r"@import\b", html, flags=re.IGNORECASE):
        issues.append(_issue("css_import_forbidden", "CSS @import is not allowed"))
    try:
        _slides_dependency_closure_files(attempt_dir, attempt_dir / "slides.html")
    except ValueError as exc:
        issues.append(_issue("css_import_forbidden", str(exc)))
    if soup.find(["iframe", "embed", "object"]):
        issues.append(_issue("embedded_document_forbidden", "iframes, embeds, and objects are not allowed"))
    if re.search(r"\b(fetch|XMLHttpRequest|WebSocket)\s*\(", html):
        issues.append(_issue("network_call_forbidden", "network calls are not allowed"))
    root = attempt_dir.resolve()
    for raw in references:
        value = unquote(raw.strip().strip("'\""))
        if not value or value.startswith(("#", "data:")):
            continue
        parsed = urlparse(value)
        if parsed.scheme or parsed.netloc or value.startswith("//"):
            issues.append(_issue("remote_or_absolute_asset", f"non-local asset reference is forbidden: {raw}"))
            continue
        relative = parsed.path
        if not relative:
            continue
        candidate = (attempt_dir / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            issues.append(_issue("asset_path_escape", f"asset path escapes the attempt directory: {raw}"))
            continue
        if not candidate.is_file():
            issues.append(_issue("missing_local_asset", f"local asset does not exist: {raw}"))
    return _dedupe_issues(issues)


def _hidden_element_ids(soup: BeautifulSoup, attempt_dir: Path) -> set[int]:
    hidden_roots: list[Tag] = []
    for tag in soup.find_all(True):
        if isinstance(tag, Tag) and _tag_is_statically_hidden(tag):
            hidden_roots.append(tag)
    css_blocks = [style.get_text(" ") for style in soup.find_all("style")]
    for link in soup.find_all("link", href=True):
        rel = {str(value).lower() for value in (link.get("rel") or [])}
        if "stylesheet" not in rel:
            continue
        parsed = urlparse(unquote(str(link.get("href") or "").strip()))
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        path = (attempt_dir / parsed.path).resolve()
        try:
            path.relative_to(attempt_dir.resolve())
            css_blocks.append(path.read_text(encoding="utf-8"))
        except (ValueError, OSError, UnicodeError):
            continue
    paginated_slide_restores = _paginated_slide_restored_properties(css_blocks)
    for css in css_blocks:
        for selector_text, declarations in re.findall(
            r"([^{}]+)\{([^{}]*)\}",
            css,
        ):
            hidden_properties = _statically_hidden_properties(declarations)
            if not hidden_properties:
                continue
            for selector in selector_text.split(","):
                selector = selector.strip()
                if not selector or selector.startswith("@"):
                    continue
                try:
                    for tag in soup.select(selector):
                        if not isinstance(tag, Tag):
                            continue
                        if (
                            _selector_targets_deck_page_root(selector)
                            and "deck-slide" in (tag.get("class") or [])
                            and hidden_properties <= paginated_slide_restores
                        ):
                            continue
                        hidden_roots.append(tag)
                except Exception:
                    continue
    hidden: set[int] = set()
    for root in hidden_roots:
        hidden.add(id(root))
        hidden.update(id(tag) for tag in root.find_all(True))
    return hidden


def _tag_is_statically_hidden(tag: Tag) -> bool:
    if tag.has_attr("hidden"):
        return True
    for attribute in ("width", "height"):
        value = str(tag.get(attribute) or "").strip().lower()
        if re.fullmatch(r"0(?:\.0+)?(?:px|pt|em|rem|%)?", value):
            return True
    return _style_is_statically_hidden(str(tag.get("style") or ""))


def _style_is_statically_hidden(style: str) -> bool:
    return bool(_statically_hidden_properties(style))


def _statically_hidden_properties(style: str) -> set[str]:
    declarations: dict[str, str] = {}
    for item in style.lower().split(";"):
        if ":" not in item:
            continue
        name, value = item.split(":", 1)
        declarations[name.strip()] = value.strip().replace("!important", "").strip()
    hidden: set[str] = set()
    if declarations.get("display") == "none":
        hidden.add("display")
    if declarations.get("visibility") in {"hidden", "collapse"}:
        hidden.add("visibility")
    opacity = declarations.get("opacity")
    if opacity is not None:
        try:
            if float(opacity) <= 0:
                hidden.add("opacity")
        except ValueError:
            pass
    zero = re.compile(r"0(?:\.0+)?(?:px|pt|em|rem|%)?$")
    hidden.update(
        name
        for name in ("width", "height", "max-width", "max-height")
        if zero.fullmatch(declarations.get(name, "")) is not None
    )
    return hidden


def _paginated_slide_restored_properties(css_blocks: list[str]) -> set[str]:
    restored: set[str] = set()
    for css in css_blocks:
        for selector_text, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            selectors = [
                selector.strip()
                for selector in selector_text.split(",")
                if selector.strip() and not selector.strip().startswith("@")
            ]
            if not any(_selector_targets_active_deck_page(selector) for selector in selectors):
                continue
            values: dict[str, str] = {}
            for item in declarations.lower().split(";"):
                if ":" not in item:
                    continue
                name, value = item.split(":", 1)
                values[name.strip()] = value.strip().replace("!important", "").strip()
            if values.get("display") and values["display"] != "none":
                restored.add("display")
            if values.get("visibility") == "visible":
                restored.add("visibility")
            if "opacity" in values:
                try:
                    if float(values["opacity"]) > 0:
                        restored.add("opacity")
                except ValueError:
                    pass
            zero = re.compile(r"0(?:\.0+)?(?:px|pt|em|rem|%)?$")
            for name in ("width", "height", "max-width", "max-height"):
                if values.get(name) and zero.fullmatch(values[name]) is None:
                    restored.add(name)
    return restored


def _selector_targets_deck_page_root(selector: str) -> bool:
    tail = re.split(r"\s+|[>+~]", selector.strip())[-1].lower()
    return tail in {
        ".deck-slide",
        ".deck-slide:not(.active)",
        ".deck-slide:not(.is-active)",
        '.deck-slide[aria-hidden="true"]',
        ".deck-slide[aria-hidden='true']",
        ".deck-slide[data-active='false']",
        '.deck-slide[data-active="false"]',
    }


def _selector_targets_active_deck_page(selector: str) -> bool:
    tail = re.split(r"\s+|[>+~]", selector.strip())[-1].lower()
    return tail in {
        ".deck-slide.active",
        ".deck-slide.is-active",
        ".deck-slide.current",
        '.deck-slide[aria-hidden="false"]',
        ".deck-slide[aria-hidden='false']",
        ".deck-slide[data-active='true']",
        '.deck-slide[data-active="true"]',
    }


def _slide_role(slide: Tag, *, index: int, slide_count: int) -> str:
    if index == 0:
        return "cover"
    if index == slide_count - 1:
        return "closing"
    declared = str(slide.get("data-slide-role") or "").strip().lower()
    return declared or "content"


def _word_count(text: str) -> int:
    latin_words = re.findall(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)*", text)
    cjk_characters = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text)
    return len(latin_words) + len(cjk_characters)


def _visible_tags(tags: list[Tag], hidden_element_ids: set[int]) -> list[Tag]:
    visible: list[Tag] = []
    seen: set[int] = set()
    for tag in tags:
        tag_id = id(tag)
        if tag_id not in seen and tag_id not in hidden_element_ids:
            visible.append(tag)
            seen.add(tag_id)
    return visible


def _has_evidence_reference(tag: Tag) -> bool:
    return any(
        str(tag.get(attribute) or "").strip()
        for attribute in ("data-evidence-ref", "data-source-id", "data-evidence-quote")
    )


def _is_substantive_native_table(table: Tag) -> bool:
    rows = table.find_all("tr")
    if len(rows) < 2:
        return False
    populated_rows = 0
    for row in rows:
        cells = [
            cell.get_text(" ", strip=True)
            for cell in row.find_all(["th", "td"], recursive=False)
        ]
        if sum(bool(cell) for cell in cells) >= 2:
            populated_rows += 1
    return populated_rows >= 2


def _has_local_source_interpretation(image: Tag, source_id: str) -> bool:
    direct = str(image.get("data-interpretation") or "").strip()
    if _word_count(direct) >= 6:
        return True
    figure = image.find_parent("figure")
    if isinstance(figure, Tag):
        caption = figure.find("figcaption")
        if (
            isinstance(caption, Tag)
            and _word_count(caption.get_text(" ", strip=True)) >= 6
        ):
            return True
    slide = image.find_parent(class_="deck-slide")
    if not isinstance(slide, Tag):
        return False
    for readout in slide.select(".evidence-readout"):
        readout_source = str(readout.get("data-source-id") or "").strip()
        if (
            readout_source == source_id
            and _word_count(readout.get_text(" ", strip=True)) >= 6
        ):
            return True
    return False


def _effective_source_id(image: Tag) -> str:
    source_id = str(image.get("data-source-id") or "").strip()
    if source_id:
        return source_id
    wrapper = image.find_parent(["figure", "picture"])
    if isinstance(wrapper, Tag):
        return str(wrapper.get("data-source-id") or "").strip()
    return ""


def _stage_runtime_skills(
    ctx: ToolContext,
    attempt_dir: Path,
    *,
    stage: str,
) -> dict[str, Any]:
    from .external_designer_author import _stage_runtime_skills as stage_active_skills

    result = stage_active_skills(ctx, attempt_dir, stage=stage)
    catalog = result.get("catalog") if isinstance(result.get("catalog"), dict) else {}
    if not result.get("files") or not catalog.get("skills"):
        if ctx.state.get("legacy_runtime_skills_compat") is True:
            catalog["available"] = False
            catalog["legacy_compat"] = True
            result["catalog"] = catalog
            return result
        raise ValueError(
            "runtime skill snapshot is required and must contain an active-stage skill pack"
        )
    staged = attempt_dir / "runtime_skills"
    if staged.is_dir():
        for path in staged.rglob("*"):
            path.chmod(0o555 if path.is_dir() else 0o444)
        staged.chmod(0o555)
    return result


def _copy_slides_dependency_closure(
    attempt_dir: Path,
    final_dir: Path,
    source_html: Path,
) -> None:
    root = Path(os.path.abspath(os.fspath(attempt_dir)))
    for source in _slides_dependency_closure_files(attempt_dir, source_html):
        relative = source.relative_to(root)
        if relative.as_posix() == "slides.html":
            continue
        target = final_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _slides_dependency_closure_files(
    attempt_dir: Path,
    source_html: Path,
) -> list[Path]:
    root = Path(os.path.abspath(os.fspath(attempt_dir)))
    queue = [_anchored_slides_dependency_path(source_html, root)]
    visited: set[Path] = set()
    while queue:
        source = queue.pop(0)
        if source in visited or not source.is_file():
            continue
        try:
            relative = source.relative_to(root)
        except ValueError:
            continue
        visited.add(source)
        if source.suffix.lower() not in {".html", ".htm", ".css"}:
            continue
        try:
            content = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if _document_contains_css_import(content, source.suffix.lower()):
            raise ValueError(f"CSS @import is not allowed: {relative.as_posix()}")
        for raw in _slides_document_local_references(content, source.suffix.lower()):
            parsed = urlparse(unquote(raw.strip()))
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            try:
                candidate = _anchored_slides_dependency_path(
                    source.parent / parsed.path,
                    root,
                )
            except ValueError:
                continue
            if candidate.is_file() and candidate not in visited:
                queue.append(candidate)
    return sorted(visited)


def _anchored_slides_dependency_path(path: Path, root: Path) -> Path:
    stable_root = Path(os.path.abspath(os.fspath(root)))
    candidate = Path(os.path.abspath(os.fspath(path)))
    relative = candidate.relative_to(stable_root)
    if ".." in relative.parts:
        raise ValueError("slides dependency escapes its attempt directory")
    current = stable_root
    for component in relative.parts:
        current = current / component
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("slides dependency cannot traverse a reparse point")
    return candidate


def _document_contains_css_import(content: str, suffix: str) -> bool:
    if suffix in {".html", ".htm"}:
        soup = BeautifulSoup(content, "html.parser")
        content = "\n".join(
            [tag.get_text(" ") for tag in soup.find_all("style")]
            + [str(tag.get("style") or "") for tag in soup.find_all(style=True)]
        )
    return re.search(r"@import\b", content, re.IGNORECASE) is not None


def _slides_document_local_references(content: str, suffix: str) -> list[str]:
    references: list[str] = []
    if suffix in {".html", ".htm"}:
        soup = BeautifulSoup(content, "html.parser")
        for tag_name, attributes in _RESOURCE_ATTRIBUTES.items():
            for tag in soup.find_all(tag_name):
                for attribute in attributes:
                    raw = str(tag.get(attribute) or "").strip()
                    if not raw:
                        continue
                    if attribute == "srcset":
                        references.extend(
                            part.strip().split()[0]
                            for part in raw.split(",")
                            if part.strip()
                        )
                    else:
                        references.append(raw)
        content = "\n".join(
            [tag.get_text(" ") for tag in soup.find_all("style")]
            + [str(tag.get("style") or "") for tag in soup.find_all(style=True)]
        )
    references.extend(
        match.group(2).strip()
        for match in re.finditer(r"url\(\s*(['\"]?)(.*?)\1\s*\)", content, re.IGNORECASE)
    )
    return references


def _validation_report(
    expected: int,
    actual: int,
    issues: list[dict[str, str]],
    visual_slides: int,
    visual_placements: int,
    source_ids: list[str],
    source_roles: list[str],
    *,
    per_slide_metrics: list[dict[str, Any]] | None = None,
    visual_unit_slides: int = 0,
    native_tables: int = 0,
    native_equations: int = 0,
    native_diagrams: int = 0,
    speaker_notes: int = 0,
    palette_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": "external_slides_validation",
        "version": 3,
        "status": "error" if issues else "ok",
        "expected_slide_count": expected,
        "actual_slide_count": actual,
        "visual_slide_count": visual_slides,
        "visual_placement_count": visual_placements,
        "unique_source_visual_count": len(source_ids),
        "source_visual_placement_count": visual_placements,
        "visual_unit_slide_count": visual_unit_slides,
        "native_table_count": native_tables,
        "native_equation_count": native_equations,
        "native_diagram_count": native_diagrams,
        "speaker_note_count": speaker_notes,
        "palette_audit": palette_audit or {},
        "per_slide_metrics": per_slide_metrics or [],
        "source_visual_ids": source_ids,
        "source_visual_roles": source_roles,
        "issues": _dedupe_issues(issues),
    }


def _merge_slides_browser_audit(
    validation: dict[str, Any],
    browser_audit: dict[str, Any],
) -> dict[str, Any]:
    merged = json.loads(json.dumps(validation, default=str))
    browser_issues: list[dict[str, Any]] = []
    for finding in (browser_audit.get("findings") or []):
        if not isinstance(finding, dict):
            continue
        issue: dict[str, Any] = _issue(
            str(finding.get("id") or "slides_browser_audit_failed"),
            str(finding.get("message") or "slides browser audit failed"),
        )
        evidence = finding.get("evidence")
        if isinstance(evidence, dict) and evidence:
            issue["evidence"] = json.loads(json.dumps(evidence, default=str))
        browser_issues.append(issue)
    merged["issues"] = _dedupe_issues(
        [*(merged.get("issues") or []), *browser_issues]
    )
    merged["status"] = "ok" if (
        merged.get("status") == "ok"
        and browser_audit.get("accepted")
        and not browser_issues
    ) else "error"
    merged["browser_audit"] = browser_audit.get("metrics") or {}
    merged["browser_audit_backend"] = str(browser_audit.get("backend") or "")
    return merged


def _browser_audit_unavailable(validation: dict[str, Any]) -> bool:
    return any(
        str(issue.get("id") or "") == "artifact_browser_audit_unavailable"
        for issue in (validation.get("issues") or [])
        if isinstance(issue, dict)
    )


def _catalog_story_role(record: dict[str, Any]) -> str:
    text = " ".join((
        str(record.get("visual_role") or ""),
        str(record.get("caption") or ""),
        str(record.get("kind") or ""),
    )).lower()
    if any(term in text for term in ("method", "architecture", "pipeline", "framework", "system")):
        return "method"
    if any(term in text for term in ("result", "table", "benchmark", "evaluation", "ablation", "analysis", "qualitative")):
        return "results"
    return "supporting"


def _issue(issue_id: str, message: str) -> dict[str, Any]:
    return {"id": issue_id, "message": message}


def _dedupe_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        key = (issue["id"], issue["message"])
        if key not in seen:
            out.append(issue)
            seen.add(key)
    return out


def _contains_json_object(path: Path) -> bool:
    try:
        return isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
    except (OSError, UnicodeError, ValueError):
        return False


def _atomic_replace_directory(
    staging_dir: Path,
    final_dir: Path,
    *,
    post_publish: Any,
) -> None:
    publish_artifact_directory(
        staging_dir,
        final_dir,
        artifact_name="slides",
        post_publish=post_publish,
    )


def _recover_interrupted_promotion(final_dir: Path) -> None:
    recover_artifact_promotion(final_dir, artifact_name="slides")


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _write_process_log(
    attempt_dir: Path,
    cmd: list[str],
    returncode: int | None,
    stdout: Any,
    stderr: Any,
    *,
    harness: str = "",
    model: str = "",
    elapsed_s: float = 0.0,
    sensitive_values: list[str] | None = None,
) -> None:
    sensitive_values = sensitive_values or []
    payload = {
        "harness": harness,
        "model": model,
        "command_sha256": hashlib.sha256("\0".join(cmd).encode("utf-8")).hexdigest(),
        "returncode": returncode,
        "elapsed_s": round(max(0.0, float(elapsed_s)), 3),
        "stdout": _redact_process_text(stdout, sensitive_values),
        "stderr": _redact_process_text(stderr, sensitive_values),
    }
    atomic_write_json(attempt_dir / "designer_author_log.json", payload)


def _process_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _process_sensitive_values(
    cmd: list[str],
    harness_api_key: Any,
    env: dict[str, str] | None = None,
) -> list[str]:
    values: set[str] = set()
    sensitive_name = re.compile(r"(?:api[_-]?key|token|secret|password)", re.I)
    api_key = str(harness_api_key or "").strip()
    if len(api_key) >= 4:
        values.add(api_key)
    for key, raw_value in (env or {}).items():
        value = str(raw_value or "").strip()
        if sensitive_name.search(str(key)) and len(value) >= 4:
            values.add(value)
    for index, token in enumerate(cmd):
        lowered = token.casefold()
        flag_name = lowered.split("=", 1)[0].lstrip("-").replace("_", "-")
        is_sensitive = flag_name.endswith(
            ("api-key", "apikey", "token", "secret", "password", "authorization")
        )
        if is_sensitive and "=" not in token and index + 1 < len(cmd):
            value = str(cmd[index + 1]).strip()
            if len(value) >= 4:
                values.add(value)
            continue
        if is_sensitive and "=" in token:
            value = token.split("=", 1)[1].strip()
            if len(value) >= 4:
                values.add(value)
    return sorted(values, key=len, reverse=True)


def _redact_process_text(value: Any, sensitive_values: list[str]) -> str:
    text = _process_text(value)
    for secret in sensitive_values:
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(?i)(\bbearer\s+)([^\s\"']+)",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)((?:--)?(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|authorization)(?:\s*[:=]\s*|\s+))([^\s,;\"']+)",
        r"\1[REDACTED]",
        text,
    )
    return text
