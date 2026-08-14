"""External coding-harness adapter for reference-poster style extraction."""

from __future__ import annotations

from contextlib import ExitStack
import json
import hashlib
import inspect
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from PIL import Image, ImageChops, ImageColor

from .external_author_process import (
    ExternalAuthorProcessRequest,
    context_cancellation_callback,
    context_cancellation_checkpoint,
    context_cancellation_token,
    run_external_author_process,
)
from ..config import harness_subprocess_env
from ..skills.registry import SkillRegistry
from ..util.io import atomic_write_json, sha256_file
from ..util.logging import log
from ..util.reference_poster import normalize_reference_poster
from ..util.reference_style_audit import (
    audit_reference_style_artifacts,
    semantic_reference_style_issues,
)

if TYPE_CHECKING:
    from ..tools._contract import ToolContext


class ReferenceStyleAgentError(RuntimeError):
    """Raised when an explicit reference poster cannot produce a style contract."""


_REFERENCE_STYLE_CONTRACT_VERSION = 4
_REFERENCE_STYLE_SANITIZER_VERSION = 4
_REFERENCE_STYLE_MAX_ATTEMPTS = 4
_REFERENCE_STYLE_SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "poster"
    / "reference_style_extraction"
    / "SKILL.md"
)
_REFERENCE_STYLE_SKILL_ID = "poster.reference_style_extraction"
_BODY_REGION_ROLES = {
    "column",
    "footer_band",
    "side_callout",
    "hero_region",
    "stacked_region",
    "full_width_band",
}
_BLUEPRINT_PLACEHOLDERS = {
    "{{PAPER_TITLE}}",
    "{{AUTHORS}}",
    "{{INSTITUTIONS}}",
    "{{TARGET_PAPER_SUMMARY}}",
    "{{SECTION_TITLE}}",
    "{{TARGET_PAPER_CONTENT}}",
    "{{TARGET_PAPER_FIGURE}}",
    "{{TARGET_PAPER_TABLE}}",
}
_REFERENCE_FONT_STACKS = {
    "sans_serif": 'Arial, "Helvetica Neue", Helvetica, sans-serif',
    "serif": '"Times New Roman", Times, Georgia, serif',
}


def prepare_reference_style_contract(
    ctx: ToolContext,
    source_path: Path,
    *,
    command: str,
    harness: str,
    model_hint: str = "",
    timeout_s: int = 600,
    page_index: int = 0,
    semantic_expectations: dict[str, Any] | None = None,
    enforce_extraction_only_artifacts: bool = False,
) -> dict[str, Any]:
    """Normalize a reference and ask the configured coding harness to analyze it."""

    context_cancellation_checkpoint(ctx, "reference_style.prepare.start")
    source = Path(source_path).expanduser().resolve()
    reference_dir = ctx.run_dir / "reference_poster"
    contract_path = ctx.run_dir / "reference_style_contract.json"
    context_cancellation_checkpoint(ctx, "reference_style.prepare.before_source_hash")
    source_sha = sha256_file(source)
    context_cancellation_checkpoint(ctx, "reference_style.prepare.after_source_hash")
    skill_bundle = _reference_style_skill_bundle(ctx.settings)
    context_cancellation_checkpoint(ctx, "reference_style.prepare.after_skill_bundle")
    skill_sha = skill_bundle["skill_sha256"]
    skill_bundle_sha = skill_bundle["bundle_sha256"]
    skill_resource_hashes = skill_bundle["resource_hashes"]
    prompt_schema_sha = _reference_style_prompt_schema_sha256()
    runtime_fingerprint = _extraction_runtime_fingerprint(
        command,
        harness,
        model_hint,
        semantic_expectations,
        canvas_contract=(ctx.state.get("canvas_plan") or {}).get("canvas"),
    )
    cached = _read_json(contract_path)
    cached_blueprint = ctx.run_dir / "reference_style_blueprint.html"
    cached_preview = ctx.run_dir / "reference_style_blueprint_preview.png"
    if (
        cached.get("source_sha256") == source_sha
        and int(cached.get("source_page_index") or 0) == page_index
        and str(cached.get("extraction_skill_sha256") or "") == skill_sha
        and str(cached.get("extraction_skill_bundle_sha256") or "") == skill_bundle_sha
        and dict(cached.get("extraction_skill_resource_sha256") or {}) == skill_resource_hashes
        and str(cached.get("extraction_prompt_schema_sha256") or "") == prompt_schema_sha
        and str(cached.get("extraction_runtime_fingerprint") or "") == runtime_fingerprint
        and int(cached.get("version") or 0) == _REFERENCE_STYLE_CONTRACT_VERSION
        and int(cached.get("sanitizer_version") or 0) == _REFERENCE_STYLE_SANITIZER_VERSION
        and _valid_cached_reference_style_contract(
            cached,
            blueprint_path=cached_blueprint,
            preview_path=cached_preview,
        )
    ):
        context_cancellation_checkpoint(ctx, "reference_style.cache.before_audit")
        cache_audit = audit_reference_style_artifacts(
            ctx.run_dir,
            expected_source_sha256=source_sha,
            expected_page_index=page_index,
            expected_skill_sha256=skill_sha,
            expected_skill_bundle_sha256=skill_bundle_sha,
            expected_skill_resource_sha256=skill_resource_hashes,
            enforce_extraction_only_artifacts=enforce_extraction_only_artifacts,
        )
        context_cancellation_checkpoint(ctx, "reference_style.cache.after_audit")
        cache_audit["expected_skill_bundle_sha256"] = skill_bundle_sha
        context_cancellation_checkpoint(ctx, "reference_style.cache.before_audit_write")
        atomic_write_json(ctx.run_dir / "reference_style_audit.json", cache_audit)
        context_cancellation_checkpoint(ctx, "reference_style.cache.after_audit_write")
        if cache_audit.get("status") == "pass":
            metadata = _read_json(reference_dir / "reference_source_metadata.json")
            context_cancellation_checkpoint(ctx, "reference_style.cache.before_state_update")
            ctx.state["reference_poster"] = metadata
            context_cancellation_checkpoint(ctx, "reference_style.cache.before_contract_state_update")
            ctx.state["reference_style_contract"] = cached
            context_cancellation_checkpoint(ctx, "reference_style.cache.before_return")
            log("reference_style.cache_hit", run_id=ctx.run_id, style_reference_id=cached.get("style_reference_id"))
            context_cancellation_checkpoint(ctx, "reference_style.cache.after_log")
            return cached

    staged_metadata = (
        ctx.state.get("reference_poster")
        if isinstance(ctx.state.get("reference_poster"), dict)
        else {}
    )
    if (
        str(staged_metadata.get("source_sha256") or "") == source_sha
        and int(staged_metadata.get("page_index") or 0) == page_index
        and (reference_dir / "reference.png").is_file()
    ):
        metadata = staged_metadata
    else:
        context_cancellation_checkpoint(ctx, "reference_style.normalize.before")
        metadata = normalize_reference_poster(source, reference_dir, page_index=page_index)
        context_cancellation_checkpoint(ctx, "reference_style.normalize.after")
    if not command.strip():
        raise ReferenceStyleAgentError(
            "reference poster style analysis requires an external coding-harness command"
        )

    for stale_name in (
        "reference_style_contract.json",
        "reference_style_audit.json",
        "reference_style_blueprint.html",
        "reference_style_blueprint_preview.png",
        "reference_style_raw_blueprint_preview.png",
    ):
        context_cancellation_checkpoint(ctx, "reference_style.prepare.before_stale_unlink")
        (ctx.run_dir / stale_name).unlink(missing_ok=True)
        context_cancellation_checkpoint(ctx, "reference_style.prepare.after_stale_unlink")
    last_error: ReferenceStyleAgentError | None = None
    failure_history: list[str] = []
    contract: dict[str, Any] = {}
    for attempt_index in range(1, _REFERENCE_STYLE_MAX_ATTEMPTS + 1):
        context_cancellation_checkpoint(ctx, "reference_style.attempt.start")
        runtime_skill = _stage_reference_style_skill_bundle(
            skill_bundle,
            reference_dir,
            # This direct extraction pack is plan-scoped; retries repair the
            # same version-4 analysis/blueprint contract rather than switching
            # to unrelated poster-repair guidance.
            stage="plan",
            ctx=ctx,
        )
        context_cancellation_checkpoint(ctx, "reference_style.attempt.after_skill_stage")
        prompt = (
            _reference_style_prompt(
                reference_dir,
                metadata,
                model_hint=model_hint,
                runtime_skill=runtime_skill,
            )
            if attempt_index == 1
            else _reference_style_repair_prompt(
                reference_dir,
                attempt_index=attempt_index,
                failure=str(last_error or "unknown extraction failure"),
                failures=failure_history,
                runtime_skill=runtime_skill,
            )
        )
        prompt_name = f"reference_style_agent_prompt_attempt_{attempt_index:02d}.md"
        context_cancellation_checkpoint(ctx, "reference_style.attempt.before_prompt_write")
        (reference_dir / prompt_name).write_text(prompt, encoding="utf-8")
        context_cancellation_checkpoint(ctx, "reference_style.attempt.after_versioned_prompt_write")
        (reference_dir / "reference_style_agent_prompt.md").write_text(prompt, encoding="utf-8")
        context_cancellation_checkpoint(ctx, "reference_style.attempt.after_prompt_write")
        invocation = _invoke_style_agent(
            command,
            prompt=prompt,
            work_dir=reference_dir,
            harness=harness,
            api_key=getattr(ctx.settings, "harness_api_key", None),
            timeout_s=max(1, int(timeout_s)),
            ctx=ctx,
        )
        context_cancellation_checkpoint(ctx, "reference_style.attempt.after_invocation")
        context_cancellation_checkpoint(ctx, "reference_style.attempt.before_versioned_process_write")
        atomic_write_json(
            reference_dir / f"reference_style_agent_process_attempt_{attempt_index:02d}.json",
            invocation,
        )
        context_cancellation_checkpoint(ctx, "reference_style.attempt.after_versioned_process_write")
        atomic_write_json(reference_dir / "reference_style_agent_process.json", invocation)
        context_cancellation_checkpoint(ctx, "reference_style.attempt.after_process_write")
        try:
            context_cancellation_checkpoint(ctx, "reference_style.attempt.before_finalize")
            contract = _finalize_reference_style_attempt(
                ctx,
                metadata=metadata,
                source_sha=source_sha,
                page_index=page_index,
                skill_path=runtime_skill["legacy_skill_path"],
                skill_sha=skill_sha,
                skill_bundle_sha=skill_bundle_sha,
                skill_resource_hashes=skill_resource_hashes,
                prompt_schema_sha=prompt_schema_sha,
                runtime_fingerprint=runtime_fingerprint,
                attempt_index=attempt_index,
                invocation=invocation,
                semantic_expectations=semantic_expectations,
                enforce_extraction_only_artifacts=enforce_extraction_only_artifacts,
            )
            context_cancellation_checkpoint(ctx, "reference_style.attempt.after_finalize")
            break
        except ReferenceStyleAgentError as exc:
            last_error = exc
            failure_history.append(str(exc))
            _archive_reference_style_attempt(
                ctx.run_dir,
                reference_dir,
                attempt_index=attempt_index,
                failure=str(exc),
                ctx=ctx,
            )
            context_cancellation_checkpoint(ctx, "reference_style.attempt.after_archive")
            if attempt_index == _REFERENCE_STYLE_MAX_ATTEMPTS:
                raise
    else:  # pragma: no cover - loop always returns or raises
        raise last_error or ReferenceStyleAgentError("reference style extraction failed")
    context_cancellation_checkpoint(ctx, "reference_style.prepare.before_final_state")
    ctx.state["reference_poster"] = metadata
    context_cancellation_checkpoint(ctx, "reference_style.prepare.before_final_contract_state")
    ctx.state["reference_style_contract"] = contract
    context_cancellation_checkpoint(ctx, "reference_style.prepare.before_ready_log")
    log(
        "reference_style.ready",
        run_id=ctx.run_id,
        style_reference_id=contract["style_reference_id"],
        palette_id=contract["color_system"]["palette_id"],
        source_suffix=metadata.get("source_suffix"),
    )
    context_cancellation_checkpoint(ctx, "reference_style.prepare.before_return")
    return contract


