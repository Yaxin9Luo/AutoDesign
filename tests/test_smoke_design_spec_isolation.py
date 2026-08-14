from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from autodesign import config, smoke
from autodesign.util import academic_palette


CANVAS_RUN_NAMES = {
    "smoke-canvas-plan-soft",
    "smoke-canvas-no-plan",
    "smoke-canvas-plan-auto-expand",
    "smoke-canvas-plan-advisory",
    "smoke-propose-schema-alias",
    "smoke-deck-plan-pending",
    "smoke-deck-plan-soft",
    "smoke-deck-plan-advisory",
}

CANVAS_SUCCESS_RUN_NAMES = {
    "smoke-canvas-plan-advisory",
    "smoke-propose-schema-alias",
    "smoke-deck-plan-pending",
    "smoke-deck-plan-soft",
    "smoke-deck-plan-advisory",
}

AUTHORED_POSTER_REVISION_ONE_ARCHIVES = frozenset({
    "out/smoke_authored_paper_poster_a0/specs/design_spec_01.json",
    (
        "out/smoke_authored_paper_poster_apply_noisy_realization_repair/"
        "specs/design_spec_01.json"
    ),
    (
        "out/smoke_authored_paper_poster_bbox_realization_ignores_truncated_ids/"
        "specs/design_spec_01.json"
    ),
    (
        "out/smoke_authored_paper_poster_bbox_realization_preflight/"
        "specs/design_spec_01.json"
    ),
    (
        "out/smoke_authored_paper_poster_dogfood_blank_legacy_layer_graph_ignored/"
        "specs/design_spec_01.json"
    ),
    (
        "out/smoke_authored_paper_poster_dogfood_draft_apply_ops/"
        "specs/design_spec_01.json"
    ),
    (
        "out/smoke_authored_paper_poster_dogfood_style_units_normalized/"
        "specs/design_spec_01.json"
    ),
    (
        "out/smoke_authored_paper_poster_draft_auto_repair_chain/"
        "specs/design_spec_01.json"
    ),
    (
        "out/smoke_authored_paper_poster_draft_noisy_missing_bbox_repair/"
        "specs/design_spec_01.json"
    ),
    (
        "out/smoke_authored_paper_poster_draft_noisy_realization_repair/"
        "specs/design_spec_01.json"
    ),
    (
        "out/smoke_authored_paper_poster_draft_preferred_over_current/"
        "specs/design_spec_01.json"
    ),
    (
        "out/smoke_authored_paper_poster_image_bind_op/"
        "specs/design_spec_01.json"
    ),
    (
        "out/smoke_authored_paper_poster_nested_bbox_relative_repair/"
        "specs/design_spec_01.json"
    ),
    (
        "out/smoke_authored_paper_poster_recovery_spec/"
        "specs/design_spec_01.json"
    ),
    (
        "out/smoke_authored_paper_poster_text_fit_resize_op/"
        "specs/design_spec_01.json"
    ),
    (
        "out/smoke_authored_paper_poster_wide_math/"
        "specs/design_spec_01.json"
    ),
    "out/smoke_paper_poster_html_first/specs/design_spec_01.json",
})
AUTHORED_POSTER_WIDE_MATH_ARCHIVE = (
    "out/smoke_authored_paper_poster_wide_math/specs/design_spec_01.json"
)
AUTHORED_POSTER_STABLE_ARCHIVES = (
    AUTHORED_POSTER_REVISION_ONE_ARCHIVES
    - {AUTHORED_POSTER_WIDE_MATH_ARCHIVE}
)


def _archive_snapshot(path: Path) -> tuple[bytes, tuple[int, int]]:
    metadata = path.stat()
    return path.read_bytes(), (metadata.st_dev, metadata.st_ino)


