"""Optional Playwright-backed HTML screenshot helpers.

These helpers are deliberately best-effort. The repo's no-API smoke suite must
still pass on machines without Playwright or a downloaded Chromium browser, so
callers receive an explicit warning list and can fall back to Pillow previews.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from ..attempt_candidates import promotion_browser_document_session
from .math_typesetting import wait_for_autodesign_math


@dataclass
class BrowserRenderResult:
    backend: str
    warnings: list[str] = field(default_factory=list)
    paths: list[Path] = field(default_factory=list)
    scale: float = 1.0
    width_px: int | None = None
    height_px: int | None = None

    @property
    def ok(self) -> bool:
        return not self.warnings


@contextmanager
def _browser_document_session(page: object, html_path: Path) -> Iterator[str]:
    with promotion_browser_document_session(html_path) as document_session:
        try:
            document_session.install(page)
            yield document_session.url
        finally:
            document_session.close()


def screenshot_html(
    html_path: Path,
    out_path: Path,
    *,
    viewport_width: int,
    viewport_height: int,
    selector: str | None = None,
    full_page: bool = False,
    prime_local_media: bool = False,
    max_edge: int | None = None,
    timeout_ms: int = 15_000,
) -> BrowserRenderResult:
    """Capture a single HTML file to ``out_path`` using Playwright.

    ``selector`` screenshots a specific element, otherwise the page viewport
    (or full page) is captured.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return BrowserRenderResult(
            backend="pillow-fallback",
            warnings=[f"playwright_unavailable: {type(e).__name__}: {e}"],
        )

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = _launch_chromium(p)
            try:
                page = browser.new_page(
                    viewport={
                        "width": max(1, int(viewport_width)),
                        "height": max(1, int(viewport_height)),
                    },
                    device_scale_factor=1,
                )
                page.set_default_timeout(timeout_ms)
                page.set_default_navigation_timeout(timeout_ms)
                with _browser_document_session(page, html_path) as document_url:
                    page.goto(
                        document_url,
                        wait_until="load",
                        timeout=timeout_ms,
                    )
                    try:
                        page.wait_for_load_state(
                            "networkidle",
                            timeout=min(2000, timeout_ms),
                        )
                    except Exception:
                        pass
                    wait_for_autodesign_math(page, timeout_ms=min(3000, timeout_ms))
                    if prime_local_media:
                        _prime_local_media(page, timeout_ms=timeout_ms)
                    if selector:
                        loc = page.locator(selector).first
                        if loc.count() <= 0:
                            return BrowserRenderResult(
                                backend="pillow-fallback",
                                warnings=[f"selector_not_found: {selector}"],
                            )
                        loc.screenshot(
                            path=str(out_path),
                            animations="disabled",
                            timeout=timeout_ms,
                        )
                    else:
                        page.screenshot(
                            path=str(out_path),
                            full_page=full_page,
                            animations="disabled",
                            timeout=timeout_ms,
                        )
            finally:
                browser.close()
        scale, width_px, height_px = downsample_image_to_max_edge(out_path, max_edge)
        return BrowserRenderResult(
            backend="playwright",
            paths=[out_path],
            scale=scale,
            width_px=width_px,
            height_px=height_px,
        )
    except Exception as e:
        return BrowserRenderResult(
            backend="pillow-fallback",
            warnings=[f"playwright_capture_failed: {type(e).__name__}: {e}"],
        )


def _prime_local_media(page: object, *, timeout_ms: int) -> None:
    """Trigger native lazy loading before a full-page delivery screenshot."""
    page.evaluate(
        "() => { document.documentElement.style.scrollBehavior = 'auto'; }"
    )
    media = page.locator("img[loading='lazy'], picture img")
    count = min(media.count(), 200)
    for index in range(count):
        try:
            media.nth(index).scroll_into_view_if_needed(
                timeout=min(1_000, timeout_ms)
            )
        except Exception:
            continue
    if count:
        page.wait_for_timeout(100)
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(100)


