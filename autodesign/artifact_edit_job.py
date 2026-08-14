"""Supervised, transactionally published artifact edits."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Literal
import uuid

from bs4 import BeautifulSoup

from .agents.atomic_artifact_promotion import publish_artifact_directory
from .apply_edits import apply_edits
from .config import Settings
from .schema import ApplyEditsResult
from .tools._deck_preview import build_deck_preview_grid
from .util.browser_render import (
    export_deck_pdf,
    screenshot_deck_slides,
    screenshot_html,
)
from .util.editable_html import ensure_editable_html_contract
from .util.io import atomic_write_json, sha256_file
from .util.math_typesetting import ensure_poster_katex_document


ArtifactEditType = Literal["poster", "deck", "landing", "video"]
_INPUT_VERSION = 1
_INPUT_FIELDS = frozenset({
    "version",
    "artifact_type",
    "source_relative_path",
    "edited_html_relative_path",
    "edits",
    "required_color_system",
    "candidate_lineage",
})
_FINAL_NAMES: dict[str, str] = {
    "poster": "poster.html",
    "deck": "deck.html",
    "landing": "index.html",
    "video": "deck.html",
}
_PROMOTION_NAMES: dict[str, str] = {
    "poster": "poster",
    "deck": "slides",
    "landing": "landing",
    "video": "video",
}
_TRUSTED_SOURCE_CONTEXT_FILES: dict[str, str] = {
    "deck": "slides_trusted_source_hashes.json",
    "landing": "landing_trusted_source_hashes.json",
    "video": "video_trusted_source_context.json",
}


class ArtifactEditJobError(RuntimeError):
    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail
        super().__init__(json.dumps(detail, ensure_ascii=False, sort_keys=True))


def run_artifact_edit_job(
    *,
    run_id: str,
    parent_run_id: str,
    input_path: Path,
    settings: Settings,
    cancellation_token: Any,
) -> dict[str, Any]:
    """Build an edited artifact in isolation and publish it transactionally."""

    cancellation_token.raise_if_cancelled("artifact_edit.load_input")
    runs_dir = (Path(settings.out_dir) / "runs").resolve()
    run_dir = _direct_run_dir(runs_dir, run_id, require_existing=True)
    parent_dir = _direct_run_dir(runs_dir, parent_run_id, require_existing=True)
    payload = _load_input(input_path, run_dir=run_dir)
    artifact_type = payload["artifact_type"]
    source_path = _resolve_run_file(
        parent_dir,
        payload["source_relative_path"],
        label="artifact edit source",
    )
    edited_html = _resolve_run_file(
        run_dir,
        payload["edited_html_relative_path"],
        label="artifact edit staged HTML",
    )
    edits = payload["edits"]
    required_color_system = payload["required_color_system"]
    source_candidate_lineage = payload["candidate_lineage"]

    work_root = run_dir / "html_first"
    if work_root.is_symlink():
        raise ArtifactEditJobError({
            "code": "invalid_artifact_edit_workdir",
            "message": "Artifact edit work directory must not be a symlink.",
        })
    work_root.mkdir(parents=True, exist_ok=True)
    if work_root.resolve() != work_root:
        raise ArtifactEditJobError({
            "code": "invalid_artifact_edit_workdir",
            "message": "Artifact edit work directory escaped its run.",
        })
    work_dir = work_root / "artifact_edit_work"
    if work_dir.is_symlink():
        raise ArtifactEditJobError({
            "code": "invalid_artifact_edit_workdir",
            "message": "Artifact edit work directory must not be a symlink.",
        })
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    promotion_name = _PROMOTION_NAMES[artifact_type]
    staging_dir = run_dir / f".{promotion_name}-final-staging-{uuid.uuid4().hex}"
    candidate_lineage: dict[str, Any] = {}
    try:
        cancellation_token.raise_if_cancelled("artifact_edit.before_render")
        if artifact_type == "poster" and _is_authored_paper_poster_html(source_path):
            edit_result = _apply_authored_paper_poster_edits(
                source_path,
                edited_html,
                settings,
                run_id,
                parent_run_id,
                edits,
                work_dir=work_dir,
                required_color_system=required_color_system,
            )
        elif artifact_type in {"deck", "landing"}:
            edit_result = _apply_authored_html_edits(
                source_path,
                edited_html,
                run_id,
                parent_run_id,
                edits,
                artifact_type=artifact_type,
                work_dir=work_dir,
            )
        elif artifact_type == "video" and source_candidate_lineage:
            edit_result = _apply_authored_video_draft_edits(
                source_path,
                edited_html,
                run_id,
                parent_run_id,
                edits,
                work_dir=work_dir,
            )
        else:
            edit_result = apply_edits(
                edited_html,
                settings=settings,
                out_dir=work_dir,
                run_id=run_id,
                cancellation_token=cancellation_token,
            )

        cancellation_token.raise_if_cancelled("artifact_edit.after_render")
        work_final = work_dir / "final"
        if not work_final.is_dir() or work_final.is_symlink():
            raise ArtifactEditJobError({
                "code": "artifact_edit_missing_final",
                "message": "Edited artifact did not produce a final directory.",
            })
        shutil.copytree(work_final, staging_dir, symlinks=False)

        if artifact_type == "poster" and required_color_system:
            _persist_palette_manifest(
                staging_dir,
                pending_manifest=work_dir / "authored_poster_edit_manifest.pending.json",
                palette_id=str(required_color_system.get("palette_id") or ""),
                required_color_system=required_color_system,
            )
            try:
                _validate_required_poster_palette_html(
                    staging_dir / "poster.html",
                    required_color_system,
                )
            except ArtifactEditJobError as exc:
                _quarantine_palette_failure(
                    run_dir,
                    staging_dir,
                    parent_run_id=parent_run_id,
                    required_color_system=required_color_system,
                    error_detail=exc.detail,
                )
                raise

        if source_candidate_lineage:
            candidate_lineage = _edited_candidate_lineage(
                source_candidate_lineage,
                parent_run_id=parent_run_id,
            )
            if artifact_type == "poster":
                _copy_poster_validation_context(
                    parent_dir,
                    run_dir,
                    cancellation_token=cancellation_token,
                )
            else:
                context_name = _TRUSTED_SOURCE_CONTEXT_FILES.get(artifact_type)
                if context_name:
                    cancellation_token.raise_if_cancelled(
                        "artifact_edit.copy_trusted_source_context"
                    )
                    context_path = parent_dir / context_name
                    if context_path.is_file():
                        shutil.copy2(context_path, run_dir / context_name)
            atomic_write_json(
                run_dir / "candidate_draft_lineage.json",
                candidate_lineage,
            )

        cancellation_token.raise_if_cancelled("artifact_edit.before_publish")
        publish_artifact_directory(
            staging_dir,
            run_dir / "final",
            artifact_name=promotion_name,
            post_publish=lambda: cancellation_token.raise_if_cancelled(
                "artifact_edit.publish"
            ),
        )
        cancellation_token.raise_if_cancelled("artifact_edit.after_publish")
        final_source = run_dir / "final" / _FINAL_NAMES[artifact_type]
        if not final_source.is_file():
            raise ArtifactEditJobError({
                "code": "artifact_edit_missing_source",
                "message": "Edited artifact final HTML is missing.",
            })
        return {
            "run_id": run_id,
            "artifact_type": artifact_type,
            "source_path": str(final_source),
            "restored_layer_ids": list(edit_result.restored_layer_ids),
            "skipped": list(edit_result.skipped),
            "candidate_lineage": candidate_lineage,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _load_input(path: Path, *, run_dir: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(run_dir) or path.is_symlink():
        raise ArtifactEditJobError({
            "code": "invalid_artifact_edit_input",
            "message": "Artifact edit input must be a regular child-run file.",
        })
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactEditJobError({
            "code": "invalid_artifact_edit_input",
            "message": f"Artifact edit input is unreadable: {exc}",
        }) from exc
    if not isinstance(payload, dict) or set(payload) != _INPUT_FIELDS:
        raise ArtifactEditJobError({
            "code": "invalid_artifact_edit_input",
            "message": "Artifact edit input fields are invalid.",
        })
    if payload.get("version") != _INPUT_VERSION:
        raise ArtifactEditJobError({
            "code": "invalid_artifact_edit_input",
            "message": "Artifact edit input version is unsupported.",
        })
    if payload.get("artifact_type") not in _FINAL_NAMES:
        raise ArtifactEditJobError({
            "code": "invalid_artifact_edit_input",
            "message": "Artifact edit type is unsupported.",
        })
    for field_name in ("source_relative_path", "edited_html_relative_path"):
        value = payload.get(field_name)
        if not isinstance(value, str) or not value:
            raise ArtifactEditJobError({
                "code": "invalid_artifact_edit_input",
                "message": f"Artifact edit {field_name} is invalid.",
            })
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise ArtifactEditJobError({
                "code": "invalid_artifact_edit_input",
                "message": f"Artifact edit {field_name} escaped its run.",
            })
    for field_name in ("edits", "required_color_system", "candidate_lineage"):
        if not isinstance(payload.get(field_name), dict):
            raise ArtifactEditJobError({
                "code": "invalid_artifact_edit_input",
                "message": f"Artifact edit {field_name} must be an object.",
            })
    return payload


def _direct_run_dir(runs_dir: Path, run_id: str, *, require_existing: bool) -> Path:
    candidate = runs_dir / run_id
    if candidate.parent != runs_dir or candidate.name != run_id or candidate.is_symlink():
        raise ArtifactEditJobError({
            "code": "invalid_run_directory",
            "message": "Artifact edit run directory is invalid.",
        })
    resolved = candidate.resolve(strict=require_existing)
    if resolved.parent != runs_dir or resolved.name != run_id:
        raise ArtifactEditJobError({
            "code": "invalid_run_directory",
            "message": "Artifact edit run directory escaped its root.",
        })
    return resolved


def _resolve_run_file(run_dir: Path, relative_value: str, *, label: str) -> Path:
    candidate = run_dir / relative_value
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(run_dir) or not resolved.is_file():
        raise ArtifactEditJobError({
            "code": "invalid_artifact_edit_path",
            "message": f"{label} is outside its run.",
        })
    return candidate


def _apply_authored_paper_poster_edits(
    source_path: Path,
    edited_html: Path,
    settings: Settings,
    run_id: str,
    parent_run_id: str,
    edits: dict[str, Any],
    *,
    work_dir: Path,
    required_color_system: dict[str, Any],
) -> ApplyEditsResult:
    final_dir = work_dir / "final"
    final_dir.mkdir(parents=True)
    for child in source_path.parent.iterdir():
        if child.is_dir():
            shutil.copytree(child, final_dir / child.name, dirs_exist_ok=True)
    html_path = final_dir / "poster.html"
    shutil.copy2(edited_html, html_path)
    ensure_poster_katex_document(
        html_path,
        Path(settings.repo_root),
        root_selector=".paper-poster",
    )
    width, height = _authored_paper_poster_size(html_path)
    preview_path = final_dir / "preview.png"
    browser_result = screenshot_html(
        html_path,
        preview_path,
        viewport_width=width,
        viewport_height=height,
        selector=".paper-poster",
        max_edge=settings.poster_preview_max_edge,
        timeout_ms=30_000,
    )
    if browser_result.warnings and (source_path.parent / "preview.png").is_file():
        shutil.copy2(source_path.parent / "preview.png", preview_path)
    restored = _html_edit_manifest_ids(edits)
    atomic_write_json(work_dir / "authored_poster_edit_manifest.pending.json", {
        "artifact_type": "poster",
        "render_mode": "authored_paper_poster_edit",
        "parent_run_id": parent_run_id,
        "palette_id": str(required_color_system.get("palette_id") or ""),
        "required_color_system": required_color_system,
        "edits": restored,
        "canvas": {"w_px": width, "h_px": height},
        "preview": {
            "backend": browser_result.backend,
            "warnings": browser_result.warnings,
            "scale": browser_result.scale,
            "width_px": browser_result.width_px,
            "height_px": browser_result.height_px,
        },
    })
    return ApplyEditsResult(
        run_id=run_id,
        run_dir=str(work_dir),
        parent_run_id=parent_run_id,
        restored_layer_ids=restored,
        skipped=[],
        artifact_type="poster",
    )


def _apply_authored_html_edits(
    source_path: Path,
    edited_html: Path,
    run_id: str,
    parent_run_id: str,
    edits: dict[str, Any],
    *,
    artifact_type: Literal["deck", "landing"],
    work_dir: Path,
) -> ApplyEditsResult:
    final_dir = work_dir / "final"
    shutil.copytree(source_path.parent, final_dir, dirs_exist_ok=True)
    output_name = "deck.html" if artifact_type == "deck" else "index.html"
    html_path = final_dir / output_name
    shutil.copy2(edited_html, html_path)
    ensure_editable_html_contract(html_path, artifact_type)
    preview_path = final_dir / "preview.png"
    preview_path.unlink(missing_ok=True)
    if artifact_type == "deck":
        shutil.copy2(html_path, final_dir / "slides.html")
        pdf_path = final_dir / "deck.pdf"
        pdf_path.unlink(missing_ok=True)
        slides_dir = final_dir / "slides"
        if slides_dir.exists():
            shutil.rmtree(slides_dir)
        render = screenshot_deck_slides(
            html_path,
            slides_dir,
            slide_w=1920,
            slide_h=1080,
        )
        slide_paths = [Path(value) for value in (render.paths or []) if Path(value).is_file()]
        if not slide_paths:
            raise ArtifactEditJobError({
                "code": "artifact_edit_render_failed",
                "message": "Edited deck renderer produced no slide images.",
            })
        build_deck_preview_grid(slide_paths, preview_path)
        export_deck_pdf(
            html_path,
            pdf_path,
            slide_w=1920,
            slide_h=1080,
            slide_pngs=slide_paths,
        )
        if not preview_path.is_file() or not pdf_path.is_file():
            raise ArtifactEditJobError({
                "code": "artifact_edit_render_failed",
                "message": "Edited deck preview or PDF was not produced.",
            })
        preview_payload = {
            "backend": render.backend,
            "warnings": render.warnings,
            "slide_image_count": len(slide_paths),
        }
    else:
        render = screenshot_html(
            html_path,
            preview_path,
            viewport_width=1440,
            viewport_height=900,
            full_page=True,
            prime_local_media=True,
            max_edge=4096,
            timeout_ms=30_000,
        )
        if not preview_path.is_file():
            raise ArtifactEditJobError({
                "code": "artifact_edit_render_failed",
                "message": "Edited landing preview was not produced.",
            })
        preview_payload = {
            "backend": render.backend,
            "warnings": render.warnings,
            "scale": render.scale,
            "width_px": render.width_px,
            "height_px": render.height_px,
        }
        card_preview_path = final_dir / "card_preview.png"
        card_preview_path.unlink(missing_ok=True)
        screenshot_html(
            html_path,
            card_preview_path,
            viewport_width=1440,
            viewport_height=900,
            full_page=False,
            prime_local_media=True,
            max_edge=1440,
            timeout_ms=30_000,
        )
        if not card_preview_path.is_file():
            raise ArtifactEditJobError({
                "code": "artifact_edit_render_failed",
                "message": "Edited landing card preview was not produced.",
            })
    restored = _html_edit_manifest_ids(edits)
    edit_manifest: dict[str, Any] = {
        "artifact_type": artifact_type,
        "render_mode": "authored_html_edit",
        "parent_run_id": parent_run_id,
        "html": output_name,
        "html_sha256": sha256_file(html_path),
        "edits": restored,
        "preview": preview_payload,
    }
    if artifact_type == "landing":
        edit_manifest.update({
            "card_preview_relative_path": "final/card_preview.png",
            "card_preview_sha256": sha256_file(final_dir / "card_preview.png"),
        })
    atomic_write_json(final_dir / "authored_html_edit_manifest.json", edit_manifest)
    return ApplyEditsResult(
        run_id=run_id,
        run_dir=str(work_dir),
        parent_run_id=parent_run_id,
        restored_layer_ids=restored,
        skipped=[],
        artifact_type=artifact_type,
    )


def _apply_authored_video_draft_edits(
    source_path: Path,
    edited_html: Path,
    run_id: str,
    parent_run_id: str,
    edits: dict[str, Any],
    *,
    work_dir: Path,
) -> ApplyEditsResult:
    final_dir = work_dir / "final"
    shutil.copytree(source_path.parent, final_dir, dirs_exist_ok=True)
    html_path = final_dir / "deck.html"
    shutil.copy2(edited_html, html_path)
    ensure_editable_html_contract(html_path, "video")
    project_html = final_dir / "project" / "index.html"
    project_html.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(html_path, project_html)
    preview_path = final_dir / "preview.png"
    preview_path.unlink(missing_ok=True)
    render = screenshot_html(
        html_path,
        preview_path,
        viewport_width=1920,
        viewport_height=1080,
        full_page=False,
        prime_local_media=True,
        max_edge=1920,
        timeout_ms=30_000,
    )
    if not preview_path.is_file():
        raise ArtifactEditJobError({
            "code": "artifact_edit_render_failed",
            "message": "Edited Video preview was not produced.",
        })
    restored = _html_edit_manifest_ids(edits)
    atomic_write_json(final_dir / "authored_html_edit_manifest.json", {
        "artifact_type": "video",
        "render_mode": "candidate_video_html_edit",
        "parent_run_id": parent_run_id,
        "html": "deck.html",
        "html_sha256": sha256_file(html_path),
        "edits": restored,
        "preview": {
            "backend": render.backend,
            "warnings": render.warnings,
            "scale": render.scale,
            "width_px": render.width_px,
            "height_px": render.height_px,
        },
    })
    return ApplyEditsResult(
        run_id=run_id,
        run_dir=str(work_dir),
        parent_run_id=parent_run_id,
        restored_layer_ids=restored,
        skipped=[],
        artifact_type="video",
    )


def _is_authored_paper_poster_html(path: Path) -> bool:
    try:
        doc = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    except (OSError, UnicodeError):
        return False
    return doc.select_one(".paper-poster") is not None and doc.select_one(".canvas") is None


def _authored_paper_poster_size(path: Path) -> tuple[int, int]:
    try:
        text = path.read_text(encoding="utf-8")
        doc = BeautifulSoup(text, "html.parser")
    except (OSError, UnicodeError):
        return (3072, 1536)
    root = doc.select_one(".paper-poster")
    if root is None:
        return (3072, 1536)
    width = _positive_int(root.get("data-w"))
    height = _positive_int(root.get("data-h"))
    if width and height:
        return width, height
    style = _style_map(str(root.get("style") or ""))
    width = _positive_int(str(style.get("width") or "").removesuffix("px"))
    height = _positive_int(str(style.get("height") or "").removesuffix("px"))
    if width and height:
        return width, height
    width = _css_px_for_selector(text, ".paper-poster", "width")
    height = _css_px_for_selector(text, ".paper-poster", "height")
    return (width, height) if width and height else (3072, 1536)


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(float(str(value)))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _style_map(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for declaration in raw.split(";"):
        key, separator, value = declaration.partition(":")
        if separator:
            result[key.strip().lower()] = value.strip()
    return result


def _css_px_for_selector(text: str, selector: str, property_name: str) -> int | None:
    match = re.search(
        rf"{re.escape(selector)}\s*\{{[^}}]*\b{re.escape(property_name)}\s*:\s*([0-9.]+)px",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    return _positive_int(match.group(1)) if match else None


def _html_edit_manifest_ids(edits: dict[str, Any]) -> list[str]:
    if "layers" in edits or "layout" in edits:
        layers = edits.get("layers") if isinstance(edits.get("layers"), dict) else {}
        layout = edits.get("layout") if isinstance(edits.get("layout"), list) else []
    else:
        layers = edits
        layout = []
    identifiers = [str(value) for value in layers]
    for index, patch in enumerate(layout, start=1):
        if not isinstance(patch, dict):
            continue
        kind = str(patch.get("kind") or "layout")
        target = patch.get("section_id") or patch.get("columns_id") or f"patch_{index}"
        identifiers.append(f"{kind}:{target}")
    return sorted(identifiers)


def _validate_required_poster_palette_html(
    html_path: Path,
    required_color_system: dict[str, Any],
) -> None:
    from .tools.propose_paper_poster_html import (
        authored_palette_diagnostics,
        required_palette_diagnostic_is_blocking,
    )

    diagnostics = authored_palette_diagnostics(
        html_path.read_text(encoding="utf-8"),
        "",
        required_color_system,
        require_selected=True,
    )
    blocking = [
        item for item in diagnostics
        if required_palette_diagnostic_is_blocking(item)
    ]
    if blocking:
        raise ArtifactEditJobError({
            "code": "poster_palette_validation_failed",
            "message": "Edited Poster HTML does not use the required palette.",
            "palette_diagnostics": blocking,
        })


def _persist_palette_manifest(
    final_dir: Path,
    *,
    pending_manifest: Path,
    palette_id: str,
    required_color_system: dict[str, Any],
) -> None:
    payload = _read_json(pending_manifest)
    manifest = payload if isinstance(payload, dict) else {
        "artifact_type": "poster",
        "render_mode": "apply_edits",
    }
    manifest["palette_id"] = palette_id
    manifest["required_color_system"] = required_color_system
    name = (
        "authored_poster_edit_manifest.json"
        if isinstance(payload, dict)
        else "apply_edits_palette_manifest.json"
    )
    atomic_write_json(final_dir / name, manifest)


def _quarantine_palette_failure(
    run_dir: Path,
    staging_dir: Path,
    *,
    parent_run_id: str,
    required_color_system: dict[str, Any],
    error_detail: dict[str, Any],
) -> None:
    for name in (
        "authored_poster_edit_manifest.json",
        "apply_edits_palette_manifest.json",
    ):
        (staging_dir / name).unlink(missing_ok=True)
    quarantine = run_dir / "quarantine" / "palette_validation_failed"
    if quarantine.exists():
        shutil.rmtree(quarantine)
    quarantine.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging_dir, quarantine)
    atomic_write_json(run_dir / "apply_edits_palette_validation_failure.json", {
        "status": "error",
        "phase": "poster_palette_validation",
        "artifact_type": "poster",
        "parent_run_id": parent_run_id,
        "palette_id": str(required_color_system.get("palette_id") or ""),
        "required_color_system": required_color_system,
        "error": error_detail,
        "quarantined_final": "quarantine/palette_validation_failed",
    })


def _edited_candidate_lineage(
    source: dict[str, Any],
    *,
    parent_run_id: str,
) -> dict[str, Any]:
    lineage = {
        **source,
        "status": "draft",
        "parent_draft_run_id": parent_run_id,
        "edited_at": datetime.now(timezone.utc).isoformat(),
    }
    if source.get("status") == "published":
        lineage["published_artifact_id_at_fork"] = f"art_{parent_run_id}"
        lineage.pop("published_version_id", None)
        lineage.pop("published_at", None)
    return lineage


def _copy_poster_validation_context(
    source_run_dir: Path,
    target_run_dir: Path,
    *,
    cancellation_token: Any,
) -> None:
    for name in (
        "paper_visual_provenance.json",
        "paper_memory.json",
        "paper_memory_dossier.json",
        "paper_visual_storyboard.json",
        "poster_content_brief.json",
        "poster_plan_contract.json",
        "poster_contract_preflight.json",
        "canvas_plan.json",
        "paper_memory.md",
        "paper_memory_dossier.md",
    ):
        cancellation_token.raise_if_cancelled("artifact_edit.copy_poster_context")
        source = source_run_dir / name
        if source.is_file():
            shutil.copy2(source, target_run_dir / name)
    for name in ("layers", "paper_evidence_packs"):
        cancellation_token.raise_if_cancelled("artifact_edit.copy_poster_context")
        source = source_run_dir / name
        if source.is_dir():
            shutil.copytree(source, target_run_dir / name, dirs_exist_ok=True)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
