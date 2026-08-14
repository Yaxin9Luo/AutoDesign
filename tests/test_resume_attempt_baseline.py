from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from autodesign.agents.external_designer_author import _record_feedback
from autodesign.runner import (
    _archive_superseded_resume_attempts,
    _load_resume_state,
)
from autodesign.util.io import sha256_file


class ResumeAttemptBaselineTests(unittest.TestCase):
    def _write_resume_metadata(self, run_dir: Path) -> None:
        run_dir.mkdir()
        (run_dir / "run_brief.json").write_text(
            json.dumps({"version": 1}),
            encoding="utf-8",
        )
        (run_dir / "resume_state.json").write_text("{}", encoding="utf-8")
        (run_dir / "canvas_plan.json").write_text("{}", encoding="utf-8")

    def _write_completed_attempt(
        self,
        attempt_dir: Path,
        *,
        attempt_index: int,
        feedback: dict[str, object],
    ) -> dict[str, object]:
        attempt_dir.mkdir(parents=True, exist_ok=True)
        poster_path = attempt_dir / "poster.html"
        poster_path.write_text(
            f"<html><body>attempt {attempt_index}</body></html>",
            encoding="utf-8",
        )
        poster_sha = sha256_file(poster_path)
        completed_feedback = {
            "attempt": attempt_index,
            "attempt_dir": str(attempt_dir),
            "validated_attempt": attempt_index,
            "validated_attempt_dir": str(attempt_dir.resolve()),
            "validated_poster_sha256": poster_sha,
            "invocation_poster_sha256": poster_sha,
            **feedback,
        }
        (attempt_dir / "validation_feedback.json").write_text(
            json.dumps(completed_feedback),
            encoding="utf-8",
        )
        (attempt_dir / "designer_author_log.json").write_text(
            json.dumps({
                "returncode": 0,
                "reason": "done_marker",
                "timeout": False,
                "poster_sha256": poster_sha,
            }),
            encoding="utf-8",
        )
        return completed_feedback

    def test_resume_skips_timed_out_attempt_with_poster_and_staged_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "resume-run"
            self._write_resume_metadata(run_dir)
            older = run_dir / "designer_author" / "attempt_01"
            newer = run_dir / "designer_author" / "attempt_02"
            older_feedback = self._write_completed_attempt(
                older,
                attempt_index=1,
                feedback={"summary": {"issue_id": "older-complete-attempt"}},
            )
            newer.mkdir()
            newer_poster = newer / "poster.html"
            newer_poster.write_text("<html><body>partial</body></html>", encoding="utf-8")
            (newer / "validation_feedback.json").write_text(
                json.dumps(older_feedback),
                encoding="utf-8",
            )
            (newer / "designer_author_log.json").write_text(
                json.dumps({
                    "returncode": None,
                    "reason": "timeout",
                    "timeout": True,
                    "poster_sha256": sha256_file(newer_poster),
                }),
                encoding="utf-8",
            )

            resume = _load_resume_state(run_dir)

            self.assertIsInstance(resume, dict)
            self.assertEqual(resume["previous_attempt_dir"], older)
            self.assertEqual(resume["prior_feedback"], older_feedback)
            self.assertEqual(resume["prior_attempts"], 1)
            self.assertEqual(resume["superseded_attempt_dirs"], [newer])

            archived = _archive_superseded_resume_attempts(resume)

            self.assertEqual(len(archived), 1)
            self.assertFalse(newer.exists())
            self.assertTrue(archived[0].is_dir())
            self.assertEqual(archived[0].name, newer.name)
            self.assertEqual(
                archived[0].parent.parent,
                run_dir / "designer_author" / "interrupted_attempts",
            )

    def test_resume_accepts_completed_invocation_with_current_validation_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "resume-run"
            self._write_resume_metadata(run_dir)
            attempt_dir = run_dir / "designer_author" / "attempt_07"
            expected_feedback = self._write_completed_attempt(
                attempt_dir,
                attempt_index=7,
                feedback={"summary": {"issue_id": "current-validation"}},
            )

            resume = _load_resume_state(run_dir)

            self.assertIsInstance(resume, dict)
            self.assertEqual(resume["previous_attempt_dir"], attempt_dir)
            self.assertEqual(resume["prior_feedback"], expected_feedback)

    def test_resume_sorts_attempts_numerically_and_preserves_indices(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "resume-run"
            self._write_resume_metadata(run_dir)

            attempt_dirs = {}
            for attempt_index in (2, 99, 100):
                attempt_dir = (
                    run_dir / "designer_author" / f"attempt_{attempt_index}"
                )
                self._write_completed_attempt(
                    attempt_dir,
                    attempt_index=attempt_index,
                    feedback={},
                )
                attempt_dirs[attempt_index] = attempt_dir

            resume = _load_resume_state(run_dir)

            self.assertIsInstance(resume, dict)
            self.assertEqual(resume["prior_attempts"], 100)
            self.assertEqual(
                [record["attempt"] for record in resume["attempt_records"]],
                [2, 99, 100],
            )
            self.assertEqual(
                [Path(record["attempt_dir"]) for record in resume["attempt_records"]],
                [attempt_dirs[2], attempt_dirs[99], attempt_dirs[100]],
            )
            self.assertEqual(resume["previous_attempt_dir"], attempt_dirs[100])
            self.assertEqual(
                resume["prior_feedback"],
                {
                    "attempt": 100,
                    "attempt_dir": str(attempt_dirs[100]),
                    "validated_attempt": 100,
                    "validated_attempt_dir": str(attempt_dirs[100].resolve()),
                    "validated_poster_sha256": sha256_file(attempt_dirs[100] / "poster.html"),
                    "invocation_poster_sha256": sha256_file(attempt_dirs[100] / "poster.html"),
                },
            )

    def test_resume_accepts_post_repair_poster_bound_by_validation_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "resume-run"
            self._write_resume_metadata(run_dir)
            attempt_dir = run_dir / "designer_author" / "attempt_03"
            self._write_completed_attempt(attempt_dir, attempt_index=3, feedback={})
            invocation = json.loads(
                (attempt_dir / "designer_author_log.json").read_text(encoding="utf-8")
            )
            poster_path = attempt_dir / "poster.html"
            poster_path.write_text(
                "<html><body>attempt 3 after local repair</body></html>",
                encoding="utf-8",
            )
            _record_feedback(
                SimpleNamespace(state={}),
                attempt_dir,
                {
                    "attempt": 3,
                    "attempt_dir": str(attempt_dir),
                    "summary": {"issue_id": "post-repair-validation"},
                },
            )
            feedback = json.loads(
                (attempt_dir / "validation_feedback.json").read_text(encoding="utf-8")
            )

            self.assertEqual(feedback["validated_poster_sha256"], sha256_file(poster_path))
            self.assertEqual(
                feedback["invocation_poster_sha256"],
                invocation["poster_sha256"],
            )

            resume = _load_resume_state(run_dir)

            self.assertIsInstance(resume, dict)
            self.assertEqual(resume["previous_attempt_dir"], attempt_dir)
            self.assertEqual(resume["prior_feedback"], feedback)


if __name__ == "__main__":
    unittest.main()
