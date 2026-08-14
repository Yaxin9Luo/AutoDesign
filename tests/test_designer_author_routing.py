from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from autodesign.runner import (
    _apply_attachment_prologue,
    _infer_skill_artifact_hint,
    _make_designer_author,
    _should_use_external_designer_author,
    _validate_reference_poster_artifact,
)


class DesignerAuthorRoutingTests(unittest.TestCase):
    def test_video_attachment_prologue_does_not_inject_poster_authoring_route(self) -> None:
        prologue = _apply_attachment_prologue(
            "Create a conference video.",
            [Path("/tmp/paper.pdf")],
            artifact_type="video",
        )

        self.assertIn("CALL `ingest_document` FIRST", prologue)
        self.assertIn("`propose_design_spec`", prologue)
        self.assertNotIn("propose_paper_poster_html", prologue)
        self.assertNotIn("academic paper posters", prologue)

    def test_external_author_supports_each_authored_artifact(self) -> None:
        settings = SimpleNamespace(designer_author_mode="external")

        for hint in ("poster", "landing", "deck", "video"):
            with self.subTest(hint=hint):
                self.assertTrue(
                    _should_use_external_designer_author(settings, artifact_hint=hint)
                )
        self.assertFalse(
            _should_use_external_designer_author(settings, artifact_hint=None)
        )

    def test_internal_mode_never_uses_external_author(self) -> None:
        settings = SimpleNamespace(designer_author_mode="internal")

        self.assertFalse(
            _should_use_external_designer_author(settings, artifact_hint="poster")
        )

    def test_factory_constructs_the_expected_author_class(self) -> None:
        external = SimpleNamespace(designer_author_mode="external")
        internal = SimpleNamespace(designer_author_mode="internal")
        with (
            patch("autodesign.runner.ExternalDesignerAuthor", return_value="poster") as poster,
            patch("autodesign.runner.ExternalLandingAuthor", return_value="landing") as landing,
            patch("autodesign.runner.ExternalSlidesAuthor", return_value="slides") as slides,
            patch("autodesign.runner.ExternalVideoAuthor", return_value="video") as video,
            patch("autodesign.runner.DesignerLoop", return_value="internal") as internal_loop,
        ):
            self.assertEqual(_make_designer_author(external, "system", artifact_hint="poster"), "poster")
            self.assertEqual(_make_designer_author(external, "system", artifact_hint="landing"), "landing")
            self.assertEqual(_make_designer_author(external, "system", artifact_hint="deck"), "slides")
            self.assertEqual(_make_designer_author(external, "system", artifact_hint="video"), "video")
            self.assertEqual(_make_designer_author(external, "system", artifact_hint=None), "internal")
            self.assertEqual(
                _make_designer_author(internal, "system", artifact_hint="poster"),
                "internal",
            )
        poster.assert_called_once_with(external, "system")
        landing.assert_called_once_with(external, "system")
        slides.assert_called_once_with(external, "system")
        video.assert_called_once_with(external, "system")
        self.assertEqual(internal_loop.call_count, 2)

    def test_canvas_artifact_type_wins_over_attachment_path_tokens(self) -> None:
        brief = (
            "Canvas Plan:\n"
            "  artifact_type: landing\n"
            "  preset_id: landing-responsive\n\n"
            "Attached files:\n"
            "  /tmp/AutoDeisgn-PosterBench/example/paper.pdf\n\n"
            "Create an interactive research project landing page."
        )

        self.assertEqual(_infer_skill_artifact_hint(brief), "landing")

    def test_reference_poster_rejects_non_poster_canvas(self) -> None:
        with self.assertRaisesRegex(ValueError, "only supports poster"):
            _validate_reference_poster_artifact(
                reference_poster=object(),
                canvas_plan={"artifact_type": "landing"},
            )

        _validate_reference_poster_artifact(
            reference_poster=object(),
            canvas_plan={"artifact_type": "poster"},
        )


if __name__ == "__main__":
    unittest.main()
