"""External coding-agent adapter for paper-to-landing HTML authoring."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile
import time
from typing import Any
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup, Tag

from ..config import authoring_max_attempts_for, harness_subprocess_env
from ..designer import invoke_designer_tool
from ..schema import AttemptCandidate, CompositionArtifacts, ToolResultRecord
from ..tools import ToolContext
from ..util.artifact_palette_validation import validate_artifact_palette
from ..util.artifact_browser_audit import audit_landing_html
from ..util.browser_render import screenshot_html
from ..util.editable_html import ensure_editable_html_contract
from ..util.io import atomic_write_json, sha256_file
from ..util.landing_visual_plan import (
    build_landing_asset_catalog,
    build_landing_visual_plan,
)
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


_REQUIRED_EVIDENCE_FILES = (
    "paper_memory.json",
    "paper_visual_provenance.json",
)
_OPTIONAL_EVIDENCE_FILES = (
    "paper_memory.md",
    "paper_memory_dossier.json",
    "paper_memory_dossier.md",
)
_EVIDENCE_DIRS = ("paper_evidence_packs", "layers")
_REMOTE_SCHEMES = {"data", "file", "ftp", "http", "https", "javascript", "ws", "wss"}
_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)


@dataclass(frozen=True)
class _InvocationResult:
    status: str
    reason: str
    returncode: int | None
    timed_out: bool
    elapsed_s: float


class ExternalLandingAuthor:
    """Run the configured local coding agent as a paper landing-page author."""

    def __init__(self, settings: Any, system_prompt: str):
        self.settings = settings
        self.system_prompt = system_prompt
        self._total_in = 0
        self._total_out = 0
        self._total_cache_read = 0
        self._total_cache_create = 0

    def run(self, brief: str, ctx: ToolContext) -> None:
        context_cancellation_checkpoint(ctx, "external_landing.before_start")
        if promote_pending_selection(ctx) != "none":
            return
        ctx.state.pop("designer_api_error", None)
        ctx.state.pop("designer_contract_abort", None)
        ctx.state.pop("landing_author_failure", None)
        ctx.state["finalized"] = False
        author_dir = ctx.run_dir / "landing_author"
        context_cancellation_checkpoint(ctx, "external_landing.before_author_dir")
        author_dir.mkdir(parents=True, exist_ok=True)
        context_cancellation_checkpoint(ctx, "external_landing.after_author_dir")
        try:
            _recover_interrupted_promotion(ctx.run_dir / "final")
        except (OSError, ValueError) as exc:
            self._fail(
                ctx,
                "external_landing_promotion_recovery_failed",
                f"interrupted landing promotion could not be recovered: {exc}",
                author_dir,
            )
            return

        command = str(getattr(self.settings, "designer_author_cmd", "") or "").strip()
        if not command:
            self._fail(
                ctx,
                "missing_designer_author_cmd",
                "external landing author requires the configured designer author command",
                author_dir,
            )
            return
        if not self._ensure_ingested(ctx, author_dir):
            return
        provenance = ctx.state.get("paper_visual_provenance")
        if not isinstance(provenance, dict):
            provenance = _read_json(ctx.run_dir / "paper_visual_provenance.json")
        rendered_layers = ctx.state.get("rendered_layers")
        if not isinstance(rendered_layers, dict):
            rendered_layers = {}
        try:
            trusted_source_hashes = _trusted_landing_source_hashes(
                ctx.run_dir,
                build_landing_asset_catalog(
                    provenance,
                    rendered_layers=rendered_layers,
                ),
                require_existing=isinstance(
                    ctx.state.get("external_author_resume"), dict
                ),
            )
        except ValueError as exc:
            self._fail(
                ctx,
                "external_landing_trusted_source_anchor_failed",
                str(exc),
                author_dir,
            )
            return

        max_attempts = authoring_max_attempts_for(self.settings, "landing")
        prior_attempts = int(ctx.state.get("landing_author_attempts") or 0)
        absolute_attempt_budget = prior_attempts + max_attempts
        timeout_s = max(
            1,
            int(getattr(self.settings, "designer_author_timeout_s", 1800) or 1800),
        )
        resume = ctx.state.pop("external_author_resume", None)
        resume = resume if isinstance(resume, dict) else {}
        previous_attempt_value = str(resume.get("previous_attempt_dir") or "").strip()
        previous_attempt = Path(previous_attempt_value) if previous_attempt_value else None
        repair_feedback = (
            resume.get("repair_feedback")
            if isinstance(resume.get("repair_feedback"), dict)
            else None
        )
        if resume:
            log(
                "landing_author.resume",
                mode="external",
                source_run_dir=resume.get("source_run_dir"),
                prior_attempts=int(resume.get("prior_attempts") or 0),
                repair_seed=repair_feedback is not None,
            )
        last_issue_id = "external_landing_attempts_exhausted"
        last_message = "external landing author exhausted its attempt budget"
        last_payload: dict[str, Any] = {}
        records: list[dict[str, Any]] = []

        for attempt_number in range(1, max_attempts + 1):
            context_cancellation_checkpoint(ctx, "external_landing.before_attempt")
            if promote_pending_selection(ctx) != "none":
                return
            context_cancellation_checkpoint(ctx, "external_landing.after_selection_check")
            attempt_dir = self._next_attempt_dir(ctx)
            log(
                "landing_author.attempt_start",
                mode="external",
                attempt=int(
                    ctx.state.get("landing_author_attempts") or attempt_number
                ),
                max_attempts=absolute_attempt_budget,
            )
            context_cancellation_checkpoint(ctx, "external_landing.before_staging")
            if not self._stage_inputs(
                ctx,
                brief=brief,
                attempt_dir=attempt_dir,
                previous_attempt=previous_attempt,
                repair_feedback=repair_feedback,
            ):
                return
            context_cancellation_checkpoint(ctx, "external_landing.after_staging")

            prompt = self._build_prompt(
                brief=brief,
                attempt_dir=attempt_dir,
                repair_feedback=repair_feedback,
            )
            context_cancellation_checkpoint(ctx, "external_landing.before_prompt_write")
            (attempt_dir / "landing_author_prompt.md").write_text(prompt, encoding="utf-8")
            (attempt_dir / "designer_author_prompt.md").write_text(prompt, encoding="utf-8")
            context_cancellation_checkpoint(ctx, "external_landing.after_prompt_write")
            invocation = self._invoke_author_command(
                command,
                prompt=prompt,
                attempt_dir=attempt_dir,
                timeout_s=timeout_s,
                run_id=ctx.run_id,
                attempt=int(
                    ctx.state.get("landing_author_attempts") or attempt_number
                ),
                ctx=ctx,
            )
            context_cancellation_checkpoint(ctx, "external_landing.after_author_process")
            invocation_payload = asdict(invocation)
            ctx.state["landing_author_invocation"] = invocation_payload
            record = {
                "attempt": attempt_number,
                "attempt_dir": str(attempt_dir),
                "invocation": invocation_payload,
            }
            records.append(record)
            ctx.state["landing_author_attempt_records"] = records
            if invocation.status == "selected":
                promote_pending_selection(ctx)
                return
            if invocation.status != "ok":
                last_issue_id = invocation.reason
                last_message = "external landing author subprocess did not satisfy the output contract"
                last_payload = invocation_payload
                context_cancellation_checkpoint(ctx, "external_landing.before_process_retry")
                log(
                    "landing_author.retry",
                    mode="external",
                    attempt=attempt_number,
                    max_attempts=max_attempts,
                    reason=invocation.reason,
                )
                continue

            diagnostics = _validate_landing_output(
                attempt_dir,
                trusted_source_hashes=trusted_source_hashes,
            )
            context_cancellation_checkpoint(ctx, "external_landing.after_validation")
            if diagnostics["accepted"]:
                context_cancellation_checkpoint(ctx, "external_landing.before_browser_audit")
                browser_audit = audit_landing_html(
                    attempt_dir / "index.html",
                    required_source_ids=(
                        diagnostics.get("metrics", {}).get("used_source_visual_ids") or []
                    ),
                )
                context_cancellation_checkpoint(ctx, "external_landing.after_browser_audit")
                context_cancellation_checkpoint(ctx, "external_landing.before_browser_qa_write")
                atomic_write_json(attempt_dir / "landing_browser_qa.json", browser_audit)
                context_cancellation_checkpoint(ctx, "external_landing.after_browser_qa_write")
                diagnostics = _merge_landing_browser_audit(diagnostics, browser_audit)
            context_cancellation_checkpoint(ctx, "external_landing.before_validation_write")
            atomic_write_json(attempt_dir / "landing_validation.json", diagnostics)
            context_cancellation_checkpoint(ctx, "external_landing.after_validation_write")
            ctx.state["landing_author_validation"] = diagnostics
            record["validation"] = diagnostics
            candidate = capture_landing_attempt_candidate(
                ctx=ctx,
                attempt_dir=attempt_dir,
                attempt=int(ctx.state.get("landing_author_attempts") or attempt_number),
                max_attempts=absolute_attempt_budget,
                diagnostics=diagnostics,
            )
            if promote_pending_selection(ctx) != "none":
                return
            if diagnostics["accepted"]:
                context_cancellation_checkpoint(ctx, "external_landing.before_promotion")
                try:
                    self._promote(
                        ctx,
                        attempt_dir=attempt_dir,
                        diagnostics=diagnostics,
                        candidate_id=candidate.candidate_id,
                    )
                except Exception as exc:
                    self._fail(
                        ctx,
                        "external_landing_promotion_failed",
                        f"accepted landing output could not be promoted: {type(exc).__name__}: {exc}",
                        attempt_dir,
                        payload=diagnostics,
                    )
                return
            if _browser_audit_unavailable(diagnostics):
                self._fail(
                    ctx,
                    "landing_browser_audit_unavailable",
                    "accepted landing HTML could not be verified in the local browser runtime",
                    attempt_dir,
                    payload=diagnostics,
                )
                return

            last_issue_id = "external_landing_validation_failed"
            last_message = "external landing author output failed deterministic validation"
            last_payload = diagnostics
            previous_attempt = attempt_dir
            repair_feedback = diagnostics
            context_cancellation_checkpoint(ctx, "external_landing.before_validation_retry")
            log(
                "landing_author.retry",
                mode="external",
                attempt=attempt_number,
                max_attempts=max_attempts,
                reason=last_issue_id,
                finding_ids=[
                    finding.get("issue_id")
                    for finding in diagnostics.get("findings") or []
                    if isinstance(finding, dict)
                ],
            )

        if self._try_promote_best_available_candidate(ctx):
            return
        self._fail(
            ctx,
            last_issue_id,
            last_message,
            author_dir,
            payload={
                "attempt_budget": max_attempts,
                "last_payload": last_payload,
                "attempt_records": records,
            },
        )

    def _try_promote_best_available_candidate(self, ctx: ToolContext) -> bool:
        context_cancellation_checkpoint(ctx, "external_landing.fallback.start")
        if promote_pending_selection(ctx) != "none":
            return True
        candidates = ranked_delivery_candidates(
            ctx.run_dir,
            artifact_type="landing",
        )
        if not candidates:
            return False

        for candidate in candidates:
            source_html = ctx.run_dir / candidate.source_relative_path
            attempt_dir = source_html.parent
            catalog = _read_json(attempt_dir / "landing_asset_catalog.json")
            trusted_hashes = _trusted_landing_source_hashes(
                ctx.run_dir,
                catalog,
                require_existing=True,
            )
            diagnostics = _validate_landing_output(
                attempt_dir,
                trusted_source_hashes=trusted_hashes,
            )
            context_cancellation_checkpoint(ctx, "external_landing.fallback.after_static")
            metrics = diagnostics.get("metrics")
            metrics = metrics if isinstance(metrics, dict) else {}
            browser_audit = audit_landing_html(
                source_html,
                required_source_ids=metrics.get("used_source_visual_ids") or [],
            )
            diagnostics = _merge_landing_browser_audit(diagnostics, browser_audit)
            assessment = assess_delivery_issues(
                "landing",
                [
                    finding
                    for finding in diagnostics.get("findings") or []
                    if isinstance(finding, dict)
                ],
            )
            if assessment.safety_state == "blocked":
                rejection = {
                    "schema_version": 1,
                    "artifact_type": "landing",
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
                    "fresh_validation": diagnostics,
                }
                context_cancellation_checkpoint(
                    ctx,
                    "external_landing.fallback.before_rejection_write",
                )
                atomic_write_json(
                    ctx.run_dir / "landing_best_available_rejected.json",
                    rejection,
                )
                ctx.state["landing_best_available_rejected"] = rejection
                log(
                    "landing_author.best_available_rejected",
                    candidate_id=candidate.candidate_id,
                    hard_blocker_ids=[
                        issue.issue_id for issue in assessment.hard_blockers
                    ],
                )
                continue
            diagnostics["delivery_assessment"] = {
                "safety_state": assessment.safety_state,
                "quality_diagnostics": [
                    item.model_dump(mode="json")
                    for item in assessment.quality_diagnostics
                ],
            }
            self._promote(
                ctx,
                attempt_dir=attempt_dir,
                diagnostics=diagnostics,
                browser_audit=browser_audit,
                candidate_id=candidate.candidate_id,
                acceptance_path="best_available_artifact_fallback",
            )
            return True
        return False

    @property
    def token_totals(self) -> tuple[int, int]:
        return self._total_in, self._total_out

    @property
    def cache_totals(self) -> tuple[int, int]:
        return self._total_cache_read, self._total_cache_create

    def _ensure_ingested(self, ctx: ToolContext, author_dir: Path) -> bool:
        switch_result = invoke_designer_tool("switch_artifact_type", {"type": "landing"}, ctx)
        if switch_result.status == "error":
            self._fail_from_tool(ctx, "switch_artifact_type", switch_result, author_dir)
            return False
        if _has_full_ingest_context(ctx):
            return True

        attachments = [str(path) for path in (ctx.state.get("attachments") or [])]
        if not attachments and not ctx.state.get("reuse_ingest_run"):
            self._fail(
                ctx,
                "external_landing_missing_ingest_input",
                "paper landing author requires attachments or a reusable ingest run",
                author_dir,
            )
            return False
        ingest_result = invoke_designer_tool(
            "ingest_document",
            {"file_paths": attachments},
            ctx,
        )
        if ingest_result.status == "error":
            self._fail_from_tool(ctx, "ingest_document", ingest_result, author_dir)
            return False
        if not _has_full_ingest_context(ctx):
            self._fail(
                ctx,
                "external_landing_missing_ingest_context",
                "ingest_document did not produce paper memory, visual provenance, and layers",
                author_dir,
            )
            return False
        return True

    def _next_attempt_dir(self, ctx: ToolContext) -> Path:
        attempt = int(ctx.state.get("landing_author_attempts") or 0) + 1
        ctx.state["landing_author_attempts"] = attempt
        attempt_dir = ctx.run_dir / "landing_author" / f"attempt_{attempt:02d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        return attempt_dir

    def _stage_inputs(
        self,
        ctx: ToolContext,
        *,
        brief: str,
        attempt_dir: Path,
        previous_attempt: Path | None,
        repair_feedback: dict[str, Any] | None,
    ) -> bool:
        copied: list[str] = []
        missing: list[str] = []
        state_files = {
            "paper_memory.json": "paper_memory",
            "paper_memory_dossier.json": "paper_memory_dossier",
            "paper_visual_provenance.json": "paper_visual_provenance",
        }
        for name in (*_REQUIRED_EVIDENCE_FILES, *_OPTIONAL_EVIDENCE_FILES):
            source = ctx.run_dir / name
            target = attempt_dir / name
            if source.exists() and source.is_file():
                shutil.copy2(source, target)
                copied.append(name)
                continue
            state_key = state_files.get(name)
            payload = ctx.state.get(state_key) if state_key else None
            if isinstance(payload, dict):
                atomic_write_json(target, payload)
                copied.append(name)
            elif name in _REQUIRED_EVIDENCE_FILES:
                missing.append(name)

        for name in _EVIDENCE_DIRS:
            source = ctx.run_dir / name
            target = attempt_dir / name
            if source.exists() and source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
                copied.append(f"{name}/")
            elif name == "layers":
                missing.append("layers/")

        try:
            runtime_skills = _stage_runtime_skills(
                ctx,
                attempt_dir,
                stage="repair" if repair_feedback is not None else "plan",
            )
        except ValueError as exc:
            missing.append(f"runtime_skills/integrity: {exc}")
            runtime_skills = {"files": [], "catalog": {"available": False}}

        repair_inputs: list[str] = []
        if previous_attempt is not None and repair_feedback is not None:
            previous_html = previous_attempt / "index.html"
            if previous_html.exists():
                shutil.copy2(previous_html, attempt_dir / "previous_index.html")
                shutil.copy2(previous_html, attempt_dir / "index.html")
                repair_inputs.extend(["previous_index.html", "index.html"])
            compact_feedback = _compact_validation_feedback(repair_feedback)
            atomic_write_json(attempt_dir / "landing_validation.json", compact_feedback)
            atomic_write_json(
                attempt_dir / "previous_landing_validation.json",
                compact_feedback,
            )
            repair_inputs.extend(
                ["landing_validation.json", "previous_landing_validation.json"]
            )

        provenance = ctx.state.get("paper_visual_provenance")
        if not isinstance(provenance, dict):
            provenance = _read_json(attempt_dir / "paper_visual_provenance.json")
        rendered_layers = ctx.state.get("rendered_layers")
        if not isinstance(rendered_layers, dict):
            rendered_layers = {}
        current_color_system = _current_landing_color_system(ctx)
        catalog = build_landing_asset_catalog(
            provenance,
            rendered_layers=rendered_layers,
        )
        plan = build_landing_visual_plan(
            provenance,
            rendered_layers=rendered_layers,
            current_color_system=current_color_system,
            brief=brief,
        )
        atomic_write_json(attempt_dir / "landing_asset_catalog.json", catalog)
        atomic_write_json(attempt_dir / "landing_visual_plan.json", plan)

        manifest = {
            "kind": "external_landing_author_input",
            "version": 1,
            "run_id": ctx.run_id,
            "brief": brief,
            "evidence_files": copied,
            "visual_plan": "landing_visual_plan.json",
            "asset_catalog": "landing_asset_catalog.json",
            "eligible_asset_count": catalog["eligible_asset_count"],
            "recommended_asset_count": plan["recommended_asset_count"],
            "required_color_system": current_color_system,
            "repair_inputs": repair_inputs,
            "runtime_skills": runtime_skills,
            "output_contract": {
                "html": "index.html",
                "done_marker": "designer_author_done.json",
            },
            "missing_required": missing,
        }
        atomic_write_json(attempt_dir / "author_input_manifest.json", manifest)
        if missing:
            self._fail(
                ctx,
                "external_landing_missing_staged_evidence",
                f"landing author staging is missing required evidence: {', '.join(missing)}",
                attempt_dir,
                payload=manifest,
            )
            return False
        return True

    def _build_prompt(
        self,
        *,
        brief: str,
        attempt_dir: Path,
        repair_feedback: dict[str, Any] | None,
    ) -> str:
        model = str(getattr(self.settings, "designer_author_model", "") or "").strip()
        model_line = f"Model hint: {model}\n" if model else ""
        dossier_instruction = (
            "paper_memory_dossier is staged and may be used for targeted detail."
            if (attempt_dir / "paper_memory_dossier.json").exists()
            else "The paper_memory_dossier is absent; fall back to paper_memory and paper_evidence_packs."
        )
        repair_block = _repair_prompt_block(repair_feedback)
        runtime_skill_instruction = (
            "Read runtime_skills/index.md first. Open only the selected artifact skill's "
            "SKILL.md and supporting resources needed for this attempt; do not read every "
            "staged skill resource."
            if (attempt_dir / "runtime_skills" / "index.md").exists()
            else "No runtime skill snapshot is staged for this run."
        )
        visual_plan = _read_json(attempt_dir / "landing_visual_plan.json")
        experience_contract = (
            visual_plan.get("visual_experience_contract")
            if isinstance(visual_plan.get("visual_experience_contract"), dict)
            else {}
        )
        color_contract = (
            experience_contract.get("color")
            if isinstance(experience_contract.get("color"), dict)
            else {}
        )
        current_color_system = color_contract.get("current_color_system")
        color_line = json.dumps(
            current_color_system,
            ensure_ascii=True,
            sort_keys=True,
        )
        three_d = (
            experience_contract.get("three_d")
            if isinstance(experience_contract.get("three_d"), dict)
            else {}
        )
        three_d_line = (
            "3D is explicitly enabled by the source or brief; keep it local, evidence-grounded, and optional to understanding the page."
            if three_d.get("enabled")
            else "3D is disabled for this run; do not add WebGL, Three.js, canvas scenes, or 3D decoration."
        )
        return f"""You are the external coding-agent author for an academic paper project page.

