from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

from autodesign.util.browser_render import (
    BrowserRenderResult,
    _image_pdf_fallback,
    export_html_pdf,
)
from scripts import web_server


class SinglePagePosterPdfExportTests(unittest.TestCase):
    def test_poster_pdf_crops_document_flow_to_the_canvas(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            html_path = root / "poster.html"
            pdf_path = root / "poster.pdf"
            html_path.write_text(
                """<!doctype html>
<html>
<head>
  <style>
    @page { size: 162.56mm 121.92mm; margin: 0; }
    html, body { margin: 0; width: 640px; min-height: 480px; }
    .paper-poster { position: relative; width: 640px; height: 480px; overflow: hidden; background: #f7f3ea; }
    .canvas-title { margin: 0; padding: 24px; font-size: 28px; }
    .canvas-footer { position: absolute; left: 24px; top: 455px; margin: 0; font-size: 20px; }
    .outside-canvas { margin-top: 24px; font-size: 24px; }
    @media print {
      html, body { width: 162.56mm; height: 121.92mm; }
      .paper-poster { width: 100%; height: 100%; }
    }
  </style>
</head>
<body>
  <main class="paper-poster" data-w="640" data-h="480">
    <h1 class="canvas-title">Visible poster canvas</h1>
    <p class="canvas-footer">Canvas footer remains visible</p>
  </main>
  <p class="outside-canvas">Outside canvas must not become PDF page two</p>
</body>
</html>""",
                encoding="utf-8",
            )

            result = export_html_pdf(
                html_path,
                pdf_path,
                viewport_width=640,
                viewport_height=480,
                page_width="162.56mm",
                page_height="121.92mm",
                enforce_single_page=True,
                canvas_selector=".paper-poster",
                canvas_width_px=640,
                canvas_height_px=480,
            )

            self.assertTrue(pdf_path.exists(), result.warnings)
            self.assertEqual(result.warnings, [])
            with fitz.open(pdf_path) as document:
                self.assertEqual(document.page_count, 1)
                self.assertIn("Visible poster canvas", document[0].get_text())
                footer = document[0].search_for("Canvas footer remains visible")
                self.assertEqual(len(footer), 1)
                self.assertLess(footer[0].y1, document[0].rect.height)
                self.assertNotIn("Outside canvas", document[0].get_text())
                self.assertAlmostEqual(document[0].rect.width, 460.8, delta=1.0)
                self.assertAlmostEqual(document[0].rect.height, 345.6, delta=1.0)

    def test_web_poster_export_requests_a_strict_canvas_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            runs_dir = Path(raw_tmp) / "runs"
            source = runs_dir / "run-1" / "final" / "poster.html"
            source.parent.mkdir(parents=True)
            source.write_text(
                '<main class="paper-poster" data-w="640" data-h="480"></main>',
                encoding="utf-8",
            )
            captured: dict[str, object] = {}

            def fake_export(
                html_path: Path,
                pdf_path: Path,
                **kwargs: object,
            ) -> BrowserRenderResult:
                captured["html_path"] = html_path
                captured.update(kwargs)
                pdf_path.write_bytes(b"%PDF-1.4\n")
                return BrowserRenderResult(backend="test", paths=[pdf_path])

            request = web_server.ArtifactExportRequest(
                artifact={
                    "artifact_id": "art_run-1",
                    "artifact_type": "poster",
                    "native_file_url": "/api/files/runs/run-1/final/poster.html",
                    "canvas": {"w": 640, "h": 480},
                },
                format="pdf",
            )
            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "export_html_pdf", side_effect=fake_export),
            ):
                response = web_server._export_artifact_sync(request)

        self.assertEqual(response.format, "pdf")
        self.assertEqual(captured["html_path"], source.resolve())
        self.assertTrue(captured["enforce_single_page"])
        self.assertEqual(captured["canvas_width_px"], 640)
        self.assertEqual(captured["canvas_height_px"], 480)

    def test_strict_fallback_creates_one_physical_page(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            image_path = root / "preview.png"
            pdf_path = root / "nested" / "poster.pdf"
            from PIL import Image

            Image.new("RGB", (640, 480), color="#f7f3ea").save(image_path)
            result = _image_pdf_fallback(
                pdf_path,
                [image_path],
                page_width="162.56mm",
                page_height="121.92mm",
            )

            self.assertEqual(result.backend, "pymupdf-single-page-fallback")
            self.assertTrue(pdf_path.exists(), result.warnings)
            with fitz.open(pdf_path) as document:
                self.assertEqual(document.page_count, 1)
                self.assertAlmostEqual(document[0].rect.width, 460.8, delta=1.0)
                self.assertAlmostEqual(document[0].rect.height, 345.6, delta=1.0)


if __name__ == "__main__":
    unittest.main()