def downsample_image_to_max_edge(
    path: Path,
    max_edge: int | None,
) -> tuple[float, int | None, int | None]:
    """Downsample an existing raster in-place when its long edge is too large."""
    if not max_edge or max_edge <= 0 or not path.exists():
        return 1.0, None, None
    try:
        from PIL import Image

        with Image.open(path) as img:
            width, height = img.size
            long_edge = max(width, height)
            if long_edge <= max_edge:
                return 1.0, width, height
            scale = max_edge / float(long_edge)
            next_size = (
                max(1, int(round(width * scale))),
                max(1, int(round(height * scale))),
            )
            resized = img.resize(next_size, Image.Resampling.LANCZOS)
            resized.save(path)
            return scale, next_size[0], next_size[1]
    except Exception:
        return 1.0, None, None


def screenshot_deck_slides(
    html_path: Path,
    slides_dir: Path,
    *,
    slide_w: int,
    slide_h: int,
    hide_selector: str | None = None,
) -> BrowserRenderResult:
    """Capture each ``.deck-slide`` element in a deck HTML file.

    ``hide_selector`` is used by hybrid PPTX export to hide editable text/table
    overlays while retaining the browser-rendered visual background.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return BrowserRenderResult(
            backend="pillow-fallback",
            warnings=[f"playwright_unavailable: {type(e).__name__}: {e}"],
        )

    try:
        slides_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        with sync_playwright() as p:
            browser = _launch_chromium(p)
            try:
                page = browser.new_page(
                    viewport={
                        "width": max(1, int(slide_w)),
                        "height": max(1, int(slide_h)),
                    },
                    device_scale_factor=1,
                )
                with _browser_document_session(page, html_path) as document_url:
                    page.goto(document_url, wait_until="load")
                    try:
                        page.wait_for_load_state("networkidle", timeout=2000)
                    except Exception:
                        pass
                    wait_for_autodesign_math(page)
                    if hide_selector:
                        page.add_style_tag(
                            content=(
                                f"{hide_selector} "
                                "{ visibility: hidden !important; }"
                            )
                        )
                    slides = page.locator(".deck-slide")
                    count = slides.count()
                    if count <= 0:
                        return BrowserRenderResult(
                            backend="pillow-fallback",
                            warnings=["deck_slide_selector_not_found: .deck-slide"],
                        )
                    for idx in range(count):
                        page.evaluate(
                            """index => {
                              const slides = Array.from(document.querySelectorAll('.deck-slide'));
                              slides.forEach((slide, slideIndex) => {
                                const active = slideIndex === index;
                                slide.classList.toggle('is-active', active);
                                slide.setAttribute('aria-hidden', active ? 'false' : 'true');
                                if (active) {
                                  slide.style.removeProperty('display');
                                  if (getComputedStyle(slide).display === 'none') {
                                    slide.style.setProperty('display', 'grid', 'important');
                                  }
                                } else {
                                  slide.style.setProperty('display', 'none', 'important');
                                }
                              });
                            }""",
                            idx,
                        )
                        out = slides_dir / f"slide_{idx:02d}.png"
                        slides.nth(idx).screenshot(path=str(out), animations="disabled")
                        paths.append(out)
            finally:
                browser.close()
        return BrowserRenderResult(backend="playwright", paths=paths)
    except Exception as e:
        return BrowserRenderResult(
            backend="pillow-fallback",
            warnings=[f"playwright_deck_capture_failed: {type(e).__name__}: {e}"],
        )


def export_deck_pdf(
    html_path: Path,
    pdf_path: Path,
    *,
    slide_w: int,
    slide_h: int,
    slide_pngs: list[Path] | None = None,
    timeout_ms: int = 15_000,
) -> BrowserRenderResult:
    """Export a deck HTML file to PDF.

    Playwright PDF is preferred. When Chromium is unavailable, fall back to an
    image PDF assembled from per-slide PNGs so deterministic smoke can still
    verify a real PDF artifact.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        fallback = _image_pdf_fallback(pdf_path, slide_pngs or [])
        fallback.warnings.insert(0, f"playwright_unavailable: {type(e).__name__}: {e}")
        return fallback

    try:
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = _launch_chromium(p)
            try:
                page = browser.new_page(
                    viewport={
                        "width": max(1, int(slide_w)),
                        "height": max(1, int(slide_h)),
                    },
                    device_scale_factor=1,
                )
                page.set_default_timeout(timeout_ms)
                page.set_default_navigation_timeout(timeout_ms)
                with _browser_document_session(page, html_path) as document_url:
                    page.goto(
                        document_url,
                        wait_until="load",
                        timeout=timeout_ms,
                    )
                    try:
                        page.wait_for_load_state(
                            "networkidle",
                            timeout=min(2000, timeout_ms),
                        )
                    except Exception:
                        pass
                    wait_for_autodesign_math(page, timeout_ms=min(3000, timeout_ms))
                    page.pdf(
                        path=str(pdf_path),
                        width=f"{max(1, int(slide_w))}px",
                        height=f"{max(1, int(slide_h))}px",
                        print_background=True,
                        margin={
                            "top": "0",
                            "right": "0",
                            "bottom": "0",
                            "left": "0",
                        },
                    )
            finally:
                browser.close()
        return BrowserRenderResult(backend="playwright-pdf", paths=[pdf_path])
    except Exception as e:
        fallback = _image_pdf_fallback(pdf_path, slide_pngs or [])
        fallback.warnings.insert(0, f"playwright_pdf_failed: {type(e).__name__}: {e}")
        return fallback