Work only inside this directory:
{attempt_dir}
{model_line}
User brief:
{brief}

Execution contract:
{repair_block}
- Directly build a self-contained dynamic academic project page for desktop browsers.
- Output exactly index.html and designer_author_done.json in the current directory.
- {runtime_skill_instruction}
- Then read author_input_manifest.json and landing_visual_plan.json, and inspect landing_asset_catalog.json for the full eligible source catalog.
- Use paper_memory, paper_visual_provenance, paper_evidence_packs, and layers as the paper evidence. {dossier_instruction} The visual plan is a recommendation, not a restriction.
- Use real paper figures and tables from layers, with their original aspect ratios and source IDs.
- Optional unmatched reserves listed in landing_visual_plan.json are shortfall-only supporting visuals; use at most two and never ground method or results claims in them.
- Meet landing_visual_plan.validation_targets.required_unique_source_visuals with unique, visible source assets. Put data-source-id equal to its catalog asset_id on the img/source itself or its nearest figure/picture wrapper; src remains the catalog output_file path.
- Keep titles, prose, equations, labels, captions, and tables as native editable text whenever they are not part of an original paper crop.
- Make the paper, method, findings, and visual evidence the first-viewport signals. Use a visual-first editorial research-page composition, not a SaaS card wall.
- Include substantive semantic sections for paper identity/overview, method, results, data or qualitative evidence, and analysis where the evidence supports them.
- Follow landing_visual_plan.visual_experience_contract. Use an academic-light editorial surface and exactly one primary accent. Current required_color_system: {color_line}
- When required_color_system includes palette_id and css_variables, set that palette_id as data-palette-id on the html root and define every exact --poster-* variable from the contract on :root. Do not introduce authored shell colors outside that palette; original source visuals keep their native colors.
- Use 3 to 8 restrained inline SVG icons for functional cues. Every icon-only control must have an accessible name.
- Include at least one purposeful source-grounded interaction for inspecting, comparing, or navigating paper evidence.
- Use desktop-first CSS and only purposeful page dynamics. If animation or transition is present, add a prefers-reduced-motion: reduce fallback that removes it without hiding content.
- Core content must not depend on JavaScript reveal to become visible; JavaScript may enhance already-visible content only.
- {three_d_line}
- Inline JavaScript is allowed for local interaction, but it must not perform network requests or load code.
- Use no remote assets or scripts. Do not use CDNs, @import, remote fonts, data URLs, file URLs, iframes, or package installation.
- Every local asset reference must remain inside this attempt directory and must resolve to a staged file.
- Do not use poster_plan_contract, paper_visual_storyboard, or poster-selected visual IDs to limit source evidence.
- Do not invoke AutoDesign tools or ordinary DesignerLoop. Author the files directly and exit.

