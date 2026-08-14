from __future__ import annotations

from contextlib import redirect_stderr
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autodesign.cli import _run_oneshot, _terminal_status_exit_code
from autodesign.video_runtime import VideoRuntimeUnavailableError


class CliExitStatusTests(unittest.TestCase):
    def test_terminal_status_mapping(self) -> None:
        self.assertEqual(_terminal_status_exit_code("pass"), 0)
        self.assertEqual(_terminal_status_exit_code("ok"), 0)
        for status in ("revise", "fail", "max_turns", "abort", "unknown", ""):
            with self.subTest(status=status):
                self.assertNotEqual(_terminal_status_exit_code(status), 0)

    def test_run_oneshot_returns_terminal_status_exit_code(self) -> None:
        for terminal_status, expected in (("pass", 0), ("fail", 1), ("abort", 1)):
            with self.subTest(terminal_status=terminal_status):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    run_dir = root / "runs" / "run-1"
                    (run_dir / "final").mkdir(parents=True)
                    settings = SimpleNamespace(out_dir=root)
                    result = SimpleNamespace(
                        run_id="run-1",
                        run_dir=str(run_dir),
                        terminal_status=terminal_status,
                        n_layers=0,
                        n_critiques=0,
                        critic_verdict=None,
                        critic_score=None,
                        wall_s=0.1,
                    )
                    runner = SimpleNamespace(run=lambda *args, **kwargs: result)
                    with (
                        patch("autodesign.cli.load_settings", return_value=settings),
                        patch("autodesign.cli.PipelineRunner", return_value=runner),
                        patch("autodesign.cli._write_cli_events"),
                    ):
                        exit_code = _run_oneshot("brief", [])

                    self.assertEqual(exit_code, expected)

    def test_run_oneshot_reports_video_runtime_setup_without_traceback(self) -> None:
        settings = SimpleNamespace(out_dir=Path("/tmp/autodesign-test"))
        runner = SimpleNamespace(
            run=lambda *args, **kwargs: (_ for _ in ()).throw(
                VideoRuntimeUnavailableError({
                    "ready": False,
                    "missing": ["hyperframes==0.7.86", "ffmpeg"],
                    "repair": "Run `autodesign setup` and install ffmpeg.",
                })
            )
        )
        stderr = io.StringIO()

        with (
            patch("autodesign.cli.load_settings", return_value=settings),
            patch("autodesign.cli.PipelineRunner", return_value=runner),
            redirect_stderr(stderr),
        ):
            exit_code = _run_oneshot("Create a video.", [])

        self.assertEqual(exit_code, 2)
        self.assertIn("autodesign setup", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
