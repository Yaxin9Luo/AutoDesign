from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from autodesign.agents.external_code_editor import (
    CodeEditorError,
    ExternalCodeEditor,
    _InvocationResult,
)
from autodesign.process_supervision import (
    ProcessIdentity,
    ProcessLedger,
    process_identity,
    process_is_alive,
    terminate_process_identities,
)
from autodesign.run_control import CancellationToken, RunCancelled


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _poster_html(label: str) -> str:
    return f"""<!doctype html>
<html><head><style>
.paper-poster {{ width: 1600px; height: 900px; background: #fff; color: #111; }}
.paper-poster section {{ border: 1px solid #777; padding: 24px; }}
</style></head><body><main class="paper-poster">
<header><h1>Grounded paper title</h1><p>Known Authors - Known Institution</p></header>
<section data-block-id="method"><h2>Method</h2><p>{label}</p>
<p>This grounded revision keeps the existing editable poster structure and adds enough real text for validation.</p></section>
</main></body></html>
"""


def _wait_until(predicate, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true before deadline")


def _pid_is_dead(pid: int) -> bool:
    try:
        identity = process_identity(pid)
    except (OSError, ProcessLookupError, ValueError):
        return True
    return not process_is_alive(identity)


class _PhaseCancellationToken:
    def __init__(self, cancel_phase: str, *, run_id: str = "edit-child") -> None:
        self.run_id = run_id
        self.cancel_phase = cancel_phase
        self.phases: list[str] = []
        self.cancelled = False

    def is_cancelled(self) -> bool:
        return self.cancelled

    def raise_if_cancelled(self, phase: str) -> None:
        self.phases.append(phase)
        if phase == self.cancel_phase:
            self.cancelled = True
        if self.cancelled:
            raise RunCancelled(self.run_id, phase)


class _ControlledEditor(ExternalCodeEditor):
    def __init__(self, settings: object, *, valid: bool) -> None:
        super().__init__(settings)
        self.valid = valid
        self.invocations: list[tuple[object, Path]] = []

    def _invoke_command(
        self,
        command: str,
        *,
        prompt: str,
        attempt_dir: Path,
        timeout_s: int,
        run_dir: Path,
        cancellation_token: object,
    ) -> _InvocationResult:
        del command, prompt, timeout_s
        self.invocations.append((cancellation_token, run_dir))
        html = _poster_html("Revised result") if self.valid else "<script>bad</script>"
        (attempt_dir / "poster.html").write_text(html, encoding="utf-8")
        (attempt_dir / "code_editor_done.json").write_text(
            '{"status":"completed"}\n', encoding="utf-8"
        )
        return _InvocationResult(status="ok", reason="done_marker", returncode=0)


class ExternalCodeEditorCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.run_dir = self.root / "runs" / "edit-child"
        self.run_dir.mkdir(parents=True)
        self.source_final_dir = self.root / "parent" / "final"
        self.source_final_dir.mkdir(parents=True)
        self.source_poster = self.source_final_dir / "poster.html"
        self.source_poster.write_text(_poster_html("Original result"), encoding="utf-8")
        self._owned_identities: set[ProcessIdentity] = set()

    def tearDown(self) -> None:
        for ledger_path in self.root.rglob("process_ledger.json"):
            try:
                snapshot = ProcessLedger(ledger_path.parent).read()
            except Exception:
                continue
            self._owned_identities.update(record.identity for record in snapshot.processes)
        if self._owned_identities:
            root_pid = next(iter(self._owned_identities)).pid
            terminate_process_identities(
                tuple(self._owned_identities), root_pid=root_pid, grace_s=0.1
            )
        self._tmp.cleanup()

    def _settings(self, **updates: object) -> SimpleNamespace:
        values: dict[str, object] = {
            "code_editor_cmd": shlex.join([sys.executable, "unused-editor.py"]),
            "code_editor_harness": "custom",
            "code_editor_timeout_s": 2,
            "code_editor_max_attempts": 2,
            "skills_dir": _REPO_ROOT / "skills",
        }
        values.update(updates)
        return SimpleNamespace(**values)

    def _run(
        self,
        editor: ExternalCodeEditor,
        token: object,
    ):
        return editor.run(
            source_poster_path=self.source_poster,
            source_final_dir=self.source_final_dir,
            run_dir=self.run_dir,
            parent_run_id="parent-run",
            instruction="Revise the method panel.",
            conversation_history=[],
            context_run_dirs=[],
            required_color_system={},
            cancellation_token=token,
        )

    def test_already_cancelled_starts_no_directories_files_or_processes(self) -> None:
        token = _PhaseCancellationToken("code_editor.before_start")
        before = list(self.run_dir.iterdir())

        with self.assertRaises(RunCancelled):
            self._run(ExternalCodeEditor(self._settings()), token)

        self.assertEqual(list(self.run_dir.iterdir()), before)
        self.assertFalse((self.run_dir / "code_editor").exists())
        self.assertFalse((self.run_dir / "process_ledger.json").exists())

    def test_exact_token_and_run_dir_are_threaded_to_every_attempt(self) -> None:
        token = _PhaseCancellationToken("never")
        editor = _ControlledEditor(self._settings(), valid=False)

        with self.assertRaises(CodeEditorError) as caught:
            self._run(editor, token)

        self.assertEqual(len(caught.exception.attempts), 2)
        self.assertEqual(editor.invocations, [(token, self.run_dir), (token, self.run_dir)])

    def test_release_guard_prevents_target_start_and_run_cancelled_escapes(self) -> None:
        attempt_dir = self.run_dir / "attempt-release"
        attempt_dir.mkdir()
        marker = attempt_dir / "target_started.txt"
        script = attempt_dir / "editor.py"
        script.write_text(
            "from pathlib import Path\nPath('target_started.txt').write_text('started')\n",
            encoding="utf-8",
        )
        token = _PhaseCancellationToken("external_author.before_spawn_release")
        editor = ExternalCodeEditor(self._settings())

        generic_handler_ran = False
        caught: RunCancelled | None = None
        try:
            try:
                editor._invoke_command(
                    shlex.join([sys.executable, str(script)]),
                    prompt="edit",
                    attempt_dir=attempt_dir,
                    timeout_s=2,
                    run_dir=self.run_dir,
                    cancellation_token=token,
                )
            except Exception:
                generic_handler_ran = True
        except RunCancelled as exc:
            caught = exc

        self.assertFalse(generic_handler_ran)
        self.assertIsNotNone(caught)
        self.assertFalse(marker.exists())
        ledger = json.loads((self.run_dir / "process_ledger.json").read_text(encoding="utf-8"))
        for record in ledger["processes"]:
            _wait_until(lambda pid=record["identity"]["pid"]: _pid_is_dead(pid))

    @unittest.skipUnless(os.name == "posix", "detached process test requires POSIX")
    def test_blocking_editor_and_detached_grandchild_die_without_late_writes(self) -> None:
        attempt_dir = self.run_dir / "attempt-blocking"
        attempt_dir.mkdir()
        child_code = (
            "import pathlib,time\n"
            "p=pathlib.Path('grandchild_marker.txt')\n"
            "while True:\n"
            " p.write_text(str(time.monotonic()))\n"
            " time.sleep(0.02)\n"
        )
        script = attempt_dir / "blocking_editor.py"
        script.write_text(
            "import pathlib,subprocess,sys,time\n"
            f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}], start_new_session=True)\n"
            "pathlib.Path('grandchild.pid').write_text(str(child.pid))\n"
            "pathlib.Path('editor.pid').write_text(str(__import__('os').getpid()))\n"
            "while True: time.sleep(0.05)\n",
            encoding="utf-8",
        )
        cancelled = threading.Event()
        token = CancellationToken(
            store=None,
            run_id="edit-child",
            signal_event=cancelled,
        )
        editor = ExternalCodeEditor(self._settings())
        outcome: dict[str, BaseException | _InvocationResult] = {}

        def invoke() -> None:
            try:
                outcome["result"] = editor._invoke_command(
                    shlex.join([sys.executable, str(script)]),
                    prompt="edit",
                    attempt_dir=attempt_dir,
                    timeout_s=30,
                    run_dir=self.run_dir,
                    cancellation_token=token,
                )
            except BaseException as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=invoke)
        thread.start()
        grandchild_pid_path = attempt_dir / "grandchild.pid"
        editor_pid_path = attempt_dir / "editor.pid"
        marker = attempt_dir / "grandchild_marker.txt"
        _wait_until(lambda: grandchild_pid_path.exists() and editor_pid_path.exists())
        _wait_until(marker.exists)
        editor_pid = int(editor_pid_path.read_text(encoding="utf-8"))
        grandchild_pid = int(grandchild_pid_path.read_text(encoding="utf-8"))
        self._owned_identities.add(process_identity(editor_pid))
        self._owned_identities.add(process_identity(grandchild_pid))

        cancelled.set()
        _wait_until(lambda: not thread.is_alive())
        thread.join()

        self.assertIsInstance(outcome.get("error"), RunCancelled)
        _wait_until(lambda: _pid_is_dead(editor_pid) and _pid_is_dead(grandchild_pid))
        stable_bytes = marker.read_bytes()
        stable_mtime = marker.stat().st_mtime_ns
        stable_deadline = time.monotonic() + 0.2
        while time.monotonic() < stable_deadline:
            self.assertEqual(marker.read_bytes(), stable_bytes)
            self.assertEqual(marker.stat().st_mtime_ns, stable_mtime)
            time.sleep(0.01)

    def test_cancel_interrupts_large_prompt_delivery_to_nonreading_editor(self) -> None:
        attempt_dir = self.run_dir / "attempt-large-prompt"
        attempt_dir.mkdir()
        script = attempt_dir / "nonreading_editor.py"
        script.write_text(
            "from pathlib import Path\nimport os,time\n"
            "Path('editor.pid').write_text(str(os.getpid()))\n"
            "while True: time.sleep(0.05)\n",
            encoding="utf-8",
        )
        cancelled = threading.Event()
        token = CancellationToken(
            store=None,
            run_id="edit-child",
            signal_event=cancelled,
        )
        outcome: dict[str, BaseException | _InvocationResult] = {}

        def invoke() -> None:
            try:
                outcome["result"] = ExternalCodeEditor(self._settings())._invoke_command(
                    shlex.join([sys.executable, str(script)]),
                    prompt="x" * (8 * 1024 * 1024),
                    attempt_dir=attempt_dir,
                    timeout_s=30,
                    run_dir=self.run_dir,
                    cancellation_token=token,
                )
            except BaseException as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=invoke)
        thread.start()
        _wait_until((attempt_dir / "editor.pid").exists)
        cancelled.set()
        stopped_cooperatively = False
        try:
            _wait_until(lambda: not thread.is_alive(), timeout_s=1.0)
            stopped_cooperatively = True
        finally:
            if thread.is_alive():
                snapshot = ProcessLedger(self.run_dir).read()
                identities = tuple(record.identity for record in snapshot.processes)
                terminate_process_identities(
                    identities,
                    root_pid=identities[-1].pid if identities else None,
                    grace_s=0.1,
                )
                _wait_until(lambda: not thread.is_alive())
            thread.join()

        self.assertTrue(stopped_cooperatively)
        self.assertIsInstance(outcome.get("error"), RunCancelled)

    def test_cancel_after_validation_starts_no_repair_attempt_or_late_records(self) -> None:
        token = _PhaseCancellationToken("code_editor.attempt.after_validation")
        editor = _ControlledEditor(self._settings(), valid=False)

        with self.assertRaises(RunCancelled):
            self._run(editor, token)

        attempt = self.run_dir / "code_editor" / "attempt_01"
        self.assertTrue((attempt / "edit_prompt.md").exists())
        self.assertFalse((attempt / "code_editor_attempt_result.json").exists())
        self.assertFalse((attempt / "validation_feedback.json").exists())
        self.assertFalse((self.run_dir / "code_editor" / "attempt_02").exists())

    def test_cancellation_boundaries_stop_all_later_attempt_writes(self) -> None:
        cases = (
            ("code_editor.attempt.before_staging", False, False, False),
            ("code_editor.attempt.after_staging", True, False, False),
            ("code_editor.attempt.before_prompt_write", True, False, False),
            ("code_editor.attempt.after_prompt_write", True, True, False),
            ("code_editor.attempt.before_validation", True, True, False),
            ("code_editor.attempt.after_validation", True, True, False),
            ("code_editor.attempt.before_attempt_record_write", True, True, False),
            ("code_editor.attempt.after_attempt_record_write", True, True, True),
            ("code_editor.attempt.before_candidate_return", True, True, True),
        )
        for index, (phase, staged, prompted, recorded) in enumerate(cases):
            with self.subTest(phase=phase):
                run_dir = self.root / "runs" / f"boundary-{index}"
                run_dir.mkdir()
                original_run_dir = self.run_dir
                self.run_dir = run_dir
                try:
                    token = _PhaseCancellationToken(phase, run_id=run_dir.name)
                    editor = _ControlledEditor(self._settings(), valid=True)
                    with self.assertRaises(RunCancelled):
                        self._run(editor, token)
                finally:
                    self.run_dir = original_run_dir
                attempt = run_dir / "code_editor" / "attempt_01"
                self.assertEqual((attempt / "current_poster.html").exists(), staged)
                self.assertEqual((attempt / "edit_prompt.md").exists(), prompted)
                self.assertEqual(
                    (attempt / "code_editor_attempt_result.json").exists(), recorded
                )
                self.assertFalse((run_dir / "code_editor" / "attempt_02").exists())

    def test_ledger_persists_identity_but_not_command_environment_or_secrets(self) -> None:
        attempt_dir = self.run_dir / "attempt-ledger"
        attempt_dir.mkdir()
        script = attempt_dir / "success_editor.py"
        script.write_text(
            "from pathlib import Path\n"
            f"Path('poster.html').write_text({_poster_html('Secure edit')!r})\n"
            "Path('code_editor_done.json').write_text('{\"status\":\"completed\"}')\n",
            encoding="utf-8",
        )
        secret = "ledger-must-not-persist-this-secret"
        with patch.dict(os.environ, {"AUTODESIGN_TEST_PRIVATE_TOKEN": secret}):
            result = ExternalCodeEditor(self._settings())._invoke_command(
                shlex.join([sys.executable, str(script)]),
                prompt="edit",
                attempt_dir=attempt_dir,
                timeout_s=2,
                run_dir=self.run_dir,
                cancellation_token=CancellationToken.never("edit-child"),
            )

        self.assertEqual(result.status, "ok")
        ledger_text = (self.run_dir / "process_ledger.json").read_text(encoding="utf-8")
        self.assertNotIn(secret, ledger_text)
        self.assertNotIn(str(script), ledger_text)
        ledger = json.loads(ledger_text)
        self.assertEqual(set(ledger), {"version", "sealed", "processes", "spawning"})
        self.assertTrue(ledger["processes"])
        self.assertEqual(
            set(ledger["processes"][0]),
            {"identity", "role", "nonce", "registered_at"},
        )

    def test_process_registration_error_remains_a_retryable_invocation_error(self) -> None:
        attempt_dir = self.run_dir / "attempt-registration-error"
        attempt_dir.mkdir()
        target_marker = attempt_dir / "target_started.txt"
        script = attempt_dir / "editor.py"
        script.write_text(
            "from pathlib import Path\nPath('target_started.txt').write_text('started')\n",
            encoding="utf-8",
        )
        (self.run_dir / "process_ledger.json").write_text("{broken", encoding="utf-8")

        result = ExternalCodeEditor(self._settings())._invoke_command(
            shlex.join([sys.executable, str(script)]),
            prompt="edit",
            attempt_dir=attempt_dir,
            timeout_s=1,
            run_dir=self.run_dir,
            cancellation_token=CancellationToken.never("edit-child"),
        )

        self.assertEqual(result.status, "error")
        self.assertIn("command_start_error", result.reason)
        self.assertFalse(target_marker.exists())

    def test_done_marker_termination_preserves_other_registered_run_processes(self) -> None:
        sentinel = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=(os.name == "posix"),
        )
        sentinel_identity = process_identity(sentinel.pid)
        self._owned_identities.add(sentinel_identity)
        self.addCleanup(sentinel.wait, 2)
        self.addCleanup(
            terminate_process_identities,
            (sentinel_identity,),
            root_pid=sentinel.pid,
            grace_s=0.1,
        )
        ProcessLedger(self.run_dir).register_existing(
            sentinel_identity, role="root-worker"
        )
        attempt_dir = self.run_dir / "attempt-owned-child"
        attempt_dir.mkdir()
        script = attempt_dir / "editor.py"
        script.write_text(
            "from pathlib import Path\nimport time\n"
            f"Path('poster.html').write_text({_poster_html('Owned edit')!r})\n"
            "Path('code_editor_done.json').write_text('{\"status\":\"completed\"}')\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )

        result = ExternalCodeEditor(self._settings())._invoke_command(
            shlex.join([sys.executable, str(script)]),
            prompt="edit",
            attempt_dir=attempt_dir,
            timeout_s=2,
            run_dir=self.run_dir,
            cancellation_token=CancellationToken.never("edit-child"),
        )

        self.assertEqual(result.status, "ok")
        self.assertTrue(process_is_alive(sentinel_identity))

    def test_normal_done_marker_timeout_parse_error_and_max_attempts_are_preserved(self) -> None:
        success_dir = self.run_dir / "normal-success"
        success_dir.mkdir()
        success_script = success_dir / "success.py"
        success_script.write_text(
            "from pathlib import Path\nimport time\n"
            f"Path('poster.html').write_text({_poster_html('Normal success')!r})\n"
            "Path('code_editor_done.json').write_text('{\"status\":\"completed\"}')\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        editor = ExternalCodeEditor(self._settings())
        success = editor._invoke_command(
            shlex.join([sys.executable, str(success_script)]),
            prompt="edit",
            attempt_dir=success_dir,
            timeout_s=5,
            run_dir=self.run_dir,
            cancellation_token=CancellationToken.never("edit-child"),
        )
        self.assertEqual(success.status, "ok")
        self.assertEqual(success.reason, "done_marker")

        timeout_dir = self.run_dir / "normal-timeout"
        timeout_dir.mkdir()
        timeout_script = timeout_dir / "timeout.py"
        timeout_script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        timed_out = editor._invoke_command(
            shlex.join([sys.executable, str(timeout_script)]),
            prompt="edit",
            attempt_dir=timeout_dir,
            timeout_s=1,
            run_dir=self.run_dir,
            cancellation_token=CancellationToken.never("edit-child"),
        )
        self.assertEqual(timed_out.status, "error")
        self.assertTrue(timed_out.timed_out)

        parse_error = editor._invoke_command(
            "unterminated-'quote",
            prompt="edit",
            attempt_dir=timeout_dir,
            timeout_s=1,
            run_dir=self.run_dir,
            cancellation_token=CancellationToken.never("edit-child"),
        )
        self.assertEqual(parse_error.status, "error")
        self.assertIn("command_parse_error", parse_error.reason)

        controlled = _ControlledEditor(self._settings(), valid=False)
        with self.assertRaises(CodeEditorError) as caught:
            self._run(controlled, CancellationToken.never("edit-child"))
        self.assertEqual(caught.exception.reason, "code_editor_validation_failed")
        self.assertEqual(len(caught.exception.attempts), 2)
        self.assertTrue(
            all(record.validation["ok"] is False for record in caught.exception.attempts)
        )


if __name__ == "__main__":
    unittest.main()