When complete, write designer_author_done.json as a JSON object describing the result, then exit.
"""

    def _invoke_author_command(
        self,
        command: str,
        *,
        prompt: str,
        attempt_dir: Path,
        timeout_s: int,
        run_id: str = "",
        attempt: int = 0,
        ctx: ToolContext | None = None,
    ) -> _InvocationResult:
        checkpoint = lambda phase: context_cancellation_checkpoint(ctx, phase)
        checkpoint("external_landing.process.before_command_parse")
        stdout_path = attempt_dir / "designer_author_stdout.log"
        stderr_path = attempt_dir / "designer_author_stderr.log"
        started = time.monotonic()
        try:
            cmd = shlex.split(command)
        except ValueError as exc:
            stderr_path.write_text(str(exc), encoding="utf-8")
            return _InvocationResult("error", "external_landing_command_parse_error", None, False, 0.0)
        if not cmd:
            return _InvocationResult("error", "external_landing_empty_command", None, False, 0.0)

        env = harness_subprocess_env(
            os.environ,
            harness=str(getattr(self.settings, "designer_author_harness", "") or ""),
            api_key=getattr(self.settings, "harness_api_key", None),
        )
        author_python = env.get("AUTODESIGN_AUTHOR_PYTHON", "").strip() or sys.executable
        env["AUTODESIGN_AUTHOR_PYTHON"] = author_python
        env.setdefault("DESIGN_ANYTHING_AUTHOR_PYTHON", author_python)
        sensitive_values = _process_sensitive_values(
            cmd,
            getattr(self.settings, "harness_api_key", None),
            env,
        )
        raw_stdout_path = attempt_dir / ".landing_author_stdout.tmp"
        raw_stderr_path = attempt_dir / ".landing_author_stderr.tmp"
        process_result = run_external_author_process(
            ExternalAuthorProcessRequest(
                run_id=run_id or f"landing:{attempt_dir.resolve()}",
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
        checkpoint("external_landing.process.after_process")
        timed_out = process_result.timed_out
        returncode = process_result.returncode
        stdout = process_result.stdout
        stderr = process_result.stderr
        checkpoint("external_landing.process.before_redacted_log_write")
        stdout_path.write_text(
            _redact_process_text(stdout, sensitive_values),
            encoding="utf-8",
        )
        checkpoint("external_landing.process.between_redacted_log_writes")
        stderr_path.write_text(
            _redact_process_text(stderr, sensitive_values),
            encoding="utf-8",
        )
        checkpoint("external_landing.process.after_redacted_log_write")
        checkpoint("external_landing.process.before_raw_log_cleanup")
        raw_stdout_path.unlink(missing_ok=True)
        raw_stderr_path.unlink(missing_ok=True)
        checkpoint("external_landing.process.after_raw_log_cleanup")

        elapsed = time.monotonic() - started
        checkpoint("external_landing.process.before_marker_reads")
        output_exists = (attempt_dir / "index.html").exists()
        done_exists = (attempt_dir / "designer_author_done.json").exists()
        checkpoint("external_landing.process.after_marker_reads")
        if process_result.status == "selected":
            reason = "attempt_selected"
        elif timed_out:
            reason = "external_landing_command_timeout"
        elif process_result.status == "spawn_error":
            reason = "external_landing_command_failed"
        elif returncode not in (0, None):
            reason = "external_landing_command_failed"
        elif not output_exists or not done_exists:
            reason = "external_landing_missing_output_contract"
        else:
            reason = "process_exit"
        result = _InvocationResult(
            (
                "selected"
                if reason == "attempt_selected"
                else "ok" if reason == "process_exit" else "error"
            ),
            reason,
            returncode,
            timed_out,
            elapsed,
        )
        checkpoint("external_landing.process.before_process_log_write")
        atomic_write_json(attempt_dir / "landing_author_process.json", asdict(result))
        checkpoint("external_landing.process.after_process_log_write")
        return result

    def _promote(
        self,
        ctx: ToolContext,
        *,
        attempt_dir: Path,
        diagnostics: dict[str, Any],
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
                candidate_id=candidate_id or "untracked-landing-candidate",
                expected_run_identity=ctx.run_directory_identity,
            ) as leased_run_dir:
                with leased_promotion_tool_context(ctx, leased_run_dir):
                    return self._promote(
                        ctx,
                        attempt_dir=(
                            leased_run_dir
                            / attempt_dir.relative_to(original_run_dir)
                        ),
                        diagnostics=diagnostics,
                        browser_audit=browser_audit,
                        candidate_id=candidate_id,
                        acceptance_path=acceptance_path,
                        selection_owned=False,
                        _normal_lease_owned=True,
                    )
        def checkpoint(phase: str) -> None:
            context_cancellation_checkpoint(ctx, phase)
            assert_promotion_run_unchanged()
        checkpoint("external_landing.promotion.start")
        if candidate_id is not None:
            assert_promotion_allowed(
                run_dir=ctx.run_dir,
                candidate_id=candidate_id,
            )
        final_dir = ctx.run_dir / "final"
        source_html = attempt_dir / "index.html"
        checkpoint("external_landing.promotion.before_staging")
        staging_dir = Path(
            tempfile.mkdtemp(prefix=".landing-final-staging-", dir=ctx.run_dir)
        )
        try:
            checkpoint("external_landing.promotion.after_staging")
            staged_html = staging_dir / "index.html"
            final_html = final_dir / "index.html"
            checkpoint("external_landing.promotion.before_copy")
            shutil.copy2(source_html, staged_html)
            ensure_editable_html_contract(staged_html, "landing")
            _copy_landing_dependency_closure(attempt_dir, staging_dir, source_html)
            shutil.copy2(
                attempt_dir / "designer_author_done.json",
                staging_dir / "designer_author_done.json",
            )
            for name in (
                "landing_asset_catalog.json",
                "landing_visual_plan.json",
                "landing_validation.json",
                "landing_browser_qa.json",
            ):
                source = attempt_dir / name
                if source.is_file():
                    shutil.copy2(source, staging_dir / name)
            atomic_write_json(
                staging_dir / "landing_validation.json",
                diagnostics,
            )
            if browser_audit is not None:
                atomic_write_json(
                    staging_dir / "landing_browser_qa.json",
                    browser_audit,
                )
            checkpoint("external_landing.promotion.after_copy")

            staged_preview = staging_dir / "preview.png"
            preview = screenshot_html(
                staged_html,
                staged_preview,
                viewport_width=1440,
                viewport_height=900,
                full_page=True,
                prime_local_media=True,
                max_edge=4096,
            )
            checkpoint("external_landing.promotion.after_preview")
            preview_backend = str(getattr(preview, "backend", "unknown") or "unknown")
            preview_warnings = list(getattr(preview, "warnings", []) or [])
            if not staged_preview.is_file():
                raise RuntimeError("landing preview renderer produced no preview image")

            staged_card_preview = staging_dir / "card_preview.png"
            screenshot_html(
                staged_html,
                staged_card_preview,
                viewport_width=1440,
                viewport_height=900,
                full_page=False,
                prime_local_media=True,
                max_edge=1440,
            )
            checkpoint("external_landing.promotion.after_card_preview")
            if not staged_card_preview.is_file():
                raise RuntimeError(
                    "landing card preview renderer produced no preview image"
                )

            final_preview = final_dir / "preview.png"
            delivery_assessment = assess_delivery_issues(
                "landing",
                [
                    finding
                    for finding in diagnostics.get("findings") or []
                    if isinstance(finding, dict)
                ],
            )
            if delivery_assessment.safety_state == "blocked":
                raise ValueError("blocked Landing validation cannot be promoted")
            direct_final = {
                "source": "external_landing_author",
                "artifact_type": "landing",
                "acceptance_path": acceptance_path,
                "attempt_dir": str(attempt_dir),
                "html": str(final_html),
                "html_sha256": sha256_file(staged_html),
                "sidecar_sha256": {
                    name: sha256_file(staging_dir / name)
                    for name in (
                        "landing_asset_catalog.json",
                        "landing_visual_plan.json",
                        "landing_validation.json",
                        "landing_browser_qa.json",
                    )
                    if (staging_dir / name).is_file()
                },
                "quality_status": delivery_assessment.safety_state,
                "quality_diagnostics": [
                    issue.issue_id
                    for issue in delivery_assessment.quality_diagnostics
                ],
                "preview": str(final_preview),
                "card_preview_relative_path": "final/card_preview.png",
                "card_preview_sha256": sha256_file(staged_card_preview),
                "preview_backend": preview_backend,
                "preview_warnings": preview_warnings,
                "validation": diagnostics,
            }
            checkpoint("external_landing.promotion.before_manifest")
            atomic_write_json(
                staging_dir / "landing_author_manifest.json",
                direct_final,
            )
            checkpoint("external_landing.promotion.before_publish")
            _atomic_replace_directory(
                staging_dir,
                final_dir,
                post_publish=lambda: checkpoint(
                    "external_landing.promotion.after_publish"
                ),
            )
        except BaseException:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

        checkpoint("external_landing.promotion.before_state")
        final_preview = final_dir / "preview.png"
        ctx.state["artifact_type"] = "landing"
        ctx.state["landing_author_direct_final"] = direct_final
        ctx.state["designer_author_direct_final"] = direct_final
        ctx.state["composition"] = CompositionArtifacts(
            html_path=str(final_html),
            html_artifact_path=str(final_html),
            preview_path=str(final_preview) if final_preview.exists() else None,
        )
        ctx.state["last_composite_payload"] = {
            "render_mode": "external_landing_author_html",
            "html_relative_path": "final/index.html",
            "preview_relative_path": "final/preview.png" if final_preview.exists() else None,
        }
        ctx.state["finalized"] = True
        ctx.state["designer_author_result"] = {
            "status": "ok",
            "source": "external_landing_author",
            "attempt_dir": str(attempt_dir),
            "final_html": str(final_html),
        }
        log(
            "landing_author.promoted",
            mode="external",
            attempt_dir=str(attempt_dir),
            html=str(final_html),
            preview=str(final_preview) if final_preview.exists() else "",
        )

    def _fail_from_tool(
        self,
        ctx: ToolContext,
        tool_name: str,
        result: ToolResultRecord,
        attempt_dir: Path,
    ) -> None:
        self._fail(
            ctx,
            f"external_landing_{tool_name}_error",
            result.error_message or f"{tool_name} failed",
            attempt_dir,
            payload=result.payload,
        )

    def _fail(
        self,
        ctx: ToolContext,
        issue_id: str,
        message: str,
        attempt_dir: Path,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        failure = {
            "type": "external_landing_author",
            "issue_id": issue_id,
            "reason": issue_id,
            "message": message,
            "attempt_dir": str(attempt_dir),
            "payload": payload or {},
        }
        ctx.state["designer_contract_abort"] = True
        ctx.state["designer_api_error"] = failure
        ctx.state["landing_author_failure"] = failure
        ctx.state["designer_author_result"] = {"status": "error", **failure}
        ctx.state["finalized"] = False
        author_dir = ctx.run_dir / "landing_author"
        author_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(author_dir / "failure.json", failure)
        log(
            "landing_author.fail",
            mode="external",
            issue_id=issue_id,
            message=message[:800],
            attempt_dir=str(attempt_dir),
        )


def capture_landing_attempt_candidate(
    *,
    ctx: ToolContext,
    attempt_dir: Path,
    attempt: int,
    max_attempts: int,
    diagnostics: dict[str, Any],
) -> AttemptCandidate:
    source_html = attempt_dir / "index.html"
    preview_paths: list[str] = []
    preview_path = attempt_dir / "attempt_preview.png"
    if source_html.is_file():
        try:
            screenshot_html(
                source_html,
                preview_path,
                viewport_width=1440,
                viewport_height=900,
                full_page=True,
                prime_local_media=True,
                max_edge=4096,
            )
        except Exception as exc:  # noqa: BLE001
            log(
                "attempt_candidate.preview_failed",
                artifact_type="landing",
                attempt=attempt,
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
    if preview_path.is_file():
        preview_paths.append("attempt_preview.png")

    assessment = assess_delivery_issues(
        "landing",
        [
            item
            for item in diagnostics.get("findings") or []
            if isinstance(item, dict)
        ],
    )
    root = Path(os.path.abspath(os.fspath(attempt_dir)))
    anchored_source_html = _anchored_landing_dependency_path(source_html, root)
    dependencies = [
        path.relative_to(root).as_posix()
        for path in _landing_dependency_closure_files(attempt_dir, source_html)
        if path != anchored_source_html
    ]
    browser_resource_paths = [
        path for path in dependencies if is_browser_preview_resource_path(path)
    ]
    for name in (
        "designer_author_done.json",
        "landing_asset_catalog.json",
        "landing_visual_plan.json",
        "landing_browser_qa.json",
    ):
        path = attempt_dir / name
        if path.is_file() and name not in dependencies:
            dependencies.append(name)
    candidate = capture_attempt_candidate(
        run_dir=ctx.run_dir,
        attempt_dir=attempt_dir,
        artifact_type="landing",
        attempt=attempt,
        max_attempts=max_attempts,
        source_path="index.html",
        dependency_paths=sorted(dependencies),
        preview_paths=preview_paths,
        validation_summary_path="landing_validation.json",
        safety_state=assessment.safety_state,
        hard_blockers=list(assessment.hard_blockers),
        warnings=list(assessment.quality_diagnostics),
        browser_resource_paths=browser_resource_paths,
    )
    log(
        "attempt_candidate.available",
        run_id=ctx.run_id,
        artifact_type="landing",
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
        catalog = _read_json(attempt_snapshot / "landing_asset_catalog.json")
        trusted_hashes = _trusted_landing_source_hashes(
            ctx.run_dir,
            catalog,
            require_existing=True,
        )
        diagnostics = _validate_landing_output(
            attempt_snapshot,
            trusted_source_hashes=trusted_hashes,
        )
        metrics = diagnostics.get("metrics")
        metrics = metrics if isinstance(metrics, dict) else {}
        browser_audit = audit_landing_html(
            source_html,
            required_source_ids=metrics.get("used_source_visual_ids") or [],
        )
        diagnostics = _merge_landing_browser_audit(diagnostics, browser_audit)
        assessment = assess_delivery_issues(
            "landing",
            [
                finding
                for finding in diagnostics.get("findings") or []
                if isinstance(finding, dict)
            ],
        )
        if assessment.safety_state == "blocked":
            raise ValueError("selected Landing candidate failed fresh validation")
        diagnostics["delivery_assessment"] = {
            "safety_state": assessment.safety_state,
            "quality_diagnostics": [
                issue.model_dump(mode="json")
                for issue in assessment.quality_diagnostics
            ],
        }
    else:
        diagnostics = _read_json(
            ctx.run_dir / candidate.validation_summary_relative_path
        )
    ExternalLandingAuthor(ctx.settings, "")._promote(
        ctx,
        attempt_dir=attempt_snapshot,
        diagnostics=diagnostics,
        browser_audit=browser_audit,
        candidate_id=candidate.candidate_id,
        acceptance_path="user_selected_attempt",
        selection_owned=True,
    )


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
    text = "" if value is None else str(value)
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


def _compact_validation_feedback(feedback: dict[str, Any]) -> dict[str, Any]:
    findings = []
    for finding in feedback.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        findings.append({
            key: finding[key]
            for key in ("issue_id", "message", "required", "actual")
            if key in finding
        })
    metrics = feedback.get("metrics") if isinstance(feedback.get("metrics"), dict) else {}
    return {
        "accepted": False,
        "findings": findings,
        "metrics": {
            key: metrics[key]
            for key in (
                "semantic_section_count",
                "word_count",
                "eligible_asset_count",
                "required_source_visual_count",
                "used_source_visual_count",
                "inline_svg_icon_count",
                "interactive_control_count",
                "source_grounded_interaction_count",
                "motion_declaration_count",
                "has_prefers_reduced_motion",
                "icon_only_control_count",
                "inaccessible_icon_only_control_count",
                "javascript_reveal_dependency_count",
            )
            if key in metrics
        },
    }


def _repair_prompt_block(feedback: dict[str, Any] | None) -> str:
    if not isinstance(feedback, dict):
        return "- This is the initial authoring attempt; create index.html directly."
    lines = [
        "- PATCH-FIRST REPAIR ATTEMPT: index.html is the prior candidate. Patch it in place; do not restart from a blank page.",
        "- Read landing_validation.json first. Fix every exact deterministic finding below while preserving valid content:",
    ]
    for finding in feedback.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        issue_id = str(finding.get("issue_id") or "landing_validation_finding")
        message = str(finding.get("message") or "repair the deterministic validation finding")
        counts = ""
        if "required" in finding or "actual" in finding:
            counts = f" required={finding.get('required')} actual={finding.get('actual')}"
        lines.append(f"  - {issue_id}: {message}{counts}")
    return "\n".join(lines)


def _has_full_ingest_context(ctx: ToolContext) -> bool:
    return bool(
        isinstance(ctx.state.get("paper_memory"), dict)
        and isinstance(ctx.state.get("paper_visual_provenance"), dict)
        and ctx.layers_dir.exists()
        and ctx.layers_dir.is_dir()
    )


def _trusted_landing_source_hashes(
    run_dir: Path,
    catalog: dict[str, Any],
    *,
    require_existing: bool = False,
    require_catalog_match: bool = True,
) -> dict[str, str]:
    anchor_path = run_dir / "landing_trusted_source_hashes.json"
    declared_hashes = {
        str(asset.get("asset_id") or "").strip(): str(
            asset.get("output_sha256") or ""
        ).strip().lower()
        for asset in catalog.get("assets") or []
        if (
            isinstance(asset, dict)
            and str(asset.get("asset_id") or "").strip()
            and str(asset.get("output_file") or "").strip()
            and re.fullmatch(
                r"[0-9a-fA-F]{64}",
                str(asset.get("output_sha256") or "").strip(),
            )
        )
    }
    expected_ids = {
        str(asset.get("asset_id") or "").strip()
        for asset in catalog.get("assets") or []
        if (
            isinstance(asset, dict)
            and str(asset.get("asset_id") or "").strip()
            and str(asset.get("output_file") or "").strip()
        )
    }
    if anchor_path.is_file():
        anchor = _read_json(anchor_path)
        hashes = anchor.get("hashes")
        if (
            anchor.get("kind") != "landing_trusted_source_hashes"
            or anchor.get("version") != 1
            or not isinstance(hashes, dict)
        ):
            raise ValueError("landing trusted source hash anchor is invalid")
        trusted = {
            str(asset_id): str(value).strip().lower()
            for asset_id, value in hashes.items()
            if str(asset_id).strip() and re.fullmatch(r"[0-9a-fA-F]{64}", str(value).strip())
        }
        if require_catalog_match and expected_ids != set(trusted):
            raise ValueError(
                "landing source catalog does not match the trusted source hash anchor"
            )
        if any(trusted.get(asset_id) != value for asset_id, value in declared_hashes.items()):
            raise ValueError(
                "landing trusted source hash anchor disagrees with ingest provenance"
            )
        return trusted
    if require_existing:
        raise ValueError(
            "landing resume requires the trusted source hash anchor from the original run"
        )

    trusted: dict[str, str] = {}
    root = run_dir.resolve()
    for asset in catalog.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        asset_id = str(asset.get("asset_id") or "").strip()
        output_file = str(asset.get("output_file") or "").strip()
        if not asset_id or not output_file:
            continue
        candidate = (root / output_file).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.is_file():
            actual_hash = sha256_file(candidate).lower()
            declared_hash = declared_hashes.get(asset_id)
            if declared_hash and actual_hash != declared_hash:
                raise ValueError(
                    f"landing source asset {asset_id} disagrees with ingest provenance"
                )
            trusted[asset_id] = actual_hash
    if not expected_ids.issubset(trusted):
        raise ValueError("landing source assets could not be bound to trusted bytes")
    atomic_write_json(
        anchor_path,
        {
            "kind": "landing_trusted_source_hashes",
            "version": 1,
            "source": "pre_author_actual_source_bytes",
            "hashes": trusted,
        },
    )
    anchor_path.chmod(0o444)
    return trusted


def _local_visual_matches_hash(
    attempt_dir: Path,
    source_path: str,
    expected_hash: str,
) -> bool:
    if not expected_hash:
        return False
    candidate = _local_dependency_path(attempt_dir, source_path)
    return bool(
        candidate is not None
        and candidate.is_file()
        and sha256_file(candidate).lower() == expected_hash
    )


def _validate_landing_output(
    attempt_dir: Path,
    *,
    trusted_source_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    html_path = attempt_dir / "index.html"
    done_path = attempt_dir / "designer_author_done.json"
    findings: list[dict[str, Any]] = []
    if not html_path.exists():
        findings.append(_finding("landing_missing_index", "index.html was not produced"))
    if not done_path.exists():
        findings.append(
            _finding("landing_missing_done_marker", "designer_author_done.json was not produced")
        )
    elif not _contains_json_object(done_path):
        findings.append(
            _finding("landing_invalid_done_marker", "designer_author_done.json must contain a JSON object")
        )
    if not html_path.exists():
        return {"accepted": False, "findings": findings, "metrics": {}}

    html = html_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    catalog = _read_json(attempt_dir / "landing_asset_catalog.json")
    plan = _read_json(attempt_dir / "landing_visual_plan.json")
    eligible_assets = [
        asset
        for asset in (catalog.get("assets") or [])
        if isinstance(asset, dict)
        and str(asset.get("asset_id") or "").strip()
        and str(asset.get("output_file") or "").strip()
    ]
    eligible_by_id = {
        str(asset["asset_id"]): {
            "path": _normalized_local_reference(str(asset["output_file"])),
            "fingerprint": str(asset.get("output_sha256") or "").strip()
            or _normalized_local_reference(str(asset["output_file"])),
            "tier": str(asset.get("visual_selection_tier") or "eligible"),
        }
        for asset in eligible_assets
    }
    eligible_paths = {
        value["path"]
        for value in eligible_by_id.values()
        if value["path"]
    }
    references: list[tuple[str, str]] = []
    for tag_name, attribute in (
        ("img", "src"),
        ("source", "src"),
        ("video", "src"),
        ("video", "poster"),
        ("audio", "src"),
        ("script", "src"),
        ("link", "href"),
        ("iframe", "src"),
        ("object", "data"),
        ("embed", "src"),
    ):
        for tag in soup.find_all(tag_name):
            value = str(tag.get(attribute) or "").strip()
            if value:
                references.append((f"{tag_name}[{attribute}]", value))
    for tag in soup.find_all(["img", "source"]):
        srcset = str(tag.get("srcset") or "").strip()
        for candidate in srcset.split(","):
            value = candidate.strip().split(" ", 1)[0]
            if value:
                references.append((f"{tag.name}[srcset]", value))

    css_sources: list[tuple[str, Path]] = [
        (tag.get_text(" "), attempt_dir) for tag in soup.find_all("style")
    ]
    for link in soup.find_all("link", href=True):
        rel = {str(value).lower() for value in (link.get("rel") or [])}
        if "stylesheet" in rel:
            dependency_path = _local_dependency_path(attempt_dir, str(link.get("href") or ""))
            if dependency_path is not None:
                try:
                    css_sources.append((dependency_path.read_text(encoding="utf-8"), dependency_path.parent))
                except (OSError, UnicodeError):
                    pass
    css_text = "\n".join(
        [content for content, _ in css_sources]
        + [str(tag.get("style") or "") for tag in soup.find_all(style=True)]
    )
    if re.search(r"@import\b", css_text, re.IGNORECASE):
        findings.append(_finding("landing_remote_reference", "CSS @import is not allowed"))
    based_references: list[tuple[str, str, Path]] = [
        (origin, value, attempt_dir) for origin, value in references
    ]
    for content, base_dir in css_sources:
        for match in _CSS_URL_RE.finditer(content):
            based_references.append(("css[url]", match.group(2).strip(), base_dir))
    for tag in soup.find_all(style=True):
        for match in _CSS_URL_RE.finditer(str(tag.get("style") or "")):
            based_references.append(("css[url]", match.group(2).strip(), attempt_dir))

    normalized_local_references: set[str] = set()
    for origin, value, base_dir in based_references:
        issue = _local_reference_issue(attempt_dir, value, base_dir=base_dir)
        if issue:
            findings.append(
                _finding(
                    "landing_remote_reference" if issue == "remote" else "landing_invalid_local_reference",
                    f"{origin} must reference an existing staged local file: {value}",
                )
            )
        else:
            dependency_path = _local_dependency_path(attempt_dir, value, base_dir=base_dir)
            if dependency_path is not None:
                normalized_local_references.add(
                    dependency_path.relative_to(attempt_dir.resolve()).as_posix()
                )

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        scheme = urlsplit(href).scheme.lower()
        if scheme in {"data", "file", "javascript"}:
            findings.append(_finding("landing_unsafe_link", f"unsafe link URL: {href}"))
    for tag in soup.find_all(["iframe", "object", "embed"]):
        findings.append(_finding("landing_unsafe_embed", f"{tag.name} is not allowed"))
    script_blocks = [
        tag.get_text(" ") for tag in soup.find_all("script") if not tag.get("src")
    ]
    for script in soup.find_all("script", src=True):
        dependency = _read_local_text_dependency(attempt_dir, str(script.get("src") or ""))
        if dependency is not None:
            script_blocks.append(dependency)
    inline_script = "\n".join(script_blocks)
    if re.search(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource)\s*\(", inline_script):
        findings.append(
            _finding("landing_network_script", "inline scripts must not perform network requests")
        )

    motion_declaration_count = _motion_declaration_count(css_text, inline_script)
    has_prefers_reduced_motion = bool(
        re.search(
            r"@media\s*(?:not\s+|only\s+)?[^\{]*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)",
            css_text,
            re.IGNORECASE,
        )
    )
    has_effective_reduced_motion = _has_effective_reduced_motion_fallback(css_text)
    if motion_declaration_count and not has_effective_reduced_motion:
        findings.append(
            _finding(
                "landing_motion_without_reduced_motion",
                "animation or transition requires a prefers-reduced-motion: reduce fallback",
            )
        )

    interactive_controls = _interactive_controls(soup)
    icon_only_controls = [
        control for control in interactive_controls if _is_icon_only_control(control)
    ]
    inaccessible_icon_only_controls = [
        control
        for control in icon_only_controls
        if not _has_accessible_control_name(control, soup)
    ]
    if inaccessible_icon_only_controls:
        findings.append({
            "issue_id": "landing_icon_control_missing_accessible_name",
            "message": "every icon-only control must have an accessible name",
            "required": 0,
            "actual": len(inaccessible_icon_only_controls),
        })

    javascript_reveal_dependency_count = _javascript_reveal_dependency_count(
        css_text,
        inline_script,
    )
    if javascript_reveal_dependency_count:
        findings.append({
            "issue_id": "landing_javascript_reveal_dependency",
            "message": "core content must be visible before JavaScript enhancement runs",
            "required": 0,
            "actual": javascript_reveal_dependency_count,
        })

    hidden_element_ids = _hidden_element_ids(soup, attempt_dir)
    optional_reserve_ids = {
        str(asset.get("asset_id") or "")
        for asset in (plan.get("optional_reserve_assets") or [])
        if isinstance(asset, dict)
    }
    visibly_placed_fingerprints: set[str] = set()
    visibly_placed_source_ids: set[str] = set()
    for tag in soup.find_all(["img", "source"]):
        source_path = _normalized_local_reference(str(tag.get("src") or ""))
        source_id = _effective_source_id(tag)
        expected = eligible_by_id.get(source_id)
        trusted_hash = str(
            (trusted_source_hashes or {}).get(source_id) or ""
        ).strip().lower()
        if (
            expected is not None
            and expected["path"] == source_path
            and expected["tier"] == "eligible"
            and (
                trusted_source_hashes is None
                or _local_visual_matches_hash(attempt_dir, source_path, trusted_hash)
            )
            and id(tag) not in hidden_element_ids
        ):
            visibly_placed_fingerprints.add(expected["fingerprint"])
            visibly_placed_source_ids.add(source_id)
    valid_source_fingerprints: set[str] = set()
    for tag in soup.find_all(["img", "source"]):
        source_path = _normalized_local_reference(str(tag.get("src") or ""))
        source_id = _effective_source_id(tag)
        if source_path not in eligible_paths and source_id not in eligible_by_id:
            continue
        if not source_id:
            matching_fingerprints = {
                candidate["fingerprint"]
                for candidate in eligible_by_id.values()
                if candidate["path"] == source_path
            }
            if matching_fingerprints & visibly_placed_fingerprints:
                continue
            findings.append(
                _finding(
                    "landing_source_visual_missing_id",
                    f"paper source visual must declare data-source-id: {source_path}",
                )
            )
            continue
        expected = eligible_by_id.get(source_id)
        if expected is None or expected["path"] != source_path:
            expected_source_ids = sorted(
                candidate_id
                for candidate_id, candidate in eligible_by_id.items()
                if candidate["path"] == source_path
            )
            expected_label = ", ".join(expected_source_ids) or "a catalog ID"
            findings.append(
                _finding(
                    "landing_source_visual_id_path_mismatch",
                    f"data-source-id {source_id} does not correspond to local asset "
                    f"path {source_path}; expected data-source-id {expected_label}",
                )
            )
            continue
        trusted_hash = str(
            (trusted_source_hashes or {}).get(source_id) or ""
        ).strip().lower()
        if trusted_source_hashes is not None and not trusted_hash:
            findings.append(
                _finding(
                    "landing_source_visual_missing_trusted_hash",
                    f"paper source visual {source_id} has no trusted source-byte hash",
                )
            )
            continue
        if trusted_source_hashes is not None and not _local_visual_matches_hash(
            attempt_dir,
            source_path,
            trusted_hash,
        ):
            findings.append(
                _finding(
                    "landing_source_visual_hash_mismatch",
                    f"paper source visual {source_id} bytes do not match the immutable ingest source",
                )
            )
            continue
        if id(tag) in hidden_element_ids:
            if expected["fingerprint"] in visibly_placed_fingerprints:
                continue
            findings.append(
                _finding(
                    "landing_source_visual_not_visible",
                    f"paper source visual {source_id} is hidden or has zero rendered size",
                )
            )
            continue
        if expected["tier"] == "reserve_unmatched":
            if source_id not in optional_reserve_ids:
                findings.append(
                    _finding(
                        "landing_unapproved_unmatched_reserve",
                        f"unmatched reserve {source_id} is not approved by the optional shortfall plan",
                    )
                )
            continue
        if expected["tier"] == "eligible":
            valid_source_fingerprints.add(expected["fingerprint"])

    validation_targets = (
        plan.get("validation_targets")
        if isinstance(plan.get("validation_targets"), dict)
        else {}
    )
    if "required_unique_source_visuals" in validation_targets:
        required_source_visuals = int(
            validation_targets.get("required_unique_source_visuals") or 0
        )
    else:
        required_source_visuals = len({
            value["fingerprint"]
            for value in eligible_by_id.values()
            if value["tier"] == "eligible"
        })
    required_source_visuals = min(8, max(0, required_source_visuals))
    if len(valid_source_fingerprints) < required_source_visuals:
        findings.append({
            "issue_id": "landing_insufficient_source_visual_density",
            "message": "landing page does not place enough unique plan-eligible paper source visuals",
            "required": required_source_visuals,
            "actual": len(valid_source_fingerprints),
        })

    experience_contract = (
        plan.get("visual_experience_contract")
        if isinstance(plan.get("visual_experience_contract"), dict)
        else {}
    )
    interaction_contract = (
        experience_contract.get("interaction")
        if isinstance(experience_contract.get("interaction"), dict)
        else {}
    )
    source_grounded_interaction_count = _source_grounded_interaction_count(
        interactive_controls,
        soup,
        visibly_placed_source_ids,
    )
    if interaction_contract.get("source_grounded_required") and not source_grounded_interaction_count:
        findings.append({
            "issue_id": "landing_missing_source_grounded_interaction",
            "message": "landing page requires a control that inspects, compares, or navigates visible paper evidence",
            "required": 1,
            "actual": 0,
        })

    sections = soup.find_all(["section", "article"])
    if len(sections) < 3:
        findings.append(
            _finding("landing_insufficient_sections", "landing page needs at least three semantic sections")
        )
    if soup.find("h1") is None:
        findings.append(_finding("landing_missing_title", "landing page needs a visible h1 paper title"))
    for tag in soup.find_all(["script", "style", "template", "noscript"]):
        tag.extract()
    word_count = len(re.findall(r"\b[\w'-]+\b", soup.get_text(" ", strip=True)))
    if word_count < 80:
        findings.append(
            _finding("landing_insufficient_content", "landing page needs substantive paper-grounded text")
        )
    semantic_blob = " ".join(
        " ".join(
            [
                str(section.get("id") or ""),
                " ".join(section.get("class") or []),
                str(section.get("data-section-role") or ""),
                section.get_text(" ", strip=True)[:300],
            ]
        ).lower()
        for section in sections
    )
    if not any(term in semantic_blob for term in ("method", "framework", "pipeline", "approach")):
        findings.append(_finding("landing_missing_method_section", "landing page needs a method section"))
    if not any(term in semantic_blob for term in ("result", "finding", "experiment", "benchmark")):
        findings.append(_finding("landing_missing_results_section", "landing page needs a results section"))
    color_contract = (
        experience_contract.get("color")
        if isinstance(experience_contract.get("color"), dict)
        else {}
    )
    required_color_system = color_contract.get("current_color_system")
    palette_audit: dict[str, Any] = {}
    if (
        isinstance(required_color_system, dict)
        and required_color_system.get("palette_id")
        and isinstance(required_color_system.get("css_variables"), dict)
        and required_color_system.get("css_variables")
    ):
        palette_audit = validate_artifact_palette(
            html,
            css_text,
            required_color_system,
            "landing",
        )
        findings.extend(palette_audit.get("blocking_findings") or [])

    unique_findings = _unique_findings(findings)
    return {
        "accepted": not unique_findings,
        "findings": unique_findings,
        "metrics": {
            "semantic_section_count": len(sections),
            "word_count": word_count,
            "eligible_asset_count": len(eligible_by_id),
            "required_source_visual_count": required_source_visuals,
            "used_source_visual_count": len(valid_source_fingerprints),
            "used_source_visual_ids": sorted(visibly_placed_source_ids),
            "local_reference_count": len(normalized_local_references),
            "inline_svg_icon_count": len(soup.find_all("svg")),
            "interactive_control_count": len(interactive_controls),
            "source_grounded_interaction_count": source_grounded_interaction_count,
            "motion_declaration_count": motion_declaration_count,
            "has_prefers_reduced_motion": has_prefers_reduced_motion,
            "has_effective_reduced_motion": has_effective_reduced_motion,
            "icon_only_control_count": len(icon_only_controls),
            "inaccessible_icon_only_control_count": len(inaccessible_icon_only_controls),
            "javascript_reveal_dependency_count": javascript_reveal_dependency_count,
            "palette_audit": palette_audit.get("debug_metrics") or {},
        },
    }


def _merge_landing_browser_audit(
    diagnostics: dict[str, Any],
    browser_audit: dict[str, Any],
) -> dict[str, Any]:
    merged = json.loads(json.dumps(diagnostics, default=str))
    browser_findings = [
        {
            "issue_id": str(finding.get("id") or "landing_browser_audit_failed"),
            "message": str(finding.get("message") or "landing browser audit failed"),
            "evidence": finding.get("evidence") or {},
        }
        for finding in (browser_audit.get("findings") or [])
        if isinstance(finding, dict)
    ]
    merged["findings"] = _unique_findings(
        [*(merged.get("findings") or []), *browser_findings]
    )
    merged["accepted"] = bool(
        merged.get("accepted")
        and browser_audit.get("accepted")
        and not browser_findings
    )
    metrics = merged.get("metrics") if isinstance(merged.get("metrics"), dict) else {}
    metrics["browser_audit"] = browser_audit.get("metrics") or {}
    metrics["browser_audit_backend"] = str(browser_audit.get("backend") or "")
    merged["metrics"] = metrics
    return merged


def _browser_audit_unavailable(diagnostics: dict[str, Any]) -> bool:
    return any(
        str(finding.get("issue_id") or "") == "artifact_browser_audit_unavailable"
        for finding in (diagnostics.get("findings") or [])
        if isinstance(finding, dict)
    )


def _motion_declaration_count(css_text: str, inline_script: str) -> int:
    css_without_comments = re.sub(r"/\*.*?\*/", "", css_text, flags=re.DOTALL)
    count = 0
    for match in re.finditer(
        r"(?:^|[;{])\s*(animation(?:-[\w-]+)?|transition(?:-[\w-]+)?|scroll-behavior)\s*:\s*([^;}]+)",
        css_without_comments,
        re.IGNORECASE,
    ):
        property_name = match.group(1).lower()
        value = match.group(2).strip().lower()
        if _motion_value_is_disabled(property_name, value):
            continue
        count += 1
    count += len(
        re.findall(
            r"\brequestAnimationFrame\s*\(|\.animate\s*\(",
            inline_script,
        )
    )
    return count


def _current_landing_color_system(ctx: ToolContext) -> dict[str, Any]:
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


def _read_local_text_dependency(attempt_dir: Path, value: str) -> str | None:
    path = _local_dependency_path(attempt_dir, value)
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _has_effective_reduced_motion_fallback(css_text: str) -> bool:
    bodies = _reduced_motion_media_bodies(css_text)
    if not bodies:
        return False
    fallback = "\n".join(bodies)
    active_properties: set[str] = set()
    for match in re.finditer(
        r"(?:^|[;{])\s*(animation(?:-[\w-]+)?|transition(?:-[\w-]+)?|scroll-behavior)\s*:\s*([^;}]+)",
        css_text,
        re.IGNORECASE,
    ):
        property_name = match.group(1).lower()
        if not _motion_value_is_disabled(property_name, match.group(2).strip().lower()):
            if property_name.startswith("animation"):
                active_properties.add("animation")
            elif property_name.startswith("transition"):
                active_properties.add("transition")
            elif property_name == "scroll-behavior":
                active_properties.add("scroll")
    disabled = {
        "animation": bool(re.search(
            r"animation(?:-duration)?\s*:\s*(?:none|0(?:\.0+)?(?:ms|s))\b",
            fallback,
            re.IGNORECASE,
        )),
        "transition": bool(re.search(
            r"transition(?:-duration)?\s*:\s*(?:none|0(?:\.0+)?(?:ms|s))\b",
            fallback,
            re.IGNORECASE,
        )),
        "scroll": bool(re.search(
            r"scroll-behavior\s*:\s*auto\b",
            fallback,
            re.IGNORECASE,
        )),
    }
    return bool(active_properties) and all(disabled[name] for name in active_properties)


def _reduced_motion_media_bodies(css_text: str) -> list[str]:
    bodies: list[str] = []
    for match in re.finditer(r"@media\b[^{}]*prefers-reduced-motion[^{}]*\{", css_text, re.IGNORECASE):
        start = match.end()
        depth = 1
        index = start
        while index < len(css_text) and depth:
            if css_text[index] == "{":
                depth += 1
            elif css_text[index] == "}":
                depth -= 1
            index += 1
        if depth == 0:
            bodies.append(css_text[start:index - 1])
    return bodies


def _motion_value_is_disabled(property_name: str, value: str) -> bool:
    compact = re.sub(r"\s+!important\s*$", "", value).strip()
    if compact in {"none", "initial", "inherit", "unset", "revert", "auto"}:
        return True
    if property_name.endswith("duration"):
        durations = re.findall(r"(?:^|[\s,])(\d*\.?\d+)(ms|s)(?=$|[\s,])", compact)
        return bool(durations) and all(float(amount) == 0 for amount, _unit in durations)
    return property_name == "scroll-behavior" and compact != "smooth"


def _interactive_controls(soup: BeautifulSoup) -> list[Tag]:
    selectors = (
        "button, a[href], summary, input:not([type=hidden]), select, textarea, "
        "[role=button], [role=link], [role=slider], [role=tab], [role=switch]"
    )
    controls: list[Tag] = []
    seen: set[int] = set()
    for control in soup.select(selectors):
        if not isinstance(control, Tag) or id(control) in seen:
            continue
        seen.add(id(control))
        controls.append(control)
    return controls


def _is_icon_only_control(control: Tag) -> bool:
    has_icon = control.find(["svg", "img"]) is not None or bool(
        control.select_one('[class*="icon" i]')
    )
    if not has_icon:
        return False
    return not _control_text_without_icons(control)


def _control_text_without_icons(control: Tag) -> str:
    clone = BeautifulSoup(str(control), "html.parser")
    for icon in clone.find_all(["svg", "img"]):
        icon.extract()
    return clone.get_text(" ", strip=True)


def _has_accessible_control_name(control: Tag, soup: BeautifulSoup) -> bool:
    if str(control.get("aria-label") or "").strip():
        return True
    labelled_by = str(control.get("aria-labelledby") or "").split()
    for element_id in labelled_by:
        label = soup.find(id=element_id)
        if isinstance(label, Tag) and label.get_text(" ", strip=True):
            return True
    if str(control.get("title") or "").strip():
        return True
    if _control_text_without_icons(control):
        return True
    image = control.find("img")
    if isinstance(image, Tag) and str(image.get("alt") or "").strip():
        return True
    for icon in control.find_all("svg"):
        if str(icon.get("aria-hidden") or "").strip().lower() == "true":
            continue
        if str(icon.get("aria-label") or "").strip():
            return True
        title = icon.find("title")
        if isinstance(title, Tag) and title.get_text(" ", strip=True):
            return True
    return False


def _source_grounded_interaction_count(
    controls: list[Tag],
    soup: BeautifulSoup,
    eligible_source_ids: set[str],
) -> int:
    grounded = 0
    for control in controls:
        source_ids = {
            str(tag.get("data-source-id") or "").strip()
            for tag in [control, *control.find_all(attrs={"data-source-id": True})]
        }
        for parent in control.parents:
            if not isinstance(parent, Tag):
                continue
            source_id = str(parent.get("data-source-id") or "").strip()
            if source_id:
                source_ids.add(source_id)
                break
        for target_id in str(control.get("aria-controls") or "").split():
            target = soup.find(id=target_id)
            if isinstance(target, Tag):
                source_ids.add(str(target.get("data-source-id") or "").strip())
        if any(source_id in eligible_source_ids for source_id in source_ids if source_id):
            grounded += 1
    return grounded


def _javascript_reveal_dependency_count(css_text: str, inline_script: str) -> int:
    if not re.search(r"\bIntersectionObserver\b|\.classList\.(?:add|remove|toggle)\s*\(", inline_script):
        return 0
    hidden_selectors: set[str] = set()
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css_text, re.DOTALL):
        selectors = match.group(1)
        declarations = match.group(2)
        if not re.search(
            r"(?:opacity\s*:\s*0(?:\D|$)|visibility\s*:\s*hidden\b|display\s*:\s*none\b)",
            declarations,
            re.IGNORECASE,
        ):
            continue
        for selector in selectors.split(","):
            normalized = selector.strip().lower()
            if re.search(r"(?:reveal|animate|in-view|inview|fade-in)", normalized):
                hidden_selectors.add(normalized)
    return len(hidden_selectors)


def _copy_landing_dependency_closure(
    attempt_dir: Path,
    final_dir: Path,
    source_html: Path,
) -> None:
    root = Path(os.path.abspath(os.fspath(attempt_dir)))
    for source in _landing_dependency_closure_files(attempt_dir, source_html):
        relative = source.relative_to(root)
        if relative.as_posix() == "index.html":
            continue
        target = final_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _landing_dependency_closure_files(
    attempt_dir: Path,
    source_html: Path,
) -> list[Path]:
    root = Path(os.path.abspath(os.fspath(attempt_dir)))
    queue: list[Path] = [
        _anchored_landing_dependency_path(source_html, root)
    ]
    visited: set[Path] = set()
    for dirname in ("layers", "assets"):
        source = root / dirname
        if source.is_dir():
            for path in source.rglob("*"):
                anchored = _anchored_landing_dependency_path(path, root)
                if anchored.is_file():
                    visited.add(anchored)
    while queue:
        source = _anchored_landing_dependency_path(queue.pop(0), root)
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
        for raw in _document_local_references(content, source.suffix.lower()):
            parsed = urlsplit(unquote(raw.strip()))
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            try:
                candidate = _anchored_landing_dependency_path(
                    source.parent / parsed.path,
                    root,
                )
            except ValueError:
                continue
            if candidate.is_file() and candidate not in visited:
                queue.append(candidate)
    return sorted(visited)


def _anchored_landing_dependency_path(path: Path, root: Path) -> Path:
    stable_root = Path(os.path.abspath(os.fspath(root)))
    candidate = Path(os.path.abspath(os.fspath(path)))
    relative = candidate.relative_to(stable_root)
    if ".." in relative.parts:
        raise ValueError("landing dependency escapes its attempt directory")
    current = stable_root
    for component in relative.parts:
        current = current / component
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("landing dependency cannot traverse a reparse point")
    return candidate


def _atomic_replace_directory(
    staging_dir: Path,
    final_dir: Path,
    *,
    post_publish: Any,
) -> None:
    publish_artifact_directory(
        staging_dir,
        final_dir,
        artifact_name="landing",
        post_publish=post_publish,
    )


def _recover_interrupted_promotion(final_dir: Path) -> None:
    recover_artifact_promotion(final_dir, artifact_name="landing")


def _document_local_references(content: str, suffix: str) -> list[str]:
    references: list[str] = []
    if suffix in {".html", ".htm"}:
        soup = BeautifulSoup(content, "html.parser")
        for tag_name, attribute in (
            ("img", "src"),
            ("source", "src"),
            ("video", "src"),
            ("video", "poster"),
            ("audio", "src"),
            ("script", "src"),
            ("link", "href"),
        ):
            for tag in soup.find_all(tag_name):
                value = str(tag.get(attribute) or "").strip()
                if value:
                    references.append(value)
        for tag in soup.find_all(["img", "source"]):
            for candidate in str(tag.get("srcset") or "").split(","):
                value = candidate.strip().split(" ", 1)[0]
                if value:
                    references.append(value)
        content = "\n".join(
            [tag.get_text(" ") for tag in soup.find_all("style")]
            + [str(tag.get("style") or "") for tag in soup.find_all(style=True)]
        )
    references.extend(match.group(2).strip() for match in _CSS_URL_RE.finditer(content))
    references.extend(
        match.group(2).strip()
        for match in re.finditer(r"@import\s+(['\"])(.*?)\1", content, re.IGNORECASE)
    )
    return references


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
        parsed = urlsplit(unquote(str(link.get("href") or "").strip()))
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        path = (attempt_dir / parsed.path).resolve()
        try:
            path.relative_to(attempt_dir.resolve())
            css_blocks.append(path.read_text(encoding="utf-8"))
        except (ValueError, OSError, UnicodeError):
            continue
    for css in css_blocks:
        for selector_text, declarations in re.findall(
            r"([^{}]+)\{([^{}]*)\}",
            css,
        ):
            if not _style_is_statically_hidden(declarations):
                continue
            for selector in selector_text.split(","):
                selector = selector.strip()
                if not selector or selector.startswith("@"):
                    continue
                try:
                    hidden_roots.extend(
                        tag for tag in soup.select(selector) if isinstance(tag, Tag)
                    )
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
    declarations: dict[str, str] = {}
    for item in style.lower().split(";"):
        if ":" not in item:
            continue
        name, value = item.split(":", 1)
        declarations[name.strip()] = value.strip().replace("!important", "").strip()
    if declarations.get("display") == "none":
        return True
    if declarations.get("visibility") in {"hidden", "collapse"}:
        return True
    opacity = declarations.get("opacity")
    if opacity is not None:
        try:
            if float(opacity) <= 0:
                return True
        except ValueError:
            pass
    zero = re.compile(r"0(?:\.0+)?(?:px|pt|em|rem|%)?$")
    return any(
        zero.fullmatch(declarations.get(name, "")) is not None
        for name in ("width", "height", "max-width", "max-height")
    )


def _local_reference_issue(
    attempt_dir: Path,
    value: str,
    *,
    base_dir: Path | None = None,
) -> str | None:
    raw = unquote(str(value or "").strip())
    if not raw or raw.startswith("#"):
        return None
    parsed = urlsplit(raw)
    if parsed.scheme.lower() in _REMOTE_SCHEMES or parsed.netloc or raw.startswith("//"):
        return "remote"
    if not parsed.path:
        return None
    relative = Path(parsed.path)
    if relative.is_absolute():
        return "outside"
    resolved = ((base_dir or attempt_dir) / relative).resolve()
    try:
        resolved.relative_to(attempt_dir.resolve())
    except ValueError:
        return "outside"
    return None if resolved.exists() and resolved.is_file() else "missing"


def _local_dependency_path(
    attempt_dir: Path,
    value: str,
    *,
    base_dir: Path | None = None,
) -> Path | None:
    if _local_reference_issue(attempt_dir, value, base_dir=base_dir):
        return None
    parsed = urlsplit(unquote(str(value or "").strip()))
    if not parsed.path:
        return None
    return ((base_dir or attempt_dir) / parsed.path).resolve()


def _normalized_local_reference(value: str) -> str:
    parsed = urlsplit(unquote(str(value or "").strip()))
    if parsed.scheme or parsed.netloc or not parsed.path:
        return ""
    return Path(parsed.path).as_posix().lstrip("./")


def _effective_source_id(tag: Tag) -> str:
    source_id = str(tag.get("data-source-id") or "").strip()
    if source_id:
        return source_id
    wrapper = tag.find_parent(["figure", "picture"])
    if isinstance(wrapper, Tag):
        return str(wrapper.get("data-source-id") or "").strip()
    return ""


def _finding(issue_id: str, message: str) -> dict[str, str]:
    return {"issue_id": issue_id, "message": message}


def _unique_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for finding in findings:
        key = (str(finding.get("issue_id") or ""), str(finding.get("message") or ""))
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _contains_json_object(path: Path) -> bool:
    try:
        return isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
    except (OSError, json.JSONDecodeError):
        return False
