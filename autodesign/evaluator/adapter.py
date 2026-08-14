"""Artifact adapters for deterministic evaluation snapshots."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import fitz
from PIL import Image

from autodesign.util.browser_render import screenshot_html

from .schema import ArtifactSnapshot

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - dependency is optional by design here.
    BeautifulSoup = None  # type: ignore[assignment]


HTML_EXTS = {".html", ".htm"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
PDF_EXTS = {".pdf"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}


def snapshot_artifact(artifact: Path, out_dir: Path, *, artifact_type: str = "poster") -> ArtifactSnapshot:
    artifact = artifact.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if artifact.is_dir():
        return _snapshot_dir(artifact, out_dir, artifact_type=artifact_type)
    return _snapshot_file(artifact, out_dir)


def _snapshot_dir(path: Path, out_dir: Path, *, artifact_type: str) -> ArtifactSnapshot:
    html_names, preview_names, pdf_names, video_names = _candidate_names(artifact_type)
    html = _first_existing(
        _candidate_dirs(path),
        html_names,
        HTML_EXTS,
    )
    preview = _first_existing(
        _candidate_dirs(path),
        preview_names,
        IMAGE_EXTS,
    )
    pdf = _first_existing(_candidate_dirs(path), pdf_names, PDF_EXTS)
    video = _first_existing(_candidate_dirs(path), video_names, VIDEO_EXTS)
    if html:
        snapshot = _snapshot_html(html, out_dir)
        snapshot.artifact_path = str(path)
        snapshot.artifact_kind = "run_dir"
    elif preview:
        snapshot = _snapshot_image(preview, out_dir)
        snapshot.artifact_path = str(path)
        snapshot.artifact_kind = "run_dir"
    elif pdf:
        snapshot = _snapshot_pdf(pdf, out_dir)
        snapshot.artifact_path = str(path)
        snapshot.artifact_kind = "run_dir"
    elif video:
        snapshot = _snapshot_video(video)
        snapshot.artifact_path = str(path)
        snapshot.artifact_kind = "run_dir"
    else:
        snapshot = ArtifactSnapshot(artifact_path=str(path), artifact_kind="run_dir")
    for key, value in (("html", html), ("preview", preview), ("pdf", pdf), ("video", video)):
        if value:
            snapshot.resolved_files[key] = str(value)
    return snapshot


def _snapshot_file(path: Path, out_dir: Path) -> ArtifactSnapshot:
    if not path.exists():
        return ArtifactSnapshot(
            artifact_path=str(path),
            artifact_kind=path.suffix.lower().lstrip(".") or "unknown",
            metadata={"exists": False, "size_bytes": None},
        )
    suffix = path.suffix.lower()
    if suffix in HTML_EXTS:
        return _snapshot_html(path, out_dir)
    if suffix in IMAGE_EXTS:
        return _snapshot_image(path, out_dir)
    if suffix in PDF_EXTS:
        return _snapshot_pdf(path, out_dir)
    if suffix in VIDEO_EXTS:
        return _snapshot_video(path)
    return ArtifactSnapshot(
        artifact_path=str(path),
        artifact_kind=suffix.lstrip(".") or "unknown",
        metadata={"exists": path.exists(), "size_bytes": _size(path)},
    )


def _snapshot_html(path: Path, out_dir: Path) -> ArtifactSnapshot:
    raw = path.read_text(encoding="utf-8", errors="replace")
    text = _html_text(raw)
    preview = _find_adjacent_preview(path)
    render_meta: dict[str, Any] = {}
    copied_preview = _copy_preview(preview, out_dir) if preview else None
    if copied_preview is None:
        copied_preview, render_meta = _render_html_preview(path, raw, out_dir)
    width = height = None
    if copied_preview:
        width, height = _image_size(Path(copied_preview))
    dom_layers = _extract_dom_layers(raw)
    return ArtifactSnapshot(
        artifact_path=str(path),
        artifact_kind="html",
        resolved_files={"html": str(path), **({"preview": str(preview)} if preview else {})},
        text=text,
        html=raw,
        preview_image=copied_preview,
        dom_layers=dom_layers,
        width=width,
        height=height,
        metadata={
            "exists": path.exists(),
            "size_bytes": _size(path),
            "render": render_meta,
            "dom_layer_count": len(dom_layers),
        },
    )


def _snapshot_image(path: Path, out_dir: Path) -> ArtifactSnapshot:
    copied = _copy_preview(path, out_dir)
    try:
        width, height = _image_size(Path(copied))
        metadata = {"exists": path.exists(), "size_bytes": _size(path)}
    except Exception as exc:
        width, height = None, None
        metadata = {"exists": path.exists(), "size_bytes": _size(path), "error": f"{type(exc).__name__}: {exc}"}
    return ArtifactSnapshot(
        artifact_path=str(path),
        artifact_kind="image",
        resolved_files={"image": str(path)},
        preview_image=copied,
        width=width,
        height=height,
        metadata=metadata,
    )


def _snapshot_pdf(path: Path, out_dir: Path) -> ArtifactSnapshot:
    text = ""
    page_count = 0
    preview: str | None = None
    width = height = None
    metadata: dict[str, Any] = {"exists": path.exists(), "size_bytes": _size(path)}
    try:
        with fitz.open(path) as doc:
            page_count = doc.page_count
            text = "\n".join(page.get_text("text") for page in doc[: min(3, page_count)])
            if page_count:
                pix = doc[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                preview_path = out_dir / "artifact_preview.png"
                pix.save(preview_path)
                preview = str(preview_path)
                width, height = pix.width, pix.height
    except Exception as exc:
        metadata["error"] = f"{type(exc).__name__}: {exc}"
    return ArtifactSnapshot(
        artifact_path=str(path),
        artifact_kind="pdf",
        resolved_files={"pdf": str(path)},
        text=text,
        preview_image=preview,
        width=width,
        height=height,
        page_count=page_count,
        metadata=metadata,
    )


def _snapshot_video(path: Path) -> ArtifactSnapshot:
    return ArtifactSnapshot(
        artifact_path=str(path),
        artifact_kind="video",
        resolved_files={"video": str(path)},
        metadata={
            "exists": path.exists(),
            "size_bytes": _size(path),
            "support": "metadata_only",
        },
    )


def _candidate_dirs(path: Path) -> tuple[Path, ...]:
    final_dir = path / "final"
    return (final_dir, path) if final_dir.is_dir() else (path,)


def _candidate_names(artifact_type: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    common_html = ("artifact.html", "final.html")
    common_preview = ("preview.png", "final.png", "contact_sheet.png")
    common_pdf = ("artifact.pdf", "final.pdf")
    if artifact_type == "deck":
        return (
            ("deck.html", "slides.html", *common_html),
            ("preview.png", "deck.png", "slides.png", *common_preview),
            ("deck.pdf", "slides.pdf", *common_pdf),
            ("deck.mp4", "video.mp4", "final.mp4"),
        )
    if artifact_type == "landing":
        return (
            ("index.html", "landing.html", *common_html),
            ("preview.png", "landing.png", *common_preview),
            ("landing.pdf", *common_pdf),
            ("landing.mp4", "video.mp4", "final.mp4"),
        )
    if artifact_type == "video":
        return (
            ("index.html", "video.html", *common_html),
            ("preview.png", "poster.png", "thumbnail.png", *common_preview),
            ("video.pdf", *common_pdf),
            ("final.mp4", "video.mp4", "render.mp4", "output.mp4"),
        )
    return (
        ("poster.html", "index.html", *common_html),
        ("preview.png", "poster.png", *common_preview),
        ("poster.pdf", *common_pdf),
        ("poster.mp4", "video.mp4", "final.mp4"),
    )


def _first_existing(paths: Path | tuple[Path, ...], preferred: tuple[str, ...], exts: set[str]) -> Path | None:
    search_paths = (paths,) if isinstance(paths, Path) else paths
    for search_path in search_paths:
        for name in preferred:
            candidate = search_path / name
            if candidate.is_file():
                return candidate
    matches = sorted(
        p
        for search_path in search_paths
        for p in search_path.iterdir()
        if p.is_file() and p.suffix.lower() in exts
    )
    return matches[0] if matches else None


def _find_adjacent_preview(path: Path) -> Path | None:
    return _first_existing(
        path.parent,
        ("preview.png", "final.png", "poster.png", f"{path.stem}.png"),
        IMAGE_EXTS,
    )


def _copy_preview(path: Path, out_dir: Path) -> str:
    target = out_dir / "artifact_preview.png"
    if path.resolve() != target.resolve():
        shutil.copyfile(path, target)
    return str(target)


def _image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


def _html_text(raw: str) -> str:
    if BeautifulSoup is None:
        return " ".join(raw.replace("<", " <").replace(">", "> ").split())
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())


def _render_html_preview(path: Path, raw: str, out_dir: Path) -> tuple[str | None, dict[str, Any]]:
    width, height, selector, full_page = _html_render_hint(raw)
    out_path = out_dir / "artifact_preview.png"
    result = screenshot_html(
        path,
        out_path,
        viewport_width=width,
        viewport_height=height,
        selector=selector,
        full_page=full_page,
        max_edge=1800,
        timeout_ms=15_000,
    )
    metadata = {
        "backend": result.backend,
        "warnings": list(result.warnings),
        "selector": selector,
        "full_page": full_page,
    }
    if result.paths and out_path.exists():
        return str(out_path), metadata
    return None, metadata


def _html_render_hint(raw: str) -> tuple[int, int, str | None, bool]:
    if BeautifulSoup is None:
        return 1440, 1000, None, True
    soup = BeautifulSoup(raw, "html.parser")
    poster = soup.select_one(".paper-poster")
    if poster is not None:
        width, height = _element_size_hint(poster, default=(3072, 1536))
        return width, height, ".paper-poster", False
    deck = soup.select_one(".deck-slide")
    if deck is not None:
        width, height = _element_size_hint(deck, default=(1920, 1080))
        return width, height, ".deck-slide", False
    artifact = soup.select_one("main.od-artifact, main.ld-landing, .od-artifact")
    if artifact is not None:
        width = _intish(artifact.get("data-w")) or 1440
        return width, 1000, None, True
    return 1440, 1000, None, True


def _element_size_hint(tag: Any, *, default: tuple[int, int]) -> tuple[int, int]:
    width = _intish(tag.get("data-w")) or _intish(tag.get("data-width"))
    height = _intish(tag.get("data-h")) or _intish(tag.get("data-height"))
    style = str(tag.get("style") or "")
    if width is None:
        width = _css_px(style, "width")
    if height is None:
        height = _css_px(style, "height")
    return max(1, width or default[0]), max(1, height or default[1])


def _css_px(style: str, key: str) -> int | None:
    import re

    match = re.search(rf"{re.escape(key)}\s*:\s*([0-9.]+)px", style, flags=re.I)
    if not match:
        return None
    return _intish(match.group(1))


def _intish(value: Any) -> int | None:
    try:
        return int(round(float(str(value).strip())))
    except (TypeError, ValueError):
        return None


def _extract_dom_layers(raw: str) -> list[dict[str, Any]]:
    if BeautifulSoup is None:
        return []
    soup = BeautifulSoup(raw, "html.parser")
    layers: list[dict[str, Any]] = []
    for tag in soup.select("[data-block-id], [data-layer-id], .layer, .od-layer"):
        block_id = str(tag.get("data-block-id") or tag.get("data-layer-id") or tag.get("id") or "")
        text = " ".join(tag.get_text(" ").split())
        if not block_id and not text:
            continue
        class_value = tag.get("class") or []
        layers.append({
            "block_id": block_id,
            "kind": str(tag.get("data-block-kind") or tag.name or ""),
            "role": str(tag.get("data-role") or tag.get("role") or ""),
            "class_name": " ".join(class_value) if isinstance(class_value, list) else str(class_value),
            "text": text[:1000],
            "src": str(tag.get("src") or ""),
        })
    return layers


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None
