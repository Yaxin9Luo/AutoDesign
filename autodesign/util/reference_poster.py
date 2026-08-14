"""Normalize a user-supplied poster into one safe raster style reference."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from math import ceil, gcd
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from PIL import Image, ImageOps

from .io import atomic_write_json, sha256_file


SUPPORTED_REFERENCE_POSTER_SUFFIXES = {
    ".html",
    ".htm",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".pptx",
}
_REFERENCE_MAX_EDGE = 3072


class ReferencePosterError(ValueError):
    """Raised when a reference poster cannot be normalized safely."""


def normalize_reference_poster(
    source_path: Path,
    output_dir: Path,
    *,
    page_index: int = 0,
) -> dict[str, Any]:
    """Create ``reference.png`` plus source metadata under ``output_dir``.

    HTML is rendered with JavaScript disabled and remote requests blocked.
    PDF/PPTX page selection is zero-based internally.
    """

    source = Path(source_path).expanduser().resolve()
    if not source.exists() or not source.is_file():
        raise ReferencePosterError(f"reference poster not found: {source}")
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_REFERENCE_POSTER_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_REFERENCE_POSTER_SUFFIXES))
        raise ReferencePosterError(
            f"unsupported reference poster format {suffix or '(none)'}; supported: {supported}"
        )
    if page_index < 0:
        raise ReferencePosterError("reference poster page index must be non-negative")

    output_dir.mkdir(parents=True, exist_ok=True)
    staged_source = output_dir / f"reference_source{suffix}"
    if source != staged_source.resolve():
        shutil.copy2(source, staged_source)
    preview_path = output_dir / "reference.png"

    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        format_metadata = _normalize_image(source, preview_path)
    elif suffix == ".pdf":
        format_metadata = _render_pdf_page(source, preview_path, page_index=page_index)
    elif suffix == ".pptx":
        format_metadata = _render_pptx_slide(
            staged_source,
            output_dir,
            preview_path,
            page_index=page_index,
        )
    else:
        format_metadata = _render_html(source, preview_path)

    with Image.open(preview_path) as image:
        preview_width, preview_height = image.size
    metadata: dict[str, Any] = {
        "version": 2,
        "source_name": source.name,
        "source_suffix": suffix,
        "source_sha256": sha256_file(source),
        "staged_source": staged_source.name,
        "preview": preview_path.name,
        "preview_width_px": preview_width,
        "preview_height_px": preview_height,
        "page_index": page_index,
        "content_transfer_forbidden": True,
        **format_metadata,
    }
    intrinsic_width, intrinsic_height, intrinsic_unit = _intrinsic_dimensions(metadata)
    metadata.update({
        "intrinsic_width": intrinsic_width,
        "intrinsic_height": intrinsic_height,
        "intrinsic_unit": intrinsic_unit,
        "intrinsic_aspect_ratio": round(intrinsic_width / intrinsic_height, 6),
    })
    metadata["default_canvas"] = reference_canvas_from_metadata(metadata)
    metadata["canvas_contract"] = dict(metadata["default_canvas"])
    base_width, _base_height, _base_dpi = _reference_canvas_base_dimensions(metadata)
    metadata["canvas_scale_factor"] = round(
        float(metadata["default_canvas"]["w_px"]) / max(1, base_width),
        6,
    )
    metadata["canvas_scale_policy"] = "aspect_preserving_4k_tiers"
    atomic_write_json(output_dir / "reference_source_metadata.json", metadata)
    return metadata


def reference_canvas_from_metadata(metadata: dict[str, Any]) -> dict[str, object]:
    """Return an aspect-preserving, roughly 4K working canvas for a reference."""

    width, height, dpi = _reference_canvas_base_dimensions(metadata)
    if width <= 0 or height <= 0:
        return {}
    scale = _reference_canvas_scale_factor(width, height)
    width = max(1, int(round(width * scale)))
    height = max(1, int(round(height * scale)))
    divisor = gcd(width, height)
    return {
        "w_px": width,
        "h_px": height,
        "dpi": dpi,
        "aspect_ratio": f"{width // divisor}:{height // divisor}",
        "color_mode": "RGB",
    }


def _reference_canvas_base_dimensions(metadata: dict[str, Any]) -> tuple[int, int, int]:
    suffix = str(metadata.get("source_suffix") or "").lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        width = _positive_int(metadata.get("original_width_px"))
        height = _positive_int(metadata.get("original_height_px"))
        dpi = 96
    elif suffix in {".html", ".htm"}:
        root_style = (
            metadata.get("computed_root_style")
            if isinstance(metadata.get("computed_root_style"), dict)
            else {}
        )
        width = _positive_int(root_style.get("width_px") or metadata.get("preview_width_px"))
        height = _positive_int(root_style.get("height_px") or metadata.get("preview_height_px"))
        dpi = 96
    else:
        width = _positive_int(metadata.get("preview_width_px"))
        height = _positive_int(metadata.get("preview_height_px"))
        dpi = 150 if suffix in {".pdf", ".pptx"} else 96
    return width, height, dpi


def _reference_canvas_scale_factor(width: int, height: int) -> float:
    """Scale low-resolution references without changing their aspect ratio."""

    long_edge = max(width, height)
    if long_edge < 1024:
        return 4.0
    if long_edge < 2048:
        return float(max(2, ceil(3840 / long_edge)))
    if long_edge < 2560:
        return 2.0
    if long_edge < 3840:
        return 4096.0 / long_edge
    return 1.0


def _intrinsic_dimensions(metadata: dict[str, Any]) -> tuple[float | int, float | int, str]:
    suffix = str(metadata.get("source_suffix") or "").lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        width = _positive_int(metadata.get("original_width_px"))
        height = _positive_int(metadata.get("original_height_px"))
        unit = "px"
    elif suffix == ".pptx":
        width = _positive_int(metadata.get("slide_width_emu"))
        height = _positive_int(metadata.get("slide_height_emu"))
        unit = "emu"
    elif suffix == ".pdf":
        width = _positive_float(metadata.get("source_page_width_pt"))
        height = _positive_float(metadata.get("source_page_height_pt"))
        unit = "pt"
    else:
        computed = metadata.get("computed_root_style")
        computed = computed if isinstance(computed, dict) else {}
        width = _positive_int(computed.get("width_px"))
        height = _positive_int(computed.get("height_px"))
        unit = "css_px"
    if width <= 0 or height <= 0:
        width = _positive_int(metadata.get("preview_width_px"))
        height = _positive_int(metadata.get("preview_height_px"))
        unit = "px"
    if width <= 0 or height <= 0:
        raise ReferencePosterError("normalized reference poster has no valid dimensions")
    return width, height, unit


def _positive_int(value: Any) -> int:
    try:
        return max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return 0


def _positive_float(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _normalize_image(source: Path, preview_path: Path) -> dict[str, Any]:
    with Image.open(source) as raw:
        image = ImageOps.exif_transpose(raw).convert("RGB")
        original_size = image.size
        image.thumbnail((_REFERENCE_MAX_EDGE, _REFERENCE_MAX_EDGE), Image.Resampling.LANCZOS)
        image.save(preview_path, format="PNG", optimize=True)
    return {
        "normalization_backend": "pillow",
        "original_width_px": original_size[0],
        "original_height_px": original_size[1],
    }


def _render_pdf_page(
    source: Path,
    preview_path: Path,
    *,
    page_index: int,
) -> dict[str, Any]:
    try:
        import fitz
    except Exception as exc:  # pragma: no cover - dependency is required by the repo
        raise ReferencePosterError(f"PyMuPDF is unavailable: {exc}") from exc

    try:
        document = fitz.open(source)
    except Exception as exc:
        raise ReferencePosterError(f"could not open reference PDF: {exc}") from exc
    try:
        if page_index >= document.page_count:
            raise ReferencePosterError(
                f"reference page {page_index + 1} exceeds PDF page count {document.page_count}"
            )
        page = document.load_page(page_index)
        long_edge = max(float(page.rect.width), float(page.rect.height), 1.0)
        scale = min(4.0, _REFERENCE_MAX_EDGE / long_edge)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        pixmap.save(str(preview_path))
        return {
            "normalization_backend": "pymupdf",
            "page_count": document.page_count,
            "source_page_width_pt": round(float(page.rect.width), 3),
            "source_page_height_pt": round(float(page.rect.height), 3),
        }
    finally:
        document.close()


def _render_pptx_slide(
    staged_source: Path,
    output_dir: Path,
    preview_path: Path,
    *,
    page_index: int,
) -> dict[str, Any]:
    try:
        from pptx import Presentation
    except Exception as exc:  # pragma: no cover - dependency is required by the repo
        raise ReferencePosterError(f"python-pptx is unavailable: {exc}") from exc

    try:
        presentation = Presentation(str(staged_source))
    except Exception as exc:
        raise ReferencePosterError(f"could not open reference PPTX: {exc}") from exc
    slide_count = len(presentation.slides)
    if page_index >= slide_count:
        raise ReferencePosterError(
            f"reference slide {page_index + 1} exceeds PPTX slide count {slide_count}"
        )

    soffice = shutil.which("soffice")
    if not soffice:
        raise ReferencePosterError("PPTX reference rendering requires LibreOffice `soffice`")
    converted_pdf = output_dir / "reference_source.pdf"
    converted_pdf.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="autodesign-lo-") as profile_dir:
        cmd = [
            soffice,
            "--headless",
            f"-env:UserInstallation={Path(profile_dir).resolve().as_uri()}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_dir),
            str(staged_source),
        ]
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    if completed.returncode != 0 or not converted_pdf.exists():
        detail = (completed.stderr or completed.stdout or "conversion produced no PDF").strip()
        raise ReferencePosterError(f"could not render reference PPTX: {detail}")

    pdf_metadata = _render_pdf_page(converted_pdf, preview_path, page_index=page_index)
    return {
        **pdf_metadata,
        "normalization_backend": "libreoffice+pymupdf",
        "slide_count": slide_count,
        "slide_width_emu": int(presentation.slide_width),
        "slide_height_emu": int(presentation.slide_height),
        "converted_pdf": converted_pdf.name,
    }


def _render_html(source: Path, preview_path: Path) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise ReferencePosterError(f"Playwright is unavailable: {exc}") from exc

    allowed_root = source.parent.resolve()
    selected = "body"
    computed: dict[str, Any] = {}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=["--no-sandbox"])
            context = browser.new_context(
                java_script_enabled=False,
                viewport={"width": 3072, "height": 1536},
                device_scale_factor=1,
            )
            page = context.new_page()

            def handle_route(route: Any) -> None:
                parsed = urlparse(route.request.url)
                if parsed.scheme == "data":
                    route.continue_()
                    return
                if parsed.scheme == "file":
                    candidate = Path(unquote(parsed.path)).resolve()
                    if candidate == allowed_root or allowed_root in candidate.parents:
                        route.continue_()
                        return
                route.abort()

            page.route("**/*", handle_route)
            page.goto(source.as_uri(), wait_until="load", timeout=30_000)
            page.emulate_media(media="screen")
            for selector in (".paper-poster", ".poster", "[data-poster-root]"):
                if page.locator(selector).count() > 0:
                    selected = selector
                    break
            locator = page.locator(selected).first
            if locator.count() <= 0:
                raise ReferencePosterError("reference HTML has no renderable body")
            computed = locator.evaluate(
                """el => {
                  const s = getComputedStyle(el);
                  const r = el.getBoundingClientRect();
                  return {
                    width_px: Math.round(r.width), height_px: Math.round(r.height),
                    background_color: s.backgroundColor, color: s.color,
                    font_family: s.fontFamily
                  };
                }"""
            )
            locator.screenshot(path=str(preview_path), animations="disabled", timeout=30_000)
            browser.close()
    except ReferencePosterError:
        raise
    except Exception as exc:
        raise ReferencePosterError(f"could not render reference HTML safely: {exc}") from exc

    _downsample_png(preview_path)
    return {
        "normalization_backend": "playwright_js_disabled",
        "html_network_policy": "local_siblings_and_data_only",
        "selected_root": selected,
        "computed_root_style": computed,
    }


def _downsample_png(path: Path) -> None:
    with Image.open(path) as raw:
        image = raw.convert("RGB")
        if max(image.size) <= _REFERENCE_MAX_EDGE:
            image.save(path, format="PNG", optimize=True)
            return
        image.thumbnail((_REFERENCE_MAX_EDGE, _REFERENCE_MAX_EDGE), Image.Resampling.LANCZOS)
        image.save(path, format="PNG", optimize=True)
