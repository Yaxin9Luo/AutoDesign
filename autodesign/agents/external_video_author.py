"""External coding-agent author for paper-to-video HyperFrames projects."""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile
from types import FunctionType
from typing import Any
from urllib.parse import unquote, urlparse

from ..candidate_assessment import assess_delivery_issues
from ..config import authoring_max_attempts_for, harness_subprocess_env
from ..designer import invoke_designer_tool
from ..schema import (
    AttemptCandidate,
    DesignSpec,
    ToolResultRecord,
    VideoDeliveryContract,
    VIDEO_MAX_DURATION_S,
    VIDEO_MIN_DURATION_S,
    VideoSceneContract,
)
from ..skills.registry import SkillBundle
from ..tools import export_video as _export_video
from ..tools._contract import ToolContext
from ..util.io import atomic_write_json
from ..util.logging import log
from ..util.browser_render import screenshot_html
from ..util.editable_html import ensure_editable_html_contract
from ..util.video_visual_plan import (
    build_video_visual_asset_catalog,
    build_video_visual_plan,
)
from .hyperframes_composer import (
    HYPERFRAMES_AUTHOR_PROTOCOL,
    ComposerResult,
    validate_authored_video_html,
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
from ..attempt_candidates import (
    assert_promotion_run_unchanged,
    capture_attempt_candidate,
    is_browser_preview_resource_path,
)
from ..attempt_selection import (
    assert_promotion_allowed,
    leased_promotion_tool_context,
    normal_promotion_lease,
    promote_pending_selection,
    ranked_delivery_candidates,
    transition_selection,
)


_PAPER_CONTEXT_FILES = (
    "paper_memory.json",
    "paper_memory.md",
    "paper_memory_dossier.json",
    "paper_memory_dossier.md",
    "paper_visual_provenance.json",
)
_NARRATIVE_CONTEXT_FILES = (
    "narrative_context.json",
    "narrative_context.md",
    "video_narrative.json",
    "video_narrative.md",
    "transcript_context.json",
    "transcript_context.txt",
    "transcript.md",
    "transcript.txt",
)
_PROTECTED_DELIVERY_FILES = {
    "delivery_manifest.json",
    "media_probe.json",
    "video_delivery_contract.json",
}
_MINIMUM_SPOKEN_WPM = 90
_SCENE_WORD_TOLERANCE = 1
_MINIMUM_SPEECH_COVERAGE_RATIO = 0.72
_SPOKEN_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*")
_VIDEO_QUALITY_ERROR_PATTERNS = (
    (re.compile(r"language must be en", re.IGNORECASE), "video_language_quality"),
    (
        re.compile(r"must contain 10-14 scenes", re.IGNORECASE),
        "video_scene_count_quality",
    ),
    (re.compile(r"title is required", re.IGNORECASE), "video_scene_title_quality"),
    (
        re.compile(r"subtitle_intent is required", re.IGNORECASE),
        "video_scene_subtitle_quality",
    ),
    (
        re.compile(
            r"narration_intent appears to use repeated filler",
            re.IGNORECASE,
        ),
        "video_scene_narration_quality",
    ),
    (
        re.compile(r"narration_intent duplicates scene", re.IGNORECASE),
        "video_scene_narration_quality",
    ),
    (
        re.compile(
            r"narration_intent must be the verbatim spoken transcript",
            re.IGNORECASE,
        ),
        "video_scene_narration_quality",
    ),
    (
        re.compile(r"total narration transcript must contain", re.IGNORECASE),
        "video_narration_quality",
    ),
    (
        re.compile(r"visual_ids must be a list", re.IGNORECASE),
        "video_visual_coverage_quality",
    ),
    (
        re.compile(
            r"source visual .* is repeated without justification",
            re.IGNORECASE,
        ),
        "video_visual_reuse_quality",
    ),
    (
        re.compile(
            r"video requires at least .* unique formal eligible source visuals",
            re.IGNORECASE,
        ),
        "video_visual_coverage_quality",
    ),
    (
        re.compile(
            r"video is missing .* source visual coverage",
            re.IGNORECASE,
        ),
        "video_visual_coverage_quality",
    ),
)


class ExternalVideoAuthor:
    """Run a configured local coding agent as the conference-video author."""

    def __init__(self, settings: Any, system_prompt: str):
        self.settings = settings
        self._total_in = 0
        self._total_out = 0
        self._total_cache_read = 0
        self._total_cache_create = 0

    def run(self, brief: str, ctx: ToolContext) -> None:
        context_cancellation_checkpoint(ctx, "external_video.before_start")
        if promote_pending_selection(ctx) != "none":
            return
        _recover_video_final_promotion(ctx.run_dir / "final")
        context_cancellation_checkpoint(ctx, "external_video.after_recovery")
        ctx.state.pop("designer_api_error", None)
        ctx.state["artifact_type"] = "video"
        command = str(getattr(self.settings, "designer_author_cmd", "") or "").strip()
        timeout_s = max(
            1, int(getattr(self.settings, "designer_author_timeout_s", 1800) or 1800)
        )
        max_attempts = authoring_max_attempts_for(self.settings, "video")
        prior_attempts = int(ctx.state.get("video_author_attempts") or 0)
        absolute_attempt_budget = prior_attempts + max_attempts
        author_root = ctx.run_dir / "video_author"
        context_cancellation_checkpoint(ctx, "external_video.before_author_dir")
        author_root.mkdir(parents=True, exist_ok=True)
        context_cancellation_checkpoint(ctx, "external_video.after_author_dir")

        if not command:
            self._fail(
                ctx,
                "missing_designer_author_cmd",
                "external video author requires the configured local designer-author command",
                author_root,
            )
            return

        if not self._ensure_ingested(ctx, author_root):
            return

        provenance = _load_context_json(ctx, "paper_visual_provenance")
        paper_memory = _load_context_json(ctx, "paper_memory")
        dossier = _load_context_json(ctx, "paper_memory_dossier")
        if not provenance or not paper_memory:
            self._fail(
                ctx,
                "video_author_missing_full_ingest",
                "external video author requires paper_memory, paper_visual_provenance, "
                "and layers from the full paper ingest",
                author_root,
            )
            return

        rendered_layers = (
            ctx.state.get("rendered_layers")
            if isinstance(ctx.state.get("rendered_layers"), dict)
            else {}
        )
        catalog = build_video_visual_asset_catalog(
            provenance,
            rendered_layers=rendered_layers,
            trusted_run_root=ctx.run_dir,
        )
        plan = build_video_visual_plan(
            provenance,
            rendered_layers=rendered_layers,
            trusted_run_root=ctx.run_dir,
            scene_count=12,
        )
        eligible_assets = [
            asset for asset in catalog["assets"]
            if asset["eligibility"]["eligible"]
        ]
        eligible_ids = {str(asset["asset_id"]) for asset in eligible_assets}
        required_assets = [
            asset for asset in eligible_assets
            if asset["can_satisfy_required_coverage"]
        ]
        required_ids = {str(asset["asset_id"]) for asset in required_assets}
        eligible_roles = {
            str(asset["asset_id"]): str(asset["video_evidence_role"])
            for asset in required_assets
        }
        try:
            _trusted_video_source_context(
                ctx.run_dir,
                eligible_assets=eligible_assets,
                eligible_asset_roles=eligible_roles,
                required_asset_ids=required_ids,
                minimum_required_visual_count=int(
                    plan["minimum_required_visual_count"]
                ),
            )
        except ValueError as exc:
            self._fail(
                ctx,
                "video_author_trusted_source_context_failed",
                str(exc),
                author_root,
            )
            return
        resume = ctx.state.get("external_author_resume")
        resume = resume if isinstance(resume, dict) else {}
        previous_attempt_value = str(resume.get("previous_attempt_dir") or "").strip()
        previous_attempt_dir = Path(previous_attempt_value) if previous_attempt_value else None
        selected_target_duration_s: int | None = None
        if previous_attempt_dir is not None:
            selected_target_duration_s, target_error = (
                _authoritative_target_from_previous_attempt(previous_attempt_dir)
            )
            if target_error:
                self._fail(
                    ctx,
                    "video_author_repair_target_invalid",
                    target_error,
                    previous_attempt_dir,
                )
                return
            if selected_target_duration_s is not None:
                plan["target_duration_s"] = selected_target_duration_s
        repair_feedback = resume.get("repair_feedback")
        last_errors = _feedback_error_messages(repair_feedback)

        for attempt_index in range(max_attempts):
            context_cancellation_checkpoint(ctx, "external_video.before_attempt")
            if promote_pending_selection(ctx) != "none":
                return
            context_cancellation_checkpoint(ctx, "external_video.after_selection_check")
            attempt_dir = self._next_attempt_dir(ctx)
            log(
                "video_author.attempt_start",
                mode="external",
                attempt=int(
                    ctx.state.get("video_author_attempts") or attempt_index + 1
                ),
                max_attempts=absolute_attempt_budget,
            )
            context_cancellation_checkpoint(ctx, "external_video.before_staging")
            staged_files, stage_errors = self._stage_inputs(
                ctx,
                brief=brief,
                attempt_dir=attempt_dir,
                provenance=provenance,
                paper_memory=paper_memory,
                dossier=dossier,
                catalog=catalog,
                plan=plan,
                previous_attempt_dir=previous_attempt_dir,
            )
            if stage_errors:
                self._fail(
                    ctx,
                    "video_author_staging_failed",
                    "; ".join(stage_errors),
                    attempt_dir,
                )
                return
            context_cancellation_checkpoint(ctx, "external_video.after_staging")

            prompt = self._build_prompt(
                brief=brief,
                attempt_dir=attempt_dir,
                previous_errors=last_errors,
                repair_baseline_staged=(attempt_dir / "repair_baseline").is_dir(),
            )
            context_cancellation_checkpoint(ctx, "external_video.before_prompt_write")
            (attempt_dir / "video_author_prompt.md").write_text(
                prompt, encoding="utf-8"
            )
            context_cancellation_checkpoint(ctx, "external_video.after_prompt_write")
            invocation_error = _invoke_author_command(
                command,
                prompt=prompt,
                attempt_dir=attempt_dir,
                timeout_s=timeout_s,
                settings=self.settings,
                run_id=ctx.run_id,
                attempt=int(
                    ctx.state.get("video_author_attempts") or attempt_index + 1
                ),
                ctx=ctx,
            )
            context_cancellation_checkpoint(ctx, "external_video.after_author_process")
            if invocation_error == "attempt_selected":
                promote_pending_selection(ctx)
                return
            if invocation_error:
                last_errors = [invocation_error]
                context_cancellation_checkpoint(ctx, "external_video.before_process_error_write")
                atomic_write_json(
                    attempt_dir / "video_author_validation_errors.json",
                    {"errors": last_errors},
                )
                context_cancellation_checkpoint(ctx, "external_video.after_process_error_write")
                previous_attempt_dir = attempt_dir
                continue

            manifest_path = attempt_dir / "video_author_manifest.json"
            done_path = attempt_dir / "designer_author_done.json"
            project_dir = attempt_dir / "project"
            context_cancellation_checkpoint(ctx, "external_video.before_marker_reads")
            manifest, manifest_error = _read_json_object(manifest_path)
            _, done_error = _read_json_object(done_path)
            context_cancellation_checkpoint(ctx, "external_video.after_marker_reads")
            if manifest_error:
                last_errors = [manifest_error]
            elif done_error:
                last_errors = [done_error]
            else:
                expected_target_duration_s = selected_target_duration_s
                last_errors = validate_video_author_output(
                    project_dir=project_dir,
                    manifest=manifest,
                    eligible_asset_ids=eligible_ids,
                    eligible_asset_roles=eligible_roles,
                    eligible_asset_paths={
                        str(asset["asset_id"]): attempt_dir / str(asset["output_file"])
                        for asset in eligible_assets
                    },
                    eligible_asset_hashes={
                        str(asset["asset_id"]): str(asset["actual_sha256"])
                        for asset in eligible_assets
                        if str(asset.get("actual_sha256") or "").strip()
                    },
                    required_asset_ids=required_ids,
                    minimum_required_visual_count=int(
                        plan["minimum_required_visual_count"]
                    ),
                    expected_target_duration_s=expected_target_duration_s,
                )
                manifest_target_duration_s, _ = _manifest_target_duration(manifest)
                if (
                    selected_target_duration_s is None
                    and manifest_target_duration_s is not None
                ):
                    selected_target_duration_s = manifest_target_duration_s
                    plan["target_duration_s"] = manifest_target_duration_s
                context_cancellation_checkpoint(ctx, "external_video.after_validation")
            if last_errors:
                context_cancellation_checkpoint(ctx, "external_video.before_validation_error_write")
                atomic_write_json(
                    attempt_dir / "video_author_validation_errors.json",
                    {"errors": last_errors},
                )
                context_cancellation_checkpoint(ctx, "external_video.after_validation_error_write")
                if (project_dir / "index.html").is_file():
                    capture_video_attempt_candidate(
                        ctx=ctx,
                        attempt_dir=attempt_dir,
                        attempt=int(
                            ctx.state.get("video_author_attempts") or attempt_index + 1
                        ),
                        max_attempts=absolute_attempt_budget,
                        manifest=manifest,
                        validation_errors=last_errors,
                    )
                context_cancellation_checkpoint(ctx, "external_video.before_validation_retry")
                previous_attempt_dir = attempt_dir
                continue

            context_cancellation_checkpoint(ctx, "external_video.before_manifest_copy")
            shutil.copy2(manifest_path, project_dir / manifest_path.name)
            context_cancellation_checkpoint(ctx, "external_video.after_manifest_copy")
            candidate = capture_video_attempt_candidate(
                ctx=ctx,
                attempt_dir=attempt_dir,
                attempt=int(ctx.state.get("video_author_attempts") or attempt_index + 1),
                max_attempts=absolute_attempt_budget,
                manifest=manifest,
                validation_errors=[],
            )
            if promote_pending_selection(ctx) != "none":
                return
            delivery_result, finalize_result = self._deliver_normal_candidate(
                candidate_id=candidate.candidate_id,
                project_dir=project_dir,
                manifest=manifest,
                ctx=ctx,
            )
            if delivery_result.status == "error":
                delivery_diagnostics = {
                    "error_message": (
                        delivery_result.error_message or "video delivery failed"
                    ),
                    "error_category": delivery_result.error_category,
                    "payload": delivery_result.payload,
                }
                context_cancellation_checkpoint(ctx, "external_video.before_delivery_error_write")
                atomic_write_json(
                    attempt_dir / "video_author_delivery_errors.json",
                    delivery_diagnostics,
                )
                context_cancellation_checkpoint(ctx, "external_video.after_delivery_error_write")
                if (
                    attempt_index + 1 < max_attempts
                    and _delivery_failure_is_repairable(delivery_result)
                ):
                    context_cancellation_checkpoint(ctx, "external_video.before_delivery_retry")
                    last_errors = _delivery_repair_feedback(delivery_result)
                    previous_attempt_dir = attempt_dir
                    continue
                self._fail(
                    ctx,
                    "video_author_delivery_failed",
                    delivery_result.error_message or "video delivery failed",
                    attempt_dir,
                    payload=delivery_result.payload,
                )
                return

            ctx.state["artifact_type"] = "video"
            if finalize_result is None or finalize_result.status == "error":
                finalize_diagnostics = {
                    "error_message": (
                        (finalize_result.error_message if finalize_result else None)
                        or "video finalization failed after delivery passed"
                    ),
                    "error_category": (
                        finalize_result.error_category if finalize_result else None
                    ),
                    "payload": finalize_result.payload if finalize_result else {},
                }
                atomic_write_json(
                    attempt_dir / "video_author_finalize_errors.json",
                    finalize_diagnostics,
                )
                self._fail(
                    ctx,
                    "video_author_finalize_failed",
                    str(finalize_diagnostics["error_message"]),
                    attempt_dir,
                    payload=finalize_result.payload if finalize_result else {},
                )
                return

            self._record_delivery_success(
                ctx,
                attempt_dir=attempt_dir,
                project_dir=project_dir,
                manifest_path=manifest_path,
                delivery_result=delivery_result,
                staged_files=staged_files,
                acceptance_path="formal_video_delivery_pass",
                quality_diagnostics=[],
            )
            log(
                "video_author.done",
                mode="external",
                attempt_dir=str(attempt_dir),
                scenes=len(manifest["scenes"]),
            )
            return

        if self._try_deliver_best_available_candidate(
            ctx,
            eligible_asset_ids=eligible_ids,
            eligible_asset_roles=eligible_roles,
            eligible_asset_paths={
                str(asset["asset_id"]): ctx.run_dir / str(asset["output_file"])
                for asset in eligible_assets
            },
            eligible_asset_hashes={
                str(asset["asset_id"]): str(asset["actual_sha256"])
                for asset in eligible_assets
                if str(asset.get("actual_sha256") or "").strip()
            },
            required_asset_ids=required_ids,
            minimum_required_visual_count=int(
                plan["minimum_required_visual_count"]
            ),
            expected_target_duration_s=selected_target_duration_s,
        ):
            return
        fallback_error = str(
            ctx.state.get("video_author_best_available_delivery_error") or ""
        ).strip()
        self._fail(
            ctx,
            "video_author_attempts_exhausted",
            fallback_error
            or "; ".join(last_errors)
            or "external video author produced no valid project",
            attempt_dir,
        )

    def _try_deliver_best_available_candidate(
        self,
        ctx: ToolContext,
        *,
        eligible_asset_ids: set[str],
        eligible_asset_roles: dict[str, str],
        eligible_asset_paths: dict[str, Path],
        eligible_asset_hashes: dict[str, str],
        required_asset_ids: set[str],
        minimum_required_visual_count: int,
        expected_target_duration_s: int | None,
    ) -> bool:
        if promote_pending_selection(ctx) != "none":
            return True
        ctx.state.pop("video_author_best_available_delivery_error", None)
        for candidate in ranked_delivery_candidates(
            ctx.run_dir,
            artifact_type="video",
        ):
            source_html = ctx.run_dir / candidate.source_relative_path
            attempt_dir = source_html.parent.parent
            project_dir = attempt_dir / "project"
            manifest_path = attempt_dir / "video_author_manifest.json"
            manifest, manifest_error = _read_json_object(manifest_path)
            if manifest_error:
                continue
            validation_errors = validate_video_author_output(
                project_dir=project_dir,
                manifest=manifest,
                eligible_asset_ids=eligible_asset_ids,
                eligible_asset_roles=eligible_asset_roles,
                eligible_asset_paths=eligible_asset_paths,
                eligible_asset_hashes=eligible_asset_hashes,
                required_asset_ids=required_asset_ids,
                minimum_required_visual_count=minimum_required_visual_count,
                expected_target_duration_s=expected_target_duration_s,
            )
            assessment = assess_delivery_issues(
                "video",
                _video_validation_issue_payloads(validation_errors),
            )
            if assessment.safety_state == "blocked":
                continue
            atomic_write_json(
                attempt_dir / "video_author_validation_errors.json",
                {
                    "errors": validation_errors,
                    "delivery_assessment": {
                        "safety_state": assessment.safety_state,
                        "quality_diagnostics": [
                            item.model_dump(mode="json")
                            for item in assessment.quality_diagnostics
                        ],
                    },
                },
            )
            delivery_result, finalize_result = self._deliver_normal_candidate(
                candidate_id=candidate.candidate_id,
                project_dir=project_dir,
                manifest=manifest,
                ctx=ctx,
            )
            if (
                delivery_result.status == "error"
            ):
                error_message = (
                    delivery_result.error_message
                    or "best available Video delivery failed"
                )
                atomic_write_json(
                    attempt_dir / "video_author_delivery_errors.json",
                    {
                        "error_message": error_message,
                        "error_category": delivery_result.error_category,
                        "payload": delivery_result.payload,
                        "acceptance_path": "best_available_artifact_fallback",
                    },
                )
                ctx.state["video_author_best_available_delivery_error"] = (
                    error_message
                )
                continue
            if finalize_result is None or finalize_result.status == "error":
                error_message = (
                    (finalize_result.error_message if finalize_result else None)
                    or "best available Video finalization failed after delivery"
                )
                atomic_write_json(
                    attempt_dir / "video_author_finalize_errors.json",
                    {
                        "error_message": error_message,
                        "error_category": (
                            finalize_result.error_category
                            if finalize_result
                            else None
                        ),
                        "payload": (
                            finalize_result.payload if finalize_result else {}
                        ),
                        "acceptance_path": "best_available_artifact_fallback",
                    },
                )
                ctx.state["video_author_best_available_delivery_error"] = (
                    error_message
                )
                return False
            self._record_delivery_success(
                ctx,
                attempt_dir=attempt_dir,
                project_dir=project_dir,
                manifest_path=manifest_path,
                delivery_result=delivery_result,
                staged_files=list(candidate.dependency_relative_paths),
                acceptance_path="best_available_artifact_fallback",
                quality_diagnostics=[
                    item.issue_id for item in assessment.quality_diagnostics
                ],
            )
            log(
                "video_author.best_available_finalized",
                mode="external",
                attempt_dir=str(attempt_dir),
                quality_diagnostics=[
                    item.issue_id for item in assessment.quality_diagnostics
                ],
            )
            return True
        return False

    @staticmethod
    def _record_delivery_success(
        ctx: ToolContext,
        *,
        attempt_dir: Path,
        project_dir: Path,
        manifest_path: Path,
        delivery_result: ToolResultRecord,
        staged_files: list[str],
        acceptance_path: str,
        quality_diagnostics: list[str],
    ) -> None:
        quality_status = (
            "ready_with_warnings" if quality_diagnostics else "ready"
        )
        ctx.state.pop("designer_api_error", None)
        ctx.state.pop("designer_contract_abort", None)
        ctx.state["video_author"] = {
            "status": "passed",
            "attempt_dir": str(attempt_dir),
            "project_dir": str(project_dir),
            "manifest_path": str(manifest_path),
            "staged_files": staged_files,
            "delivery": delivery_result.payload,
            "quality_status": quality_status,
            "quality_diagnostics": quality_diagnostics,
        }
        ctx.state["video_author_result"] = ctx.state["video_author"]
        direct_final = {
            "source": "external_video_author",
            "artifact_type": "video",
            "acceptance_path": acceptance_path,
        }
        if quality_diagnostics:
            direct_final.update({
                "quality_status": quality_status,
                "quality_diagnostics": quality_diagnostics,
            })
        ctx.state["designer_author_direct_final"] = direct_final

    def _deliver_normal_candidate(
        self,
        *,
        candidate_id: str,
        project_dir: Path,
        manifest: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[ToolResultRecord, ToolResultRecord | None]:
        original_run_dir = Path(ctx.run_dir)
        with normal_promotion_lease(
            run_dir=original_run_dir,
            candidate_id=candidate_id,
            expected_run_identity=ctx.run_directory_identity,
        ) as leased_run_dir:
            with leased_promotion_tool_context(ctx, leased_run_dir):
                def checkpoint(phase: str) -> None:
                    context_cancellation_checkpoint(ctx, phase)
                    assert_promotion_run_unchanged()

                try:
                    project_relative = project_dir.relative_to(original_run_dir)
                except ValueError as exc:
                    raise ValueError(
                        "video project must remain inside its run directory"
                    ) from exc
                checkpoint("external_video.before_delivery")
                delivery_result = deliver_authored_video_project(
                    project_dir=leased_run_dir / project_relative,
                    manifest=manifest,
                    ctx=ctx,
                )
                checkpoint("external_video.after_delivery")
                if delivery_result.status == "error":
                    return delivery_result, None
                ctx.state["artifact_type"] = "video"
                checkpoint("external_video.before_finalize")
                finalize_result = invoke_designer_tool(
                    "finalize",
                    {
                        "notes": (
                            "External video author project passed formal delivery "
                            "validation."
                        )
                    },
                    ctx,
                )
                checkpoint("external_video.after_finalize")
                return delivery_result, finalize_result

    @property
    def token_totals(self) -> tuple[int, int]:
        return self._total_in, self._total_out

    @property
    def cache_totals(self) -> tuple[int, int]:
        return self._total_cache_read, self._total_cache_create

    def _next_attempt_dir(self, ctx: ToolContext) -> Path:
        attempt = int(ctx.state.get("video_author_attempts") or 0) + 1
        ctx.state["video_author_attempts"] = attempt
        attempt_dir = ctx.run_dir / "video_author" / f"attempt_{attempt:02d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        return attempt_dir

    def _ensure_ingested(self, ctx: ToolContext, author_root: Path) -> bool:
        if _has_full_ingest_evidence(ctx):
            ctx.state["artifact_type"] = "video"
            return True

        switch_result = invoke_designer_tool(
            "switch_artifact_type",
            {"type": "video"},
            ctx,
        )
        if switch_result.status == "error":
            switch_error = str(switch_result.error_message or "").lower()
            if "invalid artifact type" in switch_error and "video" in switch_error:
                # Compatibility fallback for historical switch tools that predate video.
                ctx.state["artifact_type"] = "video"
            else:
                self._fail(
                    ctx,
                    "video_author_artifact_switch_failed",
                    switch_result.error_message or "switch_artifact_type failed",
                    author_root,
                    payload=switch_result.payload,
                )
                return False

        attachments = [str(path) for path in (ctx.state.get("attachments") or [])]
        if not attachments and not ctx.state.get("reuse_ingest_run"):
            self._fail(
                ctx,
                "video_author_missing_ingest_input",
                "external video author requires paper attachments or a reused ingest preload",
                author_root,
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
                "video_author_ingest_failed",
                ingest_result.error_message or "ingest_document failed",
                author_root,
                payload=ingest_result.payload,
            )
            return False
        if not _has_full_ingest_evidence(ctx):
            self._fail(
                ctx,
                "video_author_missing_full_ingest",
                "ingest_document did not produce paper_memory, paper_visual_provenance, "
                "and layers",
                author_root,
            )
            return False
        return True

    def _stage_inputs(
        self,
        ctx: ToolContext,
        *,
        brief: str,
        attempt_dir: Path,
        provenance: dict[str, Any],
        paper_memory: dict[str, Any],
        dossier: dict[str, Any],
        catalog: dict[str, Any],
        plan: dict[str, Any],
        previous_attempt_dir: Path | None,
    ) -> tuple[list[str], list[str]]:
        staged_files: list[str] = []
        errors: list[str] = []
        state_fallbacks = {
            "paper_memory.json": paper_memory,
            "paper_visual_provenance.json": provenance,
        }
        if dossier:
            state_fallbacks["paper_memory_dossier.json"] = dossier
        for name in _PAPER_CONTEXT_FILES:
            source = ctx.run_dir / name
            destination = attempt_dir / name
            if source.is_file():
                shutil.copy2(source, destination)
                staged_files.append(name)
            elif name in state_fallbacks:
                atomic_write_json(destination, state_fallbacks[name])
                staged_files.append(name)

        for name in _NARRATIVE_CONTEXT_FILES:
            source = ctx.run_dir / name
            if source.is_file():
                shutil.copy2(source, attempt_dir / name)
                staged_files.append(name)

        for state_key, output_name in (
            ("narrative_context", "narrative_context.json"),
            ("transcript", "transcript_context.json"),
        ):
            value = ctx.state.get(state_key)
            if value is None:
                continue
            atomic_write_json(attempt_dir / output_name, {state_key: value})
            if output_name not in staged_files:
                staged_files.append(output_name)

        layers_source = ctx.run_dir / "layers"
        if not layers_source.is_dir() and ctx.layers_dir.is_dir():
            layers_source = ctx.layers_dir
        if layers_source.is_dir():
            shutil.copytree(layers_source, attempt_dir / "layers", dirs_exist_ok=True)
            staged_files.append("layers/")
        else:
            errors.append("full ingest layers directory is missing")

        evidence_packs = ctx.run_dir / "paper_evidence_packs"
        if evidence_packs.is_dir():
            shutil.copytree(
                evidence_packs,
                attempt_dir / evidence_packs.name,
                dirs_exist_ok=True,
            )
            staged_files.append("paper_evidence_packs/")

        runtime_stage = "repair" if previous_attempt_dir is not None else "plan"
        try:
            runtime_skills = _stage_runtime_skills(
                ctx,
                attempt_dir,
                stage=runtime_stage,
            )
        except ValueError as exc:
            errors.append(str(exc))
        else:
            staged_files.extend(runtime_skills["files"])

        if previous_attempt_dir is not None:
            previous_project = previous_attempt_dir / "project"
            previous_manifest = previous_attempt_dir / "video_author_manifest.json"
            previous_delivery_errors = (
                previous_attempt_dir / "video_author_delivery_errors.json"
            )
            previous_finalize_errors = (
                previous_attempt_dir / "video_author_finalize_errors.json"
            )
            baseline_dir = attempt_dir / "repair_baseline"
            if previous_project.is_dir() and previous_manifest.is_file():
                shutil.copytree(
                    previous_project,
                    baseline_dir / "project",
                    dirs_exist_ok=True,
                )
                baseline_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(
                    previous_manifest,
                    baseline_dir / "video_author_manifest.json",
                )
                staged_files.extend([
                    "repair_baseline/project/",
                    "repair_baseline/video_author_manifest.json",
                ])
                if previous_delivery_errors.is_file():
                    shutil.copy2(
                        previous_delivery_errors,
                        baseline_dir / "video_author_delivery_errors.json",
                    )
                    staged_files.append(
                        "repair_baseline/video_author_delivery_errors.json"
                    )
                if previous_finalize_errors.is_file():
                    shutil.copy2(
                        previous_finalize_errors,
                        baseline_dir / "video_author_finalize_errors.json",
                    )
                    staged_files.append(
                        "repair_baseline/video_author_finalize_errors.json"
                    )

        atomic_write_json(attempt_dir / "video_visual_plan.json", plan)
        atomic_write_json(attempt_dir / "video_visual_asset_catalog.json", catalog)
        staged_files.extend(
            ["video_visual_plan.json", "video_visual_asset_catalog.json"]
        )

        missing_assets = [
            str(asset["asset_id"])
            for asset in catalog["assets"]
            if asset["eligibility"]["eligible"]
            if not (attempt_dir / str(asset["output_file"])).is_file()
        ]
        if missing_assets:
            errors.append(
                "eligible provenance assets are missing from staged layers: "
                + ", ".join(missing_assets[:12])
            )
        unverified_assets = [
            str(asset["asset_id"])
            for asset in catalog["assets"]
            if asset["eligibility"]["eligible"]
            if not str(asset.get("actual_sha256") or "").strip()
        ]
        if unverified_assets:
            errors.append(
                "eligible provenance assets lack trusted source hashes: "
                + ", ".join(unverified_assets[:12])
            )

        input_manifest = {
            "kind": "video_author_input_manifest",
            "version": 1,
            "run_id": ctx.run_id,
            "brief": brief,
            "evidence_source": "full_paper_ingest",
            "poster_visual_selection_used": False,
            "staged_files": staged_files,
            "eligible_asset_count": catalog["eligible_asset_count"],
            "recommended_asset_count": plan["recommended_asset_count"],
            "repair_baseline_staged": (attempt_dir / "repair_baseline").is_dir(),
            "progressive_disclosure": {
                "read_first": "video_visual_plan.json",
                "full_catalog": "video_visual_asset_catalog.json",
            },
            "output_contract": {
                "project_path": "project",
                "project_html": "project/index.html",
                "manifest": "video_author_manifest.json",
                "done_marker": "designer_author_done.json",
                "hyperframes_protocol": HYPERFRAMES_AUTHOR_PROTOCOL,
            },
        }
        atomic_write_json(attempt_dir / "video_author_input_manifest.json", input_manifest)
        return staged_files, errors

    def _build_prompt(
        self,
        *,
        brief: str,
        attempt_dir: Path,
        previous_errors: list[str],
        repair_baseline_staged: bool,
    ) -> str:
        repair = ""
        if previous_errors:
            repair = (
                "\nPrevious attempt errors to correct:\n- "
                + "\n- ".join(previous_errors)
                + "\n"
            )
        repair_instruction = ""
        if repair_baseline_staged:
            repair_instruction = (
                "This is a local repair attempt. Inspect `repair_baseline/project/` and "
                "`repair_baseline/video_author_manifest.json`, copy them into the required "
                "output locations, and patch the staged baseline first. Preserve valid "
                "content and structure; do not recreate the project from scratch. When "
                "present, read `repair_baseline/video_author_delivery_errors.json` as the "
                "failed-delivery diagnostic. If present, "
                "`repair_baseline/video_author_finalize_errors.json` is the newest "
                "finalization diagnostic and takes priority before editing. Preserve "
                "the baseline manifest's target_duration_s exactly; a repair may edit "
                "scene content and narration but must not retime the whole video.\n"
            )
        return f"""Author an editable, local HyperFrames-compatible English
conference video project.
This is an execution task. Work only inside:
{attempt_dir}

User brief:
{brief}

Read `video_author_input_manifest.json` and the compact `video_visual_plan.json`
first. Consult `video_visual_asset_catalog.json` only when more asset detail is
needed. Ground claims in paper_memory and, when present, paper_memory_dossier.
Use the full
paper_visual_provenance and staged layers; do not use poster-selected visual IDs
or paper_visual_storyboard as the video evidence source.

When `runtime_skills/index.md` is staged, read that index first. Open only the
selected artifact skill's `SKILL.md` and supporting resources as needed; do not
load unrelated skills.

{repair_instruction}

Output exactly the required author contract:
- `project/index.html`: editable 1920x1080 HyperFrames source with 10-14 scenes.
- `video_author_manifest.json`: version, language, target_duration_s,
  project_path, and ordered scenes. Every scene needs scene_id, title,
  duration_s, visual_ids, narration_intent, and subtitle_intent.
- `designer_author_done.json`: short completion summary.

HyperFrames structural protocol -- follow this literally:
- `video_author_manifest.json.project_path` MUST be exactly `"project"`.
- Keep all 10-14 scene `<section class="clip">` elements directly in
  `project/index.html`. Do not move scenes into `data-composition-src`
  sub-compositions to address file-size or track-density warnings; those
  warnings are advisory and the strict conference-video contract validates
  the ordered scenes in the root HTML.
- Put `data-composition-id` on exactly one composition root. It must also have
  `data-start="0"`, `data-duration`, `data-width="1920"`, and
  `data-height="1080"`. The default static timeline mode is the boolean
  `data-no-timeline` attribute on that root. Use a registered
  `window.__timelines` entry only when you intentionally author a seekable
  deterministic animation timeline.
- Every scene is a literal `<section id="scene_XX" class="clip" ...>` with
  `data-start`, `data-duration`, `data-track-index`, and `data-narration`.
  `data-hf-clip` is optional metadata; it never replaces `class="clip"`.
- The narration audio is a local `<audio class="clip" src="assets/narration.wav" ...>`
  with `id`, `data-start="0"`, `data-duration`, `data-track-index`, and
  `data-media-start="0"`.
- Treat the manifest as the timing source of truth: copy each ordered scene id,
  cumulative start, duration, and verbatim narration into the matching HTML
  section exactly. Do not independently retime the HTML.
- Never use `requestAnimationFrame`; it is wall-clock based and fails strict
  HyperFrames capture. Do not put `data-composition-id` on metadata or any
  second element.

Choose a total duration from 300-600 seconds based on the paper's complexity,
evidence density, and the time needed for a clear academic explanation. Record
that exact integer as target_duration_s and make the scene durations sum to it.
If this is a repair, preserve the selected baseline target exactly. Use 8-16 unique
eligible source assets across the video when available, covering method,
quantitative/results evidence, and qualitative evidence. Do not repeat an asset.
Every manifest visual_id must appear in HTML as `data-source-id` on the local
asset element. Copy each used source file into `project/assets/`; all HTML, CSS,
JS, compositions, fonts, and media dependencies must remain inside `project/`.
Use no remote URLs, network APIs, data URLs, absolute paths, or `..` references.
Use only renderer-supported system fonts, or declare every custom font with a
local `@font-face` source inside `project/`. Do not name Source Sans Pro,
Merriweather, Tiempos, or another unstaged font without a matching local font
declaration.
Treat every `narration_intent` as the canonical verbatim spoken transcript for
that scene, not as a summary or production note. Write at least 90 spoken words
per minute both per scene and across the full timeline (about 45 spoken words
for a 30-second scene). Do not satisfy this with repeated phrases, generic
filler, or empty meta-commentary. Keep speech concise enough to fit with at
most 1.25x conservative TTS compression, while targeting at least 0.72 measured
TTS speech coverage across the full video. Keep `subtitle_intent` for manifest
compatibility. Subtitles are generated from the canonical narration transcript;
do not author a separate subtitle script.
Reference `assets/narration.wav` from the required timed audio clip; AutoDesign
will generate that local file during delivery. Do not render video yourself.
{repair}
"""

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
            "type": "external_video_author",
            "reason": reason,
            "message": message,
        }
        ctx.state["video_author_result"] = {
            "status": "error",
            "reason": reason,
            "message": message,
            "attempt_dir": str(attempt_dir),
            "payload": payload or {},
        }
        log(
            "video_author.fail",
            mode="external",
            reason=reason,
            message=message[:800],
            attempt_dir=str(attempt_dir),
        )


def _manifest_target_duration(
    manifest: dict[str, Any],
) -> tuple[int | None, str | None]:
    value = manifest.get("target_duration_s")
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "video_author_manifest target_duration_s must be an integer"
    if not VIDEO_MIN_DURATION_S <= value <= VIDEO_MAX_DURATION_S:
        return None, "video_author_manifest target_duration_s must be within 300-600"
    return value, None


def _authoritative_target_from_previous_attempt(
    previous_attempt_dir: Path,
) -> tuple[int | None, str | None]:
    manifest_path = previous_attempt_dir / "video_author_manifest.json"
    if not manifest_path.is_file():
        if (previous_attempt_dir / "project").is_dir():
            return None, (
                "repair baseline has authored project files but is missing "
                "video_author_manifest.json; refusing to guess its selected duration"
            )
        return None, None
    manifest, manifest_error = _read_json_object(manifest_path)
    if manifest_error:
        return None, f"repair baseline duration is unavailable: {manifest_error}"
    target_duration_s, target_error = _manifest_target_duration(manifest)
    if target_error:
        return None, f"repair baseline duration is invalid: {target_error}"
    return target_duration_s, None


def validate_video_author_output(
    *,
    project_dir: Path,
    manifest: dict[str, Any],
    eligible_asset_ids: set[str],
    eligible_asset_roles: dict[str, str] | None = None,
    eligible_asset_paths: dict[str, Path] | None = None,
    eligible_asset_hashes: dict[str, str] | None = None,
    required_asset_ids: set[str] | None = None,
    minimum_required_visual_count: int = 0,
    expected_target_duration_s: int | None = None,
) -> list[str]:
    """Validate the authored project before any export or render side effect."""
    errors: list[str] = []
    if str(manifest.get("project_path") or "") != "project":
        errors.append("video_author_manifest project_path must be project")
    if str(manifest.get("language") or "").lower() != "en":
        errors.append("video_author_manifest language must be en")
    scenes = manifest.get("scenes")
    if not isinstance(scenes, list) or not 10 <= len(scenes) <= 14:
        errors.append("video_author_manifest must contain 10-14 scenes")
        scenes = []

    normalized_scenes: list[VideoSceneContract] = []
    used_ids: list[str] = []
    seen_scene_ids: set[str] = set()
    seen_narration: dict[str, int] = {}
    total_narration_words = 0
    cursor = 0.0
    for index, raw_scene in enumerate(scenes, start=1):
        if not isinstance(raw_scene, dict):
            errors.append(f"scene {index} must be an object")
            continue
        scene_id = str(raw_scene.get("scene_id") or "").strip()
        title = str(raw_scene.get("title") or "").strip()
        narration = str(raw_scene.get("narration_intent") or "").strip()
        narration_words = _spoken_words(narration)
        total_narration_words += len(narration_words)
        narration_signature = " ".join(word.lower() for word in narration_words)
        subtitle = str(raw_scene.get("subtitle_intent") or "").strip()
        visual_ids = raw_scene.get("visual_ids")
        if not scene_id or scene_id in seen_scene_ids:
            errors.append(f"scene {index} has a missing or duplicate scene_id")
        seen_scene_ids.add(scene_id)
        if not title:
            errors.append(f"scene {index} title is required")
        if not subtitle:
            errors.append(f"scene {index} subtitle_intent is required")
        if _looks_like_repeated_filler(narration_words):
            errors.append(
                f"scene {index} narration_intent appears to use repeated filler "
                "instead of a substantive spoken transcript"
            )
        if narration_signature:
            duplicate_index = seen_narration.get(narration_signature)
            if duplicate_index is not None:
                errors.append(
                    f"scene {index} narration_intent duplicates scene "
                    f"{duplicate_index}; repeated transcript padding is not allowed"
                )
            else:
                seen_narration[narration_signature] = index
        if not isinstance(visual_ids, list):
            errors.append(f"scene {index} visual_ids must be a list")
            visual_ids = []
        for raw_asset_id in visual_ids:
            asset_id = str(raw_asset_id).strip()
            if asset_id not in eligible_asset_ids:
                errors.append(
                    f"scene {index} references unknown source visual {asset_id or '<empty>'}"
                )
            elif asset_id in used_ids:
                errors.append(f"source visual {asset_id} is repeated without justification")
            else:
                used_ids.append(asset_id)
        try:
            duration_s = float(raw_scene.get("duration_s"))
            minimum_scene_words = _minimum_scene_word_count(duration_s)
            if len(narration_words) < minimum_scene_words:
                errors.append(
                    f"scene {index} narration_intent must be the verbatim spoken "
                    f"transcript with at least {minimum_scene_words} words for "
                    f"{duration_s:g}s at approximately 90 spoken WPM; found "
                    f"{len(narration_words)}"
                )
            normalized_scenes.append(
                VideoSceneContract(
                    scene_id=scene_id,
                    title=title,
                    start_s=cursor,
                    duration_s=duration_s,
                    narration_text=narration,
                )
            )
            cursor += duration_s
        except Exception as exc:
            errors.append(f"scene {index} timing or narration is invalid: {exc}")

    parsed_target_duration_s, target_duration_error = _manifest_target_duration(
        manifest
    )
    if target_duration_error:
        target_duration_s = 0
        errors.append(target_duration_error)
    else:
        target_duration_s = int(parsed_target_duration_s)
    if (
        expected_target_duration_s is not None
        and target_duration_s != expected_target_duration_s
    ):
        errors.append(
            "video repair must preserve selected target_duration_s "
            f"{expected_target_duration_s}; found {target_duration_s}"
        )
    if abs(cursor - target_duration_s) > 1e-6:
        errors.append("scene duration sum must equal target_duration_s")
    if target_duration_s > 0:
        minimum_total_words = math.ceil(
            target_duration_s * _MINIMUM_SPOKEN_WPM / 60
        )
        if total_narration_words < minimum_total_words:
            errors.append(
                "total narration transcript must contain at least "
                f"{minimum_total_words} words for {target_duration_s}s at 90 spoken "
                f"WPM; found {total_narration_words}"
            )

    delivery_contract: VideoDeliveryContract | None = None
    if len(normalized_scenes) == len(scenes) and not any(
        "scene duration sum" in error for error in errors
    ):
        try:
            delivery_contract = VideoDeliveryContract(
                target_duration_s=target_duration_s,
                voice_preset=str(manifest.get("voice_preset") or "female"),
                scenes=normalized_scenes,
            )
        except Exception as exc:
            errors.append(f"video delivery contract validation failed: {exc}")

    index_path = project_dir / "index.html"
    if not index_path.is_file():
        errors.append("project/index.html is required")
        return errors
    try:
        html = index_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        errors.append(f"project/index.html could not be read: {exc}")
        return errors
    errors.extend(
        validate_authored_video_html(
            html,
            delivery_contract,
            project_dir=project_dir,
        )
    )

    source_parser = _SourceVisualParser()
    try:
        source_parser.feed(html)
        source_parser.close()
    except Exception as exc:
        errors.append(f"source visual references could not be parsed: {exc}")
    missing_source_refs = sorted(set(used_ids) - source_parser.source_ids)
    if missing_source_refs:
        errors.append(
            "manifest visual_ids missing matching HTML data-source-id: "
            + ", ".join(missing_source_refs)
        )
    unknown_html_refs = sorted(source_parser.source_ids - eligible_asset_ids)
    if unknown_html_refs:
        errors.append(
            "HTML references source visuals outside full provenance: "
            + ", ".join(unknown_html_refs)
        )

    if eligible_asset_paths or eligible_asset_hashes:
        for asset_id in sorted(set(used_ids)):
            references = source_parser.media_references.get(asset_id, [])
            matched = False
            for reference in references:
                if not reference["visible"]:
                    continue
                resource_path = _local_project_resource(
                    project_dir, str(reference["resource"])
                )
                if resource_path is None or not resource_path.is_file():
                    continue
                trusted_hash = str(
                    (eligible_asset_hashes or {}).get(asset_id) or ""
                ).strip().lower()
                if trusted_hash:
                    if _file_sha256(resource_path) == trusted_hash:
                        matched = True
                        break
                    continue
                catalog_path = (eligible_asset_paths or {}).get(asset_id)
                if catalog_path is not None and catalog_path.is_file() and (
                    _file_sha256(resource_path) == _file_sha256(catalog_path)
                ):
                    matched = True
                    break
            if not matched:
                if references:
                    expected_source = (
                        "trusted catalog hash"
                        if eligible_asset_hashes is not None
                        else "catalog source"
                    )
                    errors.append(
                        f"source visual {asset_id} visible local media does not match "
                        f"{expected_source}"
                    )
                else:
                    errors.append(
                        f"source visual {asset_id} requires visible local media "
                        "(img, object, or video) bound to its catalog source"
                    )

    formal_ids = required_asset_ids
    if formal_ids is None:
        formal_ids = set(eligible_asset_roles or eligible_asset_ids)
    required_count = max(0, int(minimum_required_visual_count or 0))
    used_formal_ids = set(used_ids) & formal_ids
    if len(used_formal_ids) < required_count:
        errors.append(
            f"video requires at least {required_count} unique formal eligible source "
            f"visuals; found {len(used_formal_ids)}"
        )

    if eligible_asset_roles:
        available_roles = set(eligible_asset_roles.values())
        used_roles = {
            eligible_asset_roles[asset_id]
            for asset_id in used_ids
            if asset_id in eligible_asset_roles
        }
        for required_role in ("method", "results", "qualitative"):
            if required_role in available_roles and required_role not in used_roles:
                errors.append(f"video is missing {required_role} source visual coverage")
    return _dedupe(errors)


def _video_validation_issue_payloads(
    validation_errors: list[str],
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for raw_message in validation_errors:
        message = str(raw_message)
        issue_id = "video_author_validation"
        delivery_class = "hard"
        for pattern, quality_issue_id in _VIDEO_QUALITY_ERROR_PATTERNS:
            if pattern.search(message):
                issue_id = quality_issue_id
                delivery_class = "quality"
                break
        scene_match = re.search(r"\bscene\s+(\d+)\b", message, re.IGNORECASE)
        if scene_match and delivery_class == "quality":
            issue_id = f"{issue_id}_scene_{scene_match.group(1)}"
        issues.append({
            "issue_id": issue_id,
            "message": message,
            "delivery_class": delivery_class,
        })
    return issues


def _trusted_video_source_context(
    run_dir: Path,
    *,
    eligible_assets: list[dict[str, Any]],
    eligible_asset_roles: dict[str, str],
    required_asset_ids: set[str],
    minimum_required_visual_count: int,
) -> dict[str, Any]:
    eligible_ids = sorted({
        str(asset.get("asset_id") or "").strip()
        for asset in eligible_assets
        if str(asset.get("asset_id") or "").strip()
    })
    eligible_id_set = set(eligible_ids)
    eligible_hashes = {
        str(asset.get("asset_id") or "").strip(): str(
            asset.get("actual_sha256") or ""
        ).strip().lower()
        for asset in eligible_assets
        if str(asset.get("asset_id") or "").strip()
    }
    if set(eligible_hashes) != eligible_id_set or any(
        not re.fullmatch(r"[0-9a-f]{64}", value)
        for value in eligible_hashes.values()
    ):
        raise ValueError(
            "video eligible source assets could not be bound to trusted bytes"
        )
    if not set(eligible_asset_roles).issubset(eligible_id_set):
        raise ValueError("video source roles are outside eligible provenance")
    normalized_roles = {
        asset_id: str(eligible_asset_roles[asset_id])
        for asset_id in sorted(eligible_asset_roles)
    }
    normalized_required_ids = sorted(required_asset_ids)
    if not set(normalized_required_ids).issubset(eligible_id_set):
        raise ValueError("video required source assets are outside eligible provenance")
    minimum_count = int(minimum_required_visual_count)
    if minimum_count < 0 or minimum_count > len(normalized_required_ids):
        raise ValueError("video minimum source coverage is invalid")
    expected = {
        "kind": "video_trusted_source_context",
        "version": 1,
        "source": "pre_author_actual_source_bytes",
        "eligible_asset_ids": eligible_ids,
        "eligible_asset_roles": normalized_roles,
        "eligible_asset_hashes": {
            asset_id: eligible_hashes[asset_id]
            for asset_id in eligible_ids
        },
        "required_asset_ids": normalized_required_ids,
        "minimum_required_visual_count": minimum_count,
    }
    anchor_path = run_dir / "video_trusted_source_context.json"
    if anchor_path.is_file():
        current = _load_trusted_video_source_context(run_dir)
        if current != expected:
            raise ValueError(
                "video source context disagrees with the original trusted anchor"
            )
        return current
    atomic_write_json(anchor_path, expected)
    anchor_path.chmod(0o444)
    return expected


def _load_trusted_video_source_context(run_dir: Path) -> dict[str, Any]:
    anchor_path = run_dir / "video_trusted_source_context.json"
    anchor, error = _read_json_object(anchor_path)
    if error:
        raise ValueError(f"video trusted source context is invalid: {error}")
    eligible_ids = anchor.get("eligible_asset_ids")
    roles = anchor.get("eligible_asset_roles")
    hashes = anchor.get("eligible_asset_hashes")
    required_ids = anchor.get("required_asset_ids")
    minimum_count = anchor.get("minimum_required_visual_count")
    if (
        anchor.get("kind") != "video_trusted_source_context"
        or anchor.get("version") != 1
        or anchor.get("source") != "pre_author_actual_source_bytes"
        or not isinstance(eligible_ids, list)
        or not isinstance(roles, dict)
        or not isinstance(hashes, dict)
        or not isinstance(required_ids, list)
        or type(minimum_count) is not int
    ):
        raise ValueError("video trusted source context fields are invalid")
    normalized_ids = [str(asset_id).strip() for asset_id in eligible_ids]
    normalized_required_ids = [str(asset_id).strip() for asset_id in required_ids]
    if (
        any(not asset_id for asset_id in normalized_ids)
        or len(normalized_ids) != len(set(normalized_ids))
        or any(not asset_id for asset_id in normalized_required_ids)
        or len(normalized_required_ids) != len(set(normalized_required_ids))
        or not set(normalized_required_ids).issubset(normalized_ids)
        or minimum_count < 0
        or minimum_count > len(normalized_required_ids)
    ):
        raise ValueError("video trusted source context coverage is invalid")
    normalized_hashes = {
        str(asset_id).strip(): str(value).strip().lower()
        for asset_id, value in hashes.items()
    }
    if set(normalized_hashes) != set(normalized_ids) or any(
        not re.fullmatch(r"[0-9a-f]{64}", value)
        for value in normalized_hashes.values()
    ):
        raise ValueError("video trusted source hashes are invalid")
    normalized_roles = {
        str(asset_id).strip(): str(value).strip()
        for asset_id, value in roles.items()
    }
    if (
        any(not asset_id or not value for asset_id, value in normalized_roles.items())
        or not set(normalized_roles).issubset(normalized_ids)
    ):
        raise ValueError("video trusted source roles are invalid")
    return {
        "kind": "video_trusted_source_context",
        "version": 1,
        "source": "pre_author_actual_source_bytes",
        "eligible_asset_ids": normalized_ids,
        "eligible_asset_roles": normalized_roles,
        "eligible_asset_hashes": normalized_hashes,
        "required_asset_ids": normalized_required_ids,
        "minimum_required_visual_count": minimum_count,
    }


def _delivery_failure_is_repairable(result: ToolResultRecord) -> bool:
    payload = result.payload if isinstance(result.payload, dict) else {}
    message = str(result.error_message or "").lower()
    tts_output = str(payload.get("tts_output") or "").lower()
    lint_output = str(payload.get("lint_output") or "").lower()
    if payload.get("delivery_failure_kind") in {
        "narration_timing_unfit",
        "subtitle_readability_failed",
        "subtitle_generation_failed",
    }:
        return True
    if message.startswith("narration_timing_unfit ") or tts_output.startswith(
        "narration_timing_unfit "
    ):
        return True
    if any(
        token in message
        for token in (
            "required speech speed",
            "fitted speech duration",
            "speech coverage",
        )
    ):
        return True
    render_output = str(payload.get("render_output") or "").lower()
    if payload.get("render_ok") is False or "render failed delivery validation" in message:
        infrastructure_markers = (
            "ffmpeg executable missing",
            "ffprobe executable missing",
            "ffmpeg executable not found",
            "ffprobe executable not found",
            "failed to start ffmpeg",
            "failed to start ffprobe",
            "cannot start ffmpeg",
            "cannot start ffprobe",
            "ffprobe not found",
            "permission denied",
            "operation not permitted",
            "ffprobe timed out",
        )
        if any(
            token in render_output or token in message
            for token in infrastructure_markers
        ):
            return False
        return True
    if payload.get("tts_ok") is False or any(
        token in message
        for token in (
            "kokoro",
            "tts ",
            "provider unavailable",
            "provider error",
        )
    ):
        return False
    if (
        "audio_src_not_found" in lint_output
        and "assets/narration.wav" in lint_output
    ):
        return False
    if payload.get("lint_ok") is False and any(
        token in lint_output
        for token in (
            "cli is missing",
            "command not found",
            "no such file",
            "timed out",
            "failed to start",
            "permission denied",
            "operation not permitted",
        )
    ):
        return False
    if payload.get("lint_ok") is False or "hyperframes lint failed" in message:
        return True
    return bool(
        payload.get("composer_skipped")
        and (
            "invalid pre-authored project" in message
            or "composition failed" in message
        )
    )


def _delivery_repair_feedback(result: ToolResultRecord) -> list[str]:
    """Return only actionable delivery errors, excluding advisory lint warnings."""
    payload = result.payload if isinstance(result.payload, dict) else {}
    lint_output = str(
        payload.get("lint_output")
        or payload.get("authoring_lint_output")
        or ""
    ).strip()
    lint_errors = _hyperframes_lint_errors(lint_output)
    if lint_errors:
        return [
            "Formal delivery lint error to repair: " + error
            for error in lint_errors
        ]
    message = str(result.error_message or "formal video delivery failed").strip()
    return ["Formal delivery validation failed and must be repaired: " + message]


def _hyperframes_lint_errors(lint_output: str) -> list[str]:
    errors: list[str] = []
    active_error: list[str] | None = None
    for raw_line in str(lint_output or "").splitlines():
        line = raw_line.strip()
        if line.startswith(("✗", "×")):
            if active_error:
                errors.append(" ".join(active_error))
            active_error = [line[1:].strip()]
            continue
        if line.startswith(("⚠", "◆", "◇")):
            if active_error:
                errors.append(" ".join(active_error))
                active_error = None
            continue
        if active_error is not None and line.startswith("Fix:"):
            active_error.append(line)
    if active_error:
        errors.append(" ".join(active_error))
    return _dedupe([error for error in errors if error])


def _local_project_resource(project_dir: Path, raw_resource: str) -> Path | None:
    resource = str(raw_resource or "").strip()
    parsed = urlparse(resource)
    if (
        not resource
        or parsed.scheme
        or parsed.netloc
        or resource.startswith("//")
    ):
        return None
    relative = Path(unquote(parsed.path))
    if relative.is_absolute() or ".." in relative.parts:
        return None
    root = project_dir.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        return None
    return candidate


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture_video_attempt_candidate(
    *,
    ctx: ToolContext,
    attempt_dir: Path,
    attempt: int,
    max_attempts: int,
    manifest: dict[str, Any],
    validation_errors: list[str],
) -> AttemptCandidate:
    project_dir = attempt_dir / "project"
    source_html = project_dir / "index.html"
    validation_path = attempt_dir / "video_author_validation_errors.json"
    atomic_write_json(validation_path, {"errors": validation_errors})
    manifest_path = attempt_dir / "video_author_manifest.json"
    if manifest and not manifest_path.is_file():
        atomic_write_json(manifest_path, manifest)
    if manifest_path.is_file() and project_dir.is_dir():
        shutil.copy2(manifest_path, project_dir / manifest_path.name)

    preview_path = attempt_dir / "attempt_preview.png"
    preview_paths: list[str] = []
    if source_html.is_file():
        try:
            screenshot_html(
                source_html,
                preview_path,
                viewport_width=1920,
                viewport_height=1080,
                full_page=False,
                prime_local_media=True,
                max_edge=1920,
            )
        except Exception as exc:  # noqa: BLE001
            log(
                "attempt_candidate.preview_failed",
                artifact_type="video",
                attempt=attempt,
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
    if preview_path.is_file():
        preview_paths.append("attempt_preview.png")

    root = attempt_dir.resolve()
    dependencies: list[str] = []
    for path in sorted(project_dir.rglob("*")):
        if not path.is_file() or path.resolve() == source_html.resolve():
            continue
        relative_to_project = path.relative_to(project_dir).as_posix()
        if (
            relative_to_project == "assets/narration.wav"
            or path.suffix.lower() in {".mp4", ".srt", ".vtt"}
            or path.name in _PROTECTED_DELIVERY_FILES
        ):
            continue
        dependencies.append(path.resolve().relative_to(root).as_posix())
    for name in ("video_author_manifest.json", "designer_author_done.json"):
        path = attempt_dir / name
        if path.is_file():
            dependencies.append(name)
    browser_resource_paths = [
        path for path in dependencies if is_browser_preview_resource_path(path)
    ]

    assessment = assess_delivery_issues(
        "video",
        _video_validation_issue_payloads(validation_errors),
    )
    candidate = capture_attempt_candidate(
        run_dir=ctx.run_dir,
        attempt_dir=attempt_dir,
        artifact_type="video",
        attempt=attempt,
        max_attempts=max_attempts,
        source_path="project/index.html",
        dependency_paths=sorted(set(dependencies)),
        preview_paths=preview_paths,
        validation_summary_path="video_author_validation_errors.json",
        safety_state=assessment.safety_state,
        hard_blockers=list(assessment.hard_blockers),
        warnings=list(assessment.quality_diagnostics),
        browser_resource_paths=browser_resource_paths,
    )
    log(
        "attempt_candidate.available",
        run_id=ctx.run_id,
        artifact_type="video",
        attempt=attempt,
        max_attempts=max_attempts,
        candidate_id=candidate.candidate_id,
        safety_state=candidate.safety_state,
    )
    return candidate


def materialize_selected_attempt_for_editing(
    ctx: ToolContext,
    candidate: AttemptCandidate,
) -> None:
    """Materialize an authored Video project without starting MP4 delivery."""

    checkpoint = lambda phase: context_cancellation_checkpoint(ctx, phase)
    checkpoint("external_video.materialize_selected.start")
    assert_promotion_allowed(
        run_dir=ctx.run_dir,
        candidate_id=candidate.candidate_id,
    )
    source_html = ctx.run_dir / candidate.source_relative_path
    snapshot_root = source_html.parent.parent
    final_dir = ctx.run_dir / "final"
    checkpoint("external_video.materialize_selected.before_staging")
    staging_dir = Path(
        tempfile.mkdtemp(prefix=".video-final-staging-", dir=ctx.run_dir)
    )
    try:
        shutil.copytree(snapshot_root, staging_dir, dirs_exist_ok=True)
        checkpoint("external_video.materialize_selected.after_copy")

        project_html = staging_dir / "project" / "index.html"
        editable_html = staging_dir / "deck.html"
        shutil.copy2(project_html, editable_html)
        ensure_editable_html_contract(editable_html, "video")
        shutil.copy2(editable_html, project_html)
        checkpoint("external_video.materialize_selected.after_editable_contract")

        preview_path = staging_dir / "preview.png"
        render = screenshot_html(
            editable_html,
            preview_path,
            viewport_width=1920,
            viewport_height=1080,
            full_page=False,
            prime_local_media=True,
            max_edge=1920,
        )
        checkpoint("external_video.materialize_selected.after_preview")
        if not preview_path.is_file():
            raise RuntimeError("Video attempt materializer produced no preview image")
        atomic_write_json(
            staging_dir / "video_candidate_draft_manifest.json",
            {
                "artifact_type": "video",
                "render_mode": "candidate_video_html",
                "source": "external_video_author",
                "acceptance_path": "candidate_draft",
                "attempt": candidate.attempt,
                "source_candidate_id": candidate.candidate_id,
                "preview": {
                    "path": str(final_dir / "preview.png"),
                    "backend": getattr(render, "backend", ""),
                    "warnings": list(getattr(render, "warnings", None) or []),
                },
            },
        )
        checkpoint("external_video.materialize_selected.before_publish")
        _replace_video_candidate_final(
            staging_dir,
            final_dir,
            post_publish=lambda: checkpoint(
                "external_video.materialize_selected.after_publish"
            ),
        )
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    editable_html = final_dir / "deck.html"
    preview_path = final_dir / "preview.png"
    checkpoint("external_video.materialize_selected.before_state")
    ctx.state["artifact_type"] = "video"
    ctx.state["video_author_result"] = {
        "status": "draft",
        "mode": "candidate_video_html",
        "attempt": candidate.attempt,
        "final_html": str(editable_html),
        "final_preview": str(preview_path),
    }


def promote_selected_attempt(
    ctx: ToolContext,
    candidate: AttemptCandidate,
) -> None:
    checkpoint = lambda phase: context_cancellation_checkpoint(ctx, phase)
    checkpoint("external_video.promote_selected.start")
    assert_promotion_allowed(
        run_dir=ctx.run_dir,
        candidate_id=candidate.candidate_id,
    )
    source_html = ctx.run_dir / candidate.source_relative_path
    snapshot_root = source_html.parent.parent
    candidate_root = (
        ctx.run_dir / "attempt_selection_work" / candidate.candidate_id
    )
    checkpoint("external_video.promote_selected.before_copy")
    shutil.rmtree(candidate_root, ignore_errors=True)
    shutil.copytree(snapshot_root, candidate_root)
    checkpoint("external_video.promote_selected.after_copy")
    project_dir = candidate_root / "project"
    manifest, manifest_error = _read_json_object(
        candidate_root / "video_author_manifest.json"
    )
    if manifest_error:
        raise RuntimeError(manifest_error)

    checkpoint("external_video.promote_selected.before_delivery_state")
    transition_selection(ctx.run_dir, "delivering")
    checkpoint("external_video.promote_selected.before_delivery")
    delivery_result = deliver_authored_video_project(
        project_dir=project_dir,
        manifest=manifest,
        ctx=ctx,
    )
    checkpoint("external_video.promote_selected.after_delivery")
    if delivery_result.status == "error":
        raise RuntimeError(
            delivery_result.error_message or "selected video delivery failed"
        )
    checkpoint("external_video.promote_selected.before_finalize")
    finalize_result = invoke_designer_tool(
        "finalize",
        {"notes": "User selected authored Video candidate."},
        ctx,
    )
    checkpoint("external_video.promote_selected.after_finalize")
    if finalize_result.status == "error":
        raise RuntimeError(
            finalize_result.error_message
            or "selected video finalization failed after delivery passed"
        )
    checkpoint("external_video.promote_selected.before_state")
    ctx.state["artifact_type"] = "video"
    ctx.state["video_author"] = {
        "status": "passed",
        "attempt_dir": str(candidate_root),
        "project_dir": str(project_dir),
        "manifest_path": str(candidate_root / "video_author_manifest.json"),
        "delivery": delivery_result.payload,
    }
    ctx.state["video_author_result"] = ctx.state["video_author"]
    ctx.state["designer_author_direct_final"] = {
        "source": "external_video_author",
        "artifact_type": "video",
        "acceptance_path": "user_selected_attempt",
    }


def _replace_video_candidate_final(
    staging_dir: Path,
    final_dir: Path,
    *,
    post_publish: Any,
) -> None:
    publish_artifact_directory(
        staging_dir,
        final_dir,
        artifact_name="video",
        post_publish=post_publish,
    )


def _recover_video_final_promotion(final_dir: Path) -> None:
    recover_artifact_promotion(final_dir, artifact_name="video")


def deliver_authored_video_project(
    *,
    project_dir: Path,
    manifest: dict[str, Any],
    ctx: ToolContext,
) -> ToolResultRecord:
    """Hand a pre-authored project through the existing export_video gates."""
    scenes = _normalized_manifest_scenes(manifest)
    spec_result = invoke_designer_tool(
        "propose_design_spec",
        _video_design_spec(scenes).model_dump(mode="json"),
        ctx,
    )
    if spec_result.status == "error":
        return spec_result
    _ensure_rendered_layers_from_provenance(ctx)

    source_project = project_dir.resolve()

    class PreauthoredProjectComposer:
        def __init__(self, settings: Any, system_prompt: str):
            self.settings = settings

        def compose(
            self,
            composer_context: str,
            proj_dir: Path,
            delivery_contract: Any | None = None,
        ) -> ComposerResult:
            _copy_authored_project(source_project, proj_dir)
            html = (proj_dir / "index.html").read_text(encoding="utf-8")
            errors = validate_authored_video_html(
                html,
                delivery_contract,
                project_dir=proj_dir,
            )
            if errors:
                return ComposerResult(
                    index_html="",
                    proj_dir=proj_dir,
                    model="external-coding-agent",
                    skipped=True,
                    skip_reason="invalid pre-authored project: " + "; ".join(errors),
                )
            return ComposerResult(
                index_html=html,
                proj_dir=proj_dir,
                model="external-coding-agent",
            )

    export_globals = dict(_export_video.__globals__)
    export_globals["HyperFramesComposer"] = PreauthoredProjectComposer
    isolated_export = FunctionType(
        _export_video.__code__,
        export_globals,
        name=_export_video.__name__,
        argdefs=_export_video.__defaults__,
        closure=_export_video.__closure__,
    )
    isolated_export.__kwdefaults__ = _export_video.__kwdefaults__
    original_settings = ctx.settings
    ctx.settings = _SettingsOverlay(original_settings, enable_video_composer=True)
    try:
        result = isolated_export(
            {
                "video_id": f"external-{ctx.run_id}",
                "tone": "academic",
                "duration_s": int(manifest["target_duration_s"]),
                "n_scenes": len(scenes),
                "voice_preset": str(manifest.get("voice_preset") or "female"),
            },
            ctx=ctx,
        )
    finally:
        ctx.settings = original_settings
    return _enforce_delivery_speech_coverage(
        result,
        manifest=manifest,
        ctx=ctx,
    )


def _minimum_scene_word_count(duration_s: float) -> int:
    exact = duration_s * _MINIMUM_SPOKEN_WPM / 60
    return max(1, math.ceil(exact - _SCENE_WORD_TOLERANCE))


def _spoken_words(text: str) -> list[str]:
    return _SPOKEN_WORD_RE.findall(str(text or ""))


def _looks_like_repeated_filler(words: list[str]) -> bool:
    if len(words) < 20:
        return False
    normalized = [word.lower() for word in words]
    counts: dict[str, int] = {}
    for word in normalized:
        counts[word] = counts.get(word, 0) + 1
    unique_ratio = len(counts) / len(normalized)
    dominant_ratio = max(counts.values()) / len(normalized)
    return unique_ratio < 0.35 or dominant_ratio > 0.20


def _enforce_delivery_speech_coverage(
    result: ToolResultRecord,
    *,
    manifest: dict[str, Any],
    ctx: ToolContext,
) -> ToolResultRecord:
    payload = dict(result.payload or {})
    if result.status != "ok" or payload.get("tts_ok") is not True:
        return result

    metrics, error = _measured_speech_coverage(
        payload=payload,
        manifest=manifest,
        run_dir=ctx.run_dir,
    )
    if metrics:
        payload.update(metrics)
    if error:
        _invalidate_failed_video_delivery_state(ctx)
        return ToolResultRecord(
            status="error",
            error_message=error,
            error_category="validation",
            payload=payload,
        )
    return result.model_copy(update={"payload": payload})


def _measured_speech_coverage(
    *,
    payload: dict[str, Any],
    manifest: dict[str, Any],
    run_dir: Path,
) -> tuple[dict[str, Any], str]:
    project_dir = _local_project_resource(
        run_dir,
        str(payload.get("project_dir") or ""),
    )
    if project_dir is None or not project_dir.is_dir():
        return {}, "video delivery speech coverage requires a local project directory"
    timing_path = _local_project_resource(
        project_dir,
        str(payload.get("narration_timing_path") or ""),
    )
    if timing_path is None or not timing_path.is_file():
        return {}, "video delivery speech coverage requires measured TTS timing"
    try:
        timing = json.loads(timing_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, f"video delivery speech coverage timing is invalid: {exc}"
    if not isinstance(timing, list):
        return {}, "video delivery speech coverage timing must be a list"

    expected_scene_ids = [
        str(scene.get("scene_id") or "")
        for scene in manifest.get("scenes") or []
        if isinstance(scene, dict)
    ]
    measured_by_scene: dict[str, float] = {}
    for item in timing:
        if not isinstance(item, dict):
            return {}, "video delivery speech coverage timing entries must be objects"
        scene_id = str(item.get("scene_id") or "").strip()
        if not scene_id or scene_id in measured_by_scene:
            return {}, "video delivery speech coverage timing has duplicate scene ids"
        try:
            speech_duration_s = float(item.get("speech_duration_s"))
        except (TypeError, ValueError):
            return {}, f"video delivery speech duration is invalid for {scene_id}"
        if speech_duration_s <= 0:
            return {}, f"video delivery speech duration must be positive for {scene_id}"
        measured_by_scene[scene_id] = speech_duration_s

    if set(measured_by_scene) != set(expected_scene_ids):
        return {}, "video delivery speech coverage requires measured timing for every scene"

    delivery_manifest_path = _local_project_resource(
        project_dir,
        str(payload.get("delivery_manifest_path") or ""),
    )
    if delivery_manifest_path is None or not delivery_manifest_path.is_file():
        return {}, "video delivery speech coverage requires the export delivery manifest"
    delivery_manifest, delivery_error = _read_json_object(delivery_manifest_path)
    if delivery_error:
        return {}, f"video delivery speech coverage manifest is invalid: {delivery_error}"

    media_probe = payload.get("media_probe")
    if not isinstance(media_probe, dict):
        media_probe = delivery_manifest.get("media_probe")
    if not isinstance(media_probe, dict):
        return {}, "video delivery speech coverage requires the actual media probe"
    try:
        coverage_duration_s = float(media_probe.get("duration_s"))
    except (TypeError, ValueError):
        return {}, "video delivery media probe duration is invalid"
    if not math.isfinite(coverage_duration_s) or coverage_duration_s <= 0:
        return {}, "video delivery media probe duration must be positive"

    speech_duration_s = sum(measured_by_scene.values())
    speech_coverage_ratio = speech_duration_s / coverage_duration_s
    exported_metrics: dict[str, float | int] = {}
    for key in (
        "speech_duration_s",
        "coverage_duration_s",
        "speech_coverage_ratio",
        "minimum_speech_coverage_ratio",
        "measured_speech_scene_count",
    ):
        try:
            exported_metrics[key] = (
                int(payload[key])
                if key == "measured_speech_scene_count"
                else float(payload[key])
            )
        except (KeyError, TypeError, ValueError):
            return {}, f"video export is missing valid {key}"

    if (
        abs(float(exported_metrics["speech_duration_s"]) - speech_duration_s) > 1e-3
        or abs(
            float(exported_metrics["coverage_duration_s"]) - coverage_duration_s
        ) > 1e-3
        or abs(
            float(exported_metrics["speech_coverage_ratio"]) - speech_coverage_ratio
        ) > 1e-6
        or int(exported_metrics["measured_speech_scene_count"])
        != len(measured_by_scene)
        or float(exported_metrics["minimum_speech_coverage_ratio"])
        != _MINIMUM_SPEECH_COVERAGE_RATIO
    ):
        return {}, "video export speech coverage metrics do not match actual media timing"

    for key, expected in exported_metrics.items():
        try:
            observed = (
                int(delivery_manifest[key])
                if key == "measured_speech_scene_count"
                else float(delivery_manifest[key])
            )
        except (KeyError, TypeError, ValueError):
            return {}, f"video delivery manifest is missing valid {key}"
        tolerance = 0 if key == "measured_speech_scene_count" else 1e-6
        if abs(float(observed) - float(expected)) > tolerance:
            return {}, f"video delivery manifest {key} does not match export payload"

    metrics = {
        "speech_duration_s": float(exported_metrics["speech_duration_s"]),
        "coverage_duration_s": float(exported_metrics["coverage_duration_s"]),
        "speech_coverage_ratio": float(exported_metrics["speech_coverage_ratio"]),
        "minimum_speech_coverage_ratio": float(
            exported_metrics["minimum_speech_coverage_ratio"]
        ),
        "measured_speech_scene_count": int(
            exported_metrics["measured_speech_scene_count"]
        ),
    }
    if speech_coverage_ratio < _MINIMUM_SPEECH_COVERAGE_RATIO:
        return metrics, (
            f"actual TTS speech coverage {speech_coverage_ratio:.3f} is below "
            f"the required {_MINIMUM_SPEECH_COVERAGE_RATIO:.2f}"
        )
    return metrics, ""


def _invalidate_failed_video_delivery_state(ctx: ToolContext) -> None:
    ctx.state.pop("video_delivery", None)
    last_payload = ctx.state.get("last_composite_payload")
    if isinstance(last_payload, dict) and last_payload.get("artifact_type") == "video":
        ctx.state.pop("last_composite_payload", None)
    composition = ctx.state.get("composition")
    layer_manifest = getattr(composition, "layer_manifest", None)
    if isinstance(layer_manifest, list) and any(
        isinstance(item, dict) and item.get("kind") == "video"
        for item in layer_manifest
    ):
        ctx.state.pop("composition", None)


class _SourceVisualParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.source_ids: set[str] = set()
        self.media_references: dict[str, list[dict[str, Any]]] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized = {name.lower(): value or "" for name, value in attrs}
        source_id = normalized.get("data-source-id", "").strip()
        if source_id:
            self.source_ids.add(source_id)
        if not source_id or tag.lower() not in {"img", "object", "video"}:
            return
        resource = normalized.get("data" if tag.lower() == "object" else "src", "")
        style = normalized.get("style", "").lower().replace(" ", "")
        hidden = (
            "hidden" in normalized
            or normalized.get("aria-hidden", "").lower() == "true"
            or "display:none" in style
            or "visibility:hidden" in style
            or "opacity:0" in style
            or normalized.get("width", "").strip() in {"0", "0px"}
            or normalized.get("height", "").strip() in {"0", "0px"}
        )
        self.media_references.setdefault(source_id, []).append({
            "tag": tag.lower(),
            "resource": resource.strip(),
            "visible": not hidden and bool(resource.strip()),
        })


class _SettingsOverlay:
    def __init__(self, settings: Any, **overrides: Any):
        self._settings = settings
        self._overrides = overrides

    def __getattr__(self, name: str) -> Any:
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._settings, name)


def _load_context_json(ctx: ToolContext, state_key: str) -> dict[str, Any]:
    state_value = ctx.state.get(state_key)
    if isinstance(state_value, dict):
        return state_value
    path = ctx.run_dir / f"{state_key}.json"
    value, _ = _read_json_object(path)
    return value


def _has_full_ingest_evidence(ctx: ToolContext) -> bool:
    layers_available = (ctx.run_dir / "layers").is_dir() or ctx.layers_dir.is_dir()
    return bool(
        _load_context_json(ctx, "paper_memory")
        and _load_context_json(ctx, "paper_visual_provenance")
        and layers_available
    )


def _read_json_object(path: Path) -> tuple[dict[str, Any], str]:
    if not path.is_file():
        return {}, f"{path.name} is required"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, f"{path.name} is not valid JSON: {exc}"
    if not isinstance(value, dict):
        return {}, f"{path.name} must contain a JSON object"
    return value, ""


def _stage_runtime_skills(
    ctx: ToolContext,
    attempt_dir: Path,
    *,
    stage: str,
) -> dict[str, Any]:
    """Stage only hash-verified runtime skill content for the active phase."""
    snapshot_root = ctx.run_dir / "runtime_skills"
    if not snapshot_root.exists():
        if ctx.state.get("legacy_runtime_skills_compat") is True:
            return {
                "files": [],
                "catalog": {
                    "stage": stage,
                    "available": False,
                    "legacy_compat": True,
                },
            }
        raise ValueError(
            "runtime skill snapshot is required and must contain an active-stage skill pack"
        )
    snapshot, error = _read_json_object(snapshot_root / "snapshot.json")
    if error:
        raise ValueError("runtime skill snapshot is missing or unreadable")
    if int(snapshot.get("version") or 0) != 2:
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

    staged_root = attempt_dir / "runtime_skills"
    entries: list[dict[str, Any]] = []
    for item in selected:
        if not isinstance(item, dict):
            raise ValueError("runtime skill snapshot contains an invalid selected entry")
        skill_id = str(item.get("id") or "").strip()
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", skill_id)
        pack_path = f"packs/{safe_name}"
        if not skill_id or str(item.get("pack_path") or "") != pack_path:
            raise ValueError(f"unsafe runtime skill snapshot pack path for {skill_id!r}")
        pack = bundle.get(skill_id)
        if pack is None:
            raise ValueError(f"runtime skill snapshot pack is unavailable: {skill_id}")
        canonical_root = (snapshot_root / pack_path).resolve()
        if pack.root.resolve() != canonical_root:
            raise ValueError(f"runtime skill snapshot root mismatch: {skill_id}")
        if not pack.verify_integrity():
            raise ValueError(f"runtime skill snapshot hash mismatch: {skill_id}")
        if stage not in pack.manifest.stages:
            continue
        skeleton = pack.render(stage)
        if not skeleton:
            raise ValueError(f"runtime skill stage skeleton is missing: {skill_id}:{stage}")
        target_root = staged_root / pack_path
        target_root.mkdir(parents=True, exist_ok=True)
        (target_root / "SKILL.md").write_text(
            skeleton.rstrip() + "\n",
            encoding="utf-8",
        )
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
            if not target.is_relative_to(target_root.resolve()):
                raise ValueError(f"runtime skill resource escapes staged pack: {resource.path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            resources.append({
                "id": resource.id,
                "path": str((Path(pack_path) / relative).as_posix()),
                "description": resource.description,
                "when_to_read": resource.when_to_read,
            })
        entries.append({
            "id": skill_id,
            "path": str((Path(pack_path) / "SKILL.md").as_posix()),
            "resources": resources,
        })

    if not entries:
        if ctx.state.get("legacy_runtime_skills_compat") is True:
            return {
                "files": [],
                "catalog": {
                    "stage": stage,
                    "available": False,
                    "legacy_compat": True,
                },
            }
        raise ValueError(
            "runtime skill snapshot is required and must contain an active-stage skill pack"
        )
    index_lines = [
        "# Runtime Skills",
        "",
        f"Read this index first. Active stage: {stage}.",
        "Resources are staged files; read only those needed for the current task.",
        "",
    ]
    for entry in entries:
        index_lines.append(f"- `{entry['path']}`: {entry['id']} compact operating guidance.")
        index_lines.extend(
            f"  - `{resource['path']}`: {resource['description']} "
            f"{resource['when_to_read']}".rstrip()
            for resource in entry["resources"]
        )
    staged_root.mkdir(parents=True, exist_ok=True)
    (staged_root / "index.md").write_text(
        "\n".join(index_lines) + "\n",
        encoding="utf-8",
    )
    _make_tree_files_read_only(staged_root)
    files = sorted(
        str(path.relative_to(attempt_dir))
        for path in staged_root.rglob("*")
        if path.is_file()
    )
    return {
        "files": files,
        "catalog": {
            "stage": stage,
            "available": True,
            "index": "runtime_skills/index.md",
            "skills": entries,
            "source": "run_snapshot",
        },
    }


def _invoke_author_command(
    command: str,
    *,
    prompt: str,
    attempt_dir: Path,
    timeout_s: int,
    settings: Any,
    run_id: str = "",
    attempt: int = 0,
    ctx: ToolContext | None = None,
) -> str:
    checkpoint = lambda phase: context_cancellation_checkpoint(ctx, phase)
    checkpoint("external_video.process.before_command_parse")
    try:
        cmd = shlex.split(command)
    except ValueError as exc:
        return f"designer author command could not be parsed: {exc}"
    if not cmd:
        return "designer author command is empty"
    env = harness_subprocess_env(
        os.environ,
        harness=str(getattr(settings, "designer_author_harness", "custom") or "custom"),
        api_key=getattr(settings, "harness_api_key", None),
    )
    sensitive_values = _process_sensitive_values(cmd, env, settings)
    raw_stdout_path = attempt_dir / ".video_author_stdout.tmp"
    raw_stderr_path = attempt_dir / ".video_author_stderr.tmp"
    result = run_external_author_process(
        ExternalAuthorProcessRequest(
            run_id=run_id or f"video:{attempt_dir.resolve()}",
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
            sensitive_values=tuple(sensitive_values),
        )
    )
    checkpoint("external_video.process.after_process")
    checkpoint("external_video.process.before_raw_log_cleanup")
    raw_stdout_path.unlink(missing_ok=True)
    raw_stderr_path.unlink(missing_ok=True)
    checkpoint("external_video.process.after_raw_log_cleanup")
    checkpoint("external_video.process.before_process_log_write")
    atomic_write_json(
        attempt_dir / "video_author_process_log.json",
        {
            "harness": str(
                getattr(settings, "designer_author_harness", "custom") or "custom"
            ),
            "model": str(getattr(settings, "designer_author_model", "") or ""),
            "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
            "returncode": result.returncode,
            "elapsed_s": round(result.elapsed_s, 3),
            "status": result.status,
            "reason": result.reason,
            "stdout": _redact_process_output(
                result.stdout,
                sensitive_values=sensitive_values,
            ),
            "stderr": _redact_process_output(
                result.stderr,
                sensitive_values=sensitive_values,
            ),
        },
    )
    checkpoint("external_video.process.after_process_log_write")
    if result.status == "selected":
        return "attempt_selected"
    if result.status == "timeout":
        return f"designer author command timed out after {timeout_s} seconds"
    if result.status == "spawn_error":
        return (
            "designer author command failed to start: "
            + (result.stderr or result.reason)
        )
    if result.returncode != 0:
        return f"designer author command exited with status {result.returncode}"
    return ""


def _feedback_error_messages(feedback: Any) -> list[str]:
    if not isinstance(feedback, dict):
        return []
    errors = [
        str(item).strip()
        for item in feedback.get("errors", [])
        if str(item).strip()
    ]
    if errors:
        return errors
    message = str(feedback.get("error_message") or "").strip()
    return [message] if message else []


def _process_sensitive_values(
    cmd: list[str],
    env: dict[str, str],
    settings: Any,
) -> set[str]:
    values = {
        str(getattr(settings, "harness_api_key", "") or "").strip(),
    }
    sensitive_name = re.compile(r"(?:api[_-]?key|token|secret|password)", re.I)
    for key, value in env.items():
        if sensitive_name.search(key) and str(value).strip():
            values.add(str(value).strip())
    for index, token in enumerate(cmd):
        if "=" in token:
            name, value = token.split("=", 1)
            if sensitive_name.search(name) and value:
                values.add(value)
        elif sensitive_name.search(token) and index + 1 < len(cmd):
            values.add(cmd[index + 1])
    return {value for value in values if len(value) >= 4}


def _redact_process_output(text: str, *, sensitive_values: set[str]) -> str:
    redacted = str(text or "").replace("[REDACTED]", "<redacted>")
    for value in sorted(sensitive_values, key=len, reverse=True):
        redacted = redacted.replace(value, "<redacted>")
    return redacted


def _normalized_manifest_scenes(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    scenes: list[dict[str, Any]] = []
    cursor = 0.0
    for raw_scene in manifest["scenes"]:
        duration_s = float(raw_scene["duration_s"])
        scenes.append(
            {
                "scene_id": str(raw_scene["scene_id"]),
                "title": str(raw_scene["title"]),
                "start_s": cursor,
                "duration_s": duration_s,
                "narration_text": str(raw_scene["narration_intent"]),
                "visual_ids": list(raw_scene.get("visual_ids") or []),
            }
        )
        cursor += duration_s
    return scenes


def _video_design_spec(
    scenes: list[dict[str, Any]],
) -> DesignSpec:
    return DesignSpec.model_validate(
        {
            "brief": "External coding-agent authored conference video",
            "artifact_type": "video",
            "canvas": {"w_px": 1920, "h_px": 1080},
            "palette": [],
            "typography": {},
            "mood": ["academic", "conference"],
            "html_artifact": {
                "title": "Conference video",
                "target": "video",
                "frames": [
                    {
                        "frame_id": scene["scene_id"],
                        "kind": "scene",
                        "title": scene["title"],
                        "duration_s": scene["duration_s"],
                        "speaker_notes": scene["narration_text"],
                        "blocks": [
                            {
                                "block_id": f"{scene['scene_id']}-{asset_id}",
                                "kind": "image",
                                "role": "source_evidence",
                                "source_id": asset_id,
                            }
                            for asset_id in scene["visual_ids"]
                        ],
                    }
                    for scene in scenes
                ],
            },
        }
    )


def _ensure_rendered_layers_from_provenance(ctx: ToolContext) -> None:
    provenance = _load_context_json(ctx, "paper_visual_provenance")
    rendered = ctx.state.setdefault("rendered_layers", {})
    for asset in provenance.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        asset_id = str(asset.get("asset_id") or "").strip()
        output_file = str(asset.get("output_file") or "").strip()
        source = ctx.run_dir / output_file
        if not asset_id or not source.is_file() or asset_id in rendered:
            continue
        rendered[asset_id] = {
            "kind": asset.get("kind") or "image",
            "src_path": str(source),
            "png_path": str(source),
            "caption": asset.get("caption_short") or asset.get("caption_full") or "",
        }


def _copy_authored_project(source: Path, destination: Path) -> None:
    for child in source.iterdir():
        if child.name in _PROTECTED_DELIVERY_FILES or child.name in {"narration", "renders"}:
            continue
        target = destination / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)
    (destination / "assets" / "narration.wav").unlink(missing_ok=True)


def _make_tree_files_read_only(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            path.chmod(path.stat().st_mode & ~0o222)


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