def _finalize_reference_style_attempt(
    ctx: ToolContext,
    *,
    metadata: dict[str, Any],
    source_sha: str,
    page_index: int,
    skill_path: Path,
    skill_sha: str,
    skill_bundle_sha: str,
    skill_resource_hashes: dict[str, str],
    prompt_schema_sha: str,
    runtime_fingerprint: str,
    attempt_index: int,
    invocation: dict[str, Any],
    semantic_expectations: dict[str, Any] | None,
    enforce_extraction_only_artifacts: bool,
) -> dict[str, Any]:
    context_cancellation_checkpoint(ctx, "reference_style.finalize.start")
    reference_dir = ctx.run_dir / "reference_poster"
    analysis = _read_json(reference_dir / "reference_style_analysis.json")
    context_cancellation_checkpoint(ctx, "reference_style.finalize.after_analysis_read")
    if not analysis:
        raise ReferenceStyleAgentError(
            "reference style agent did not write a valid reference_style_analysis.json "
            f"(status={invocation.get('status')}, reason={invocation.get('reason')})"
        )
    if int(analysis.get("version") or 0) != 4 or not isinstance(
        analysis.get("body_region_structure"), dict
    ):
        raise ReferenceStyleAgentError(
            "reference style analysis must use version 4 body_region_structure; legacy column-only analysis is rejected"
        )
    contract = _compile_reference_style_contract(analysis, metadata)
    context_cancellation_checkpoint(ctx, "reference_style.finalize.after_contract_compile")
    contract["source_page_index"] = page_index
    contract["extraction_skill"] = skill_path.name
    contract["extraction_skill_sha256"] = skill_sha
    contract["extraction_skill_bundle_sha256"] = skill_bundle_sha
    contract["extraction_skill_resource_sha256"] = dict(skill_resource_hashes)
    contract["extraction_prompt_schema_sha256"] = prompt_schema_sha
    contract["extraction_runtime_fingerprint"] = runtime_fingerprint
    contract["extraction_attempt_count"] = attempt_index
    raw_blueprint = reference_dir / "reference_style_blueprint.html"
    if not raw_blueprint.exists():
        raise ReferenceStyleAgentError(
            "reference style agent did not write reference_style_blueprint.html"
        )
    review = _read_json(reference_dir / "reference_style_agent_review.json")
    context_cancellation_checkpoint(ctx, "reference_style.finalize.after_review_read")
    if not _valid_reference_style_review(review, raw_blueprint):
        raise ReferenceStyleAgentError(
            "reference style agent did not complete a blueprint-hash-bound rendered review"
        )
    _validate_raw_reference_style_blueprint(raw_blueprint, contract)
    context_cancellation_checkpoint(ctx, "reference_style.finalize.after_blueprint_validation")
    expected_region_ids = [
        str(region["region_id"])
        for region in contract["style_tokens"]["body_region_structure"]["regions"]
    ]
    raw_preview_path = ctx.run_dir / "reference_style_raw_blueprint_preview.png"
    expected_canvas = _reference_canvas_contract(metadata)
    context_cancellation_checkpoint(ctx, "reference_style.finalize.before_raw_browser_render")
    _render_and_measure_reference_blueprint(
        raw_blueprint,
        raw_preview_path,
        expected_region_ids=expected_region_ids,
        expected_canvas=expected_canvas,
        cancellation_check=lambda phase: context_cancellation_checkpoint(ctx, phase),
    )
    context_cancellation_checkpoint(ctx, "reference_style.finalize.after_raw_browser_render")
    blueprint_path = ctx.run_dir / "reference_style_blueprint.html"
    context_cancellation_checkpoint(ctx, "reference_style.finalize.before_blueprint_promotion")
    _sanitize_reference_style_blueprint(
        raw_blueprint,
        blueprint_path,
        contract,
        cancellation_check=lambda phase: context_cancellation_checkpoint(ctx, phase),
    )
    context_cancellation_checkpoint(ctx, "reference_style.finalize.after_blueprint_promotion")
    preview_path = ctx.run_dir / "reference_style_blueprint_preview.png"
    context_cancellation_checkpoint(ctx, "reference_style.finalize.before_sanitized_browser_render")
    measured_regions = _render_and_measure_reference_blueprint(
        blueprint_path,
        preview_path,
        expected_region_ids=expected_region_ids,
        style_contract=contract,
        expected_canvas=expected_canvas,
        cancellation_check=lambda phase: context_cancellation_checkpoint(ctx, phase),
    )
    context_cancellation_checkpoint(ctx, "reference_style.finalize.after_sanitized_browser_render")
    visual_diff_ratio = _image_diff_ratio(raw_preview_path, preview_path)
    context_cancellation_checkpoint(ctx, "reference_style.finalize.after_visual_diff")
    if visual_diff_ratio > 0.0:
        raise ReferenceStyleAgentError(
            "sanitization changed the reviewed blueprint appearance "
            f"(pixel_diff_ratio={visual_diff_ratio:.6f}); author a clean placeholder-only blueprint"
        )
    contract["style_tokens"]["layout_rhythm"]["region_boxes"] = measured_regions
    contract["blueprint"] = {
        "path": blueprint_path.name,
        "sha256": sha256_file(blueprint_path),
        "preview_path": preview_path.name,
        "preview_sha256": sha256_file(preview_path),
        "raw_preview_path": raw_preview_path.name,
        "raw_preview_sha256": sha256_file(raw_preview_path),
        "sanitization_visual_diff_ratio": visual_diff_ratio,
        "content_policy": "placeholder-only sanitized style scaffold",
    }
    contract["blueprint_review"] = {
        **review,
        "sanitized_blueprint_sha256": sha256_file(blueprint_path),
        "sanitized_blueprint_rendered": True,
        "sanitized_visual_equivalent_to_reviewed_raw": True,
    }
    contract_path = ctx.run_dir / "reference_style_contract.json"
    context_cancellation_checkpoint(ctx, "reference_style.finalize.before_contract_write")
    atomic_write_json(contract_path, contract)
    context_cancellation_checkpoint(ctx, "reference_style.finalize.after_contract_write")
    context_cancellation_checkpoint(ctx, "reference_style.finalize.before_audit")
    audit = audit_reference_style_artifacts(
        ctx.run_dir,
        expected_source_sha256=source_sha,
        expected_page_index=page_index,
        expected_skill_sha256=skill_sha,
        expected_skill_bundle_sha256=skill_bundle_sha,
        expected_skill_resource_sha256=skill_resource_hashes,
        enforce_extraction_only_artifacts=enforce_extraction_only_artifacts,
    )
    context_cancellation_checkpoint(ctx, "reference_style.finalize.after_audit")
    context_cancellation_checkpoint(ctx, "reference_style.finalize.before_audit_write")
    atomic_write_json(ctx.run_dir / "reference_style_audit.json", audit)
    context_cancellation_checkpoint(ctx, "reference_style.finalize.after_audit_write")
    if audit.get("status") != "pass":
        failed = ", ".join(
            str(item.get("check") or "unknown")
            for item in audit.get("issues") or []
            if isinstance(item, dict)
        )
        raise ReferenceStyleAgentError(
            f"reference style extraction audit failed: {failed or 'unknown'}"
        )
    context_cancellation_checkpoint(ctx, "reference_style.finalize.before_semantic_validation")
    semantic_issues = semantic_reference_style_issues(contract, semantic_expectations)
    context_cancellation_checkpoint(ctx, "reference_style.finalize.after_semantic_validation")
    if semantic_issues:
        raise ReferenceStyleAgentError(
            "reference style semantic expectations failed: " + "; ".join(semantic_issues)
        )
    context_cancellation_checkpoint(ctx, "reference_style.finalize.before_return")
    return contract


def _archive_reference_style_attempt(
    run_dir: Path,
    reference_dir: Path,
    *,
    attempt_index: int,
    failure: str,
    ctx: ToolContext | None = None,
) -> None:
    archive = reference_dir / "reference_style_attempts" / f"attempt_{attempt_index:02d}"
    if archive.exists():
        _raise_if_reference_style_cancelled(ctx, "reference_style.archive.before_stale_remove")
        shutil.rmtree(archive)
        _raise_if_reference_style_cancelled(ctx, "reference_style.archive.after_stale_remove")
    _raise_if_reference_style_cancelled(ctx, "reference_style.archive.before_directory_create")
    archive.mkdir(parents=True, exist_ok=True)
    _raise_if_reference_style_cancelled(ctx, "reference_style.archive.after_directory_create")
    reference_names = (
        "reference_style_analysis.json",
        "reference_style_blueprint.html",
        "reference_style_agent_review.json",
        "reference_style_agent_done.json",
        "reference_style_agent_process.json",
        f"reference_style_agent_process_attempt_{attempt_index:02d}.json",
        f"reference_style_agent_prompt_attempt_{attempt_index:02d}.md",
        "reference_style_agent.stdout.log",
        "reference_style_agent.stderr.log",
    )
    for name in reference_names:
        source = reference_dir / name
        if source.is_file():
            _raise_if_reference_style_cancelled(ctx, "reference_style.archive.before_attempt_copy")
            shutil.copy2(source, archive / name)
            _raise_if_reference_style_cancelled(ctx, "reference_style.archive.after_attempt_copy")
    root_artifacts = {
        "reference_style_contract.json": "reference_style_contract.json",
        "reference_style_audit.json": "reference_style_audit.json",
        "reference_style_blueprint.html": "sanitized_reference_style_blueprint.html",
        "reference_style_blueprint_preview.png": "sanitized_reference_style_blueprint_preview.png",
        "reference_style_raw_blueprint_preview.png": "locally_rendered_raw_blueprint_preview.png",
    }
    for name, archived_name in root_artifacts.items():
        source = run_dir / name
        if source.is_file():
            _raise_if_reference_style_cancelled(ctx, "reference_style.archive.before_root_copy")
            shutil.copy2(source, archive / archived_name)
            _raise_if_reference_style_cancelled(ctx, "reference_style.archive.before_root_unlink")
            source.unlink(missing_ok=True)
            _raise_if_reference_style_cancelled(ctx, "reference_style.archive.after_root_unlink")
    _raise_if_reference_style_cancelled(ctx, "reference_style.archive.before_failure_write")
    atomic_write_json(archive / "failure.json", {"attempt": attempt_index, "error": failure})
    _raise_if_reference_style_cancelled(ctx, "reference_style.archive.after_failure_write")


def _reference_style_repair_prompt(
    reference_dir: Path,
    *,
    attempt_index: int,
    failure: str,
    failures: list[str] | None = None,
    runtime_skill: dict[str, Any] | None = None,
) -> str:
    history = list(dict.fromkeys([*(failures or []), failure]))
    history_block = "\n".join(f"- {item}" for item in history)
    guidance = _reference_style_repair_guidance(failure)
    metadata = _read_json(reference_dir / "reference_source_metadata.json")
    canvas = _reference_canvas_contract(metadata)
    canvas_label = f"{canvas['w_px']}x{canvas['h_px']}"
    prior_archive = reference_dir / "reference_style_attempts" / f"attempt_{attempt_index - 1:02d}"
    prior_names = ("reference_style_analysis.json", "reference_style_blueprint.html")
    existing_prior = [name for name in prior_names if (prior_archive / name).is_file()]
    missing_prior = [name for name in prior_names if name not in existing_prior]
    if not missing_prior:
        baseline_instruction = (
            "Begin from the archived prior files: copy `reference_style_analysis.json` and "
            f"`reference_style_blueprint.html` from `reference_style_attempts/attempt_{attempt_index - 1:02d}/` "
            "back to the active directory, then edit those copies in place. Do not regenerate either file from scratch."
        )
    else:
        available_text = ", ".join(f"`{name}`" for name in existing_prior) or "none"
        missing_text = ", ".join(f"`{name}`" for name in missing_prior)
        baseline_instruction = (
            "The previous attempt did not produce a complete patch baseline. "
            f"Available prior files: {available_text}. Missing prior files: {missing_text}. "
            "Copy any available prior file into the active directory and preserve it. Reconstruct only the missing "
            "files from `reference.png`, the extraction skill, and the version-4 schema."
        )
    runtime_instruction = _reference_runtime_skill_instruction(runtime_skill)
    return f"""You are repairing a failed Reference Style Agent extraction, not authoring a poster.

Work only inside {reference_dir}. {runtime_instruction} Read `reference.png` and the archived prior output under `reference_style_attempts/attempt_{attempt_index - 1:02d}/`. The mandatory output-contract resource is the sole source of the exact version-4 JSON enums and required filenames; do not invent enum values or schema fields.

The current deterministic local failure is:
{failure}

All deterministic failures seen so far must remain fixed:
{history_block}

Repair scope for this failure:
{guidance}

{baseline_instruction}

Re-inspect the reference and patch the active files within the stated scope. Do not regress earlier fixes. Do not weaken checks, hide content, merge disjoint islands into an overlapping wrapper, or move ornamental chrome into content regions. Keep every top-level section inside exactly one tight, non-overlapping body region. Preserve only reference style and placeholder content.

Render the repaired blueprint at {canvas_label} and compare it with `reference.png`. Then write a hash-bound `reference_style_agent_review.json` and `reference_style_agent_done.json` using the same schema and filenames required by the extraction skill.
"""


def _reference_style_repair_guidance(failure: str) -> str:
    lowered = failure.lower()
    if "chrome_presence" in lowered:
        return (
            "Synchronize `chrome_treatment.present` with the blueprint's root-level `chrome-layer`. If the "
            "reference has no decoration extending through body gutters or multiple body regions, set chrome "
            "absent and remove the root-level `chrome-layer`; keep any header-confined wedges or stripes inside "
            "`identity-header`. If body-spanning chrome is genuinely observed, declare it present and keep exactly "
            "one root layer. Do not change the body-region map, layout mode, palette, or typography."
        )
    if "outside its analysis palette" in lowered:
        return (
            "Synchronize palette declarations and CSS colors only. Preserve the existing body-region "
            "decomposition, header geometry, section-heading mode, typography, and chrome decision. "
            "If the reference visibly repeats more accent colors than the core palette roles can express, "
            "declare them under `palette.additional_roles` with descriptive role names and six-digit hex values."
        )
    if lowered.startswith("rendered ") or "does not match analysis" in lowered:
        return (
            "Synchronize the named analysis token and its rendered CSS only, choosing the reference-observed "
            "treatment. Preserve the existing body-region decomposition, palette, unrelated style tokens, and "
            "all earlier fixes."
        )
    if "semantic expectations failed" in lowered:
        return (
            "Correct only the listed macro semantic fields and the matching region geometry or style token. "
            "Preserve unrelated palette, typography, placeholder structure, and already-correct semantics."
        )
    if "sanitization changed" in lowered or "visible text" in lowered:
        return (
            "Remove or replace the unsupported markup/text that caused sanitization drift while preserving "
            "the existing region map, dimensions, palette, typography, and reference-observed appearance."
        )
    return (
        "Repair the reported structural or audit defect at its source. Preserve every unrelated region, style "
        "token, and previously passing invariant."
    )


