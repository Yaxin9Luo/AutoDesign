"""External code-editor adapter for multi-turn paper-poster revisions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from bs4 import BeautifulSoup, Comment

from ..config import harness_subprocess_env
from ..run_control import CancellationToken
from ..skills.registry import SkillRegistry
from ..util.io import atomic_write_json, sha256_file
from ..util.logging import log
from .external_author_process import (
    ExternalAuthorProcessRequest,
    run_external_author_process,
)


_CONTEXT_FILES = (
    "paper_memory.md",
    "paper_memory.json",
    "paper_memory_dossier.md",
    "paper_memory_dossier.json",
    "paper_visual_provenance.json",
    "poster_content_brief.json",
    "poster_plan_contract.json",
    "paper_visual_storyboard.json",
    "author_input_manifest.json",
    "academic_identity_assets.json",
)
_CONTEXT_DIRS = ("layers", "paper_evidence_packs", "academic_identity_assets")
_SKILL_RELATIVE_PATH = Path("poster") / "paper_poster_revision" / "SKILL.md"
_DIRECT_SKILL_ID = "poster.paper_poster_revision"
_REVISION_BASELINE_HTML_FILES = frozenset(
    {"current_poster.html", "parent_poster.html"}
)
_PRESERVE_WHITESPACE_TAGS = frozenset({"pre", "style", "textarea"})


@dataclass(frozen=True)
class CodeEditorAttemptRecord:
    attempt: int
    attempt_dir: str
    invocation: dict[str, Any]
    validation: dict[str, Any]


@dataclass(frozen=True)
class CodeEditorResult:
    attempt_dir: Path
    poster_path: Path
    attempts: list[CodeEditorAttemptRecord] = field(default_factory=list)
    validation_summary: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _InvocationResult:
    status: str
    reason: str
    returncode: int | None = None
    timed_out: bool = False
    elapsed_s: float = 0.0


class CodeEditorError(RuntimeError):
    def __init__(
        self,
        reason: str,
        message: str,
        *,
        attempts: list[CodeEditorAttemptRecord] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.attempts = attempts or []


class ExternalCodeEditor:
    """Run a local coding-agent command to edit an existing poster.html."""

    def __init__(self, settings: Any):
        self.settings = settings

    def run(
        self,
        *,
        source_poster_path: Path,
        source_final_dir: Path,
        run_dir: Path,
        parent_run_id: str,
        instruction: str,
        conversation_history: list[dict[str, Any]],
        context_run_dirs: list[Path],
        required_color_system: dict[str, Any],
        selection_context: dict[str, Any] | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> CodeEditorResult:
        token = (
            cancellation_token
            if cancellation_token is not None
            else CancellationToken.never(run_dir.name)
        )
        token.raise_if_cancelled("code_editor.before_start")
        command = str(getattr(self.settings, "code_editor_cmd", "") or "").strip()
        harness = str(getattr(self.settings, "code_editor_harness", "custom") or "custom").strip()
        timeout_s = max(1, int(getattr(self.settings, "code_editor_timeout_s", 600) or 600))
        max_attempts = max(1, int(getattr(self.settings, "code_editor_max_attempts", 2) or 2))
        if not command:
            raise CodeEditorError(
                "missing_code_editor_cmd",
                "external code editor command is not configured",
            )

        token.raise_if_cancelled("code_editor.before_workspace")
        editor_dir = run_dir / "code_editor"
        editor_dir.mkdir(parents=True, exist_ok=True)
        token.raise_if_cancelled("code_editor.after_workspace")
        log(
            "code_editor.start",
            run_id=run_dir.name,
            parent_run_id=parent_run_id,
            harness=harness or "custom",
            max_attempts=max_attempts,
            timeout_s=timeout_s,
        )

        attempts: list[CodeEditorAttemptRecord] = []
        repair_feedback: dict[str, Any] | None = None
        current_input = source_poster_path
        for attempt_index in range(1, max_attempts + 1):
            token.raise_if_cancelled("code_editor.attempt.before_workspace")
            attempt_dir = editor_dir / f"attempt_{attempt_index:02d}"
            if attempt_dir.exists():
                shutil.rmtree(attempt_dir)
            attempt_dir.mkdir(parents=True, exist_ok=True)
            token.raise_if_cancelled("code_editor.attempt.after_workspace")
            log(
                "code_editor.attempt_start",
                run_id=run_dir.name,
                attempt=attempt_index,
                max_attempts=max_attempts,
                attempt_dir=str(attempt_dir),
                repair=repair_feedback is not None,
            )

            token.raise_if_cancelled("code_editor.attempt.before_staging")
            self._stage_inputs(
                attempt_dir=attempt_dir,
                source_poster_path=current_input,
                parent_poster_path=source_poster_path,
                source_final_dir=source_final_dir,
                context_run_dirs=context_run_dirs,
                parent_run_id=parent_run_id,
                instruction=instruction,
                conversation_history=conversation_history,
                required_color_system=required_color_system,
                selection_context=selection_context,
                repair_feedback=repair_feedback,
            )
            token.raise_if_cancelled("code_editor.attempt.after_staging")
            token.raise_if_cancelled("code_editor.attempt.before_prompt")
            prompt = self._build_prompt(
                attempt_dir=attempt_dir,
                parent_run_id=parent_run_id,
                instruction=instruction,
                conversation_history=conversation_history,
                required_color_system=required_color_system,
                selection_context=selection_context,
                repair_feedback=repair_feedback,
            )
            token.raise_if_cancelled("code_editor.attempt.after_prompt")
            token.raise_if_cancelled("code_editor.attempt.before_prompt_write")
            (attempt_dir / "edit_prompt.md").write_text(prompt, encoding="utf-8")
            token.raise_if_cancelled("code_editor.attempt.after_prompt_write")
            staged_file_hashes = _snapshot_staged_file_hashes(attempt_dir)
            token.raise_if_cancelled("code_editor.attempt.before_invocation")
            invocation = self._invoke_command(
                command,
                prompt=prompt,
                attempt_dir=attempt_dir,
                timeout_s=timeout_s,
                run_dir=run_dir,
                cancellation_token=token,
            )
            token.raise_if_cancelled("code_editor.attempt.after_invocation")
            _log_code_editor_agent_output(
                attempt_dir=attempt_dir,
                run_id=run_dir.name,
                attempt_index=attempt_index,
                max_attempts=max_attempts,
                invocation=asdict(invocation),
            )
            poster_path = attempt_dir / "poster.html"
            token.raise_if_cancelled("code_editor.attempt.before_validation")
            validation = self._validate_output(
                attempt_dir,
                poster_path,
                instruction=instruction,
                selection_context=selection_context,
                staged_file_hashes=staged_file_hashes,
                required_color_system=required_color_system,
            )
            token.raise_if_cancelled("code_editor.attempt.after_validation")
            record = CodeEditorAttemptRecord(
                attempt=attempt_index,
                attempt_dir=str(attempt_dir),
                invocation=asdict(invocation),
                validation=validation,
            )
            token.raise_if_cancelled("code_editor.attempt.before_attempt_record")
            attempts.append(record)
            token.raise_if_cancelled("code_editor.attempt.after_attempt_record")
            token.raise_if_cancelled("code_editor.attempt.before_attempt_record_write")
            atomic_write_json(
                attempt_dir / "code_editor_attempt_result.json",
                {
                    "attempt": attempt_index,
                    "invocation": asdict(invocation),
                    "validation": validation,
                },
            )
            token.raise_if_cancelled("code_editor.attempt.after_attempt_record_write")
            if invocation.status == "ok" and validation.get("ok") is True:
                token.raise_if_cancelled("code_editor.attempt.before_candidate_return")
                log("code_editor.attempt_ok", run_id=run_dir.name, attempt=attempt_index)
                return CodeEditorResult(
                    attempt_dir=attempt_dir,
                    poster_path=poster_path,
                    attempts=attempts,
                    validation_summary=validation,
                )

            current_input = poster_path if poster_path.exists() else source_poster_path
            repair_feedback = {
                "reason": invocation.reason if invocation.status != "ok" else "validation_failed",
                "invocation": asdict(invocation),
                "validation": validation,
            }
            token.raise_if_cancelled("code_editor.attempt.before_feedback_write")
            atomic_write_json(attempt_dir / "validation_feedback.json", repair_feedback)
            token.raise_if_cancelled("code_editor.attempt.after_feedback_write")
            log(
                "code_editor.attempt_rejected",
                run_id=run_dir.name,
                attempt=attempt_index,
                reason=repair_feedback["reason"],
                errors=validation.get("errors") or [],
            )

        token.raise_if_cancelled("code_editor.before_max_attempts_error")
        raise CodeEditorError(
            "code_editor_validation_failed",
            "external code editor did not produce a valid revised poster",
            attempts=attempts,
        )

    def _stage_inputs(
        self,
        *,
        attempt_dir: Path,
        source_poster_path: Path,
        parent_poster_path: Path,
        source_final_dir: Path,
        context_run_dirs: list[Path],
        parent_run_id: str,
        instruction: str,
        conversation_history: list[dict[str, Any]],
        required_color_system: dict[str, Any],
        selection_context: dict[str, Any] | None,
        repair_feedback: dict[str, Any] | None,
    ) -> None:
        shutil.copy2(source_poster_path, attempt_dir / "current_poster.html")
        if parent_poster_path != source_poster_path:
            shutil.copy2(parent_poster_path, attempt_dir / "parent_poster.html")

        if source_final_dir.exists():
            for child in source_final_dir.iterdir():
                if child.is_dir():
                    shutil.copytree(child, attempt_dir / child.name, dirs_exist_ok=True)

        staged_context: list[str] = []
        seen_files: set[str] = set()
        for context_dir in context_run_dirs:
            if not context_dir.exists():
                continue
            for name in _CONTEXT_FILES:
                src = _find_context_file(context_dir, name)
                if src is None or name in seen_files:
                    continue
                shutil.copy2(src, attempt_dir / name)
                staged_context.append(name)
                seen_files.add(name)
            for name in _CONTEXT_DIRS:
                src_dir = _find_context_dir(context_dir, name)
                if src_dir is None:
                    continue
                shutil.copytree(src_dir, attempt_dir / name, dirs_exist_ok=True)
                staged_context.append(f"{name}/")

        runtime_skill = _stage_direct_skill_bundle(
            settings=self.settings,
            attempt_dir=attempt_dir,
            skill_id=_DIRECT_SKILL_ID,
            stage="repair",
        )
        if runtime_skill is None:
            raise CodeEditorError(
                "missing_paper_poster_revision_skill",
                "repo-owned paper poster revision skill is missing",
            )
        shutil.copy2(runtime_skill["skill_path"], attempt_dir / "paper_poster_revision_skill.md")

        source_manifest = {
            "parent_run_id": parent_run_id,
            "source_poster": "current_poster.html",
            "parent_poster": "parent_poster.html" if (attempt_dir / "parent_poster.html").exists() else "current_poster.html",
            "instruction": instruction,
            "conversation_history_turns": len(conversation_history),
            "context_files": sorted(set(staged_context)),
            "runtime_skill": runtime_skill["catalog"],
            "has_selection_context": bool(selection_context),
            "repair": repair_feedback is not None,
            "palette_id": str(required_color_system.get("palette_id") or ""),
        }
        if selection_context:
            atomic_write_json(attempt_dir / "selection_context.json", selection_context)
            source_manifest["selection_context_summary"] = _selection_context_summary(selection_context)
        atomic_write_json(attempt_dir / "source_manifest.json", source_manifest)
        atomic_write_json(attempt_dir / "conversation_history.json", conversation_history)
        atomic_write_json(
            attempt_dir / "required_color_system.json",
            required_color_system,
        )
        if repair_feedback is not None:
            atomic_write_json(attempt_dir / "validation_feedback.json", repair_feedback)

    def _build_prompt(
        self,
        *,
        attempt_dir: Path,
        parent_run_id: str,
        instruction: str,
        conversation_history: list[dict[str, Any]],
        required_color_system: dict[str, Any],
        selection_context: dict[str, Any] | None,
        repair_feedback: dict[str, Any] | None,
    ) -> str:
        context_files = [
            p.name
            for p in attempt_dir.iterdir()
            if p.name in _CONTEXT_FILES or p.name in _CONTEXT_DIRS
        ]
        repair_block = ""
        if repair_feedback is not None:
            repair_block = (
                "\nThis is a repair attempt. Read validation_feedback.json first, "
                "then fix the current_poster.html into a valid poster.html without "
                "restarting from scratch.\n"
            )
        history_lines = []
        for item in conversation_history[-8:]:
            role = str(item.get("role") or "").strip()
            text = str(item.get("text") or "").strip()
            if role and text:
                history_lines.append(f"- {role}: {text[:500]}")
        history_block = "\n".join(history_lines) or "- No prior chat turns staged."
        context_block = "\n".join(f"- {name}" for name in sorted(context_files)) or "- No extra paper context files were staged."
        required_palette_id = str(required_color_system.get("palette_id") or "").strip()
        selection_block = ""
        if selection_context:
            summary = _selection_context_summary(selection_context)
            selection_block = (
                "\nSelected edit area:\n"
                "- Read `selection_context.json` before editing.\n"
                f"- Selection summary: {json.dumps(summary, ensure_ascii=False)}\n"
                "- Treat the selected area as the user's intended local edit target.\n"
                "- If `selection_context.kind` is `multi`, revise all selected `items` as a coordinated local edit. Use item order, labels, block ids, selectors, rects, and excerpts to map wording like 'area 1' or 'the second selected panel'.\n"
                "- If selected items include an `instruction`, treat it as that area's specific edit note; apply the general user request across all items and each item instruction locally.\n"
                "- If `selection_context.kind` is `drawing`, interpret `drawing_paths` as the user's freehand red markup in poster document pixels; circled, crossed, underlined, or arrowed regions are strong localization hints.\n"
                "- Multi selections may include drawing items; their `drawing_paths` are localization hints for that item, not poster content to preserve.\n"
                "- Prefer changing the matching section/block/panel only; avoid unrelated global restyling or poster-wide rewrites.\n"
                "- If selector or block id cannot be found in current_poster.html, use the rect, nearby headings, text excerpt, and HTML excerpt to locate the closest matching panel.\n"
                "- If the target remains ambiguous, make the safest narrow edit and note the limitation in code_editor_done.json.\n"
            )
        return f"""You are revising an existing AutoDesign academic paper poster.