def export_html_pdf(
    html_path: Path,
    pdf_path: Path,
    *,
    viewport_width: int,
    viewport_height: int,
    page_width: str,
    page_height: str,
    fallback_pngs: list[Path] | None = None,
    enforce_single_page: bool = False,
    canvas_selector: str | None = None,
    canvas_width_px: int | None = None,
    canvas_height_px: int | None = None,
    timeout_ms: int = 15_000,
) -> BrowserRenderResult:
    """Export a single-frame HTML artifact to PDF with a physical page size.

    Poster exports can opt into a print-only viewport crop. This keeps the PDF
    faithful to the editable canvas instead of allowing document flow outside
    that canvas to paginate onto extra pages.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        fallback = _image_pdf_fallback(
            pdf_path,
            fallback_pngs or [],
            page_width=page_width if enforce_single_page else None,
            page_height=page_height if enforce_single_page else None,
        )
        fallback.warnings.insert(0, f"playwright_unavailable: {type(e).__name__}: {e}")
        return fallback

    try:
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = _launch_chromium(p)
            try:
                page = browser.new_page(
                    viewport={
                        "width": max(1, int(viewport_width)),
                        "height": max(1, int(viewport_height)),
                    },
                    device_scale_factor=1,
                )
                page.set_default_timeout(timeout_ms)
                page.set_default_navigation_timeout(timeout_ms)
                with _browser_document_session(page, html_path) as document_url:
                    page.goto(
                        document_url,
                        wait_until="load",
                        timeout=timeout_ms,
                    )
                    try:
                        page.wait_for_load_state(
                            "networkidle",
                            timeout=min(2000, timeout_ms),
                        )
                    except Exception:
                        pass
                    wait_for_autodesign_math(page, timeout_ms=min(3000, timeout_ms))
                    if enforce_single_page:
                        _prepare_single_page_pdf_crop(
                            page,
                            page_width=page_width,
                            page_height=page_height,
                            canvas_selector=canvas_selector,
                            canvas_width_px=canvas_width_px,
                            canvas_height_px=canvas_height_px,
                        )
                    page.pdf(
                        path=str(pdf_path),
                        width=page_width,
                        height=page_height,
                        print_background=True,
                        margin={
                            "top": "0",
                            "right": "0",
                            "bottom": "0",
                            "left": "0",
                        },
                    )
            finally:
                browser.close()
        if enforce_single_page:
            _verify_single_page_pdf(
                pdf_path,
                page_width=page_width,
                page_height=page_height,
            )
        return BrowserRenderResult(
            backend="playwright-pdf",
            paths=[pdf_path],
        )
    except Exception as e:
        pdf_path.unlink(missing_ok=True)
        fallback = _image_pdf_fallback(
            pdf_path,
            fallback_pngs or [],
            page_width=page_width if enforce_single_page else None,
            page_height=page_height if enforce_single_page else None,
        )
        fallback.warnings.insert(0, f"playwright_pdf_failed: {type(e).__name__}: {e}")
        return fallback


def _prepare_single_page_pdf_crop(
    page: object,
    *,
    page_width: str,
    page_height: str,
    canvas_selector: str | None,
    canvas_width_px: int | None,
    canvas_height_px: int | None,
) -> None:
    """Make one known canvas the only printable frame without touching source HTML."""

    if not canvas_selector or not canvas_width_px or not canvas_height_px:
        raise ValueError("single-page PDF export requires a canvas selector and dimensions")
    if canvas_width_px <= 0 or canvas_height_px <= 0:
        raise ValueError("single-page PDF export requires positive canvas dimensions")

    page.emulate_media(media="print")
    selected = page.evaluate(
        """selector => {
          const canvas = document.querySelector(selector);
          if (!canvas || canvas.parentElement !== document.body) return false;
          canvas.setAttribute('data-autodesign-pdf-canvas', 'true');
          return true;
        }""",
        canvas_selector,
    )
    if not selected:
        raise ValueError("single-page PDF canvas selector did not match a body-root canvas")
    page.add_style_tag(
        content=f"""
