from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from types import SimpleNamespace

from autodesign.attempt_candidates import write_selection_journal
from autodesign.agents.external_author_process import (
    ExternalAuthorProcessRequest,
    context_attempt_selection_callback,
    run_external_author_process,
)
from autodesign.process_supervision import (
    process_identity,
    process_is_alive,
    terminate_process_identities,
)
from autodesign.schema import AttemptSelectionJournal
from autodesign.run_control import RunCancelled


class _AlreadyCancelledToken:
    can_cancel = True

    def raise_if_cancelled(self, phase: str) -> None:
        raise RunCancelled("cancel-wins", phase)

    def is_cancelled(self) -> bool:
        return True

    def wait(self, timeout: float, poll_interval: float = 0.01) -> bool:
        del timeout, poll_interval
        return True


def _wait_for_path(path: Path, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.01)
    raise AssertionError(f"path was not created before timeout: {path}")


@unittest.skipUnless(os.name == "posix", "process-tree selection check requires POSIX")
class ExternalAuthorCrossProcessSelectionTests(unittest.TestCase):
    def test_durable_selection_interrupts_author_in_separate_worker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_id = "cross-process-selection"
            run_dir = root / run_id
            run_dir.mkdir()
            pids_path = run_dir / "author-pids.json"
            result_path = run_dir / "worker-result.json"
            late_writes_path = run_dir / "late-writes.log"
            child_script = root / "child.py"
            child_script.write_text(
                "import time\nwhile True:\n    time.sleep(1)\n",
                encoding="utf-8",
            )
            author_script = root / "author.py"
            author_script.write_text(
                textwrap.dedent(
                    f"""
                    import json
                    import os
                    from pathlib import Path
                    import subprocess
                    import sys
                    import time

                    child = subprocess.Popen(
                        [sys.executable, {str(child_script)!r}],
                        start_new_session=True,
                    )
                    Path({str(pids_path)!r}).write_text(
                        json.dumps([os.getpid(), child.pid]),
                        encoding="utf-8",
                    )
                    while True:
                        with Path({str(late_writes_path)!r}).open(
                            "a", encoding="utf-8"
                        ) as handle:
                            handle.write("author-write\\n")
                        time.sleep(0.01)
                    """
                ),
                encoding="utf-8",
            )
            worker_script = root / "worker.py"
            worker_script.write_text(
                textwrap.dedent(
                    f"""
                    import json
                    from pathlib import Path
                    import sys
                    from types import SimpleNamespace

                    from autodesign.agents.external_author_process import (
                        ExternalAuthorProcessRequest,
                        context_attempt_selection_callback,
                        run_external_author_process,
                    )

                    run_dir = Path({str(run_dir)!r})
                    result = run_external_author_process(ExternalAuthorProcessRequest(
                        run_id={run_id!r},
                        attempt=2,
                        command=[sys.executable, {str(author_script)!r}],
                        cwd=run_dir,
                        prompt="x" * (5 * 1024 * 1024),
                        timeout_s=60,
                        stdout_path=run_dir / "stdout.log",
                        stderr_path=run_dir / "stderr.log",
                        run_dir=run_dir,
                        selection_requested=context_attempt_selection_callback(
                            SimpleNamespace(run_dir=run_dir)
                        ),
                    ))
                    Path({str(result_path)!r}).write_text(json.dumps({{
                        "status": result.status,
                        "reason": result.reason,
                    }}), encoding="utf-8")
                    """
                ),
                encoding="utf-8",
            )

            worker = subprocess.Popen(
                [sys.executable, str(worker_script)],
                cwd=Path(__file__).resolve().parents[1],
            )
            identities = []
            try:
                _wait_for_path(pids_path)
                pids = json.loads(pids_path.read_text(encoding="utf-8"))
                identities = [process_identity(int(pid)) for pid in pids]

                write_selection_journal(
                    run_dir,
                    AttemptSelectionJournal(
                        run_id=run_id,
                        candidate_id="poster-attempt-02-selection",
                        candidate_sha256="a" * 64,
                        source_attempt=2,
                        idempotency_key="cross-process-choice",
                        state="promoting",
                        updated_at="2026-08-03T00:00:00+00:00",
                    ),
                )

                worker.wait(timeout=5)
                self.assertEqual(worker.returncode, 0)
                payload = json.loads(result_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["status"], "selected")
                self.assertEqual(
                    payload["reason"],
                    "selected_attempt:attempt_02",
                )
                self.assertTrue(
                    all(not process_is_alive(identity) for identity in identities),
                    "managed external-author PID survived durable selection",
                )
                writes_after_exit = late_writes_path.read_bytes()
                time.sleep(0.15)
                self.assertEqual(late_writes_path.read_bytes(), writes_after_exit)
            finally:
                if identities:
                    terminate_process_identities(
                        identities,
                        root_pid=identities[0].pid,
                        grace_s=0.1,
                    )
                if worker.poll() is None:
                    worker.kill()
                    worker.wait(timeout=3)

    def test_selection_present_before_spawn_never_releases_author(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            for state in (
                "requested",
                "terminating",
                "promoting",
                "delivering",
                "complete",
            ):
                with self.subTest(state=state):
                    run_dir = Path(raw) / f"pre-spawn-{state}"
                    run_dir.mkdir()
                    marker = run_dir / "author-started"
                    write_selection_journal(
                        run_dir,
                        AttemptSelectionJournal(
                            run_id=run_dir.name,
                            candidate_id="poster-attempt-01-selection",
                            candidate_sha256="b" * 64,
                            source_attempt=1,
                            idempotency_key="pre-spawn-choice",
                            state=state,
                            updated_at="2026-08-03T00:00:00+00:00",
                        ),
                    )

                    result = run_external_author_process(
                        ExternalAuthorProcessRequest(
                            run_id=run_dir.name,
                            attempt=1,
                            command=[
                                sys.executable,
                                "-c",
                                "from pathlib import Path; "
                                f"Path({str(marker)!r}).touch()",
                            ],
                            cwd=run_dir,
                            prompt="",
                            timeout_s=5,
                            stdout_path=run_dir / "stdout.log",
                            stderr_path=run_dir / "stderr.log",
                            run_dir=run_dir,
                            selection_requested=context_attempt_selection_callback(
                                SimpleNamespace(run_dir=run_dir)
                            ),
                        )
                    )

                    self.assertEqual(result.status, "selected")
                    self.assertIsNone(result.process_group_id)
                    self.assertFalse(marker.exists())

    def test_complete_selection_stops_running_author_tree_without_late_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "running-complete-selection"
            run_dir.mkdir()
            pids_path = run_dir / "pids.json"
            writes_path = run_dir / "writes.log"
            author = run_dir / "author.py"
            author.write_text(
                textwrap.dedent(
                    f"""
                    import json
                    import os
                    from pathlib import Path
                    import subprocess
                    import sys
                    import time

                    child = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(60)"],
                        start_new_session=True,
                    )
                    Path({str(pids_path)!r}).write_text(
                        json.dumps([os.getpid(), child.pid]), encoding="utf-8"
                    )
                    while True:
                        with Path({str(writes_path)!r}).open(
                            "a", encoding="utf-8"
                        ) as handle:
                            handle.write("write\\n")
                        time.sleep(0.01)
                    """
                ),
                encoding="utf-8",
            )
            request = ExternalAuthorProcessRequest(
                run_id=run_dir.name,
                attempt=1,
                command=[sys.executable, str(author)],
                cwd=run_dir,
                prompt="",
                timeout_s=60,
                stdout_path=run_dir / "stdout.log",
                stderr_path=run_dir / "stderr.log",
                run_dir=run_dir,
                selection_requested=context_attempt_selection_callback(
                    SimpleNamespace(run_dir=run_dir)
                ),
            )
            identities = []
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(run_external_author_process, request)
                try:
                    _wait_for_path(pids_path)
                    identities = [
                        process_identity(int(pid))
                        for pid in json.loads(pids_path.read_text(encoding="utf-8"))
                    ]
                    write_selection_journal(
                        run_dir,
                        AttemptSelectionJournal(
                            run_id=run_dir.name,
                            candidate_id="poster-attempt-01-selection",
                            candidate_sha256="d" * 64,
                            source_attempt=1,
                            idempotency_key="completed-selection",
                            state="complete",
                            updated_at="2026-08-03T00:00:00+00:00",
                        ),
                    )
                    result = future.result(timeout=5)
                finally:
                    if identities:
                        terminate_process_identities(
                            identities,
                            root_pid=identities[0].pid,
                            grace_s=0.1,
                        )

            self.assertEqual(result.status, "selected")
            self.assertTrue(all(not process_is_alive(item) for item in identities))
            writes_after_exit = writes_path.read_bytes()
            time.sleep(0.15)
            self.assertEqual(writes_path.read_bytes(), writes_after_exit)

    def test_corrupt_selection_journal_is_ignored_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "corrupt-selection"
            journal_path = run_dir / "attempt_candidates" / "selection.json"
            journal_path.parent.mkdir(parents=True)
            journal_path.write_text("{not-json", encoding="utf-8")
            before = journal_path.read_bytes()
            callback = context_attempt_selection_callback(
                SimpleNamespace(run_dir=run_dir)
            )
            self.assertIsNotNone(callback)
            assert callback is not None

            self.assertIsNone(callback())
            self.assertEqual(journal_path.read_bytes(), before)

    def test_run_cancellation_wins_when_selection_is_also_pending(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "cancel-wins"
            run_dir.mkdir()
            write_selection_journal(
                run_dir,
                AttemptSelectionJournal(
                    run_id=run_dir.name,
                    candidate_id="poster-attempt-01-selection",
                    candidate_sha256="c" * 64,
                    source_attempt=1,
                    idempotency_key="cancel-race",
                    state="requested",
                    updated_at="2026-08-03T00:00:00+00:00",
                ),
            )

            with self.assertRaises(RunCancelled):
                run_external_author_process(
                    ExternalAuthorProcessRequest(
                        run_id=run_dir.name,
                        attempt=1,
                        command=[sys.executable, "-c", "pass"],
                        cwd=run_dir,
                        prompt="",
                        timeout_s=5,
                        stdout_path=run_dir / "stdout.log",
                        stderr_path=run_dir / "stderr.log",
                        run_dir=run_dir,
                        cancellation_token=_AlreadyCancelledToken(),
                        interruption_requested=lambda: True,
                        selection_requested=context_attempt_selection_callback(
                            SimpleNamespace(run_dir=run_dir)
                        ),
                    )
                )


if __name__ == "__main__":
    unittest.main()
