from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / "web"
FIXTURE_PATH = "/tests/browser/canvasDeckNavigation.html"


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_fixture(process: subprocess.Popen[str], url: str) -> None:
    deadline = time.monotonic() + 20
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise AssertionError(
                f"Vite exited before serving the fixture ({process.returncode}).\n{output}"
            )
        try:
            with urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, URLError) as error:
            last_error = error
        time.sleep(0.05)
    raise AssertionError(f"Vite did not serve {url}: {last_error}")


def _terminate_vite(process: subprocess.Popen[str]) -> str:
    if process.poll() is None:
        process.terminate()
    try:
        output, _ = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate(timeout=5)
    return output


class CanvasDeckNavigationBrowserTests(unittest.TestCase):
    def test_canvas_navigates_stacked_and_script_driven_decks(self) -> None:
        vite = WEB_ROOT / "node_modules" / ".bin" / "vite"
        self.assertTrue(vite.is_file(), f"Declared Vite executable is missing: {vite}")
        port = _free_loopback_port()
        fixture_url = f"http://127.0.0.1:{port}{FIXTURE_PATH}"
        process: subprocess.Popen[str] | None = None

        with tempfile.TemporaryDirectory(prefix="autodesign-deck-nav-") as temp_dir:
            env = {**os.environ, "NO_COLOR": "1", "TMPDIR": temp_dir}
            try:
                process = subprocess.Popen(
                    [
                        str(vite),
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--strictPort",
                    ],
                    cwd=WEB_ROOT,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                _wait_for_fixture(process, fixture_url)

                from playwright.sync_api import sync_playwright

                from autodesign.util.browser_render import _launch_chromium

                with sync_playwright() as playwright:
                    browser = _launch_chromium(playwright)
                    context = browser.new_context(viewport={"width": 1440, "height": 1000})
                    context.add_init_script("window.localStorage.clear()")
                    page = context.new_page()
                    page.set_default_timeout(5_000)
                    page_errors: list[str] = []
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    try:
                        page.goto(fixture_url, wait_until="load")
                        page.wait_for_function(
                            "() => Boolean(window.__canvasDeckNavigationHarness)"
                        )
                        page.wait_for_load_state("networkidle")
                        stacked_source = page.evaluate(
                            "window.__canvasDeckNavigationHarness.load('stacked', 'authored-b')"
                        )
                        self._wait_for_deck(
                            page,
                            expected_slides=3,
                            expected_source=stacked_source,
                        )

                        stacked = page.evaluate(
                            "window.__canvasDeckNavigationHarness.snapshot()"
                        )
                        self.assertEqual(stacked["mode"], "stacked")
                        self.assertEqual(stacked["hash"], "#authored-b")
                        self.assertEqual(
                            [slide["painted"] for slide in stacked["slides"]],
                            [True, True, True],
                        )
                        self.assertEqual(
                            [slide["ariaHidden"] for slide in stacked["slides"]],
                            [None, None, None],
                        )
                        self.assertFalse(stacked["authorScriptRan"])
                        self.assertEqual(stacked["progress"], "2 / 3")
                        self.assertEqual(
                            stacked["thumbnailPressed"],
                            ["false", "true", "false"],
                        )
                        self.assertEqual(stacked["thumbnailScrollY"], [0, 720, 1440])

                        page.evaluate(
                            "window.__canvasDeckNavigationHarness.clickThumbnail(2)"
                        )
                        page.wait_for_function(
                            """() => {
                              const snapshot = window.__canvasDeckNavigationHarness.snapshot();
                              return snapshot.hash === '#3' && snapshot.scrollY > 0;
                            }"""
                        )
                        stacked_after_click = page.evaluate(
                            "window.__canvasDeckNavigationHarness.snapshot()"
                        )
                        self.assertGreater(stacked_after_click["scrollY"], 0)
                        self.assertEqual(
                            stacked_after_click["thumbnailPaintedIndexes"],
                            [0, 0, 0],
                        )
                        self.assertEqual(stacked_after_click["progress"], "3 / 3")

                        real_source = page.evaluate(
                            "window.__canvasDeckNavigationHarness.load('real-main-18')"
                        )
                        self._wait_for_deck(
                            page,
                            expected_slides=18,
                            expected_source=real_source,
                        )
                        page.wait_for_function(
                            """() => {
                              const iframe = document.querySelector(
                                'iframe[title="Deck navigation regression"]'
                              );
                              const nodes = iframe?.contentDocument?.querySelectorAll(
                                '[data-kind="text"][data-layer-id]'
                              );
                              return nodes?.length === 36
                                && [...nodes].every((node) => node.isContentEditable);
                            }"""
                        )
                        real_initial = page.evaluate(
                            "window.__canvasDeckNavigationHarness.snapshot()"
                        )
                        self.assertEqual(real_initial["mode"], "player")
                        self.assertEqual(real_initial["slideCount"], 18)
                        self.assertFalse(real_initial["authorScriptRan"])
                        self.assertAlmostEqual(
                            real_initial["mainGeometry"]["slideLeft"], 0, delta=1
                        )
                        self.assertAlmostEqual(
                            real_initial["mainGeometry"]["slideTop"], 0, delta=1
                        )
                        for geometry in real_initial["thumbnailGeometry"]:
                            self.assertEqual(geometry["viewportWidth"], 1920)
                            self.assertEqual(geometry["viewportHeight"], 1080)
                            self.assertAlmostEqual(geometry["slideLeft"], 0, delta=1)
                            self.assertAlmostEqual(geometry["slideTop"], 0, delta=1)
                            self.assertAlmostEqual(geometry["slideWidth"], 1920, delta=1)
                            self.assertAlmostEqual(geometry["slideHeight"], 1080, delta=1)

                        first_title = page.evaluate(
                            "window.__canvasDeckNavigationHarness.editSlideTitle"
                            "(0, 'Edited first slide')"
                        )
                        self.assertEqual(first_title, "title-1")
                        first_edit = page.evaluate(
                            "window.__canvasDeckNavigationHarness.snapshot()"
                        )
                        self.assertEqual(first_edit["selectedLayerIds"], ["title-1"])
                        self.assertEqual(
                            first_edit["pendingLayerEdits"]["title-1"]["text"],
                            "Edited first slide",
                        )
                        first_image = page.evaluate(
                            "window.__canvasDeckNavigationHarness.selectEditable(0, 'image')"
                        )
                        self.assertEqual(first_image, "image-1")
                        self.assertEqual(
                            page.evaluate(
                                "window.__canvasDeckNavigationHarness.snapshot().selectedLayerIds"
                            ),
                            ["image-1"],
                        )

                        page.evaluate(
                            "window.__canvasDeckNavigationHarness.clickThumbnail(17)"
                        )
                        page.wait_for_function(
                            "() => window.__canvasDeckNavigationHarness.snapshot().progress === '18 / 18'"
                        )
                        last_title = page.evaluate(
                            "window.__canvasDeckNavigationHarness.editSlideTitle"
                            "(17, 'Edited last slide')"
                        )
                        self.assertEqual(last_title, "title-18")
                        last_image = page.evaluate(
                            "window.__canvasDeckNavigationHarness.selectEditable(17, 'image')"
                        )
                        self.assertEqual(last_image, "image-18")
                        real_edited = page.evaluate(
                            "window.__canvasDeckNavigationHarness.snapshot()"
                        )
                        self.assertEqual(real_edited["selectedLayerIds"], ["image-18"])
                        self.assertEqual(
                            real_edited["pendingLayerEdits"]["title-1"]["text"],
                            "Edited first slide",
                        )
                        self.assertEqual(
                            real_edited["pendingLayerEdits"]["title-18"]["text"],
                            "Edited last slide",
                        )

                        player_variants = (
                            "active",
                            "is-active",
                            "current",
                            "aria-hidden",
                            "data-active",
                            "display",
                            "visibility",
                            "opacity",
                            "zero-size",
                            "styled-display-player",
                        )
                        for variant in player_variants:
                            with self.subTest(variant=variant):
                                source = page.evaluate(
                                    "args => window.__canvasDeckNavigationHarness.load(args.variant, args.hash)",
                                    {
                                        "variant": variant,
                                        "hash": "authored-b" if variant == "active" else "",
                                    },
                                )
                                self._wait_for_deck(
                                    page,
                                    expected_slides=3,
                                    expected_source=source,
                                )
                                if variant == "active":
                                    restored = page.evaluate(
                                        "window.__canvasDeckNavigationHarness.snapshot()"
                                    )
                                    self.assertEqual(restored["hash"], "#authored-b")
                                    self.assertEqual(
                                        [slide["painted"] for slide in restored["slides"]],
                                        [False, True, False],
                                    )
                                page.evaluate(
                                    "window.__canvasDeckNavigationHarness.clickThumbnail(1)"
                                )
                                page.wait_for_function(
                                    "() => window.__canvasDeckNavigationHarness.snapshot().hash === '#authored-b'"
                                )
                                selected = page.evaluate(
                                    "window.__canvasDeckNavigationHarness.snapshot()"
                                )
                                self.assertEqual(selected["mode"], "player")
                                self.assertEqual(
                                    [slide["painted"] for slide in selected["slides"]],
                                    [False, True, False],
                                )
                                self.assertEqual(
                                    [slide["ariaHidden"] for slide in selected["slides"]],
                                    ["true", "false", "true"],
                                )
                                self.assertEqual(
                                    [slide["ariaCurrent"] for slide in selected["slides"]],
                                    [None, "page", None],
                                )
                                self.assertEqual(selected["scrollY"], 0)
                                self.assertEqual(
                                    selected["thumbnailPaintedIndexes"],
                                    [0, 1, 2],
                                )
                                self.assertEqual(selected["progress"], "2 / 3")
                                self.assertEqual(
                                    selected["thumbnailPressed"],
                                    ["false", "true", "false"],
                                )
                                self.assertFalse(selected["authorScriptRan"])
                                self.assertEqual(
                                    [slide["style"] for slide in selected["slides"]],
                                    [
                                        "--author-token:slide-1",
                                        "--author-token:slide-2",
                                        "--author-token:slide-3",
                                    ],
                                )
                                expected_first_class = {
                                    "active": "deck-slide active",
                                    "is-active": "deck-slide is-active",
                                    "current": "deck-slide current",
                                    "visibility": "deck-slide is-active",
                                    "opacity": "deck-slide is-active",
                                    "zero-size": "deck-slide is-active",
                                    "styled-display-player": "deck-slide is-active",
                                }.get(variant, "deck-slide")
                                self.assertEqual(
                                    [slide["className"] for slide in selected["slides"]],
                                    [expected_first_class, "deck-slide", "deck-slide"],
                                )
                                if variant == "styled-display-player":
                                    self.assertNotEqual(
                                        selected["slides"][1]["transform"],
                                        "none",
                                    )
                                    self.assertEqual(
                                        selected["slides"][1]["transformOrigin"],
                                        "0px 0px",
                                    )
                                    self.assertEqual(
                                        selected["slides"][1]["pointerEvents"],
                                        "none",
                                    )
                                    baseline_geometry = selected["thumbnailGeometry"][0]
                                    for geometry in selected["thumbnailGeometry"][1:]:
                                        self.assertAlmostEqual(
                                            geometry["slideLeft"],
                                            baseline_geometry["slideLeft"],
                                            delta=1,
                                        )
                                        self.assertAlmostEqual(
                                            geometry["slideTop"],
                                            baseline_geometry["slideTop"],
                                            delta=1,
                                        )
                                        self.assertAlmostEqual(
                                            geometry["slideWidth"],
                                            baseline_geometry["slideWidth"],
                                            delta=1,
                                        )
                                        self.assertAlmostEqual(
                                            geometry["slideHeight"],
                                            baseline_geometry["slideHeight"],
                                            delta=1,
                                        )

                                page.evaluate(
                                    "window.__canvasDeckNavigationHarness.scrollMain(500)"
                                )
                                page.wait_for_timeout(50)
                                after_scroll = page.evaluate(
                                    "window.__canvasDeckNavigationHarness.snapshot()"
                                )
                                self.assertEqual(after_scroll["hash"], "#authored-b")
                                self.assertTrue(after_scroll["slides"][1]["painted"])

                                if variant == "active":
                                    page.evaluate(
                                        "window.__canvasDeckNavigationHarness.pressKey('End')"
                                    )
                                    page.wait_for_function(
                                        "() => window.__canvasDeckNavigationHarness.snapshot().hash === '#3'"
                                    )
                                    page.evaluate(
                                        "window.__canvasDeckNavigationHarness.pressKey('Home')"
                                    )
                                    page.wait_for_function(
                                        "() => window.__canvasDeckNavigationHarness.snapshot().hash === '#frame-a'"
                                    )
                                    page.evaluate(
                                        "window.__canvasDeckNavigationHarness.clickThumbnail(1)"
                                    )
                                    page.wait_for_function(
                                        "() => window.__canvasDeckNavigationHarness.snapshot().hash === '#authored-b'"
                                    )
                                cleaned = page.evaluate(
                                    "window.__canvasDeckNavigationHarness.cleanedCloneSnapshot()"
                                )
                                self.assertFalse(cleaned["hasNavigationMarker"])
                                self.assertEqual(
                                    cleaned["styles"],
                                    [
                                        "--author-token:slide-1",
                                        "--author-token:slide-2",
                                        "--author-token:slide-3",
                                    ],
                                )
                                self.assertEqual(
                                    cleaned["classes"],
                                    [expected_first_class, "deck-slide", "deck-slide"],
                                )
                                self.assertEqual(
                                    cleaned["ariaHidden"],
                                    ["false", "true", "true"]
                                    if variant == "aria-hidden"
                                    else [None, None, None],
                                )
                                self.assertEqual(
                                    cleaned["ariaCurrent"],
                                    ["__autodesign_missing__", None, None]
                                    if variant == "active"
                                    else [None, None, None],
                                )
                                self.assertEqual(
                                    cleaned["dataActive"],
                                    ["true", "false", "false"]
                                    if variant == "data-active"
                                    else [None, None, None],
                                )
                                cleaned_document = page.evaluate(
                                    "window.__canvasDeckNavigationHarness.cleanedDocumentSnapshot()"
                                )
                                self.assertEqual(cleaned_document, {
                                    "hasNavigationMarker": False,
                                    "hasNavigationStyle": False,
                                    "bodyCurrentSlide": None,
                                })

                                remounted_source = page.evaluate(
                                    "window.__canvasDeckNavigationHarness.remountCurrentSource()"
                                )
                                self._wait_for_deck(
                                    page,
                                    expected_slides=3,
                                    expected_source=remounted_source,
                                )
                                page.wait_for_function(
                                    "() => window.__canvasDeckNavigationHarness.snapshot().slides[1]?.painted === true"
                                )
                                remounted = page.evaluate(
                                    "window.__canvasDeckNavigationHarness.snapshot()"
                                )
                                self.assertEqual(
                                    [slide["painted"] for slide in remounted["slides"]],
                                    [False, True, False],
                                )

                                if variant == "active":
                                    thumbnail_document_ids = remounted[
                                        "thumbnailDocumentIds"
                                    ]
                                    document_id = remounted["documentId"]
                                    page.evaluate(
                                        "window.__canvasDeckNavigationHarness.reloadMain()"
                                    )
                                    page.wait_for_function(
                                        "previous => window.__canvasDeckNavigationHarness.snapshot().documentId !== previous",
                                        arg=document_id,
                                    )
                                    self._wait_for_deck(
                                        page,
                                        expected_slides=3,
                                        expected_source=remounted_source,
                                    )
                                    reloaded = page.evaluate(
                                        "window.__canvasDeckNavigationHarness.snapshot()"
                                    )
                                    self.assertEqual(
                                        [slide["painted"] for slide in reloaded["slides"]],
                                        [False, True, False],
                                    )
                                    self.assertTrue(all(
                                        before != after
                                        for before, after in zip(
                                            thumbnail_document_ids,
                                            reloaded["thumbnailDocumentIds"],
                                        )
                                    ))

                        page.reload(wait_until="networkidle")
                        page.wait_for_function(
                            "() => Boolean(window.__canvasDeckNavigationHarness)"
                        )
                        page.evaluate(
                            "window.__canvasDeckNavigationHarness.resetThumbnailInsertionLog()"
                        )
                        generated_source = page.evaluate(
                            "window.__canvasDeckNavigationHarness.load('generated-12')"
                        )
                        self._wait_for_deck(
                            page,
                            expected_slides=12,
                            expected_source=generated_source,
                        )
                        generated = page.evaluate(
                            "window.__canvasDeckNavigationHarness.snapshot()"
                        )
                        self.assertEqual(generated["mode"], "player")
                        self.assertEqual(sum(
                            slide["painted"] for slide in generated["slides"]
                        ), 1)
                        self.assertFalse(generated["authorScriptRan"])
                        self.assertEqual(
                            generated["thumbnailPaintedIndexes"],
                            list(range(12)),
                        )
                        self.assertEqual(generated["thumbnailReady"], ["true"] * 12)
                        self.assertTrue(all(
                            "allow-scripts" not in (sandbox or "")
                            for sandbox in generated["thumbnailSandboxes"]
                        ))
                        insertion_log = page.evaluate(
                            "window.__canvasDeckNavigationHarness.thumbnailInsertionLog()"
                        )
                        self.assertGreaterEqual(len(insertion_log), 12)
                        self.assertTrue(all(
                            entry == {"ready": "false", "visibility": "hidden"}
                            for entry in insertion_log
                        ))
                        for geometry in generated["thumbnailGeometry"]:
                            self.assertEqual(geometry["viewportWidth"], 1920)
                            self.assertEqual(geometry["viewportHeight"], 1080)
                            self.assertAlmostEqual(geometry["slideLeft"], 0, delta=1)
                            self.assertAlmostEqual(geometry["slideTop"], 0, delta=1)
                            self.assertAlmostEqual(geometry["slideWidth"], 1920, delta=1)
                            self.assertAlmostEqual(geometry["slideHeight"], 1080, delta=1)

                        page.evaluate(
                            "window.__canvasDeckNavigationHarness.clickThumbnail(0)"
                        )
                        page.wait_for_function(
                            "() => window.__canvasDeckNavigationHarness.snapshot().progress === '1 / 12'"
                        )
                        generated_first = page.evaluate(
                            "window.__canvasDeckNavigationHarness.snapshot()"
                        )
                        self.assertEqual(
                            [slide["painted"] for slide in generated_first["slides"]],
                            [True] + [False] * 11,
                        )

                        page.evaluate(
                            "window.__canvasDeckNavigationHarness.clickThumbnail(1)"
                        )
                        page.wait_for_function(
                            "() => window.__canvasDeckNavigationHarness.snapshot().progress === '2 / 12'"
                        )
                        generated_second = page.evaluate(
                            "window.__canvasDeckNavigationHarness.snapshot()"
                        )
                        self.assertEqual(
                            [slide["painted"] for slide in generated_second["slides"]],
                            [False, True] + [False] * 10,
                        )

                        page.evaluate(
                            "window.__canvasDeckNavigationHarness.clickThumbnail(11)"
                        )
                        page.wait_for_function(
                            "() => window.__canvasDeckNavigationHarness.snapshot().progress === '12 / 12'"
                        )
                        generated_last = page.evaluate(
                            "window.__canvasDeckNavigationHarness.snapshot()"
                        )
                        self.assertEqual(
                            [slide["painted"] for slide in generated_last["slides"]],
                            [False] * 11 + [True],
                        )

                        page.evaluate(
                            "window.__canvasDeckNavigationHarness.clickThumbnail(0)"
                        )
                        page.wait_for_function(
                            "() => window.__canvasDeckNavigationHarness.snapshot().progress === '1 / 12'"
                        )

                        centered_source = page.evaluate(
                            "window.__canvasDeckNavigationHarness.load('centered-script-player')"
                        )
                        self._wait_for_deck(
                            page,
                            expected_slides=3,
                            expected_source=centered_source,
                        )
                        for selected_index in range(3):
                            page.evaluate(
                                "index => window.__canvasDeckNavigationHarness.clickThumbnail(index)",
                                selected_index,
                            )
                            page.wait_for_function(
                                "index => window.__canvasDeckNavigationHarness.snapshot().progress === `${index + 1} / 3`",
                                arg=selected_index,
                            )
                            centered = page.evaluate(
                                "window.__canvasDeckNavigationHarness.snapshot()"
                            )
                            self.assertFalse(centered["authorScriptRan"])
                            self.assertEqual(centered["mode"], "player")
                            self.assertEqual(
                                [slide["painted"] for slide in centered["slides"]],
                                [index == selected_index for index in range(3)],
                            )
                            self.assertEqual(
                                centered["slides"][selected_index]["transform"],
                                "none",
                            )
                            for geometry in [
                                centered["mainGeometry"],
                                *centered["thumbnailGeometry"],
                            ]:
                                self.assertAlmostEqual(
                                    geometry["viewportWidth"], 1920, delta=2
                                )
                                self.assertAlmostEqual(
                                    geometry["viewportHeight"], 1080, delta=2
                                )
                                self.assertAlmostEqual(geometry["slideLeft"], 0, delta=1)
                                self.assertAlmostEqual(geometry["slideTop"], 0, delta=1)
                                self.assertAlmostEqual(geometry["slideWidth"], 1920, delta=1)
                                self.assertAlmostEqual(geometry["slideHeight"], 1080, delta=1)

                        mismatched_source = page.evaluate(
                            "window.__canvasDeckNavigationHarness.load"
                            "('mismatched-centered-script-player')"
                        )
                        self._wait_for_deck(
                            page,
                            expected_slides=3,
                            expected_source=mismatched_source,
                        )
                        page.evaluate(
                            "window.__canvasDeckNavigationHarness.clickThumbnail(1)"
                        )
                        page.wait_for_function(
                            "() => window.__canvasDeckNavigationHarness.snapshot().progress === '2 / 3'"
                        )
                        mismatched = page.evaluate(
                            "window.__canvasDeckNavigationHarness.snapshot()"
                        )
                        self.assertLess(
                            mismatched["mainGeometry"]["viewportWidth"], 1920
                        )
                        for geometry in [
                            mismatched["mainGeometry"],
                            *mismatched["thumbnailGeometry"],
                        ]:
                            self.assertAlmostEqual(geometry["slideLeft"], 0, delta=1)
                            self.assertAlmostEqual(geometry["slideTop"], 0, delta=1)
                            self.assertAlmostEqual(geometry["slideWidth"], 1920, delta=1)
                            self.assertAlmostEqual(geometry["slideHeight"], 1080, delta=1)

                        css_centered_source = page.evaluate(
                            "window.__canvasDeckNavigationHarness.load('css-centered-player')"
                        )
                        self._wait_for_deck(
                            page,
                            expected_slides=3,
                            expected_source=css_centered_source,
                        )
                        page.evaluate(
                            "window.__canvasDeckNavigationHarness.clickThumbnail(0)"
                        )
                        page.wait_for_function(
                            "() => window.__canvasDeckNavigationHarness.snapshot().progress === '1 / 3'"
                        )
                        css_centered = page.evaluate(
                            "window.__canvasDeckNavigationHarness.snapshot()"
                        )
                        self.assertNotEqual(
                            css_centered["slides"][0]["transform"],
                            "none",
                        )
                        for geometry in [
                            css_centered["mainGeometry"],
                            *css_centered["thumbnailGeometry"],
                        ]:
                            self.assertAlmostEqual(geometry["slideLeft"], 0, delta=1)
                            self.assertAlmostEqual(geometry["slideTop"], 0, delta=1)
                            self.assertAlmostEqual(geometry["slideWidth"], 1920, delta=1)
                            self.assertAlmostEqual(geometry["slideHeight"], 1080, delta=1)

                        transform_source = page.evaluate(
                            "window.__canvasDeckNavigationHarness.load('transform-player')"
                        )
                        self._wait_for_deck(
                            page,
                            expected_slides=12,
                            expected_source=transform_source,
                        )
                        transformed_initial = page.evaluate(
                            "window.__canvasDeckNavigationHarness.snapshot()"
                        )
                        self.assertEqual(
                            transformed_initial["thumbnailPaintedIndexes"],
                            list(range(12)),
                        )
                        self.assertEqual(
                            [slide["painted"] for slide in transformed_initial["slides"]],
                            [True] + [False] * 11,
                        )
                        page.evaluate(
                            "window.__canvasDeckNavigationHarness.clickThumbnail(11)"
                        )
                        page.wait_for_function(
                            "() => window.__canvasDeckNavigationHarness.snapshot().progress === '12 / 12'"
                        )
                        transformed_last = page.evaluate(
                            "window.__canvasDeckNavigationHarness.snapshot()"
                        )
                        self.assertEqual(transformed_last["mode"], "player")
                        self.assertEqual(
                            [slide["painted"] for slide in transformed_last["slides"]],
                            [False] * 11 + [True],
                        )

                        for legacy_variant in (
                            "legacy-class-root",
                            "legacy-slide-class-root",
                        ):
                            legacy_source = page.evaluate(
                                "variant => window.__canvasDeckNavigationHarness.load(variant)",
                                legacy_variant,
                            )
                            self._wait_for_deck(
                                page,
                                expected_slides=3,
                                expected_source=legacy_source,
                            )
                            legacy = page.evaluate(
                                "window.__canvasDeckNavigationHarness.snapshot()"
                            )
                            for geometry in [
                                legacy["mainGeometry"],
                                *legacy["thumbnailGeometry"],
                            ]:
                                self.assertAlmostEqual(geometry["slideLeft"], 0, delta=1)
                                self.assertAlmostEqual(
                                    geometry["slideTop"], 0, delta=1, msg=repr(legacy)
                                )
                                self.assertAlmostEqual(geometry["slideWidth"], 1920, delta=1)
                                self.assertAlmostEqual(geometry["slideHeight"], 1080, delta=1)
                            if legacy_variant == "legacy-slide-class-root":
                                for helper_geometry in [
                                    legacy["mainHelperGeometry"],
                                    *legacy["thumbnailHelperGeometry"],
                                ]:
                                    self.assertIsNotNone(helper_geometry)
                                    self.assertAlmostEqual(helper_geometry["width"], 320, delta=1)
                                    self.assertAlmostEqual(helper_geometry["height"], 80, delta=1)

                        for stacked_variant in (
                            "active-stacked",
                            "absolute-stacked",
                            "nested-absolute-stacked",
                        ):
                            with self.subTest(variant=stacked_variant):
                                stacked_source = page.evaluate(
                                    "variant => window.__canvasDeckNavigationHarness.load(variant)",
                                    stacked_variant,
                                )
                                self._wait_for_deck(
                                    page,
                                    expected_slides=3,
                                    expected_source=stacked_source,
                                )
                                stacked_snapshot = page.evaluate(
                                    "window.__canvasDeckNavigationHarness.snapshot()"
                                )
                                self.assertEqual(stacked_snapshot["mode"], "stacked")
                        self.assertEqual(page_errors, [])
                    finally:
                        try:
                            page.evaluate(
                                "window.__canvasDeckNavigationHarness?.unmount()"
                            )
                        finally:
                            context.close()
                            browser.close()
            finally:
                if process is not None:
                    _terminate_vite(process)

    def _wait_for_deck(
        self,
        page: object,
        *,
        expected_slides: int,
        expected_source: str,
    ) -> None:
        try:
            page.wait_for_function(
                """
                expected => {
                  const harness = window.__canvasDeckNavigationHarness;
                  if (!harness) return false;
                  const snapshot = harness.snapshot();
                  const expectedBase = expected.source.split('#', 1)[0];
                  const iframeBase = snapshot.iframeHref?.split('#', 1)[0];
                  return snapshot.storeArtifactUrl === expected.source
                    && iframeBase === expectedBase
                    && snapshot.slideCount === expected.slides
                    && document.querySelectorAll('button[title^="Slide "]').length === expected.slides
                    && snapshot.thumbnailPaintedIndexes.length === expected.slides
                    && (snapshot.mode === 'player'
                      ? snapshot.thumbnailPaintedIndexes.every((value, index) => value === index)
                      : snapshot.thumbnailPaintedIndexes.every((value) => value >= 0));
                }
                """,
                arg={"slides": expected_slides, "source": expected_source},
            )
        except Exception as error:
            state = page.evaluate(
                """() => ({
                  snapshot: window.__canvasDeckNavigationHarness?.snapshot(),
                  buttons: document.querySelectorAll('button[title^="Slide "]').length,
                  iframeCount: document.querySelectorAll('iframe').length,
                  bodyText: document.body.innerText.slice(0, 500),
                })"""
            )
            raise AssertionError(
                f"Deck did not mount {expected_slides} slides: {state}"
            ) from error


if __name__ == "__main__":
    unittest.main()
