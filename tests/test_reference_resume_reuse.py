from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autodesign.agents.external_designer_author import (
    ExternalDesignerAuthor,
    _stage_reference_style_inputs,
)
from autodesign.tools._contract import ToolContext


class ReferenceResumeReuseTests(unittest.TestCase):
    def test_resume_uses_audited_preparer_instead_of_attempt_local_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            reference_path = run_dir / "reference.png"
            reference_path.write_bytes(b"same-reference")
            previous_attempt = run_dir / "designer_author" / "attempt_12"
            previous_attempt.mkdir(parents=True)
            untrusted_contract = {
                "version": 4,
                "transfer_mode": "reference_first_reconstruction",
                "style_reference_id": "attempt_local_tampered",
                "source_sha256": "0" * 64,
                "source_page_index": 0,
                "style_tokens": {"body_region_structure": {"regions": []}},
            }
            (previous_attempt / "reference_style_contract.json").write_text(
                json.dumps(untrusted_contract),
                encoding="utf-8",
            )
            (previous_attempt / "reference_style_blueprint.html").write_text(
                "<!doctype html><html><body>tampered</body></html>",
                encoding="utf-8",
            )
            ctx = ToolContext(
                settings=SimpleNamespace(),
                run_id="resume-test",
                run_dir=run_dir,
                layers_dir=run_dir / "layers",
            )
            ctx.state.update({
                "reference_page_index": 0,
                "reference_poster_path": str(reference_path),
                "designer_author_resume": {
                    "previous_attempt_dir": str(previous_attempt),
                },
            })
            settings = SimpleNamespace(
                designer_author_harness="codex",
                designer_author_cmd="true",
                designer_author_timeout_s=60,
                designer_author_max_attempts=1,
                designer_author_model="gpt-test",
            )
            author = ExternalDesignerAuthor(settings, "")

            with (
                patch(
                    "autodesign.agents.external_designer_author.prepare_reference_style_contract"
                ) as prepare,
                patch.object(author, "_ensure_ingested", return_value=False),
            ):
                author.run("Create a poster", ctx)

            prepare.assert_called_once()
            self.assertEqual(prepare.call_args.args[1], reference_path)
            self.assertNotIn("reference_style_contract", ctx.state)

    def test_staging_uses_only_run_level_reference_contract_and_blueprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            contract = {
                "version": 4,
                "transfer_mode": "reference_first_reconstruction",
                "style_reference_id": "reference_test",
                "style_tokens": {"body_region_structure": {"regions": []}},
            }
            blueprint = run_dir / "reference_style_blueprint.html"
            blueprint.write_text(
                "<!doctype html><html><head></head><body></body></html>",
                encoding="utf-8",
            )
            ctx = SimpleNamespace(
                run_id="resume-test",
                run_dir=run_dir,
                state={
                    "reference_poster_path": str(run_dir / "reference.png"),
                    "reference_style_contract": contract,
                    "reference_style_blueprint_path": str(blueprint),
                },
            )
            staged_dir = run_dir / "designer_author" / "attempt_13"
            staged_dir.mkdir(parents=True)

            staged = _stage_reference_style_inputs(ctx, staged_dir)

            self.assertIn("reference_style_contract.json", staged)
            self.assertIn("reference_style_blueprint.html", staged)
            self.assertEqual(
                json.loads((staged_dir / "reference_style_contract.json").read_text()),
                contract,
            )


if __name__ == "__main__":
    unittest.main()