class SmokeDesignSpecIsolationTests(unittest.TestCase):
    def _run_authored_poster_check(self, *, failure_message: str) -> None:
        output = StringIO()
        try:
            with redirect_stdout(output), redirect_stderr(output):
                smoke.check_authored_paper_poster_html_no_api()
        except (Exception, SystemExit) as exc:
            transcript = output.getvalue()
            self.fail(
                f"{failure_message}: {type(exc).__name__}: {exc}\n"
                f"smoke transcript tail:\n{transcript[-4000:]}"
            )

    def _assert_authored_poster_archive_set(self, repo_root: Path) -> None:
        self.assertEqual(len(AUTHORED_POSTER_REVISION_ONE_ARCHIVES), 17)
        self.assertEqual(len(AUTHORED_POSTER_STABLE_ARCHIVES), 16)
        self.assertNotIn(
            AUTHORED_POSTER_WIDE_MATH_ARCHIVE,
            AUTHORED_POSTER_STABLE_ARCHIVES,
        )
        actual = {
            path.relative_to(repo_root).as_posix()
            for path in (repo_root / "out").glob(
                "**/specs/design_spec_01.json"
            )
        }
        self.assertEqual(actual, AUTHORED_POSTER_REVISION_ONE_ARCHIVES)

    def _assert_canvas_run_layout(self, canvas_root: Path) -> None:
        self.assertTrue(canvas_root.is_dir())
        run_names = {
            path.name
            for path in canvas_root.iterdir()
            if path.is_dir() and path.name != "specs"
        }
        self.assertEqual(run_names, CANVAS_RUN_NAMES)
        for run_name in CANVAS_RUN_NAMES:
            self.assertTrue(
                (canvas_root / run_name / "layers").is_dir(),
                f"{run_name} must own its layers directory",
            )

    def _run_canvas_check(self) -> None:
        output = StringIO()
        try:
            with redirect_stdout(output):
                smoke.check_conference_poster_defaults_no_api()
        except SystemExit as exc:
            transcript = output.getvalue()
            expected_markers = (
                "propose_design_spec should accept common schema aliases/coercions",
                "immutable DesignSpec revision archive conflicts with existing bytes",
            )
            if exc.code != 1 or not all(marker in transcript for marker in expected_markers):
                raise
            self.fail(
                "conference-poster smoke reused one immutable revision-1 archive "
                "for the advisory and schema-alias runs"
            )

    def test_conference_poster_smoke_isolates_design_spec_archives(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            repo_root = Path(raw_tmp)
            canvas_root = repo_root / "out" / "smoke_canvas_plan"
            sentinel_path = canvas_root / "specs" / "design_spec_99.json"
            sentinel_path.parent.mkdir(parents=True)
            sentinel_path.write_bytes(b'{"owner":"preexisting-smoke-sentinel"}\n')
            sentinel_snapshot = _archive_snapshot(sentinel_path)

            with mock.patch.object(config, "REPO_ROOT", repo_root):
                self._run_canvas_check()

                self._assert_canvas_run_layout(canvas_root)
                archive_paths = {
                    run_name: canvas_root
                    / run_name
                    / "specs"
                    / "design_spec_01.json"
                    for run_name in CANVAS_SUCCESS_RUN_NAMES
                }
                for run_name, archive_path in archive_paths.items():
                    self.assertTrue(
                        archive_path.is_file(),
                        f"{run_name} must persist its revision-1 DesignSpec archive",
                    )
                first_snapshots = {
                    run_name: _archive_snapshot(archive_path)
                    for run_name, archive_path in archive_paths.items()
                }

                self._run_canvas_check()

            self._assert_canvas_run_layout(canvas_root)
            self.assertTrue(
                sentinel_path.is_file(),
                "the unrelated archive at the former shared root must survive",
            )
            self.assertEqual(_archive_snapshot(sentinel_path), sentinel_snapshot)
            for run_name, archive_path in archive_paths.items():
                self.assertTrue(
                    archive_path.is_file(),
                    f"{run_name} archive must survive a rerun",
                )
                self.assertEqual(
                    _archive_snapshot(archive_path),
                    first_snapshots[run_name],
                    f"{run_name} archive bytes and identity must survive a rerun",
                )

    def test_composite_smoke_isolates_auxiliary_paper_context_archive(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            repo_root = Path(raw_tmp)
            primary_root = repo_root / "out" / "smoke"
            auxiliary_root = primary_root / "smoke-paper-bg"
            auxiliary_archive = auxiliary_root / "specs" / "design_spec_01.json"

            with mock.patch.object(config, "REPO_ROOT", repo_root):
                smoke.check_composite_no_api()
                first_auxiliary_snapshot = (
                    _archive_snapshot(auxiliary_archive)
                    if auxiliary_archive.is_file()
                    else None
                )

                smoke.check_composite_no_api()

            self.assertTrue(
                (primary_root / "specs" / "design_spec_01.json").is_file(),
                "the primary composite DesignSpec must remain directly under out/smoke",
            )
            primary_composite = primary_root / "composites" / "iter_01"
            for artifact_name in ("poster.psd", "poster.svg", "poster.html", "preview.png"):
                self.assertTrue(
                    (primary_composite / artifact_name).is_file(),
                    f"the primary {artifact_name} must remain under out/smoke",
                )
            self.assertIsNotNone(
                first_auxiliary_snapshot,
                "the auxiliary paper-background context must own a DesignSpec archive",
            )
            self.assertTrue(auxiliary_archive.is_file())
            self.assertTrue(
                (auxiliary_root / "layers").is_dir(),
                "the auxiliary paper-background context must own its layers directory",
            )
            self.assertEqual(
                _archive_snapshot(auxiliary_archive),
                first_auxiliary_snapshot,
                "the auxiliary archive bytes and identity must survive a rerun",
            )

    def test_authored_poster_smoke_creates_every_owned_clean_root_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            repo_root = Path(raw_tmp).resolve()
            self.assertFalse((repo_root / "out").exists())

            with mock.patch.object(config, "REPO_ROOT", repo_root):
                self._run_authored_poster_check(
                    failure_message=(
                        "clean-root authored-poster smoke must create its dogfood "
                        "layers directory before writing other.png"
                    )
                )

            self._assert_authored_poster_archive_set(repo_root)
            self.assertTrue(
                (
                    repo_root
                    / "out"
                    / "smoke_paper_poster_dogfood_contract"
                    / "layers"
                ).is_dir()
            )

    def test_authored_poster_smoke_pins_persisted_palette_across_reruns(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            repo_root = Path(raw_tmp).resolve()
            dogfood_layers = (
                repo_root
                / "out"
                / "smoke_paper_poster_dogfood_contract"
                / "layers"
            )
            dogfood_layers.mkdir(parents=True)
            phase_palette = ["bright_cobalt"]
            fallback_phases: list[str] = []
            real_random_palette_id = academic_palette._random_palette_id

            def phase_random_palette_id(
                library,
                palettes,
                *,
                excluded_ids=None,
            ):
                palette_id = phase_palette[0]
                excluded = set(excluded_ids or ())
                fallback_phases.append(palette_id)
                if palette_id in palettes and palette_id not in excluded:
                    return palette_id
                return real_random_palette_id(
                    library,
                    palettes,
                    excluded_ids=excluded,
                )

            with (
                mock.patch.object(config, "REPO_ROOT", repo_root),
                mock.patch.object(
                    academic_palette,
                    "_random_palette_id",
                    side_effect=phase_random_palette_id,
                ),
            ):
                self._run_authored_poster_check(
                    failure_message="first fixed-palette authored-poster smoke failed"
                )
                self._assert_authored_poster_archive_set(repo_root)
                first_snapshots = {
                    relative: _archive_snapshot(repo_root / relative)
                    for relative in AUTHORED_POSTER_STABLE_ARCHIVES
                }
                self.assertTrue(
                    (repo_root / AUTHORED_POSTER_WIDE_MATH_ARCHIVE).is_file()
                )
                self.assertIn("bright_cobalt", fallback_phases)

                phase_palette[0] = "deep_navy"
                self._run_authored_poster_check(
                    failure_message=(
                        "phase-changed authored-poster rerun must not let a "
                        "persisted logical run consume random palette fallback"
                    )
                )

            self._assert_authored_poster_archive_set(repo_root)
            self.assertIn("deep_navy", fallback_phases)
            self.assertTrue(
                (repo_root / AUTHORED_POSTER_WIDE_MATH_ARCHIVE).is_file()
            )
            for relative, first_snapshot in first_snapshots.items():
                self.assertEqual(
                    _archive_snapshot(repo_root / relative),
                    first_snapshot,
                    f"{relative} bytes and identity must survive the rerun",
                )


if __name__ == "__main__":
    unittest.main()