def _extraction_runtime_fingerprint(
    command: str,
    harness: str,
    model_hint: str,
    semantic_expectations: dict[str, Any] | None,
    canvas_contract: dict[str, Any] | None = None,
) -> str:
    payload = json.dumps(
        {
            "command": command.strip(),
            "harness": harness.strip(),
            "model_hint": model_hint.strip(),
            "semantic_expectations": semantic_expectations or {},
            "canvas_contract": canvas_contract or {},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _reference_style_prompt_schema_sha256() -> str:
    source = "\n".join(
        inspect.getsource(function)
        for function in (
            _reference_style_prompt,
            _reference_style_repair_prompt,
            _reference_style_repair_guidance,
            _validate_reference_style_analysis_schema,
            _validate_raw_reference_style_blueprint,
            _normalized_body_region_structure,
        )
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _reference_style_skill_bundle(settings: Any) -> dict[str, Any]:
    skills_dir = Path(getattr(settings, "skills_dir", "") or "")
    registry = SkillRegistry.load(skills_dir)
    pack = registry.get(_REFERENCE_STYLE_SKILL_ID)
    if pack is None or not pack.verify_integrity():
        raise ReferenceStyleAgentError(
            "reference style extraction skill is invalid or missing"
        )
    root = Path(pack.root)
    skill_path = root / "SKILL.md"
    try:
        manifest = pack.manifest.model_dump(mode="json")
        skill_text = skill_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReferenceStyleAgentError(
            f"reference style extraction skill is unavailable: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise ReferenceStyleAgentError("reference style extraction skill manifest is invalid")
    if not any(
        resource.id == "output_contract_v4" and "plan" in resource.stages
        for resource in pack.manifest.resources
    ):
        raise ReferenceStyleAgentError(
            "reference style extraction skill is missing mandatory output_contract_v4"
        )

    resources: list[dict[str, Any]] = []
    for resource in manifest.get("resources") or []:
        if not isinstance(resource, dict):
            continue
        relative = str(resource.get("path") or "").strip()
        source = (root / relative).resolve()
        try:
            source.relative_to(root.resolve())
        except ValueError:
            continue
        if relative and source.is_file():
            resources.append({"meta": resource, "path": relative, "source": source})
    canonical_manifest = dict(manifest)
    canonical_manifest["resources"] = sorted(
        [dict(item["meta"]) for item in resources],
        key=lambda item: str(item.get("id") or ""),
    )
    digest = hashlib.sha256()
    digest.update(json.dumps(canonical_manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    digest.update(skill_text.encode("utf-8"))
    for resource in sorted(resources, key=lambda item: str(item["meta"].get("id") or "")):
        digest.update(str(resource["meta"].get("id") or "").encode("utf-8"))
        digest.update(Path(resource["source"]).read_bytes())
    bundle_sha = str(getattr(pack, "content_hash", "") or digest.hexdigest())
    render = getattr(pack, "render", None)
    staged_skill_text = str(render("plan") or "").strip() if callable(render) else ""
    if not staged_skill_text:
        staged_skill_text = skill_text.strip()
    staged_skill_text += "\n"
    return {
        "pack": pack,
        "root": root,
        "manifest": manifest,
        "skill_path": skill_path,
        "skill_text": skill_text,
        "staged_skill_text": staged_skill_text,
        # Compatibility field: bind the exact skill file presented to the
        # external agent. The bundle hash below covers source + resources.
        "skill_sha256": hashlib.sha256(staged_skill_text.encode("utf-8")).hexdigest(),
        "bundle_sha256": bundle_sha,
        "resource_hashes": {
            str(item["path"]): sha256_file(Path(item["source"]))
            for item in resources
        },
        "resources": resources,
    }


def _stage_reference_style_skill_bundle(
    bundle: dict[str, Any],
    reference_dir: Path,
    *,
    stage: str,
    ctx: ToolContext | None = None,
) -> dict[str, Any]:
    staged_dir = reference_dir / "runtime_skills"
    if staged_dir.exists():
        _raise_if_reference_style_cancelled(ctx, "reference_style.skill.before_stale_remove")
        shutil.rmtree(staged_dir)
        _raise_if_reference_style_cancelled(ctx, "reference_style.skill.after_stale_remove")
    _raise_if_reference_style_cancelled(ctx, "reference_style.skill.before_directory_create")
    staged_dir.mkdir(parents=True, exist_ok=True)
    _raise_if_reference_style_cancelled(ctx, "reference_style.skill.after_directory_create")
    pack = bundle.get("pack")
    if stage == "plan" and bundle.get("staged_skill_text"):
        skill_text = str(bundle["staged_skill_text"])
    else:
        render = getattr(pack, "render", None)
        skill_text = str(render(stage) or "").strip() if callable(render) else ""
        if not skill_text:
            skill_text = str(bundle["skill_text"]).strip()
        skill_text += "\n"
    staged_skill_path = staged_dir / "SKILL.md"
    _raise_if_reference_style_cancelled(ctx, "reference_style.skill.before_skill_write")
    staged_skill_path.write_text(skill_text, encoding="utf-8")
    _raise_if_reference_style_cancelled(ctx, "reference_style.skill.after_skill_write")

    resources: list[dict[str, str]] = []
    for item in bundle.get("resources") or []:
        meta = item.get("meta") if isinstance(item, dict) else None
        if not isinstance(meta, dict):
            continue
        stages = meta.get("stages") or []
        if stages and stage not in stages:
            continue
        relative = str(item.get("path") or "")
        source = Path(item["source"])
        target = staged_dir / relative
        _raise_if_reference_style_cancelled(ctx, "reference_style.skill.before_resource_directory_create")
        target.parent.mkdir(parents=True, exist_ok=True)
        _raise_if_reference_style_cancelled(ctx, "reference_style.skill.before_resource_copy")
        shutil.copy2(source, target)
        _raise_if_reference_style_cancelled(ctx, "reference_style.skill.after_resource_copy")
        resources.append({
            "id": str(meta.get("id") or relative),
            "path": relative,
            "description": str(meta.get("description") or "Runtime resource."),
            "when_to_read": str(meta.get("when_to_read") or "Read only when needed."),
        })
    index = [
        "# Runtime Skills",
        "",
        "Read this index first. Resources are staged files, not prompt context.",
        "",
        f"## {_REFERENCE_STYLE_SKILL_ID}",
        "- `SKILL.md`: compact operating guidance for this attempt.",
    ]
    if resources:
        index.extend(["", "## Resources (read on demand)"])
        index.extend(
            f"- `{item['path']}`: {item['description']} {item['when_to_read']}".rstrip()
            for item in resources
        )
    _raise_if_reference_style_cancelled(ctx, "reference_style.skill.before_index_write")
    (staged_dir / "index.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    _raise_if_reference_style_cancelled(ctx, "reference_style.skill.after_index_write")
    legacy_skill_path = reference_dir / "reference_style_agent_skill.md"
    _raise_if_reference_style_cancelled(ctx, "reference_style.skill.before_legacy_copy")
    shutil.copy2(staged_skill_path, legacy_skill_path)
    _raise_if_reference_style_cancelled(ctx, "reference_style.skill.after_legacy_copy")
    return {"legacy_skill_path": legacy_skill_path, "resources": resources}


def _raise_if_reference_style_cancelled(
    ctx: ToolContext | None,
    phase: str,
) -> None:
    if ctx is not None:
        context_cancellation_checkpoint(ctx, phase)


def _run_reference_style_cancellation_check(
    cancellation_check: Callable[[str], None] | None,
    phase: str,
) -> None:
    if cancellation_check is not None:
        cancellation_check(phase)


def _reference_runtime_skill_instruction(runtime_skill: dict[str, Any] | None) -> str:
    resources = runtime_skill.get("resources") if isinstance(runtime_skill, dict) else []
    output_contract = next(
        (
            item for item in resources
            if str(item.get("id") or "") == "output_contract_v4"
            or str(item.get("path") or "").endswith("output_contract_v4.md")
        ),
        None,
    )
    if isinstance(output_contract, dict):
        return (
            "Read `runtime_skills/index.md` first, then `runtime_skills/SKILL.md`. "
            f"`runtime_skills/{output_contract['path']}` is mandatory before writing output; read the geometry, chrome, and failure resources only when their constraints apply."
        )
    raise ReferenceStyleAgentError(
        "staged reference style skill is missing mandatory output_contract_v4"
    )


def _reference_style_prompt(
    reference_dir: Path,
    metadata: dict[str, Any],
    *,
    model_hint: str,
    runtime_skill: dict[str, Any] | None = None,
) -> str:
    model_line = f"\nModel hint from AutoDesign: {model_hint}\n" if model_hint else ""
    canvas = _reference_canvas_contract(metadata)
    canvas_label = f"{canvas['w_px']}x{canvas['h_px']}"
    aspect_ratio = str(canvas.get("aspect_ratio") or "")
    runtime_instruction = _reference_runtime_skill_instruction(runtime_skill)
    return f"""You are reconstructing a reusable HTML/CSS template from a reference academic poster.

This is an execution task. Work only inside:
{reference_dir}
{model_line}
{runtime_instruction}
Then read `reference_source_metadata.json` and inspect `reference.png` with the image/file tools available to your coding harness. The image is the sole visual reference. Do not edit it and do not author the target poster.

Recover the reference poster's transferable layout and visual grammar, not merely its palette. Measure the identity header, optional lead band, two-to-six macro body regions, reading order, section-title treatment, typography hierarchy, emphasis language, figure/table framing, whitespace rhythm, and any root-level decorative chrome.

The mandatory output-contract resource defines the exact version-4 JSON enums, blueprint placeholders, filenames, and hash-bound review object. Follow it exactly. Use the geometry, chrome, and failure resources while measuring and checking the blueprint; do not invent schema fields or enum values.

Reference-owned choices include typography, title alignment/color, region proportions, section separators, lead bands, surfaces, and spacing. Only these constraints remain locked:
- fixed {canvas_label} board with {aspect_ratio} aspect ratio
- one identity header containing target-paper title, authors, and institutions only
- all scientific content and visuals come from the target paper
- no overlap, clipping, scripts, remote assets, copied reference content, or bitmap text

Never copy, transcribe, summarize, or reuse the reference poster's title, authors, institutions, body text, logos, QR codes, icons, figures, tables, citations, links, or claims. Collapse and reflow space reserved for removed assets.

Render the final `reference_style_blueprint.html` at {canvas_label}, inspect it beside `reference.png`, write the SHA-bound review required by the output contract, then write `reference_style_agent_done.json` and exit.
"""


def _reference_canvas_contract(metadata: dict[str, Any] | None) -> dict[str, Any]:
    source = metadata if isinstance(metadata, dict) else {}
    canvas = source.get("canvas_contract") if isinstance(source.get("canvas_contract"), dict) else {}
    if not canvas and isinstance(source.get("default_canvas"), dict):
        canvas = source["default_canvas"]
    width = max(1, int(canvas.get("w_px") or source.get("preview_width_px") or 3072))
    height = max(1, int(canvas.get("h_px") or source.get("preview_height_px") or 1536))
    return {
        "w_px": width,
        "h_px": height,
        "dpi": int(canvas.get("dpi") or 96),
        "aspect_ratio": str(canvas.get("aspect_ratio") or f"{width}:{height}"),
        "color_mode": str(canvas.get("color_mode") or "RGB"),
    }


def _invoke_style_agent(
    command: str,
    *,
    prompt: str,
    work_dir: Path,
    harness: str,
    api_key: str | None,
    timeout_s: int,
    ctx: ToolContext,
) -> dict[str, Any]:
    context_cancellation_checkpoint(ctx, "reference_style.invoke.start")
    try:
        cmd = shlex.split(command)
    except ValueError as exc:
        return {"status": "error", "reason": "command_parse_error", "error": str(exc)}
    if not cmd:
        return {"status": "error", "reason": "empty_command"}

    stdout_path = work_dir / "reference_style_agent.stdout.log"
    stderr_path = work_dir / "reference_style_agent.stderr.log"
    done_path = work_dir / "reference_style_agent_done.json"
    analysis_path = work_dir / "reference_style_analysis.json"
    blueprint_path = work_dir / "reference_style_blueprint.html"
    review_path = work_dir / "reference_style_agent_review.json"
    raw_stdout_path = work_dir / ".reference_style_agent.stdout.tmp"
    raw_stderr_path = work_dir / ".reference_style_agent.stderr.tmp"
    for stale_path in (
        done_path,
        analysis_path,
        blueprint_path,
        review_path,
        stdout_path,
        stderr_path,
        raw_stdout_path,
        raw_stderr_path,
    ):
        context_cancellation_checkpoint(ctx, "reference_style.invoke.before_stale_unlink")
        stale_path.unlink(missing_ok=True)
        context_cancellation_checkpoint(ctx, "reference_style.invoke.after_stale_unlink")
    env = harness_subprocess_env(os.environ, harness=harness, api_key=api_key)
    author_python = (
        env.get("AUTODESIGN_AUTHOR_PYTHON", "").strip()
        or env.get("DESIGN_ANYTHING_AUTHOR_PYTHON", "").strip()
        or sys.executable
    )
    env["AUTODESIGN_AUTHOR_PYTHON"] = author_python
    env.setdefault("DESIGN_ANYTHING_AUTHOR_PYTHON", author_python)
    stable_at: float | None = None

    def completion_requested() -> str | None:
        nonlocal stable_at
        has_required = (
            analysis_path.exists()
            and blueprint_path.exists()
            and review_path.exists()
        )
        if has_required and done_path.exists():
            return "done_marker"
        if has_required:
            stable_at = stable_at or time.monotonic()
            if time.monotonic() - stable_at >= 3.0:
                return "stable_analysis_without_done_marker"
        else:
            stable_at = None
        return None

    process_result = None
    try:
        context_cancellation_checkpoint(ctx, "reference_style.invoke.before_registered_spawn")
        process_result = run_external_author_process(
            ExternalAuthorProcessRequest(
                run_id=ctx.run_id,
                attempt=0,
                command=cmd,
                cwd=work_dir,
                prompt=prompt,
                timeout_s=max(0.01, float(timeout_s)),
                stdout_path=raw_stdout_path,
                stderr_path=raw_stderr_path,
                env=env,
                completion_requested=completion_requested,
                interruption_requested=context_cancellation_callback(ctx),
                poll_interval_s=0.05,
                run_dir=ctx.run_dir,
                cancellation_token=context_cancellation_token(ctx),
            )
        )
        context_cancellation_checkpoint(ctx, "reference_style.invoke.after_process")
    finally:
        raw_stdout_path.unlink(missing_ok=True)
        raw_stderr_path.unlink(missing_ok=True)

    context_cancellation_checkpoint(ctx, "reference_style.invoke.before_result")
    sensitive_values = _reference_style_sensitive_values(cmd, api_key, env)
    context_cancellation_checkpoint(ctx, "reference_style.invoke.before_redacted_log_write")
    stdout_path.write_text(
        _redact_reference_style_process_text(process_result.stdout, sensitive_values),
        encoding="utf-8",
    )
    context_cancellation_checkpoint(ctx, "reference_style.invoke.between_redacted_log_writes")
    stderr_path.write_text(
        _redact_reference_style_process_text(process_result.stderr, sensitive_values),
        encoding="utf-8",
    )
    context_cancellation_checkpoint(ctx, "reference_style.invoke.after_redacted_log_write")
    status = "ok" if analysis_path.exists() and blueprint_path.exists() else "error"
    safe_command = [
        _redact_reference_style_process_text(part, sensitive_values)
        for part in cmd
    ]
    reason = process_result.reason
    error = None
    if process_result.status == "spawn_error":
        reason = "process_start_error"
        error = _redact_reference_style_process_text(
            process_result.stderr.strip() or process_result.reason,
            sensitive_values,
        )
    return {
        "status": status,
        "reason": reason,
        "returncode": process_result.returncode,
        "timed_out": process_result.timed_out,
        "elapsed_s": round(process_result.elapsed_s, 3),
        "command": safe_command,
        "analysis_written": analysis_path.exists(),
        "blueprint_written": blueprint_path.exists(),
        "done_marker_written": done_path.exists(),
        **({"error": error} if error else {}),
    }


def _reference_style_sensitive_values(
    command: list[str],
    api_key: str | None,
    env: dict[str, str],
) -> tuple[str, ...]:
    values: set[str] = set()
    sensitive_name = re.compile(r"(?:api[_-]?key|token|secret|password|authorization)", re.I)
    if api_key and len(api_key.strip()) >= 4:
        values.add(api_key.strip())
    for name, raw_value in env.items():
        value = str(raw_value or "").strip()
        if sensitive_name.search(name) and len(value) >= 4:
            values.add(value)
    for index, token in enumerate(command):
        flag, separator, inline_value = token.partition("=")
        normalized = flag.casefold().lstrip("-").replace("_", "-")
        if not normalized.endswith(
            ("api-key", "apikey", "token", "secret", "password", "authorization")
        ):
            continue
        value = inline_value if separator else (command[index + 1] if index + 1 < len(command) else "")
        if len(value.strip()) >= 4:
            values.add(value.strip())
    return tuple(sorted(values, key=len, reverse=True))


def _redact_reference_style_process_text(
    value: Any,
    sensitive_values: tuple[str, ...],
) -> str:
    text = "" if value is None else str(value)
    for secret in sensitive_values:
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(\bbearer\s+)([^\s\"']+)", r"\1[REDACTED]", text)
    return re.sub(
        r"(?i)((?:--)?(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|authorization)(?:\s*[:=]\s*|\s+))([^\s,;\"']+)",
        r"\1[REDACTED]",
        text,
    )


def _validate_reference_style_analysis_schema(analysis: dict[str, Any]) -> None:
    checks = (
        ("header_treatment.mode", (analysis.get("header_treatment") or {}).get("mode"), {"open_white", "tinted_open", "top_rule_white", "filled_band", "subtle_outline", "split_identity"}),
        ("header_treatment.alignment", (analysis.get("header_treatment") or {}).get("alignment"), {"left", "center"}),
        ("header_treatment.composition", (analysis.get("header_treatment") or {}).get("composition"), {"full_width_identity", "left_identity_cluster", "centered_identity"}),
        ("header_treatment.background_role", (analysis.get("header_treatment") or {}).get("background_role"), {"background", "secondary", "primary"}),
        ("header_treatment.rule_placement", (analysis.get("header_treatment") or {}).get("rule_placement"), {"none", "top", "bottom"}),
        ("section_heading_treatment.mode", (analysis.get("section_heading_treatment") or {}).get("mode"), {"filled_band", "outlined_band", "underline", "text_only"}),
        ("section_heading_treatment.corner_style", (analysis.get("section_heading_treatment") or {}).get("corner_style"), {"square", "rounded", "capsule"}),
        ("body_region_structure.layout_mode", (analysis.get("body_region_structure") or {}).get("layout_mode"), {"equal_regions", "weighted_regions", "freeform_regions"}),
        ("chrome_treatment.placement", (analysis.get("chrome_treatment") or {}).get("placement"), {"none", "gutters", "section_edges"}),
        ("chrome_treatment.density", (analysis.get("chrome_treatment") or {}).get("density"), {"none", "sparse", "frequent"}),
    )
    for path, value, allowed in checks:
        if value not in allowed:
            raise ReferenceStyleAgentError(
                f"reference style analysis field {path} must be one of {sorted(allowed)}, got {value!r}"
            )
    typography = analysis.get("typography_style") or {}
    for key in ("display_family_category", "body_family_category"):
        if typography.get(key) not in {"sans_serif", "serif"}:
            raise ReferenceStyleAgentError(
                f"reference style analysis field typography_style.{key} must be sans_serif or serif"
            )
    regions = (analysis.get("body_region_structure") or {}).get("regions") or []
    for index, region in enumerate(regions):
        role = region.get("region_role") if isinstance(region, dict) else None
        if role not in _BODY_REGION_ROLES:
            raise ReferenceStyleAgentError(
                f"reference style analysis region {index + 1} has invalid region_role {role!r}"
            )


def _reference_additional_palette_roles(palette: dict[str, Any]) -> dict[str, str]:
    core_keys = {
        "background", "ink", "primary", "secondary", "accent",
        "header_text", "section_heading_text", "additional_roles",
    }
    reserved_roles = {"background", "text", "primary", "secondary", "accent", "header_text", "section_heading_text", "on_primary", "bar"}
    candidates: list[tuple[Any, Any]] = [
        (key, value) for key, value in palette.items() if key not in core_keys
    ]
    nested = palette.get("additional_roles")
    if isinstance(nested, dict):
        candidates.extend(nested.items())
    roles: dict[str, str] = {}
    for raw_key, raw_value in candidates:
        role = re.sub(r"[^a-z0-9]+", "_", str(raw_key).strip().lower()).strip("_")
        color = str(raw_value or "").strip().upper()
        if (
            role
            and role not in reserved_roles
            and re.fullmatch(r"#[0-9A-F]{6}", color)
        ):
            roles[role] = color
    return roles


def _compile_reference_style_contract(
    analysis: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    if int(analysis.get("version") or 0) != 4 or not isinstance(
        analysis.get("body_region_structure"), dict
    ):
        raise ReferenceStyleAgentError(
            "reference style analysis must use version 4 body_region_structure"
        )
    _validate_reference_style_analysis_schema(analysis)
    source_sha = str(metadata.get("source_sha256") or "")
    canvas_contract = _reference_canvas_contract(metadata)
    style_id = f"reference_{source_sha[:8]}"
    header = _normalized_header(analysis.get("header_treatment"))
    section = _normalized_section_heading(analysis.get("section_heading_treatment"))
    lead_band = _normalized_lead_band(analysis.get("lead_band"))
    section_structure = _normalized_section_structure(analysis.get("section_structure"))
    body_region_structure = _normalized_body_region_structure(analysis.get("body_region_structure"))
    column_structure = _compatibility_column_structure(body_region_structure)
    palette = analysis.get("palette") if isinstance(analysis.get("palette"), dict) else {}
    additional_palette_roles = _reference_additional_palette_roles(palette)
    background = _hex(palette.get("background"), "#FFFFFF")
    ink = _readable_color(_hex(palette.get("ink"), "#171717"), background, minimum=7.0)
    primary = _hex(palette.get("primary"), "#1F5F99")
    secondary = _hex(palette.get("secondary"), "#E7EEF5")
    accent = _hex(palette.get("accent"), primary)
    surface_colors = {"background": background, "secondary": secondary, "primary": primary}
    header_surface = surface_colors.get(str(header.get("background_role") or "background"), background)
    header_text = _readable_color(
        _hex(palette.get("header_text"), ink),
        header_surface,
        minimum=4.5,
    )
    section_surface = surface_colors.get(str(section.get("fill_role") or "background"), background)
    section_heading_text = _readable_color(
        _hex(palette.get("section_heading_text"), ink),
        section_surface,
        minimum=4.5,
    )
    on_primary = _readable_color("#FFFFFF", primary, minimum=3.0)
    roles = {
        "background": background,
        "text": ink,
        "primary": primary,
        "secondary": secondary,
        "accent": accent,
        "header_text": header_text,
        "section_heading_text": section_heading_text,
        "on_primary": on_primary,
        "bar": primary,
        **additional_palette_roles,
    }
    css_variables = {
        "--poster-bg": background,
        "--poster-text": ink,
        "--poster-primary": primary,
        "--poster-secondary": secondary,
        "--poster-accent": accent,
        "--poster-header-text": header_text,
        "--poster-section-heading-text": section_heading_text,
        "--poster-on-primary": on_primary,
        "--poster-bar": primary,
    }
    css_variables.update({
        f"--poster-reference-{role.replace('_', '-')}": color
        for role, color in additional_palette_roles.items()
    })
    color_system = {
        "version": 1,
        "palette_id": style_id,
        "palette_name": "Reference Poster",
        "selection_reason": "explicit reference poster style",
        "roles": roles,
        "css_variables": css_variables,
        "allowed_hexes": list(dict.fromkeys(roles.values())),
        "usage_contract": "Use this run-scoped reference palette while preserving target-paper content and system hard gates.",
    }

    surfaces = _normalized_surfaces(analysis.get("surfaces"))
    spacing = _normalized_spacing(analysis.get("spacing"))
    layout = _normalized_layout(
        analysis.get("layout_rhythm"),
        region_ids=[
            str(region["region_id"])
            for region in body_region_structure["regions"]
        ],
    )
    chrome = _normalized_chrome(analysis.get("chrome_treatment"))
    table = _normalized_table(analysis.get("table_treatment"))
    formula = _normalized_formula(analysis.get("formula_treatment"))
    figure = _normalized_figure(analysis.get("figure_treatment"))
    typography = _normalized_typography(analysis.get("typography_style"))
    typography["lead_band_size_px"] = int(lead_band["text_size_px"])
    header_mode = str(header["mode"])
    section_mode = str(section["mode"])

    aesthetic_contract = {
        "reference_priority_policy": "Reconstruct the reference poster's layout and visual grammar from a blank canvas. Do not reuse the normal AutoDesign poster skin when it conflicts with this contract.",
        "canvas_policy": f"Use the reference canvas color {background} with readable academic ink {ink}; no decorative image or gradient background.",
        "palette_usage_policy": "Use only the run-scoped reference palette CSS variables; source figures and source table crops keep their original paper colors.",
        "header_surface_policy": _header_policy(header),
        "lead_band_policy": _lead_band_policy(lead_band),
        "section_surface_policy": _section_policy(section_mode),
        "section_separation_policy": _section_structure_policy(section_structure),
        "body_region_structure_policy": _body_region_structure_policy(body_region_structure),
        "column_structure_policy": _body_region_structure_policy(body_region_structure),
        "chrome_policy": _chrome_policy(chrome),
        "table_surface_policy": _table_policy(table),
        "formula_surface_policy": _formula_policy(formula),
        "source_wrapper_policy": _figure_policy(figure),
        "color_dominance_policy": "Transfer the reference color rhythm without turning body content into a dashboard of colored cards.",
    }
    typography_contract = _typography_contract(typography)
    return {
        "version": _REFERENCE_STYLE_CONTRACT_VERSION,
        "body_region_schema_version": 1,
        "sanitizer_version": _REFERENCE_STYLE_SANITIZER_VERSION,
        "transfer_mode": "reference_first_reconstruction",
        "style_reference_id": style_id,
        "source_sha256": source_sha,
        "source_suffix": metadata.get("source_suffix"),
        "canvas_contract": canvas_contract,
        "summary": (
            f"Reference style with {header_mode} header, {section_mode} section headings, "
            f"and {primary} primary accent."
        ),
        "color_system": color_system,
        "aesthetic_contract": aesthetic_contract,
        "typography_contract": typography_contract,
        "style_tokens": {
            "header_treatment": header,
            "lead_band": lead_band,
            "section_heading_treatment": section,
            "section_structure": section_structure,
            "body_region_structure": body_region_structure,
            "column_structure": column_structure,
            "chrome_treatment": chrome,
            "surfaces": surfaces,
            "spacing": spacing,
            "layout_rhythm": layout,
            "typography_style": typography,
            "table_treatment": table,
            "formula_treatment": formula,
            "figure_treatment": figure,
            "reference_palette_roles": {
                "background": background,
                "ink": ink,
                "primary": primary,
                "secondary": secondary,
                "accent": accent,
                "header_text": header_text,
                "section_heading_text": section_heading_text,
                **additional_palette_roles,
            },
        },
        "required_root_attributes": {
            "data-reference-style-id": style_id,
            "data-reference-transfer-mode": "reference_first",
            "data-header-style": header_mode,
            "data-header-alignment": header["alignment"],
            "data-header-top-rule": header["top_rule"],
            "data-header-rule-placement": header["rule_placement"],
            "data-lead-band": "present" if lead_band["present"] else "absent",
            "data-section-heading-style": section_mode,
            "data-section-heading-corner": section["corner_style"],
            "data-section-separation": section_structure["inter_section_dividers"],
            "data-major-section-count": str(body_region_structure["major_section_count"]),
            "data-reference-layout-mode": str(body_region_structure["layout_mode"]),
            "data-reference-body-region-count": str(body_region_structure["region_count"]),
            "data-reference-region-count": str(body_region_structure["region_count"]),
        },
        "locked_system_constraints": [
            f"{canvas_contract['w_px']}x{canvas_contract['h_px']} fixed reference canvas",
            "reference-owned body region count, proportions, and geometry",
            "header content is title, authors, and institutions only",
            "target-paper content and source assets only",
            "no logos, overlap, clipping, scripts, or remote assets",
        ],
        "reference_owned_decisions": [
            "header composition and alignment",
            "typography family, hierarchy, and role sizes",
            "lead-band presence and treatment",
            "top-level major-section count and subsection hierarchy",
            "section-heading and section-separation treatment",
            "panel, table, figure, spacing, and emphasis treatment",
        ],
        "content_transfer_forbidden": True,
        "do_not_copy": [
            "reference text or claims",
            "reference authors or institutions",
            "reference logos or icons",
            "reference figures or tables",
        ],
    }


def _normalized_header(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    legacy_top_rule = _choice(
        raw.get("top_rule"), {"none", "primary", "ink"},
        "primary" if str(raw.get("mode") or "") == "top_rule_white" else "none",
    )
    rule_placement = _choice(
        raw.get("rule_placement"), {"none", "top", "bottom"},
        "top" if legacy_top_rule != "none" else "none",
    )
    return {
        "mode": _choice(
            raw.get("mode"),
            {"open_white", "tinted_open", "top_rule_white", "filled_band", "subtle_outline", "split_identity"},
            "open_white",
        ),
        "alignment": _choice(raw.get("alignment"), {"left", "center"}, "left"),
        "composition": _choice(
            raw.get("composition"),
            {"full_width_identity", "left_identity_cluster", "centered_identity"},
            "full_width_identity",
        ),
        "background_role": _choice(
            raw.get("background_role"), {"background", "secondary", "primary"},
            "primary" if str(raw.get("mode") or "") == "filled_band" else "background",
        ),
        "title_color_role": _choice(
            raw.get("title_color_role"), {"primary", "ink", "header_text"}, "ink"
        ),
        "rule_placement": rule_placement,
        "rule_color_role": _choice(
            raw.get("rule_color_role"), {"primary", "ink"},
            legacy_top_rule if legacy_top_rule != "none" else "primary",
        ),
        "rule_width_px": _bounded_int(raw.get("rule_width_px"), 0, 16, 0),
        "top_rule": (
            _choice(raw.get("rule_color_role"), {"primary", "ink"}, "primary")
            if rule_placement == "top"
            else "none"
        ),
    }


def _normalized_section_heading(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    mode = _choice(
        raw.get("mode"),
        {"filled_band", "outlined_band", "underline", "rule_only", "text_only"},
        "underline",
    )
    if mode == "rule_only":
        mode = "underline"
    return {
        "mode": mode,
        "text_color_role": _choice(
            raw.get("text_color_role"), {"primary", "ink", "on_primary"},
            "on_primary" if mode == "filled_band" else "ink",
        ),
        "fill_role": _choice(raw.get("fill_role"), {"background", "secondary", "primary"}, "primary" if mode == "filled_band" else "background"),
        "border_role": _choice(raw.get("border_role"), {"primary", "ink"}, "primary"),
        "border_width_px": _bounded_int(raw.get("border_width_px"), 0, 8, 1 if mode == "outlined_band" else 0),
        "corner_style": _choice(raw.get("corner_style"), {"square", "rounded", "capsule"}, "square"),
        "rule_color_role": _choice(raw.get("rule_color_role"), {"primary", "ink"}, "ink"),
        "rule_width_px": _bounded_int(raw.get("rule_width_px"), 1, 12, 3),
    }


def _normalized_lead_band(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    return {
        "present": bool(raw.get("present")),
        "placement": "below_identity",
        "background_role": _choice(raw.get("background_role"), {"primary", "secondary", "accent"}, "primary"),
        "text_color_role": _choice(raw.get("text_color_role"), {"on_primary", "ink"}, "on_primary"),
        "alignment": _choice(raw.get("alignment"), {"left", "center"}, "center"),
        "height_px": _bounded_int(raw.get("height_px"), 40, 140, 64),
        "text_size_px": _bounded_int(raw.get("text_size_px"), 28, 48, 38),
    }


def _normalized_section_structure(value: Any) -> dict[str, str]:
    raw = value if isinstance(value, dict) else {}
    return {
        "inter_section_dividers": _choice(raw.get("inter_section_dividers"), {"none", "hairline", "strong"}, "none"),
        "outer_border": _choice(raw.get("outer_border"), {"none", "hairline"}, "none"),
        "vertical_accent_rules": _choice(raw.get("vertical_accent_rules"), {"none", "sparse", "frequent"}, "none"),
    }


def _normalized_body_region_structure(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    per_region = raw.get("major_sections_per_region")
    raw_regions = raw.get("regions")
    if "major_sections_per_column" in raw:
        raise ReferenceStyleAgentError(
            "version-4 body_region_structure must not use major_sections_per_column"
        )
    if not isinstance(per_region, list) or not isinstance(raw_regions, list):
        raise ReferenceStyleAgentError(
            "version-4 body_region_structure requires regions and major_sections_per_region"
        )
    if not 2 <= len(raw_regions) <= 6 or len(per_region) != len(raw_regions):
        raise ReferenceStyleAgentError(
            "reference style analysis must report two to six observed body regions"
        )
    try:
        declared_region_count = int(raw.get("region_count"))
        declared_total = int(raw.get("major_section_count"))
        normalized_counts = [int(item) for item in per_region]
    except (TypeError, ValueError) as exc:
        raise ReferenceStyleAgentError(
            "reference style body-region counts must be integers"
        ) from exc
    if declared_region_count != len(raw_regions):
        raise ReferenceStyleAgentError(
            "reference style region_count does not match regions length"
        )
    if any(count < 1 or count > 3 for count in normalized_counts):
        raise ReferenceStyleAgentError(
            "reference style major section counts must be between one and three per region"
        )
    regions: list[dict[str, Any]] = []
    for index, item in enumerate(raw_regions):
        if not isinstance(item, dict):
            raise ReferenceStyleAgentError("reference style regions must be JSON objects")
        region_id = str(item.get("region_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", region_id):
            raise ReferenceStyleAgentError(
                f"reference style analysis has invalid body region id: {region_id!r}"
            )
        try:
            section_count = int(item.get("section_count"))
            reading_order = int(item.get("reading_order"))
        except (TypeError, ValueError) as exc:
            raise ReferenceStyleAgentError(
                f"reference style region {region_id} has non-integer counts"
            ) from exc
        if section_count != normalized_counts[index]:
            raise ReferenceStyleAgentError(
                f"reference style region {region_id} section_count disagrees with major_sections_per_region"
            )
        if reading_order != index + 1:
            raise ReferenceStyleAgentError(
                "reference style regions must be listed in contiguous reading_order starting at one"
            )
        regions.append({
            "region_id": region_id,
            "region_role": str(item.get("region_role") or ""),
            "section_count": section_count,
            "reading_order": reading_order,
        })
    ids = [str(region["region_id"]) for region in regions]
    if len(ids) != len(set(ids)):
        raise ReferenceStyleAgentError("reference style analysis body region ids must be unique")
    if declared_total != sum(normalized_counts):
        raise ReferenceStyleAgentError(
            "reference style major_section_count does not match per-region totals"
        )
    return {
        "layout_mode": str(raw.get("layout_mode")),
        "region_count": len(regions),
        "major_section_count": sum(normalized_counts),
        "major_sections_per_region": normalized_counts,
        "regions": regions,
        "subsection_treatment": _choice(
            raw.get("subsection_treatment"),
            {"inline_colored_label", "small_heading", "none"},
            "inline_colored_label",
        ),
    }


def _compatibility_column_structure(body_regions: dict[str, Any]) -> dict[str, Any]:
    layout_mode = str(body_regions.get("layout_mode") or "equal_regions")
    compatibility_mode = {
        "equal_regions": "equal_columns",
        "weighted_regions": "weighted_columns",
    }.get(layout_mode, layout_mode)
    per_region = [int(item) for item in body_regions.get("major_sections_per_region") or []]
    return {
        "layout_mode": compatibility_mode,
        "region_count": int(body_regions.get("region_count") or len(per_region)),
        "major_section_count": int(body_regions.get("major_section_count") or sum(per_region)),
        "major_sections_per_column": per_region,
        "region_roles": [
            str(item.get("region_role") or "column")
            for item in body_regions.get("regions") or []
            if isinstance(item, dict)
        ],
        "subsection_treatment": str(
            body_regions.get("subsection_treatment") or "inline_colored_label"
        ),
        "compatibility_only": True,
    }


def _normalized_surfaces(value: Any) -> dict[str, str]:
    raw = value if isinstance(value, dict) else {}
    return {
        "panel_fill": _choice(raw.get("panel_fill"), {"white", "near_white", "transparent"}, "white"),
        "border_style": _choice(raw.get("border_style"), {"none", "hairline"}, "none"),
        "corner_style": _choice(raw.get("corner_style"), {"square", "subtle"}, "square"),
        "shadow_style": _choice(raw.get("shadow_style"), {"none", "subtle"}, "none"),
    }


def _normalized_table(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    return {
        "observed": bool(raw.get("observed")),
        "rule_style": _choice(raw.get("rule_style"), {"none", "booktabs", "hairline_grid", "minimal"}, "none"),
        "header_fill": _choice(raw.get("header_fill"), {"none", "light", "primary"}, "none"),
    }


def _normalized_formula(value: Any) -> dict[str, str]:
    raw = value if isinstance(value, dict) else {}
    return {
        "frame": _choice(raw.get("frame"), {"none", "hairline", "box"}, "none"),
        "background": _choice(raw.get("background"), {"none", "light"}, "none"),
    }


def _normalized_figure(value: Any) -> dict[str, str]:
    raw = value if isinstance(value, dict) else {}
    return {
        "frame": _choice(raw.get("frame"), {"none", "hairline"}, "none"),
        "caption_alignment": _choice(raw.get("caption_alignment"), {"left", "center"}, "left"),
    }


def _normalized_typography(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    legacy_family = _choice(raw.get("family_category"), {"sans_serif", "serif"}, "sans_serif")
    display_family = _choice(
        raw.get("display_family_category"), {"sans_serif", "serif"}, legacy_family
    )
    body_family = _choice(
        raw.get("body_family_category"), {"sans_serif", "serif"}, legacy_family
    )
    return {
        "family_category": body_family,
        "font_family": _REFERENCE_FONT_STACKS[body_family],
        "display_family_category": display_family,
        "display_font_family": _REFERENCE_FONT_STACKS[display_family],
        "body_family_category": body_family,
        "body_font_family": _REFERENCE_FONT_STACKS[body_family],
        "title_weight": _bounded_int(raw.get("title_weight"), 500, 800, 700),
        "identity_weight": _bounded_int(raw.get("identity_weight"), 400, 700, 500),
        "section_heading_weight": _bounded_int(raw.get("section_heading_weight"), 500, 800, 700),
        "body_weight": _bounded_int(raw.get("body_weight"), 350, 550, 400),
        "title_size_px": _bounded_int(raw.get("title_size_px"), 48, 96, 72),
        "identity_size_px": _bounded_int(raw.get("identity_size_px"), 20, 36, 26),
        "section_heading_size_px": _bounded_int(raw.get("section_heading_size_px"), 28, 48, 34),
        "body_size_px": _bounded_int(raw.get("body_size_px"), 20, 30, 24),
        "caption_size_px": _bounded_int(raw.get("caption_size_px"), 16, 24, 20),
        "lead_band_size_px": _bounded_int(raw.get("lead_band_size_px"), 28, 48, 38),
    }


def _typography_contract(typography: dict[str, Any]) -> dict[str, Any]:
    title_size = int(typography["title_size_px"])
    identity_size = int(typography["identity_size_px"])
    heading_size = int(typography["section_heading_size_px"])
    body_size = int(typography["body_size_px"])
    caption_size = int(typography["caption_size_px"])
    lead_band_size = int(typography["lead_band_size_px"])
    return {
        "source": "reference_poster",
        "family_category": typography["family_category"],
        "font_family": typography["font_family"],
        "display_family_category": typography["display_family_category"],
        "display_font_family": typography["display_font_family"],
        "body_family_category": typography["body_family_category"],
        "body_font_family": typography["body_font_family"],
        "primary_font_family": typography["font_family"].split(",", 1)[0].strip().strip('"'),
        "title_font_size_px": title_size,
        "identity_rows_font_size_px": identity_size,
        "section_heading_font_size_px": heading_size,
        "subsection_heading_font_size_px": body_size,
        "body_font_size_px": body_size,
        "readout_font_size_px": body_size,
        "table_text_font_size_px": body_size,
        "caption_label_font_size_px": caption_size,
        "caption_font_size_px": caption_size,
        "label_font_size_px": caption_size,
        "lead_band_font_size_px": lead_band_size,
        "font_size_tolerance_px": 1.5,
        "title_weight": int(typography["title_weight"]),
        "identity_weight": int(typography["identity_weight"]),
        "section_heading_weight": int(typography["section_heading_weight"]),
        "body_weight": int(typography["body_weight"]),
        "title_weight_min": max(400, int(typography["title_weight"]) - 100),
        "title_weight_max": min(900, int(typography["title_weight"]) + 100),
        "heading_weight_min": max(400, int(typography["section_heading_weight"]) - 100),
        "body_weight_max": min(650, int(typography["body_weight"]) + 150),
        "max_body_italic_ratio": 0.15,
        "fixed_role_font_sizes_required": True,
    }


def _normalized_spacing(value: Any) -> dict[str, int]:
    raw = value if isinstance(value, dict) else {}
    limits = {
        "outer_margin_px": (20, 180),
        "column_gap_px": (16, 100),
        "section_gap_px": (8, 80),
        "panel_padding_px": (6, 60),
    }
    out: dict[str, int] = {}
    for key, (lower, upper) in limits.items():
        try:
            number = int(round(float(raw.get(key))))
        except (TypeError, ValueError):
            continue
        out[key] = max(lower, min(upper, number))
    return out


def _normalized_layout(value: Any, *, region_ids: list[str]) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    region_count = len(region_ids)
    proportions = raw.get("region_proportions")
    if not isinstance(proportions, list):
        proportions = raw.get("column_proportions")
    normalized: list[float] = []
    if isinstance(proportions, list) and len(proportions) == region_count:
        try:
            normalized = [max(0.5, min(2.0, float(item))) for item in proportions]
        except (TypeError, ValueError):
            normalized = []
    region_boxes: list[dict[str, Any]] = []
    raw_boxes = raw.get("region_boxes")
    if isinstance(raw_boxes, list) and len(raw_boxes) == region_count:
        boxes_by_id: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(raw_boxes):
            if not isinstance(item, dict):
                boxes_by_id = {}
                break
            region_id = str(item.get("region_id") or f"region_{index + 1}")
            if region_id not in region_ids or region_id in boxes_by_id:
                boxes_by_id = {}
                break
            try:
                x = max(0.0, min(95.0, float(item.get("x_pct"))))
                y = max(0.0, min(95.0, float(item.get("y_pct"))))
                width = max(5.0, min(100.0 - x, float(item.get("w_pct"))))
                height = max(5.0, min(100.0 - y, float(item.get("h_pct"))))
            except (TypeError, ValueError):
                boxes_by_id = {}
                break
            boxes_by_id[region_id] = {
                "region_id": region_id,
                "x_pct": round(x, 3),
                "y_pct": round(y, 3),
                "w_pct": round(width, 3),
                "h_pct": round(height, 3),
            }
        if set(boxes_by_id) == set(region_ids):
            region_boxes = [boxes_by_id[region_id] for region_id in region_ids]
    return {
        "region_proportions": normalized or [1.0] * region_count,
        "column_proportions": normalized or [1.0] * region_count,
        "region_boxes": region_boxes,
        "density": _choice(raw.get("density"), {"dense", "balanced"}, "dense"),
    }


def _normalized_chrome(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    present = bool(raw.get("present"))
    return {
        "present": present,
        "placement": _choice(
            raw.get("placement"),
            {"none", "gutters", "section_edges"},
            "gutters" if present else "none",
        ),
        "density": _choice(
            raw.get("density"),
            {"none", "sparse", "frequent"},
            "sparse" if present else "none",
        ),
        "crossing_policy": "never_cross_content",
    }


def _header_policy(header: dict[str, Any]) -> str:
    mode = str(header.get("mode") or "open_white")
    policy = {
        "open_white": "Use the reference's open white identity area with no enclosing band or decorative frame.",
        "tinted_open": "Use the reference's pale open identity surface without turning it into a saturated banner.",
        "filled_band": "Use one filled reference-color identity surface with readable identity text.",
        "subtle_outline": "Use the reference's white identity area with only its subtle hairline outline.",
        "top_rule_white": "Use a white identity area with one top reference-color rule only.",
        "split_identity": "Use the reference's split/asymmetric identity composition, but collapse any logo or QR reservation and reflow only target title, authors, and institutions.",
    }[mode]
    placement = str(header.get("rule_placement") or "none")
    if placement == "none":
        policy += " The reference has no identity rule; do not add a colored top or bottom border."
    else:
        policy += f" Use exactly one `{placement}` identity rule at {int(header.get('rule_width_px') or 0)}px."
    return policy


def _section_policy(mode: str) -> str:
    return {
        "filled_band": "Use compact filled primary major section-heading bands with readable section-heading text; keep section bodies white/neutral.",
        "outlined_band": "Use the reference's lightly filled or white outlined major-heading capsules/bands; preserve its border weight and corner shape without filling them as solid bars.",
        "underline": "Use unfilled major section headings with the reference-colored text/rule treatment and one underline only; do not add a second top rule or box the section body.",
        "text_only": "Use primary-colored major section-heading text with minimal rules and unboxed white/neutral section bodies.",
    }[mode]


def _lead_band_policy(lead_band: dict[str, Any]) -> str:
    if not lead_band.get("present"):
        return "Do not invent a full-width lead or summary band when the reference has none."
    return (
        "Place one full-width target-paper summary band immediately below the identity header and outside it. "
        "Match the reference band geometry and color, but write a concise target-paper summary and never copy reference wording."
    )


def _section_structure_policy(section_structure: dict[str, Any]) -> str:
    dividers = str(section_structure.get("inter_section_dividers") or "none")
    border = str(section_structure.get("outer_border") or "none")
    vertical = str(section_structure.get("vertical_accent_rules") or "none")
    return (
        f"Inter-section dividers: {dividers}; section outer borders: {border}; vertical accent rules: {vertical}. "
        "Do not add default AutoDesign section top lines, colored side stems, or panel frames when the reference omits them."
    )


def _body_region_structure_policy(body_regions: dict[str, Any]) -> str:
    per_region = [
        int(item) for item in body_regions.get("major_sections_per_region") or [1, 1, 1]
    ]
    subsection = str(body_regions.get("subsection_treatment") or "inline_colored_label")
    layout_mode = str(body_regions.get("layout_mode") or "equal_regions")
    role_summary = [
        f"{item.get('region_id')}:{item.get('region_role')}"
        for item in body_regions.get("regions") or []
        if isinstance(item, dict)
    ]
    if all(item == 1 for item in per_region):
        return (
            f"Use exactly one top-level `.poster-section` in each of the {len(per_region)} reference-owned "
            f"macro body regions using `{layout_mode}` geometry and roles {role_summary}. "
            f"Organize all additional paper topics inside that major section with `{subsection}` subsection headings, "
            "inline lead labels, figures, tables, and prose. Every footer band, side callout, and bottom question "
            "must remain inside its own declared region; do not leave top-level content unowned."
        )
    return (
        f"Match the reference top-level major-section counts per macro region exactly: {per_region}; "
        f"region roles are {role_summary}. Use `{subsection}` for nested topics instead of inventing extra major sections, "
        "and keep every top-level section owned by exactly one declared body region."
    )


def _chrome_policy(chrome: dict[str, Any]) -> str:
    if not bool(chrome.get("present")):
        return "Do not invent ornamental routes, rails, connectors, or section-edge chrome when the reference has none."
    placement = str(chrome.get("placement") or "gutters")
    density = str(chrome.get("density") or "sparse")
    return (
        f"Use `{density}` ornamental chrome only in the root-level `[data-style-role=chrome-layer]`, "
        f"confined to the reference's `{placement}` geometry behind content. Never attach it to section/column "
        "pseudo-elements or let it cross text, figures, tables, or formulas."
    )


def _table_policy(table: dict[str, Any]) -> str:
    observed = bool(table.get("observed"))
    style = _choice(table.get("rule_style"), {"none", "booktabs", "hairline_grid", "minimal"}, "none")
    if not observed or style == "none":
        return (
            "The reference does not establish a table-rule style. Keep native target-paper tables visually open: "
            "no outer top/bottom frame, no row-by-row horizontal rules, no colored box, and at most one subtle header underline when needed for readability."
        )
    if style == "minimal":
        return "Use the reference's minimal table treatment with at most one subtle header rule and no outer frame or row-by-row rules."
    return f"Follow the visibly observed reference `{style}` table treatment while keeping native table text editable and avoiding heavier rules than the reference."


def _formula_policy(formula: dict[str, Any]) -> str:
    frame = _choice(formula.get("frame"), {"none", "hairline", "box"}, "none")
    background = _choice(formula.get("background"), {"none", "light"}, "none")
    if frame == "none":
        return (
            "Keep equations in normal white content flow with no top/bottom separator rules, outline, or decorative frame. "
            + ("A light reference-matched background is allowed." if background == "light" else "Do not add a formula background panel.")
        )
    return f"Use only the reference's `{frame}` formula frame and `{background}` background; do not add extra separator rules."


def _figure_policy(figure: dict[str, Any]) -> str:
    frame = _choice(figure.get("frame"), {"none", "hairline"}, "none")
    if frame == "hairline":
        return "Use at most a subtle neutral hairline around source visuals; no colored wrapper border, shadow, or card chrome."
    return "Keep source figure/table wrapper borders transparent with no visible outline or shadow."


def _validate_raw_reference_style_blueprint(
    blueprint_path: Path,
    contract: dict[str, Any],
) -> None:
    try:
        soup = BeautifulSoup(blueprint_path.read_text(encoding="utf-8"), "html.parser")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReferenceStyleAgentError(f"cannot read reference style blueprint: {exc}") from exc
    forbidden = sorted({
        tag.name for tag in soup.find_all(
            [
                "script", "img", "svg", "canvas", "iframe", "object", "embed",
                "video", "audio", "link", "base", "form", "input", "button", "a",
            ]
        )
    })
    if forbidden:
        raise ReferenceStyleAgentError(
            f"raw reference style blueprint contains forbidden tags: {forbidden}"
        )
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        for name, value in tag.attrs.items():
            serialized = " ".join(value) if isinstance(value, list) else str(value or "")
            if str(name).lower().startswith("on") or re.search(
                r"(?:https?:|data:|javascript:|url\s*\()", serialized, flags=re.I
            ):
                raise ReferenceStyleAgentError(
                    f"raw reference style blueprint contains unsafe attribute {name}"
                )
    identity_headers = soup.select('[data-style-role="identity-header"]')
    if len(identity_headers) != 1:
        raise ReferenceStyleAgentError(
            "raw reference style blueprint must contain exactly one identity-header"
        )
    identity_header = identity_headers[0]
    visible_nodes = [
        node
        for node in soup.find_all(string=True)
        if (
            isinstance(node.parent, Tag)
            and not isinstance(node, Comment)
            and node.parent.name not in {"style", "title", "[document]"}
            and not isinstance(node.parent.find_parent("head"), Tag)
        )
    ]
    visible_text = "".join(str(node) for node in visible_nodes)
    identity_text = identity_header.get_text("", strip=False)
    for placeholder in ("{{PAPER_TITLE}}", "{{AUTHORS}}", "{{INSTITUTIONS}}"):
        if identity_text.count(placeholder) != 1 or visible_text.count(placeholder) != 1:
            raise ReferenceStyleAgentError(
                f"identity placeholder {placeholder} must appear exactly once and only inside identity-header"
            )
    for node in soup.find_all(string=True):
        if (
            not isinstance(node.parent, Tag)
            or isinstance(node, Comment)
            or node.parent.name in {"style", "title", "[document]"}
            or isinstance(node.parent.find_parent("head"), Tag)
        ):
            continue
        remainder = re.sub(r"\{\{[A-Z0-9_]+\}\}", "", str(node))
        if remainder.strip():
            raise ReferenceStyleAgentError(
                "raw reference style blueprint visible text must contain approved placeholders only"
            )
        for placeholder in re.findall(r"\{\{[A-Z0-9_]+\}\}", str(node)):
            if placeholder not in _BLUEPRINT_PLACEHOLDERS:
                raise ReferenceStyleAgentError(
                    f"raw reference style blueprint contains unknown placeholder {placeholder}"
                )
    allowed_hexes = {
        str(item).upper()
        for item in (contract.get("color_system") or {}).get("allowed_hexes", [])
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", str(item or ""))
    }
    _validate_blueprint_palette_css(soup, allowed_hexes)


def _validate_blueprint_palette_css(soup: BeautifulSoup, allowed_hexes: set[str]) -> None:
    css_sources = [
        style.get_text("\n", strip=False)
        for style in soup.find_all("style")
    ]
    css_sources.extend(
        str(tag.get("style") or "")
        for tag in soup.find_all(style=True)
        if isinstance(tag, Tag)
    )
    used_hexes = {
        match.group(0).upper()
        for css in css_sources
        for match in re.finditer(r"#[0-9A-Fa-f]{6}\b", css)
    }
    unexpected = sorted(used_hexes - allowed_hexes)
    if unexpected:
        raise ReferenceStyleAgentError(
            f"raw reference style blueprint uses colors outside its analysis palette: {unexpected}"
        )
    malformed_hexes = sorted({
        match.group(0)
        for css in css_sources
        for match in re.finditer(r"#[0-9A-Fa-f]{3,8}\b", css)
        if len(match.group(0)) != 7
    })
    if malformed_hexes:
        raise ReferenceStyleAgentError(
            "raw reference style blueprint must use six-digit palette colors only: "
            f"{malformed_hexes}"
        )
    color_declaration = re.compile(
        r"(?:^|[;{])\s*(?:--[\w-]+|color|background(?:-color)?|border(?:-[\w-]+)?|"
        r"outline(?:-[\w-]+)?|box-shadow|text-shadow|fill|stroke)\s*:\s*([^;}]+)",
        flags=re.I,
    )
    color_function = re.compile(
        r"\b(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch|color)\s*\(",
        flags=re.I,
    )
    for css in css_sources:
        for declaration in color_declaration.finditer(css):
            value = declaration.group(1)
            if color_function.search(value):
                raise ReferenceStyleAgentError(
                    "raw reference style blueprint uses a non-hex color function; declare and use a six-digit palette color"
                )
            named_color_probe = re.sub(r"var\s*\([^)]*\)", "", value, flags=re.I)
            for token in re.findall(r"\b[A-Za-z]+\b", named_color_probe):
                if token.lower() in {"transparent", "currentcolor", "none", "inherit", "initial", "unset"}:
                    continue
                try:
                    ImageColor.getrgb(token)
                except ValueError:
                    continue
                raise ReferenceStyleAgentError(
                    f"raw reference style blueprint uses named color {token!r}; use a declared six-digit palette color"
                )


def _sanitize_reference_style_blueprint(
    source_path: Path,
    destination_path: Path,
    contract: dict[str, Any],
    *,
    cancellation_check: Callable[[str], None] | None = None,
) -> None:
    _run_reference_style_cancellation_check(
        cancellation_check,
        "reference_style.sanitize.start",
    )
    try:
        soup = BeautifulSoup(source_path.read_text(encoding="utf-8"), "html.parser")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReferenceStyleAgentError(f"cannot read reference style blueprint: {exc}") from exc

    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()
    for tag in list(soup.find_all(["script", "img", "svg", "canvas", "iframe", "object", "embed", "video", "audio", "link", "meta", "base", "form", "input", "button", "a"])):
        tag.decompose()
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        for attr in list(tag.attrs):
            name = str(attr).lower()
            value = str(tag.attrs.get(attr) or "")
            if name.startswith("on") or name in {"src", "href", "srcset", "action", "formaction"}:
                del tag.attrs[attr]
                continue
            if "url(" in value.lower() or "javascript:" in value.lower() or "data:" in value.lower():
                del tag.attrs[attr]

    allowed_hexes = set(
        str(item).upper()
        for item in (contract.get("color_system") or {}).get("allowed_hexes", [])
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", str(item or ""))
    )
    tokens = contract.get("style_tokens") if isinstance(contract.get("style_tokens"), dict) else {}
    header = tokens.get("header_treatment") if isinstance(tokens.get("header_treatment"), dict) else {}
    section_structure = tokens.get("section_structure") if isinstance(tokens.get("section_structure"), dict) else {}
    chrome = tokens.get("chrome_treatment") if isinstance(tokens.get("chrome_treatment"), dict) else {}
    table = tokens.get("table_treatment") if isinstance(tokens.get("table_treatment"), dict) else {}
    formula = tokens.get("formula_treatment") if isinstance(tokens.get("formula_treatment"), dict) else {}
    for style_tag in soup.find_all("style"):
        css = style_tag.get_text("\n", strip=False)
        css = re.sub(r"@import\s+[^;]+;?", "", css, flags=re.I)
        css = re.sub(r"@font-face\s*\{.*?\}", "", css, flags=re.I | re.S)
        css = re.sub(r"url\s*\([^)]*\)", "none", css, flags=re.I)
        css = re.sub(r"expression\s*\([^)]*\)", "", css, flags=re.I)
        css = _strip_unsafe_generated_content(css)

        def replace_hex(match: re.Match[str]) -> str:
            color = match.group(0).upper()
            if not allowed_hexes or color in allowed_hexes:
                return color
            return _nearest_allowed_hex(color, allowed_hexes)

        css = re.sub(r"#[0-9A-Fa-f]{6}\b", replace_hex, css)
        rule_placement = str(header.get("rule_placement") or "none")
        if rule_placement == "none":
            css = _strip_css_properties_for_matching_rules(
                css,
                ("identity-header", "poster-header", "reference-style-blueprint"),
                ("border", "border-top", "border-block", "border-block-start", "box-shadow"),
            )
            css = _remove_css_pseudo_rules_for_matching_selectors(
                css,
                ("identity-header", "poster-header", "reference-style-blueprint"),
            )
        elif rule_placement == "bottom":
            css = _strip_css_properties_for_matching_rules(
                css,
                ("identity-header", "poster-header", "reference-style-blueprint"),
                ("border-top", "border-block-start"),
            )
        elif rule_placement == "top":
            css = _strip_css_properties_for_matching_rules(
                css,
                ("identity-header", "poster-header", "reference-style-blueprint"),
                ("border-bottom", "border-block-end"),
            )
        if str(section_structure.get("inter_section_dividers") or "none") == "none":
            css = _strip_css_properties_for_matching_rules(
                css,
                ('data-style-role="section"', "poster-section"),
                ("border-top", "border-bottom"),
            )
        if not bool(table.get("observed")) or str(table.get("rule_style") or "none") == "none":
            css = _strip_css_properties_for_matching_rules(
                css,
                ("table-slot", "booktabs", "native-table", "table"),
                ("border", "border-top", "border-right", "border-bottom", "border-left"),
            )
        if str(formula.get("frame") or "none") == "none":
            css = _strip_css_properties_for_matching_rules(
                css,
                ("formula-slot", "formula", "math-block"),
                (
                    "border", "border-top", "border-right", "border-bottom", "border-left",
                    "background", "background-color", "box-shadow",
                ),
            )
        css = _remove_content_region_pseudo_rules(css)
        style_tag.string = css

    for node in list(soup.find_all(string=True)):
        if not isinstance(node, NavigableString) or isinstance(node, Comment):
            continue
        if isinstance(node.parent, Tag) and node.parent.name == "style":
            continue
        text = str(node)
        if not text.strip():
            continue
        placeholders = re.findall(r"\{\{[A-Z0-9_]+\}\}", text)
        if placeholders and all(item in _BLUEPRINT_PLACEHOLDERS for item in placeholders):
            remainder = re.sub(r"\{\{[A-Z0-9_]+\}\}", "", text)
            if not remainder.strip():
                continue
        safe = " ".join(item for item in placeholders if item in _BLUEPRINT_PLACEHOLDERS)
        node.replace_with(safe)

    root = soup.select_one(".reference-style-blueprint")
    if not isinstance(root, Tag):
        raise ReferenceStyleAgentError(
            "reference style blueprint must contain a .reference-style-blueprint root"
        )
    root["class"] = _append_class(root.get("class"), "paper-poster")
    required_root_attributes = contract.get("required_root_attributes")
    if isinstance(required_root_attributes, dict):
        for name, value in required_root_attributes.items():
            root[str(name)] = str(value)
    identity = root.find(attrs={"data-style-role": "identity-header"})
    body_container = root.find(attrs={"data-style-role": "body-regions"})
    if not isinstance(body_container, Tag):
        body_container = root.find(attrs={"data-style-role": "columns"})
        if isinstance(body_container, Tag):
            body_container["data-style-role"] = "body-regions"
    if isinstance(identity, Tag):
        identity["class"] = _append_class(identity.get("class"), "poster-header")
        identity["data-panel-role"] = "identity_header"
    lead = root.find(attrs={"data-style-role": "lead-band"})
    if isinstance(lead, Tag):
        lead["class"] = _append_class(lead.get("class"), "reference-lead-band")
    if isinstance(body_container, Tag):
        body_container["class"] = _append_class(body_container.get("class"), "poster-body-regions")
        body_container["class"] = _append_class(body_container.get("class"), "poster-columns")
        body_container["data-layout-region"] = "reference_body_regions"
    region_tags = [
        tag for tag in (
            body_container.find_all(attrs={"data-style-role": "body-region"})
            if isinstance(body_container, Tag)
            else []
        )
        if isinstance(tag, Tag)
    ]
    if not region_tags:
        region_tags = [
            tag for tag in (
                body_container.find_all(attrs={"data-style-role": "column"})
                if isinstance(body_container, Tag)
                else []
            )
            if isinstance(tag, Tag)
        ]
        for tag in region_tags:
            tag["data-style-role"] = "body-region"
    region_structure = (
        tokens.get("body_region_structure")
        if isinstance(tokens.get("body_region_structure"), dict)
        else {}
    )
    expected_regions = [
        item for item in region_structure.get("regions") or []
        if isinstance(item, dict)
    ]
    expected_per_region = [
        int(item) for item in region_structure.get("major_sections_per_region") or []
    ]
    expected_region_ids = [str(item.get("region_id") or "") for item in expected_regions]
    expected_region_count = len(expected_regions)
    for index, region in enumerate(region_tags):
        expected = expected_regions[index] if index < len(expected_regions) else {}
        region_id = str(region.get("data-region-id") or expected.get("region_id") or f"region_{index + 1}")
        region_role = str(region.get("data-region-role") or expected.get("region_role") or "column")
        region["data-region-id"] = region_id
        region["data-region-role"] = region_role
        region["class"] = _append_class(region.get("class"), "poster-body-region")
        region["class"] = _append_class(region.get("class"), "poster-column")
    chrome_layers = [
        tag for tag in root.find_all(attrs={"data-style-role": "chrome-layer"})
        if isinstance(tag, Tag)
    ]
    direct_chrome_layers = [tag for tag in chrome_layers if tag.parent is root]
    if bool(chrome.get("present")):
        if len(chrome_layers) != 1 or len(direct_chrome_layers) != 1:
            raise ReferenceStyleAgentError(
                "reference blueprint with decorative chrome must provide exactly one root-level chrome-layer"
            )
        chrome_layer = direct_chrome_layers[0]
        chrome_layer["class"] = _append_class(chrome_layer.get("class"), "reference-chrome")
        chrome_layer["aria-hidden"] = "true"
    else:
        for chrome_layer in chrome_layers:
            chrome_layer.decompose()
    for section_tag in root.find_all(attrs={"data-style-role": "section"}):
        if isinstance(section_tag, Tag):
            section_tag["class"] = _append_class(section_tag.get("class"), "poster-section")
    for heading in root.find_all(attrs={"data-style-role": "section-heading"}):
        if isinstance(heading, Tag):
            heading["class"] = _append_class(heading.get("class"), "section-heading")

    required_roles = {
        "identity-header", "body-regions", "body-region", "section", "section-heading"
    }
    roles = {
        str(tag.get("data-style-role") or "").strip()
        for tag in root.find_all(True)
        if isinstance(tag, Tag)
    }
    missing = sorted(required_roles - roles)
    actual_region_ids = [str(region.get("data-region-id") or "") for region in region_tags]
    actual_region_roles = [str(region.get("data-region-role") or "") for region in region_tags]
    expected_region_roles = [str(item.get("region_role") or "") for item in expected_regions]
    all_region_tags = [
        tag for tag in root.find_all(attrs={"data-style-role": "body-region"})
        if isinstance(tag, Tag)
    ]
    if len(actual_region_ids) != len(set(actual_region_ids)):
        raise ReferenceStyleAgentError("reference style blueprint body region ids must be unique")
    all_major_sections = [
        section for section in root.find_all(attrs={"data-style-role": "section"})
        if isinstance(section, Tag)
        and not isinstance(section.find_parent(attrs={"data-style-role": "section"}), Tag)
    ]
    unowned_sections = [
        section for section in all_major_sections
        if not isinstance(section.find_parent(attrs={"data-style-role": "body-region"}), Tag)
    ]
    body_placeholders = {
        "{{SECTION_TITLE}}",
        "{{TARGET_PAPER_CONTENT}}",
        "{{TARGET_PAPER_FIGURE}}",
        "{{TARGET_PAPER_TABLE}}",
    }
    unowned_placeholder_nodes = []
    for node in root.find_all(string=True):
        if not isinstance(node.parent, Tag) or node.parent.name == "style":
            continue
        if not any(placeholder in str(node) for placeholder in body_placeholders):
            continue
        owner = (
            node.parent
            if node.parent.get("data-style-role") == "body-region"
            else node.parent.find_parent(attrs={"data-style-role": "body-region"})
        )
        if not isinstance(owner, Tag):
            unowned_placeholder_nodes.append(node)
    for section_tag in all_major_sections:
        owner = section_tag.find_parent(attrs={"data-style-role": "body-region"})
        if isinstance(owner, Tag):
            section_tag["data-reference-region-member"] = str(owner.get("data-region-id") or "")
    actual_per_region = [len(_top_level_sections_for_region(region)) for region in region_tags]
    if (
        missing
        or not isinstance(body_container, Tag)
        or len(region_tags) != expected_region_count
        or len(all_region_tags) != len(region_tags)
        or actual_region_ids != expected_region_ids
        or actual_region_roles != expected_region_roles
        or actual_per_region != expected_per_region
        or unowned_sections
        or unowned_placeholder_nodes
    ):
        raise ReferenceStyleAgentError(
            "reference style blueprint is missing required semantic roles "
            f"(missing={missing}, regions={len(region_tags)}, expected_region_ids={expected_region_ids}, "
            f"actual_region_ids={actual_region_ids}, expected_region_roles={expected_region_roles}, "
            f"actual_region_roles={actual_region_roles}, expected_sections={expected_per_region}, "
            f"actual_sections={actual_per_region}, unowned_sections={len(unowned_sections)}, "
            f"unowned_placeholder_nodes={len(unowned_placeholder_nodes)})"
        )
    _run_reference_style_cancellation_check(
        cancellation_check,
        "reference_style.sanitize.before_destination_directory",
    )
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    _run_reference_style_cancellation_check(
        cancellation_check,
        "reference_style.sanitize.before_destination_write",
    )
    destination_path.write_text(str(soup), encoding="utf-8")
    _run_reference_style_cancellation_check(
        cancellation_check,
        "reference_style.sanitize.after_destination_write",
    )


def _top_level_sections_for_region(region: Tag) -> list[Tag]:
    sections: list[Tag] = []
    for tag in region.find_all(attrs={"data-style-role": "section"}):
        if not isinstance(tag, Tag):
            continue
        if tag.find_parent(attrs={"data-style-role": "body-region"}) is not region:
            continue
        if isinstance(tag.find_parent(attrs={"data-style-role": "section"}), Tag):
            continue
        sections.append(tag)
    return sections


def _normalize_blueprint_major_sections(regions: list[Tag], expected_per_region: list[int]) -> None:
    if len(regions) != len(expected_per_region):
        return
    for region, expected in zip(regions, expected_per_region):
        sections = _top_level_sections_for_region(region)
        if len(sections) <= expected:
            continue
        target = sections[max(0, expected - 1)]
        for extra in sections[expected:]:
            heading = extra.find(attrs={"data-style-role": "section-heading"})
            if isinstance(heading, Tag):
                heading.name = "h3"
                heading["data-style-role"] = "subsection-heading"
                heading_classes = [
                    str(item) for item in (heading.get("class") or [])
                    if str(item) != "section-heading"
                ]
                heading["class"] = _append_class(heading_classes, "subsection-heading")
            for child in list(extra.contents):
                target.append(child.extract() if hasattr(child, "extract") else child)
            extra.decompose()


def _append_class(value: Any, class_name: str) -> list[str]:
    classes = [str(item) for item in value] if isinstance(value, list) else str(value or "").split()
    if class_name not in classes:
        classes.append(class_name)
    return classes


def _image_diff_ratio(left_path: Path, right_path: Path) -> float:
    with Image.open(left_path).convert("RGB") as left, Image.open(right_path).convert("RGB") as right:
        if left.size != right.size:
            return 1.0
        histogram = ImageChops.difference(left, right).convert("L").histogram()
        changed = left.width * left.height - histogram[0]
        return round(changed / max(1, left.width * left.height), 8)


def _strip_css_properties_for_matching_rules(
    css: str,
    selector_needles: tuple[str, ...],
    properties: tuple[str, ...],
) -> str:
    property_names = {item.lower() for item in properties}

    def rewrite(match: re.Match[str]) -> str:
        selector = match.group(1)
        declarations = match.group(2)
        selector_text = selector.lower()
        if not any(_selector_matches_needle(selector_text, needle) for needle in selector_needles):
            return match.group(0)
        kept = []
        for declaration in declarations.split(";"):
            name = declaration.split(":", 1)[0].strip().lower()
            if name in property_names:
                continue
            kept.append(declaration)
        return f"{selector}{{{';'.join(kept)}}}"

    return re.sub(r"([^{}]+)\{([^{}]*)\}", rewrite, css)


def _remove_css_pseudo_rules_for_matching_selectors(
    css: str,
    selector_needles: tuple[str, ...],
) -> str:
    def rewrite(match: re.Match[str]) -> str:
        selector = match.group(1)
        selector_text = selector.lower()
        if (
            ("::before" in selector_text or "::after" in selector_text)
            and any(_selector_matches_needle(selector_text, needle) for needle in selector_needles)
        ):
            return ""
        return match.group(0)

    return re.sub(r"([^{}]+)\{([^{}]*)\}", rewrite, css)


def _remove_content_region_pseudo_rules(css: str) -> str:
    content_compound = re.compile(
        r"(?:\.poster-(?:section|column|body-region)|"
        r"\[data-style-role\s*=\s*['\"]?(?:section|column|body-region)['\"]?\]|"
        r"^section(?:[.#\[].*)?$)",
        flags=re.I,
    )

    def rewrite(match: re.Match[str]) -> str:
        selector_text = match.group(1)
        kept: list[str] = []
        for selector in selector_text.split(","):
            pseudo = re.search(r"::(?:before|after)", selector, flags=re.I)
            if not pseudo:
                kept.append(selector)
                continue
            prefix = selector[:pseudo.start()].strip()
            final_compound = re.split(r"[\s>+~]+", prefix)[-1]
            if not content_compound.search(final_compound):
                kept.append(selector)
        if not kept:
            return ""
        return f"{','.join(kept)}{{{match.group(2)}}}"

    return re.sub(r"([^{}]+)\{([^{}]*)\}", rewrite, css)


def _strip_unsafe_generated_content(css: str) -> str:
    def rewrite(match: re.Match[str]) -> str:
        value = match.group(3).strip().lower()
        if value in {"''", '""', "none", "normal"}:
            return match.group(0)
        return match.group(1)

    return re.sub(
        r"(^|[;{])(\s*content\s*:\s*)([^;}{]+)(;?)",
        rewrite,
        css,
        flags=re.I | re.M,
    )


def _selector_matches_needle(selector: str, needle: str) -> bool:
    if needle.lower() != "table":
        return needle.lower() in selector
    return bool(re.search(r"(?:^|[\s>+~,])table(?:$|[\s>+~,.#:\[])", selector, flags=re.I))


def _nearest_allowed_hex(color: str, allowed: set[str]) -> str:
    rgb = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
    return min(
        allowed,
        key=lambda candidate: sum(
            (rgb[channel] - int(candidate[1 + channel * 2:3 + channel * 2], 16)) ** 2
            for channel in range(3)
        ),
    )


def _choice(value: Any, allowed: set[str], fallback: str) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    return text if text in allowed else fallback


def _bounded_int(value: Any, lower: int, upper: int, fallback: int) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return fallback
    return max(lower, min(upper, number))


def _hex(value: Any, fallback: str) -> str:
    text = str(value or "").strip().upper()
    return text if re.fullmatch(r"#[0-9A-F]{6}", text) else fallback


def _readable_color(candidate: str, background: str, *, minimum: float) -> str:
    if _contrast_ratio(candidate, background) >= minimum:
        return candidate
    black = "#111111"
    white = "#FFFFFF"
    return black if _contrast_ratio(black, background) >= _contrast_ratio(white, background) else white


def _contrast_ratio(a: str, b: str) -> float:
    lighter, darker = sorted((_relative_luminance(a), _relative_luminance(b)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _relative_luminance(color: str) -> float:
    channels = [int(color[index:index + 2], 16) / 255.0 for index in (1, 3, 5)]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _render_and_measure_reference_blueprint(
    blueprint_path: Path,
    preview_path: Path,
    *,
    expected_region_ids: list[str],
    style_contract: dict[str, Any] | None = None,
    expected_canvas: dict[str, Any] | None = None,
    cancellation_check: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    _run_reference_style_cancellation_check(
        cancellation_check,
        "reference_style.browser.start",
    )
    chrome_hidden_path = preview_path.with_name(f".{preview_path.stem}.chrome-hidden.png")
    chrome_hidden_written = False
    style_snapshot: dict[str, Any] = {}
    content_boxes: list[dict[str, Any]] = []
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise ReferenceStyleAgentError(
            f"Playwright is required to verify the sanitized reference blueprint: {exc}"
        ) from exc

    try:
        with sync_playwright() as playwright, ExitStack() as browser_resources:
            _run_reference_style_cancellation_check(
                cancellation_check,
                "reference_style.browser.before_launch",
            )
            try:
                browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            except Exception:
                _run_reference_style_cancellation_check(
                    cancellation_check,
                    "reference_style.browser.before_fallback_launch",
                )
                browser = playwright.chromium.launch(
                    channel="chrome",
                    headless=True,
                    args=["--no-sandbox"],
                )
            browser_resources.callback(_close_reference_style_browser_resource, browser)
            _run_reference_style_cancellation_check(
                cancellation_check,
                "reference_style.browser.after_launch",
            )
            canvas = _reference_canvas_contract({"canvas_contract": expected_canvas or {}})
            _run_reference_style_cancellation_check(
                cancellation_check,
                "reference_style.browser.before_context_create",
            )
            context = browser.new_context(
                java_script_enabled=False,
                viewport={"width": int(canvas["w_px"]), "height": int(canvas["h_px"])},
                device_scale_factor=1,
            )
            browser_resources.callback(_close_reference_style_browser_resource, context)
            _run_reference_style_cancellation_check(
                cancellation_check,
                "reference_style.browser.before_page_create",
            )
            page = context.new_page()
            _run_reference_style_cancellation_check(
                cancellation_check,
                "reference_style.browser.before_page_load",
            )
            page.goto(blueprint_path.resolve().as_uri(), wait_until="load", timeout=30_000)
            _run_reference_style_cancellation_check(
                cancellation_check,
                "reference_style.browser.after_page_load",
            )
            root = page.locator(".reference-style-blueprint").first
            if root.count() != 1:
                raise ReferenceStyleAgentError("sanitized reference blueprint has no unique root")
            _run_reference_style_cancellation_check(
                cancellation_check,
                "reference_style.browser.before_preview_write",
            )
            root.screenshot(path=str(preview_path), animations="disabled", timeout=30_000)
            _run_reference_style_cancellation_check(
                cancellation_check,
                "reference_style.browser.after_preview_write",
            )
            chrome_layer = root.locator('[data-style-role="chrome-layer"]').first
            if chrome_layer.count() == 1:
                previous_visibility = chrome_layer.evaluate("el => el.style.visibility")
                chrome_layer.evaluate("el => { el.style.visibility = 'hidden'; }")
                _run_reference_style_cancellation_check(
                    cancellation_check,
                    "reference_style.browser.before_chrome_preview_write",
                )
                root.screenshot(path=str(chrome_hidden_path), animations="disabled", timeout=30_000)
                _run_reference_style_cancellation_check(
                    cancellation_check,
                    "reference_style.browser.after_chrome_preview_write",
                )
                chrome_layer.evaluate(
                    "(el, previous) => { el.style.visibility = previous; }",
                    previous_visibility,
                )
                chrome_hidden_written = True
            measured = root.evaluate(
                """root => {
                  const rr = root.getBoundingClientRect();
                  return Array.from(root.querySelectorAll('[data-style-role="body-region"], [data-style-role="column"]')).map((el, index) => {
                    const r = el.getBoundingClientRect();
                    return {
                      region_id: el.getAttribute('data-region-id') || `region_${index + 1}`,
                      region_role: el.getAttribute('data-region-role') || 'column',
                      section_count: Array.from(el.querySelectorAll('[data-style-role="section"]'))
                        .filter(section => section.closest('[data-style-role="body-region"], [data-style-role="column"]') === el)
                        .filter(section => !section.parentElement.closest('[data-style-role="section"]')).length,
                      x_pct: ((r.left - rr.left) / rr.width) * 100,
                      y_pct: ((r.top - rr.top) / rr.height) * 100,
                      w_pct: (r.width / rr.width) * 100,
                      h_pct: (r.height / rr.height) * 100
                    };
                  });
                }"""
            )
            content_boxes = root.evaluate(
                r"""root => {
                  const rr = root.getBoundingClientRect();
                  const pattern = /\{\{(?:SECTION_TITLE|TARGET_PAPER_CONTENT|TARGET_PAPER_FIGURE|TARGET_PAPER_TABLE)\}\}/;
                  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                  const elements = new Set();
                  while (walker.nextNode()) {
                    const node = walker.currentNode;
                    if (!pattern.test(node.nodeValue || '')) continue;
                    const parent = node.parentElement;
                    if (!parent || !parent.closest('[data-style-role="body-region"], [data-style-role="column"]')) continue;
                    elements.add(parent);
                  }
                  return Array.from(elements).map((el, index) => {
                    const r = el.getBoundingClientRect();
                    return {
                      region_id: `content_${index + 1}`,
                      x_pct: ((r.left - rr.left) / rr.width) * 100,
                      y_pct: ((r.top - rr.top) / rr.height) * 100,
                      w_pct: (r.width / rr.width) * 100,
                      h_pct: (r.height / rr.height) * 100
                    };
                  }).filter(box => box.w_pct > 0 && box.h_pct > 0);
                }"""
            )
            if style_contract is not None:
                style_snapshot = root.evaluate(
                    """root => {
                      const effectiveBackground = el => {
                        let current = el;
                        while (current) {
                          const value = getComputedStyle(current).backgroundColor;
                          if (value && value !== 'rgba(0, 0, 0, 0)' && value !== 'transparent') return value;
                          current = current.parentElement;
                        }
                        return 'rgba(0, 0, 0, 0)';
                      };
                      const boxStyle = el => {
                        if (!el) return {};
                        const cs = getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return {
                          background_color: effectiveBackground(el),
                          font_family: cs.fontFamily,
                          border_top_width_px: parseFloat(cs.borderTopWidth) || 0,
                          border_right_width_px: parseFloat(cs.borderRightWidth) || 0,
                          border_bottom_width_px: parseFloat(cs.borderBottomWidth) || 0,
                          border_left_width_px: parseFloat(cs.borderLeftWidth) || 0,
                          border_top_color: cs.borderTopColor,
                          border_bottom_color: cs.borderBottomColor,
                          border_radius_px: parseFloat(cs.borderTopLeftRadius) || 0,
                          height_px: rect.height
                        };
                      };
                      const header = root.querySelector('[data-style-role="identity-header"]');
                      const title = header ? header.querySelector('h1, [data-style-role="title"]') || header : null;
                      const heading = root.querySelector('[data-style-role="section-heading"]');
                      const body = root.querySelector('[data-style-role="section"]');
                      return {header: boxStyle(header), title: boxStyle(title), heading: boxStyle(heading), body: boxStyle(body)};
                    }"""
                )
            _run_reference_style_cancellation_check(
                cancellation_check,
                "reference_style.browser.before_close",
            )
    except ReferenceStyleAgentError:
        raise
    except Exception as exc:
        raise ReferenceStyleAgentError(
            f"could not render sanitized reference blueprint: {type(exc).__name__}: {exc}"
        ) from exc

    if not isinstance(measured, list) or len(measured) != len(expected_region_ids):
        raise ReferenceStyleAgentError(
            "sanitized reference blueprint region measurement count does not match the contract"
        )
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(measured):
        if not isinstance(item, dict):
            raise ReferenceStyleAgentError("sanitized reference blueprint returned invalid region geometry")
        try:
            x = float(item.get("x_pct"))
            y = float(item.get("y_pct"))
            width = float(item.get("w_pct"))
            height = float(item.get("h_pct"))
        except (TypeError, ValueError) as exc:
            raise ReferenceStyleAgentError(
                "sanitized reference blueprint returned non-numeric region geometry"
            ) from exc
        if width < 2 or height < 2 or x < -0.25 or y < -0.25 or x + width > 100.25 or y + height > 100.25:
            raise ReferenceStyleAgentError(
                f"sanitized reference blueprint region {index + 1} falls outside the poster canvas"
            )
        normalized.append({
            "region_id": str(item.get("region_id") or f"region_{index + 1}"),
            "region_role": str(item.get("region_role") or "column"),
            "section_count": int(item.get("section_count") or 0),
            "x_pct": round(x, 3),
            "y_pct": round(y, 3),
            "w_pct": round(width, 3),
            "h_pct": round(height, 3),
        })
    if [str(item["region_id"]) for item in normalized] != expected_region_ids:
        raise ReferenceStyleAgentError(
            "sanitized reference blueprint measured region ids do not match the contract"
        )
    for left_index, left in enumerate(normalized):
        for right in normalized[left_index + 1:]:
            overlap_w = max(
                0.0,
                min(left["x_pct"] + left["w_pct"], right["x_pct"] + right["w_pct"])
                - max(left["x_pct"], right["x_pct"]),
            )
            overlap_h = max(
                0.0,
                min(left["y_pct"] + left["h_pct"], right["y_pct"] + right["h_pct"])
                - max(left["y_pct"], right["y_pct"]),
            )
            overlap_area = overlap_w * overlap_h
            if overlap_area > 0.01:
                _run_reference_style_cancellation_check(
                    cancellation_check,
                    "reference_style.browser.before_chrome_cleanup",
                )
                chrome_hidden_path.unlink(missing_ok=True)
                raise ReferenceStyleAgentError(
                    "sanitized reference blueprint body regions overlap; ornamental chrome must not own content geometry"
                )
    if chrome_hidden_written:
        crossing_ratio = _chrome_crossing_ratio(
            preview_path,
            chrome_hidden_path,
            content_boxes,
            inset_px=6,
        )
        _run_reference_style_cancellation_check(
            cancellation_check,
            "reference_style.browser.before_chrome_cleanup",
        )
        chrome_hidden_path.unlink(missing_ok=True)
        if crossing_ratio > 0.0005:
            raise ReferenceStyleAgentError(
                "reference blueprint chrome visibly crosses body-region interiors "
                f"(changed_pixel_ratio={crossing_ratio:.6f})"
            )
    if style_contract is not None:
        _validate_rendered_style_tokens(style_snapshot, style_contract)
    _run_reference_style_cancellation_check(
        cancellation_check,
        "reference_style.browser.before_return",
    )
    return normalized


def _close_reference_style_browser_resource(resource: Any) -> None:
    try:
        resource.close()
    except Exception:
        pass


def _chrome_crossing_ratio(
    visible_path: Path,
    hidden_path: Path,
    regions: list[dict[str, Any]],
    *,
    inset_px: int,
) -> float:
    with Image.open(visible_path).convert("RGB") as visible, Image.open(hidden_path).convert("RGB") as hidden:
        diff = ImageChops.difference(visible, hidden).convert("L")
        changed = 0
        sampled = 0
        for region in regions:
            left = int(round(float(region["x_pct"]) * visible.width / 100)) + inset_px
            top = int(round(float(region["y_pct"]) * visible.height / 100)) + inset_px
            right = int(round((float(region["x_pct"]) + float(region["w_pct"])) * visible.width / 100)) - inset_px
            bottom = int(round((float(region["y_pct"]) + float(region["h_pct"])) * visible.height / 100)) - inset_px
            if right <= left or bottom <= top:
                continue
            crop = diff.crop((left, top, right, bottom))
            histogram = crop.histogram()
            area = crop.width * crop.height
            sampled += area
            changed += area - histogram[0]
        return round(changed / max(1, sampled), 8)


def _validate_rendered_style_tokens(
    snapshot: dict[str, Any],
    contract: dict[str, Any],
) -> None:
    tokens = contract.get("style_tokens") if isinstance(contract.get("style_tokens"), dict) else {}
    colors = (contract.get("color_system") or {}).get("roles") or {}
    header_token = tokens.get("header_treatment") or {}
    heading_token = tokens.get("section_heading_treatment") or {}
    typography = tokens.get("typography_style") or {}
    header = snapshot.get("header") if isinstance(snapshot.get("header"), dict) else {}
    heading = snapshot.get("heading") if isinstance(snapshot.get("heading"), dict) else {}
    title = snapshot.get("title") if isinstance(snapshot.get("title"), dict) else {}
    body = snapshot.get("body") if isinstance(snapshot.get("body"), dict) else {}

    expected_header_bg = str(colors.get(str(header_token.get("background_role") or "background")) or "")
    if _css_color_to_hex(header.get("background_color")) != expected_header_bg.upper():
        raise ReferenceStyleAgentError(
            "rendered identity-header background does not match header_treatment.background_role"
        )
    placement = str(header_token.get("rule_placement") or "none")
    expected_rule_width = float(header_token.get("rule_width_px") or 0)
    top_width = float(header.get("border_top_width_px") or 0)
    bottom_width = float(header.get("border_bottom_width_px") or 0)
    if placement == "none" and (top_width > 0.5 or bottom_width > 0.5):
        raise ReferenceStyleAgentError("rendered identity header has an undeclared top/bottom rule")
    if placement == "top" and abs(top_width - expected_rule_width) > 1.0:
        raise ReferenceStyleAgentError("rendered identity-header top rule width does not match analysis")
    if placement == "bottom" and abs(bottom_width - expected_rule_width) > 1.0:
        raise ReferenceStyleAgentError("rendered identity-header bottom rule width does not match analysis")

    heading_mode = str(heading_token.get("mode") or "")
    heading_widths = [
        float(heading.get(f"border_{side}_width_px") or 0)
        for side in ("top", "right", "bottom", "left")
    ]
    if heading_mode in {"filled_band", "outlined_band"}:
        expected_fill = str(colors.get(str(heading_token.get("fill_role") or "primary")) or "")
        if _css_color_to_hex(heading.get("background_color")) != expected_fill.upper():
            raise ReferenceStyleAgentError("rendered section-heading fill does not match analysis")
    if heading_mode == "outlined_band":
        expected_border = float(heading_token.get("border_width_px") or 0)
        if min(heading_widths) + 1.0 < expected_border:
            raise ReferenceStyleAgentError("rendered outlined section heading is missing its full border")
        radius = float(heading.get("border_radius_px") or 0)
        height = float(heading.get("height_px") or 0)
        corner = str(heading_token.get("corner_style") or "square")
        if corner == "square" and radius > 3:
            raise ReferenceStyleAgentError("rendered section heading is rounded but analysis says square")
        if corner == "rounded" and radius < 4:
            raise ReferenceStyleAgentError("rendered section heading lacks the declared rounded corners")
        if corner == "capsule" and (height <= 0 or radius < height * 0.3):
            raise ReferenceStyleAgentError("rendered section heading lacks the declared capsule corners")
    elif heading_mode == "underline":
        expected_width = float(heading_token.get("rule_width_px") or 0)
        if abs(float(heading.get("border_bottom_width_px") or 0) - expected_width) > 1.0:
            raise ReferenceStyleAgentError("rendered section-heading underline width does not match analysis")
    elif heading_mode == "text_only" and max(heading_widths) > 0.5:
        raise ReferenceStyleAgentError("rendered text-only section heading has an undeclared border")

    display_category = str(typography.get("display_family_category") or "")
    body_category = str(typography.get("body_family_category") or "")
    for label, rendered, expected in (
        ("title", title.get("font_family"), display_category),
        ("section heading", heading.get("font_family"), display_category),
        ("body", body.get("font_family"), body_category),
    ):
        if not _font_family_matches_category(str(rendered or ""), expected):
            raise ReferenceStyleAgentError(
                f"rendered {label} font family does not match typography_style.{expected or 'unknown'}"
            )


def _css_color_to_hex(value: Any) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", text):
        return text.upper()
    match = re.match(r"rgba?\(\s*(\d+)\D+(\d+)\D+(\d+)", text)
    if not match:
        return ""
    return "#" + "".join(f"{max(0, min(255, int(item))):02X}" for item in match.groups())


def _font_family_matches_category(value: str, category: str) -> bool:
    normalized = value.lower()
    if category == "serif":
        return any(name in normalized for name in ("times", "georgia", "serif")) and "sans-serif" not in normalized
    if category == "sans_serif":
        return any(name in normalized for name in ("arial", "helvetica", "sans-serif", "sans serif"))
    return False


def _valid_reference_style_review(value: dict[str, Any], blueprint_path: Path) -> bool:
    return bool(
        isinstance(value, dict)
        and str(value.get("status") or "") == "ok"
        and value.get("rendered_blueprint_inspected") is True
        and value.get("header_matches_reference") is True
        and value.get("body_region_geometry_matches_reference") is True
        and value.get("chrome_avoids_content") is True
        and str(value.get("blueprint_sha256") or "") == sha256_file(blueprint_path)
    )


def _valid_cached_reference_style_contract(
    contract: dict[str, Any],
    *,
    blueprint_path: Path,
    preview_path: Path,
) -> bool:
    if not blueprint_path.exists() or not preview_path.exists():
        return False
    blueprint = contract.get("blueprint") if isinstance(contract.get("blueprint"), dict) else {}
    review = contract.get("blueprint_review") if isinstance(contract.get("blueprint_review"), dict) else {}
    actual_sha = sha256_file(blueprint_path)
    return bool(
        str(blueprint.get("sha256") or "") == actual_sha
        and str(blueprint.get("preview_path") or "") == preview_path.name
        and str(blueprint.get("preview_sha256") or "") == sha256_file(preview_path)
        and (blueprint_path.parent / str(blueprint.get("raw_preview_path") or "")).is_file()
        and str(blueprint.get("raw_preview_sha256") or "")
        == sha256_file(blueprint_path.parent / str(blueprint.get("raw_preview_path") or ""))
        and str(review.get("sanitized_blueprint_sha256") or "") == actual_sha
        and review.get("sanitized_blueprint_rendered") is True
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
