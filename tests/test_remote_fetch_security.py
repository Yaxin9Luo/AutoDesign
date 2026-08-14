from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
import socket
import threading
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request
from PIL import Image

from autodesign import image_backend
from autodesign.util.openresearch_api import OpenResearchApiClient
from scripts import web_server


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (1, 1), "white").save(output, format="PNG")
    return output.getvalue()


class _ImageHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        payload = _png_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class RemoteFetchSecurityTests(unittest.TestCase):
    def test_provider_image_url_rejects_local_files(self) -> None:
        with self.assertRaisesRegex(image_backend.ImageGenerationError, "http"):
            image_backend._png_from_url(
                "file:///etc/passwd",
                model="fake",
                allow_private_network=False,
            )

    def test_provider_image_url_rejects_loopback_in_public_mode(self) -> None:
        with self.assertRaisesRegex(image_backend.ImageGenerationError, "public"):
            image_backend._png_from_url(
                "http://127.0.0.1/internal.png",
                model="fake",
                allow_private_network=False,
            )

    def test_provider_image_url_allows_loopback_for_explicit_local_mode(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ImageHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = image_backend._png_from_url(
                f"http://127.0.0.1:{server.server_port}/image.png",
                model="fake",
                allow_private_network=True,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual((result.width, result.height), (1, 1))

    def test_provider_image_timeout_is_reported_as_fetch_failure(self) -> None:
        with patch.object(image_backend.request, "urlopen", side_effect=socket.timeout()):
            with self.assertRaisesRegex(
                image_backend.ImageGenerationError,
                "Image URL fetch failed",
            ):
                image_backend._png_from_url(
                    "https://images.example.org/result.png",
                    model="fake",
                    allow_private_network=True,
                )

    def test_public_request_rejects_private_custom_provider_base(self) -> None:
        request = Request({
            "type": "http",
            "headers": [
                (b"x-openai-key", b"test-placeholder-key"),
                (b"x-custom-openai-base", b"http://127.0.0.1:9000/v1"),
            ],
        })
        with patch.object(web_server, "_RUN_ACCESS_CONTROL", True):
            with self.assertRaises(HTTPException) as caught:
                web_server._request_env_overrides(request)
        self.assertEqual(caught.exception.status_code, 400)

    def test_openresearch_transport_controls_are_not_request_overrides(self) -> None:
        request = Request({
            "type": "http",
            "headers": [
                (b"x-openresearch-token", b"test-placeholder-token"),
                (b"x-openresearch-api-url", b"https://attacker.example"),
                (b"x-openresearch-submitter-cmd", b"not-allowed"),
                (b"x-openresearch-submitter", b"auto"),
                (b"x-openresearch-submitter-timeout", b"999"),
            ],
        })
        with patch.object(web_server, "_RUN_ACCESS_CONTROL", True):
            overrides, _has_key = web_server._request_env_overrides(request)
        self.assertEqual(
            overrides,
            {"AUTODESIGN_OPENRESEARCH_TOKEN": "test-placeholder-token"},
        )

    def test_openresearch_local_mode_keeps_submitter_overrides(self) -> None:
        request = Request({
            "type": "http",
            "headers": [
                (b"x-openresearch-submitter", b"auto"),
                (b"x-openresearch-submitter-timeout", b"999"),
            ],
        })
        with patch.object(web_server, "_RUN_ACCESS_CONTROL", False):
            overrides, _has_key = web_server._request_env_overrides(request)
        self.assertEqual(
            overrides,
            {
                "AUTODESIGN_OPENRESEARCH_SUBMITTER": "auto",
                "AUTODESIGN_OPENRESEARCH_SUBMITTER_TIMEOUT_SECONDS": "999",
            },
        )

    def test_openresearch_client_rejects_private_base_in_public_mode(self) -> None:
        with self.assertRaises(ValueError):
            OpenResearchApiClient(
                api_url="http://127.0.0.1:9000",
                token="test-placeholder-token",
                credentials_path=Path("/missing"),
                allow_private_network=False,
            )

    def test_openresearch_client_keeps_explicit_local_mode(self) -> None:
        client = OpenResearchApiClient(
            api_url="http://127.0.0.1:9000",
            token="test-placeholder-token",
            credentials_path=Path("/missing"),
            allow_private_network=True,
        )
        self.assertEqual(client.api_url, "http://127.0.0.1:9000")


if __name__ == "__main__":
    unittest.main()
