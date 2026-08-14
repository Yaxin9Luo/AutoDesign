from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import autodesign.eval_protocol as protocol

from autodesign.eval_protocol import (
    EVAL_PROTOCOL,
    EVALUATOR_FINGERPRINT,
    VLM_PROMPT_FINGERPRINT,
    fingerprint_files,
    fingerprint_installed_distributions,
    fingerprint_local_python_closure,
    fingerprint_local_python_symbol_closure,
    fingerprint_python_symbols,
)
from autodesign.evaluator.quality_schema import PosterQualityReport


class EvaluatorProtocolTest(unittest.TestCase):
    def test_runtime_dependency_versions_change_fingerprint(self) -> None:
        with patch.object(
            protocol.metadata,
            "version",
            side_effect=lambda name: {"Pillow": "10.0", "numpy": "1.0"}[name],
        ):
            first = fingerprint_installed_distributions(
                ["Pillow", "numpy"], namespace="test-runtime"
            )
        with patch.object(
            protocol.metadata,
            "version",
            side_effect=lambda name: {"Pillow": "11.0", "numpy": "1.0"}[name],
        ):
            second = fingerprint_installed_distributions(
                ["Pillow", "numpy"], namespace="test-runtime"
            )

        self.assertNotEqual(first, second)

    def test_final_protocol_has_automatic_sha256_fingerprints(self) -> None:
        self.assertEqual(EVAL_PROTOCOL, "posterbench-final")
        self.assertRegex(EVALUATOR_FINGERPRINT, r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(VLM_PROMPT_FINGERPRINT, r"^sha256:[0-9a-f]{64}$")

    def test_fingerprint_changes_when_a_tracked_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "scoring.py"
            source.write_text("SCORE = 1\n", encoding="utf-8")
            first = fingerprint_files([source], namespace="test")
            source.write_text("SCORE = 2\n", encoding="utf-8")
            second = fingerprint_files([source], namespace="test")

        self.assertNotEqual(first, second)

    def test_symbol_fingerprint_ignores_unrelated_report_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "runner.py"
            source.write_text(
                "def score(value):\n    return value + 1\n\n"
                "def render_report():\n    return 'first'\n",
                encoding="utf-8",
            )
            first = fingerprint_python_symbols(source, ["score"], namespace="test")
            source.write_text(
                "def score(value):\n    return value + 1\n\n"
                "def render_report():\n    return 'second'\n",
                encoding="utf-8",
            )
            report_only = fingerprint_python_symbols(source, ["score"], namespace="test")
            source.write_text(
                "def score(value):\n    return value + 2\n\n"
                "def render_report():\n    return 'second'\n",
                encoding="utf-8",
            )
            scoring_change = fingerprint_python_symbols(source, ["score"], namespace="test")

        self.assertEqual(first, report_only)
        self.assertNotEqual(first, scoring_change)

    def test_local_dependency_closure_tracks_transitive_scoring_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "autodesign"
            evaluator = package / "evaluator"
            util = package / "util"
            evaluator.mkdir(parents=True)
            util.mkdir(parents=True)
            for init in (package / "__init__.py", evaluator / "__init__.py", util / "__init__.py"):
                init.write_text("", encoding="utf-8")
            entry = evaluator / "score.py"
            helper = util / "helper.py"
            entry.write_text("from ..util.helper import SCORE\nRESULT = SCORE\n", encoding="utf-8")
            helper.write_text("SCORE = 1\n", encoding="utf-8")
            first = fingerprint_local_python_closure(
                [entry], package_root=package, namespace="test"
            )
            helper.write_text("SCORE = 2\n", encoding="utf-8")
            second = fingerprint_local_python_closure(
                [entry], package_root=package, namespace="test"
            )

        self.assertNotEqual(first, second)

    def test_symbol_dependency_closure_ignores_unused_local_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "autodesign"
            evaluator = package / "evaluator"
            evaluator.mkdir(parents=True)
            for init in (package / "__init__.py", evaluator / "__init__.py"):
                init.write_text("", encoding="utf-8")
            entry = evaluator / "score.py"
            used = evaluator / "used.py"
            unused = evaluator / "unused.py"
            entry.write_text(
                "from .used import SCORE\nfrom .unused import UNUSED\n"
                "def evaluate():\n    return SCORE\n",
                encoding="utf-8",
            )
            used.write_text("SCORE = 1\n", encoding="utf-8")
            unused.write_text("UNUSED = 1\n", encoding="utf-8")
            first = fingerprint_local_python_symbol_closure(
                {entry: ["evaluate"]}, package_root=package, namespace="test"
            )
            unused.write_text("UNUSED = 2\n", encoding="utf-8")
            unused_change = fingerprint_local_python_symbol_closure(
                {entry: ["evaluate"]}, package_root=package, namespace="test"
            )
            used.write_text("SCORE = 2\n", encoding="utf-8")
            used_change = fingerprint_local_python_symbol_closure(
                {entry: ["evaluate"]}, package_root=package, namespace="test"
            )

        self.assertEqual(first, unused_change)
        self.assertNotEqual(first, used_change)

    def test_quality_schema_imports_from_packaged_tree_without_repo_files(self) -> None:
        source_package = Path(__file__).resolve().parents[1] / "autodesign"
        with tempfile.TemporaryDirectory() as tmp:
            isolated_root = Path(tmp)
            shutil.copytree(source_package, isolated_root / "autodesign")
            script = (
                "import sys; "
                f"sys.path.insert(0, {str(isolated_root)!r}); "
                "from autodesign.evaluator.quality_schema import PosterQualityReport; "
                "print(PosterQualityReport.__name__)"
            )
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=isolated_root,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("PosterQualityReport", completed.stdout)

    def test_small_subset_report_help_uses_only_lightweight_dependencies(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "build_small_subset_eval_report.py"
        completed = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=script.parent.parent,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--suite-dir", completed.stdout)

    def test_quality_report_uses_protocol_metadata_without_rubric_version(self) -> None:
        report = PosterQualityReport(
            candidate_name="candidate",
            artifact="poster.png",
            paper=None,
            mode="benchmark",
        ).to_dict()

        self.assertEqual(report["eval_protocol"], EVAL_PROTOCOL)
        self.assertEqual(report["evaluator_fingerprint"], EVALUATOR_FINGERPRINT)
        self.assertNotIn("rubric_version", report)

if __name__ == "__main__":
    unittest.main()
