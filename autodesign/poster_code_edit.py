"""Core poster code-edit promotion and delivery assembly."""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any
from urllib.parse import unquote

from bs4 import BeautifulSoup

from .agents.external_code_editor import ExternalCodeEditor
from .config import Settings
from .run_control import CancellationToken
from .util.browser_render import screenshot_html
from .util.io import atomic_write_json
from .util.logging import log


def run_poster_code_edit_sync(
    *,
    run_id: str,
    runs_dir: Path,
    source_run_id: str,
    source_run_dir: Path,
    source_poster_path: Path,
    artifact: dict[str, Any],
    instruction: str,
    conversation_history: list[dict[str, Any]],
    selection_context: dict[str, Any] | None,
    required_color_system: dict[str, Any],
    settings: Settings,
    cancellation_token: CancellationToken | None = None,
) -> dict[str, Any]:
    """Run an external poster revision and assemble its complete final directory."""

    token = cancellation_token or CancellationToken.never(run_id)
    token.raise_if_cancelled("poster_code_edit.prepare")
    runs_root = runs_dir.resolve()
    run_dir = runs_dir / run_id
    canonical_run = run_dir.resolve()
    canonical_source_run = source_run_dir.resolve()
    canonical_source_poster = source_poster_path.resolve()
    if not canonical_run.is_relative_to(runs_root):
        raise ValueError("poster code-edit run is outside the configured runs directory")
    if not canonical_source_run.is_relative_to(runs_root):
        raise ValueError("poster code-edit source run is outside the configured runs directory")
    if canonical_run == canonical_source_run:
        raise ValueError("poster code-edit run must differ from its source run")
    if not canonical_source_poster.is_relative_to(canonical_source_run):
        raise ValueError("poster code-edit source poster is outside its source run")
    final_dir = run_dir / "final"
    _make_directory(final_dir, token, "poster_code_edit.before_final_directory")
    source_final_dir = source_poster_path.parent
    context_run_dirs = _poster_revision_context_run_dirs(
        runs_dir,
        source_run_id,
        artifact,
    )
    if source_run_dir not in context_run_dirs:
        context_run_dirs.insert(0, source_run_dir)

    log(
        "code_editor.prepare",
        run_id=run_id,
        source_run_id=source_run_id,
        context_runs=[path.name for path in context_run_dirs],
    )
    editor = ExternalCodeEditor(settings)
    edit_result = editor.run(
        source_poster_path=source_poster_path,
        source_final_dir=source_final_dir,
        run_dir=run_dir,
        parent_run_id=source_run_id,
        instruction=instruction,
        conversation_history=conversation_history,
        selection_context=selection_context,
        context_run_dirs=context_run_dirs,
        required_color_system=required_color_system,
        cancellation_token=token,
    )
    token.raise_if_cancelled("poster_code_edit.after_author")

    for child in source_final_dir.iterdir():
        token.raise_if_cancelled("poster_code_edit.copy_source_assets")
        if child.is_dir():
            _copy_tree(
                child,
                final_dir / child.name,
                token,
                "poster_code_edit.copy_source_assets",
            )

    html_path = final_dir / "poster.html"
    _copy_file(
        edit_result.poster_path,
        html_path,
        token,
        "poster_code_edit.copy_poster",
    )
    promoted_assets = _promote_referenced_html_assets(
        edit_result.poster_path,
        edit_result.poster_path.parent,
        final_dir,
        token,
    )
    token.raise_if_cancelled("poster_code_edit.after_asset_promotion")
    width, height = _authored_paper_poster_size(html_path)
    preview_path = final_dir / "preview.png"
    log("code_editor.validate_preview", run_id=run_id, width=width, height=height)
    token.raise_if_cancelled("poster_code_edit.before_preview")
    browser_result = screenshot_html(
        html_path,
        preview_path,
        viewport_width=width,
        viewport_height=height,
        selector=".paper-poster",
        max_edge=settings.poster_preview_max_edge,
        timeout_ms=30_000,
    )
    token.raise_if_cancelled("poster_code_edit.after_preview")
    if browser_result.warnings and (source_final_dir / "preview.png").exists():
        _copy_file(
            source_final_dir / "preview.png",
            preview_path,
            token,
            "poster_code_edit.copy_fallback_preview",
        )
    if not preview_path.exists():
        raise RuntimeError("; ".join(browser_result.warnings) or "preview screenshot failed")

    attempts = [asdict(record) for record in edit_result.attempts]
    context_summary = selection_context_summary(selection_context)
    manifest = {
        "artifact_type": "poster",
        "render_mode": "external_code_editor_revision",
        "parent_run_id": source_run_id,
        "parent_artifact_id": artifact.get("artifact_id") or f"art_{source_run_id}",
        "palette_id": str(required_color_system.get("palette_id") or ""),
        "required_color_system": required_color_system,
        "code_editor_harness": getattr(settings, "code_editor_harness", "codex"),
        "user_instruction": instruction,
        "attempts": attempts,
        "validation_summary": edit_result.validation_summary,
        "selection_context_summary": context_summary,
        "promoted_assets": promoted_assets,
        "canvas": {"w_px": width, "h_px": height},
        "preview": {
            "backend": browser_result.backend,
            "warnings": browser_result.warnings,
            "scale": browser_result.scale,
            "width_px": browser_result.width_px,
            "height_px": browser_result.height_px,
        },
    }
    token.raise_if_cancelled("poster_code_edit.before_manifest")
    atomic_write_json(final_dir / "code_editor_revision_manifest.json", manifest)
    token.raise_if_cancelled("poster_code_edit.after_manifest")
    return {
        "run_dir": str(run_dir),
        "attempt_dir": str(edit_result.attempt_dir),
        "poster_path": str(html_path),
        "preview_path": str(preview_path),
        "attempts": attempts,
        "validation_summary": edit_result.validation_summary,
        "selection_context_summary": context_summary,
        "promoted_assets": promoted_assets,
    }


