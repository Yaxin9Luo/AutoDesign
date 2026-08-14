from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import autodesign.agents.external_designer_author as poster_author_module
import autodesign.agents.external_landing_author as landing_author_module
import autodesign.agents.external_slides_author as slides_author_module
import autodesign.agents.external_video_author as video_author_module
import autodesign.agents.atomic_artifact_promotion as atomic_promotion_module
import autodesign.agents.external_author_process as author_process_module
import autodesign.process_supervision as process_supervision_module
from autodesign.agents.external_author_process import (
    ExternalAuthorProcessRequest,
    ExternalAuthorProcessResult,
    run_external_author_process,
    terminate_registered_author_process,
)
from autodesign.config import Settings
from autodesign.process_supervision import (
    ProcessIdentity,
    ProcessLedger,
    process_identity,
    process_is_alive,
    spawn_registered_process,
    terminate_process_identities,
)
from autodesign.run_control import CancellationToken, RunCancelled, RunControlStore
from autodesign.run_supervisor import RunSupervisor
from autodesign.run_worker_protocol import PipelineWorkerRequest
from autodesign.tools._contract import ToolContext


def _wait_for(predicate, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before polling deadline")


def _pid_is_dead(pid: int) -> bool:
    try:
        process_identity(pid)
    except (OSError, ProcessLookupError, ValueError):
        return True
    return False


async def _wait_for_async(predicate, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met before polling deadline")


class _PhaseCancellationToken:
    can_cancel = True

    def __init__(
        self,
        phase: str,
        *,
        run_id: str,
        raise_on_trigger: bool = True,
    ) -> None:
        self.run_id = run_id
        self.phase = phase
        self.raise_on_trigger = raise_on_trigger
        self.cancelled = False
        self.seen_phases: list[str] = []

    def is_cancelled(self) -> bool:
        return self.cancelled

    def raise_if_cancelled(self, phase: str) -> None:
        self.seen_phases.append(phase)
        if phase == self.phase:
            self.cancelled = True
            if not self.raise_on_trigger:
                return
        if self.cancelled:
            raise RunCancelled(self.run_id, phase)

    def wait(self, timeout: float, poll_interval: float = 0.01) -> bool:
        del timeout, poll_interval
        return self.cancelled


class _MutableToken:
    can_cancel = True

    def __init__(self, run_id: str = "author-loop") -> None:
        self.run_id = run_id
        self.cancelled = False

    def is_cancelled(self) -> bool:
        return self.cancelled

    def raise_if_cancelled(self, phase: str) -> None:
        if self.cancelled:
            raise RunCancelled(self.run_id, phase)

    def wait(self, timeout: float, poll_interval: float = 0.01) -> bool:
        del timeout, poll_interval
        return self.cancelled


class ExternalAuthorCancellationTests(unittest.TestCase):
    def _control(self, root: Path, run_id: str) -> tuple[RunControlStore, CancellationToken, Path]:
        runs_dir = root / "runs"
        store = RunControlStore(runs_dir)
        reserved = store.reserve(run_id, "poster")
        store.transition(run_id, reserved, "queued")
        running = store.read(run_id)
        store.transition(run_id, running, "running")
        return store, CancellationToken.for_run(store, run_id), runs_dir / run_id

    def test_registered_spawn_is_durable_before_target_work_and_preserves_stdin(self) -> None:
        """Removing the registered shim or releasing before fsync must fail this test."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "run"
            run_dir.mkdir()
            observation = root / "observation.json"
            script = root / "author.py"
            script.write_text(
                textwrap.dedent(
                    f"""
                    import json
                    import os
                    import pathlib
                    import sys

                    ledger = json.loads(pathlib.Path({str(run_dir / 'process_ledger.json')!r}).read_text())
                    registered = any(
                        item["identity"]["pid"] == os.getpid()
                        and item["role"] == "external-author"
                        for item in ledger["processes"]
                    )
                    pathlib.Path({str(observation)!r}).write_text(json.dumps({{
                        "registered": registered,
                        "stdin": sys.stdin.read(),
                    }}))
                    """
                ),
                encoding="utf-8",
            )

            result = run_external_author_process(
                ExternalAuthorProcessRequest(
                    run_id="durable-spawn",
                    attempt=1,
                    command=[sys.executable, str(script)],
                    cwd=root,
                    prompt="private prompt",
                    timeout_s=5,
                    stdout_path=root / "stdout.log",
                    stderr_path=root / "stderr.log",
                    run_dir=run_dir,
                )
            )

            payload = json.loads(observation.read_text(encoding="utf-8"))
            self.assertEqual(result.status, "ok")
            self.assertTrue(payload["registered"])
            self.assertEqual(payload["stdin"], "private prompt")
            self.assertEqual(len(ProcessLedger(run_dir).read().processes), 1)

    @unittest.skipUnless(os.name == "posix", "real detached descendant check requires POSIX")
    def test_run_cancel_kills_author_and_detached_grandchild_without_late_writes(self) -> None:
        """Blind parent-only termination leaves the detached writer alive."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store, token, run_dir = self._control(root, "cancel-tree")
            pids_path = run_dir / "author-pids.json"
            marker = run_dir / "late-marker.log"
            child_code = (
                "import pathlib,time; p=pathlib.Path(" + repr(str(marker)) + "); "
                "\nwhile True:\n p.open('a').write('child\\n'); time.sleep(.01)"
            )
            script = root / "author.py"
            script.write_text(
                textwrap.dedent(
                    f"""
                    import json
                    import os
                    import pathlib
                    import subprocess
                    import sys
                    import time

                    child = subprocess.Popen(
                        [sys.executable, "-c", {child_code!r}],
                        start_new_session=True,
                    )
                    pathlib.Path({str(pids_path)!r}).write_text(json.dumps([os.getpid(), child.pid]))
                    marker = pathlib.Path({str(marker)!r})
                    while True:
                        with marker.open("a", encoding="utf-8") as handle:
                            handle.write("author\\n")
                        time.sleep(0.01)
                    """
                ),
                encoding="utf-8",
            )
            request = ExternalAuthorProcessRequest(
                run_id="cancel-tree",
                attempt=1,
                command=[sys.executable, str(script)],
                cwd=root,
                prompt="",
                timeout_s=30,
                stdout_path=run_dir / "stdout.log",
                stderr_path=run_dir / "stderr.log",
                run_dir=run_dir,
                cancellation_token=token,
            )

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(run_external_author_process, request)
                _wait_for(pids_path.is_file)
                pids = json.loads(pids_path.read_text(encoding="utf-8"))
                _wait_for(lambda: marker.is_file() and marker.stat().st_size > 0)
                store.request_cancel("cancel-tree")
                with self.assertRaises(RunCancelled) as caught:
                    future.result(timeout=5)

            self.assertEqual(caught.exception.run_id, "cancel-tree")
            self.assertIn("run_cancelled", caught.exception.phase)
            for pid in pids:
                _wait_for(lambda pid=pid: _pid_is_dead(pid))
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            size_after_cancel = marker.stat().st_size
            time.sleep(0.08)
            self.assertEqual(marker.stat().st_size, size_after_cancel)

    def test_cancelled_author_never_persists_unredacted_process_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store, token, run_dir = self._control(root, "redacted-cancel")
            ready = run_dir / "ready"
            explicit_secret = "explicit-secret-value"
            env_secret = "environment-secret-value"
            command_secret = "command-secret-value"
            script = root / "author.py"
            script.write_text(
                textwrap.dedent(
                    f"""
                    import os
                    from pathlib import Path
                    import sys
                    import time

                    print({explicit_secret!r} * 500, flush=True)
                    print(os.environ["AUTHOR_API_KEY"] * 500, flush=True)
                    print(sys.argv[1] * 500, flush=True)
                    print({explicit_secret!r} * 500, file=sys.stderr, flush=True)
                    print(os.environ["AUTHOR_API_KEY"] * 500, file=sys.stderr, flush=True)
                    print(sys.argv[1] * 500, file=sys.stderr, flush=True)
                    Path({str(ready)!r}).write_text("ready")
                    time.sleep(30)
                    """
                ),
                encoding="utf-8",
            )
            env = dict(os.environ)
            env["AUTHOR_API_KEY"] = env_secret
            request = ExternalAuthorProcessRequest(
                run_id="redacted-cancel",
                attempt=1,
                command=[
                    sys.executable,
                    str(script),
                    f"--api-key={command_secret}",
                ],
                cwd=root,
                prompt="prompt-must-not-become-a-redaction-source",
                timeout_s=30,
                stdout_path=run_dir / "stdout.log",
                stderr_path=run_dir / "stderr.log",
                env=env,
                run_dir=run_dir,
                cancellation_token=token,
                sensitive_values=(explicit_secret,),
            )

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(run_external_author_process, request)
                _wait_for(ready.is_file)
                for path in run_dir.rglob("*"):
                    if path.is_file():
                        content = path.read_bytes()
                        self.assertNotIn(explicit_secret.encode(), content)
                        self.assertNotIn(env_secret.encode(), content)
                        self.assertNotIn(command_secret.encode(), content)
                store.request_cancel("redacted-cancel")
                with self.assertRaises(RunCancelled):
                    future.result(timeout=5)

            for path in run_dir.rglob("*"):
                if path.is_file():
                    content = path.read_bytes()
                    self.assertNotIn(explicit_secret.encode(), content)
                    self.assertNotIn(env_secret.encode(), content)
                    self.assertNotIn(command_secret.encode(), content)
            self.assertIn(b"[REDACTED]", (run_dir / "stdout.log").read_bytes())
            self.assertIn(b"[REDACTED]", (run_dir / "stderr.log").read_bytes())

    @unittest.skipUnless(os.name == "posix", "detached pipe owner requires POSIX")
    def test_natural_parent_exit_terminates_tracked_detached_pipe_owner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "run"
            run_dir.mkdir()
            pids_path = run_dir / "pids.json"
            marker = run_dir / "late-writes.log"
            process_secret = "detached-process-secret"
            grandchild = root / "grandchild.py"
            grandchild.write_text(
                textwrap.dedent(
                    f"""
                    from pathlib import Path
                    import signal
                    import time

                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    marker = Path({str(marker)!r})
                    while True:
                        with marker.open("a", encoding="utf-8") as handle:
                            handle.write("late\\n")
                        time.sleep(0.01)
                    """
                ),
                encoding="utf-8",
            )
            parent = root / "parent.py"
            parent.write_text(
                textwrap.dedent(
                    f"""
                    import json
                    import os
                    from pathlib import Path
                    import subprocess
                    import sys
                    import time

                    child = subprocess.Popen(
                        [sys.executable, {str(grandchild)!r}],
                        start_new_session=True,
                    )
                    Path({str(pids_path)!r}).write_text(json.dumps([os.getpid(), child.pid]))
                    print({process_secret!r}, flush=True)
                    print({process_secret!r}, file=sys.stderr, flush=True)
                    time.sleep(0.2)
                    """
                ),
                encoding="utf-8",
            )
            request = ExternalAuthorProcessRequest(
                run_id="natural-exit-tree",
                attempt=1,
                command=[sys.executable, str(parent)],
                cwd=root,
                prompt="",
                timeout_s=10,
                stdout_path=run_dir / "stdout.log",
                stderr_path=run_dir / "stderr.log",
                run_dir=run_dir,
                sensitive_values=(process_secret,),
            )

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(run_external_author_process, request)
                _wait_for(pids_path.is_file)
                pids = json.loads(pids_path.read_text(encoding="utf-8"))
                _wait_for(lambda: marker.is_file() and marker.stat().st_size > 0)
                _wait_for(lambda: _pid_is_dead(pids[0]))
                with author_process_module._REGISTRY_LOCK:
                    self.assertIn("natural-exit-tree", author_process_module._REGISTRY)
                result = future.result(timeout=5)

            self.assertEqual(result.status, "ok")
            for pid in pids:
                _wait_for(lambda pid=pid: _pid_is_dead(pid))
            size_after_return = marker.stat().st_size
            time.sleep(0.08)
            self.assertEqual(marker.stat().st_size, size_after_return)
            for path in run_dir.rglob("*"):
                if path.is_file():
                    self.assertNotIn(process_secret.encode(), path.read_bytes())

    @unittest.skipUnless(os.name == "posix", "detached child race requires POSIX")
    def test_immediate_parent_exit_still_terminates_detached_child(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "run"
            run_dir.mkdir()
            child_pid_path = run_dir / "child.pid"
            child = root / "child.py"
            child.write_text("import time; time.sleep(30)\n", encoding="utf-8")
            parent = root / "parent.py"
            parent.write_text(
                textwrap.dedent(
                    f"""
                    from pathlib import Path
                    import subprocess
                    import sys

                    child = subprocess.Popen(
                        [sys.executable, {str(child)!r}],
                        start_new_session=True,
                    )
                    Path({str(child_pid_path)!r}).write_text(str(child.pid))
                    """
                ),
                encoding="utf-8",
            )
            child_pid: int | None = None
            try:
                result = run_external_author_process(
                    ExternalAuthorProcessRequest(
                        run_id="immediate-parent-exit",
                        attempt=1,
                        command=[sys.executable, str(parent)],
                        cwd=root,
                        prompt="",
                        timeout_s=5,
                        stdout_path=run_dir / "stdout.log",
                        stderr_path=run_dir / "stderr.log",
                        run_dir=run_dir,
                    )
                )
                self.assertEqual(result.status, "ok")
                self.assertTrue(child_pid_path.is_file())
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                _wait_for(lambda: _pid_is_dead(child_pid))
            finally:
                if child_pid is None and child_pid_path.is_file():
                    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                if child_pid is not None and not _pid_is_dead(child_pid):
                    os.kill(child_pid, 9)

    @unittest.skipUnless(os.name == "posix", "detached double-fork check requires POSIX")
    def test_immediate_double_fork_is_reaped_by_durable_owner_nonce(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "run"
            run_dir.mkdir()
            grandchild_pid_path = run_dir / "grandchild.pid"
            grandchild = root / "grandchild.py"
            grandchild.write_text("import time; time.sleep(30)\n", encoding="utf-8")
            child = root / "child.py"
            child.write_text(
                textwrap.dedent(
                    f"""
                    from pathlib import Path
                    import subprocess
                    import sys

                    grandchild = subprocess.Popen(
                        [sys.executable, {str(grandchild)!r}],
                        start_new_session=True,
                    )
                    Path({str(grandchild_pid_path)!r}).write_text(str(grandchild.pid))
                    """
                ),
                encoding="utf-8",
            )
            parent = root / "parent.py"
            parent.write_text(
                textwrap.dedent(
                    f"""
                    import subprocess
                    import sys

                    subprocess.Popen(
                        [sys.executable, {str(child)!r}],
                        start_new_session=True,
                    )
                    """
                ),
                encoding="utf-8",
            )
            grandchild_pid: int | None = None
            try:
                with patch.object(
                    author_process_module,
                    "_capture_registered_descendants",
                ):
                    result = run_external_author_process(
                        ExternalAuthorProcessRequest(
                            run_id="immediate-double-fork",
                            attempt=1,
                            command=[sys.executable, str(parent)],
                            cwd=root,
                            prompt="",
                            timeout_s=5,
                            stdout_path=run_dir / "stdout.log",
                            stderr_path=run_dir / "stderr.log",
                            run_dir=run_dir,
                        )
                    )
                self.assertEqual(result.status, "ok")
                self.assertTrue(grandchild_pid_path.is_file())
                grandchild_pid = int(grandchild_pid_path.read_text(encoding="utf-8"))
                _wait_for(lambda: _pid_is_dead(grandchild_pid))
            finally:
                if grandchild_pid is None and grandchild_pid_path.is_file():
                    grandchild_pid = int(grandchild_pid_path.read_text(encoding="utf-8"))
                if grandchild_pid is not None and not _pid_is_dead(grandchild_pid):
                    os.kill(grandchild_pid, 9)

    def test_cancel_before_spawn_creates_no_logs_or_target_work(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store, token, run_dir = self._control(root, "cancel-before")
            marker = root / "target-started"
            store.request_cancel("cancel-before")

            with self.assertRaises(RunCancelled):
                run_external_author_process(
                    ExternalAuthorProcessRequest(
                        run_id="cancel-before",
                        attempt=1,
                        command=[
                            sys.executable,
                            "-c",
                            f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')",
                        ],
                        cwd=root,
                        prompt="",
                        timeout_s=5,
                        stdout_path=run_dir / "stdout.log",
                        stderr_path=run_dir / "stderr.log",
                        run_dir=run_dir,
                        cancellation_token=token,
                    )
                )

            self.assertFalse(marker.exists())
            self.assertFalse((run_dir / "stdout.log").exists())
            self.assertEqual(ProcessLedger(run_dir).read().processes, ())

    def test_cancel_during_spawn_registration_never_releases_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_dir = root / "run"
            run_dir.mkdir()
            marker = root / "target-started"
            token = _PhaseCancellationToken(
                "external_author.before_spawn_release",
                run_id="registration-race",
            )

            with self.assertRaises(RunCancelled):
                run_external_author_process(
                    ExternalAuthorProcessRequest(
                        run_id=token.run_id,
                        attempt=1,
                        command=[
                            sys.executable,
                            "-c",
                            f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')",
                        ],
                        cwd=root,
                        prompt="",
                        timeout_s=5,
                        stdout_path=root / "stdout.log",
                        stderr_path=root / "stderr.log",
                        run_dir=run_dir,
                        cancellation_token=token,
                    )
                )

            self.assertFalse(marker.exists())
            snapshot = ProcessLedger(run_dir).read()
            self.assertEqual(len(snapshot.processes), 1)
            self.assertFalse(process_is_alive(snapshot.processes[0].identity))
            self.assertTrue(all(item.status == "failed" for item in snapshot.spawning))
            self.assertIn("external_author.before_spawn_release", token.seen_phases)

    def test_release_hook_failure_closes_hidden_popen_streams(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run"
            run_dir.mkdir()
            captured: list[subprocess.Popen[bytes]] = []
            real_popen = subprocess.Popen
            release_error = RuntimeError("release denied")

            def capture_popen(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                captured.append(process)
                return process

            def deny_release(_identity: ProcessIdentity) -> None:
                raise release_error

            with patch.object(
                process_supervision_module.subprocess,
                "Popen",
                side_effect=capture_popen,
            ):
                with self.assertRaises(RuntimeError) as caught:
                    spawn_registered_process(
                        ProcessLedger(run_dir),
                        [sys.executable, "-c", "pass"],
                        role="external-author",
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        release_hook=deny_release,
                    )

            self.assertIs(caught.exception, release_error)
            self.assertEqual(len(captured), 1)
            process = captured[0]
            self.assertIsNotNone(process.poll())
            for stream in (process.stdin, process.stdout, process.stderr):
                self.assertIsNotNone(stream)
                self.assertTrue(stream.closed)

    @unittest.skipUnless(os.name == "posix", "nonblocking pipe regression requires POSIX")
    def test_cancel_interrupts_blocked_large_prompt_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store, token, run_dir = self._control(root, "blocked-prompt")
            ready = run_dir / "ready"
            script = root / "author.py"
            script.write_text(
                "from pathlib import Path\nimport time\n"
                f"Path({str(ready)!r}).write_text('ready')\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            request = ExternalAuthorProcessRequest(
                run_id="blocked-prompt",
                attempt=1,
                command=[sys.executable, str(script)],
                cwd=root,
                prompt="x" * (8 * 1024 * 1024),
                timeout_s=30,
                stdout_path=run_dir / "stdout.log",
                stderr_path=run_dir / "stderr.log",
                run_dir=run_dir,
                cancellation_token=token,
            )
            executor = ThreadPoolExecutor(max_workers=1)
            future = executor.submit(run_external_author_process, request)
            delayed = False
            try:
                _wait_for(ready.is_file)
                started = time.monotonic()
                store.request_cancel("blocked-prompt")
                try:
                    future.result(timeout=1.5)
                except RunCancelled:
                    pass
                except FutureTimeoutError:
                    delayed = True
                    terminate_registered_author_process(
                        "blocked-prompt", "test_cleanup"
                    )
                    with self.assertRaises(RunCancelled):
                        future.result(timeout=3)
                self.assertLess(time.monotonic() - started, 1.5)
            finally:
                if not future.done():
                    terminate_registered_author_process(
                        "blocked-prompt", "test_cleanup"
                    )
                executor.shutdown(wait=True)
            self.assertFalse(delayed, "prompt write ignored cancellation until forced cleanup")

    def test_windows_prompt_fallback_aborts_and_joins_writer_before_cancellation(self) -> None:
        class BlockingStream:
            def __init__(self) -> None:
                self.closed = False
                self.released = threading.Event()

            def fileno(self) -> int:
                return 123

            def write(self, payload: bytes) -> int:
                self.released.wait(timeout=2)
                return len(payload)

            def close(self) -> None:
                self.closed = True

        stream = BlockingStream()
        process = SimpleNamespace(stdin=stream, poll=lambda: None)
        token = _MutableToken("windows-prompt")
        token.cancelled = True
        request = ExternalAuthorProcessRequest(
            run_id="windows-prompt",
            attempt=1,
            command=["author"],
            cwd=Path("."),
            prompt="x" * 1024,
            timeout_s=5,
            stdout_path=Path("stdout.log"),
            stderr_path=Path("stderr.log"),
            cancellation_token=token,
        )
        abort_calls: list[bool] = []

        def abort() -> None:
            abort_calls.append(True)
            stream.released.set()

        def blocking_write(_descriptor: int, payload: bytes) -> int:
            stream.released.wait(timeout=2)
            return len(payload)

        with patch.object(author_process_module.os, "name", "nt"), patch.object(
            author_process_module.os,
            "set_blocking",
            side_effect=OSError("unsupported"),
        ), patch.object(
            author_process_module.os,
            "write",
            side_effect=blocking_write,
        ):
            with self.assertRaises(RunCancelled):
                author_process_module._write_prompt_cancellable(
                    process,
                    request,
                    deadline=time.monotonic() + 5,
                    abort=abort,
                )

        self.assertEqual(abort_calls, [True])
        self.assertTrue(stream.closed)
        self.assertFalse(any(
            thread.name.startswith("external-author-prompt-windows-prompt")
            for thread in threading.enumerate()
        ))

    @unittest.skipUnless(os.name == "posix", "real fallback process probe requires POSIX")
    def test_set_blocking_failure_fallback_birth_verifies_and_terminates_author(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store, token, run_dir = self._control(root, "fallback-author")
            ready = run_dir / "ready"
            script = root / "author.py"
            script.write_text(
                "from pathlib import Path\nimport time\n"
                f"Path({str(ready)!r}).write_text('ready')\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            request = ExternalAuthorProcessRequest(
                run_id="fallback-author",
                attempt=1,
                command=[sys.executable, str(script)],
                cwd=root,
                prompt="x" * (8 * 1024 * 1024),
                timeout_s=30,
                stdout_path=run_dir / "stdout.log",
                stderr_path=run_dir / "stderr.log",
                run_dir=run_dir,
                cancellation_token=token,
            )
            executor = ThreadPoolExecutor(max_workers=1)
            with patch.object(
                author_process_module.os,
                "set_blocking",
                side_effect=OSError("unsupported"),
            ):
                future = executor.submit(run_external_author_process, request)
                try:
                    _wait_for(ready.is_file)
                    store.request_cancel("fallback-author")
                    with self.assertRaises(RunCancelled):
                        future.result(timeout=3)
                finally:
                    if not future.done():
                        terminate_registered_author_process(
                            "fallback-author", "test_cleanup"
                        )
                    executor.shutdown(wait=True)

            snapshot = ProcessLedger(run_dir).read()
            self.assertTrue(snapshot.processes)
            self.assertTrue(all(
                not process_is_alive(record.identity)
                for record in snapshot.processes
            ))
            self.assertFalse(any(
                thread.name.startswith("external-author-prompt-fallback-author")
                for thread in threading.enumerate()
            ))

    def test_set_blocking_failure_fallback_delivers_prompt_normally(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            script = root / "author.py"
            script.write_text(
                "import sys\nprint('OUT:' + sys.stdin.read())\n",
                encoding="utf-8",
            )
            with patch.object(
                author_process_module.os,
                "set_blocking",
                side_effect=OSError("unsupported"),
            ):
                result = run_external_author_process(
                    ExternalAuthorProcessRequest(
                        run_id="fallback-normal",
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
            self.assertFalse(any(
                thread.name.startswith("external-author-prompt-fallback-normal")
                for thread in threading.enumerate()
            ))

    def test_interruption_callback_is_run_cancellation_not_attempt_selection(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ready = root / "ready"
            script = root / "author.py"
            script.write_text(
                "from pathlib import Path\nimport time\n"
                "Path('ready').write_text('yes')\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )

            with self.assertRaises(RunCancelled) as caught:
                run_external_author_process(
                    ExternalAuthorProcessRequest(
                        run_id="callback-cancel",
                        attempt=1,
                        command=[sys.executable, str(script)],
                        cwd=root,
                        prompt="",
                        timeout_s=5,
                        stdout_path=root / "stdout.log",
                        stderr_path=root / "stderr.log",
                        run_dir=root / "run",
                        interruption_requested=lambda: ready.is_file(),
                    )
                )

            self.assertEqual(caught.exception.run_id, "callback-cancel")
            self.assertIn("run_cancelled", caught.exception.phase)

    @unittest.skipUnless(os.name == "posix", "POSIX signal permission behavior")
    def test_permission_denied_termination_reports_survivor_instead_of_crashing(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        identity = process_identity(process.pid)
        try:
            with patch("autodesign.process_supervision.os.kill", side_effect=PermissionError(1, "denied")), patch(
                "autodesign.process_supervision.os.killpg",
                side_effect=PermissionError(1, "denied"),
            ):
                report = terminate_process_identities(
                    [identity], root_pid=identity.pid, grace_s=0.01
                )
            self.assertIn(identity.pid, {item.pid for item in report.survivors})
            self.assertTrue(process_is_alive(identity))
            self.assertNotIn(identity.pid, {item.pid for item in report.terminated})
        finally:
            process.kill()
            process.wait(timeout=3)


class ExternalArtifactAuthorBarrierTests(unittest.TestCase):
    def _context(self, root: Path, token: _MutableToken) -> ToolContext:
        layers_dir = root / "layers"
        layers_dir.mkdir(parents=True, exist_ok=True)
        return ToolContext(
            settings=SimpleNamespace(
                designer_author_cmd="fake-author",
                designer_author_timeout_s=5,
                designer_author_harness="custom",
                designer_author_model="",
                designer_author_max_attempts=2,
                harness_api_key=None,
                repo_root=Path(__file__).resolve().parents[1],
            ),
            run_dir=root,
            layers_dir=layers_dir,
            run_id=token.run_id,
            cancellation_token=token,
        )

    @staticmethod
    def _write_minimal_valid_deck(path: Path) -> None:
        path.write_text(
            "<!doctype html><html><body>"
            '<main id="deck" data-slide-count="1" '
            'data-autodesign-artifact-root="deck">'
            '<section class="deck-slide" id="slide-1">'
            "<h1>Cancellation barrier</h1>"
            "</section></main></body></html>",
            encoding="utf-8",
        )

    @staticmethod
    def _write_promotion_journal(
        root: Path,
        artifact: str,
        *,
        phase: str,
        backup_name: str = "",
        trusted: bool = True,
    ) -> Path:
        journal_path = root / f".{artifact}-final-promotion.json"
        payload = {
            "version": 1,
            "phase": phase,
            "final_name": "final",
            "backup_name": backup_name,
            "staging_name": f".{artifact}-final-staging-crash",
        }
        if trusted:
            payload["transaction_owner"] = (
                "autodesign.atomic_artifact_promotion.v1"
            )
        journal_path.write_text(json.dumps(payload), encoding="utf-8")
        return journal_path

    @staticmethod
    def _recovery_cases():
        return (
            (
                "poster",
                poster_author_module._recover_poster_final_promotion,
            ),
            (
                "landing",
                landing_author_module._recover_interrupted_promotion,
            ),
            (
                "slides",
                slides_author_module._recover_interrupted_promotion,
            ),
            (
                "video",
                video_author_module._recover_video_final_promotion,
            ),
        )

    def test_untrusted_landing_and_slides_journals_without_backup_never_mutate_final(self) -> None:
        for artifact, recover in (
            ("landing", landing_author_module._recover_interrupted_promotion),
            ("slides", slides_author_module._recover_interrupted_promotion),
        ):
            for phase in (
                "prepared",
                "backup_created",
                "final_installed",
                "rollback_started",
                "rolled_back",
                "committed",
            ):
                with self.subTest(artifact=artifact, phase=phase), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    final_dir = root / "final"
                    final_dir.mkdir()
                    original = b"existing-final-must-survive"
                    (final_dir / "artifact.bin").write_bytes(original)
                    self._write_promotion_journal(
                        root,
                        artifact,
                        phase=phase,
                        trusted=False,
                    )

                    recover(final_dir)

                    self.assertEqual((final_dir / "artifact.bin").read_bytes(), original)
                    self.assertFalse(
                        (root / f".{artifact}-final-promotion.json").exists()
                    )

    def test_all_author_recovery_phases_preserve_only_committed_artifacts(self) -> None:
        for artifact, recover in self._recovery_cases():
            for phase in (
                "prepared",
                "backup_created",
                "final_installed",
                "rollback_started",
                "rolled_back",
                "committed",
            ):
                for replacement in (False, True):
                    with self.subTest(
                        artifact=artifact,
                        phase=phase,
                        replacement=replacement,
                    ), tempfile.TemporaryDirectory() as raw:
                        root = Path(raw)
                        final_dir = root / "final"
                        backup_name = (
                            f".{artifact}-final-backup-crash" if replacement else ""
                        )
                        backup_dir = root / backup_name if backup_name else None
                        staging_dir = root / f".{artifact}-final-staging-crash"
                        staging_dir.mkdir()
                        (staging_dir / "artifact.txt").write_text(
                            "staged", encoding="utf-8"
                        )

                        if phase == "prepared":
                            final_dir.mkdir()
                            (final_dir / "artifact.txt").write_text(
                                "old" if replacement else "new",
                                encoding="utf-8",
                            )
                        elif phase in {"backup_created"}:
                            if replacement:
                                assert backup_dir is not None
                                backup_dir.mkdir()
                                (backup_dir / "artifact.txt").write_text(
                                    "old", encoding="utf-8"
                                )
                        elif phase in {
                            "final_installed",
                            "rollback_started",
                            "committed",
                        }:
                            final_dir.mkdir()
                            (final_dir / "artifact.txt").write_text(
                                "new", encoding="utf-8"
                            )
                            if replacement:
                                assert backup_dir is not None
                                backup_dir.mkdir()
                                (backup_dir / "artifact.txt").write_text(
                                    "old", encoding="utf-8"
                                )
                        elif phase == "rolled_back" and replacement:
                            final_dir.mkdir()
                            (final_dir / "artifact.txt").write_text(
                                "old", encoding="utf-8"
                            )

                        self._write_promotion_journal(
                            root,
                            artifact,
                            phase=phase,
                            backup_name=backup_name,
                        )

                        recover(final_dir)
                        recover(final_dir)

                        if phase == "committed":
                            self.assertEqual(
                                (final_dir / "artifact.txt").read_text(encoding="utf-8"),
                                "new",
                            )
                        elif replacement:
                            self.assertEqual(
                                (final_dir / "artifact.txt").read_text(encoding="utf-8"),
                                "old",
                            )
                        else:
                            self.assertFalse(final_dir.exists())
                        self.assertFalse(staging_dir.exists())
                        journal_path = root / f".{artifact}-final-promotion.json"
                        if phase == "committed":
                            self.assertTrue(journal_path.is_file())
                            if backup_dir is not None:
                                self.assertTrue(backup_dir.is_dir())
                            atomic_promotion_module.reconcile_artifact_promotion(
                                final_dir,
                                artifact_name=artifact,
                                accept=True,
                            )
                        else:
                            if backup_dir is not None:
                                self.assertFalse(backup_dir.exists())
                            self.assertFalse(journal_path.exists())

    def test_all_authors_recover_before_starting_a_new_author_run(self) -> None:
        cases = (
            (
                "poster",
                poster_author_module.ExternalDesignerAuthor,
            ),
            (
                "landing",
                landing_author_module.ExternalLandingAuthor,
            ),
            (
                "slides",
                slides_author_module.ExternalSlidesAuthor,
            ),
            (
                "video",
                video_author_module.ExternalVideoAuthor,
            ),
        )
        for artifact, author_type in cases:
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                token = _MutableToken(f"{artifact}-startup-recovery")
                ctx = self._context(root, token)
                ctx.settings.designer_author_cmd = ""
                final_dir = root / "final"
                final_dir.mkdir()
                (final_dir / "artifact.txt").write_text(
                    "uncommitted", encoding="utf-8"
                )
                self._write_promotion_journal(
                    root,
                    artifact,
                    phase="final_installed",
                )

                author_type(ctx.settings, "ignored").run("brief", ctx)

                self.assertFalse(final_dir.exists())
                self.assertFalse(
                    (root / f".{artifact}-final-promotion.json").exists()
                )

    def test_startup_recovery_never_accepts_committed_publish_without_global_authority(self) -> None:
        author_types = {
            "poster": poster_author_module.ExternalDesignerAuthor,
            "landing": landing_author_module.ExternalLandingAuthor,
            "slides": slides_author_module.ExternalSlidesAuthor,
            "video": video_author_module.ExternalVideoAuthor,
        }
        for artifact, recover in self._recovery_cases():
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                final_dir = root / "final"
                final_dir.mkdir()
                (final_dir / "artifact.txt").write_text("new", encoding="utf-8")
                backup_dir = root / f".{artifact}-final-backup-pending"
                backup_dir.mkdir()
                (backup_dir / "artifact.txt").write_text("old", encoding="utf-8")
                journal_path = self._write_promotion_journal(
                    root,
                    artifact,
                    phase="committed",
                    backup_name=backup_dir.name,
                )

                recover(final_dir)
                recover(final_dir)
                token = _MutableToken(f"{artifact}-committed-startup")
                ctx = self._context(root, token)
                ctx.settings.designer_author_cmd = ""
                author_types[artifact](ctx.settings, "ignored").run("brief", ctx)

                self.assertEqual(
                    (final_dir / "artifact.txt").read_text(encoding="utf-8"),
                    "new",
                )
                self.assertEqual(
                    (backup_dir / "artifact.txt").read_text(encoding="utf-8"),
                    "old",
                )
                self.assertTrue(journal_path.is_file())

    def test_artifact_promotion_rejects_symlinked_run_and_final_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            foreign = root / "foreign"
            foreign.mkdir()
            alias = root / "run-alias"
            alias.symlink_to(foreign, target_is_directory=True)
            final_dir = alias / "final"
            final_dir.mkdir()
            (final_dir / "sentinel.txt").write_text("foreign-old", encoding="utf-8")
            staging = alias / ".poster-final-staging-test"
            staging.mkdir()
            (staging / "sentinel.txt").write_text("foreign-new", encoding="utf-8")

            with self.assertRaises(ValueError):
                atomic_promotion_module.publish_artifact_directory(
                    staging,
                    final_dir,
                    artifact_name="poster",
                    post_publish=lambda: None,
                )

            self.assertEqual(
                (foreign / "final" / "sentinel.txt").read_text(encoding="utf-8"),
                "foreign-old",
            )
            self.assertFalse((foreign / ".poster-final-promotion.json").exists())

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            foreign = root / "foreign"
            foreign.mkdir()
            (foreign / "sentinel.txt").write_text("foreign-old", encoding="utf-8")
            final_dir = root / "final"
            final_dir.symlink_to(foreign, target_is_directory=True)
            staging = root / ".poster-final-staging-test"
            staging.mkdir()
            (staging / "sentinel.txt").write_text("new", encoding="utf-8")

            with self.assertRaises(ValueError):
                atomic_promotion_module.publish_artifact_directory(
                    staging,
                    final_dir,
                    artifact_name="poster",
                    post_publish=lambda: None,
                )

            self.assertTrue(final_dir.is_symlink())
            self.assertEqual(
                (foreign / "sentinel.txt").read_text(encoding="utf-8"),
                "foreign-old",
            )
            self.assertFalse((root / ".poster-final-promotion.json").exists())

    def test_committed_promotion_remains_reversible_until_supervisor_reconciles(self) -> None:
        for replacement in (False, True):
            for accept in (False, True):
                with self.subTest(
                    replacement=replacement,
                    accept=accept,
                ), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    final_dir = root / "final"
                    if replacement:
                        final_dir.mkdir()
                        (final_dir / "artifact.txt").write_text("old", encoding="utf-8")
                    staging = root / ".poster-final-staging-test"
                    staging.mkdir()
                    (staging / "artifact.txt").write_text("new", encoding="utf-8")

                    atomic_promotion_module.publish_artifact_directory(
                        staging,
                        final_dir,
                        artifact_name="poster",
                        post_publish=lambda: None,
                    )

                    journal = root / ".poster-final-promotion.json"
                    self.assertTrue(journal.is_file())
                    atomic_promotion_module.reconcile_artifact_promotion(
                        final_dir,
                        artifact_name="poster",
                        accept=accept,
                    )
                    atomic_promotion_module.reconcile_artifact_promotion(
                        final_dir,
                        artifact_name="poster",
                        accept=accept,
                    )

                    if accept:
                        self.assertEqual(
                            (final_dir / "artifact.txt").read_text(encoding="utf-8"),
                            "new",
                        )
                    elif replacement:
                        self.assertEqual(
                            (final_dir / "artifact.txt").read_text(encoding="utf-8"),
                            "old",
                        )
                    else:
                        self.assertFalse(final_dir.exists())
                    self.assertFalse(journal.exists())

    def test_all_adapter_publish_wrappers_retain_rollback_until_reconciliation(self) -> None:
        cases = (
            (
                "poster",
                lambda staging, final: poster_author_module._publish_poster_final(
                    staging,
                    final,
                    lambda _phase: None,
                ),
            ),
            (
                "landing",
                lambda staging, final: landing_author_module._atomic_replace_directory(
                    staging,
                    final,
                    post_publish=lambda: None,
                ),
            ),
            (
                "slides",
                lambda staging, final: slides_author_module._atomic_replace_directory(
                    staging,
                    final,
                    post_publish=lambda: None,
                ),
            ),
            (
                "video",
                lambda staging, final: video_author_module._replace_video_candidate_final(
                    staging,
                    final,
                    post_publish=lambda: None,
                ),
            ),
        )
        for artifact, publish in cases:
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                final_dir = root / "final"
                final_dir.mkdir()
                (final_dir / "artifact.txt").write_text("old", encoding="utf-8")
                staging_dir = root / f".{artifact}-final-staging-test"
                staging_dir.mkdir()
                (staging_dir / "artifact.txt").write_text("new", encoding="utf-8")

                publish(staging_dir, final_dir)

                journal_path = root / f".{artifact}-final-promotion.json"
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                self.assertEqual(journal["phase"], "committed")
                backup_dir = root / journal["backup_name"]
                self.assertTrue(backup_dir.is_dir())
                self.assertEqual(
                    (backup_dir / "artifact.txt").read_text(encoding="utf-8"),
                    "old",
                )
                self.assertEqual(
                    (final_dir / "artifact.txt").read_text(encoding="utf-8"),
                    "new",
                )
                second_staging = root / f".{artifact}-final-staging-second"
                second_staging.mkdir()
                (second_staging / "artifact.txt").write_text(
                    "newer", encoding="utf-8"
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "awaiting global run reconciliation",
                ):
                    publish(second_staging, final_dir)
                self.assertEqual(
                    (backup_dir / "artifact.txt").read_text(encoding="utf-8"),
                    "old",
                )

                atomic_promotion_module.reconcile_artifact_promotion(
                    final_dir,
                    artifact_name=artifact,
                    accept=False,
                )
                self.assertEqual(
                    (final_dir / "artifact.txt").read_text(encoding="utf-8"),
                    "old",
                )

    def test_landing_commit_can_be_rejected_after_adapter_returns(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            token = _MutableToken("landing-global-reconcile")
            ctx = self._context(root, token)
            attempt = root / "landing-attempt"
            attempt.mkdir()
            (attempt / "index.html").write_text(
                "<html><body>new-final</body></html>", encoding="utf-8"
            )
            (attempt / "designer_author_done.json").write_text(
                "{}", encoding="utf-8"
            )
            final_dir = root / "final"
            final_dir.mkdir()
            (final_dir / "sentinel.txt").write_text("old-final", encoding="utf-8")
            author = landing_author_module.ExternalLandingAuthor(
                ctx.settings, "ignored"
            )

            def render(_source: Path, output: Path, **_kwargs):
                output.write_bytes(b"preview")
                return SimpleNamespace(backend="test", warnings=[])

            with patch.object(
                landing_author_module,
                "screenshot_html",
                side_effect=render,
            ):
                author._promote(ctx, attempt_dir=attempt, diagnostics={})

            self.assertTrue(
                (root / ".landing-final-promotion.json").is_file()
            )
            atomic_promotion_module.reconcile_artifact_promotion(
                final_dir,
                artifact_name="landing",
                accept=False,
            )

            self.assertEqual(
                (final_dir / "sentinel.txt").read_text(encoding="utf-8"),
                "old-final",
            )
            self.assertEqual(
                sorted(path.name for path in final_dir.iterdir()),
                ["sentinel.txt"],
            )

    def test_reconcile_reject_preserves_old_final_before_first_replace(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            final_dir = root / "final"
            final_dir.mkdir()
            (final_dir / "artifact.txt").write_text("old", encoding="utf-8")
            staging_dir = root / ".poster-final-staging-crash"
            staging_dir.mkdir()
            (staging_dir / "artifact.txt").write_text("new", encoding="utf-8")
            self._write_promotion_journal(
                root,
                "poster",
                phase="prepared",
                backup_name=".poster-final-backup-not-created",
            )

            atomic_promotion_module.reconcile_artifact_promotion(
                final_dir,
                artifact_name="poster",
                accept=False,
            )

            self.assertEqual(
                (final_dir / "artifact.txt").read_text(encoding="utf-8"),
                "old",
            )
            self.assertFalse(staging_dir.exists())

    def test_reconcile_accept_missing_final_keeps_recovery_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            final_dir = root / "final"
            backup_dir = root / ".poster-final-backup-crash"
            backup_dir.mkdir()
            (backup_dir / "artifact.txt").write_text("old", encoding="utf-8")
            journal_path = self._write_promotion_journal(
                root,
                "poster",
                phase="committed",
                backup_name=backup_dir.name,
            )

            with self.assertRaisesRegex(ValueError, "committed final is missing"):
                atomic_promotion_module.reconcile_artifact_promotion(
                    final_dir,
                    artifact_name="poster",
                    accept=True,
                )

            self.assertTrue(journal_path.is_file())
            self.assertEqual(
                (backup_dir / "artifact.txt").read_text(encoding="utf-8"),
                "old",
            )

    def test_recovery_missing_required_backup_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            final_dir = root / "final"
            final_dir.mkdir()
            (final_dir / "artifact.txt").write_text("new", encoding="utf-8")
            journal_path = self._write_promotion_journal(
                root,
                "landing",
                phase="final_installed",
                backup_name=".landing-final-backup-missing",
            )

            with self.assertRaisesRegex(ValueError, "required backup is missing"):
                landing_author_module._recover_interrupted_promotion(final_dir)

            self.assertTrue(journal_path.is_file())
            self.assertEqual(
                (final_dir / "artifact.txt").read_text(encoding="utf-8"),
                "new",
            )

    def test_reject_reconciliation_crash_matrix_converges_without_losing_previous_final(self) -> None:
        class InjectedPromotionCrash(RuntimeError):
            pass

        fault_points = (
            "after_rollback_started",
            "before_restore",
            "after_restore",
            "after_rolled_back",
            "after_staging_cleanup",
            "after_backup_cleanup",
            "after_journal_cleanup",
        )
        for replacement in (False, True):
            for fault_point in fault_points:
                with self.subTest(
                    replacement=replacement,
                    fault_point=fault_point,
                ), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    final_dir = root / "final"
                    old_bytes = b"old-final\x00byte-for-byte"
                    if replacement:
                        final_dir.mkdir()
                        (final_dir / "artifact.bin").write_bytes(old_bytes)
                    staging_dir = root / ".poster-final-staging-seed"
                    staging_dir.mkdir()
                    (staging_dir / "artifact.bin").write_bytes(b"new-final")
                    atomic_promotion_module.publish_artifact_directory(
                        staging_dir,
                        final_dir,
                        artifact_name="poster",
                        post_publish=lambda: None,
                    )
                    journal_path = root / ".poster-final-promotion.json"
                    journal = json.loads(journal_path.read_text(encoding="utf-8"))
                    backup_dir = (
                        root / journal["backup_name"]
                        if journal["backup_name"]
                        else None
                    )
                    staged_residue = root / journal["staging_name"]
                    staged_residue.mkdir()
                    (staged_residue / "residue.txt").write_text(
                        "residue", encoding="utf-8"
                    )

                    original_durable = atomic_promotion_module.durable_replace_json
                    original_replace = atomic_promotion_module.os.replace
                    original_remove = atomic_promotion_module._remove_path
                    original_unlink = Path.unlink
                    fired = False

                    def durable(path: Path, payload: dict) -> None:
                        nonlocal fired
                        original_durable(path, payload)
                        if (
                            not fired
                            and fault_point == "after_rollback_started"
                            and payload.get("phase") == "rollback_started"
                        ):
                            fired = True
                            raise InjectedPromotionCrash(fault_point)
                        if (
                            not fired
                            and fault_point == "after_rolled_back"
                            and payload.get("phase") == "rolled_back"
                        ):
                            fired = True
                            raise InjectedPromotionCrash(fault_point)

                    def replace(src: Path | str, dst: Path | str) -> None:
                        nonlocal fired
                        src_path = Path(src)
                        dst_path = Path(dst)
                        is_restore = backup_dir is not None and (
                            src_path == backup_dir and dst_path == final_dir
                        )
                        if not fired and fault_point == "before_restore" and is_restore:
                            fired = True
                            raise InjectedPromotionCrash(fault_point)
                        original_replace(src, dst)
                        if not fired and fault_point == "after_restore" and is_restore:
                            fired = True
                            raise InjectedPromotionCrash(fault_point)

                    def remove(path: Path | None) -> None:
                        nonlocal fired
                        is_first_restore = not replacement and path == final_dir
                        if (
                            not fired
                            and fault_point == "before_restore"
                            and is_first_restore
                        ):
                            fired = True
                            raise InjectedPromotionCrash(fault_point)
                        original_remove(path)
                        if (
                            not fired
                            and fault_point == "after_restore"
                            and is_first_restore
                        ):
                            fired = True
                            raise InjectedPromotionCrash(fault_point)
                        if (
                            not fired
                            and fault_point == "after_staging_cleanup"
                            and path == staged_residue
                        ):
                            fired = True
                            raise InjectedPromotionCrash(fault_point)
                        if (
                            not fired
                            and fault_point == "after_backup_cleanup"
                            and path == backup_dir
                        ):
                            fired = True
                            raise InjectedPromotionCrash(fault_point)

                    def unlink(path: Path, *args, **kwargs) -> None:
                        nonlocal fired
                        original_unlink(path, *args, **kwargs)
                        if (
                            not fired
                            and fault_point == "after_journal_cleanup"
                            and path == journal_path
                        ):
                            fired = True
                            raise InjectedPromotionCrash(fault_point)

                    with patch.object(
                        atomic_promotion_module,
                        "durable_replace_json",
                        side_effect=durable,
                    ), patch.object(
                        atomic_promotion_module.os,
                        "replace",
                        side_effect=replace,
                    ), patch.object(
                        atomic_promotion_module,
                        "_remove_path",
                        side_effect=remove,
                    ), patch.object(Path, "unlink", new=unlink):
                        with self.assertRaises(InjectedPromotionCrash):
                            atomic_promotion_module.reconcile_artifact_promotion(
                                final_dir,
                                artifact_name="poster",
                                accept=False,
                            )

                    self.assertTrue(fired)
                    crash_journal = (
                        json.loads(journal_path.read_text(encoding="utf-8"))
                        if journal_path.is_file()
                        else None
                    )
                    if fault_point in {
                        "after_rollback_started",
                        "before_restore",
                        "after_restore",
                    }:
                        self.assertEqual(crash_journal["phase"], "rollback_started")
                    elif fault_point != "after_journal_cleanup":
                        self.assertEqual(crash_journal["phase"], "rolled_back")

                    atomic_promotion_module.recover_artifact_promotion(
                        final_dir,
                        artifact_name="poster",
                    )
                    atomic_promotion_module.reconcile_artifact_promotion(
                        final_dir,
                        artifact_name="poster",
                        accept=False,
                    )
                    atomic_promotion_module.recover_artifact_promotion(
                        final_dir,
                        artifact_name="poster",
                    )

                    if replacement:
                        self.assertEqual(
                            (final_dir / "artifact.bin").read_bytes(),
                            old_bytes,
                        )
                        self.assertEqual(
                            sorted(path.name for path in final_dir.iterdir()),
                            ["artifact.bin"],
                        )
                    else:
                        self.assertFalse(final_dir.exists())
                    self.assertEqual(
                        list(root.glob(".poster-final-*")),
                        [],
                    )

    def test_all_author_process_requests_receive_exact_token_and_run_directory(self) -> None:
        """Dropping either request field makes cancellation non-authoritative."""
        fake_result = ExternalAuthorProcessResult(
            status="error",
            reason="process_exit",
            returncode=1,
            timed_out=False,
            elapsed_s=0.01,
            stdout="",
            stderr="",
            process_group_id=None,
        )
        cases: list[tuple[str, object, object]] = []
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            token = _MutableToken("request-propagation")
            ctx = self._context(root, token)
            settings = ctx.settings

            poster = poster_author_module.ExternalDesignerAuthor(settings, "ignored")
            poster_attempt = root / "poster-attempt"
            poster_attempt.mkdir()
            with patch.object(
                poster_author_module, "run_external_author_process", return_value=fake_result
            ) as run_process, patch.object(poster_author_module, "_write_process_log"):
                poster._invoke_author_command(
                    sys.executable,
                    prompt="prompt",
                    attempt_dir=poster_attempt,
                    timeout_s=5,
                    poster_stable_s=0.01,
                    previous_poster_sha256="",
                    run_id=ctx.run_id,
                    attempt=1,
                    ctx=ctx,
                )
                cases.append(("poster", run_process.call_args.args[0], ctx))

            slides = slides_author_module.ExternalSlidesAuthor(settings, "ignored")
            slides_attempt = root / "slides-attempt"
            slides_attempt.mkdir()
            with patch.object(
                slides_author_module, "run_external_author_process", return_value=fake_result
            ) as run_process, patch.object(slides_author_module, "_write_process_log"):
                slides._invoke(
                    sys.executable,
                    prompt="prompt",
                    attempt_dir=slides_attempt,
                    run_id=ctx.run_id,
                    attempt=1,
                    ctx=ctx,
                )
                cases.append(("slides", run_process.call_args.args[0], ctx))

            landing = landing_author_module.ExternalLandingAuthor(settings, "ignored")
            landing_attempt = root / "landing-attempt"
            landing_attempt.mkdir()
            with patch.object(
                landing_author_module, "run_external_author_process", return_value=fake_result
            ) as run_process:
                landing._invoke_author_command(
                    sys.executable,
                    prompt="prompt",
                    attempt_dir=landing_attempt,
                    timeout_s=5,
                    run_id=ctx.run_id,
                    attempt=1,
                    ctx=ctx,
                )
                cases.append(("landing", run_process.call_args.args[0], ctx))

            video_attempt = root / "video-attempt"
            video_attempt.mkdir()
            with patch.object(
                video_author_module, "run_external_author_process", return_value=fake_result
            ) as run_process:
                video_author_module._invoke_author_command(
                    sys.executable,
                    prompt="prompt",
                    attempt_dir=video_attempt,
                    timeout_s=5,
                    settings=settings,
                    run_id=ctx.run_id,
                    attempt=1,
                    ctx=ctx,
                )
                cases.append(("video", run_process.call_args.args[0], ctx))

        self.assertEqual([name for name, _, _ in cases], ["poster", "slides", "landing", "video"])
        for name, request, case_ctx in cases:
            with self.subTest(author=name):
                self.assertIs(request.cancellation_token, case_ctx.cancellation_token)
                self.assertEqual(request.run_dir, case_ctx.run_dir)
                self.assertIs(request.interruption_requested.__self__, case_ctx)
                self.assertIsNotNone(request.selection_requested)
                self.assertIsNone(request.selection_requested())

    def test_slides_cancellation_after_raw_cleanup_prevents_process_log_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            token = _PhaseCancellationToken(
                "external_slides.process.after_raw_log_cleanup",
                run_id="slides-log-barrier",
                raise_on_trigger=False,
            )
            ctx = self._context(root, token)
            attempt = root / "slides-attempt"
            attempt.mkdir()
            author = slides_author_module.ExternalSlidesAuthor(ctx.settings, "ignored")
            result = ExternalAuthorProcessResult(
                "error", "process_exit", 1, False, 0.01, "stdout", "stderr", None
            )
            with patch.object(
                slides_author_module, "run_external_author_process", return_value=result
            ), patch.object(slides_author_module, "_write_process_log") as write_log:
                with self.assertRaises(RunCancelled):
                    author._invoke(
                        sys.executable,
                        prompt="prompt",
                        attempt_dir=attempt,
                        ctx=ctx,
                    )
            write_log.assert_not_called()

    def test_landing_cancellation_after_stdout_prevents_stderr_log_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            token = _MutableToken("landing-log-barrier")
            ctx = self._context(root, token)
            attempt = root / "landing-attempt"
            attempt.mkdir()
            stdout_path = attempt / "designer_author_stdout.log"
            stderr_path = attempt / "designer_author_stderr.log"
            author = landing_author_module.ExternalLandingAuthor(ctx.settings, "ignored")
            result = ExternalAuthorProcessResult(
                "error", "process_exit", 1, False, 0.01, "stdout", "stderr", None
            )
            original_write_text = Path.write_text

            def write_text(path: Path, data: str, *args, **kwargs):
                written = original_write_text(path, data, *args, **kwargs)
                if path == stdout_path:
                    token.cancelled = True
                return written

            with patch.object(
                landing_author_module, "run_external_author_process", return_value=result
            ), patch.object(Path, "write_text", new=write_text):
                with self.assertRaises(RunCancelled):
                    author._invoke_author_command(
                        sys.executable,
                        prompt="prompt",
                        attempt_dir=attempt,
                        timeout_s=5,
                        ctx=ctx,
                    )
            self.assertTrue(stdout_path.is_file())
            self.assertFalse(stderr_path.exists())

    def test_landing_cancellation_during_preview_never_promotes_final(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            token = _MutableToken("landing-promotion-barrier")
            ctx = self._context(root, token)
            attempt = root / "landing-attempt"
            attempt.mkdir()
            (attempt / "index.html").write_text("<html></html>", encoding="utf-8")
            (attempt / "designer_author_done.json").write_text("{}", encoding="utf-8")
            author = landing_author_module.ExternalLandingAuthor(ctx.settings, "ignored")

            def render(_source: Path, output: Path, **_kwargs):
                output.write_bytes(b"preview")
                token.cancelled = True
                return SimpleNamespace(backend="test", warnings=[])

            with patch.object(landing_author_module, "screenshot_html", side_effect=render):
                with self.assertRaises(RunCancelled):
                    author._promote(ctx, attempt_dir=attempt, diagnostics={})

            self.assertFalse((root / "final").exists())
            self.assertFalse(ctx.state.get("finalized"))

    def test_landing_cancellation_after_atomic_publish_rolls_back_final(self) -> None:
        for has_previous_final in (False, True):
            with self.subTest(has_previous_final=has_previous_final):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    token = _PhaseCancellationToken(
                        "external_landing.promotion.after_publish",
                        run_id="landing-publish-barrier",
                    )
                    ctx = self._context(root, token)  # type: ignore[arg-type]
                    attempt = root / "landing-attempt"
                    attempt.mkdir()
                    (attempt / "index.html").write_text(
                        "<html><body>new-final</body></html>",
                        encoding="utf-8",
                    )
                    (attempt / "designer_author_done.json").write_text(
                        "{}", encoding="utf-8"
                    )
                    final_dir = root / "final"
                    if has_previous_final:
                        final_dir.mkdir()
                        (final_dir / "sentinel.txt").write_text(
                            "old-final", encoding="utf-8"
                        )
                    author = landing_author_module.ExternalLandingAuthor(
                        ctx.settings, "ignored"
                    )

                    def render(_source: Path, output: Path, **_kwargs):
                        output.write_bytes(b"preview")
                        return SimpleNamespace(backend="test", warnings=[])

                    with patch.object(
                        landing_author_module,
                        "screenshot_html",
                        side_effect=render,
                    ):
                        with self.assertRaises(RunCancelled):
                            author._promote(
                                ctx,
                                attempt_dir=attempt,
                                diagnostics={},
                            )

                    if has_previous_final:
                        self.assertEqual(
                            (final_dir / "sentinel.txt").read_text(encoding="utf-8"),
                            "old-final",
                        )
                        self.assertEqual(
                            sorted(path.name for path in final_dir.iterdir()),
                            ["sentinel.txt"],
                        )
                    else:
                        self.assertFalse(final_dir.exists())
                    self.assertFalse(ctx.state.get("finalized"))

    def test_slides_cancellation_during_preview_never_promotes_final(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            token = _MutableToken("slides-promotion-barrier")
            ctx = self._context(root, token)
            attempt = root / "slides-attempt"
            attempt.mkdir()
            for name in (
                "designer_author_done.json",
                "slides_visual_plan.json",
                "slides_asset_catalog.json",
                "slides_validation.json",
            ):
                (attempt / name).write_text("{}", encoding="utf-8")
            self._write_minimal_valid_deck(attempt / "slides.html")
            author = slides_author_module.ExternalSlidesAuthor(ctx.settings, "ignored")

            def render(_source: Path, output_dir: Path, **_kwargs):
                output_dir.mkdir(parents=True, exist_ok=True)
                slide = output_dir / "slide-1.png"
                slide.write_bytes(b"slide")
                token.cancelled = True
                return SimpleNamespace(paths=[slide], backend="test", warnings=[])

            def build_preview(_paths: list[Path], output: Path) -> None:
                output.write_bytes(b"preview")

            with patch.object(
                slides_author_module,
                "screenshot_deck_slides",
                side_effect=render,
            ), patch.object(
                slides_author_module,
                "build_deck_preview_grid",
                side_effect=build_preview,
            ):
                with self.assertRaises(RunCancelled):
                    author._promote(
                        ctx,
                        attempt_dir=attempt,
                        expected_slide_count=1,
                        validation={},
                    )

            self.assertFalse((root / "final").exists())
            self.assertFalse(ctx.state.get("finalized"))

    def test_slides_cancellation_after_atomic_publish_rolls_back_final(self) -> None:
        for has_previous_final in (False, True):
            with self.subTest(has_previous_final=has_previous_final):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    token = _PhaseCancellationToken(
                        "external_slides.promotion.after_publish",
                        run_id="slides-publish-barrier",
                    )
                    ctx = self._context(root, token)  # type: ignore[arg-type]
                    attempt = root / "slides-attempt"
                    attempt.mkdir()
                    for name in (
                        "designer_author_done.json",
                        "slides_visual_plan.json",
                        "slides_asset_catalog.json",
                        "slides_validation.json",
                    ):
                        (attempt / name).write_text("{}", encoding="utf-8")
                    self._write_minimal_valid_deck(attempt / "slides.html")
                    final_dir = root / "final"
                    if has_previous_final:
                        final_dir.mkdir()
                        (final_dir / "sentinel.txt").write_text(
                            "old-final", encoding="utf-8"
                        )
                    author = slides_author_module.ExternalSlidesAuthor(
                        ctx.settings, "ignored"
                    )

                    def render(_source: Path, output_dir: Path, **_kwargs):
                        output_dir.mkdir(parents=True, exist_ok=True)
                        slide = output_dir / "slide-1.png"
                        slide.write_bytes(b"slide")
                        return SimpleNamespace(
                            paths=[slide], backend="test", warnings=[]
                        )

                    def build_preview(_paths: list[Path], output: Path) -> None:
                        output.write_bytes(b"preview")

                    with patch.object(
                        slides_author_module,
                        "screenshot_deck_slides",
                        side_effect=render,
                    ), patch.object(
                        slides_author_module,
                        "build_deck_preview_grid",
                        side_effect=build_preview,
                    ):
                        with self.assertRaises(RunCancelled):
                            author._promote(
                                ctx,
                                attempt_dir=attempt,
                                expected_slide_count=1,
                                validation={},
                            )

                    if has_previous_final:
                        self.assertEqual(
                            (final_dir / "sentinel.txt").read_text(encoding="utf-8"),
                            "old-final",
                        )
                        self.assertEqual(
                            sorted(path.name for path in final_dir.iterdir()),
                            ["sentinel.txt"],
                        )
                    else:
                        self.assertFalse(final_dir.exists())
                    self.assertFalse(ctx.state.get("finalized"))

    def test_landing_and_slides_recovery_resolves_transaction_phases(self) -> None:
        cases = (
            (landing_author_module, "landing"),
            (slides_author_module, "slides"),
        )
        for module, artifact in cases:
            with self.subTest(artifact=artifact, phase="prepared-first-publish"):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    final_dir = root / "final"
                    final_dir.mkdir()
                    (final_dir / "artifact.txt").write_text(
                        "new-uncommitted", encoding="utf-8"
                    )
                    module.atomic_write_json(
                        root / f".{artifact}-final-promotion.json",
                        {
                            "version": 1,
                            "transaction_owner": (
                                "autodesign.atomic_artifact_promotion.v1"
                            ),
                            "phase": "prepared",
                            "final_name": "final",
                            "backup_name": "",
                            "staging_name": f".{artifact}-final-staging-moved",
                        },
                    )

                    module._recover_interrupted_promotion(final_dir)

                    self.assertFalse(final_dir.exists())

            with self.subTest(artifact=artifact, phase="final-installed"):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    final_dir = root / "final"
                    final_dir.mkdir()
                    (final_dir / "artifact.txt").write_text(
                        "new-uncommitted", encoding="utf-8"
                    )
                    backup_dir = root / f".{artifact}-final-backup-crash"
                    backup_dir.mkdir()
                    (backup_dir / "artifact.txt").write_text(
                        "old-final", encoding="utf-8"
                    )
                    module.atomic_write_json(
                        root / f".{artifact}-final-promotion.json",
                        {
                            "version": 1,
                            "transaction_owner": (
                                "autodesign.atomic_artifact_promotion.v1"
                            ),
                            "phase": "final_installed",
                            "final_name": "final",
                            "backup_name": backup_dir.name,
                            "staging_name": f".{artifact}-final-staging-moved",
                        },
                    )

                    module._recover_interrupted_promotion(final_dir)

                    self.assertEqual(
                        (final_dir / "artifact.txt").read_text(encoding="utf-8"),
                        "old-final",
                    )
                    self.assertFalse(backup_dir.exists())

            with self.subTest(artifact=artifact, phase="committed"):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    final_dir = root / "final"
                    final_dir.mkdir()
                    (final_dir / "artifact.txt").write_text(
                        "new-final", encoding="utf-8"
                    )
                    backup_dir = root / f".{artifact}-final-backup-cleanup"
                    backup_dir.mkdir()
                    (backup_dir / "artifact.txt").write_text(
                        "old-final", encoding="utf-8"
                    )
                    module.atomic_write_json(
                        root / f".{artifact}-final-promotion.json",
                        {
                            "version": 1,
                            "transaction_owner": (
                                "autodesign.atomic_artifact_promotion.v1"
                            ),
                            "phase": "committed",
                            "final_name": "final",
                            "backup_name": backup_dir.name,
                            "staging_name": f".{artifact}-final-staging-moved",
                        },
                    )

                    module._recover_interrupted_promotion(final_dir)

                    self.assertEqual(
                        (final_dir / "artifact.txt").read_text(encoding="utf-8"),
                        "new-final",
                    )
                    self.assertTrue(backup_dir.exists())
                    self.assertTrue(
                        (root / f".{artifact}-final-promotion.json").is_file()
                    )
                    atomic_promotion_module.reconcile_artifact_promotion(
                        final_dir,
                        artifact_name=artifact,
                        accept=True,
                    )
                    self.assertFalse(backup_dir.exists())

    def test_poster_cancellation_during_preview_never_promotes_final(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            token = _MutableToken("poster-promotion-barrier")
            ctx = self._context(root, token)
            attempt = root / "poster-attempt"
            attempt.mkdir()
            poster_path = attempt / "poster.html"
            poster_path.write_text(
                '<main class="paper-poster" style="width:1600px;height:900px"></main>',
                encoding="utf-8",
            )
            author = poster_author_module.ExternalDesignerAuthor(ctx.settings, "ignored")

            def render(*, preview_path: Path, **_kwargs):
                preview_path.write_bytes(b"preview")
                token.cancelled = True
                return SimpleNamespace(
                    backend="test",
                    warnings=[],
                    scale=1.0,
                    width_px=1600,
                    height_px=900,
                )

            with patch.object(poster_author_module, "_render_direct_preview", side_effect=render):
                with self.assertRaises(RunCancelled):
                    author._promote_direct_final(
                        ctx,
                        attempt_index=1,
                        attempt_dir=attempt,
                        poster_path=poster_path,
                        poster_sha256="",
                    )

            self.assertFalse((root / "final").exists())
            self.assertFalse(ctx.state.get("finalized"))

    def test_poster_cancellation_during_staged_manifest_preserves_previous_final(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            token = _MutableToken("poster-manifest-barrier")
            ctx = self._context(root, token)
            attempt = root / "poster-attempt"
            attempt.mkdir()
            poster_path = attempt / "poster.html"
            poster_path.write_text(
                '<main class="paper-poster" style="width:1600px;height:900px">new</main>',
                encoding="utf-8",
            )
            final_dir = root / "final"
            final_dir.mkdir()
            old_html = final_dir / "poster.html"
            old_html.write_text("old-final", encoding="utf-8")
            author = poster_author_module.ExternalDesignerAuthor(ctx.settings, "ignored")
            original_atomic_write_json = poster_author_module.atomic_write_json

            def render(*, preview_path: Path, **_kwargs):
                preview_path.write_bytes(b"preview")
                return SimpleNamespace(
                    backend="test", warnings=[], scale=1.0, width_px=1600, height_px=900,
                )

            def write_manifest(path: Path, data):
                original_atomic_write_json(path, data)
                if path.parent.name.startswith(".poster-final-staging-"):
                    token.cancelled = True

            with patch.object(
                poster_author_module, "_render_direct_preview", side_effect=render
            ), patch.object(
                poster_author_module, "atomic_write_json", side_effect=write_manifest
            ):
                with self.assertRaises(RunCancelled):
                    author._promote_direct_final(
                        ctx,
                        attempt_index=1,
                        attempt_dir=attempt,
                        poster_path=poster_path,
                        poster_sha256="",
                    )

            self.assertEqual(old_html.read_text(encoding="utf-8"), "old-final")
            self.assertEqual(sorted(path.name for path in final_dir.iterdir()), ["poster.html"])
            self.assertFalse(ctx.state.get("finalized"))

    def test_poster_cancellation_after_atomic_publish_restores_previous_final(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            token = _PhaseCancellationToken(
                "external_poster.promotion.after_final_publish",
                run_id="poster-publish-barrier",
            )
            ctx = self._context(root, token)  # type: ignore[arg-type]
            attempt = root / "poster-attempt"
            attempt.mkdir()
            poster_path = attempt / "poster.html"
            poster_path.write_text(
                '<main class="paper-poster" style="width:1600px;height:900px">new</main>',
                encoding="utf-8",
            )
            final_dir = root / "final"
            final_dir.mkdir()
            old_html = final_dir / "poster.html"
            old_html.write_text("old-final", encoding="utf-8")
            author = poster_author_module.ExternalDesignerAuthor(ctx.settings, "ignored")

            def render(*, preview_path: Path, **_kwargs):
                preview_path.write_bytes(b"preview")
                return SimpleNamespace(
                    backend="test", warnings=[], scale=1.0, width_px=1600, height_px=900,
                )

            with patch.object(poster_author_module, "_render_direct_preview", side_effect=render):
                with self.assertRaises(RunCancelled):
                    author._promote_direct_final(
                        ctx,
                        attempt_index=1,
                        attempt_dir=attempt,
                        poster_path=poster_path,
                        poster_sha256="",
                    )

            self.assertEqual(old_html.read_text(encoding="utf-8"), "old-final")
            self.assertEqual(sorted(path.name for path in final_dir.iterdir()), ["poster.html"])
            self.assertFalse(ctx.state.get("finalized"))

    def test_poster_fallbacks_use_atomic_promotion_and_cancel_without_publishing(self) -> None:
        for fallback_kind in ("best_candidate", "best_available_artifact"):
            for cancellation_phase in (
                "external_poster.fallback.after_preview",
                "external_poster.fallback.after_final_publish",
            ):
                for replacement in (False, True):
                    with self.subTest(
                        fallback_kind=fallback_kind,
                        cancellation_phase=cancellation_phase,
                        replacement=replacement,
                    ), tempfile.TemporaryDirectory() as raw:
                        root = Path(raw)
                        token = _PhaseCancellationToken(
                            cancellation_phase,
                            run_id="poster-fallback-cancel",
                        )
                        ctx = self._context(root, token)  # type: ignore[arg-type]
                        attempt_dir = root / "designer_author" / "attempt_01"
                        candidate_dir = attempt_dir / "candidate"
                        candidate_dir.mkdir(parents=True)
                        measure_html = candidate_dir / "poster.html"
                        measure_html.write_text(
                            '<main class="paper-poster">new fallback</main>',
                            encoding="utf-8",
                        )
                        final_dir = root / "final"
                        if replacement:
                            final_dir.mkdir()
                            (final_dir / "poster.html").write_text(
                                "old-final", encoding="utf-8"
                            )
                        candidate = {
                            "candidate_id": "candidate-1",
                            "candidate_relative_dir": "designer_author/attempt_01/candidate",
                            "candidate_score": 1.0,
                            "candidate_score_reasons": [],
                            "status": "near_miss",
                            "stage": "validation",
                            "payload": {},
                            "_candidate_dir_abs": str(candidate_dir),
                            "_measure_html_abs": str(measure_html),
                            "_preview_png_abs": str(candidate_dir / "missing-preview.png"),
                        }
                        author = poster_author_module.ExternalDesignerAuthor(
                            ctx.settings, "ignored"
                        )

                        def render(*, preview_path: Path, **_kwargs):
                            preview_path.write_bytes(b"preview")
                            return SimpleNamespace(
                                backend="test",
                                warnings=[],
                                scale=1.0,
                                width_px=3072,
                                height_px=1536,
                            )

                        with patch.object(
                            poster_author_module,
                            "ensure_poster_katex_document",
                            return_value={"detected": False},
                        ), patch.object(
                            poster_author_module,
                            "_poster_root_scroll_metrics",
                            return_value={"available": False},
                        ), patch.object(
                            poster_author_module,
                            "apply_poster_typesetting_patch",
                            return_value={"applied": False},
                        ), patch.object(
                            poster_author_module,
                            "_maybe_repair_collapsed_poster_header",
                            return_value=None,
                        ), patch.object(
                            poster_author_module,
                            "_render_direct_preview",
                            side_effect=render,
                        ):
                            with self.assertRaises(RunCancelled):
                                author._promote_html_first_candidate_fallback(
                                    ctx,
                                    attempt_index=1,
                                    attempt_dir=attempt_dir,
                                    candidate=candidate,
                                    acceptance={"accepted": True, "reason": "test"},
                                    rejected_candidates=[],
                                    source_reason="test",
                                    source_message="test",
                                    last_feedback=None,
                                    fallback_kind=fallback_kind,
                                )

                        if replacement:
                            self.assertEqual(
                                (final_dir / "poster.html").read_text(encoding="utf-8"),
                                "old-final",
                            )
                            self.assertEqual(
                                sorted(path.name for path in final_dir.iterdir()),
                                ["poster.html"],
                            )
                        else:
                            self.assertFalse(final_dir.exists())
                        self.assertFalse(ctx.state.get("finalized"))

    def test_poster_recovery_keeps_old_final_when_crash_precedes_first_replace(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            final_dir = root / "final"
            final_dir.mkdir()
            (final_dir / "poster.html").write_text("old-final", encoding="utf-8")
            staging_dir = root / ".poster-final-staging-crash"
            staging_dir.mkdir()
            (staging_dir / "poster.html").write_text("new-final", encoding="utf-8")
            poster_author_module.atomic_write_json(
                root / ".poster-final-promotion.json",
                {
                    "version": 1,
                    "phase": "prepared",
                    "final_name": "final",
                    "backup_name": ".poster-final-backup-crash",
                    "staging_name": staging_dir.name,
                },
            )

            poster_author_module._recover_poster_final_promotion(final_dir)

            self.assertEqual((final_dir / "poster.html").read_text(), "old-final")
            self.assertFalse(staging_dir.exists())
            self.assertFalse((root / ".poster-final-promotion.json").exists())

    def test_poster_recovery_removes_uncommitted_first_publish(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            final_dir = root / "final"
            final_dir.mkdir()
            (final_dir / "poster.html").write_text("new-uncommitted", encoding="utf-8")
            poster_author_module.atomic_write_json(
                root / ".poster-final-promotion.json",
                {
                    "version": 1,
                    "phase": "prepared",
                    "final_name": "final",
                    "backup_name": "",
                    "staging_name": ".poster-final-staging-moved",
                },
            )

            poster_author_module._recover_poster_final_promotion(final_dir)

            self.assertFalse(final_dir.exists())
            self.assertFalse((root / ".poster-final-promotion.json").exists())

    def test_poster_recovery_keeps_committed_final_after_backup_cleanup_crash(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            final_dir = root / "final"
            final_dir.mkdir()
            (final_dir / "poster.html").write_text("new-final", encoding="utf-8")
            poster_author_module.atomic_write_json(
                root / ".poster-final-promotion.json",
                {
                    "version": 1,
                    "phase": "committed",
                    "final_name": "final",
                    "backup_name": ".poster-final-backup-cleaned",
                    "staging_name": ".poster-final-staging-installed",
                },
            )

            poster_author_module._recover_poster_final_promotion(final_dir)

            self.assertEqual((final_dir / "poster.html").read_text(), "new-final")
            self.assertFalse((root / ".poster-final-promotion.json").exists())

    def test_video_cancellation_during_candidate_preview_never_promotes_final(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            token = _MutableToken("video-materialize-barrier")
            ctx = self._context(root, token)
            snapshot = root / "video_author" / "attempt_01"
            project = snapshot / "project"
            project.mkdir(parents=True)
            (project / "index.html").write_text("<html></html>", encoding="utf-8")
            candidate = SimpleNamespace(
                candidate_id="video-candidate",
                attempt=1,
                source_relative_path="video_author/attempt_01/project/index.html",
            )

            def render(_source: Path, output: Path, **_kwargs):
                output.write_bytes(b"preview")
                token.cancelled = True
                return SimpleNamespace(backend="test", warnings=[])

            with patch.object(
                video_author_module,
                "assert_promotion_allowed",
            ), patch.object(video_author_module, "screenshot_html", side_effect=render):
                with self.assertRaises(RunCancelled):
                    video_author_module.materialize_selected_attempt_for_editing(
                        ctx,
                        candidate,
                    )

            self.assertFalse((root / "final").exists())
            self.assertNotIn("video_author_result", ctx.state)

    def test_video_cancellation_after_atomic_publish_rolls_back_final(self) -> None:
        for has_previous_final in (False, True):
            with self.subTest(has_previous_final=has_previous_final):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    token = _PhaseCancellationToken(
                        "external_video.materialize_selected.after_publish",
                        run_id="video-publish-barrier",
                    )
                    ctx = self._context(root, token)  # type: ignore[arg-type]
                    snapshot = root / "video_author" / "attempt_01"
                    project = snapshot / "project"
                    project.mkdir(parents=True)
                    (project / "index.html").write_text(
                        "<html><body>new-final</body></html>",
                        encoding="utf-8",
                    )
                    final_dir = root / "final"
                    if has_previous_final:
                        final_dir.mkdir()
                        (final_dir / "sentinel.txt").write_text(
                            "old-final", encoding="utf-8"
                        )
                    candidate = SimpleNamespace(
                        candidate_id="video-candidate",
                        attempt=1,
                        source_relative_path=(
                            "video_author/attempt_01/project/index.html"
                        ),
                    )

                    def render(_source: Path, output: Path, **_kwargs):
                        output.write_bytes(b"preview")
                        return SimpleNamespace(backend="test", warnings=[])

                    with patch.object(
                        video_author_module,
                        "assert_promotion_allowed",
                    ), patch.object(
                        video_author_module,
                        "screenshot_html",
                        side_effect=render,
                    ):
                        with self.assertRaises(RunCancelled):
                            video_author_module.materialize_selected_attempt_for_editing(
                                ctx,
                                candidate,
                            )

                    if has_previous_final:
                        self.assertEqual(
                            (final_dir / "sentinel.txt").read_text(encoding="utf-8"),
                            "old-final",
                        )
                        self.assertEqual(
                            sorted(path.name for path in final_dir.iterdir()),
                            ["sentinel.txt"],
                        )
                    else:
                        self.assertFalse(final_dir.exists())
                    self.assertNotIn("video_author_result", ctx.state)

    def test_video_cancellation_after_selected_delivery_prevents_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            token = _MutableToken("video-selected-delivery-barrier")
            ctx = self._context(root, token)
            snapshot = root / "video_author" / "attempt_01"
            project = snapshot / "project"
            project.mkdir(parents=True)
            (project / "index.html").write_text("<html></html>", encoding="utf-8")
            (snapshot / "video_author_manifest.json").write_text("{}", encoding="utf-8")
            candidate = SimpleNamespace(
                candidate_id="video-candidate",
                attempt=1,
                source_relative_path="video_author/attempt_01/project/index.html",
            )

            def deliver(**_kwargs):
                token.cancelled = True
                return SimpleNamespace(status="ok", error_message="", payload={})

            with patch.object(
                video_author_module,
                "assert_promotion_allowed",
            ), patch.object(
                video_author_module,
                "transition_selection",
            ), patch.object(
                video_author_module,
                "deliver_authored_video_project",
                side_effect=deliver,
            ), patch.object(video_author_module, "invoke_designer_tool") as finalize:
                with self.assertRaises(RunCancelled):
                    video_author_module.promote_selected_attempt(ctx, candidate)

            finalize.assert_not_called()
            self.assertNotIn("video_author_result", ctx.state)

    def test_poster_cancellation_after_validation_stops_retry_and_feedback_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            token = _MutableToken("poster-barrier")
            ctx = self._context(root, token)
            author = poster_author_module.ExternalDesignerAuthor(ctx.settings, "ignored")
            invocations = 0

            def invoke(*_args, attempt_dir: Path, **_kwargs):
                nonlocal invocations
                invocations += 1
                (attempt_dir / "poster.html").write_text("<html></html>", encoding="utf-8")
                return poster_author_module._InvocationResult(
                    "ok", "process_exit", 0, False, 0.01, ""
                )

            def validate(*_args, **_kwargs):
                token.cancelled = True
                return {"status": "error", "issues": [{"id": "bad"}]}

            with patch.object(poster_author_module, "authoring_max_attempts_for", return_value=2), patch.object(
                author, "_ensure_ingested", return_value=True
            ), patch.object(author, "_stage_inputs", return_value=True), patch.object(
                author, "_build_prompt", return_value="prompt"
            ), patch.object(author, "_invoke_author_command", side_effect=invoke), patch.object(
                author, "_direct_final_validation_feedback", side_effect=validate
            ), patch.object(poster_author_module, "promote_pending_selection", return_value="none"):
                with self.assertRaises(RunCancelled):
                    author.run("poster", ctx)

            self.assertEqual(invocations, 1)
            attempt = root / "designer_author" / "attempt_01"
            self.assertFalse((attempt / "validation_feedback.json").exists())

    def test_slides_cancellation_after_validation_stops_retry_and_validation_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            token = _MutableToken("slides-barrier")
            ctx = self._context(root, token)
            ctx.state.update({"paper_visual_provenance": {}, "paper_memory": {"ok": True}})
            author = slides_author_module.ExternalSlidesAuthor(ctx.settings, "ignored")
            invocations = 0

            def invoke(*_args, attempt_dir: Path, **_kwargs):
                nonlocal invocations
                invocations += 1
                (attempt_dir / "slides.html").write_text("<html></html>", encoding="utf-8")
                return {"status": "ok", "reason": "process_exit", "returncode": 0}

            def validate(*_args, **_kwargs):
                token.cancelled = True
                return {"status": "error", "issues": [], "source_visual_ids": []}

            with patch.object(slides_author_module, "authoring_max_attempts_for", return_value=2), patch.object(
                author, "_ensure_ingested", return_value=True
            ), patch.object(author, "_stage_inputs"), patch.object(
                author, "_build_prompt", return_value="prompt"
            ), patch.object(author, "_invoke", side_effect=invoke), patch.object(
                slides_author_module, "_expected_slide_count", return_value=2
            ), patch.object(slides_author_module, "build_slides_asset_catalog", return_value={}), patch.object(
                slides_author_module, "build_slides_visual_plan", return_value={}
            ), patch.object(slides_author_module, "_trusted_slides_source_hashes", return_value={}), patch.object(
                slides_author_module, "_validate_slides", side_effect=validate
            ), patch.object(slides_author_module, "promote_pending_selection", return_value="none"):
                with self.assertRaises(RunCancelled):
                    author.run("slides", ctx)

            self.assertEqual(invocations, 1)
            self.assertFalse((root / "slides_author/attempt_01/slides_validation.json").exists())

    def test_landing_cancellation_after_validation_stops_retry_and_validation_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            token = _MutableToken("landing-barrier")
            ctx = self._context(root, token)
            ctx.state.update({"paper_visual_provenance": {}, "paper_memory": {"ok": True}})
            author = landing_author_module.ExternalLandingAuthor(ctx.settings, "ignored")
            invocations = 0

            def invoke(*_args, attempt_dir: Path, **_kwargs):
                nonlocal invocations
                invocations += 1
                (attempt_dir / "index.html").write_text("<html></html>", encoding="utf-8")
                return landing_author_module._InvocationResult("ok", "process_exit", 0, False, 0.01)

            def validate(*_args, **_kwargs):
                token.cancelled = True
                return {"accepted": False, "findings": [], "metrics": {}}

            with patch.object(landing_author_module, "authoring_max_attempts_for", return_value=2), patch.object(
                author, "_ensure_ingested", return_value=True
            ), patch.object(author, "_stage_inputs", return_value=True), patch.object(
                author, "_build_prompt", return_value="prompt"
            ), patch.object(author, "_invoke_author_command", side_effect=invoke), patch.object(
                landing_author_module, "build_landing_asset_catalog", return_value={"assets": []}
            ), patch.object(landing_author_module, "_trusted_landing_source_hashes", return_value={}), patch.object(
                landing_author_module, "_validate_landing_output", side_effect=validate
            ), patch.object(landing_author_module, "promote_pending_selection", return_value="none"):
                with self.assertRaises(RunCancelled):
                    author.run("landing", ctx)

            self.assertEqual(invocations, 1)
            self.assertFalse((root / "landing_author/attempt_01/landing_validation.json").exists())

    def test_video_cancellation_after_validation_stops_retry_and_error_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            token = _MutableToken("video-barrier")
            ctx = self._context(root, token)
            ctx.state.update({
                "paper_visual_provenance": {"assets": []},
                "paper_memory": {"ok": True},
            })
            author = video_author_module.ExternalVideoAuthor(ctx.settings, "ignored")
            invocations = 0

            def invoke(*_args, **_kwargs):
                nonlocal invocations
                invocations += 1
                return ""

            def validate(*_args, **_kwargs):
                token.cancelled = True
                return ["invalid"]

            with patch.object(video_author_module, "authoring_max_attempts_for", return_value=2), patch.object(
                author, "_ensure_ingested", return_value=True
            ), patch.object(video_author_module, "_load_context_json", side_effect=lambda _ctx, key: ctx.state.get(key, {})), patch.object(
                video_author_module, "build_video_visual_asset_catalog", return_value={"assets": []}
            ), patch.object(video_author_module, "build_video_visual_plan", return_value={"minimum_required_visual_count": 0}), patch.object(
                author, "_stage_inputs", return_value=([], [])
            ), patch.object(author, "_build_prompt", return_value="prompt"), patch.object(
                video_author_module, "_invoke_author_command", side_effect=invoke
            ), patch.object(video_author_module, "_read_json_object", return_value=({}, None)), patch.object(
                video_author_module, "validate_video_author_output", side_effect=validate
            ), patch.object(video_author_module, "promote_pending_selection", return_value="none"):
                with self.assertRaises(RunCancelled):
                    author.run("video", ctx)

            self.assertEqual(invocations, 1)
            self.assertFalse((root / "video_author/attempt_01/video_author_validation_errors.json").exists())

    def test_stale_identity_is_never_signalled_or_reported_terminated(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        current = process_identity(process.pid)
        stale = ProcessIdentity(
            pid=current.pid,
            birth_id=current.birth_id + "-stale",
            process_group_id=current.process_group_id,
            parent_pid=current.parent_pid,
        )
        try:
            report = terminate_process_identities([stale], root_pid=stale.pid, grace_s=0.01)
            self.assertTrue(process_is_alive(current))
            self.assertIn(stale, report.stale_identities)
            self.assertNotIn(stale, report.terminated)
        finally:
            process.kill()
            process.wait(timeout=3)


@unittest.skipUnless(os.name == "posix", "real supervisor process-tree check requires POSIX")
class ExternalAuthorSupervisorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_completion_cleanup_preserves_root_worker_result(self) -> None:
        """Including the root-worker nonce in author cleanup kills its own worker."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runs_dir = root / "out" / "runs"
            run_id = "supervised-author-completion"
            run_dir = runs_dir / run_id
            ready_path = run_dir / "author-ready"
            author_pids_path = run_dir / "author-pids.json"
            grandchild = root / "grandchild.py"
            grandchild.write_text(
                "import time\nwhile True:\n    time.sleep(1)\n",
                encoding="utf-8",
            )
            author = root / "author.py"
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
                        [sys.executable, {str(grandchild)!r}], start_new_session=True
                    )
                    Path({str(author_pids_path)!r}).write_text(
                        json.dumps([os.getpid(), child.pid]), encoding="utf-8"
                    )
                    Path({str(ready_path)!r}).write_text("ready", encoding="utf-8")
                    while True:
                        time.sleep(1)
                    """
                ),
                encoding="utf-8",
            )
            worker = root / "worker.py"
            worker.write_text(
                textwrap.dedent(
                    f"""
                    import json
                    import signal
                    import sys
                    from pathlib import Path

                    from autodesign.agents.external_author_process import (
                        ExternalAuthorProcessRequest,
                        run_external_author_process,
                    )
                    from autodesign.run_worker_protocol import decode_request
                    from autodesign.schema import RunResult

                    request = decode_request(sys.stdin.buffer)
                    run_dir = Path(request.settings.out_dir) / "runs" / request.run_id
                    ready_path = Path({str(ready_path)!r})
                    signal.signal(signal.SIGTERM, lambda *_args: None)
                    result = run_external_author_process(ExternalAuthorProcessRequest(
                        run_id=request.run_id,
                        attempt=1,
                        command=[sys.executable, {str(author)!r}],
                        cwd=run_dir,
                        prompt="completion fixture",
                        timeout_s=30,
                        stdout_path=run_dir / "author.stdout.log",
                        stderr_path=run_dir / "author.stderr.log",
                        run_dir=run_dir,
                        completion_requested=lambda: "artifact_ready" if ready_path.is_file() else None,
                        poll_interval_s=0.01,
                    ))
                    (run_dir / "worker_result.json").write_text(json.dumps({{
                        "job_kind": request.job_kind,
                        "run_id": request.run_id,
                        "ok": True,
                        "result": RunResult(
                            run_id=request.run_id,
                            run_dir=str(run_dir),
                            artifact_type="poster",
                            terminal_status="pass",
                            finalize_notes=result.reason,
                        ).model_dump(mode="json"),
                    }}), encoding="utf-8")
                    """
                ),
                encoding="utf-8",
            )

            settings = Settings(
                anthropic_api_key="",
                anthropic_base_url=None,
                gemini_api_key="",
                designer_model="test",
                critic_model="test",
                repo_root=Path(__file__).resolve().parents[1],
                out_dir=root / "out",
            )
            store = RunControlStore(runs_dir)
            reserved = store.reserve(run_id, "poster")
            store.transition(run_id, reserved, "queued")
            request = PipelineWorkerRequest(
                job_kind="pipeline",
                run_id=run_id,
                brief="external-author-completion-fixture",
                attachments=(),
                template=None,
                palette_id=None,
                resume_run=None,
                reference_poster=None,
                settings=settings,
            )
            supervisor = RunSupervisor(
                runs_dir,
                control_store=store,
                worker_command=(sys.executable, str(worker)),
                grace_s=0.15,
            )

            supervised = await supervisor.start(request)
            author_identities: list[ProcessIdentity] = []
            try:
                outcome = await asyncio.wait_for(supervisor.wait(run_id), timeout=10)
            finally:
                if supervised.process.returncode is None:
                    supervised.process.kill()
                    await supervised.process.wait()
                if author_pids_path.is_file():
                    for pid in json.loads(author_pids_path.read_text(encoding="utf-8")):
                        try:
                            identity = process_identity(int(pid))
                        except (OSError, ProcessLookupError, ValueError):
                            continue
                        if process_is_alive(identity):
                            author_identities.append(identity)
                    if author_identities:
                        terminate_process_identities(
                            author_identities,
                            root_pid=author_identities[0].pid,
                            grace_s=0.1,
                        )

            self.assertEqual(outcome.returncode, 0)
            self.assertTrue(outcome.ok, outcome.error)
            assert outcome.result is not None
            self.assertEqual(outcome.result["finalize_notes"], "artifact_ready")
            self.assertTrue((run_dir / "worker_result.json").is_file())
            for pid in json.loads(author_pids_path.read_text(encoding="utf-8")):
                await _wait_for_async(lambda pid=pid: _pid_is_dead(int(pid)))

    async def test_supervisor_cancel_seals_author_ledger_and_stops_future_output(self) -> None:
        """The supervisor must reach the real external-author registered spawn path."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runs_dir = root / "out" / "runs"
            run_id = "supervised-external-author"
            run_dir = runs_dir / run_id
            marker = run_dir / "author-output.log"
            pids_path = run_dir / "external-author-pids.json"
            process_secret = "supervisor-hard-cancel-secret"
            grandchild = root / "grandchild.py"
            grandchild.write_text(
                "from pathlib import Path\nimport time\n"
                f"marker = Path({str(marker)!r})\n"
                "while True:\n"
                "    with marker.open('a', encoding='utf-8') as handle:\n"
                "        handle.write('grandchild\\n')\n"
                "    time.sleep(0.01)\n",
                encoding="utf-8",
            )
            author = root / "author.py"
            author.write_text(
                "import json, os, subprocess, sys, time\n"
                "from pathlib import Path\n"
                f"print({process_secret!r}, flush=True)\n"
                f"print({process_secret!r}, file=sys.stderr, flush=True)\n"
                f"child = subprocess.Popen([sys.executable, {str(grandchild)!r}], start_new_session=True)\n"
                f"Path({str(pids_path)!r}).write_text(json.dumps([os.getpid(), child.pid]))\n"
                f"marker = Path({str(marker)!r})\n"
                "while True:\n"
                "    with marker.open('a', encoding='utf-8') as handle:\n"
                "        handle.write('author\\n')\n"
                "    time.sleep(0.01)\n",
                encoding="utf-8",
            )
            worker = root / "worker.py"
            worker.write_text(
                textwrap.dedent(
                    f"""
                    import sys
                    from pathlib import Path
                    from autodesign.agents.external_author_process import ExternalAuthorProcessRequest, run_external_author_process
                    from autodesign.run_control import CancellationToken, RunControlStore
                    from autodesign.run_worker_protocol import decode_request

                    request = decode_request(sys.stdin.buffer)
                    run_dir = Path(request.settings.out_dir) / "runs" / request.run_id
                    token = CancellationToken.for_run(RunControlStore(run_dir.parent), request.run_id)
                    run_external_author_process(ExternalAuthorProcessRequest(
                        run_id=request.run_id,
                        attempt=1,
                        command=[sys.executable, {str(author)!r}],
                        cwd=run_dir,
                        prompt="supervised prompt",
                        timeout_s=60,
                        stdout_path=run_dir / "author.stdout.log",
                        stderr_path=run_dir / "author.stderr.log",
                        run_dir=run_dir,
                        cancellation_token=token,
                        interruption_requested=token.is_cancelled,
                        sensitive_values=({process_secret!r},),
                    ))
                    """
                ),
                encoding="utf-8",
            )

            settings = Settings(
                anthropic_api_key="",
                anthropic_base_url=None,
                gemini_api_key="",
                designer_model="test",
                critic_model="test",
                harness_api_key=process_secret,
                repo_root=Path(__file__).resolve().parents[1],
                out_dir=root / "out",
            )
            store = RunControlStore(runs_dir)
            reserved = store.reserve(run_id, "poster")
            store.transition(run_id, reserved, "queued")
            request = PipelineWorkerRequest(
                job_kind="pipeline",
                run_id=run_id,
                brief="external-author-fixture",
                attachments=(),
                template=None,
                palette_id=None,
                resume_run=None,
                reference_poster=None,
                settings=settings,
            )
            supervisor = RunSupervisor(
                runs_dir,
                control_store=store,
                worker_command=(sys.executable, str(worker)),
                grace_s=0.15,
            )
            supervised = await supervisor.start(request)
            await _wait_for_async(pids_path.is_file)
            await _wait_for_async(lambda: marker.is_file() and marker.stat().st_size > 0)
            pids = json.loads(pids_path.read_text(encoding="utf-8"))
            await _wait_for_async(
                lambda: {item.role for item in ProcessLedger(run_dir).read().processes}
                >= {"root-worker", "external-author"}
            )

            outcome = await supervisor.cancel(run_id, "integration_cancel")

            self.assertEqual(outcome.state, "cancelled")
            self.assertTrue(ProcessLedger(run_dir).read().sealed)
            control = store.read(run_id)
            self.assertEqual(control.state, "cancelled")
            self.assertTrue(control.writes_frozen)
            for pid in [supervised.process.pid, *pids]:
                await _wait_for_async(lambda pid=pid: _pid_is_dead(pid))
            size_after_cancel = marker.stat().st_size
            await asyncio.sleep(0.08)
            self.assertEqual(marker.stat().st_size, size_after_cancel)
            for path in run_dir.rglob("*"):
                if path.is_file():
                    self.assertNotIn(process_secret.encode(), path.read_bytes())


if __name__ == "__main__":
    unittest.main()