@media print {{
  @page {{ size: {page_width} {page_height}; margin: 0; }}
  html, body {{
    width: {page_width} !important;
    height: {page_height} !important;
    min-width: 0 !important;
    min-height: 0 !important;
    max-width: {page_width} !important;
    max-height: {page_height} !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow: hidden !important;
    overflow-clip-margin: 0 !important;
  }}
  body {{ position: relative !important; }}
  body > :not([data-autodesign-pdf-canvas="true"]) {{ display: none !important; }}
  [data-autodesign-pdf-canvas="true"] {{
    position: absolute !important;
    left: 0 !important;
    top: 0 !important;
    width: {canvas_width_px}px !important;
    height: {canvas_height_px}px !important;
    min-width: 0 !important;
    min-height: 0 !important;
    max-width: none !important;
    max-height: none !important;
    margin: 0 !important;
    overflow: hidden !important;
    overflow-clip-margin: 0 !important;
    clip-path: inset(0) !important;
    contain: layout paint style !important;
    isolation: isolate !important;
  }}
}}
"""
    )
    target = page.evaluate(
        """() => {
          const root = document.querySelector('[data-autodesign-pdf-canvas="true"]');
          const body = document.body.getBoundingClientRect();
          return {
            rootFound: Boolean(root),
            targetWidth: body.width,
            targetHeight: body.height,
          };
        }"""
    )
    target_width = float(target.get("targetWidth") or 0)
    target_height = float(target.get("targetHeight") or 0)
    if not target.get("rootFound") or target_width <= 0 or target_height <= 0:
        raise ValueError("single-page PDF print viewport could not be measured")
    scale_x = target_width / float(canvas_width_px)
    scale_y = target_height / float(canvas_height_px)
    if abs(scale_x - scale_y) > max(scale_x, scale_y) * 0.01:
        raise ValueError(
            "pdf_frame_aspect_ratio_mismatch: "
            f"canvas={canvas_width_px}x{canvas_height_px}, "
            f"page={target_width:.3f}x{target_height:.3f}"
        )
    scale = (scale_x + scale_y) / 2.0
    page.add_style_tag(
        content=f"""
