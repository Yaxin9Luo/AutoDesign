from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autodesign.artifact_edit_job import _apply_authored_html_edits
from autodesign.util.io import sha256_file
from scripts import web_server


class LandingCardPreviewTests(unittest.TestCase):
    def test_landing_edit_regenerates_full_page_and_viewport_previews(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_final = root / "source" / "final"
            source_final.mkdir(parents=True)
            source_html = source_final / "index.html"
            source_html.write_text(
                "<!doctype html><html><body><main "
                "data-autodesign-artifact-root='landing'><h1>Original</h1>"
                "</main></body></html>",
                encoding="utf-8",
            )
            source_hash = sha256_file(source_html)
            (source_final / "landing_author_manifest.json").write_text(
                json.dumps({
                    "artifact_type": "landing",
                    "html_sha256": source_hash,
                    "quality_status": "ready_with_warnings",
                    "quality_diagnostics": ["source_only_quality"],
                }),
                encoding="utf-8",
            )
            edited_html = root / "edited.html"
            edited_html.write_text(
                source_html.read_text(encoding="utf-8").replace(
                    "Original", "Edited"
                ),
                encoding="utf-8",
            )
            work_dir = root / "edited-run"
            calls: list[tuple[str, bool]] = []

            def fake_screenshot(_html_path: Path, output_path: Path, **kwargs):
                calls.append((output_path.name, bool(kwargs.get("full_page"))))
                output_path.write_bytes(output_path.name.encode("ascii"))
                return SimpleNamespace(
                    backend="test",
                    warnings=[],
                    scale=1.0,
                    width_px=1440,
                    height_px=900,
                )

            with patch(
                "autodesign.artifact_edit_job.screenshot_html",
                side_effect=fake_screenshot,
            ):
                _apply_authored_html_edits(
                    source_html,
                    edited_html,
                    "edited-run",
                    "source",
                    {"layers": {}},
                    artifact_type="landing",
                    work_dir=work_dir,
                )

            final_dir = work_dir / "final"
            manifest = json.loads(
                (final_dir / "authored_html_edit_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            author_manifest = json.loads(
                (final_dir / "landing_author_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            artifact = web_server._build_artifact_response(
                work_dir,
                "edited-run",
                "landing",
                baseline_artifact_json=None,
            )
            self.assertEqual(
                calls,
                [("preview.png", True), ("card_preview.png", False)],
            )
            self.assertEqual(
                manifest["card_preview_relative_path"],
                "final/card_preview.png",
            )
            self.assertEqual(
                manifest["card_preview_sha256"],
                sha256_file(final_dir / "card_preview.png"),
            )
            self.assertEqual(
                manifest["html_sha256"],
                sha256_file(final_dir / "index.html"),
            )
            self.assertEqual(author_manifest["html_sha256"], source_hash)
            self.assertIsNotNone(artifact)
            assert artifact is not None
            self.assertIsNone(artifact.quality_status)
            self.assertEqual(artifact.quality_diagnostics, [])
            self.assertEqual(
                artifact.card_preview_url,
                "/api/files/runs/edited-run/final/card_preview.png",
            )


if __name__ == "__main__":
    unittest.main()
