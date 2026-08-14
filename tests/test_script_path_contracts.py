from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import audit_pdf_visual_ingest as ingest_audit
from scripts import poster_quality_eval
from scripts import spike_pdf_figures


class ScriptPathContractTests(unittest.TestCase):
    def test_ingest_audit_requires_explicit_paper_selection(self) -> None:
        args = ingest_audit._parse_args([])

        self.assertEqual(
            args.out.parent.parent,
            ingest_audit.REPO_ROOT / "out",
        )
        with self.assertRaisesRegex(SystemExit, "Provide --paper"):
            ingest_audit._resolve_papers(args)

    def test_ingest_audit_requires_a_root_for_bulk_discovery(self) -> None:
        args = ingest_audit._parse_args(["--all-papers"])

        with self.assertRaisesRegex(SystemExit, "--all-papers requires --paper-root"):
            ingest_audit._resolve_papers(args)

    def test_ingest_audit_discovers_papers_from_cli_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paper = root / "example-paper" / "paper.pdf"
            paper.parent.mkdir()
            paper.write_bytes(b"%PDF-1.7\n")
            args = ingest_audit._parse_args(
                ["--all-papers", "--paper-root", str(root)]
            )

            self.assertEqual(
                ingest_audit._resolve_papers(args),
                {"example-paper": paper.resolve()},
            )

    def test_poster_eval_does_not_fall_back_to_a_sibling_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository_root = root / "workspace" / "repo"
            sibling_data = repository_root.parent / "data"
            sibling_data.mkdir(parents=True)

            with patch.object(poster_quality_eval, "_REPO_ROOT", repository_root):
                self.assertEqual(
                    poster_quality_eval._resolve_data_dir(None),
                    (repository_root / "data").resolve(),
                )

    def test_poster_eval_defaults_to_a_repository_owned_output_directory(self) -> None:
        args = poster_quality_eval._parse_args([])

        self.assertEqual(
            Path(args.out_dir),
            poster_quality_eval._REPO_ROOT / "out" / "poster_quality_eval",
        )

    def test_pdf_figure_spike_uses_explicit_positional_paths(self) -> None:
        args = spike_pdf_figures._parse_args(["paper.pdf", "out/pdf-figures"])

        self.assertEqual(args.pdf, Path("paper.pdf"))
        self.assertEqual(args.out_dir, Path("out/pdf-figures"))


if __name__ == "__main__":
    unittest.main()
