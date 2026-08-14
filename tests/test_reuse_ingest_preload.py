from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autodesign.config import Settings
from autodesign.runner import PipelineRunner


class ReuseIngestPreloadTest(unittest.TestCase):
    def test_missing_reuse_fails_before_enhancer_or_designer(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            out_dir = Path(raw_tmp)
            settings = Settings(
                anthropic_api_key="",
                anthropic_base_url=None,
                gemini_api_key="",
                designer_model="designer",
                critic_model="critic",
                repo_root=Path(__file__).resolve().parents[1],
                out_dir=out_dir,
            )
            runner = PipelineRunner(settings)
            with (
                patch("autodesign.runner._run_enhancer", side_effect=AssertionError("enhancer called")),
                patch("autodesign.runner._make_designer_author", side_effect=AssertionError("designer called")),
            ):
                result = runner.run(
                    "Create a research paper project landing page.",
                    run_id="missing-reuse",
                    reuse_ingest_run="does-not-exist",
                )

            self.assertEqual(result.terminal_status, "fail")
            self.assertIn("reuse", result.finalize_notes.lower())
            self.assertTrue(
                (out_dir / "runs" / "missing-reuse" / "reuse_ingest_preload.json").exists()
            )


if __name__ == "__main__":
    unittest.main()
