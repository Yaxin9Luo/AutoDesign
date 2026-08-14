from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import time
import unittest

from autodesign.agents.external_author_process import (
    ExternalAuthorProcessRequest,
    process_group_is_alive,
    run_external_author_process,
    terminate_registered_author_process,
)


class ExternalAuthorProcessTests(unittest.TestCase):
    def test_normal_exit_captures_output_and_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            script = root / "author.py"
            script.write_text(
                "import sys\n"
                "text = sys.stdin.read()\n"
                "print('OUT:' + text)\n"
                "print('ERR', file=sys.stderr)\n",
                encoding="utf-8",
            )

            result = run_external_author_process(
                ExternalAuthorProcessRequest(
                    run_id="run-normal",
                    attempt=1,
                    command=[sys.executable, str(script)],
                    cwd=root,
                    prompt="hello",
                    timeout_s=5,
                    stdout_path=root / "stdout.log",
                    stderr_path=root / "stderr.log",
                )
            )

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.returncode, 0)
            self.assertIn("OUT:hello", result.stdout)
            self.assertIn("ERR", result.stderr)
            self.assertIn("OUT:hello", (root / "stdout.log").read_text())

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_selection_terminates_registered_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            child_pid_path = root / "child.pid"
            script = root / "author.py"
            script.write_text(
                textwrap.dedent(
                    f"""
                    import subprocess
                    import sys
                    import time

                    child = subprocess.Popen([
                        sys.executable,
                        "-c",
                        "import time; time.sleep(60)",
                    ])
                    open({str(child_pid_path)!r}, "w", encoding="utf-8").write(str(child.pid))
                    time.sleep(60)
                    """
                ),
                encoding="utf-8",
            )
            request = ExternalAuthorProcessRequest(
                run_id="run-selected",
                attempt=2,
                command=[sys.executable, str(script)],
                cwd=root,
                prompt="author",
                timeout_s=30,
                stdout_path=root / "stdout.log",
                stderr_path=root / "stderr.log",
            )

            with ThreadPoolExecutor(max_workers=1) as executor:
                result_future = executor.submit(run_external_author_process, request)
                deadline = time.monotonic() + 5
                while not child_pid_path.is_file() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(child_pid_path.is_file())
                self.assertTrue(
                    terminate_registered_author_process(
                        "run-selected",
                        "attempt_selected",
                    )
                )
                result = result_future.result(timeout=5)

            self.assertEqual(result.status, "selected")
            self.assertEqual(result.reason, "attempt_selected")
            self.assertFalse(process_group_is_alive(result.process_group_id))

    def test_timeout_is_distinct_from_process_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            script = root / "author.py"
            script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

            result = run_external_author_process(
                ExternalAuthorProcessRequest(
                    run_id="run-timeout",
                    attempt=1,
                    command=[sys.executable, str(script)],
                    cwd=root,
                    prompt="",
                    timeout_s=0.1,
                    stdout_path=root / "stdout.log",
                    stderr_path=root / "stderr.log",
                )
            )

            self.assertEqual(result.status, "timeout")
            self.assertTrue(result.timed_out)

    def test_artifact_completion_condition_stops_process_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            marker = root / "done.json"
            script = root / "author.py"
            script.write_text(
                "import pathlib, time\n"
                "pathlib.Path('done.json').write_text('{}')\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )

            result = run_external_author_process(
                ExternalAuthorProcessRequest(
                    run_id="run-complete",
                    attempt=1,
                    command=[sys.executable, str(script)],
                    cwd=root,
                    prompt="",
                    timeout_s=5,
                    stdout_path=root / "stdout.log",
                    stderr_path=root / "stderr.log",
                    completion_requested=lambda: (
                        "done_marker" if marker.is_file() else None
                    ),
                )
            )

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.reason, "done_marker")

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_duplicate_run_id_does_not_unregister_the_original_process(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ready_path = root / "ready"
            script = root / "author.py"
            script.write_text(
                "import pathlib, time\n"
                "pathlib.Path('ready').write_text('ready')\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            original = ExternalAuthorProcessRequest(
                run_id="run-duplicate",
                attempt=1,
                command=[sys.executable, str(script)],
                cwd=root,
                prompt="",
                timeout_s=10,
                stdout_path=root / "original.stdout.log",
                stderr_path=root / "original.stderr.log",
            )
            duplicate = ExternalAuthorProcessRequest(
                run_id="run-duplicate",
                attempt=2,
                command=[sys.executable, "-c", "pass"],
                cwd=root,
                prompt="",
                timeout_s=10,
                stdout_path=root / "duplicate.stdout.log",
                stderr_path=root / "duplicate.stderr.log",
            )

            with ThreadPoolExecutor(max_workers=1) as executor:
                original_future = executor.submit(
                    run_external_author_process,
                    original,
                )
                deadline = time.monotonic() + 5
                while not ready_path.is_file() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready_path.is_file())

                with self.assertRaisesRegex(
                    RuntimeError,
                    "already registered",
                ):
                    run_external_author_process(duplicate)

                self.assertTrue(
                    terminate_registered_author_process(
                        "run-duplicate",
                        "attempt_selected",
                    )
                )
                result = original_future.result(timeout=5)

            self.assertEqual(result.status, "selected")
            self.assertEqual(result.reason, "attempt_selected")

    def test_spawn_failure_cleans_registry(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = run_external_author_process(
                ExternalAuthorProcessRequest(
                    run_id="run-spawn",
                    attempt=1,
                    command=[str(root / "missing-command")],
                    cwd=root,
                    prompt="",
                    timeout_s=5,
                    stdout_path=root / "stdout.log",
                    stderr_path=root / "stderr.log",
                )
            )

            self.assertEqual(result.status, "spawn_error")
            self.assertFalse(
                terminate_registered_author_process("run-spawn", "attempt_selected")
            )


if __name__ == "__main__":
    unittest.main()
