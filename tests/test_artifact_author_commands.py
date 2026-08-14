from __future__ import annotations

import shlex
import unittest
from pathlib import Path

from autodesign.config import (
    artifact_author_command_for_harness,
    designer_author_command_for_harness,
)


class ArtifactAuthorCommandTests(unittest.TestCase):
    def test_poster_command_builder_remains_backward_compatible(self) -> None:
        self.assertEqual(
            artifact_author_command_for_harness("codex", artifact_type="poster"),
            designer_author_command_for_harness("codex"),
        )

    def test_claude_command_names_landing_outputs(self) -> None:
        command = artifact_author_command_for_harness(
            "claude",
            artifact_type="landing",
        )

        self.assertIn("landing_author_prompt.md", command)
        self.assertIn("index.html", command)
        self.assertIn("designer_author_done.json", command)
        self.assertNotIn("create or update poster.html", command)

    def test_kimi_command_names_slides_outputs(self) -> None:
        argv = shlex.split(
            artifact_author_command_for_harness("kimi", artifact_type="deck")
        )

        self.assertIn("slides_author_prompt.md", argv)
        self.assertIn("slides.html", argv)
        self.assertIn("designer_author_done.json", argv)

    def test_zcode_command_names_video_project_outputs(self) -> None:
        argv = shlex.split(
            artifact_author_command_for_harness("zcode", artifact_type="video")
        )

        self.assertIn("video_author_prompt.md", argv)
        self.assertIn("project/index.html", argv)
        self.assertIn("video_author_manifest.json", argv)
        self.assertIn("designer_author_done.json", argv)

    def test_explicit_custom_command_is_preserved(self) -> None:
        self.assertEqual(
            artifact_author_command_for_harness(
                "custom",
                artifact_type="landing",
                explicit_cmd="my-author --stdin",
            ),
            "my-author --stdin",
        )

    def test_web_harness_matrix_reports_locked_zcode_model_selection(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "web_server.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            '"model_selection_mode": "locked_config" if h.id == "zcode"',
            source,
        )
        self.assertNotIn(
            '"model_selection_mode": "prompt_model_command" if h.id == "zcode"',
            source,
        )


if __name__ == "__main__":
    unittest.main()