def selection_context_summary(
    selection_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
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
        point_count = sum(
            len(path.get("points") or [])
            for path in drawing_paths
            if isinstance(path, dict) and isinstance(path.get("points"), list)
        )
        summary["drawing_paths"] = {
            "count": len(drawing_paths),
            "points": point_count,
        }
    items = selection_context.get("items")
    if isinstance(items, list):
        item_summaries: list[dict[str, Any]] = []
        total_drawing_paths = 0
        total_drawing_points = 0
        for index, item in enumerate(items[:6], start=1):
            if not isinstance(item, dict):
                continue
            item_rect = item.get("rect")
            item_summary: dict[str, Any] = {
                "index": index,
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
                item_point_count = sum(
                    len(path.get("points") or [])
                    for path in item_paths
                    if isinstance(path, dict) and isinstance(path.get("points"), list)
                )
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
            item_summaries.append(
                {
                    key: value
                    for key, value in item_summary.items()
                    if value not in (None, "", [], {})
                }
            )
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
            str(item)[:120]
            for item in headings[:4]
            if str(item).strip()
        ]
    text = str(selection_context.get("text_excerpt") or "").strip()
    if text:
        summary["text_excerpt"] = text[:240]
    instruction = str(selection_context.get("instruction") or "").strip()
    if instruction:
        summary["instruction"] = instruction[:240]
    return {
        key: value
        for key, value in summary.items()
        if value not in (None, "", [], {})
    }


def _promote_referenced_html_assets(
    html_path: Path,
    source_dir: Path,
    final_dir: Path,
    token: CancellationToken,
) -> list[str]:
    try:
        html_text = html_path.read_text(encoding="utf-8")
    except OSError:
        return []
    source_root = source_dir.resolve()
    final_root = final_dir.resolve()
    copied: list[str] = []
    for relative in sorted(_relative_html_asset_refs(html_text)):
        token.raise_if_cancelled("poster_code_edit.before_referenced_asset")
        source = (source_dir / relative).resolve()
        try:
            source.relative_to(source_root)
        except ValueError:
            continue
        if not source.is_file():
            continue
        target = (final_dir / relative).resolve()
        try:
            target.relative_to(final_root)
        except ValueError:
            continue
        _copy_file(
            source,
            target,
            token,
            "poster_code_edit.copy_referenced_asset",
        )
        copied.append(relative)
    if copied:
        log("code_editor.assets.promoted", count=len(copied), assets=copied[:20])
    return copied


def _copy_tree(
    source: Path,
    target: Path,
    token: CancellationToken,
    phase: str,
) -> None:
    _make_directory(target, token, f"{phase}.mkdir")
    for current_root, directory_names, file_names in os.walk(source):
        directory_names.sort()
        file_names.sort()
        current = Path(current_root)
        relative_root = current.relative_to(source)
        target_root = target / relative_root
        _make_directory(target_root, token, f"{phase}.mkdir")
        for directory_name in directory_names:
            _make_directory(
                target_root / directory_name,
                token,
                f"{phase}.mkdir",
            )
        for file_name in file_names:
            _copy_file(
                current / file_name,
                target_root / file_name,
                token,
                f"{phase}.copy",
            )


def _make_directory(path: Path, token: CancellationToken, phase: str) -> None:
    token.raise_if_cancelled(phase)
    path.mkdir(parents=True, exist_ok=True)


def _copy_file(
    source: Path,
    target: Path,
    token: CancellationToken,
    phase: str,
) -> None:
    _make_directory(target.parent, token, f"{phase}.mkdir")
    token.raise_if_cancelled(phase)
    shutil.copy2(source, target)


def _poster_revision_context_run_dirs(
    runs_dir: Path,
    source_run_id: str,
    artifact: dict[str, Any],
) -> list[Path]:
    seen: set[str] = set()
    context_run_dirs: list[Path] = []
    current: str | None = source_run_id
    first = True
    while current and current not in seen and len(context_run_dirs) < 12:
        seen.add(current)
        run_dir = runs_dir / current
        if run_dir.exists():
            context_run_dirs.append(run_dir)
        current = _poster_revision_parent_run_id(
            run_dir,
            artifact if first else {},
        )
        first = False
    return context_run_dirs


def _poster_revision_parent_run_id(
    run_dir: Path,
    artifact: dict[str, Any],
) -> str | None:
    for raw in (artifact.get("parent_run_id"), artifact.get("parent_artifact_id")):
        parent = _run_id_from_maybe_artifact_ref(raw)
        if parent:
            return parent
    for name in ("code_editor_revision_manifest.json", "authored_poster_edit_manifest.json"):
        manifest = _read_json_file(run_dir / "final" / name)
        if not isinstance(manifest, dict):
            continue
        for key in ("parent_run_id", "parent_artifact_id"):
            parent = _run_id_from_maybe_artifact_ref(manifest.get(key))
            if parent:
                return parent
    return None


def _run_id_from_maybe_artifact_ref(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    clean = value.strip()
    return clean[len("art_"):] if clean.startswith("art_") else clean


def _read_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _relative_html_asset_refs(html_text: str) -> set[str]:
    refs: set[str] = set()
    for match in re.finditer(
        r'''\b(?:src|href)=["']([^"']+)["']''',
        html_text,
        flags=re.IGNORECASE,
    ):
        ref = _portable_html_asset_ref(match.group(1))
        if ref:
            refs.add(ref)
    for match in re.finditer(
        r'''\bsrcset=["']([^"']+)["']''',
        html_text,
        flags=re.IGNORECASE,
    ):
        for raw in match.group(1).split(","):
            ref = _portable_html_asset_ref(raw.strip().split(" ", 1)[0])
            if ref:
                refs.add(ref)
    for match in re.finditer(r"url\(([^)]+)\)", html_text, flags=re.IGNORECASE):
        ref = _portable_html_asset_ref(match.group(1).strip().strip("'\""))
        if ref:
            refs.add(ref)
    return refs


def _portable_html_asset_ref(raw: str) -> str | None:
    value = unquote(str(raw or "").strip()).split("#", 1)[0].split("?", 1)[0].strip()
    if not value or value.startswith("#") or value.startswith("/"):
        return None
    lowered = value.lower()
    if lowered.startswith(("data:", "http://", "https://", "file:", "javascript:")):
        return None
    if "://" in lowered or "\\" in value:
        return None
    parts = [part for part in value.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return None
    return "/".join(parts)


def _authored_paper_poster_size(path: Path) -> tuple[int, int]:
    try:
        html_text = path.read_text(encoding="utf-8")
        document = BeautifulSoup(html_text, "html.parser")
    except Exception:
        return (3072, 1536)
    root = document.select_one(".paper-poster")
    if root is None:
        return (3072, 1536)
    data_width = _positive_int(root.get("data-w"))
    data_height = _positive_int(root.get("data-h"))
    if data_width and data_height:
        return (data_width, data_height)
    style = _style_map(str(root.get("style") or ""))
    width = _positive_int(style.get("width"))
    height = _positive_int(style.get("height"))
    if width and height:
        return (width, height)
    css_width = _css_px_for_selector(html_text, ".paper-poster", "width")
    css_height = _css_px_for_selector(html_text, ".paper-poster", "height")
    if css_width and css_height:
        return (css_width, css_height)
    return (3072, 1536)


def _css_px_for_selector(css_text: str, selector: str, property_name: str) -> int | None:
    pattern = re.compile(
        rf"{re.escape(selector)}\s*\{{[^}}]*\b{re.escape(property_name)}\s*:\s*([0-9.]+)px",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(css_text)
    return _positive_int(match.group(1)) if match else None


def _positive_int(raw: Any) -> int | None:
    if raw is None:
        return None
    clean = str(raw).strip().lower().removesuffix("px").strip()
    try:
        value = int(float(clean))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _style_map(raw_style: str) -> dict[str, str]:
    style: dict[str, str] = {}
    for part in str(raw_style).split(";"):
        key, separator, value = part.partition(":")
        if separator:
            style[key.strip().lower()] = value.strip()
    return style
