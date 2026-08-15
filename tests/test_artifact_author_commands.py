from __future__ import annotations

import shlex
import unittest
from pathlib import Path

from autodesign.config import (
    artifact_author_command_for_harness,
    code_editor_command_for_harness,
    designer_author_command_for_harness,
)
from scripts import web_server


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

    def test_deepseek_commands_cover_all_artifact_and_editor_surfaces(self) -> None:
        expected_by_artifact = {
            "poster": ("designer_author_prompt.md", "poster.html"),
            "landing": ("landing_author_prompt.md", "index.html"),
            "deck": ("slides_author_prompt.md", "slides.html"),
            "video": ("video_author_prompt.md", "project/index.html"),
        }
        for artifact_type, expected in expected_by_artifact.items():
            with self.subTest(artifact_type=artifact_type):
                argv = shlex.split(artifact_author_command_for_harness(
                    "deepseek",
                    artifact_type=artifact_type,
                    model="deepseek-v4-pro",
                ))
                self.assertTrue(any(part.endswith("deepseek_harness_agent.py") for part in argv))
                self.assertIn("--dsh-bin", argv)
                dsh_bin = argv[argv.index("--dsh-bin") + 1]
                self.assertEqual(Path(dsh_bin).name, "dsh")
                self.assertIn("--model", argv)
                self.assertIn("deepseek-v4-pro", argv)
                for value in expected:
                    self.assertIn(value, argv)

        editor_argv = shlex.split(code_editor_command_for_harness(
            "deepseek",
            model="deepseek-v4-flash",
        ))
        self.assertTrue(any(part.endswith("deepseek_harness_agent.py") for part in editor_argv))
        self.assertIn("edit_prompt.md", editor_argv)
        self.assertIn("poster.html", editor_argv)
        self.assertIn("code_editor_done.json", editor_argv)

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
        request = web_server.HarnessMatrixRequest(
            paper_path="paper.pdf",
            prompt="Create a poster.",
            harnesses=[{"id": "zcode", "model": "glm"}],
        )
        snapshot = web_server._initial_harness_matrix_snapshot(
            "matrix-zcode",
            request,
            matrix_dir=Path("matrix-zcode"),
        )

        self.assertEqual(snapshot["rows"][0]["model_selection_mode"], "locked_config")


if __name__ == "__main__":
    unittest.main()
