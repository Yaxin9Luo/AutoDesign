from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import shutil
import tempfile
import threading
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from autodesign import attempt_candidates as attempt_candidates_module
from autodesign.attempt_candidates import attempt_promotion_lease
from autodesign.util import browser_render as browser_render_module
from autodesign.util.artifact_browser_audit import (
    audit_landing_html,
    audit_slides_html,
)
from autodesign.util.browser_render import (
    _launch_chromium,
    export_deck_pdf,
    export_html_pdf,
    screenshot_deck_slides,
    screenshot_html,
)


@contextmanager
def _recording_http_origin() -> Iterator[tuple[str, list[str]]]:
    requests: list[str] = []
    response_body = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">'
        b'<rect width="8" height="8" fill="red"/></svg>'
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _RendererTraceSession:
    def __init__(self, events: list[str]) -> None:
        self.url = "https://autodesign.invalid/final/document.html"
        self._events = events

    def install(self, _page: object) -> None:
        self._events.append("session.install")

    def close(self) -> None:
        self._events.append("session.close")


class _RendererTracePromotionContext:
    def __init__(
        self,
        events: list[str],
        *,
        final_assert_error: bool,
    ) -> None:
        self._events = events
        self._session = _RendererTraceSession(events)
        self._final_assert_error = final_assert_error

    def __enter__(self) -> _RendererTraceSession:
        self._events.append("accessor.enter")
        return self._session

    def __exit__(self, *_exc: object) -> None:
        self._events.append("accessor.exit_and_final_assert")
        if self._final_assert_error:
            raise RuntimeError("forced promotion final lease assertion failure")


class _RendererTraceLocator:
    def __init__(self, page: "_RendererTracePage", count: int) -> None:
        self._page = page
        self._count = count

    @property
    def first(self) -> "_RendererTraceLocator":
        return self

    def count(self) -> int:
        return self._count

    def nth(self, _index: int) -> "_RendererTraceLocator":
        return self

    def screenshot(self, **_kwargs: object) -> None:
        self._page.events.append("render.body")
        if self._page.screenshot_error == "locator":
            raise RuntimeError("forced locator screenshot render-body failure")


class _RendererTracePage:
    def __init__(
        self,
        events: list[str],
        *,
        selector_count: int = 1,
        deck_slide_count: int = 1,
        pdf_error: bool = False,
        screenshot_error: str | None = None,
    ) -> None:
        self.events = events
        self._selector_count = selector_count
        self._deck_slide_count = deck_slide_count
        self._pdf_error = pdf_error
        self.screenshot_error = screenshot_error

    def set_default_timeout(self, _timeout: int) -> None:
        return

    def set_default_navigation_timeout(self, _timeout: int) -> None:
        return

    def goto(self, *_args: object, **_kwargs: object) -> None:
        self.events.append("page.goto")

    def wait_for_load_state(self, *_args: object, **_kwargs: object) -> None:
        return

    def locator(self, selector: str) -> _RendererTraceLocator:
        count = (
            self._deck_slide_count
            if selector == ".deck-slide"
            else self._selector_count
        )
        return _RendererTraceLocator(self, count)

    def screenshot(self, **_kwargs: object) -> None:
        self.events.append("render.body")
        if self.screenshot_error == "page":
            raise RuntimeError("forced page screenshot render-body failure")

    def evaluate(self, *_args: object, **_kwargs: object) -> None:
        return

    def add_style_tag(self, **_kwargs: object) -> None:
        return

    def pdf(self, **_kwargs: object) -> None:
        self.events.append("render.body")
        if self._pdf_error:
            raise RuntimeError("forced PDF render-body failure")


class _RendererTraceBrowser:
    def __init__(self, events: list[str], page: _RendererTracePage) -> None:
        self._events = events
        self._page = page

    def new_page(self, **_kwargs: object) -> _RendererTracePage:
        return self._page

    def close(self) -> None:
        self._events.append("browser.close")


class _RendererTracePlaywright:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_exc: object) -> None:
        return


@contextmanager
def _renderer_teardown_trace(
    *,
    selector_count: int = 1,
    deck_slide_count: int = 1,
    pdf_error: bool = False,
    screenshot_error: str | None = None,
    final_assert_error: bool = False,
) -> Iterator[list[str]]:
    events: list[str] = []
    page = _RendererTracePage(
        events,
        selector_count=selector_count,
        deck_slide_count=deck_slide_count,
        pdf_error=pdf_error,
        screenshot_error=screenshot_error,
    )
    browser = _RendererTraceBrowser(events, page)

    def promotion_session(_path: Path) -> _RendererTracePromotionContext:
        return _RendererTracePromotionContext(
            events,
            final_assert_error=final_assert_error,
        )

    with (
        patch(
            "playwright.sync_api.sync_playwright",
            return_value=_RendererTracePlaywright(),
        ),
        patch.object(
            browser_render_module,
            "promotion_browser_document_session",
            side_effect=promotion_session,
        ),
        patch.object(
            browser_render_module,
            "_launch_chromium",
            return_value=browser,
        ),
        patch.object(browser_render_module, "wait_for_autodesign_math"),
    ):
        yield events


class ArtifactBrowserAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        layers = self.root / "layers"
        layers.mkdir()
        Image.new("RGB", (640, 360), (40, 100, 150)).save(layers / "figure.png")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_landing_rejects_offscreen_transform(self) -> None:
        path = self._write_landing(
            source_style="transform:translateX(-10000px);width:640px;height:360px",
        )

        report = audit_landing_html(path, required_source_ids=["fig-1"])

        self.assertEqual(report["backend"], "playwright")
        self.assertIn("landing_source_evidence_not_visible", self._finding_ids(report))

    def test_landing_rejects_one_pixel_ancestor_and_full_clip_path(self) -> None:
        cases = (
            ("width:1px;height:1px;overflow:hidden", "width:640px;height:360px"),
            ("", "width:640px;height:360px;clip-path:inset(100%)"),
        )
        for ancestor_style, source_style in cases:
            with self.subTest(ancestor_style=ancestor_style, source_style=source_style):
                path = self._write_landing(
                    ancestor_style=ancestor_style,
                    source_style=source_style,
                )
                report = audit_landing_html(path, required_source_ids=["fig-1"])
                self.assertIn(
                    "landing_source_evidence_not_visible",
                    self._finding_ids(report),
                )

    def test_landing_rejects_broken_image(self) -> None:
        path = self._write_landing(source_path="layers/missing.png")

        report = audit_landing_html(path, required_source_ids=["fig-1"])

        self.assertIn("landing_source_evidence_broken", self._finding_ids(report))

    def test_landing_blocks_network_by_default(self) -> None:
        path = self._write_landing(source_path="https://example.com/remote.png")

        report = audit_landing_html(path, required_source_ids=["fig-1"])

        self.assertGreater(report["metrics"]["blocked_request_count"], 0)
        self.assertIn("landing_runtime_network_request", self._finding_ids(report))
        self.assertIn("landing_source_evidence_broken", self._finding_ids(report))

    def test_landing_rejects_runtime_network_request_even_when_local_content_loads(self) -> None:
        path = self._write_landing(
            script="const beacon = new Image(); beacon.src = 'https://example.com/pixel.png'",
        )

        report = audit_landing_html(path, required_source_ids=["fig-1"])

        self.assertIn("landing_runtime_network_request", self._finding_ids(report))

    def test_landing_delivery_audit_ignores_mobile_only_layout_defects(self) -> None:
        path = self._write_landing(
            extra_style=(
                "@media (max-width:600px){"
                "main{width:640px;overflow:clip}"
                ".paper-source{display:none}"
                "}"
            ),
        )

        report = audit_landing_html(path, required_source_ids=["fig-1"])

        self.assertEqual(report["status"], "ok", report["findings"])
        self.assertEqual(
            sorted(report["metrics"]["snapshots"]),
            ["desktop_js_disabled", "desktop_js_enabled"],
        )
        self.assertFalse(
            any(
                item.get("evidence", {}).get("viewport") == "mobile"
                for item in report["findings"]
            )
        )

    def test_landing_rejects_javascript_only_reveal(self) -> None:
        path = self._write_landing(
            core_class="js-core",
            extra_style=".js-core{opacity:0}.ready .js-core{opacity:1}",
            script="document.documentElement.classList.add('ready')",
        )

        report = audit_landing_html(path, required_source_ids=["fig-1"])

        ids = self._finding_ids(report)
        self.assertIn("landing_core_content_js_dependent", ids)
        self.assertIn("landing_source_evidence_js_dependent", ids)
        self.assertNotIn("landing_content_clipped", ids)

    def test_landing_unpainted_text_is_not_reported_as_clipped(self) -> None:
        cases = (
            ("in_frame", ".hidden-core{opacity:0}"),
            (
                "off_frame",
                ".hidden-core{opacity:0;transform:translateX(-10000px)}",
            ),
            ("ancestor_hidden", ".hidden-core{visibility:hidden}"),
        )
        for label, extra_style in cases:
            with self.subTest(label=label):
                path = self._write_landing(
                    core_class="hidden-core",
                    extra_style=extra_style,
                )

                report = audit_landing_html(path, required_source_ids=["fig-1"])

                self.assertNotIn("landing_content_clipped", self._finding_ids(report))
                for snapshot in report["metrics"]["snapshots"].values():
                    self.assertFalse(snapshot["title_visible"])
                    self.assertEqual(snapshot["visible_section_count"], 0)
                    self.assertEqual(snapshot["visible_word_count"], 0)

    def test_landing_accepts_normal_below_fold_evidence(self) -> None:
        path = self._write_landing(before_source='<div style="height:1400px"></div>')

        report = audit_landing_html(path, required_source_ids=["fig-1"])

        self.assertEqual(report["status"], "ok", report["findings"])
        desktop = report["metrics"]["snapshots"]["desktop_js_enabled"]
        source = desktop["sources"][0]
        self.assertLess(source["raw_rect"]["top"], 900)
        self.assertTrue(source["effectively_visible"])

    def test_landing_measures_below_fold_evidence_inside_page_overflow_shell(self) -> None:
        path = self._write_landing(
            before_source='<div style="height:4000px"></div>',
            ancestor_tag="span",
            ancestor_style="display:inline;overflow:hidden",
            extra_style="main{overflow:clip}",
        )

        report = audit_landing_html(path, required_source_ids=["fig-1"])

        self.assertNotIn("landing_source_evidence_not_visible", self._finding_ids(report))
        self.assertEqual(report["status"], "ok", report["findings"])

    def test_landing_measures_each_reused_source_dom_node(self) -> None:
        path = self._write_landing(
            before_source=(
                '<img data-source-id="fig-1" src="layers/figure.png" '
                'style="display:none" alt="Hidden duplicate">'
                '<div style="height:1400px"></div>'
            ),
        )

        report = audit_landing_html(path, required_source_ids=["fig-1"])

        self.assertEqual(report["status"], "ok", report["findings"])
        sources = report["metrics"]["snapshots"]["desktop_js_enabled"]["sources"]
        self.assertFalse(sources[0]["effectively_visible"])
        self.assertTrue(sources[1]["effectively_visible"])

    def test_landing_checks_js_disabled_geometry(self) -> None:
        path = self._write_landing(
            extra_style=".wide{width:calc(100% + 100px)}.js .wide{width:100%}",
            core_class="wide",
            script="document.documentElement.classList.add('js')",
        )

        report = audit_landing_html(path, required_source_ids=["fig-1"])

        self.assertIn("landing_document_horizontal_overflow", self._finding_ids(report))

    def test_landing_primes_native_lazy_evidence_before_measurement(self) -> None:
        path = self._write_landing(
            before_source='<div style="height:4000px"></div>',
            source_loading="lazy",
        )

        report = audit_landing_html(path, required_source_ids=["fig-1"])

        self.assertEqual(report["status"], "ok", report["findings"])
        self.assertTrue(
            report["metrics"]["snapshots"]["desktop_js_enabled"]["sources"][0]["loaded"]
        )

    def test_landing_rejects_one_pixel_clipped_text(self) -> None:
        path = self._write_landing(
            core_class="clipped-copy",
            extra_style=".clipped-copy section{height:1px;overflow:hidden}",
        )

        report = audit_landing_html(path, required_source_ids=["fig-1"])

        self.assertIn("landing_content_clipped", self._finding_ids(report))

    def test_landing_does_not_scroll_inner_carousel_to_reveal_source(self) -> None:
        path = self._write_landing(
            ancestor_style="width:320px;overflow-x:auto;overflow-y:hidden",
            source_style="display:block;margin-left:640px;width:640px;height:360px",
        )

        report = audit_landing_html(path, required_source_ids=["fig-1"])

        self.assertIn("landing_source_evidence_not_visible", self._finding_ids(report))

    def test_slides_reject_offscreen_transform_and_clipped_evidence(self) -> None:
        cases = (
            ("", "transform:translateX(-10000px)"),
            ("width:1px;height:1px;overflow:hidden", ""),
            ("", "clip-path:inset(100%)"),
        )
        for ancestor_style, source_style in cases:
            with self.subTest(ancestor_style=ancestor_style, source_style=source_style):
                path = self._write_slides(
                    ancestor_style=ancestor_style,
                    source_style=source_style,
                )
                report = audit_slides_html(
                    path,
                    required_source_ids=["fig-1"],
                    expected_slide_count=3,
                )
                self.assertIn(
                    "slides_source_evidence_not_visible",
                    self._finding_ids(report),
                )

    def test_slides_reject_broken_image(self) -> None:
        path = self._write_slides(source_path="layers/missing.png")

        report = audit_slides_html(
            path,
            required_source_ids=["fig-1"],
            expected_slide_count=3,
        )

        self.assertIn("slides_source_evidence_broken", self._finding_ids(report))

    def test_slides_rejects_runtime_network_request(self) -> None:
        path = self._write_slides(
            script="const beacon = new Image(); beacon.src = 'https://example.com/pixel.png'",
        )

        report = audit_slides_html(
            path,
            required_source_ids=["fig-1"],
            expected_slide_count=3,
        )

        self.assertIn("slides_runtime_network_request", self._finding_ids(report))

    def test_slides_reject_javascript_only_reveal(self) -> None:
        path = self._write_slides(
            core_class="js-core",
            extra_style=".js-core{opacity:0}.ready .js-core{opacity:1}",
            script="document.documentElement.classList.add('ready')",
        )

        report = audit_slides_html(
            path,
            required_source_ids=["fig-1"],
            expected_slide_count=3,
        )

        ids = self._finding_ids(report)
        self.assertIn("slides_core_content_js_dependent", ids)
        self.assertIn("slides_source_evidence_js_dependent", ids)

    def test_slides_checks_js_disabled_geometry_and_clipping(self) -> None:
        path = self._write_slides(
            extra_style=(
                ".deck-slide{width:100px;height:100px;overflow:hidden}"
                ".js .deck-slide{width:1920px;height:1080px}"
            ),
            script="document.documentElement.classList.add('js')",
        )

        report = audit_slides_html(
            path,
            required_source_ids=["fig-1"],
            expected_slide_count=3,
        )

        ids = self._finding_ids(report)
        self.assertIn("slides_root_geometry_invalid", ids)
        self.assertIn("slides_content_clipped", ids)

    def test_slides_accept_normal_stacked_deck(self) -> None:
        path = self._write_slides()

        report = audit_slides_html(
            path,
            required_source_ids=["fig-1"],
            expected_slide_count=3,
        )

        self.assertEqual(report["status"], "ok", report["findings"])
        slides = report["metrics"]["snapshots"]["js_disabled"]["slides"]
        self.assertEqual(len(slides), 3)
        self.assertGreater(slides[1]["width"], 1919)
        self.assertTrue(slides[1]["sources"][0]["effectively_visible"])

    def test_slides_accept_single_active_slide_player(self) -> None:
        path = self._write_slides(
            extra_style=(
                ".deck-slide{display:none}"
                ".deck-slide.is-active{display:block}"
            ),
            first_slide_class="is-active",
            script=(
                "const slides=[...document.querySelectorAll('.deck-slide')];"
                "let active=0;"
                "const show=i=>{active=(i+slides.length)%slides.length;"
                "slides.forEach((slide,index)=>"
                "slide.classList.toggle('is-active',index===active));};"
                "addEventListener('keydown',event=>{"
                "if(event.key==='ArrowRight')show(active+1);"
                "if(event.key==='ArrowLeft')show(active-1);});"
            ),
        )

        report = audit_slides_html(
            path,
            required_source_ids=["fig-1"],
            expected_slide_count=3,
        )

        self.assertEqual(report["status"], "ok", report["findings"])
        for mode in ("js_enabled", "js_disabled"):
            slides = report["metrics"]["snapshots"][mode]["slides"]
            self.assertEqual(len(slides), 3)
            self.assertTrue(all(slide["effectively_visible"] for slide in slides))
            self.assertTrue(all(slide["width"] > 1919 for slide in slides))
        source = report["metrics"]["snapshots"]["js_disabled"]["slides"][1]["sources"][0]
        self.assertTrue(source["effectively_visible"])

    def test_slides_reject_visible_deck_navigation_controls(self) -> None:
        path = self._write_slides(
            extra_body=(
                '<nav class="deck-controls" aria-label="Slide navigation">'
                '<button aria-label="Previous slide">Previous</button>'
                '<button aria-label="Next slide">Next</button>'
                "</nav>"
            ),
        )

        report = audit_slides_html(
            path,
            required_source_ids=["fig-1"],
            expected_slide_count=3,
        )

        self.assertIn("slides_visible_navigation_controls", self._finding_ids(report))

    def test_screenshot_deck_slides_captures_single_active_slide_player(self) -> None:
        path = self._write_slides(
            extra_style=(
                ".deck-slide{display:none}"
                ".deck-slide.is-active{display:grid}"
            ),
            first_slide_class="is-active",
        )
        slides_dir = self.root / "slide-previews"

        result = screenshot_deck_slides(
            path,
            slides_dir,
            slide_w=1920,
            slide_h=1080,
        )

        self.assertEqual(result.backend, "playwright", result.warnings)
        self.assertEqual(len(result.paths), 3)
        for output_path in result.paths:
            self.assertTrue(output_path.is_file())
            with Image.open(output_path) as image:
                self.assertGreater(image.width, 1900)
                self.assertGreater(image.height, 1000)

    def test_screenshot_html_renders_document_during_promotion_lease(self) -> None:
        run_dir = self.root / "run-1"
        html_path = run_dir / "final" / "poster.html"
        html_path.parent.mkdir(parents=True)
        html_path.write_text(
            """<!doctype html>
<style>
  html, body, .paper-poster { margin: 0; width: 320px; height: 180px; }
  .paper-poster { background: rgb(11, 37, 71); }
</style>
<main class="paper-poster">Rendered poster</main>
""",
            encoding="utf-8",
        )
        preview_path = run_dir / "final" / "preview.png"

        with attempt_promotion_lease(run_dir) as leased_run_dir:
            result = screenshot_html(
                leased_run_dir / "final" / "poster.html",
                preview_path,
                viewport_width=320,
                viewport_height=180,
                selector=".paper-poster",
            )

        self.assertEqual(result.backend, "playwright", result.warnings)
        self.assertTrue(preview_path.is_file())
        with Image.open(preview_path) as image:
            pixel = image.getpixel((10, 10))[:3]
            self.assertTrue(
                all(abs(actual - expected) <= 1 for actual, expected in zip(
                    pixel,
                    (11, 37, 71),
                )),
                pixel,
            )

    def test_screenshot_deck_slides_renders_document_during_promotion_lease(self) -> None:
        run_dir = self.root / "run-1"
        html_path = run_dir / "final" / "deck.html"
        html_path.parent.mkdir(parents=True)
        html_path.write_text(
            """<!doctype html>
<style>
  html, body { margin: 0; }
  .deck-slide { width: 320px; height: 180px; background: rgb(53, 91, 137); }
</style>
<section class="deck-slide">Rendered slide</section>
""",
            encoding="utf-8",
        )
        slides_dir = run_dir / "final" / "slides"

        with attempt_promotion_lease(run_dir) as leased_run_dir:
            result = screenshot_deck_slides(
                leased_run_dir / "final" / "deck.html",
                slides_dir,
                slide_w=320,
                slide_h=180,
            )

        self.assertEqual(result.backend, "playwright", result.warnings)
        self.assertEqual(len(result.paths), 1)
        with Image.open(result.paths[0]) as image:
            pixel = image.getpixel((10, 10))[:3]
            self.assertTrue(
                all(abs(actual - expected) <= 3 for actual, expected in zip(
                    pixel,
                    (53, 91, 137),
                )),
                pixel,
            )

    def test_promotion_route_blocks_metadata_external_links_symlinks_and_network(
        self,
    ) -> None:
        run_dir = self.root / "run-1"
        final_dir = run_dir / "final"
        assets_dir = run_dir / "assets"
        final_dir.mkdir(parents=True)
        assets_dir.mkdir()
        (run_dir / "run_control.json").write_text(
            '{"secret":"RUN_METADATA_SECRET"}',
            encoding="utf-8",
        )
        outside_path = self.root / "outside.html"
        outside_path.write_text(
            "<!doctype html><body>RUN_EXTERNAL_SECRET</body>",
            encoding="utf-8",
        )
        (assets_dir / "allowed.css").write_text(
            ".paper-poster { background: rgb(31, 47, 63); }",
            encoding="utf-8",
        )
        allowed_image = assets_dir / "allowed.png"
        Image.new("RGB", (16, 16), (23, 199, 89)).save(allowed_image)
        secret_svg = assets_dir / "secret.svg"
        secret_svg.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"><text>SYMLINK_SECRET</text></svg>',
            encoding="utf-8",
        )
        (assets_dir / "linked.svg").symlink_to(secret_svg)

        with _recording_http_origin() as (network_origin, network_requests):
            html_path = final_dir / "poster.html"
            html_path.write_text(
                f"""<!doctype html>
<link rel="stylesheet" href="../assets/allowed.css">
<main class="paper-poster" style="width:320px;height:180px">
  <img id="allowed" src="../assets/allowed.png" width="16" height="16">
  <iframe src="../run_control.json"></iframe>
  <iframe src="{outside_path.as_uri()}"></iframe>
  <iframe src="../assets/linked.svg"></iframe>
  <img id="network" src="{network_origin}/probe.svg">
</main>
""",
                encoding="utf-8",
            )
            session_factory = getattr(
                attempt_candidates_module,
                "promotion_browser_document_session",
                None,
            )
            self.assertTrue(
                callable(session_factory),
                "promotion rendering requires a lease-bound routed document session",
            )
            assert session_factory is not None

            with attempt_promotion_lease(run_dir) as leased_run_dir:
                with session_factory(
                    leased_run_dir / "final" / "poster.html"
                ) as document_session:
                    from playwright.sync_api import sync_playwright

                    with sync_playwright() as playwright:
                        browser = _launch_chromium(playwright)
                        page = browser.new_page(
                            viewport={"width": 320, "height": 180},
                            device_scale_factor=1,
                        )
                        page.set_default_timeout(1_000)
                        document_session.install(page)
                        page.goto(document_session.url, wait_until="load")
                        readable_frames = [
                            frame
                            for frame in page.frames
                            if frame.url
                            and not frame.url.startswith(
                                ("about:blank", "chrome-error:")
                            )
                        ]
                        frame_text = "\n".join(
                            frame.locator("body").inner_text()
                            for frame in readable_frames
                        )
                        background = page.locator(".paper-poster").evaluate(
                            "element => getComputedStyle(element).backgroundColor"
                        )
                        allowed_loaded = page.locator("#allowed").evaluate(
                            "element => element.naturalWidth"
                        )
                        network_loaded = page.locator("#network").evaluate(
                            "element => element.naturalWidth"
                        )
                        allowed_preview = final_dir / "allowed-preview.png"
                        page.locator("#allowed").screenshot(path=str(allowed_preview))
                        browser.close()

            self.assertNotIn("RUN_METADATA_SECRET", frame_text)
            self.assertNotIn("RUN_EXTERNAL_SECRET", frame_text)
            self.assertNotIn("SYMLINK_SECRET", frame_text)
            self.assertEqual(network_requests, [])
            self.assertEqual(background, "rgb(31, 47, 63)")
            self.assertEqual(allowed_loaded, 16)
            self.assertEqual(network_loaded, 0)
            with Image.open(allowed_preview) as image:
                self.assertEqual(image.getpixel((8, 8))[:3], (23, 199, 89))

    def test_explicit_browser_resources_use_one_immutable_checked_snapshot(
        self,
    ) -> None:
        run_dir = self.root / "run-explicit-snapshot"
        attempt_dir = run_dir / "landing_author" / "attempt_01"
        assets_dir = attempt_dir / "assets"
        assets_dir.mkdir(parents=True)
        (attempt_dir / "index.html").write_text(
            """<!doctype html>
<link rel="stylesheet" href="assets/style.css">
<main id="style-probe">
  <span id="font-probe">Snapshot Font Probe</span>
  <img id="image-probe" src="assets/figure.png" width="16" height="16">
  <iframe src="private.json"></iframe>
  <img id="network-probe" src="NETWORK_ORIGIN/probe.svg">
</main>
""",
            encoding="utf-8",
        )
        (assets_dir / "style.css").write_text(
            """@font-face {
  font-family: SnapshotFont;
  src: url('font.woff2') format('woff2');
}
html, body { margin: 0; }
#style-probe { width: 320px; height: 180px; background: rgb(19, 61, 103); }
#font-probe { font: 32px SnapshotFont; }
""",
            encoding="utf-8",
        )
        Image.new("RGB", (16, 16), (17, 203, 91)).save(
            assets_dir / "figure.png"
        )
        shutil.copyfile(
            Path(__file__).resolve().parents[1]
            / "assets/vendor/katex/fonts/KaTeX_Main-Regular.woff2",
            assets_dir / "font.woff2",
        )
        (attempt_dir / "private.json").write_text(
            '{"secret":"INTERNAL_RESOURCE_SECRET"}',
            encoding="utf-8",
        )
        (attempt_dir / "preview.png").write_bytes(b"preview")
        (attempt_dir / "validation.json").write_text("{}", encoding="utf-8")

        with _recording_http_origin() as (network_origin, network_requests):
            html_path = attempt_dir / "index.html"
            html_path.write_text(
                html_path.read_text(encoding="utf-8").replace(
                    "NETWORK_ORIGIN",
                    network_origin,
                ),
                encoding="utf-8",
            )
            candidate = attempt_candidates_module.capture_attempt_candidate(
                run_dir=run_dir,
                attempt_dir=attempt_dir,
                artifact_type="landing",
                attempt=1,
                max_attempts=1,
                source_path="index.html",
                dependency_paths=[
                    "assets/style.css",
                    "assets/figure.png",
                    "assets/font.woff2",
                    "private.json",
                ],
                browser_resource_paths=[
                    "assets/style.css",
                    "assets/figure.png",
                    "assets/font.woff2",
                ],
                preview_paths=["preview.png"],
                validation_summary_path="validation.json",
                safety_state="ready",
                hard_blockers=[],
                warnings=[],
            )
            response_types: dict[str, str] = {}

            from playwright.sync_api import sync_playwright

            with attempt_promotion_lease(run_dir) as leased_run_dir:
                with sync_playwright() as playwright:
                    browser = _launch_chromium(playwright)
                    try:
                        page = browser.new_page(
                            viewport={"width": 320, "height": 180},
                            device_scale_factor=1,
                        )
                        page.set_default_timeout(2_000)
                        page.on(
                            "response",
                            lambda response: response_types.__setitem__(
                                response.url.rsplit("/", 1)[-1],
                                response.headers.get("content-type", ""),
                            ),
                        )
                        with attempt_candidates_module.promotion_browser_document_session(
                            leased_run_dir / candidate.source_relative_path
                        ) as document_session:
                            replacements = {
                                "style.css": (
                                    b"#style-probe { background: rgb(191, 17, 29); }"
                                ),
                                "font.woff2": b"not-a-valid-font",
                            }
                            for relative_value in (
                                candidate.browser_resource_relative_paths or []
                            ):
                                resource = run_dir / relative_value
                                replacement = resource.with_name(
                                    f"{resource.name}.replacement"
                                )
                                if resource.name == "figure.png":
                                    Image.new("RGB", (16, 16), (211, 31, 47)).save(
                                        replacement,
                                        format="PNG",
                                    )
                                else:
                                    replacement.write_bytes(replacements[resource.name])
                                replacement.replace(resource)

                            document_session.install(page)
                            page.goto(document_session.url, wait_until="load")
                            page.wait_for_load_state("networkidle", timeout=2_000)
                            style_background = page.locator("#style-probe").evaluate(
                                "element => getComputedStyle(element).backgroundColor"
                            )
                            image_width = page.locator("#image-probe").evaluate(
                                "element => element.naturalWidth"
                            )
                            font_loaded = page.evaluate(
                                """async () => {
                                  await document.fonts.ready;
                                  return document.fonts.check(
                                    '32px SnapshotFont',
                                    'Snapshot Font Probe'
                                  );
                                }"""
                            )
                            frame_text = "\n".join(
                                frame.locator("body").inner_text()
                                for frame in page.frames
                                if frame.url
                                and not frame.url.startswith(
                                    ("about:blank", "chrome-error:")
                                )
                            )
                            image_preview = run_dir / "retained-image.png"
                            page.locator("#image-probe").screenshot(
                                path=str(image_preview)
                            )
                    finally:
                        browser.close()

            self.assertEqual(style_background, "rgb(19, 61, 103)")
            self.assertEqual(image_width, 16)
            self.assertTrue(font_loaded)
            self.assertNotIn("INTERNAL_RESOURCE_SECRET", frame_text)
            self.assertEqual(network_requests, [])
            self.assertTrue(response_types.get("style.css", "").startswith("text/css"))
            self.assertTrue(response_types.get("figure.png", "").startswith("image/png"))
            self.assertTrue(response_types.get("font.woff2", "").startswith("font/woff2"))
            with Image.open(image_preview) as image:
                self.assertEqual(image.getpixel((8, 8))[:3], (17, 203, 91))

    def test_export_renderers_share_promotion_route_boundary(self) -> None:
        run_dir = self.root / "run-1"
        final_dir = run_dir / "final"
        final_dir.mkdir(parents=True)

        with _recording_http_origin() as (network_origin, network_requests):
            html_path = final_dir / "document.html"
            html_path.write_text(
                f"""<!doctype html>
<style>
  html, body {{ margin: 0; }}
  .paper-poster, .deck-slide {{ width: 320px; height: 180px; background: #123456; }}
</style>
<main class="paper-poster">
  <section class="deck-slide">Routed document</section>
  <img src="{network_origin}/escape.svg">
</main>
""",
                encoding="utf-8",
            )
            poster_preview = final_dir / "preview.png"
            slides_dir = final_dir / "slides"
            deck_pdf = final_dir / "deck.pdf"
            html_pdf = final_dir / "document.pdf"

            with attempt_promotion_lease(run_dir) as leased_run_dir:
                leased_html = leased_run_dir / "final" / "document.html"
                screenshot_result = screenshot_html(
                    leased_html,
                    poster_preview,
                    viewport_width=320,
                    viewport_height=180,
                    selector=".paper-poster",
                )
                deck_result = screenshot_deck_slides(
                    leased_html,
                    slides_dir,
                    slide_w=320,
                    slide_h=180,
                )
                deck_pdf_result = export_deck_pdf(
                    leased_html,
                    deck_pdf,
                    slide_w=320,
                    slide_h=180,
                    slide_pngs=deck_result.paths,
                )
                html_pdf_result = export_html_pdf(
                    leased_html,
                    html_pdf,
                    viewport_width=320,
                    viewport_height=180,
                    page_width="320px",
                    page_height="180px",
                    fallback_pngs=[poster_preview],
                )

            self.assertEqual(screenshot_result.backend, "playwright", screenshot_result.warnings)
            self.assertEqual(deck_result.backend, "playwright", deck_result.warnings)
            self.assertEqual(deck_pdf_result.backend, "playwright-pdf", deck_pdf_result.warnings)
            self.assertEqual(html_pdf_result.backend, "playwright-pdf", html_pdf_result.warnings)
            self.assertEqual(
                network_requests,
                [],
                "all four renderers must block requests outside the synthetic origin",
            )

    def test_all_four_renderers_close_session_accessor_then_browser_once(self) -> None:
        expected_backends = {
            "screenshot_html": "playwright",
            "screenshot_deck": "playwright",
            "deck_pdf": "playwright-pdf",
            "html_pdf": "playwright-pdf",
        }
        for renderer in ("screenshot_html", "screenshot_deck", "deck_pdf", "html_pdf"):
            with self.subTest(renderer=renderer):
                with _renderer_teardown_trace() as events:
                    result = self._invoke_traced_renderer(renderer)

                self.assertEqual(result.backend, expected_backends[renderer])
                self.assertEqual(result.warnings, [])
                self._assert_renderer_teardown(events, expected_render_body_count=1)

    def test_screenshot_selector_missing_still_uses_ordered_teardown(self) -> None:
        with _renderer_teardown_trace(selector_count=0) as events:
            result = screenshot_html(
                self.root / "missing-selector.html",
                self.root / "missing-selector.png",
                viewport_width=320,
                viewport_height=180,
                selector=".paper-poster",
            )

        self.assertIn("selector_not_found: .paper-poster", result.warnings)
        self.assertEqual(result.backend, "pillow-fallback")
        self._assert_renderer_teardown(events, expected_render_body_count=0)

    def test_zero_slide_deck_still_uses_ordered_teardown(self) -> None:
        with _renderer_teardown_trace(deck_slide_count=0) as events:
            result = screenshot_deck_slides(
                self.root / "zero-slides.html",
                self.root / "zero-slides",
                slide_w=320,
                slide_h=180,
            )

        self.assertIn(
            "deck_slide_selector_not_found: .deck-slide",
            result.warnings,
        )
        self.assertEqual(result.backend, "pillow-fallback")
        self._assert_renderer_teardown(events, expected_render_body_count=0)

    def test_screenshot_body_failures_still_use_ordered_teardown(self) -> None:
        cases = (
            ("screenshot_html_page", "page"),
            ("screenshot_html", "locator"),
            ("screenshot_deck", "locator"),
        )
        for renderer, screenshot_error in cases:
            with self.subTest(
                renderer=renderer,
                screenshot_error=screenshot_error,
            ):
                with _renderer_teardown_trace(
                    screenshot_error=screenshot_error
                ) as events:
                    result = self._invoke_traced_renderer(renderer)

                self.assertEqual(result.backend, "pillow-fallback")
                self.assertTrue(
                    any(
                        f"forced {screenshot_error} screenshot render-body failure"
                        in item
                        for item in result.warnings
                    ),
                    result.warnings,
                )
                self._assert_renderer_teardown(
                    events,
                    expected_render_body_count=1,
                )

    def test_both_pdf_body_failures_still_use_ordered_teardown(self) -> None:
        for renderer in ("deck_pdf", "html_pdf"):
            with self.subTest(renderer=renderer):
                with _renderer_teardown_trace(pdf_error=True) as events:
                    result = self._invoke_traced_renderer(renderer)

                self.assertTrue(
                    any("forced PDF render-body failure" in item for item in result.warnings),
                    result.warnings,
                )
                self.assertEqual(result.backend, "pdf-unavailable")
                self._assert_renderer_teardown(
                    events,
                    expected_render_body_count=1,
                )

    def test_final_lease_assert_failure_closes_every_renderer_browser(self) -> None:
        for renderer in ("screenshot_html", "screenshot_deck", "deck_pdf", "html_pdf"):
            with self.subTest(renderer=renderer):
                with _renderer_teardown_trace(final_assert_error=True) as events:
                    result = self._invoke_traced_renderer(renderer)

                self.assertTrue(
                    any(
                        "forced promotion final lease assertion failure" in item
                        for item in result.warnings
                    ),
                    result.warnings,
                )
                expected_backend = (
                    "pillow-fallback"
                    if renderer.startswith("screenshot_")
                    else "pdf-unavailable"
                )
                self.assertEqual(result.backend, expected_backend)
                self._assert_renderer_teardown(
                    events,
                    expected_render_body_count=1,
                )

    def test_slides_accept_responsive_scale_with_1920_by_1080_logical_frame(self) -> None:
        path = self._write_slides(
            extra_style=".deck-slide{transform:scale(.975);transform-origin:top left}",
        )

        report = audit_slides_html(
            path,
            required_source_ids=["fig-1"],
            expected_slide_count=3,
        )

        self.assertEqual(report["status"], "ok", report["findings"])
        slide = report["metrics"]["snapshots"]["js_enabled"]["slides"][1]
        self.assertEqual(slide["layout_width"], 1920)
        self.assertLess(slide["width"], 1920)

    def test_slides_ignore_clipped_decorative_pseudo_overflow(self) -> None:
        path = self._write_slides(
            extra_style=(
                ".deck-slide{overflow:hidden}"
                ".deck-slide::after{content:'';position:absolute;width:500px;height:500px;"
                "right:-250px;bottom:-250px;background:#eee;border-radius:50%}"
            ),
        )

        report = audit_slides_html(
            path,
            required_source_ids=["fig-1"],
            expected_slide_count=3,
        )

        self.assertNotIn("slides_internal_overflow", self._finding_ids(report))
        self.assertEqual(report["status"], "ok", report["findings"])

    def test_slides_reject_scrollable_slide_overflow(self) -> None:
        path = self._write_slides(
            extra_style=".deck-slide{overflow:auto}.deck-slide p{width:2200px}",
        )

        report = audit_slides_html(
            path,
            required_source_ids=["fig-1"],
            expected_slide_count=3,
        )

        self.assertIn("slides_internal_overflow", self._finding_ids(report))

    def test_slides_reject_clipped_plain_div_and_span_text(self) -> None:
        path = self._write_slides(
            extra_slide_content=(
                '<div style="height:1px;overflow:hidden">'
                '<span>Important scientific explanation clipped outside its container.</span>'
                "</div>"
            ),
        )

        report = audit_slides_html(
            path,
            required_source_ids=["fig-1"],
            expected_slide_count=3,
        )

        self.assertIn("slides_content_clipped", self._finding_ids(report))

    def _invoke_traced_renderer(self, renderer: str):
        html_path = self.root / f"{renderer}.html"
        if renderer in {"screenshot_html", "screenshot_html_page"}:
            return screenshot_html(
                html_path,
                self.root / f"{renderer}.png",
                viewport_width=320,
                viewport_height=180,
                selector=(
                    None if renderer == "screenshot_html_page" else ".paper-poster"
                ),
            )
        if renderer == "screenshot_deck":
            return screenshot_deck_slides(
                html_path,
                self.root / f"{renderer}-slides",
                slide_w=320,
                slide_h=180,
            )
        if renderer == "deck_pdf":
            return export_deck_pdf(
                html_path,
                self.root / f"{renderer}.pdf",
                slide_w=320,
                slide_h=180,
            )
        if renderer == "html_pdf":
            return export_html_pdf(
                html_path,
                self.root / f"{renderer}.pdf",
                viewport_width=320,
                viewport_height=180,
                page_width="320px",
                page_height="180px",
            )
        raise AssertionError(f"unknown traced renderer: {renderer}")

    def _assert_renderer_teardown(
        self,
        events: list[str],
        *,
        expected_render_body_count: int,
    ) -> None:
        self.assertEqual(events.count("accessor.enter"), 1, events)
        self.assertEqual(events.count("session.install"), 1, events)
        self.assertEqual(events.count("page.goto"), 1, events)
        self.assertEqual(
            events.count("render.body"),
            expected_render_body_count,
            events,
        )
        lifecycle = [
            event
            for event in events
            if event
            in {
                "session.close",
                "accessor.exit_and_final_assert",
                "browser.close",
            }
        ]
        self.assertEqual(
            lifecycle,
            [
                "session.close",
                "accessor.exit_and_final_assert",
                "browser.close",
            ],
            events,
        )
        for event in lifecycle:
            self.assertEqual(events.count(event), 1, events)
        close_index = events.index("session.close")
        self.assertLess(events.index("page.goto"), close_index, events)
        for index, event in enumerate(events):
            if event == "render.body":
                self.assertLess(index, close_index, events)

    def _write_landing(
        self,
        *,
        source_path: str = "layers/figure.png",
        source_style: str = "width:100%;max-width:640px;height:auto",
        ancestor_style: str = "",
        ancestor_tag: str = "div",
        before_source: str = "",
        core_class: str = "",
        extra_style: str = "",
        script: str = "",
        source_loading: str = "",
    ) -> Path:
        words = " ".join(["grounded"] * 40)
        path = self.root / "landing.html"
        path.write_text(
            f"""<!doctype html><html><head>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
              *{{box-sizing:border-box}} html,body{{margin:0;max-width:100%}}
              main{{width:100%}} section{{padding:24px}}
              {extra_style}
            </style></head><body><main class="{core_class}">
              <section><h1>Audited Paper</h1><p>{words}</p>
                {before_source}<{ancestor_tag} style="{ancestor_style}">
                  <img class="paper-source" data-source-id="fig-1"
                    src="{source_path}" loading="{source_loading}" style="{source_style}" alt="Paper figure">
                </{ancestor_tag}>
              </section>
              <section><h2>Method</h2><p>{words}</p></section>
              <section><h2>Results</h2><p>{words}</p></section>
            </main><script>{script}</script></body></html>""",
            encoding="utf-8",
        )
        return path

    def _write_slides(
        self,
        *,
        source_path: str = "layers/figure.png",
        source_style: str = "",
        ancestor_style: str = "",
        core_class: str = "",
        extra_style: str = "",
        script: str = "",
        extra_slide_content: str = "",
        first_slide_class: str = "",
        extra_body: str = "",
    ) -> Path:
        copy = " ".join(["evidence"] * 30)
        path = self.root / "slides.html"
        path.write_text(
            f"""<!doctype html><html><head><style>
              *{{box-sizing:border-box}} html,body{{margin:0}}
              .deck-slide{{position:relative;width:1920px;height:1080px;
                overflow:hidden;padding:64px}}
              .paper-source{{display:block;width:640px;height:360px;{source_style}}}
              {extra_style}
            </style></head><body>
              <section class="deck-slide {first_slide_class}" id="slide-1"><div class="{core_class}">
                <h1>Audited Deck</h1><p>{copy}</p></div></section>
              <section class="deck-slide" id="slide-2"><div class="{core_class}">
                <h2>Method</h2><p>{copy}</p><div style="{ancestor_style}">
                  <figure><img class="paper-source" data-source-id="fig-1"
                    src="{source_path}" alt="Paper evidence">
                    <figcaption>Original paper evidence with a local interpretation.</figcaption>
                  </figure>
                </div><div data-visual-unit="diagram">Input to model to output.</div>
              </div></section>
              <section class="deck-slide" id="slide-3"><div class="{core_class}">
                <h2>Takeaways</h2><p>{copy}</p>{extra_slide_content}</div></section>
              {extra_body}
              <script>{script}</script>
            </body></html>""",
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _finding_ids(report: dict) -> set[str]:
        return {str(item.get("id") or "") for item in report.get("findings") or []}


if __name__ == "__main__":
    unittest.main()