@media print {{
  [data-autodesign-pdf-canvas="true"] {{
    transform: scale({scale:.8f}) !important;
    transform-origin: top left !important;
  }}
}}
"""
    )


_CSS_LENGTH_RE = re.compile(
    r"^\s*(?P<value>[0-9]+(?:\.[0-9]+)?)\s*(?P<unit>px|mm|cm|in|pt)\s*$",
    re.IGNORECASE,
)


def _pdf_page_size_points(page_width: str, page_height: str) -> tuple[float, float]:
    factors = {
        "px": 72.0 / 96.0,
        "mm": 72.0 / 25.4,
        "cm": 72.0 / 2.54,
        "in": 72.0,
        "pt": 1.0,
    }

    def points(raw: str) -> float:
        match = _CSS_LENGTH_RE.fullmatch(str(raw))
        if match is None:
            raise ValueError(f"unsupported PDF page dimension: {raw!r}")
        return float(match.group("value")) * factors[match.group("unit").lower()]

    return points(page_width), points(page_height)


def _verify_single_page_pdf(
    pdf_path: Path,
    *,
    page_width: str,
    page_height: str,
) -> None:
    """Ensure strict poster export produced one page at the requested size."""

    import fitz

    expected_width, expected_height = _pdf_page_size_points(page_width, page_height)
    with fitz.open(pdf_path) as document:
        if document.page_count != 1:
            raise ValueError(f"pdf_page_count={document.page_count}, expected=1")
        page = document[0]
        if (
            abs(page.rect.width - expected_width) > 2.0
            or abs(page.rect.height - expected_height) > 2.0
        ):
            raise ValueError(
                "pdf_page_size_mismatch: "
                f"actual={page.rect.width:.2f}x{page.rect.height:.2f}pt, "
                f"expected={expected_width:.2f}x{expected_height:.2f}pt"
            )


def _image_pdf_fallback(
    pdf_path: Path,
    slide_pngs: list[Path],
    *,
    page_width: str | None = None,
    page_height: str | None = None,
) -> BrowserRenderResult:
    if not slide_pngs:
        return BrowserRenderResult(
            backend="pdf-unavailable",
            warnings=["pdf_fallback_no_slide_pngs"],
        )
    try:
        existing_images = [path for path in slide_pngs if path.exists()]
        if not existing_images:
            return BrowserRenderResult(
                backend="pdf-unavailable",
                warnings=["pdf_fallback_no_readable_slide_pngs"],
            )
        if page_width is not None and page_height is not None:
            import fitz

            width_pt, height_pt = _pdf_page_size_points(page_width, page_height)
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_path.unlink(missing_ok=True)
            document = fitz.open()
            page = document.new_page(width=width_pt, height=height_pt)
            page.insert_image(page.rect, filename=str(existing_images[0]), keep_proportion=False)
            document.save(pdf_path)
            document.close()
            _verify_single_page_pdf(pdf_path, page_width=page_width, page_height=page_height)
            return BrowserRenderResult(
                backend="pymupdf-single-page-fallback",
                warnings=["playwright_pdf_unavailable_used_image_pdf"],
                paths=[pdf_path],
            )

        from PIL import Image

        images = []
        for path in existing_images:
            images.append(Image.open(path).convert("RGB"))
        if not images:
            return BrowserRenderResult(
                backend="pdf-unavailable",
                warnings=["pdf_fallback_no_readable_slide_pngs"],
            )
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        first, rest = images[0], images[1:]
        first.save(pdf_path, "PDF", save_all=True, append_images=rest, resolution=96.0)
        for image in images:
            image.close()
        return BrowserRenderResult(
            backend="pillow-pdf-fallback",
            warnings=["playwright_pdf_unavailable_used_image_pdf"],
            paths=[pdf_path],
        )
    except Exception as e:
        return BrowserRenderResult(
            backend="pdf-unavailable",
            warnings=[f"pdf_fallback_failed: {type(e).__name__}: {e}"],
        )


def _launch_chromium(p):
    try:
        return p.chromium.launch(args=["--no-sandbox"])
    except Exception as primary:
        allow_chrome = (
            os.getenv("AUTODESIGN_ALLOW_CHROME_CHANNEL_FALLBACK", "").strip()
            or os.getenv("DESIGN_ANYTHING_ALLOW_CHROME_CHANNEL_FALLBACK", "").strip()
        ).lower() in {"1", "true", "yes", "on"}
        if not allow_chrome:
            raise RuntimeError(
                f"{primary}; install Playwright Chromium with "
                "`uv run python -m playwright install chromium`"
            ) from primary
        try:
            return p.chromium.launch(channel="chrome", args=["--no-sandbox"])
        except Exception as secondary:
            raise RuntimeError(
                f"{primary}; chrome_channel_failed: {secondary}"
            ) from secondary
