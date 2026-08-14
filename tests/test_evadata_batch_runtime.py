from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
BATCH_SCRIPT = REPO / "scripts" / "run_evadata_poster_batch_4x.sh"


class EvaDataBatchRuntimeTest(unittest.TestCase):
    def test_final_artifact_delivery_does_not_require_zero_cli_exit(self) -> None:
        script = BATCH_SCRIPT.read_text(encoding="utf-8")
        success_condition = next(
            line.strip()
            for line in script.splitlines()
            if line.strip().startswith('if [[ -n "$run_dir"')
            and 'final_dir/poster.html' in line
        )

        self.assertNotIn('"$rc" == "0"', success_condition)
        self.assertIn('process_exit_code="$rc"', script)

    def test_reconcile_never_invents_pass_or_zero_exit_status(self) -> None:
        script = BATCH_SCRIPT.read_text(encoding="utf-8")

        self.assertNotIn(
            '"terminal_status": terminal_status or data.get("terminal_status") or "pass"',
            script,
        )
        self.assertGreaterEqual(
            script.count(
                'resolved_terminal_status = terminal_status or recorded_terminal_status or "unknown"'
            ),
            2,
        )
        self.assertGreaterEqual(
            script.count('"exit_code": recorded_exit_code'),
            2,
        )
        self.assertGreaterEqual(
            script.count(
                'data["reconcile_error"] = "run telemetry and status marker did not provide terminal_status"'
            ),
            2,
        )

    def test_resume_reads_completed_status_with_repo_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            paper = root / "paper.pdf"
            paper.write_bytes(b"%PDF-1.4\n")
            batch = root / "batch"
            (batch / "status").mkdir(parents=True)
            task_id = "001-test-paper"
            (batch / "tasks.tsv").write_text(
                f"1\t{task_id}\ttest\tpaper\t{paper}\n",
                encoding="utf-8",
            )
            (batch / "status" / f"{task_id}.done").write_text(
                json.dumps({"task_id": task_id, "terminal_status": "max_turns"}),
                encoding="utf-8",
            )

            env = os.environ.copy()
            env.pop("PYTHON_BIN", None)
            env.update(
                {
                    "REPO": str(REPO),
                    "AUTODESIGN_REPO": str(REPO),
                    "EVADATA_DIR": str(root),
                    "DESIGNER_AUTHOR_HARNESS": "opencode",
                    "PLANNER_CMD": "/bin/true",
                    "SKIP_PROMPT_ENHANCER": "1",
                }
            )
            result = subprocess.run(
                ["/bin/bash", str(BATCH_SCRIPT), "resume", str(batch)],
                cwd=REPO,
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("succeeded (max_turns)", (batch / "index.md").read_text())


if __name__ == "__main__":
    unittest.main()
