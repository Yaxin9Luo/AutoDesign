from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from autodesign import config, smoke
from scripts import poster_quality_eval


class SmokePrivateEvalIsolationTests(unittest.TestCase):
    def test_html_reference_smoke_does_not_read_private_eval_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = StringIO()
            with (
                mock.patch.object(config, "REPO_ROOT", Path(tmp)),
                mock.patch.object(
                    poster_quality_eval,
                    "load_eval_set",
                    side_effect=AssertionError("private eval config was read"),
                ),
                redirect_stdout(output),
                redirect_stderr(output),
            ):
                smoke.check_poster_quality_html_reference_no_api()

        self.assertIn("native HTML references feed discovery", output.getvalue())

    def test_named_eval_set_still_fails_closed_without_private_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "poster_quality_sets.json"
            with mock.patch.object(poster_quality_eval, "SET_CONFIG_PATH", missing):
                with self.assertRaisesRegex(SystemExit, "eval set config not found"):
                    poster_quality_eval.load_eval_set("private-set")


if __name__ == "__main__":
    unittest.main()
