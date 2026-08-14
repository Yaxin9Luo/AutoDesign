from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autodesign.util.io import sha256_file
from scripts import web_server


class WebArtifactDeliveryMetadataTests(unittest.TestCase):
    def _landing_run(self, root: Path, *, manifest_hash: str | None = None) -> Path:
        run_dir = root / "landing-run"
        final_dir = run_dir / "final"
        final_dir.mkdir(parents=True)
        html_path = final_dir / "index.html"
        html_path.write_text(
            "<!doctype html><html><body><main data-autodesign-artifact-root='landing'>"
            "<h1 data-layer-id='title'>Paper</h1></main></body></html>",
            encoding="utf-8",
        )
        (final_dir / "preview.png").write_bytes(b"full-page-preview")
        (final_dir / "card_preview.png").write_bytes(b"viewport-preview")
        (run_dir / "run_control.json").write_text("{}", encoding="utf-8")
        manifest = {
            "artifact_type": "landing",
            "html_sha256": manifest_hash or sha256_file(html_path),
            "quality_status": "ready_with_warnings",
            "quality_diagnostics": ["landing_reduced_motion_missing"],
            "card_preview_relative_path": "final/card_preview.png",
            "card_preview_sha256": sha256_file(final_dir / "card_preview.png"),
        }
        (final_dir / "landing_author_manifest.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        return run_dir

    def test_hash_bound_landing_metadata_is_exposed_separately_from_qa_preview(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._landing_run(Path(tmp))

            artifact = web_server._build_artifact_response(
                run_dir,
                "landing-run",
                "landing",
                baseline_artifact_json=None,
            )

            self.assertIsNotNone(artifact)
            assert artifact is not None
            self.assertEqual(
                artifact.preview_url,
                "/api/files/runs/landing-run/final/preview.png",
            )
            self.assertEqual(
                artifact.card_preview_url,
                "/api/files/runs/landing-run/final/card_preview.png",
            )
            self.assertEqual(artifact.quality_status, "ready_with_warnings")
            self.assertEqual(
                artifact.quality_diagnostics,
                ["landing_reduced_motion_missing"],
            )

    def test_stale_landing_manifest_metadata_is_not_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._landing_run(Path(tmp), manifest_hash="0" * 64)

            artifact = web_server._build_artifact_response(
                run_dir,
                "landing-run",
                "landing",
                baseline_artifact_json=None,
            )

            self.assertIsNotNone(artifact)
            assert artifact is not None
            self.assertIsNone(artifact.card_preview_url)
            self.assertIsNone(artifact.quality_status)
            self.assertEqual(artifact.quality_diagnostics, [])

    def test_edited_landing_uses_the_hash_bound_edit_manifest_card_preview(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._landing_run(Path(tmp))
            final_dir = run_dir / "final"
            html_path = final_dir / "index.html"
            html_path.write_text(
                "<!doctype html><html><body><main "
                "data-autodesign-artifact-root='landing'><h1>Edited</h1>"
                "</main></body></html>",
                encoding="utf-8",
            )
            (final_dir / "edited_card_preview.png").write_bytes(
                b"edited-viewport-preview"
            )
            (final_dir / "authored_html_edit_manifest.json").write_text(
                json.dumps({
                    "artifact_type": "landing",
                    "html_sha256": sha256_file(html_path),
                    "card_preview_relative_path": "final/edited_card_preview.png",
                    "card_preview_sha256": sha256_file(
                        final_dir / "edited_card_preview.png"
                    ),
                }),
                encoding="utf-8",
            )

            artifact = web_server._build_artifact_response(
                run_dir,
                "landing-run",
                "landing",
                baseline_artifact_json=None,
            )

            self.assertIsNotNone(artifact)
            assert artifact is not None
            self.assertEqual(
                artifact.card_preview_url,
                "/api/files/runs/landing-run/final/edited_card_preview.png",
            )
            self.assertIsNone(artifact.quality_status)

    def test_edited_landing_merges_edit_preview_with_fresh_author_quality(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._landing_run(Path(tmp))
            final_dir = run_dir / "final"
            html_path = final_dir / "index.html"
            html_path.write_text(
                "<!doctype html><html><body><main "
                "data-autodesign-artifact-root='landing'><h1>Published edit</h1>"
                "</main></body></html>",
                encoding="utf-8",
            )
            edited_hash = sha256_file(html_path)
            (final_dir / "edited_card_preview.png").write_bytes(
                b"edited-viewport-preview"
            )
            (final_dir / "authored_html_edit_manifest.json").write_text(
                json.dumps({
                    "artifact_type": "landing",
                    "html_sha256": edited_hash,
                    "card_preview_relative_path": "final/edited_card_preview.png",
                    "card_preview_sha256": sha256_file(
                        final_dir / "edited_card_preview.png"
                    ),
                }),
                encoding="utf-8",
            )
            author_manifest_path = final_dir / "landing_author_manifest.json"
            author_manifest = json.loads(
                author_manifest_path.read_text(encoding="utf-8")
            )
            author_manifest.update({
                "html_sha256": edited_hash,
                "quality_status": "ready_with_warnings",
                "quality_diagnostics": ["landing_content_clipped"],
            })
            author_manifest_path.write_text(
                json.dumps(author_manifest),
                encoding="utf-8",
            )

            artifact = web_server._build_artifact_response(
                run_dir,
                "landing-run",
                "landing",
                baseline_artifact_json=None,
            )

            self.assertIsNotNone(artifact)
            assert artifact is not None
            self.assertEqual(
                artifact.card_preview_url,
                "/api/files/runs/landing-run/final/edited_card_preview.png",
            )
            self.assertEqual(artifact.quality_status, "ready_with_warnings")
            self.assertEqual(
                artifact.quality_diagnostics,
                ["landing_content_clipped"],
            )

    def test_poster_quality_metadata_uses_the_direct_author_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "poster-run"
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            html_path = final_dir / "poster.html"
            html_path.write_text(
                "<!doctype html><html><body><main class='paper-poster'>"
                "<h1>Poster</h1></main></body></html>",
                encoding="utf-8",
            )
            (final_dir / "designer_author_direct_manifest.json").write_text(
                json.dumps({
                    "artifact_type": "poster",
                    "html_sha256": sha256_file(html_path),
                    "quality_status": "ready_with_warnings",
                    "quality_diagnostics": ["poster_typography_polish"],
                }),
                encoding="utf-8",
            )

            artifact = web_server._build_artifact_response(
                run_dir,
                "poster-run",
                "poster",
                baseline_artifact_json=None,
            )

            self.assertIsNotNone(artifact)
            assert artifact is not None
            self.assertEqual(artifact.quality_status, "ready_with_warnings")
            self.assertEqual(
                artifact.quality_diagnostics,
                ["poster_typography_polish"],
            )

    def test_history_preview_preserves_hash_bound_card_and_quality_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = self._landing_run(root)
            artifact = web_server._build_artifact_response(
                run_dir,
                "landing-run",
                "landing",
                baseline_artifact_json=None,
            )
            assert artifact is not None

            compact = web_server._history_artifact_preview(
                web_server._dump_model(artifact),
                fallback_id="fallback",
            )
            self.assertIsNotNone(compact)
            assert compact is not None
            self.assertEqual(compact.get("card_preview_url"), artifact.card_preview_url)
            self.assertEqual(compact.get("quality_status"), "ready_with_warnings")
            self.assertEqual(
                compact.get("quality_diagnostics"),
                ["landing_reduced_motion_missing"],
            )

            with (
                patch.object(web_server, "RUNS_DIR", root),
                patch.object(
                    web_server,
                    "_history_control_allows_artifact",
                    return_value=True,
                ),
            ):
                cold = web_server._history_artifact_preview_from_run(
                    "landing-run",
                    "landing",
                )
            self.assertIsNotNone(cold)
            assert cold is not None
            self.assertEqual(cold.get("card_preview_url"), artifact.card_preview_url)
            self.assertEqual(cold.get("quality_status"), "ready_with_warnings")


if __name__ == "__main__":
    unittest.main()