Read `runtime_skills/index.md` first, then read `paper_poster_revision_skill.md` and any listed resource only when needed.

User revision request:
{instruction.strip()}
{selection_block}

Parent run id: {parent_run_id}

Conversation context:
{history_block}

Available files:
- current_poster.html: the poster to revise
- source_manifest.json: staged source summary
- conversation_history.json: exact recent chat turns
- required_color_system.json: the required user-selected palette and exact CSS variables
{context_block}

Output contract:
- Write the revised poster to `poster.html`.
- Write `code_editor_done.json` with a short JSON summary.
- Keep the work inside this directory.
- Edit the existing HTML/CSS; do not regenerate a new unrelated poster.
- Preserve native editable text and existing data-layer/data-block ids where practical.
- Keep `data-palette-id="{required_palette_id}"` on `.paper-poster` and define every exact `--poster-*` variable from `required_color_system.json` on that root.
- Use only local staged assets or existing valid relative asset references.
- If you add a local asset, save the file under this directory and reference it with a portable relative path.
- Do not leave empty or missing image `src` attributes. Do not look up or add logos unless the user explicitly requests one in this revision.
- For an explicitly requested logo, first use an exact staged/provided file when available. Otherwise search only the organization's or conference's official website/media kit, download the exact mark into this directory, reference it with a portable relative path, and record it in `code_editor_done.json` as `fetched_identity_assets: [{{"path": "assets/example.svg", "source_url": "https://official.example/...", "source_type": "official"}}]`.
- Never invent, redraw, generate, approximate, substitute, or create a text badge/logo mark. If no official asset can be verified, preserve the header and state the limitation in `code_editor_done.json`.
- Do not use remote, file:, script, iframe, event-handler, or unsafe URLs.
- Do not invent authors, affiliations, logos, metrics, benchmarks, source figures, or paper claims.
- If the requested change cannot be grounded in the staged paper context or current poster, make the safest local layout/text edit and state the limitation in code_editor_done.json.
{repair_block}
If you need Python, use this interpreter exactly: {sys.executable}
Do not install packages, create virtual environments, or run setup commands.
"""

    def _invoke_command(
        self,
        command: str,
        *,
        prompt: str,
        attempt_dir: Path,
        timeout_s: int,
        run_dir: Path,
        cancellation_token: CancellationToken,
    ) -> _InvocationResult:
        cancellation_token.raise_if_cancelled("code_editor.invoke.before_output_reset")
        poster_path = attempt_dir / "poster.html"
        done_marker = attempt_dir / "code_editor_done.json"
        stdout_path = attempt_dir / ".code_editor_log.stdout.tmp"
        stderr_path = attempt_dir / ".code_editor_log.stderr.tmp"
        for path in (poster_path, done_marker):
            try:
                path.unlink()
            except OSError:
                pass
        cancellation_token.raise_if_cancelled("code_editor.invoke.after_output_reset")
        try:
            cmd = shlex.split(command)
        except ValueError as exc:
            return _InvocationResult(status="error", reason=f"command_parse_error: {exc}")
        if not cmd:
            return _InvocationResult(status="error", reason="empty_command")

        try:
            cancellation_token.raise_if_cancelled("code_editor.invoke.before_environment")
            env = harness_subprocess_env(
                os.environ,
                harness=str(getattr(self.settings, "code_editor_harness", "") or ""),
                api_key=getattr(self.settings, "harness_api_key", None),
            )
            author_python = (
                env.get("AUTODESIGN_AUTHOR_PYTHON", "").strip()
                or env.get("DESIGN_ANYTHING_AUTHOR_PYTHON", "").strip()
                or sys.executable
            )
            env["AUTODESIGN_AUTHOR_PYTHON"] = author_python
            env.setdefault("DESIGN_ANYTHING_AUTHOR_PYTHON", author_python)
            process_result = run_external_author_process(
                ExternalAuthorProcessRequest(
                    run_id=run_dir.name,
                    attempt=0,
                    command=cmd,
                    cwd=attempt_dir,
                    prompt=prompt,
                    timeout_s=timeout_s,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    env=env,
                    completion_requested=lambda: (
                        "done_marker"
                        if done_marker.exists() and poster_path.exists()
                        else None
                    ),
                    interruption_requested=cancellation_token.is_cancelled,
                    poll_interval_s=0.05,
                    run_dir=run_dir,
                    cancellation_token=cancellation_token,
                )
            )
            cancellation_token.raise_if_cancelled(
                "code_editor.invoke.after_process_wait"
            )
        except Exception as exc:  # noqa: BLE001
            return _InvocationResult(
                status="error",
                reason=f"command_start_error: {exc}",
            )

        timed_out = process_result.timed_out
        reason = process_result.reason
        if process_result.status == "spawn_error":
            detail = process_result.stderr.strip() or reason
            reason = f"command_start_error: {detail}"
        status = (
            "ok"
            if poster_path.exists() and done_marker.exists() and not timed_out
            else "error"
        )
        if status != "ok" and poster_path.exists() and not done_marker.exists():
            reason = "missing_done_marker"
        if status != "ok" and not poster_path.exists():
            reason = "missing_poster_html"
        return _InvocationResult(
            status=status,
            reason=reason,
            returncode=process_result.returncode,
            timed_out=timed_out,
            elapsed_s=round(process_result.elapsed_s, 3),
        )

    def _validate_output(
        self,
        attempt_dir: Path,
        poster_path: Path,
        *,
        instruction: str = "",
        selection_context: dict[str, Any] | None = None,
        staged_file_hashes: dict[str, str] | None = None,
        required_color_system: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        if not poster_path.exists():
            return {"ok": False, "errors": ["poster.html missing"], "warnings": warnings}
        try:
            html = poster_path.read_text(encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "errors": [f"poster.html unreadable: {exc}"], "warnings": warnings}
        if len(html.strip()) < 200:
            errors.append("poster.html is too small")
        lowered = html.lower()
        if "<script" in lowered:
            errors.append("script tags are not allowed")
        if "<iframe" in lowered:
            errors.append("iframes are not allowed")
        if " onload=" in lowered or " onclick=" in lowered or " onerror=" in lowered:
            errors.append("inline event handlers are not allowed")
        if ".paper-poster" not in html and "paper-poster" not in html:
            errors.append("paper-poster root marker not found")
        baseline_hashes = staged_file_hashes or {}
        baseline_name = (
            "parent_poster.html"
            if baseline_hashes.get("parent_poster.html")
            else "current_poster.html"
        )
        baseline_hash = baseline_hashes.get(baseline_name)
        if baseline_hash and _revision_html_fingerprint(html) == baseline_hash:
            errors.append("poster.html is unchanged from the parent poster")

        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"HTML parse failed: {exc}")
            soup = BeautifulSoup("", "html.parser")
        for tag in soup.find_all("img"):
            raw = str(tag.get("src") or "").strip()
            if not raw:
                alt = str(tag.get("alt") or "").strip()
                suffix = f" ({alt[:80]})" if alt else ""
                errors.append(f"image tag missing src{suffix}")
                continue
            _validate_asset_ref(raw, attempt_dir, errors)
        for tag in soup.find_all("source"):
            raw = str(tag.get("src") or "").strip()
            if raw:
                _validate_asset_ref(raw, attempt_dir, errors)
        for tag in soup.find_all(attrs={"srcset": True}):
            for raw in _split_srcset(str(tag.get("srcset") or "")):
                _validate_asset_ref(raw, attempt_dir, errors)
        for raw in re.findall(r"url\(([^)]+)\)", html, flags=re.IGNORECASE):
            cleaned = raw.strip().strip("'\"")
            if cleaned:
                _validate_asset_ref(cleaned, attempt_dir, errors)

        if required_color_system:
            from ..tools.propose_paper_poster_html import authored_palette_diagnostics

            palette_diagnostics = authored_palette_diagnostics(
                html,
                "",
                required_color_system,
                require_selected=True,
            )
            for diagnostic in palette_diagnostics:
                issue_id = str(diagnostic.get("issue_id") or "")
                if issue_id == "paper_poster_html_required_palette_mismatch":
                    errors.append(
                        "Required palette mismatch: expected "
                        f"{diagnostic.get('required_palette_id') or '(missing)'}, got "
                        f"{diagnostic.get('actual_palette_id') or '(missing)'}."
                    )
                elif issue_id == "paper_poster_html_palette_id_missing":
                    errors.append("Required palette id is missing from .paper-poster.")
                elif issue_id == "paper_poster_html_palette_css_variable_mismatch":
                    errors.append(
                        "Required palette CSS variable mismatch: "
                        f"{json.dumps(diagnostic.get('mismatches') or [], ensure_ascii=False)}"
                    )
                elif issue_id == "paper_poster_html_palette_extra_authored_hex":
                    shell_extra_colors = (
                        diagnostic.get("shell_extra_colors")
                        or diagnostic.get("shell_extra_hexes")
                        or []
                    )
                    source_visual_extra_colors = (
                        diagnostic.get("source_visual_extra_colors")
                        or diagnostic.get("source_visual_extra_hexes")
                        or []
                    )
                    if shell_extra_colors:
                        errors.append(
                            "Foreign shell/UI palette colors are not allowed: "
                            f"{', '.join(str(item) for item in shell_extra_colors)}."
                        )
                    if source_visual_extra_colors:
                        warnings.append(
                            "Source visual colors retained outside the selected poster palette: "
                            f"{', '.join(str(item) for item in source_visual_extra_colors)}."
                        )
        return {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "bytes": len(html.encode("utf-8")),
        }


def _find_context_file(context_dir: Path, name: str) -> Path | None:
    direct = context_dir / name
    if direct.exists() and direct.is_file():
        return direct
    matches = sorted(context_dir.glob(f"designer_author/attempt_*/{name}"))
    return matches[-1] if matches else None


def _stage_direct_skill_bundle(
    *,
    settings: Any,
    attempt_dir: Path,
    skill_id: str,
    stage: str,
) -> dict[str, Any] | None:
    """Stage compact direct-agent guidance without putting resource bodies in prompts."""

    skills_dir = Path(getattr(settings, "skills_dir", "") or "")
    registry = SkillRegistry.load(skills_dir)
    pack = registry.get(skill_id)
    if pack is None:
        return None
    if pack.manifest.manifest_version == 2 and not pack.verify_integrity():
        return None
    root = Path(pack.root)
    if not root.is_dir():
        return None
    manifest_path = root / "skill.json"
    skill_path = root / "SKILL.md"
    if not manifest_path.is_file() or not skill_path.is_file():
        return None
    manifest = pack.manifest.model_dump(mode="json")

    staged_dir = attempt_dir / "runtime_skills"
    staged_dir.mkdir(parents=True, exist_ok=True)
    skill_text = _compact_skill_text(pack, skill_path, stage)
    staged_skill_path = staged_dir / "SKILL.md"
    staged_skill_path.write_text(skill_text, encoding="utf-8")
    resources = _stage_skill_resources(root, staged_dir, manifest, stage)
    index_lines = [
        "# Runtime Skills",
        "",
        "Read this index first. Runtime resources are staged files, not prompt context.",
        "",
        f"## {skill_id}",
        "- `SKILL.md`: compact operating guidance for this attempt.",
    ]
    if resources:
        index_lines.extend(["", "## Resources (read on demand)"])
        index_lines.extend(
            f"- `{item['path']}`: {item['description']} {item['when_to_read']}".rstrip()
            for item in resources
        )
    (staged_dir / "index.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    return {
        "skill_path": staged_skill_path,
        "catalog": {
            "id": skill_id,
            "stage": stage,
            "index": "runtime_skills/index.md",
            "resources": resources,
        },
    }


def _compact_skill_text(pack: Any, skill_path: Path, stage: str) -> str:
    render = getattr(pack, "render", None)
    if callable(render):
        rendered = str(render(stage) or "").strip()
        if rendered:
            return rendered + "\n"
    return skill_path.read_text(encoding="utf-8")


def _stage_skill_resources(
    root: Path,
    staged_dir: Path,
    manifest: dict[str, Any],
    stage: str,
) -> list[dict[str, str]]:
    staged: list[dict[str, str]] = []
    for resource in manifest.get("resources") or []:
        if not isinstance(resource, dict):
            continue
        stages = resource.get("stages") or []
        if stages and stage not in stages:
            continue
        relative = str(resource.get("path") or "").strip()
        if not relative:
            continue
        source = (root / relative).resolve()
        try:
            source.relative_to(root.resolve())
        except ValueError:
            continue
        if not source.is_file():
            continue
        target = staged_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        staged.append({
            "id": str(resource.get("id") or relative),
            "path": relative,
            "description": str(resource.get("description") or "Runtime resource."),
            "when_to_read": str(resource.get("when_to_read") or "Read only when needed."),
        })
    return staged


def _selection_context_summary(selection_context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(selection_context, dict):
        return None
    rect = selection_context.get("rect")
    summary: dict[str, Any] = {
        "kind": selection_context.get("kind"),
        "block_id": selection_context.get("block_id"),
        "selector": selection_context.get("selector"),
    }
    if isinstance(rect, dict):
        summary["rect"] = {
            key: rect.get(key)
            for key in ("x", "y", "w", "h")
            if isinstance(rect.get(key), (int, float))
        }
    drawing_paths = selection_context.get("drawing_paths")
    if isinstance(drawing_paths, list):
        point_count = 0
        for path in drawing_paths:
            if isinstance(path, dict) and isinstance(path.get("points"), list):
                point_count += len(path.get("points") or [])
        summary["drawing_paths"] = {
            "count": len(drawing_paths),
            "points": point_count,
        }
    items = selection_context.get("items")
    if isinstance(items, list):
        item_summaries: list[dict[str, Any]] = []
        total_drawing_paths = 0
        total_drawing_points = 0
        for idx, item in enumerate(items[:6], start=1):
            if not isinstance(item, dict):
                continue
            item_rect = item.get("rect")
            item_summary: dict[str, Any] = {
                "index": idx,
                "kind": item.get("kind"),
                "label": str(item.get("label") or "").strip()[:120],
                "block_id": item.get("block_id"),
                "selector": item.get("selector"),
            }
            if isinstance(item_rect, dict):
                item_summary["rect"] = {
                    key: item_rect.get(key)
                    for key in ("x", "y", "w", "h")
                    if isinstance(item_rect.get(key), (int, float))
                }
            item_paths = item.get("drawing_paths")
            if isinstance(item_paths, list):
                item_point_count = 0
                for path in item_paths:
                    if isinstance(path, dict) and isinstance(path.get("points"), list):
                        item_point_count += len(path.get("points") or [])
                item_summary["drawing_paths"] = {
                    "count": len(item_paths),
                    "points": item_point_count,
                }
                total_drawing_paths += len(item_paths)
                total_drawing_points += item_point_count
            item_text = str(item.get("text_excerpt") or "").strip()
            if item_text:
                item_summary["text_excerpt"] = item_text[:180]
            item_instruction = str(item.get("instruction") or "").strip()
            if item_instruction:
                item_summary["instruction"] = item_instruction[:240]
            item_summaries.append({
                k: v
                for k, v in item_summary.items()
                if v not in (None, "", [], {})
            })
        valid_item_count = len([item for item in items if isinstance(item, dict)])
        summary["item_count"] = valid_item_count
        summary["items"] = item_summaries
        if valid_item_count > len(item_summaries):
            summary["items_truncated"] = valid_item_count - len(item_summaries)
        if total_drawing_paths:
            summary["multi_drawing_paths"] = {
                "count": total_drawing_paths,
                "points": total_drawing_points,
            }
    headings = selection_context.get("nearby_headings")
    if isinstance(headings, list):
        summary["nearby_headings"] = [
            str(item).strip()[:120]
            for item in headings[:4]
            if str(item).strip()
        ]
    text = str(selection_context.get("text_excerpt") or "").strip()
    if text:
        summary["text_excerpt"] = text[:240]
    instruction = str(selection_context.get("instruction") or "").strip()
    if instruction:
        summary["instruction"] = instruction[:240]
    return {k: v for k, v in summary.items() if v not in (None, "", [], {})}


def _find_context_dir(context_dir: Path, name: str) -> Path | None:
    direct = context_dir / name
    if direct.exists() and direct.is_dir():
        return direct
    matches = sorted(context_dir.glob(f"designer_author/attempt_*/{name}"))
    return matches[-1] if matches else None


def _split_srcset(value: str) -> list[str]:
    refs: list[str] = []
    for part in value.split(","):
        first = part.strip().split(" ", 1)[0].strip()
        if first:
            refs.append(first)
    return refs


def _snapshot_staged_file_hashes(root: Path) -> dict[str, str]:
    root = root.resolve()
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            relative = path.resolve().relative_to(root).as_posix()
            if relative in _REVISION_BASELINE_HTML_FILES:
                hashes[relative] = _revision_html_fingerprint(
                    path.read_text(encoding="utf-8")
                )
            else:
                hashes[relative] = sha256_file(path)
        except (OSError, ValueError):
            continue
    return hashes


def _revision_html_fingerprint(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for comment in list(soup.find_all(string=lambda value: isinstance(value, Comment))):
        comment.extract()
    for tag in soup.find_all(True):
        classes = tag.get("class")
        if isinstance(classes, list):
            tag["class"] = sorted(str(value) for value in classes)
    for node in list(soup.find_all(string=True)):
        parent_name = str(getattr(node.parent, "name", "") or "").lower()
        if parent_name in _PRESERVE_WHITESPACE_TAGS:
            continue
        normalized = re.sub(r"\s+", " ", str(node)).strip()
        if normalized:
            node.replace_with(normalized)
        else:
            node.extract()
    canonical = soup.decode(formatter="minimal")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_asset_ref(raw: str, attempt_dir: Path, errors: list[str]) -> None:
    value = unquote(raw.strip()).split("#", 1)[0].split("?", 1)[0].strip()
    if not value or value.startswith("data:") or value.startswith("#"):
        return
    lowered = value.lower()
    if lowered.startswith(("http://", "https://", "file:", "javascript:")):
        errors.append(f"unsafe or remote asset reference: {raw}")
        return
    if value.startswith("/"):
        errors.append(f"absolute asset reference is not portable: {raw}")
        return
    target = (attempt_dir / value).resolve()
    try:
        target.relative_to(attempt_dir.resolve())
    except ValueError:
        errors.append(f"asset reference escapes attempt directory: {raw}")
        return
    if not target.exists():
        errors.append(f"asset reference missing: {raw}")


def _log_code_editor_agent_output(
    *,
    attempt_dir: Path,
    run_id: str,
    attempt_index: int,
    max_attempts: int,
    invocation: dict[str, Any],
) -> None:
    done_summary = _json_summary_excerpt(attempt_dir / "code_editor_done.json")
    stdout_excerpt = _tail_text_excerpt(attempt_dir / ".code_editor_log.stdout.tmp", limit=1800)
    stderr_excerpt = _tail_text_excerpt(attempt_dir / ".code_editor_log.stderr.tmp", limit=900)
    if not done_summary and not stdout_excerpt and not stderr_excerpt:
        return
    log(
        "code_editor.agent_output",
        run_id=run_id,
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
