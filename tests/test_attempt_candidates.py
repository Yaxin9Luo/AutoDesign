from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from autodesign.attempt_candidates import (
    capture_attempt_candidate,
    load_attempt_candidate,
    load_attempt_candidates,
    load_candidate_index,
    load_selection_journal,
    rebuild_candidate_index,
    write_selection_journal,
)
from autodesign.schema import AttemptIssue, AttemptSelectionJournal
from autodesign.tools._contract import ToolContext


class AttemptCandidateStoreTests(unittest.TestCase):
    def _attempt(self, root: Path) -> tuple[Path, Path]:
        run_dir = root / "run-1"
        attempt_dir = run_dir / "landing_author" / "attempt_01"
        assets_dir = attempt_dir / "assets"
        assets_dir.mkdir(parents=True)
        (attempt_dir / "index.html").write_text(
            "<!doctype html><h1>Attempt one</h1>",
            encoding="utf-8",
        )
        (assets_dir / "hero.png").write_bytes(b"hero")
        (attempt_dir / "attempt_preview.png").write_bytes(b"preview")
        (attempt_dir / "landing_validation.json").write_text(
            json.dumps({"accepted": True}),
            encoding="utf-8",
        )
        return run_dir, attempt_dir

    def _capture(self, run_dir: Path, attempt_dir: Path):
        return capture_attempt_candidate(
            run_dir=run_dir,
            attempt_dir=attempt_dir,
            artifact_type="landing",
            attempt=1,
            max_attempts=4,
            source_path="index.html",
            dependency_paths=["assets/hero.png"],
            preview_paths=["attempt_preview.png"],
            validation_summary_path="landing_validation.json",
            safety_state="ready_with_warnings",
            hard_blockers=[],
            warnings=[
                AttemptIssue(
                    issue_id="small_copy",
                    message="Small body copy",
                )
            ],
        )

    def test_capture_writes_immutable_manifest_and_atomic_index(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir, attempt_dir = self._attempt(Path(raw))

            candidate = self._capture(run_dir, attempt_dir)

            self.assertEqual(candidate.attempt, 1)
            self.assertEqual(candidate.safety_state, "ready_with_warnings")
            self.assertTrue((attempt_dir / "attempt_candidate.json").is_file())
            self.assertTrue((attempt_dir / "candidate/index.html").is_file())
            self.assertTrue((attempt_dir / "candidate/assets/hero.png").is_file())
            index = load_candidate_index(run_dir)
            self.assertIsNotNone(index)
            assert index is not None
            self.assertEqual(index.candidate_ids, [candidate.candidate_id])
            loaded = load_attempt_candidate(run_dir, 1)
            self.assertEqual(loaded.candidate_id, candidate.candidate_id)

    def test_capture_is_idempotent_but_rejects_changed_immutable_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir, attempt_dir = self._attempt(Path(raw))
            first = self._capture(run_dir, attempt_dir)

            second = self._capture(run_dir, attempt_dir)
            self.assertEqual(second.candidate_id, first.candidate_id)

            (attempt_dir / "index.html").write_text(
                "<!doctype html><h1>Changed</h1>",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "immutable"):
                self._capture(run_dir, attempt_dir)

    def test_capture_rejects_absolute_and_parent_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir, attempt_dir = self._attempt(Path(raw))
            for unsafe in ("/tmp/file.html", "../outside.html"):
                with self.subTest(path=unsafe):
                    with self.assertRaisesRegex(ValueError, "attempt-relative"):
                        capture_attempt_candidate(
                            run_dir=run_dir,
                            attempt_dir=attempt_dir,
                            artifact_type="landing",
                            attempt=1,
                            max_attempts=4,
                            source_path=unsafe,
                            dependency_paths=[],
                            preview_paths=[],
                            validation_summary_path="landing_validation.json",
                            safety_state="blocked",
                            hard_blockers=[],
                            warnings=[],
                        )

    def test_capture_rejects_browser_resource_outside_dependency_closure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir, attempt_dir = self._attempt(Path(raw))

            with self.assertRaisesRegex(ValueError, "browser resource"):
                capture_attempt_candidate(
                    run_dir=run_dir,
                    attempt_dir=attempt_dir,
                    artifact_type="landing",
                    attempt=1,
                    max_attempts=4,
                    source_path="index.html",
                    dependency_paths=[],
                    browser_resource_paths=["assets/hero.png"],
                    preview_paths=["attempt_preview.png"],
                    validation_summary_path="landing_validation.json",
                    safety_state="ready",
                    hard_blockers=[],
                    warnings=[],
                )

    def test_tampered_browser_resource_manifest_does_not_expose_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir, attempt_dir = self._attempt(Path(raw))
            (attempt_dir / "designer_author_done.json").write_text(
                '{"provider":"private"}',
                encoding="utf-8",
            )
            candidate = capture_attempt_candidate(
                run_dir=run_dir,
                attempt_dir=attempt_dir,
                artifact_type="landing",
                attempt=1,
                max_attempts=4,
                source_path="index.html",
                dependency_paths=[
                    "assets/hero.png",
                    "designer_author_done.json",
                ],
                browser_resource_paths=["assets/hero.png"],
                preview_paths=["attempt_preview.png"],
                validation_summary_path="landing_validation.json",
                safety_state="ready",
                hard_blockers=[],
                warnings=[],
            )
            manifest_path = attempt_dir / "attempt_candidate.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["browser_resource_relative_paths"].append(
                candidate.dependency_relative_paths[1]
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "failed integrity"):
                load_attempt_candidate(run_dir, candidate.attempt)

    def test_valid_attempt_remains_loadable_when_later_snapshot_is_tampered(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir, first_dir = self._attempt(Path(raw))
            first = self._capture(run_dir, first_dir)
            second_dir = run_dir / "landing_author" / "attempt_02"
            assets_dir = second_dir / "assets"
            assets_dir.mkdir(parents=True)
            (second_dir / "index.html").write_text(
                "<!doctype html><h1>Attempt two</h1>",
                encoding="utf-8",
            )
            (assets_dir / "hero.png").write_bytes(b"second hero")
            (second_dir / "attempt_preview.png").write_bytes(b"second preview")
            (second_dir / "landing_validation.json").write_text(
                json.dumps({"accepted": True}),
                encoding="utf-8",
            )
            capture_attempt_candidate(
                run_dir=run_dir,
                attempt_dir=second_dir,
                artifact_type="landing",
                attempt=2,
                max_attempts=4,
                source_path="index.html",
                dependency_paths=["assets/hero.png"],
                preview_paths=["attempt_preview.png"],
                validation_summary_path="landing_validation.json",
                safety_state="ready",
                hard_blockers=[],
                warnings=[],
            )
            (second_dir / "candidate" / "index.html").write_text(
                "tampered", encoding="utf-8"
            )

            self.assertEqual(
                load_attempt_candidate(run_dir, first.attempt).candidate_id,
                first.candidate_id,
            )
            self.assertEqual(
                [candidate.attempt for candidate in load_attempt_candidates(run_dir)],
                [first.attempt],
            )

    def test_index_rebuild_ignores_partial_and_hash_mismatched_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir, attempt_dir = self._attempt(root)
            (attempt_dir / "attempt_preview_only.png").write_bytes(b"preview")
            empty = rebuild_candidate_index(run_dir)
            self.assertEqual(empty.candidate_ids, [])

            candidate = self._capture(run_dir, attempt_dir)
            (attempt_dir / "candidate/index.html").write_text(
                "<!doctype html><h1>Tampered</h1>",
                encoding="utf-8",
            )
            rebuilt = rebuild_candidate_index(run_dir)
            self.assertEqual(rebuilt.candidate_ids, [])
            with self.assertRaisesRegex(ValueError, "integrity"):
                load_attempt_candidate(run_dir, candidate.attempt)

    def test_selection_journal_round_trips_every_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-1"
            run_dir.mkdir()
            for state in (
                "requested",
                "terminating",
                "promoting",
                "delivering",
                "complete",
                "failed",
            ):
                journal = AttemptSelectionJournal(
                    run_id="run-1",
                    candidate_id="candidate-1",
                    candidate_sha256="a" * 64,
                    source_attempt=1,
                    idempotency_key="request-1",
                    state=state,
                    updated_at="2026-07-29T00:00:00+00:00",
                )
                write_selection_journal(run_dir, journal)
                loaded = load_selection_journal(run_dir)
                self.assertIsNotNone(loaded)
                assert loaded is not None
                self.assertEqual(loaded.state, state)

    def test_poster_capture_creates_editable_snapshot_from_first_attempt(self) -> None:
        from autodesign.agents.external_designer_author import (
            capture_poster_attempt_candidate,
        )

        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run-poster"
            attempt_dir = run_dir / "designer_author" / "attempt_01"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "poster.html").write_text(
                "<!doctype html><html><head></head><body>"
                "<main class='paper-poster'>Poster \\\\(x^2\\\\)</main>"
                "</body></html>",
                encoding="utf-8",
            )
            ctx = ToolContext(
                settings=SimpleNamespace(
                    repo_root=Path(__file__).resolve().parents[1],
                ),
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
                run_id="run-poster",
            )

            preview_had_katex: list[bool] = []

            def fake_preview(*, html_path, preview_path, **_kwargs):
                preview_had_katex.append(
                    "data-autodesign-katex"
                    in html_path.read_text(encoding="utf-8")
                )
                preview_path.write_bytes(b"preview")
                return SimpleNamespace(warnings=[])

            with patch(
                "autodesign.agents.external_designer_author._render_direct_preview",
                side_effect=fake_preview,
            ):
                candidate = capture_poster_attempt_candidate(
                    ctx=ctx,
                    attempt=1,
                    max_attempts=4,
                    attempt_dir=attempt_dir,
                    diagnostics={"candidate_safety_state": "ready"},
                )

            self.assertEqual(candidate.attempt, 1)
            self.assertEqual(preview_had_katex, [True])
            self.assertTrue((attempt_dir / "attempt_candidate.json").is_file())
            self.assertTrue((attempt_dir / "candidate" / "poster.html").is_file())
            self.assertNotIn(
                "data-autodesign-katex",
                (attempt_dir / "candidate" / "poster.html").read_text(
                    encoding="utf-8"
                ),
            )


if __name__ == "__main__":
    unittest.main()
