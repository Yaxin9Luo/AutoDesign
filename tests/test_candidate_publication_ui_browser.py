from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / "web"
FIXTURE_PATH = "/tests/browser/candidatePublicationOwner.html"


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


class CandidatePublicationMountedUiTests(unittest.TestCase):
    def test_mounted_actions_react_only_to_transient_publication_owner(self) -> None:
        vite = WEB_ROOT / "node_modules" / ".bin" / "vite"
        self.assertTrue(vite.is_file(), f"Declared Vite executable is missing: {vite}")
        port = _free_loopback_port()
        fixture_url = f"http://127.0.0.1:{port}{FIXTURE_PATH}"
        process: subprocess.Popen[str] | None = None

        with tempfile.TemporaryDirectory(prefix="autodesign-mounted-ui-") as temp_dir:
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

                from playwright.sync_api import expect, sync_playwright

                from autodesign.util.browser_render import _launch_chromium

                with sync_playwright() as playwright:
                    browser = None
                    context = None
                    page = None
                    try:
                        browser = _launch_chromium(playwright)
                        context = browser.new_context()
                        context.add_init_script(
                            """
                            window.localStorage.clear();
                            window.__candidatePublicationInitialStorageLength =
                              window.localStorage.length;
                            """
                        )
                        page = context.new_page()
                        page_errors: list[str] = []
                        api_requests: list[str] = []
                        page.on("pageerror", lambda error: page_errors.append(str(error)))

                        def record_api_request(request: object) -> None:
                            request_url = getattr(request, "url", "")
                            if urlsplit(request_url).path.startswith("/api/"):
                                api_requests.append(request_url)

                        page.on("request", record_api_request)
                        page.goto(fixture_url, wait_until="load")
                        page.wait_for_function(
                            "() => Boolean(window.__candidatePublicationHarness)"
                        )

                        inspector_action = page.locator(
                            '[data-harness="inspector"] button',
                            has_text="Use this attempt",
                        )
                        dock_action = page.locator('[data-harness="dock"] button')
                        expect(inspector_action).to_have_count(1)
                        expect(dock_action).to_have_count(1)
                        expect(inspector_action).to_be_enabled()
                        expect(dock_action).to_be_enabled()
                        self.assertEqual(
                            page.evaluate(
                                "window.__candidatePublicationHarness.mountCount"
                            ),
                            1,
                        )

                        # The persisted store's boot recovery is outside the
                        # owner-transition window; require it to settle first.
                        page.wait_for_load_state("networkidle")
                        transition_api_request_start = len(api_requests)
                        page.evaluate(
                            "window.__candidatePublicationHarness.beginObservation()"
                        )
                        page.evaluate(
                            "window.__candidatePublicationHarness.installOwner()"
                        )
                        expect(inspector_action).to_be_disabled()
                        expect(dock_action).to_be_disabled()
                        self.assertEqual(
                            page.evaluate(
                                "window.__candidatePublicationHarness.snapshot()"
                            ),
                            {
                                "ownerActive": True,
                                "operationConversationId": (
                                    "candidate-publication-mounted:candidate-publish"
                                ),
                                "sourceRunId": "run-candidate-publication-mounted",
                                "pending": False,
                                "candidateMessages": 0,
                                "candidateProgress": False,
                                "nonOwnerMutations": [],
                                "initialLocalStorageLength": 0,
                            },
                        )

                        page.evaluate(
                            "window.__candidatePublicationHarness.clearOwner()"
                        )
                        expect(inspector_action).to_be_enabled()
                        expect(dock_action).to_be_enabled()
                        self.assertEqual(
                            page.evaluate(
                                "window.__candidatePublicationHarness.finishObservation()"
                            ),
                            {
                                "apiRequests": [],
                                "timerCalls": [],
                                "nonOwnerMutations": [],
                                "nonOwnerStateEqual": True,
                            },
                        )
                        self.assertEqual(
                            api_requests[transition_api_request_start:], []
                        )

                        page.evaluate(
                            "window.__candidatePublicationHarness."
                            "hydrateTerminalFailure()"
                        )
                        terminal_action = page.locator(
                            '[data-harness="inspector"] button',
                            has_text="Retry finalization",
                        )
                        expect(terminal_action).to_have_count(1)
                        expect(terminal_action).to_be_enabled()
                        inspector = page.locator('[data-harness="inspector"]')
                        expect(inspector).not_to_contain_text(
                            "Finalizing Attempt"
                        )
                        expect(inspector).to_contain_text(
                            "narration_timing_unfit scene=scene_11"
                        )
                        self.assertNotIn(
                            "/Users/",
                            inspector.inner_text(),
                        )
                        self.assertNotIn(
                            "Kokoro synthesis failed",
                            inspector.inner_text(),
                        )
                        self.assertEqual(page_errors, [])
                    finally:
                        try:
                            if page is not None:
                                page.evaluate(
                                    "window.__candidatePublicationHarness.unmount()"
                                )
                        finally:
                            try:
                                if context is not None:
                                    context.close()
                            finally:
                                if browser is not None:
                                    browser.close()
            finally:
                if process is not None:
                    _terminate_vite(process)


if __name__ == "__main__":
    unittest.main()
