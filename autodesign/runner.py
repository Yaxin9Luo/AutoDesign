"""PipelineRunner — wires designer + tools + critic into one cohesive run.

Owns: per-run paths, ToolContext, and runtime result summaries. Does NOT own
business logic; that lives in designer.py / critic.py / tools/*.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from .agents import (
    ClaimGraphExtractor,
    EnhancerResult,
    ExternalDesignerAuthor,
    ExternalLandingAuthor,
    ExternalSlidesAuthor,
    ExternalVideoAuthor,
    PromptEnhancer,
)
from .agents.claim_graph_extractor import EXTRACT_FAIL_THESIS
from .agents.prompt_enhancer import load_enhancer_system_prompt
from .config import (
    Settings,
    artifact_author_command_for_harness,
    designer_author_command_for_harness,
    effective_poster_harness_mode,
)
from .designer import DesignerLoop, invoke_designer_tool
from .run_control import CancellationToken
from .schema import (
    ArtifactType, ClaimGraph, RunResult, ToolResultRecord,
)
from .skills import SkillBundle, inject_skill_context, select_skills
from .tools import TOOL_HANDLERS, ToolContext
from .tools.paper_poster_renderer import (
    AUTHORED_PAPER_POSTER_REQUIRED_STATE_KEY,
    is_academic_paper_poster_context,
)
from .util.canvas_planner import (
    apply_canvas_plan_prologue,
    plan_canvas,
)
from .util.academic_palette import require_academic_color_system
from .util.claim_graph_validator import validate_claim_graph
from .util.deck_planner import (
    apply_deck_plan_prologue,
    plan_deck,
)
from .util.design_events import style_snapshot_from_spec
from .util.design_feedback import blocking_design_findings, design_feedback_to_dict
from .util.design_spec_fingerprint import design_spec_sha256
from .util.visual_reference_contract import (
    only_visual_reference_progression_findings,
    visual_reference_summary,
)
from .util.io import atomic_write_json, ensure_dirs, sha256_file
from .util.ids import new_run_id
from .util.logging import log, run_context
from .util.pipeline_cache import read_json_cache, stable_cache_key, write_json_cache
from .util.run_paths import resolve_run_dir
from .util.run_telemetry import write_run_telemetry_summary
from .util.reference_poster import normalize_reference_poster
from .video_runtime import require_video_runtime, video_environment_profile
from .video_delivery_validation import (
    validate_current_video_delivery as _validate_current_video_delivery,
)


def _preflight_video_runtime(
    canvas_plan: dict[str, Any] | None,
    *,
    artifact_type: str | None = None,
) -> None:
    artifact_type = str(
        artifact_type or (canvas_plan or {}).get("artifact_type") or ""
    ).lower()
    if artifact_type == ArtifactType.VIDEO.value:
        require_video_runtime(
            artifact_type,
            profile=video_environment_profile(),
        )


class PipelineRunner:

    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self, brief: str,
            attachments: list[Path] | None = None,
            template: str | None = None,
            skip_enhancer: bool = False,
            no_claim_graph: bool = False,
            run_id: str | None = None,
            reuse_ingest_run: str | None = None,
            resume_run: str | None = None,
            reference_poster: Path | None = None,
            reference_page_index: int = 0,
            palette_id: str | None = None,
            canvas_preset_id: str | None = None,
            cancellation_token: CancellationToken | None = None,
            supervised: bool = False,
            ) -> RunResult:
        token = cancellation_token or CancellationToken.never(str(run_id or resume_run or ""))
        token.raise_if_cancelled("runner.start")
        if reference_poster is not None:
            author_mode = str(
                getattr(self.settings, "designer_author_mode", "internal") or "internal"
            ).strip().lower()
            if author_mode != "external":
                raise ValueError(
                    "reference poster style requires designer_author_mode=external"
                )
        # The web shim passes a pre-generated run_id so it can subscribe
        # to the SSE event channel before the run starts. CLI callers
        # leave it None and we mint one here.
        # For --resume-run, force the run_id to the resumed run's id so
        # events + telemetry stay in the same run directory.
        resume_ctx: dict[str, Any] | None = None
        if resume_run:
            resume_path = resolve_run_dir(self.settings.out_dir, resume_run)
            resume_ctx = _load_resume_state(resume_path)
            token.raise_if_cancelled("runner.after_resume_load")
            if not isinstance(resume_ctx, dict):
                # `_load_resume_state` returned an error RunResult; propagate it.
                token.raise_if_cancelled("runner.before_resume_load_failure_return")
                return resume_ctx
            run_id = resume_path.name
            palette_id = _resume_palette_id(resume_ctx)
            canvas_preset_id = _resume_canvas_preset_id(resume_ctx)
            active_settings, refusal_reason = _settings_for_external_author_resume(
                self.settings,
                resume_ctx,
            )
            if refusal_reason:
                token.raise_if_cancelled("runner.before_resume_author_refusal")
                return _resume_author_refusal_result(
                    source_dir=resume_path,
                    resume_ctx=resume_ctx,
                    message=refusal_reason,
                )
        else:
            active_settings = self.settings
        run_id = run_id or new_run_id()
        if cancellation_token is None:
            token = CancellationToken.never(run_id)
        token.raise_if_cancelled("runner.before_run_context")
        # Tag every log() call inside this run with run_id so the FastAPI
        # SSE channel can filter by run without explicit plumbing through
        # tools (composite, critic, etc. don't all carry run_id today).
        active_runner = self if active_settings is self.settings else PipelineRunner(active_settings)
        run_dir = active_settings.out_dir / "runs" / run_id
        with run_context(run_id, run_dir=run_dir):
            return active_runner._run_inner(
                brief, attachments, template, skip_enhancer,
                no_claim_graph, run_id, reuse_ingest_run,
                resume_ctx, reference_poster, reference_page_index,
                palette_id=palette_id,
                canvas_preset_id=canvas_preset_id,
                cancellation_token=token,
                supervised=supervised,
            )

    def _run_inner(self, brief: str,
                   attachments: list[Path] | None,
                   template: str | None,
                   skip_enhancer: bool,
                   no_claim_graph: bool,
                   run_id: str,
                   reuse_ingest_run: str | None,
                   resume_ctx: dict[str, Any] | None = None,
                   reference_poster: Path | None = None,
                   reference_page_index: int = 0,
                   palette_id: str | None = None,
                   canvas_preset_id: str | None = None,
                   cancellation_token: CancellationToken | None = None,
                   supervised: bool = False) -> RunResult:
        token = cancellation_token or CancellationToken.never(run_id)
        token.raise_if_cancelled("runner.before_run_directory")
        run_dir = self.settings.out_dir / "runs" / run_id
        layers_dir = run_dir / "layers"
        ensure_dirs(run_dir, layers_dir)
        token.raise_if_cancelled("runner.after_run_directory")

        # v1.1: inject an "Attached files" prologue into the brief so the
        # designer knows to call `ingest_document` FIRST. We don't change the
        # designer signature — attachments travel as part of the brief text.
        # v2.3: same mechanism for --template — a "Template:" block with the
        # resolved canvas lands in the prologue BEFORE attachments.
        attachments = list(attachments or [])
        reference_metadata: dict[str, Any] = {}
        runtime_skill_snapshot: dict[str, Any] | None = None
        if resume_ctx is not None:
            token.raise_if_cancelled("runner.before_resume_restore")
            # Rehydrate every upstream stage from disk. Nothing under
            # ingest/enhancer/claim-graph/canvas-plan gets recomputed.
            persisted_brief = resume_ctx["run_brief_json"]
            persisted_state = resume_ctx["resume_state_json"]
            persisted_attachments = [
                Path(p) for p in (persisted_state.get("attachments") or [])
            ]
            attachments = [path for path in persisted_attachments if path.exists()]
            persisted_reference = str(persisted_state.get("reference_poster") or "").strip()
            metadata_path = run_dir / "reference_poster" / "reference_source_metadata.json"
            reference_metadata = (
                json.loads(metadata_path.read_text(encoding="utf-8"))
                if metadata_path.exists()
                else {}
            )
            expected_sha = str(reference_metadata.get("source_sha256") or "")
            staged_name = str(reference_metadata.get("staged_source") or "").strip()
            if not staged_name:
                staged_suffix = str(reference_metadata.get("source_suffix") or "").strip()
                staged_name = f"reference_source{staged_suffix}" if staged_suffix else ""
            staged_candidate = run_dir / "reference_poster" / staged_name if staged_name else None
            reference_poster = (
                staged_candidate
                if staged_candidate is not None and staged_candidate.is_file()
                else None
            )
            if reference_poster is not None and expected_sha and sha256_file(reference_poster) != expected_sha:
                raise ValueError("resume staged reference poster no longer matches its source SHA")
            if reference_poster is None and persisted_reference:
                candidate = Path(persisted_reference)
                if candidate.exists() and (not expected_sha or sha256_file(candidate) == expected_sha):
                    reference_poster = candidate
                elif candidate.exists():
                    raise ValueError(
                        "resume reference poster no longer matches the staged source SHA"
                    )
            reference_page_index = max(
                0,
                int(persisted_state.get("reference_page_index") or 0),
            )
            brief = str(persisted_brief.get("raw_user_brief") or "")
            template = persisted_brief.get("effective_template")
            skip_enhancer = bool(persisted_brief.get("skip_enhancer"))
            no_claim_graph = True  # designer_author consumes cached artifacts only
            effective_template = template
            canvas_plan = resume_ctx["canvas_plan"]
            _preflight_video_runtime(
                canvas_plan,
                artifact_type=str(resume_ctx.get("artifact_type") or ""),
            )
            token.raise_if_cancelled("runner.after_resume_preflight")
            deck_plan = resume_ctx.get("deck_plan") or {}
            if _paper_source_sanity_required(
                brief,
                persisted_attachments,
                reference_poster=reference_poster,
                requested_template=effective_template,
            ):
                missing_papers = [
                    path for path in persisted_attachments
                    if path.suffix.lower() == ".pdf" and not path.is_file()
                ]
                if missing_papers:
                    _raise_unverifiable_paper_source(
                        "resume run cannot verify its original paper PDF because it is no longer available: "
                        + ", ".join(str(path) for path in missing_papers)
                    )
                _validate_paper_source_attachments(attachments)
            designer_input_brief = str(
                persisted_brief.get("final_designer_input")
                or persisted_brief.get("designer_input_brief")
                or ""
            )
            run_brief_version = int(persisted_brief.get("version") or 1)
            if run_brief_version >= 2:
                runtime_skill_snapshot = _load_runtime_skill_snapshot(run_dir)
            snapshot_ids = [
                str(item.get("id") or "")
                for item in list((runtime_skill_snapshot or {}).get("selected") or [])
                if isinstance(item, dict) and str(item.get("id") or "")
            ]
            skill_bundle = _ResumeSkillBundle(
                ids=snapshot_ids or list(persisted_state.get("skill_bundle_ids") or []),
                runtime_state=(runtime_skill_snapshot or {}).get("runtime_state"),
            )
            skill_contexts = _snapshot_stage_contexts(runtime_skill_snapshot)
            log(
                "run.resume.enabled",
                run_id=run_id,
                source_run=resume_ctx["source_run_dir"],
                prior_attempts=resume_ctx["prior_attempts"],
                prior_feedback_issue_id=resume_ctx["prior_feedback_issue_id"],
                incremental_budget=resume_ctx["incremental_budget"],
            )
            log("run.start", run_id=run_id, brief_chars=len(designer_input_brief),
                pid=os.getpid(),
                attachments=len(attachments), template=effective_template or "(none)",
                requested_template=template or "(none)",
                canvas_preset=canvas_plan.get("preset_id") if isinstance(canvas_plan, dict) else None,
                canvas_lock=canvas_plan.get("lock_level") if isinstance(canvas_plan, dict) else None,
                deck_slide_count=deck_plan.get("slide_count") if isinstance(deck_plan, dict) else None,
                deck_lock=deck_plan.get("lock_level") if isinstance(deck_plan, dict) else None,
                skip_enhancer=skip_enhancer, selected_skills=skill_bundle.ids,
                resumed=True)
            wall_start = time.monotonic()
            enhancer_result = EnhancerResult(
                enhanced_brief=designer_input_brief,
                original_brief=designer_input_brief,
                model=self.settings.enhancer_model,
                skipped=True,
                skip_reason="resume",
            )
        else:
            token.raise_if_cancelled("runner.before_reference_normalization")
            effective_template = _select_effective_template(brief, attachments, template)
            if reference_poster is not None:
                reference_metadata = normalize_reference_poster(
                    reference_poster,
                    run_dir / "reference_poster",
                    page_index=max(0, int(reference_page_index)),
                )
            token.raise_if_cancelled("runner.after_reference_normalization")
            paper_source_sanity_required = _paper_source_sanity_required(
                brief,
                attachments,
                reference_poster=reference_poster,
                requested_template=effective_template,
            )
            if paper_source_sanity_required:
                sanity_attachments = list(attachments)
                if not any(path.suffix.lower() == ".pdf" for path in sanity_attachments):
                    sanity_attachments = _paper_source_attachments_from_reuse(
                        self.settings.out_dir,
                        reuse_ingest_run,
                    )
                if not sanity_attachments:
                    _raise_unverifiable_paper_source(
                        "reference poster mode requires a verifiable original paper PDF; "
                        "attach it or reuse an ingest run that records its source attachment"
                    )
                _validate_paper_source_attachments(sanity_attachments)
            token.raise_if_cancelled("runner.before_canvas_plan")
            canvas_plan = plan_canvas(
                brief,
                attachments,
                requested_template=effective_template,
                reference_metadata=reference_metadata or None,
            )
            token.raise_if_cancelled("runner.after_canvas_plan")
            token.raise_if_cancelled("runner.before_preflight")
            _preflight_video_runtime(canvas_plan)
            token.raise_if_cancelled("runner.after_preflight")
            _validate_reference_poster_artifact(
                reference_poster=reference_poster,
                canvas_plan=canvas_plan,
            )
            if reference_metadata:
                reference_metadata = dict(reference_metadata)
                if isinstance(reference_metadata.get("canvas_contract"), dict):
                    reference_metadata.setdefault(
                        "reference_canvas_contract",
                        dict(reference_metadata["canvas_contract"]),
                    )
                reference_metadata["canvas_contract"] = dict(canvas_plan.get("canvas") or {})
                reference_metadata["canvas_source"] = str(canvas_plan.get("source") or "")
                token.raise_if_cancelled("runner.before_reference_metadata_write")
                atomic_write_json(
                    run_dir / "reference_poster" / "reference_source_metadata.json",
                    reference_metadata,
                )
            token.raise_if_cancelled("runner.before_deck_plan")
            deck_plan = plan_deck(
                brief,
                attachments,
                canvas_plan=canvas_plan,
            )
            token.raise_if_cancelled("runner.after_deck_plan")
            effective_brief = _apply_template_prologue(brief, effective_template)
            effective_brief = apply_canvas_plan_prologue(effective_brief, canvas_plan)
            effective_brief = apply_deck_plan_prologue(effective_brief, deck_plan)
            effective_brief = _apply_attachment_prologue(
                effective_brief,
                attachments,
                artifact_type=str(canvas_plan.get("artifact_type") or ""),
            )
            runner_prologues, clean_brief = _split_runner_prologues(effective_brief)
            token.raise_if_cancelled("runner.before_skill_selection")
            skill_bundle = select_skills(
                effective_brief,
                attachments,
                _infer_skill_artifact_hint(effective_brief),
                self.settings,
            )
            token.raise_if_cancelled("runner.after_skill_selection")
            skill_contexts = skill_bundle.render_all()

            log("run.start", run_id=run_id, brief_chars=len(clean_brief),
                pid=os.getpid(),
                attachments=len(attachments), template=effective_template or "(none)",
                requested_template=template or "(none)",
                canvas_preset=canvas_plan.get("preset_id"),
                canvas_lock=canvas_plan.get("lock_level"),
                deck_slide_count=deck_plan.get("slide_count"),
                deck_lock=deck_plan.get("lock_level"),
                skip_enhancer=skip_enhancer, selected_skills=skill_bundle.ids)
            wall_start = time.monotonic()

            # v2.4 Prompt Enhancer — runs before DesignerLoop. `--skip-enhancer`
            # bypasses unconditionally; otherwise the settings gate decides.
            if reuse_ingest_run:
                enhancer_result = EnhancerResult(
                    enhanced_brief=clean_brief,
                    original_brief=clean_brief,
                    model=self.settings.enhancer_model,
                    skipped=True,
                    skip_reason="deferred_until_reuse_ingest_preload",
                )
                designer_input_brief = _compose_designer_input(
                    clean_brief,
                    runner_prologues=runner_prologues,
                    plan_context=skill_contexts.get("plan", ""),
                )
            else:
                enhancer_result = _run_enhancer(
                    self.settings,
                    clean_brief,
                    skip_enhancer=skip_enhancer,
                    attachments=attachments,
                    template=effective_template,
                    enhance_context=skill_contexts.get("enhance", ""),
                    cancellation_token=token,
                )
                designer_input_brief = _compose_designer_input(
                    enhancer_result.enhanced_brief,
                    runner_prologues=runner_prologues,
                    plan_context=skill_contexts.get("plan", ""),
                )

        if resume_ctx is not None:
            _validate_reference_poster_artifact(
                reference_poster=reference_poster,
                canvas_plan=canvas_plan,
            )
        if reference_poster is not None:
            author_mode = str(
                getattr(self.settings, "designer_author_mode", "internal") or "internal"
            ).strip().lower()
            if author_mode != "external":
                raise ValueError(
                    "reference poster style requires designer_author_mode=external"
                )

        ctx = ToolContext(
            settings=self.settings, run_dir=run_dir,
            layers_dir=layers_dir, run_id=run_id,
            cancellation_token=token,
        )
        token.raise_if_cancelled("runner.after_tool_context")
        selected_color_system: dict[str, Any] = {}
        if palette_id:
            selected_color_system = require_academic_color_system(
                palette_id,
                selection_reason="explicit structured Web palette selection",
            )
            palette_id = str(selected_color_system["palette_id"])
        if resume_ctx is None:
            token.raise_if_cancelled("runner.before_runtime_skill_snapshot")
            runtime_skill_snapshot = _write_runtime_skill_snapshot(
                run_dir,
                skill_bundle=skill_bundle,
                skill_contexts=skill_contexts,
            )
        skill_runtime_state = _runtime_skill_state(
            skill_bundle,
            runtime_skill_snapshot,
        )
        ctx.state["skills"] = skill_runtime_state
        ctx.state["skill_contexts"] = skill_contexts
        if runtime_skill_snapshot is not None:
            ctx.state["runtime_skill_bundle"] = SkillBundle.from_runtime_state(
                skill_runtime_state,
            )
            ctx.state["runtime_skill_snapshot"] = runtime_skill_snapshot
            ctx.state["runtime_skill_snapshot_root"] = str(run_dir / "runtime_skills")
            ctx.state["runtime_skill_resource_roots"] = _snapshot_resource_roots(
                run_dir,
                runtime_skill_snapshot,
            )
        ctx.state["canvas_plan"] = canvas_plan
        ctx.state["deck_plan"] = deck_plan
        ctx.state["raw_user_brief"] = brief
        if selected_color_system:
            ctx.state["palette_id"] = palette_id
            ctx.state["required_color_system"] = selected_color_system
        ctx.state["attachments"] = [str(path) for path in attachments]
        if reference_poster is not None:
            ctx.state["reference_poster_path"] = str(reference_poster)
            ctx.state["reference_page_index"] = max(0, int(reference_page_index))
            if reference_metadata:
                ctx.state["reference_poster"] = reference_metadata
        ctx.state["paper_source_sanity_required"] = _paper_source_sanity_required(
            brief,
            attachments,
            reference_poster=reference_poster,
            requested_template=effective_template,
        )
        if reuse_ingest_run:
            reuse_path = resolve_run_dir(self.settings.out_dir, reuse_ingest_run)
            ctx.state["reuse_ingest_run"] = str(reuse_path)
            log(
                "run.reuse_ingest.enabled",
                source_run=str(reuse_ingest_run),
                source_dir=ctx.state["reuse_ingest_run"],
            )
            reuse_result = invoke_designer_tool(
                "ingest_document", {}, ctx, handlers=TOOL_HANDLERS,
            )
            token.raise_if_cancelled("runner.after_reuse_ingest")
            atomic_write_json(
                run_dir / "reuse_ingest_preload.json",
                reuse_result.model_dump(mode="json"),
            )
            if reuse_result.status != "ok":
                issue_id = str((reuse_result.payload or {}).get("issue_id") or "reuse_ingest_preload_failed")
                log(
                    "run.reuse_ingest.preload_failed",
                    issue_id=issue_id,
                    reason=reuse_result.error_message or "",
                )
                failure_result = _reuse_ingest_failure_result(
                    run_id=run_id,
                    run_dir=run_dir,
                    artifact_type=str((canvas_plan or {}).get("artifact_type") or "landing"),
                    issue_id=issue_id,
                    message=reuse_result.error_message or "reused ingest preload failed",
                    wall_s=round(time.monotonic() - wall_start, 2),
                    settings=self.settings,
                    selected_skills=list(skill_bundle.ids or []),
                    canvas_plan=canvas_plan,
                    deck_plan=deck_plan,
                )
                token.raise_if_cancelled("runner.before_failure_event")
                log(
                    "pipeline.finished" if supervised else "run.done",
                    run_id=run_id,
                    wall_s=failure_result.wall_s,
                    terminal_status="fail",
                    reason=issue_id,
                )
                token.raise_if_cancelled("runner.before_failure_telemetry")
                write_run_telemetry_summary(run_dir)
                token.raise_if_cancelled("runner.after_failure_telemetry")
                return failure_result
            enhancer_result = _run_enhancer(
                self.settings,
                clean_brief,
                skip_enhancer=skip_enhancer,
                attachments=attachments,
                template=effective_template,
                enhance_context=skill_contexts.get("enhance", ""),
                cancellation_token=token,
            )
            designer_input_brief = _compose_designer_input(
                enhancer_result.enhanced_brief,
                runner_prologues=runner_prologues,
                plan_context=skill_contexts.get("plan", ""),
            )
            reuse_prompt_context = _reuse_ingest_prompt_context(reuse_result.payload)
            designer_input_brief = f"{designer_input_brief.rstrip()}\n\n{reuse_prompt_context}\n"
            ctx.state["reuse_ingest_planner_payload"] = dict(reuse_result.payload or {})
            log(
                "run.reuse_ingest.preloaded",
                source_dir=ctx.state["reuse_ingest_run"],
                figures=len((reuse_result.payload or {}).get("figures") or []),
                tables=len((reuse_result.payload or {}).get("tables") or []),
                prompt_context_chars=len(reuse_prompt_context),
            )
        token.raise_if_cancelled("runner.before_plan_snapshot_write")
        atomic_write_json(run_dir / "canvas_plan.json", canvas_plan)
        if deck_plan:
            atomic_write_json(run_dir / "deck_plan.json", deck_plan)
        token.raise_if_cancelled("runner.after_plan_snapshot_write")
        ctx.state["run_brief"] = designer_input_brief
        ctx.state["visual_reference_brief"] = designer_input_brief

        # Persist the minimum bits needed for a later `--resume-run` to skip
        # the enhancer / claim-graph / brief-prologue pipeline. Cheap unconditional
        # writes; the resume path bails cleanly if either file is missing.
        # In resume mode the files are already on disk from the prior run — don't
        # overwrite them with the (recomputed) skip-enhancer variants.
        if resume_ctx is None:
            token.raise_if_cancelled("runner.before_resume_snapshot_write")
            atomic_write_json(run_dir / "run_brief.json", {
                "version": 2,
                "raw_user_brief": brief,
                "clean_brief": clean_brief,
                "enhanced_brief": enhancer_result.enhanced_brief,
                "runner_prologues": runner_prologues,
                "designer_input_brief": designer_input_brief,
                "final_designer_input": designer_input_brief,
                "effective_template": effective_template,
                "skip_enhancer": bool(skip_enhancer),
                "no_claim_graph": bool(no_claim_graph),
                "reference_poster": str(reference_poster) if reference_poster else None,
                "reference_page_index": max(0, int(reference_page_index)),
                "palette_id": palette_id,
                "canvas_preset_id": canvas_preset_id,
            })
            atomic_write_json(run_dir / "resume_state.json", {
                "version": 2,
                "artifact_type": str(
                    (canvas_plan or {}).get("artifact_type") or ArtifactType.POSTER.value
                ),
                "attachments": [str(path) for path in attachments],
                "reference_poster": str(reference_poster) if reference_poster else None,
                "reference_page_index": max(0, int(reference_page_index)),
                "reuse_ingest_run": ctx.state.get("reuse_ingest_run"),
                "parent_run_id": (
                    Path(ctx.state["reuse_ingest_run"]).name
                    if ctx.state.get("reuse_ingest_run") else None
                ),
                "skill_bundle_ids": list(skill_bundle.ids or []),
                "palette_id": palette_id,
                "canvas_preset_id": canvas_preset_id,
                "designer_author": _designer_author_resume_metadata(self.settings),
            })
            token.raise_if_cancelled("runner.after_resume_snapshot_write")

        # v2.8.0 ClaimGraph extractor — runs between enhancer and designer
        # whenever the brief attaches a PDF and the stage is enabled.
        # Result lives in `ctx.state["claim_graph"]` so the designer prompt
        # + critic can reference it; on validation failure we drop back to
        # None and degrade to v2.7.3 chapter-order behavior.
        if resume_ctx is not None:
            claim_graph = None
        else:
            claim_graph = _run_claim_graph_extractor(
                self.settings, attachments,
                no_claim_graph=(no_claim_graph or bool(reuse_ingest_run)),
                cancellation_token=token,
            )
        ctx.state["claim_graph"] = claim_graph

        # In resume mode, pre-seed the active external author from its prior
        # checkpoint so numbering continues at checkpoint+1. Incomplete attempts
        # after that checkpoint are archived before their numbers are retried.
        if resume_ctx is not None:
            ctx.rehydrate_design_spec_state()
            _archive_superseded_resume_attempts(resume_ctx)
            _restore_external_author_resume_state(ctx, resume_ctx)
            # Also load the ingest artifacts back into ctx.state so
            # `_has_author_context` short-circuits inside the designer loop.
            from .tools.ingest_document import _load_ingest_state_from_dir
            load_result = _load_ingest_state_from_dir(ctx, run_dir)
            if isinstance(load_result, ToolResultRecord):
                issue_id = str(
                    (load_result.payload or {}).get("issue_id")
                    or "resume_ingest_reload_failed"
                )
                message = (
                    load_result.error_message
                    or "resume ingest artifacts could not be reloaded"
                )
                log(
                    "run.resume.ingest_reload_failed",
                    issue_id=issue_id,
                    reason=message,
                    error_category=load_result.error_category,
                )
                failure_result = _reuse_ingest_failure_result(
                    run_id=run_id,
                    run_dir=run_dir,
                    artifact_type=str(
                        (canvas_plan or {}).get("artifact_type") or "landing"
                    ),
                    issue_id=issue_id,
                    message=message,
                    wall_s=round(time.monotonic() - wall_start, 2),
                    settings=self.settings,
                    selected_skills=list(skill_bundle.ids or []),
                    canvas_plan=canvas_plan,
                    deck_plan=deck_plan,
                )
                token.raise_if_cancelled("runner.before_resume_failure_event")
                log(
                    "pipeline.finished" if supervised else "run.done",
                    run_id=run_id,
                    wall_s=failure_result.wall_s,
                    terminal_status="fail",
                    reason=issue_id,
                )
                token.raise_if_cancelled("runner.before_resume_failure_telemetry")
                write_run_telemetry_summary(run_dir)
                token.raise_if_cancelled("runner.after_resume_failure_telemetry")
                return failure_result

        token.raise_if_cancelled("runner.before_author_prompt")
        system_prompt = (self.settings.prompts_dir / "designer.md").read_text(encoding="utf-8")
        system_prompt += _poster_harness_mode_prompt(self.settings)
        ctx.state["runtime_skill_stage"] = "plan"
        designer_artifact_hint = str(
            (canvas_plan or {}).get("artifact_type") or ""
        ).strip().lower() or _infer_skill_artifact_hint(designer_input_brief)
        designer = _make_designer_author(
            self.settings,
            system_prompt,
            artifact_hint=designer_artifact_hint,
        )
        token.raise_if_cancelled("runner.before_author")
        designer.run(designer_input_brief, ctx)
        token.raise_if_cancelled("runner.after_author")
        in_tok, out_tok = designer.token_totals
        cache_read_tok, cache_create_tok = _designer_cache_totals(designer)

        _recover_missing_paper_poster_spec_after_designer_timeout(ctx)
        _recover_missing_composite(ctx, brief=designer_input_brief)

        while _should_run_env_repair(ctx, self.settings):
            token.raise_if_cancelled("runner.before_repair_attempt")
            attempt = int(ctx.state.get("env_repair_attempts") or 0) + 1
            ctx.state["env_repair_attempts"] = attempt
            blocking = blocking_design_findings(
                ctx.state.get("last_design_feedback")
                or (ctx.state.get("last_composite_payload") or {}).get("design_feedback")
            )
            repair_brief = _build_env_repair_brief(
                original_enhanced_brief=designer_input_brief,
                ctx=ctx,
                attempt=attempt,
            )
            repair_brief = inject_skill_context(
                repair_brief,
                skill_contexts.get("repair", ""),
            )
            pre_repair_snapshot = _snapshot_env_repair_state(ctx)
            pre_repair_score = _design_feedback_severity_score(_current_design_feedback(ctx))
            log(
                "run.env_repair.start",
                attempt=attempt,
                max_attempts=int(getattr(self.settings, "max_env_repair_attempts", 1) or 0),
                blocking_findings=len(blocking),
            )
            # Force recovery to composite the repaired spec if the designer
            # proposes one but stops before calling composite itself.
            ctx.state["composition"] = None
            ctx.state["runtime_skill_stage"] = "repair"
            repair_designer = _make_designer_author(
                self.settings,
                system_prompt,
                artifact_hint=designer_artifact_hint,
            )
            repair_designer.run(repair_brief, ctx)
            token.raise_if_cancelled("runner.after_repair_attempt")
            repair_in, repair_out = repair_designer.token_totals
            repair_cache_read, repair_cache_create = _designer_cache_totals(repair_designer)
            in_tok += repair_in
            out_tok += repair_out
            cache_read_tok += repair_cache_read
            cache_create_tok += repair_cache_create
            _recover_missing_composite(ctx, brief=repair_brief)
            _restore_env_repair_state_if_worse(
                ctx,
                snapshot=pre_repair_snapshot,
                before_score=pre_repair_score,
                attempt=attempt,
            )
            log(
                "run.env_repair.done",
                attempt=attempt,
                finalized=bool(ctx.state.get("finalized", False)),
                input_tokens=repair_in,
                output_tokens=repair_out,
                remaining_blocking_findings=len(blocking_design_findings(
                    ctx.state.get("last_design_feedback")
                    or (ctx.state.get("last_composite_payload") or {}).get("design_feedback")
                )),
            )

        # Both spec + composition are runtime-only state. We still require
        # them to have been produced as a sanity check that the designer
        # completed a full workflow.
        spec = ctx.state.get("design_spec")
        composition = ctx.state.get("composition")
        direct_final = bool(ctx.state.get("designer_author_direct_final"))
        if spec is None and not direct_final:
            log("run.warning", reason="designer exited without proposing a DesignSpec")
        if composition is None:
            log("run.warning", reason="designer exited without producing composition artifacts")

        wall_s = round(time.monotonic() - wall_start, 2)

        terminal_status, critic_score = _derive_episode_outcome(
            ctx, finalized=ctx.state.get("finalized", False),
            spec_present=(spec is not None or direct_final),
            composition_present=composition is not None,
        )
        crits = ctx.state.get("critique_results") or []
        last_crit = crits[-1] if crits else None
        direct_final_manifest = ctx.state.get("designer_author_direct_final")
        if (
            terminal_status == "pass"
            and isinstance(direct_final_manifest, dict)
            and str(direct_final_manifest.get("source") or "") in {
                "external_designer_author",
                "external_landing_author",
                "external_slides_author",
                "external_video_author",
            }
            and str(direct_final_manifest.get("acceptance_path") or "") != "critic_pass"
        ):
            last_crit = None
        token.raise_if_cancelled("runner.before_result")
        result = RunResult(
            run_id=run_id,
            run_dir=str(run_dir),
            artifact_type=_artifact_type_from_state(ctx),
            terminal_status=terminal_status,
            critic_verdict=getattr(last_crit, "verdict", None),
            critic_score=critic_score,
            n_layers=_count_layers(ctx),
            n_critiques=len(crits),
            finalize_notes=str(ctx.state.get("finalize_notes") or ""),
            wall_s=wall_s,
            designer_model=self.settings.designer_model,
            planner_model=self.settings.designer_model,
            critic_model=self.settings.critic_model,
            image_model=self.settings.image_model,
            total_input_tokens=in_tok,
            total_output_tokens=out_tok,
            total_cache_read_tokens=cache_read_tok,
            total_cache_create_tokens=cache_create_tok,
            selected_skills=skill_bundle.ids,
            visual_reference=visual_reference_summary(ctx),
            canvas_plan=dict(ctx.state.get("canvas_plan") or {}),
            deck_plan=dict(ctx.state.get("deck_plan") or {}),
            style_snapshot=style_snapshot_from_spec(spec),
        )
        token.raise_if_cancelled("runner.before_pipeline_finished")
        log("pipeline.finished" if supervised else "run.done", run_id=run_id,
            wall_s=wall_s,
            terminal_status=terminal_status,
            critic_score=critic_score,
            n_layers=result.n_layers,
            n_critiques=result.n_critiques,
            designer_model=self.settings.designer_model,
            critic_model=self.settings.critic_model,
            image_model=self.settings.image_model,
            total_input_tokens=in_tok,
            total_output_tokens=out_tok,
            total_cache_read_tokens=cache_read_tok,
            total_cache_create_tokens=cache_create_tok,
            visual_reference_status=result.visual_reference.get("visual_reference_status"),
            visual_reference_required=result.visual_reference.get("visual_reference_required"))
        token.raise_if_cancelled("runner.before_telemetry")
        telemetry_path = write_run_telemetry_summary(run_dir)
        token.raise_if_cancelled("runner.after_telemetry")
        if telemetry_path is not None:
            log(
                "run.telemetry_summary.written",
                path=str(telemetry_path),
            )

        return result


def _should_use_external_designer_author(
    settings: Settings,
    *,
    artifact_hint: str | None,
) -> bool:
    mode = str(
        getattr(settings, "designer_author_mode", "internal") or "internal"
    ).strip().lower()
    artifact = str(artifact_hint or "").strip().lower()
    return mode == "external" and artifact in {"poster", "landing", "deck", "video"}


def _validate_reference_poster_artifact(
    *,
    reference_poster: object | None,
    canvas_plan: dict[str, Any] | None,
) -> None:
    if reference_poster is None:
        return
    artifact_type = str((canvas_plan or {}).get("artifact_type") or "").strip().lower()
    if artifact_type != "poster":
        raise ValueError(
            "reference poster style only supports poster artifacts; "
            f"resolved artifact_type={artifact_type or 'unknown'}"
        )


def _make_designer_author(
    settings: Settings,
    system_prompt: str,
    *,
    artifact_hint: str | None = None,
):
    artifact = str(artifact_hint or "").strip().lower()
    if not _should_use_external_designer_author(settings, artifact_hint=artifact):
        return DesignerLoop(settings, system_prompt)
    if artifact == "poster":
        return ExternalDesignerAuthor(settings, system_prompt)
    author_settings = _artifact_author_settings(settings, artifact)
    if artifact == "landing":
        return ExternalLandingAuthor(author_settings, system_prompt)
    if artifact == "deck":
        return ExternalSlidesAuthor(author_settings, system_prompt)
    return ExternalVideoAuthor(author_settings, system_prompt)


class _ArtifactAuthorSettings:
    def __init__(self, settings: Settings, *, command: str):
        self._settings = settings
        self.designer_author_cmd = command

    def __getattr__(self, name: str) -> Any:
        return getattr(self._settings, name)


def _artifact_author_settings(settings: Settings, artifact_type: str) -> Settings | _ArtifactAuthorSettings:
    harness = str(getattr(settings, "designer_author_harness", "custom") or "custom")
    model = str(getattr(settings, "designer_author_model", "") or "")
    configured = str(getattr(settings, "designer_author_cmd", "") or "").strip()
    if not hasattr(settings, "designer_author_harness"):
        return settings
    default_command = designer_author_command_for_harness(harness, model)
    explicit_command = configured if configured and configured != default_command else None
    command = artifact_author_command_for_harness(
        harness,
        artifact_type=artifact_type,
        model=model,
        explicit_cmd=explicit_command,
    )
    return _ArtifactAuthorSettings(settings, command=command)


def _reuse_ingest_failure_result(
    *,
    run_id: str,
    run_dir: Path,
    artifact_type: str,
    issue_id: str,
    message: str,
    wall_s: float,
    settings: Settings,
    selected_skills: list[str],
    canvas_plan: dict[str, Any],
    deck_plan: dict[str, Any],
) -> RunResult:
    return RunResult(
        run_id=run_id,
        run_dir=str(run_dir),
        artifact_type=artifact_type,
        terminal_status="fail",
        finalize_notes=f"{issue_id}: {message}",
        wall_s=wall_s,
        designer_model=settings.designer_model,
        planner_model=settings.designer_model,
        critic_model=settings.critic_model,
        image_model=settings.image_model,
        selected_skills=selected_skills,
        canvas_plan=dict(canvas_plan or {}),
        deck_plan=dict(deck_plan or {}),
    )


def _reuse_ingest_prompt_context(payload: dict[str, Any]) -> str:
    paper_memory = payload.get("paper_memory") if isinstance(payload.get("paper_memory"), dict) else {}
    storyboard = payload.get("paper_visual_storyboard") if isinstance(payload.get("paper_visual_storyboard"), dict) else {}
    provenance = payload.get("paper_visual_provenance") if isinstance(payload.get("paper_visual_provenance"), dict) else {}
    planner_context = {
        "figures": list(payload.get("figures") or [])[:12],
        "tables": list(payload.get("tables") or [])[:6],
        "paper_identity": {
            "source_file": paper_memory.get("source_file"),
            "metadata": paper_memory.get("metadata") or {},
            "categories": paper_memory.get("categories") or {},
        },
        "paper_memory_dossier": payload.get("paper_memory_dossier") or {},
        "paper_visual_storyboard": {
            key: (
                _reuse_selected_asset_prompt_records(storyboard.get(key))
                if key == "selected_assets"
                else storyboard.get(key)
            )
            for key in (
                "central_thesis", "storyline", "target_visual_count",
                "selected_assets", "panel_jobs", "metrics",
            )
            if storyboard.get(key) not in (None, [], {})
        },
        "paper_visual_provenance": {
            key: provenance.get(key)
            for key in ("source_documents", "generation_policy", "metrics")
            if provenance.get(key) not in (None, [], {})
        },
    }
    compact = _compact_reuse_prompt_value(planner_context)
    return (
        "## Reused Paper Ingest Context (already loaded)\n"
        "The reusable paper evidence has been copied into this run before planning. "
        "Use these source IDs and the matching local assets; do not invent paper facts or "
        "replace source figures with decorative imagery.\n"
        f"```json\n{json.dumps(compact, ensure_ascii=False, indent=2)}\n```"
    )


def _reuse_selected_asset_prompt_records(value: Any) -> list[dict[str, Any]]:
    fields = (
        "asset_id", "kind", "story_role", "reason", "caption_short",
        "caption_full", "source_page", "planner_eligible", "designer_eligible",
    )
    return [
        {key: item.get(key) for key in fields if item.get(key) not in (None, "", [], {})}
        for item in list(value or [])[:12]
        if isinstance(item, dict)
    ]


def _compact_reuse_prompt_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 6:
        return "[nested context omitted]"
    if isinstance(value, str):
        return value if len(value) <= 800 else value[:797].rstrip() + "..."
    if isinstance(value, list):
        return [
            _compact_reuse_prompt_value(item, depth=depth + 1)
            for item in value[:16]
        ]
    if isinstance(value, dict):
        return {
            str(key): _compact_reuse_prompt_value(item, depth=depth + 1)
            for key, item in list(value.items())[:48]
        }
    return value


class _ResumeSkillBundle:
    """Minimal stand-in for select_skills' bundle when resuming.

    The resume path doesn't re-render skill contexts (enhancer/plan/repair)
    because the enhancer stage is skipped entirely; the persisted
    designer_input_brief already carries whatever skill enhancements were
    applied on the original run. `to_runtime_state()` still needs to return
    something JSON-serializable for `ctx.state["skills"]`.
    """

    def __init__(
        self,
        *,
        ids: list[str],
        runtime_state: dict[str, Any] | None = None,
    ) -> None:
        self.ids = list(ids or [])
        self._runtime_state = dict(runtime_state or {})

    def to_runtime_state(self) -> dict[str, Any]:
        if self._runtime_state:
            return dict(self._runtime_state)
        return {"ids": self.ids, "resumed": True}

    def render_all(self) -> dict[str, str]:
        return {"enhance": "", "plan": "", "repair": ""}


_RUNTIME_SKILL_STAGES = ("enhance", "plan", "critique", "repair")
_RUNTIME_SKILL_CONTROL_PREFIXES = (
    "Attached files:",
    "Template:",
    "Canvas Plan:",
    "Deck Plan:",
)
_RUNTIME_SKILL_SEPARATOR = "\n\n---\n\n"


def _split_runner_prologues(brief: str) -> tuple[str, str]:
    """Separate runner controls from the clean user brief.

    The enhancer must never receive runner controls or a skill block as user
    content. Controls are restored after enhancement in their original order.
    """
    text = str(brief or "")
    leading_ws = text[:len(text) - len(text.lstrip())]
    remaining = text.lstrip()
    blocks: list[str] = []
    while remaining.startswith(_RUNTIME_SKILL_CONTROL_PREFIXES):
        separator_at = remaining.find(_RUNTIME_SKILL_SEPARATOR)
        if separator_at < 0:
            break
        blocks.append(remaining[:separator_at].rstrip())
        remaining = remaining[separator_at + len(_RUNTIME_SKILL_SEPARATOR):]
    if not blocks:
        return "", text
    return _RUNTIME_SKILL_SEPARATOR.join(blocks), leading_ws + remaining


def _compose_designer_input(
    enhanced_brief: str,
    *,
    runner_prologues: str,
    plan_context: str,
) -> str:
    """Assemble the sole designer prompt in stage order."""
    parts = [
        str(runner_prologues or "").strip(),
        str(enhanced_brief or "").strip(),
        str(plan_context or "").strip(),
    ]
    return _RUNTIME_SKILL_SEPARATOR.join(part for part in parts if part)


def _write_runtime_skill_snapshot(
    run_dir: Path,
    *,
    skill_bundle: Any,
    skill_contexts: dict[str, str],
) -> dict[str, Any]:
    """Persist the selected skill set before any designer work can mutate it."""
    snapshot_root = run_dir / "runtime_skills"
    snapshot_path = snapshot_root / "snapshot.json"
    if snapshot_path.exists():
        existing = _load_runtime_skill_snapshot(run_dir)
        if existing is None:
            raise ValueError("runtime skill snapshot exists but is unreadable")
        expected_signature = _runtime_skill_bundle_signature(skill_bundle)
        existing_signature = _runtime_skill_selected_signature(
            list(existing.get("selected") or [])
        )
        expected_contexts = {
            stage: str(skill_contexts.get(stage) or "")
            for stage in _RUNTIME_SKILL_STAGES
        }
        if (
            existing_signature != expected_signature
            or _snapshot_stage_contexts(existing) != expected_contexts
        ):
            raise ValueError(
                "runtime skill snapshot conflicts with the current selected skills or stage contexts"
            )
        return existing

    selected: list[dict[str, Any]] = []
    for pack in list(getattr(skill_bundle, "packs", []) or []):
        manifest = getattr(pack, "manifest", None)
        skill_id = str(getattr(pack, "id", "") or "")
        if not skill_id:
            continue
        pack_name = _runtime_skill_pack_name(skill_id)
        pack_root = Path(getattr(pack, "root"))
        target_root = snapshot_root / "packs" / pack_name
        source_skill = pack_root / "SKILL.md"
        if source_skill.is_file():
            _atomic_copy_file(source_skill, target_root / "SKILL.md")

        manifest_version = int(getattr(manifest, "manifest_version", 1) or 1)
        resources = _runtime_skill_resources(manifest)
        copied_resources: list[dict[str, Any]] = []
        if manifest_version >= 2:
            for resource in resources:
                relative_path = str(resource.get("path") or "")
                source_path = _runtime_skill_source_path(pack_root, relative_path)
                if source_path is None or not source_path.is_file():
                    raise ValueError(
                        f"runtime skill resource missing for snapshot: {skill_id}:{relative_path}"
                    )
                _atomic_copy_file(source_path, target_root / relative_path)
                copied_resources.append(resource)

        stages = [str(stage) for stage in (getattr(manifest, "stages", []) or [])]
        stage_skeleton = {
            stage: str(pack.render(stage) or "")
            for stage in stages
            if stage in _RUNTIME_SKILL_STAGES
        }
        selected.append({
            "id": skill_id,
            "version": str(getattr(manifest, "version", "") or ""),
            "manifest_version": manifest_version,
            "content_hash": _runtime_skill_content_hash(pack, copied_resources),
            "stages": stages,
            "stage_skeleton": stage_skeleton,
            "resources": copied_resources,
            "resource_hashes": dict(getattr(pack, "resource_hashes", {}) or {}),
            "pack_path": f"packs/{pack_name}",
        })

    runtime_state = _snapshot_runtime_skill_state(
        skill_bundle.to_runtime_state(),
        run_dir=run_dir,
        selected=selected,
    )
    snapshot = {
        "version": 2,
        "selected": selected,
        "stage_contexts": {
            stage: str(skill_contexts.get(stage) or "")
            for stage in _RUNTIME_SKILL_STAGES
        },
        "runtime_state": runtime_state,
    }
    _atomic_write_text(snapshot_root / "index.md", _runtime_skill_index(selected))
    atomic_write_json(snapshot_path, snapshot)
    log("runtime_skills.snapshot.written", path=str(snapshot_path), selected=[
        item["id"] for item in selected
    ])
    return snapshot


def _runtime_skill_bundle_signature(skill_bundle: Any) -> list[tuple[Any, ...]]:
    signature: list[tuple[Any, ...]] = []
    for pack in list(getattr(skill_bundle, "packs", []) or []):
        manifest = getattr(pack, "manifest", None)
        resources = _runtime_skill_resources(manifest)
        signature.append((
            str(getattr(pack, "id", "") or ""),
            str(getattr(manifest, "version", "") or ""),
            int(getattr(manifest, "manifest_version", 1) or 1),
            _runtime_skill_content_hash(pack, resources),
            tuple(str(stage) for stage in (getattr(manifest, "stages", []) or [])),
            tuple(str(resource.get("id") or "") for resource in resources),
        ))
    return signature


def _runtime_skill_selected_signature(selected: list[Any]) -> list[tuple[Any, ...]]:
    signature: list[tuple[Any, ...]] = []
    for item in selected:
        if not isinstance(item, dict):
            return []
        resources = item.get("resources")
        resources = resources if isinstance(resources, list) else []
        signature.append((
            str(item.get("id") or ""),
            str(item.get("version") or ""),
            int(item.get("manifest_version") or 1),
            str(item.get("content_hash") or ""),
            tuple(str(stage) for stage in (item.get("stages") or [])),
            tuple(
                str(resource.get("id") or "")
                for resource in resources
                if isinstance(resource, dict)
            ),
        ))
    return signature


def _load_runtime_skill_snapshot(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "runtime_skills" / "snapshot.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or int(payload.get("version") or 0) != 2:
        return None
    return payload


def _snapshot_stage_contexts(snapshot: dict[str, Any] | None) -> dict[str, str]:
    stored = (snapshot or {}).get("stage_contexts")
    stored = stored if isinstance(stored, dict) else {}
    return {
        stage: str(stored.get(stage) or "")
        for stage in _RUNTIME_SKILL_STAGES
    }


def _runtime_skill_state(
    skill_bundle: Any,
    snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    state = dict(getattr(skill_bundle, "to_runtime_state")() or {})
    snapshot_state = (snapshot or {}).get("runtime_state")
    if isinstance(snapshot_state, dict):
        state = dict(snapshot_state)
    if snapshot is not None:
        state["snapshot_version"] = 2
        state["snapshot_selected"] = list(snapshot.get("selected") or [])
    return state


def _snapshot_runtime_skill_state(
    runtime_state: dict[str, Any],
    *,
    run_dir: Path,
    selected: list[dict[str, Any]],
) -> dict[str, Any]:
    """Re-root v2 resource reads to the immutable run-local copy."""
    state = deepcopy(runtime_state)
    selected_by_id = {
        str(item.get("id") or ""): item
        for item in selected
        if isinstance(item, dict)
    }
    packs = state.get("packs")
    if not isinstance(packs, list):
        return state
    for summary in packs:
        if not isinstance(summary, dict):
            continue
        selected_pack = selected_by_id.get(str(summary.get("id") or ""))
        if selected_pack is None:
            continue
        pack_path = str(selected_pack.get("pack_path") or "")
        if not pack_path:
            continue
        summary["root"] = str(run_dir / "runtime_skills" / pack_path)
        summary["content_hash"] = str(selected_pack.get("content_hash") or "")
        summary["resource_hashes"] = dict(selected_pack.get("resource_hashes") or {})
    return state


def _snapshot_resource_roots(
    run_dir: Path,
    snapshot: dict[str, Any],
) -> dict[str, str]:
    roots: dict[str, str] = {}
    for selected in list(snapshot.get("selected") or []):
        if not isinstance(selected, dict):
            continue
        skill_id = str(selected.get("id") or "")
        pack_path = str(selected.get("pack_path") or "")
        if skill_id and pack_path:
            roots[skill_id] = str(run_dir / "runtime_skills" / pack_path)
    return roots


def _runtime_skill_resources(manifest: Any) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    for resource in list(getattr(manifest, "resources", []) or []):
        if hasattr(resource, "model_dump"):
            value = resource.model_dump(mode="json")
        elif isinstance(resource, dict):
            value = dict(resource)
        else:
            continue
        if isinstance(value, dict):
            resources.append(value)
    return resources


def _runtime_skill_source_path(pack_root: Path, relative_path: str) -> Path | None:
    if not relative_path:
        return None
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        return None
    candidate = (pack_root / path).resolve()
    try:
        candidate.relative_to(pack_root.resolve())
    except ValueError:
        return None
    return candidate


def _runtime_skill_content_hash(pack: Any, resources: list[dict[str, Any]]) -> str:
    declared_hash = str(getattr(pack, "content_hash", "") or "")
    if declared_hash:
        return declared_hash
    root = Path(getattr(pack, "root"))
    paths = [root / "skill.json", root / "SKILL.md"]
    for resource in resources:
        path = _runtime_skill_source_path(root, str(resource.get("path") or ""))
        if path is not None:
            paths.append(path)
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: str(item)):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _runtime_skill_pack_name(skill_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", skill_id)


def _runtime_skill_index(selected: list[dict[str, Any]]) -> str:
    lines = ["# Runtime skill snapshot", ""]
    if not selected:
        lines.append("No runtime skills selected.")
    for item in selected:
        lines.append(
            f"- `{item['id']}` v{item['version']} "
            f"(`{item['content_hash']}`)"
        )
    return "\n".join(lines) + "\n"


def _atomic_copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=target.name, dir=str(target.parent))
    try:
        with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.replace(temp_name, target)
    except Exception:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_name, path)
    except Exception:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise


def _resume_palette_id(resume_ctx: dict[str, Any] | None) -> str | None:
    persisted_palette_id = str(
        (resume_ctx or {}).get("run_brief_json", {}).get("palette_id")
        or (resume_ctx or {}).get("resume_state_json", {}).get("palette_id")
        or ""
    ).strip()
    return persisted_palette_id or None


def _resume_canvas_preset_id(resume_ctx: dict[str, Any] | None) -> str | None:
    persisted_canvas_preset_id = str(
        (resume_ctx or {}).get("run_brief_json", {}).get("canvas_preset_id")
        or (resume_ctx or {}).get("resume_state_json", {}).get("canvas_preset_id")
        or ""
    ).strip()
    return persisted_canvas_preset_id or None


def _designer_author_resume_metadata(settings: Settings) -> dict[str, Any]:
    """Persist external-author routing without persisting an executable command."""
    mode = str(
        getattr(settings, "designer_author_mode", "internal") or "internal"
    ).strip().lower()
    harness = str(
        getattr(settings, "designer_author_harness", "custom") or "custom"
    ).strip().lower()
    model = str(getattr(settings, "designer_author_model", "") or "").strip()
    command = str(getattr(settings, "designer_author_cmd", "") or "").strip()
    custom_command_sha256 = None
    if mode == "external" and harness == "custom" and command:
        custom_command_sha256 = hashlib.sha256(command.encode("utf-8")).hexdigest()
    return {
        "mode": mode,
        "harness": harness,
        "model": model or None,
        "custom_command_sha256": custom_command_sha256,
    }


def _settings_for_external_author_resume(
    settings: Settings,
    resume_ctx: dict[str, Any],
) -> tuple[Settings, str]:
    """Restore a persisted external harness, failing closed for custom commands."""
    persisted = (resume_ctx.get("resume_state_json") or {}).get("designer_author")
    if not isinstance(persisted, dict):
        # v1 resume metadata predates transport persistence. Preserve the
        # caller's configured behavior for backward compatibility.
        return settings, ""
    mode = str(persisted.get("mode") or "").strip().lower()
    if mode != "external":
        return settings, ""
    harness = str(persisted.get("harness") or "").strip().lower()
    model = str(persisted.get("model") or "").strip() or None
    if not harness:
        return settings, "resume refused: persisted external author harness is missing"
    if harness == "custom":
        expected_hash = str(persisted.get("custom_command_sha256") or "").strip().lower()
        current_command = str(
            getattr(settings, "designer_author_cmd", "") or ""
        ).strip()
        current_hash = (
            hashlib.sha256(current_command.encode("utf-8")).hexdigest()
            if current_command else ""
        )
        if not expected_hash or current_hash != expected_hash:
            return (
                settings,
                "resume refused: current custom command does not match the persisted command fingerprint",
            )
        return (
            replace(
                settings,
                designer_author_mode="external",
                designer_author_harness="custom",
                designer_author_model=model,
                designer_author_cmd=current_command,
            ),
            "",
        )
    command = designer_author_command_for_harness(harness, model)
    if not command:
        return settings, f"resume refused: unsupported persisted external author harness {harness!r}"
    return (
        replace(
            settings,
            designer_author_mode="external",
            designer_author_harness=harness,
            designer_author_model=model,
            designer_author_cmd=command,
        ),
        "",
    )


def _resume_author_refusal_result(
    *,
    source_dir: Path,
    resume_ctx: dict[str, Any],
    message: str,
) -> RunResult:
    artifact_type = str(
        resume_ctx.get("artifact_type")
        or (resume_ctx.get("resume_state_json") or {}).get("artifact_type")
        or ArtifactType.POSTER.value
    )
    return RunResult(
        run_id=source_dir.name,
        run_dir=str(source_dir),
        artifact_type=artifact_type,
        terminal_status="fail",
        finalize_notes=message,
    )


_EXTERNAL_AUTHOR_RESUME_CONTRACTS: dict[str, dict[str, Any]] = {
    "poster": {
        "author_dir": "designer_author",
        "state_prefix": "designer_author",
        "output_path": "poster.html",
        "process_log": "designer_author_log.json",
        "feedback_path": "validation_feedback.json",
        "final_path": "final/poster.html",
    },
    "landing": {
        "author_dir": "landing_author",
        "state_prefix": "landing_author",
        "output_path": "index.html",
        "process_log": "landing_author_process.json",
        "feedback_path": "landing_validation.json",
        "final_path": "final/index.html",
    },
    "deck": {
        "author_dir": "slides_author",
        "state_prefix": "slides_author",
        "output_path": "slides.html",
        "process_log": "designer_author_log.json",
        "feedback_path": "slides_validation.json",
        "final_path": "final/deck.html",
    },
    "video": {
        "author_dir": "video_author",
        "state_prefix": "video_author",
        "output_path": "project",
        "required_companion": "video_author_manifest.json",
        "process_log": None,
        "feedback_path": "video_author_finalize_errors.json",
        "fallback_feedback_paths": [
            "video_author_delivery_errors.json",
            "video_author_validation_errors.json",
        ],
        "final_path": "final/video_delivery.json",
    },
}


def _resume_artifact_type(
    *,
    canvas_plan: dict[str, Any],
    resume_state: dict[str, Any],
    run_brief: dict[str, Any],
) -> str:
    for value in (
        canvas_plan.get("artifact_type"),
        resume_state.get("artifact_type"),
        run_brief.get("artifact_type"),
    ):
        artifact_type = str(value or "").strip().lower()
        if artifact_type in _EXTERNAL_AUTHOR_RESUME_CONTRACTS:
            return artifact_type
    return ArtifactType.POSTER.value


def _resume_terminal_status(source_dir: Path) -> str:
    terminal_status = ""
    events_path = source_dir / "run_events.jsonl"
    if not events_path.exists():
        return terminal_status
    try:
        with events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if "run.done" not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("event") == "run.done":
                    terminal_status = str(event.get("terminal_status") or "")
    except OSError:
        return ""
    return terminal_status


def _restore_external_author_resume_state(
    ctx: ToolContext,
    resume_ctx: dict[str, Any],
) -> None:
    """Restore counters and the prior repair baseline for one external harness."""
    artifact_type = str(
        resume_ctx.get("artifact_type") or ArtifactType.POSTER.value
    ).strip().lower()
    state_prefix = str(
        resume_ctx.get("author_state_prefix")
        or _EXTERNAL_AUTHOR_RESUME_CONTRACTS.get(artifact_type, {}).get("state_prefix")
        or "designer_author"
    )
    previous_attempt_dir = resume_ctx.get("previous_attempt_dir")
    resume_payload = {
        "prior_attempts": resume_ctx["prior_attempts"],
        "previous_attempt_dir": (
            str(previous_attempt_dir) if previous_attempt_dir else None
        ),
        "previous_output_path": (
            str(resume_ctx["previous_output_path"])
            if resume_ctx.get("previous_output_path") else None
        ),
        "repair_feedback": resume_ctx["prior_feedback"],
        "source_run_dir": resume_ctx["source_run_dir"],
        "incremental_budget": resume_ctx["incremental_budget"],
    }

    if artifact_type == ArtifactType.POSTER.value:
        # Preserve the historical Poster state contract byte-for-byte.
        ctx.state["designer_author_attempts"] = resume_ctx["prior_attempts"]
        ctx.state["designer_author_attempt_records"] = resume_ctx["attempt_records"]
        ctx.state["designer_author_validation_feedback"] = (
            resume_ctx["validation_feedback_history"]
        )
        ctx.state["designer_author_last_feedback"] = resume_ctx["prior_feedback"]
        resume_payload.pop("previous_output_path")
        ctx.state["designer_author_resume"] = resume_payload
        return

    resume_payload["artifact_type"] = artifact_type
    resume_payload["author_state_prefix"] = state_prefix
    ctx.state["artifact_type"] = artifact_type
    ctx.state[f"{state_prefix}_attempts"] = resume_ctx["prior_attempts"]
    ctx.state[f"{state_prefix}_attempt_records"] = resume_ctx["attempt_records"]
    ctx.state[f"{state_prefix}_validation_feedback"] = (
        resume_ctx["validation_feedback_history"]
    )
    ctx.state[f"{state_prefix}_last_feedback"] = resume_ctx["prior_feedback"]
    ctx.state[f"{state_prefix}_resume"] = resume_payload
    ctx.state["external_author_resume"] = resume_payload


def _archive_superseded_resume_attempts(
    resume_ctx: dict[str, Any],
) -> list[Path]:
    """Move incomplete attempts after the checkpoint out of the active sequence."""
    raw_dirs = list(resume_ctx.get("superseded_attempt_dirs") or [])
    attempt_dirs = [Path(value) for value in raw_dirs]
    existing = [path for path in attempt_dirs if path.is_dir()]
    if not existing:
        return []

    author_dir = existing[0].parent
    archive_root = author_dir / "interrupted_attempts"
    archive_root.mkdir(parents=True, exist_ok=True)
    batch_dir = archive_root / f"resume_{int(time.time() * 1000)}"
    suffix = 1
    while batch_dir.exists():
        batch_dir = archive_root / f"resume_{int(time.time() * 1000)}_{suffix}"
        suffix += 1
    batch_dir.mkdir()

    archived_pairs: list[tuple[Path, Path]] = []
    for source in existing:
        if source.parent != author_dir or re.fullmatch(r"attempt_\d+", source.name) is None:
            continue
        destination = batch_dir / source.name
        shutil.move(str(source), str(destination))
        archived_pairs.append((source, destination))
    archived = [destination for _source, destination in archived_pairs]

    atomic_write_json(
        batch_dir / "archive_manifest.json",
        {
            "reason": "resume_from_last_usable_checkpoint",
            "resume_from_attempt": int(resume_ctx.get("prior_attempts") or 0),
            "archived_attempts": [
                {
                    "original": str(source),
                    "archived": str(destination),
                }
                for source, destination in archived_pairs
            ],
        },
    )
    log(
        "run.resume.incomplete_attempts_archived",
        resume_from_attempt=int(resume_ctx.get("prior_attempts") or 0),
        archived_attempts=[path.name for path in archived],
        archive_dir=str(batch_dir),
    )
    return archived


def _completed_invocation_matches_poster(
    invocation: dict[str, Any],
    poster_path: Path,
) -> bool:
    if not poster_path.exists() or invocation.get("timeout") is not False:
        return False
    poster_sha256 = str(invocation.get("poster_sha256") or "")
    if not poster_sha256:
        return False
    reason = str(invocation.get("reason") or "")
    if reason == "process_exit":
        return invocation.get("returncode") == 0
    return reason in {
        "done_marker",
        "poster_stable_without_done_marker",
        "poster_changed_stable_without_done_marker",
    }


def _feedback_matches_validated_attempt(
    feedback: dict[str, Any],
    *,
    attempt_index: int,
    attempt_dir: Path,
    poster_sha256: str,
    invocation_poster_sha256: str,
) -> bool:
    try:
        feedback_attempt = int(feedback.get("validated_attempt"))
    except (TypeError, ValueError):
        return False
    feedback_attempt_dir = str(feedback.get("validated_attempt_dir") or "")
    return (
        feedback_attempt == attempt_index
        and bool(feedback_attempt_dir)
        and Path(feedback_attempt_dir).resolve() == attempt_dir.resolve()
        and str(feedback.get("validated_poster_sha256") or "") == poster_sha256
        and str(feedback.get("invocation_poster_sha256") or "")
        == invocation_poster_sha256
    )


def _load_resume_state(source_dir: Path) -> "dict[str, Any] | RunResult":
    """Inspect a run directory and recover its external-author resume state.

    Returns a dict of resume context on success, or a `RunResult` with
    ``terminal_status="fail"`` on invalid state (finalized, no attempts,
    missing run_brief.json). Poster keeps its stricter hash-bound baseline
    selection; other artifacts use their independent author contracts.
    """
    from .schema import RunResult  # local import to avoid cycles at module load
    artifact_type = ArtifactType.POSTER.value

    def _err(reason: str, message: str) -> RunResult:
        return RunResult(
            run_id=source_dir.name,
            run_dir=str(source_dir),
            artifact_type=artifact_type,
            terminal_status="fail",
            critic_verdict=None,
            critic_score=None,
            n_layers=0,
            n_critiques=0,
            finalize_notes=f"resume refused: {reason} — {message}",
            wall_s=0.0,
            designer_model="",
            planner_model="",
            critic_model="",
            image_model="",
        )

    if (source_dir / "final" / "poster.html").exists():
        # `final/poster.html` alone is not enough: the delivery fallback copies
        # a poster there even when validation ultimately failed. Look at the
        # last `run.done` event to see whether the prior run actually succeeded.
        terminal_status = ""
        events_path = source_dir / "run_events.jsonl"
        if events_path.exists():
            try:
                with events_path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        if "run.done" not in line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if data.get("event") == "run.done":
                            terminal_status = str(data.get("terminal_status") or "")
            except OSError:
                pass
        if terminal_status in {"ok", "pass"}:
            return _err(
                "already_finalized",
                f"prior run terminated with status={terminal_status}; use --from-run to restart the designer.",
            )

    run_brief_path = source_dir / "run_brief.json"
    resume_state_path = source_dir / "resume_state.json"
    if not run_brief_path.exists() or not resume_state_path.exists():
        return _err(
            "missing_resume_metadata",
            "run_brief.json or resume_state.json missing (predates resume support).",
        )

    try:
        run_brief_json = json.loads(run_brief_path.read_text(encoding="utf-8"))
        resume_state_json = json.loads(resume_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return _err("metadata_unreadable", str(e))
    if int(run_brief_json.get("version") or 1) >= 2:
        if _load_runtime_skill_snapshot(source_dir) is None:
            return _err(
                "missing_runtime_skill_snapshot",
                "runtime_skills/snapshot.json missing or unreadable for this v2 run.",
            )

    canvas_plan_path = source_dir / "canvas_plan.json"
    if not canvas_plan_path.exists():
        return _err(
            "missing_canvas_plan",
            "canvas_plan.json missing under the resume target.",
        )
    canvas_plan = json.loads(canvas_plan_path.read_text(encoding="utf-8"))
    deck_plan_path = source_dir / "deck_plan.json"
    deck_plan = (
        json.loads(deck_plan_path.read_text(encoding="utf-8"))
        if deck_plan_path.exists() else {}
    )
    artifact_type = _resume_artifact_type(
        canvas_plan=canvas_plan,
        resume_state=resume_state_json,
        run_brief=run_brief_json,
    )
    resume_contract = _EXTERNAL_AUTHOR_RESUME_CONTRACTS[artifact_type]

    if artifact_type != ArtifactType.POSTER.value:
        if artifact_type == ArtifactType.VIDEO.value:
            final_is_current = _validate_current_video_delivery(source_dir).is_passed
        else:
            final_path = source_dir / str(resume_contract["final_path"])
            final_is_current = final_path.exists()
        if final_is_current and _resume_terminal_status(source_dir) in {"ok", "pass"}:
            return _err(
                "already_finalized",
                "prior run terminated successfully; use --from-run to restart the designer.",
            )

    designer_dir = source_dir / str(resume_contract["author_dir"])
    if not designer_dir.exists() or not designer_dir.is_dir():
        return _err(
            "no_designer_author",
            f"prior run never entered {resume_contract['author_dir']}; nothing to resume.",
        )
    attempts = sorted(
        (
            (int(match.group(1)), attempt_dir)
            for attempt_dir in designer_dir.glob("attempt_*")
            if attempt_dir.is_dir()
            and (match := re.fullmatch(r"attempt_(\d+)", attempt_dir.name))
        ),
        key=lambda item: item[0],
    )
    if not attempts:
        return _err(
            "no_attempts",
            "designer_author directory is empty; nothing to resume.",
        )

    # Rebuild attempt records + validation feedback history.
    attempt_records: list[dict[str, Any]] = []
    invocation_by_attempt: dict[int, dict[str, Any]] = {}
    validation_feedback_history: list[dict[str, Any]] = []
    process_log_name = resume_contract.get("process_log")
    feedback_names = [
        str(resume_contract["feedback_path"]),
        *[
            str(name)
            for name in list(resume_contract.get("fallback_feedback_paths") or [])
        ],
    ]

    def _attempt_feedback(attempt_dir: Path) -> dict[str, Any] | None:
        for feedback_name in feedback_names:
            feedback_path = attempt_dir / feedback_name
            if not feedback_path.exists():
                continue
            try:
                feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(feedback, dict):
                return feedback
        return None

    for attempt_index, adir in attempts:
        log_path = adir / str(process_log_name) if process_log_name else None
        invocation: dict[str, Any] = {}
        if log_path is not None and log_path.exists():
            try:
                invocation = json.loads(log_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                invocation = {}
        invocation_by_attempt[attempt_index] = invocation
        attempt_records.append({
            "attempt": attempt_index,
            "attempt_dir": str(adir),
            "invocation": invocation,
            "phase": "resumed_from_disk",
        })
        feedback = _attempt_feedback(adir)
        if feedback is not None:
            validation_feedback_history.append(feedback)

    # Poster retains its strict invocation/hash binding. The other independent
    # harnesses persist artifact-specific validation payloads without Poster's
    # validated-poster hash envelope, so their newest usable authored output is
    # the repair baseline.
    prior_feedback: dict[str, Any] | None = None
    previous_attempt_dir: Path | None = None
    previous_output_path: Path | None = None
    if artifact_type == ArtifactType.POSTER.value:
        for attempt_index, adir in reversed(attempts):
            poster_path = adir / "poster.html"
            invocation = invocation_by_attempt[attempt_index]
            if not _completed_invocation_matches_poster(invocation, poster_path):
                continue
            vf_path = adir / "validation_feedback.json"
            try:
                vf = json.loads(vf_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(vf, dict) and _feedback_matches_validated_attempt(
                vf,
                attempt_index=attempt_index,
                attempt_dir=adir,
                poster_sha256=sha256_file(poster_path),
                invocation_poster_sha256=str(invocation["poster_sha256"]),
            ):
                prior_feedback = vf
                previous_attempt_dir = adir
                previous_output_path = poster_path
                break
    else:
        for _, adir in reversed(attempts):
            output_path = adir / str(resume_contract["output_path"])
            companion_name = resume_contract.get("required_companion")
            if not output_path.exists():
                continue
            if companion_name and not (adir / str(companion_name)).is_file():
                continue
            previous_attempt_dir = adir
            previous_output_path = output_path
            prior_feedback = _attempt_feedback(adir)
            break

    prior_feedback_issue_id = ""
    if isinstance(prior_feedback, dict):
        payload = prior_feedback.get("payload") if isinstance(prior_feedback.get("payload"), dict) else {}
        prior_feedback_issue_id = str(
            payload.get("issue_id") or prior_feedback.get("issue_id") or ""
        )

    checkpoint_attempt = attempts[-1][0]
    if previous_attempt_dir is not None:
        match = re.fullmatch(r"attempt_(\d+)", previous_attempt_dir.name)
        if match is not None:
            checkpoint_attempt = int(match.group(1))
    superseded_attempt_dirs = [
        attempt_dir
        for attempt_index, attempt_dir in attempts
        if attempt_index > checkpoint_attempt
    ]
    active_attempt_records = [
        record
        for record in attempt_records
        if int(record.get("attempt") or 0) <= checkpoint_attempt
    ]
    def _feedback_attempt_index(feedback: dict[str, Any]) -> int:
        try:
            return int(
                feedback.get("validated_attempt")
                or feedback.get("attempt")
                or 0
            )
        except (TypeError, ValueError):
            return 0

    active_feedback_history = [
        feedback
        for feedback in validation_feedback_history
        if _feedback_attempt_index(feedback) <= checkpoint_attempt
    ]

    return {
        "source_run_dir": str(source_dir),
        "run_brief_json": run_brief_json,
        "resume_state_json": resume_state_json,
        "canvas_plan": canvas_plan,
        "deck_plan": deck_plan,
        "artifact_type": artifact_type,
        "author_state_prefix": str(resume_contract["state_prefix"]),
        "prior_attempts": checkpoint_attempt,
        "attempt_records": active_attempt_records,
        "validation_feedback_history": active_feedback_history,
        "superseded_attempt_dirs": superseded_attempt_dirs,
        "prior_feedback": prior_feedback,
        "previous_attempt_dir": previous_attempt_dir,
        "previous_output_path": previous_output_path,
        "prior_feedback_issue_id": prior_feedback_issue_id,
        # Incremental budget: the designer loop consumes the current attempt
        # budget after the last usable checkpoint. Incomplete later attempts
        # are archived and their numbers are retried.
        "incremental_budget": True,
    }


def _designer_cache_totals(designer: Any) -> tuple[int, int]:
    value = getattr(designer, "cache_totals", (0, 0))
    try:
        read_tokens, create_tokens = value
        return int(read_tokens or 0), int(create_tokens or 0)
    except Exception:
        return 0, 0


def _recover_missing_composite(ctx: ToolContext, *, brief: str) -> None:
    """Last-ditch recovery when the designer stops after rendering layers.

    Kimi-class designers occasionally complete the expensive part of a paper
    poster run (ingestion + spec + text/image layers) and then end the turn
    before calling composite/finalize. Designers can also call composite and
    forget finalize. Previously both surfaced as `abort` even though enough
    state existed to render the artifact. We try one deterministic composite
    pass when needed, then reuse the normal finalize tool so high-severity
    grounding issues still block publication.
    """
    if ctx.state.get("design_spec") is None:
        return
    if ctx.state.get("finalized", False):
        return
    designer_error = ctx.state.get("designer_api_error")
    if (
        ctx.state.get("designer_contract_abort")
        and isinstance(designer_error, dict)
        and str(designer_error.get("type") or "") == "external_video_author"
    ):
        return

    if _state_requests_video(ctx, brief):
        delivery = ctx.state.get("video_delivery")
        current_spec_hash = design_spec_sha256(ctx.state["design_spec"])
        delivery_is_current = (
            isinstance(delivery, dict)
            and delivery.get("status") == "passed"
            and delivery.get("design_spec_sha256") == current_spec_hash
            and int(delivery.get("design_spec_revision") or 0)
            == int(ctx.state.get("spec_revision_count") or 0)
        )
        if not delivery_is_current:
            log(
                "run.auto_export_video.start",
                reason="designer_exited_without_current_passed_video_delivery",
            )
            export_result = invoke_designer_tool(
                "export_video", {}, ctx, handlers=TOOL_HANDLERS,
            )
            _log_tool_recovery_result("run.auto_export_video.done", export_result)
            if export_result.status != "ok":
                ctx.state["finalize_notes"] = (
                    "Designer stopped before delivering video; automatic export_video "
                    f"failed: {export_result.error_message or 'unknown error'}"
                )
                return
        finalize_result = invoke_designer_tool("finalize", {
            "notes": (
                "Auto-finalized after export_video produced a current passed "
                "HTML-first video delivery."
            )
        }, ctx, handlers=TOOL_HANDLERS)
        _log_tool_recovery_result("run.auto_finalize_video.done", finalize_result)
        if finalize_result.status != "ok":
            ctx.state["finalize_notes"] = (
                "Video delivery was produced but automatic finalize was blocked: "
                f"{finalize_result.error_message or 'unknown error'}"
            )
        return

    if ctx.state.get("composition") is None:
        rendered_layers = ctx.state.get("rendered_layers") or {}
        log(
            "run.auto_composite.start",
            reason="designer_exited_without_composite",
            rendered_layers=len(rendered_layers),
        )
        composite_result = invoke_designer_tool(
            "composite", {}, ctx, handlers=TOOL_HANDLERS,
        )
        _log_tool_recovery_result("run.auto_composite.done", composite_result)
        if composite_result.status != "ok":
            ctx.state["finalize_notes"] = (
                "Designer stopped before composite; automatic composite failed: "
                f"{composite_result.error_message or 'unknown error'}"
            )
            return

    _maybe_run_dogfood_terminal_feedback_auto_repair(ctx)

    if not _maybe_run_dogfood_auto_critique(ctx):
        _mark_dogfood_report_only_partial(ctx)
        return

    finalize_result = invoke_designer_tool("finalize", {
        "notes": (
            "Auto-finalized after the designer produced renderable artifacts "
            "but stopped before finalize."
        )
    }, ctx, handlers=TOOL_HANDLERS)
    _log_tool_recovery_result("run.auto_finalize.done", finalize_result)
    if finalize_result.status != "ok":
        _mark_dogfood_report_only_partial(ctx)
        ctx.state["finalize_notes"] = (
            "Designer stopped before finalize; automatic finalize was blocked: "
            f"{finalize_result.error_message or 'unknown error'}"
        )


def _maybe_run_dogfood_terminal_feedback_auto_repair(ctx: ToolContext) -> None:
    """Apply cheap deterministic DOM repairs when the LLM loop has stalled."""
    if effective_poster_harness_mode(ctx.settings) != "dogfood":
        return
    if ctx.state.get("finalized", False):
        return
    if ctx.state.get("dogfood_terminal_feedback_auto_repair_attempted"):
        return
    if ctx.state.get("design_spec") is None or ctx.state.get("composition") is None:
        return
    if int(ctx.state.get("spec_recovery_count") or 0) > 0:
        log(
            "run.terminal_feedback_auto_repair.skip",
            reason="deterministic_spec_recovery_is_diagnostic_only",
            spec_recovery_reason=ctx.state.get("spec_recovery_reason"),
        )
        return
    feedback = (
        ctx.state.get("last_design_feedback")
        or (ctx.state.get("last_composite_payload") or {}).get("design_feedback")
    )
    blocking = blocking_design_findings(feedback)
    if not blocking:
        return
    report_only = ctx.state.get("dogfood_blocking_composite_report_only")
    designer_timeout = _designer_api_error_looks_like_timeout(ctx.state.get("designer_api_error"))
    if (
        not _dogfood_latest_critic_budget_exhausted(ctx, ctx.settings)
        and not report_only
        and not designer_timeout
    ):
        return
    ctx.state["dogfood_terminal_feedback_auto_repair_attempted"] = True
    log(
        "run.terminal_feedback_auto_repair.start",
        blocking_findings=len(blocking),
        critique_count=len(ctx.state.get("critique_results") or []),
        reason=(
            "dogfood_report_only_blocking_composite"
            if report_only else
            "dogfood_designer_timeout_after_blocking_composite"
            if designer_timeout else
            "dogfood_max_critique_iters_exhausted"
        ),
    )
    previous_blocking_count = len(blocking)
    max_passes = max(1, min(4, int(os.getenv("POSTER_DOGFOOD_TERMINAL_REPAIR_PASSES", "3") or "3")))
    for repair_pass in range(1, max_passes + 1):
        repair_result = invoke_designer_tool("apply_design_ops", {
            "ops": [{
                "op": "html_auto_repair_feedback",
                "finding_id": "dogfood_terminal_feedback_auto_repair",
                "repair_pass": repair_pass,
            }],
        }, ctx, handlers=TOOL_HANDLERS)
        _log_tool_recovery_result(
            "run.terminal_feedback_auto_repair.design_ops.done",
            repair_result,
        )
        if repair_result.status != "ok":
            return
        composite_result = invoke_designer_tool(
            "composite", {}, ctx, handlers=TOOL_HANDLERS,
        )
        _log_tool_recovery_result(
            "run.terminal_feedback_auto_repair.composite.done",
            composite_result,
        )
        if composite_result.status != "ok":
            return
        feedback = (
            ctx.state.get("last_design_feedback")
            or (ctx.state.get("last_composite_payload") or {}).get("design_feedback")
        )
        blocking = blocking_design_findings(feedback)
        log(
            "run.terminal_feedback_auto_repair.pass",
            repair_pass=repair_pass,
            remaining_blocking_findings=len(blocking),
            previous_blocking_findings=previous_blocking_count,
        )
        if not blocking:
            return
        if len(blocking) >= previous_blocking_count:
            return
        previous_blocking_count = len(blocking)


def _mark_dogfood_report_only_partial(ctx: ToolContext) -> None:
    if not _dogfood_report_only_mode(ctx):
        return
    composite_payload = ctx.state.get("last_composite_payload") or {}
    if not (
        composite_payload.get("preview_sha256")
        or composite_payload.get("preview_relative_path")
        or composite_payload.get("html_relative_path")
    ):
        return
    blocking = blocking_design_findings(
        ctx.state.get("last_design_feedback")
        or composite_payload.get("design_feedback")
    )
    if only_visual_reference_progression_findings(blocking):
        ctx.state.pop("dogfood_blocking_composite_report_only", None)
        log(
            "run.report_only_partial.skipped",
            reason="visual_reference_progression_remains_actionable",
            blockers=[finding.get("id") for finding in blocking],
        )
        return
    ctx.state["dogfood_blocking_composite_report_only"] = {
        "blocking_findings": len(blocking),
        "reason": "dogfood_report_only_after_blocking_composite",
    }
    log(
        "run.report_only_partial",
        reason="dogfood_report_only_after_blocking_composite",
        blocking_findings=len(blocking),
    )


def _recover_missing_paper_poster_spec_after_designer_timeout(ctx: ToolContext) -> None:
    """Build a deterministic paper-poster spec when a dogfood designer times out.

    In the six-pack harness, a no-byte designer timeout can happen after the
    expensive source-ingest phase has already produced a poster content brief,
    plan contract, selected source visuals, and rendered ingest assets. In that
    state the run has enough source-backed material for the existing recovery
    spec path, and losing the candidate makes the outer visual loop noisy.
    """
    if ctx.state.get("design_spec") is not None:
        return
    if not _dogfood_report_only_mode(ctx):
        return
    error = ctx.state.get("designer_api_error")
    if not _designer_api_error_looks_like_timeout(error):
        return
    if not _has_paper_poster_recovery_state(ctx):
        return
    if os.getenv("DOGFOOD_DISABLE_TIMEOUT_SPEC_RECOVERY", "1").strip().lower() not in {"0", "false", "no"}:
        log(
            "run.auto_spec_recovery.skip",
            reason="dogfood_timeout_spec_recovery_disabled",
        )
        ctx.state["finalize_notes"] = (
            "Designer timed out after paper-poster ingest; dogfood timeout spec "
            "recovery is disabled so this run remains a failed generation "
            "instead of becoming a deterministic fallback candidate."
        )
        return
    log(
        "run.auto_spec_recovery.start",
        reason="designer_timeout_after_poster_contract",
        designer_turn=(error or {}).get("turn") if isinstance(error, dict) else None,
    )
    ctx.state["_pending_spec_recovery_reason"] = "designer_timeout_after_poster_contract"
    result = invoke_designer_tool(
        "propose_design_spec", {}, ctx, handlers=TOOL_HANDLERS,
    )
    _log_tool_recovery_result("run.auto_spec_recovery.done", result)
    if result.status != "ok":
        ctx.state["finalize_notes"] = (
            "Designer timed out after paper-poster ingest; deterministic spec "
            "recovery failed: "
            f"{result.error_message or 'unknown error'}"
        )


def _designer_api_error_looks_like_timeout(value: Any) -> bool:
    if isinstance(value, dict):
        text = " ".join(
            str(value.get(key) or "")
            for key in ("error", "error_type", "message")
        )
    else:
        text = str(value or "")
    normalized = text.lower()
    return bool(
        "timed out" in normalized
        or "timeout" in normalized
        or "returncode=28" in normalized
        or "curl: (28)" in normalized
    )


def _has_paper_poster_recovery_state(ctx: ToolContext) -> bool:
    state = ctx.state if isinstance(ctx.state, dict) else {}
    brief = state.get("poster_content_brief")
    contract = state.get("poster_plan_contract")
    if not (
        isinstance(brief, dict)
        and isinstance(contract, dict)
        and (
            brief.get("kind") == "paper_poster_content_brief"
            or contract.get("kind") == "paper_poster_plan_contract"
        )
    ):
        return False
    rendered = state.get("rendered_layers")
    if not isinstance(rendered, dict):
        return False
    return any(
        isinstance(record, dict)
        and str(layer_id).startswith(("ingest_fig_", "ingest_table_"))
        and str(record.get("src_path") or "").strip()
        for layer_id, record in rendered.items()
    )


def _maybe_run_dogfood_auto_critique(ctx: ToolContext) -> bool:
    """Run the required dogfood critic pass before auto-finalize.

    Returns True when the runner may proceed to finalize. A critic revise/fail
    verdict is a terminal quality signal for the outer harness, so we leave the
    run unfinalized instead of papering it over with auto-finalize.
    """
    if effective_poster_harness_mode(ctx.settings) != "dogfood":
        return True
    spec = ctx.state.get("design_spec")
    if spec is None:
        return True
    required = bool(ctx.state.get(AUTHORED_PAPER_POSTER_REQUIRED_STATE_KEY)) or is_academic_paper_poster_context(
        spec,
        ctx,
    )
    if not required:
        return True
    composite_payload = ctx.state.get("last_composite_payload") or {}
    if not composite_payload:
        return True
    feedback = (
        ctx.state.get("last_design_feedback")
        or composite_payload.get("design_feedback")
    )
    if blocking_design_findings(feedback):
        return True

    current_sha = composite_payload.get("preview_sha256")
    crit_sha = ctx.state.get("last_critique_preview_sha256")
    crits = ctx.state.get("critique_results") or []
    last_crit = crits[-1] if crits else None
    if current_sha and crit_sha == current_sha and last_crit is not None:
        if getattr(last_crit, "verdict", None) == "pass":
            return True
        if len(crits) >= max(0, int(getattr(ctx.settings, "max_critique_iters", 0) or 0)):
            return True
        ctx.state["finalize_notes"] = (
            "Designer stopped after a latest-composite critic verdict of "
            f"{getattr(last_crit, 'verdict', 'unknown')}; leaving the run for repair."
        )
        return False

    log(
        "run.auto_critique.start",
        reason="dogfood_latest_composite_requires_critic",
        composite_iteration=composite_payload.get("iteration"),
    )
    critique_result = invoke_designer_tool(
        "critique", {}, ctx, handlers=TOOL_HANDLERS,
    )
    _log_tool_recovery_result("run.auto_critique.done", critique_result)
    if critique_result.status != "ok":
        ctx.state["finalize_notes"] = (
            "Designer stopped before dogfood critique; automatic critique failed: "
            f"{critique_result.error_message or 'unknown error'}"
        )
        return False
    crits = ctx.state.get("critique_results") or []
    last_crit = crits[-1] if crits else None
    if getattr(last_crit, "verdict", None) != "pass":
        ctx.state["finalize_notes"] = (
            "Automatic dogfood critique requested repair with verdict "
            f"{getattr(last_crit, 'verdict', 'unknown')}; leaving the run unfinalized."
        )
        return False
    return True


def _log_tool_recovery_result(event: str, result: ToolResultRecord) -> None:
    payload = result.payload if isinstance(result.payload, dict) else {}
    issues = payload.get("layout_grounding_issues") or []
    feedback = payload.get("design_feedback") or {}
    feedback_counts = feedback.get("counts") if isinstance(feedback, dict) else {}
    log(
        event,
        status=result.status,
        error=result.error_message,
        layout_grounding_issues=len(issues) if isinstance(issues, list) else 0,
        design_feedback_blockers=(
            int(feedback_counts.get("blocker", 0))
            if isinstance(feedback_counts, dict) else 0
        ),
    )


def _should_run_env_repair(ctx: ToolContext, settings: Settings) -> bool:
    if ctx.state.get("finalized", False):
        return False
    max_attempts = max(0, int(getattr(settings, "max_env_repair_attempts", 1) or 0))
    attempts = int(ctx.state.get("env_repair_attempts") or 0)
    if attempts >= max_attempts:
        return False
    feedback = (
        ctx.state.get("last_design_feedback")
        or (ctx.state.get("last_composite_payload") or {}).get("design_feedback")
    )
    blocking = blocking_design_findings(feedback)
    if not blocking:
        return False
    if _dogfood_latest_critic_budget_exhausted(ctx, settings):
        log(
            "run.env_repair.skip",
            reason="dogfood_max_critique_iters_exhausted",
            critique_count=len(ctx.state.get("critique_results") or []),
        )
        return False
    if _dogfood_report_only_mode(ctx):
        composite_payload = ctx.state.get("last_composite_payload") or {}
        if not (
            composite_payload.get("preview_sha256")
            or composite_payload.get("preview_relative_path")
            or composite_payload.get("html_relative_path")
        ):
            log(
                "run.env_repair.skip",
                reason="dogfood_report_only_after_design_feedback",
                critique_count=len(ctx.state.get("critique_results") or []),
                blocking_findings=len(blocking),
            )
            return False
        if ctx.state.get("dogfood_blocking_composite_report_only"):
            log(
                "run.env_repair.skip",
                reason="dogfood_report_only_after_blocking_composite",
                critique_count=len(ctx.state.get("critique_results") or []),
                blocking_findings=len(blocking),
            )
            return False
        designer_error = ctx.state.get("designer_api_error")
        if _designer_api_error_looks_like_timeout(designer_error):
            log(
                "run.env_repair.skip",
                reason="dogfood_designer_timeout_after_blocking_composite",
                critique_count=len(ctx.state.get("critique_results") or []),
                blocking_findings=len(blocking),
                designer_turn=(
                    designer_error.get("turn")
                    if isinstance(designer_error, dict) else None
                ),
            )
            return False
        log(
            "run.env_repair.allow",
            reason="dogfood_blocking_composite_designer_repair",
            critique_count=len(ctx.state.get("critique_results") or []),
            blocking_findings=len(blocking),
        )
    return True


def _dogfood_report_only_mode(ctx: ToolContext) -> bool:
    return effective_poster_harness_mode(ctx.settings) == "dogfood"


def _dogfood_env_repair_skip_reason(ctx: ToolContext) -> str:
    composite_payload = ctx.state.get("last_composite_payload") or {}
    if (
        composite_payload.get("preview_sha256")
        or composite_payload.get("preview_relative_path")
        or composite_payload.get("html_relative_path")
    ):
        return "dogfood_report_only_after_current_composite"
    return "dogfood_report_only_after_design_feedback"


def _dogfood_latest_critic_budget_exhausted(ctx: ToolContext, settings: Settings) -> bool:
    if effective_poster_harness_mode(settings) != "dogfood":
        return False
    max_iters = max(0, int(getattr(settings, "max_critique_iters", 0) or 0))
    if max_iters <= 0:
        return False
    crits = ctx.state.get("critique_results") or []
    if len(crits) < max_iters:
        return False
    composite_payload = ctx.state.get("last_composite_payload") or {}
    current_sha = composite_payload.get("preview_sha256")
    critic_sha = ctx.state.get("last_critique_preview_sha256")
    return bool(current_sha and critic_sha == current_sha)


_ENV_REPAIR_STATE_KEYS = (
    "design_spec",
    "composition",
    "last_design_feedback",
    "last_composite_payload",
    "rendered_layers",
    "finalized",
    "finalize_notes",
    "visual_reference_revision_required",
    "visual_reference_revision_spec_revision",
    "visual_reference_revision_composited",
    "visual_reference_revision_iteration",
    "visual_reference_revision_source_spec_revision",
)


def _snapshot_env_repair_state(ctx: ToolContext) -> dict[str, Any]:
    return {
        key: deepcopy(ctx.state[key])
        for key in _ENV_REPAIR_STATE_KEYS
        if key in ctx.state
    }


def _restore_env_repair_state(ctx: ToolContext, snapshot: dict[str, Any]) -> None:
    for key in _ENV_REPAIR_STATE_KEYS:
        if key in snapshot:
            ctx.state[key] = deepcopy(snapshot[key])
        else:
            ctx.state.pop(key, None)


def _restore_env_repair_state_if_worse(
    ctx: ToolContext,
    *,
    snapshot: dict[str, Any],
    before_score: tuple[int, int, int, int],
    attempt: int,
) -> None:
    after_score = _design_feedback_severity_score(_current_design_feedback(ctx))
    regression_reason = _env_repair_regression_reason(ctx, snapshot)
    if after_score <= before_score and not regression_reason:
        return

    had_composition = snapshot.get("composition") is not None
    _restore_env_repair_state(ctx, snapshot)
    log(
        "run.env_repair.revert_worse",
        attempt=attempt,
        before_score=list(before_score),
        after_score=list(after_score),
        reason=regression_reason or "severity_counts_worsened",
    )
    if not had_composition or ctx.state.get("design_spec") is None:
        return

    # A rejected repair may already have updated run_dir/final symlinks to a
    # worse composite. Re-render the restored spec so downstream eval reads
    # the same candidate represented by runner state.
    ctx.state["composition"] = None
    composite_result = invoke_designer_tool(
        "composite", {}, ctx, handlers=TOOL_HANDLERS,
    )
    _log_tool_recovery_result("run.env_repair.restore_composite.done", composite_result)
    if composite_result.status != "ok":
        ctx.state["finalize_notes"] = (
            "Environment repair worsened design_feedback; restore composite failed: "
            f"{composite_result.error_message or 'unknown error'}"
        )


def _current_design_feedback(ctx: ToolContext) -> Any:
    return (
        ctx.state.get("last_design_feedback")
        or (ctx.state.get("last_composite_payload") or {}).get("design_feedback")
    )


def _design_feedback_severity_score(value: Any) -> tuple[int, int, int, int]:
    feedback = design_feedback_to_dict(value) or {}
    counts = feedback.get("counts") if isinstance(feedback, dict) else {}
    if not isinstance(counts, dict):
        counts = {}
    return (
        _safe_count(counts.get("blocker")),
        _safe_count(counts.get("high")),
        _safe_count(counts.get("medium")),
        _safe_count(counts.get("low")),
    )


def _safe_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _env_repair_regression_reason(
    ctx: ToolContext,
    snapshot: dict[str, Any],
) -> str:
    """Reject repairs that reduce measured visual area while still failing it."""
    before_feedback = (
        snapshot.get("last_design_feedback")
        or (snapshot.get("last_composite_payload") or {}).get("design_feedback")
    )
    after_feedback = _current_design_feedback(ctx)
    before_ratio = _paper_visual_area_ratio(before_feedback)
    after_ratio = _paper_visual_area_ratio(after_feedback)
    if before_ratio is None or after_ratio is None:
        return ""
    if (
        after_ratio + 0.04 < before_ratio
        and _has_paper_visual_area_finding(after_feedback)
    ):
        return f"paper_visual_area_regressed:{before_ratio:.3f}->{after_ratio:.3f}"
    return ""


def _paper_visual_area_ratio(feedback_value: Any) -> float | None:
    feedback = design_feedback_to_dict(feedback_value) or {}
    values: list[float] = []
    for finding in list(feedback.get("findings") or []):
        if not isinstance(finding, dict):
            continue
        if not _is_paper_visual_area_finding(finding):
            continue
        values.extend(_visual_area_numbers(finding))
    if not values:
        return None
    return min(values)


def _has_paper_visual_area_finding(feedback_value: Any) -> bool:
    feedback = design_feedback_to_dict(feedback_value) or {}
    for finding in list(feedback.get("findings") or []):
        if isinstance(finding, dict) and _is_paper_visual_area_finding(finding):
            return True
    return False


def _is_paper_visual_area_finding(finding: dict[str, Any]) -> bool:
    finding_id = str(finding.get("id") or "").lower()
    message = str(finding.get("message") or "").lower()
    return (
        "paper-visual-area-low" in finding_id
        or "poster_contract_visual_area_low" in finding_id
        or "visual area" in message
    )


def _visual_area_numbers(value: Any) -> list[float]:
    out: list[float] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_s = str(key).lower()
            if key_s == "visual_area_ratio":
                parsed = _float_or_none(item)
                if parsed is not None:
                    out.append(parsed)
            elif key_s in {"message", "snippet"}:
                out.extend(_visual_area_numbers(str(item)))
            elif isinstance(item, (dict, list)):
                out.extend(_visual_area_numbers(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(_visual_area_numbers(item))
    elif isinstance(value, str):
        for pattern in (
            r"visual_area_ratio\s*=\s*([0-9]*\.?[0-9]+)",
            r"visual area is\s+([0-9]*\.?[0-9]+)",
        ):
            for match in re.findall(pattern, value, flags=re.IGNORECASE):
                parsed = _float_or_none(match)
                if parsed is not None:
                    out.append(parsed)
    return out


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _poster_harness_mode_prompt(settings: Settings) -> str:
    mode = effective_poster_harness_mode(settings)
    if mode == "cheap":
        body = (
            "POSTER_HARNESS_MODE=cheap: for academic paper posters, run "
            "ingest -> poster_content_brief/poster_plan_contract -> "
            "propose_paper_poster_html -> composite -> finalize using tool "
            "design_feedback only. Do not call critique or "
            "generate_visual_reference unless an existing blocker explicitly "
            "requires that tool."
        )
    elif mode == "standard":
        body = (
            "POSTER_HARNESS_MODE=standard: use one critic pass and one "
            "repair pass at most. Use concrete tool feedback before spending "
            "critic/tool calls."
        )
    elif mode == "quality":
        body = (
            "POSTER_HARNESS_MODE=quality: visual reference is allowed, and up "
            "to two critic/repair passes may be used for substantial posters."
        )
    else:
        body = (
            "Default best-quality paper-poster route: keep "
            "outputs traceable to poster_content_brief, poster_plan_contract, "
            "design_feedback, and critic scorecards for before/after eval. "
            "For academic paper posters, prefer `propose_paper_poster_html` "
            "after `ingest_document`: write complete authored poster HTML/CSS "
            "with semantic panels, source figures/tables, native result "
            "structures, compact local readouts, and short source-backed "
            "summaries bound by `data-source-id`/`data-layer-id`. Use browser layout for "
            "panel interiors: CSS grid, flex, normal flow, float, and "
            "shape-outside are allowed when they produce better figure/text "
            "composition. Place the required "
            "`poster_plan_contract.selected_visuals[]` count and every "
            "`storyboard_selected_assets[]` ID before lower-priority text; "
            "do not put `data-block-id` on inline emphasis spans inside text. "
            "For `conference_editorial_flow`, the HTML-first contract is "
            "generation-hard: author one compact identity header and exactly "
            "three `.poster-column` columns, each with several normal-flow "
            "`.poster-section` blocks. Do not submit `.poster-grid`, six "
            "`.flow-panel` cards, child lanes, or `panel_content_plan` for "
            "that profile. Every source figure/table needs its own local "
            "flow unit, with the bound asset and a concise readout as direct "
            "siblings; visible `<figcaption>` and readouts starting with "
            "`Fig. N`, `Figure N`, or `Table N` are validation failures. "
            "Do not make a large Core contributions section as pure prose or "
            "mini-card text; merge it into Motivation or back it with a "
            "source/native evidence unit and local readout. "
            "For legacy fixed-panel contracts, every required "
            "`poster_plan_contract.layout_slot_contract.slots[]` entry must be "
            "authored as a visible panel root with matching `data-slot-id` and "
            "`data-panel-role`, filled with paper-specific leaf text, native "
            "units, and local image-text binding before calling the tool. "
            "Inside each dense slot, treat `child_lanes[]` as content/capacity "
            "hints, not geometry to copy. Author each panel as one local DOM "
            "flow: heading, one or more bound figures/tables when available, "
            "reading note, short source-backed readout, native units "
            "when useful, and takeaway. For ordinary figures/tables, put the "
            "source before the readout it supports and use "
            "float/shape-outside so text wraps around it. Do not submit one empty outer box or "
            "a flex/grid cluster that leaves the panel mostly blank. In panels "
            "that include a source visual, design figure-first: preserve the "
            "source image aspect ratio, use contain sizing, and place local "
            "claim/readout/takeaway copy in the same grid/flex/flow group. Do "
            "not stretch, `object-fit: cover`, arbitrary-crop, or shrink the "
            "source to a small text-afterthought. For landscape/wide "
            "legacy research_synthesis_dense posters, use the fixed contract grid: "
            "a CVPR-style 84x42 landscape board with three columns and exactly "
            "six substantive main panels, two stacked in each column. "
            "Title/meta/footer/citation strips do not count. Each large "
            "main panel must combine at least two content modes from readable "
            "source visual/figure/table, native table/result structure, compact "
            "source-backed readout, and local takeaway text. A lone "
            "source image or a lone prose box is not sufficient. Budget regions before writing copy: "
            "each dense main panel gets one heading, one or more source visual/table "
            "groups when available, compact readout text, native result/model "
            "units, and one takeaway region; keep most visual/table panels to 35-90 "
            "visible words unless the contract text budget is smaller. Use the fixed "
            "paper-poster typography contract on A0/landscape-sized canvases: "
            "Times New Roman, title 56px, author/institution identity rows 28px, major section headings 36px, "
            "body/readout/table prose 24px, captions/labels 20px, with line-height at least 1.12. Never "
            "create text boxes shorter than one line-height, never rely on bottom:0 "
            "or a canvas-edge footer for poster copy, and leave padding below every "
            "text block. For automatic paper posters, do not add extra shell "
            "panels. If a region is crowded, shorten, split, or convert prose "
            "into a compact comparison table, short result discussion, or source-grounded bullets; do not overlap text, hide overflow, "
            "or rely on a later hidden post-pass to add missing content. "
            "For research_synthesis_dense, "
            "the handmade LongCat/SAM2/ViT references are the primary quality "
            "targets, while iteration 51 LongCat is only a secondary dense "
            "visual anchor with known overlap/text-extraction defects. "
            "Do not rely on deterministic contract slot shells, source "
            "autoplace, or `contract_autofill`; dogfood may render a small "
            "diagnostic supplement for evaluation, but its word/native-unit "
            "share is scored as designer_contract debt and cannot satisfy the "
            "density target. Use the academic design system only: "
            "Times New Roman with fixed title/heading/body/readout/table/caption/label "
            "font sizes, title 56px/1.08/600, body weight 400, section headings 700, and the selected "
            "`color_system` from poster_content_brief/poster_plan_contract; "
            "fallback to the Cardinal Red academic palette only when no "
            "color_system exists. Use the fixed identity-header treatment: "
            "white/near-white header with a single top accent rule only. Do not "
            "invent per-panel colors, tinted "
            "panel bodies, table zebra fills, or heavy colored borders. "
            "The runtime may measure that DOM into authored_html blocks for "
            "editing and audit, but the designer-authored CSS and browser flow "
            "should be the source of truth for panel-internal composition. "
            "Designer-authored `propose_design_spec` calls must pass a complete "
            "top-level `{\"design_spec\": ...}` payload; empty spec calls are "
            "designer_contract blockers and will not be deterministically "
            "recovered. "
            "For academic paper posters, every final composite must run "
            "`critique` before `finalize`; if you repair after a critique, "
            "call `composite` and then `critique` again before finalizing. "
            "If `composite` returns blocking `design_feedback`, do not end the "
            "turn and do not call `finalize`; revise the authored HTML/spec "
            "using the exact feedback, call `composite` again, and continue "
            "until there are no blockers or the designer turn budget is reached. "
            "After `ingest_document`, use `propose_paper_poster_html` for the "
            "initial paper poster unless a non-poster artifact is requested. "
            "If you use `propose_design_spec` instead, call it with a complete "
            "top-level `{\"design_spec\": ...}` object; empty "
            "`propose_design_spec` arguments are treated as a designer contract "
            "failure in the best-quality route rather than an authored plan. "
            "Do not submit a legacy `layer_graph`-only paper poster, a hollow "
            "authored_html shell, or a spec whose storyboard is only declared "
            "in metadata; those are designer_contract failures. "
            "For authored_html paper posters, prefer omitting legacy "
            "`layer_graph` fallbacks entirely. If you include any fallback text "
            "layers, every chip/card/model-card/text layer must contain real "
            "paper-specific text; never submit empty placeholder text layers. "
            "Do not call `generate_visual_reference` as a generic polish step. "
            "Only call it when deterministic feedback has source `visual_reference` "
            "or a critic issue explicitly asks for a visual-reference/image repair."
        )
    return "\n\n## Poster Quality Route\n\n" + body + "\n"


def _build_env_repair_brief(
    *,
    original_enhanced_brief: str,
    ctx: ToolContext,
    attempt: int,
) -> str:
    spec = ctx.state.get("design_spec")
    if hasattr(spec, "model_dump"):
        spec_payload = spec.model_dump(mode="json")
    else:
        spec_payload = spec or {}
    feedback_payload = design_feedback_to_dict(
        ctx.state.get("last_design_feedback")
        or (ctx.state.get("last_composite_payload") or {}).get("design_feedback")
    ) or {}
    poster_plan_contract = (
        ctx.state.get("poster_plan_contract")
        if isinstance(ctx.state.get("poster_plan_contract"), dict)
        else {}
    )
    poster_contract_preflight = (
        ctx.state.get("poster_contract_preflight")
        if isinstance(ctx.state.get("poster_contract_preflight"), dict)
        else {}
    )
    scorecard = _latest_critic_scorecard(ctx)
    artifact_type = _artifact_type_from_state(ctx)
    repair_original_brief = _strip_attachment_prologue_for_repair(
        original_enhanced_brief,
    )
    return (
        f"Automatic environment repair pass {attempt}.\n\n"
        "The previous designer loop ended without a valid finalize because the "
        "runtime environment emitted blocking DesignFeedback. Treat this as "
        "hard validation feedback, not optional critique.\n\n"
        "Ingest state is already populated for this run. Use the existing "
        "`ctx.state[\"ingested\"]`, `ctx.state[\"poster_content_brief\"]`, "
        "`rendered_layers`, and the Current DesignSpec below; do not treat "
        "any historical attachment text as a command to ingest again.\n\n"
        "Original enhanced brief:\n"
        f"{repair_original_brief}\n\n"
        "Current DesignSpec JSON:\n"
        "```json\n"
        f"{json.dumps(spec_payload, ensure_ascii=False, indent=2)}\n"
        "```\n\n"
        "Latest design_feedback JSON:\n"
        "```json\n"
        f"{json.dumps(feedback_payload, ensure_ascii=False, indent=2)}\n"
        "```\n\n"
        "Latest poster_plan_contract JSON (if any):\n"
        "```json\n"
        f"{json.dumps(poster_plan_contract, ensure_ascii=False, indent=2)}\n"
        "```\n\n"
        "Latest poster_contract_preflight JSON (if any):\n"
        "```json\n"
        f"{json.dumps(poster_contract_preflight, ensure_ascii=False, indent=2)}\n"
        "```\n\n"
        "Latest critic scorecard JSON (if any):\n"
        "```json\n"
        f"{json.dumps(scorecard, ensure_ascii=False, indent=2)}\n"
        "```\n\n"
        "Repair constraints:\n"
        f"- Keep artifact_type exactly `{artifact_type}`.\n"
        "- Do not create a new artifact, switch topic, or change the user's core brief.\n"
        "- Reuse existing layer_id values wherever possible so history and editability survive.\n"
        "- Do not call ingest_document. If you need a paper visual, reference an existing `ingest_fig_NN` / `ingest_table_NN` layer from the current spec or rendered_layers.\n"
        "- Do not regenerate source figures or regenerate a background unless a finding explicitly requires it.\n"
        "- Fix blocker/P0 design_feedback findings first, then high findings if the repair is local.\n"
        "- Preserve other passing hard contracts while repairing one blocker: do not remove method callouts, evidence bullets, local readouts, or source figures unless you replace them with equivalent or denser content.\n"
        "- If a blocker has source `visual_reference`, run or complete the visual-reference loop: call `generate_visual_reference` when missing, revise editable layers/spec from its guidance, then call `composite`.\n"
        "- Use each finding's `stage` and `repair_route` when present: `local_refine` means apply_design_ops/edit specific layers; `pivot_layout_archetype` means propose_design_spec and rewrite `html_artifact.layout_plan`; `revise_content_strategy` means replace panel copy from poster_content_brief; `revise_visual_curation`/`swap_visual` means swap/place selected source visuals; `resize_visual` means recompute an existing source visual's figure box from its intrinsic aspect ratio inside the panel/lane max constraints without changing its source binding; `revise_typography_system`/`shrink_text` means restore the fixed paper-poster typography contract and solve fit with local splitting, spacing, or layout repair rather than changing role font sizes; `revise_authored_html` means patch `authored_body_html`/`authored_css` directly; `adjust_size_or_archetype` means preserve user size if explicit, otherwise revise size metadata or layout archetype.\n"
        "- Prefer `apply_design_ops` for local layer/slot/text/bbox/callout repairs.\n"
        "- If the current DesignSpec contains `html_artifact`, target HTML block ids with `html_*` ops and `block_id`; legacy `layer_id` ops only apply to `layer_graph`/rendered_layers.\n"
        "- For paper-poster visual-density blockers, increase or resize figure/table/image blocks by changing the intrinsic-ratio figure box and surrounding panel packing, not by stretching source images to arbitrary bboxes; keep at least the required method callouts and evidence bullets from design_feedback.\n"
        "- If a paper-density blocker says the poster already has enough placed visuals but the visual-area gap is large, use `propose_design_spec` for a structural slot rewrite instead of repeated local bbox growth; compact title/thesis bands, enlarge method/results/qualitative visual panels, and shorten/delete low-value prose so repair does not create text/visual overlaps.\n"
        "- Use `propose_design_spec` only when the repair requires a structural rewrite that cannot be expressed as design ops.\n"
        "- Then call `composite`, then `finalize`.\n"
    )


def _latest_critic_scorecard(ctx: ToolContext) -> dict[str, float]:
    crits = ctx.state.get("critique_results") or []
    if not crits:
        return {}
    last = crits[-1]
    scores = getattr(last, "dimension_scores", None)
    return dict(scores or {}) if isinstance(scores, dict) else {}


def _strip_attachment_prologue_for_repair(brief: str) -> str:
    """Remove the historical ingest-first prologue from repair briefs."""
    text = str(brief or "")
    if not text.startswith("Attached files:"):
        return text
    marker = "\n\n---\n\n"
    if marker not in text:
        return text
    return text.split(marker, 1)[1]


def _infer_skill_artifact_hint(brief: str) -> str | None:
    text = (brief or "").lower()
    explicit = re.search(
        r"(?m)^\s*artifact_type\s*:\s*(poster|deck|landing|video)\s*$",
        text,
    )
    if explicit:
        return explicit.group(1)
    # Control prologues inserted by the runner are stronger than content words
    # in the user's brief. A paper poster about video or animation is still a
    # poster and needs poster runtime skills.
    if any(t in text for t in ("artifact_type: poster", "type: poster", "poster", "海报")):
        return "poster"
    if any(t in text for t in ("type: deck", "deck", "slides", "slide deck", "ppt", "pptx", "powerpoint", "keynote", "演示", "幻灯片")):
        return "deck"
    if any(t in text for t in ("type: landing", "landing", "web page", "website", "网页", "网站", "着陆页")):
        return "landing"
    if any(t in text for t in ("type: video", "video", "mp4", "animated", "视频", "动画")):
        return "video"
    return None


def _brief_requests_video(brief: str) -> bool:
    head = brief[:500].lower()
    return "type: video" in head or "export_video" in head


def _state_requests_video(ctx: ToolContext, brief: str) -> bool:
    spec = ctx.state.get("design_spec")
    artifact_type = getattr(spec, "artifact_type", None)
    artifact_type = getattr(artifact_type, "value", artifact_type)
    return str(artifact_type or "").lower() == "video" or _brief_requests_video(brief)


def _run_enhancer(
    settings: Settings,
    effective_brief: str,
    *,
    skip_enhancer: bool,
    attachments: list[Path] | None = None,
    template: str | None = None,
    enhance_context: str = "",
    cancellation_token: CancellationToken | None = None,
) -> EnhancerResult:
    """Run the v2.4 Prompt Enhancer pre-designer stage.

    Returns an `EnhancerResult` either way — when skipped, its
    `enhanced_brief` equals the raw `effective_brief` so the runner can
    use it uniformly as the designer input. API failures also fall back
    to pass-through rather than crashing the run.
    """
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled("runner.enhancer.start")
    if skip_enhancer or not settings.enable_prompt_enhancer:
        reason = "--skip-enhancer" if skip_enhancer else "disabled in settings"
        log("prompt.enhance.skipped", reason=reason)
        return EnhancerResult(
            enhanced_brief=effective_brief,
            original_brief=effective_brief,
            model=settings.enhancer_model,
            skipped=True,
            skip_reason=reason,
        )
    try:
        system_prompt = load_enhancer_system_prompt(settings)
    except FileNotFoundError as e:
        log("prompt.enhance.missing_prompt", error=str(e),
            fallback="pass-through-raw-brief")
        return EnhancerResult(
            enhanced_brief=effective_brief,
            original_brief=effective_brief,
            model=settings.enhancer_model,
            skipped=True,
            skip_reason="system_prompt_missing",
        )
    cache_key = stable_cache_key({
        "stage": "prompt_enhancer",
        "cache_format": 3,
        "provider": settings.enhancer_provider,
        "model": settings.enhancer_model,
        "thinking_budget": settings.enhancer_thinking_budget,
        "template": template or "",
        "attachments": _attachment_cache_fingerprints(attachments or []),
        "brief_sha256": _sha256_text(effective_brief),
        "enhance_context_sha256": _sha256_text(enhance_context),
        "system_prompt_sha256": _sha256_text(system_prompt),
    })
    cached = read_json_cache(settings, "prompt_enhancer", cache_key)
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled("runner.enhancer.after_cache_read")
    if cached is not None:
        enhanced_brief = str(cached.get("enhanced_brief") or "")
        if enhanced_brief:
            log(
                "prompt.enhance.cache_hit",
                model=settings.enhancer_model,
                cache_key=cache_key[:12],
                enhanced_chars=len(enhanced_brief),
            )
            return EnhancerResult(
                enhanced_brief=enhanced_brief,
                original_brief=effective_brief,
                model=str(cached.get("model") or settings.enhancer_model),
                skipped=False,
                skip_reason="cache_hit",
                wall_time_s=0.0,
            )
    enhancer = PromptEnhancer(settings, system_prompt)
    result = enhancer.enhance(
        effective_brief,
        enhance_context=enhance_context,
        cancellation_token=cancellation_token,
    )
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled("runner.enhancer.after_model")
    if not result.skipped and result.enhanced_brief.strip():
        if cancellation_token is not None:
            cancellation_token.raise_if_cancelled("runner.enhancer.before_cache_write")
        write_json_cache(settings, "prompt_enhancer", cache_key, {
            "model": result.model,
            "enhanced_brief": result.enhanced_brief,
            "original_brief_sha256": _sha256_text(effective_brief),
            "enhance_context_sha256": _sha256_text(enhance_context),
            "system_prompt_sha256": _sha256_text(system_prompt),
        })
    return result


def _run_claim_graph_extractor(
    settings: Settings,
    attachments: list[Path],
    *,
    no_claim_graph: bool,
    cancellation_token: CancellationToken | None = None,
) -> ClaimGraph | None:
    """v2.8.0 — extract a `ClaimGraph` from the first attached PDF.

    Skip conditions (any one returns None):
      - `no_claim_graph` (`--no-claim-graph` CLI flag) is True
      - `settings.enable_claim_graph` is False
      - no PDF in attachments
      - PDF text extraction fails
      - extractor returns the sentinel "<extraction failed: timeout>"
        thesis (max_turns / api_error)
      - validator rejects the graph

    Failures are logged but never raise — the designer degrades to
    v2.7.3 chapter-order behavior on any of the above.
    """
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled("runner.claim_graph.start")
    if no_claim_graph:
        log("claim_graph.skipped", reason="--no-claim-graph")
        return None
    if not settings.enable_claim_graph:
        log("claim_graph.skipped", reason="disabled in settings")
        return None

    pdf = next((p for p in attachments if p.suffix.lower() == ".pdf"), None)
    if pdf is None:
        log("claim_graph.skipped", reason="no PDF attachment")
        return None

    try:
        if cancellation_token is not None:
            cancellation_token.raise_if_cancelled("runner.claim_graph.before_pdf_extract")
        paper_raw_text = _extract_pdf_text_for_claim_graph(pdf)
    except Exception as e:
        log("claim_graph.skipped",
            reason=f"pdf_text_extract_failed: {type(e).__name__}: {e}")
        return None
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled("runner.claim_graph.after_pdf_extract")
    if not paper_raw_text or len(paper_raw_text) < 200:
        log("claim_graph.skipped",
            reason="paper_raw_text too short", chars=len(paper_raw_text or ""))
        return None
    try:
        claim_prompt = (settings.prompts_dir / "claim_graph_extractor.md").read_text(encoding="utf-8")
    except Exception:
        claim_prompt = ""
    try:
        pdf_sha = sha256_file(pdf)
    except Exception:
        pdf_sha = ""
    cache_key = stable_cache_key({
        "stage": "claim_graph",
        "model": settings.claim_graph_model,
        "pdf_sha256": pdf_sha,
        "paper_raw_text_sha256": _sha256_text(paper_raw_text),
        "system_prompt_sha256": _sha256_text(claim_prompt),
        "source_scope": os.getenv("PAPER_SOURCE_SCOPE", ""),
    })
    cached = read_json_cache(settings, "claim_graph", cache_key)
    if cached is not None and isinstance(cached.get("claim_graph"), dict):
        try:
            graph = ClaimGraph.model_validate(cached["claim_graph"])
        except Exception as e:
            log(
                "claim_graph.cache_invalid",
                cache_key=cache_key[:12],
                error=f"{type(e).__name__}: {e}"[:500],
            )
        else:
            errors = validate_claim_graph(graph, paper_raw_text)
            if not errors and graph.thesis != EXTRACT_FAIL_THESIS:
                log(
                    "claim_graph.cache_hit",
                    model=settings.claim_graph_model,
                    cache_key=cache_key[:12],
                    thesis_chars=len(graph.thesis),
                    n_tensions=len(graph.tensions),
                    n_mechanisms=len(graph.mechanisms),
                    n_evidence=len(graph.evidence),
                    n_implications=len(graph.implications),
                )
                return graph
            log(
                "claim_graph.cache_invalid",
                cache_key=cache_key[:12],
                n_errors=len(errors),
                failed=graph.thesis == EXTRACT_FAIL_THESIS,
            )

    try:
        extractor = ClaimGraphExtractor(settings)
    except Exception as e:
        log("claim_graph.skipped",
            reason=f"extractor_init_failed: {type(e).__name__}: {e}")
        return None

    try:
        graph = extractor.extract(
            paper_path=pdf,
            paper_raw_text=paper_raw_text,
            cancellation_token=cancellation_token,
        )
    except Exception as e:
        log("claim_graph.skipped",
            reason=f"extractor_failed: {type(e).__name__}: {e}")
        return None
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled("runner.claim_graph.after_model")

    if graph.thesis == EXTRACT_FAIL_THESIS:
        log("claim_graph.degraded",
            reason="extractor returned sentinel thesis (max_turns/api_error)")
        return None

    errors = validate_claim_graph(graph, paper_raw_text)
    if errors:
        log("claim_graph.invalid",
            n_errors=len(errors), first_errors=errors[:3])
        return None

    log("claim_graph.ready",
        thesis_chars=len(graph.thesis),
        n_tensions=len(graph.tensions),
        n_mechanisms=len(graph.mechanisms),
        n_evidence=len(graph.evidence),
        n_implications=len(graph.implications))
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled("runner.claim_graph.before_cache_write")
    write_json_cache(settings, "claim_graph", cache_key, {
        "model": settings.claim_graph_model,
        "pdf_sha256": pdf_sha,
        "paper_raw_text_sha256": _sha256_text(paper_raw_text),
        "claim_graph": graph.model_dump(mode="json"),
    })
    return graph


def _sha256_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _attachment_cache_fingerprints(attachments: list[Path]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in attachments:
        try:
            stat = path.stat()
            digest = sha256_file(path) if path.is_file() else ""
        except Exception:
            stat = None
            digest = ""
        out.append({
            "name": path.name,
            "suffix": path.suffix.lower(),
            "sha256": digest,
            "bytes": int(stat.st_size) if stat is not None else 0,
        })
    return out


def _artifact_type_from_state(ctx: ToolContext) -> str:
    spec = ctx.state.get("design_spec")
    if spec is not None:
        raw = getattr(spec, "artifact_type", None)
        if raw is not None:
            return getattr(raw, "value", str(raw))
    return str(ctx.state.get("artifact_type") or ArtifactType.POSTER.value)


def _count_layers(ctx: ToolContext) -> int:
    composition = ctx.state.get("composition")
    manifest = getattr(composition, "layer_manifest", None)
    if isinstance(manifest, list) and manifest:
        return len(manifest)
    rendered = ctx.state.get("rendered_layers") or {}
    if isinstance(rendered, dict):
        return len(rendered)
    return 0


def _extract_pdf_text_for_claim_graph(pdf: Path) -> str:
    """Cheap text-only PDF extraction. Mirrors the page-text path from
    `tools.ingest_document._ingest_pdf` but skips figure / table / VLM
    work — the extractor only needs raw text to ground evidence quotes."""
    import fitz  # pymupdf

    from .util.pdf import extract_page_text
    from .tools.ingest_document import _paper_body_page_window

    doc = fitz.open(pdf)
    try:
        all_page_texts = extract_page_text(doc)
    finally:
        doc.close()
    page_window = _paper_body_page_window(all_page_texts)
    body_page_count = int(page_window.get("body_page_count") or len(all_page_texts))
    page_texts = all_page_texts[:body_page_count]
    log(
        "claim_graph.body_window",
        file=pdf.name,
        total_pages=page_window.get("total_page_count"),
        body_pages=page_window.get("body_page_count"),
        references_start_page=page_window.get("references_start_page"),
        appendix_start_page=page_window.get("appendix_start_page"),
        cutoff_reason=page_window.get("cutoff_reason"),
        source_scope=page_window.get("source_scope"),
    )
    return "\n\n".join(page_texts)


def _derive_episode_outcome(
    ctx: ToolContext,
    *,
    finalized: bool,
    spec_present: bool,
    composition_present: bool,
) -> tuple[str, float | None]:
    """Compute (terminal_status, critic_score) from the run's end state.

    - "pass": finalized, no blocking design_feedback, and last critique
      verdict==pass (or no critique was requested)
    - "revise": hit max_critique_iters with last verdict==revise
    - "fail": last verdict==fail
    - "max_turns": no spec
    - "abort": no composition, blocked finalize, or other partial state
    """
    crits = ctx.state.get("critique_results") or []
    last = crits[-1] if crits else None
    score = float(last.score) if last is not None else None
    has_blocking_feedback = bool(blocking_design_findings(
        ctx.state.get("last_design_feedback")
        or (ctx.state.get("last_composite_payload") or {}).get("design_feedback")
    ))
    if ctx.state.get("designer_contract_abort"):
        return "fail", score
    if not spec_present:
        return "max_turns", score
    if not composition_present:
        return "abort", score
    direct_final_manifest = ctx.state.get("designer_author_direct_final")
    if (
        isinstance(direct_final_manifest, dict)
        and str(direct_final_manifest.get("source") or "") == "external_designer_author"
        and str(direct_final_manifest.get("acceptance_path") or "") == "soft_accept"
        and finalized
    ):
        return "pass", None
    if (
        isinstance(direct_final_manifest, dict)
        and str(direct_final_manifest.get("source") or "") in {
            "external_landing_author",
            "external_slides_author",
            "external_video_author",
        }
        and finalized
        and not has_blocking_feedback
    ):
        acceptance_path = str(direct_final_manifest.get("acceptance_path") or "")
        current_score = score if acceptance_path == "critic_pass" and getattr(last, "verdict", None) == "pass" else None
        return "pass", current_score
    if crits:
        if last.verdict == "pass":
            if finalized and not has_blocking_feedback:
                return "pass", score
            return "abort", score
        if last.verdict == "revise":
            # finalize after revise → counts as "revise" terminal (not great
            # but the designer stopped; reward signals "kinda OK")
            return "revise", score
        return "fail", score
    # finalize fired but no critique was ever called — count as pass with no
    # critic score. This is rare but possible.
    return "pass" if finalized and not has_blocking_feedback else "abort", None


def _apply_attachment_prologue(
    brief: str,
    attachments: list[Path],
    *,
    artifact_type: str | None = None,
) -> str:
    """Prefix the brief with an 'Attached files:' block when v1.1 attachments
    are present, instructing the designer to call `ingest_document` first.

    The designer prompt (prompts/designer.md § "Ingestion workflow") teaches
    the model to treat this prefix as a signal.
    """
    if not attachments:
        return brief
    lines = ["Attached files:"]
    for p in attachments:
        try:
            size = p.stat().st_size if p.exists() else 0
        except OSError:
            size = 0
        kb = size // 1024
        lines.append(f"  - {p} ({kb} KB)")
    if str(artifact_type or "poster").strip().lower() == "poster":
        lines.append(
            "\nCALL `ingest_document` FIRST with these file_paths. For academic "
            "paper posters, prefer `propose_paper_poster_html` next: submit "
            "constrained HTML/CSS with semantic panels, source figures/tables, "
            "native result structures, compact local readouts, and source "
            "figures bound via `data-source-id` or `data-layer-id` "
            "from the returned manifest. Place the required selected source visual "
            "IDs and do not mark inline emphasis spans as blocks. If you use `propose_design_spec` instead, "
            "send a complete top-level `{\"design_spec\": ...}` payload. Ingested "
            "figures are pre-registered in rendered_layers."
        )
    else:
        lines.append(
            "\nCALL `ingest_document` FIRST with these file_paths, then follow the "
            "artifact-specific workflow. When using the normal tool loop, call "
            "`propose_design_spec` with a complete top-level "
            "`{\"design_spec\": ...}` payload. Ingested figures are "
            "pre-registered in rendered_layers."
        )
    return "\n".join(lines) + "\n\n---\n\n" + brief


def _select_effective_template(
    brief: str,
    attachments: list[Path],
    requested_template: str | None,
) -> str | None:
    """Choose the runner-level template to inject into the designer brief.

    Explicit templates always win. Adaptive defaults now live in
    util.canvas_planner so poster scene/aspect decisions are inspectable.
    """
    if requested_template:
        return requested_template.strip().lower().replace("_", "-")
    return None


def _paper_source_sanity_required(
    brief: str,
    attachments: list[Path],
    *,
    reference_poster: Path | None,
    requested_template: str | None = None,
) -> bool:
    if reference_poster is not None:
        return True
    if not any(Path(path).suffix.lower() == ".pdf" for path in attachments):
        return False
    text = str(brief or "").lower()
    poster_intent = any(token in text for token in ("poster", "海报"))
    template_intent = bool(str(requested_template or "").strip())
    return poster_intent or template_intent


def _validate_paper_source_attachments(attachments: list[Path]) -> None:
    from .util.paper_source_sanity import assert_valid_paper_source_pdf

    for path in attachments:
        if Path(path).suffix.lower() == ".pdf":
            assert_valid_paper_source_pdf(Path(path))


def _paper_source_attachments_from_reuse(
    out_dir: Path,
    reuse_ingest_run: str | None,
) -> list[Path]:
    reuse_value = str(reuse_ingest_run or "").strip()
    if not reuse_value:
        return []
    source_dir = resolve_run_dir(out_dir, reuse_value)
    resume_state = source_dir / "resume_state.json"
    if not resume_state.is_file():
        return []
    try:
        payload = json.loads(resume_state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [
        Path(str(value)).expanduser()
        for value in (payload.get("attachments") or [])
        if Path(str(value)).suffix.lower() == ".pdf"
        and Path(str(value)).expanduser().is_file()
    ]


def _raise_unverifiable_paper_source(message: str) -> None:
    from .util.paper_source_sanity import PaperSourceVerificationError

    raise PaperSourceVerificationError(message)


def _apply_template_prologue(brief: str, template: str | None) -> str:
    """Prefix the brief with a 'Template:' block resolving a registered poster
    template to its canvas preset (w_px / h_px / dpi / aspect_ratio / color_mode).

    The designer sees this as explicit input (same mechanism as attachments),
    reads the resolved dims, and emits them on `DesignSpec.canvas` unchanged
    unless the free-text user brief overrides. Template is validated at the
    CLI before we get here, so an unknown name silently becomes a no-op
    (defensive — don't fail the whole run on a template typo).
    """
    from .config import resolve_template
    canvas = resolve_template(template) if template else None
    if canvas is None:
        return brief
    # Compact one-line serialization so the designer can scan quickly.
    canvas_str = ", ".join(f"{k}={v!r}" for k, v in canvas.items())
    block = (
        f"Template: {template}\n"
        f"  canvas: {canvas_str}\n"
        f"\nThis is a registered academic-poster preset — USE THIS CANVAS "
        f"verbatim on your `DesignSpec.canvas` unless the free-text brief "
        f"explicitly overrides specific dims."
    )
    return block + "\n\n---\n\n" + brief
