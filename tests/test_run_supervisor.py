from __future__ import annotations

import asyncio
from contextlib import redirect_stderr
from dataclasses import replace
import inspect
from io import StringIO
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from autodesign.config import Settings
from autodesign.process_supervision import (
    ProcessIdentity,
    ProcessLedger,
    ProcessLedgerError,
    owned_processes_are_quiescent,
    TerminationReport,
    process_identity,
    process_is_alive,
    spawn_registered_process,
    terminate_process_identities,
)
from autodesign.run_control import InvalidRunTransition, RunControlError, RunControlStore
from autodesign.run_supervisor import (
    RunSupervisor,
    TerminalReconciliation,
)
from autodesign.run_worker_protocol import PipelineWorkerRequest
from autodesign.util.logging import append_jsonl_event, read_jsonl_events


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cancellation_worker.py"


async def _wait_for(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.015)
    raise AssertionError("condition was not met before polling deadline")


def _pid_is_missing(pid: int) -> bool:
    try:
        process_identity(pid)
    except (OSError, ProcessLookupError, ValueError):
        return True
    return False


class RunSupervisorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runs_dir = self.root / "out" / "runs"
        self.secret = "super-secret-value-92731"
        self.secrets = (
            self.secret,
            "auth-token-secret-18472",
            "gemini-secret-38472",
            "openai-secret-58472",
            "openrouter-secret-68472",
            "harness-secret-78472",
            "openresearch-secret-88472",
            "header-secret-98472",
        )
        self.settings = Settings(
            anthropic_api_key=self.secret,
            anthropic_base_url=None,
            gemini_api_key=self.secrets[2],
            designer_model="designer-model",
            critic_model="critic-model",
            anthropic_auth_token=self.secrets[1],
            anthropic_custom_headers={"X-Private": self.secrets[7]},
            openai_compat_api_key=self.secrets[3],
            openrouter_api_key=self.secrets[4],
            harness_api_key=self.secrets[5],
            openresearch_token=self.secrets[6],
            repo_root=Path(__file__).resolve().parents[1],
            out_dir=self.root / "out",
        )
        self.store = RunControlStore(self.runs_dir)
        self.supervisors: list[RunSupervisor] = []

    async def asyncTearDown(self) -> None:
        for supervisor in self.supervisors:
            for run_id in list(supervisor.active_run_ids()):
                try:
                    await supervisor.cancel(run_id, "test_cleanup")
                except BaseException:
                    pass
        self._tmp.cleanup()

    def _request(self, run_id: str, mode: str | dict[str, object]) -> PipelineWorkerRequest:
        brief = json.dumps(mode) if isinstance(mode, dict) else mode
        return PipelineWorkerRequest(
            job_kind="pipeline",
            run_id=run_id,
            brief=brief,
            attachments=(),
            template=None,
            palette_id=None,
            resume_run=None,
            reference_poster=None,
            settings=self.settings,
        )

    def _queued(self, run_id: str) -> None:
        reserved = self.store.reserve(run_id, "poster")
        self.store.transition(run_id, reserved, "queued")

    def _supervisor(self, **kwargs) -> RunSupervisor:
        supervisor = RunSupervisor(
            self.runs_dir,
            control_store=self.store,
            worker_command=(sys.executable, str(FIXTURE)),
            grace_s=0.15,
            **kwargs,
        )
        self.supervisors.append(supervisor)
        return supervisor

    def _reconciliation(self, run_id: str) -> dict[str, str | None]:
        record = self.store.read(run_id)
        return {
            "decision": record.terminal_reconciliation_decision,
            "phase": record.terminal_reconciliation_phase,
            "terminal_state": record.terminal_reconciliation_terminal_state,
            "status": record.terminal_reconciliation_status,
            "diagnostic": record.terminal_reconciliation_diagnostic,
        }

    async def _start_mode(self, run_id: str, mode: str | dict[str, object], **kwargs):
        self._queued(run_id)
        supervisor = self._supervisor(**kwargs)
        supervised = await supervisor.start(self._request(run_id, mode))
        run_dir = self.runs_dir / run_id
        await _wait_for(lambda: (run_dir / "fixture_observation.json").is_file())
        return supervisor, supervised, run_dir

    async def test_settings_payload_is_sent_over_stdin_not_argv_or_disk(self) -> None:
        supervisor, supervised, run_dir = await self._start_mode("stdin-only", "success")
        await supervisor.wait("stdin-only")
        observation = json.loads((run_dir / "fixture_observation.json").read_text())

        self.assertFalse(observation["argv_contains_secret"])
        self.assertEqual(observation["env_keys_containing_secret"], [])
        self.assertEqual(observation["credential_env_keys"], [])
        self.assertNotIn(self.secret, " ".join(supervised.command))
        self.assertFalse(any(path.name.endswith("request.json") for path in run_dir.rglob("*")))

    async def test_secret_is_absent_from_argv_env_control_result_events_and_logs(self) -> None:
        supervisor, supervised, run_dir = await self._start_mode(
            "secret-redaction", {"mode": "secret_stderr", "stderr_bytes": 1_500_000},
            log_max_bytes=64_000,
        )
        await supervisor.wait("secret-redaction")

        self.assertNotIn(self.secret, " ".join(supervised.command))
        for path in self.root.rglob("*"):
            if path.is_file():
                contents = path.read_bytes()
                for secret in self.secrets:
                    self.assertNotIn(secret.encode(), contents, path)

    async def test_cancel_waits_until_worker_is_dead(self) -> None:
        supervisor, supervised, _ = await self._start_mode("blocked", "blocked")
        identity = process_identity(supervised.process.pid)

        outcome = await supervisor.cancel("blocked", "user_requested")

        self.assertEqual(outcome.state, "cancelled")
        self.assertFalse(process_is_alive(identity))
        self.assertIsNotNone(supervised.process.returncode)
        if os.name == "posix":
            with self.assertRaises(ProcessLookupError):
                os.kill(identity.pid, 0)

    async def test_cancellation_wins_missing_result_exit_race(self) -> None:
        supervisor, supervised, run_dir = await self._start_mode(
            "cancel-missing-result",
            "blocked",
        )

        cancellation = await supervisor.cancel(
            "cancel-missing-result",
            "user_requested",
        )
        worker_outcome = await supervised.monitor_task

        self.assertEqual(cancellation.state, "cancelled")
        self.assertIsNone(worker_outcome.exit_diagnostic)
        self.assertFalse(any(
            event.get("event") == "worker.exit"
            for event in read_jsonl_events(run_dir / "run_events.jsonl")
        ))

    async def test_cancel_kills_child_and_detached_grandchild(self) -> None:
        supervisor, supervised, run_dir = await self._start_mode(
            "tree", "spawn_detached_child"
        )
        await _wait_for(lambda: (run_dir / "fixture_descendant.json").is_file())
        ledger = ProcessLedger(run_dir)
        await _wait_for(lambda: len(ledger.read().processes) >= 3)
        identities = tuple(record.identity for record in ledger.read().processes)
        root_identity = process_identity(supervised.process.pid)

        outcome = await supervisor.cancel("tree", "user_requested")

        self.assertEqual(outcome.state, "cancelled")
        self.assertFalse(process_is_alive(root_identity))
        for identity in identities:
            self.assertFalse(process_is_alive(identity), identity)
            if os.name == "posix":
                with self.assertRaises(ProcessLookupError):
                    os.kill(identity.pid, 0)

    @unittest.skipUnless(os.name == "posix", "owner nonce scan is POSIX-specific")
    async def test_cancel_kills_unregistered_detached_descendant_by_owner_nonce(self) -> None:
        supervisor, _, run_dir = await self._start_mode(
            "unregistered-detached",
            "spawn_unregistered_detached_child",
        )
        pid_path = run_dir / "fixture_unregistered_detached_pid.txt"
        heartbeat = run_dir / "generated_media" / "unregistered_heartbeat.txt"
        await _wait_for(lambda: pid_path.is_file() and heartbeat.is_file())
        pid = int(pid_path.read_text(encoding="utf-8"))
        try:
            outcome = await supervisor.cancel(
                "unregistered-detached",
                "user_requested",
            )
            self.assertEqual(outcome.state, "cancelled")
            self.assertTrue(outcome.quiesced)
            await _wait_for(lambda: _pid_is_missing(pid))
            size_after_cancel = heartbeat.stat().st_size
            await asyncio.sleep(0.1)
            self.assertEqual(heartbeat.stat().st_size, size_after_cancel)
        finally:
            if not _pid_is_missing(pid):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    async def test_ignore_term_escalates_to_hard_kill(self) -> None:
        supervisor, supervised, _ = await self._start_mode("ignore-term", "ignore_term")
        identity = process_identity(supervised.process.pid)

        outcome = await supervisor.cancel("ignore-term", "user_requested")

        self.assertEqual(outcome.state, "cancelled")
        self.assertFalse(process_is_alive(identity))
        if os.name == "posix":
            self.assertEqual(supervised.process.returncode, -9)

    async def test_spawn_registration_race_cannot_orphan_child(self) -> None:
        supervisor, _, run_dir = await self._start_mode("spawn-race", "spawn_child")
        await _wait_for(lambda: len(ProcessLedger(run_dir).read().processes) >= 2)

        outcome = await supervisor.cancel("spawn-race", "race")
        ledger = ProcessLedger(run_dir).read()

        self.assertEqual(outcome.surviving_pids, ())
        for record in ledger.processes:
            self.assertFalse(process_is_alive(record.identity), record)

    async def test_cancel_between_queued_and_worker_registration_never_leaves_live_worker(self) -> None:
        run_id = "queued-registration-race"
        self._queued(run_id)
        supervisor = self._supervisor(root_registration_delay_s=0.2)
        start_task = asyncio.create_task(supervisor.start(self._request(run_id, "blocked")))
        ledger_path = self.runs_dir / run_id / "process_ledger.json"
        await _wait_for(
            lambda: ledger_path.exists()
            and '"status": "spawning"' in ledger_path.read_text(encoding="utf-8")
        )
        cancel_task = asyncio.create_task(supervisor.cancel(run_id, "race"))

        supervised = await start_task
        root_identity = next(
            record.identity
            for record in ProcessLedger(self.runs_dir / run_id).read().processes
            if record.role == "root-worker"
        )
        outcome = await cancel_task

        self.assertEqual(outcome.state, "cancelled")
        self.assertFalse(process_is_alive(root_identity))

    async def test_worker_crash_between_spawn_and_register_cannot_release_child_work(self) -> None:
        def crash_registration(_identity: ProcessIdentity) -> None:
            raise RuntimeError("simulated supervisor crash before durable registration")

        run_id = "registration-crash"
        self._queued(run_id)
        supervisor = self._supervisor(root_registration_hook=crash_registration)

        with self.assertRaises(RuntimeError):
            await supervisor.start(self._request(run_id, "spawn_child"))

        run_dir = self.runs_dir / run_id
        self.assertFalse((run_dir / "fixture_observation.json").exists())
        ledger = ProcessLedger(run_dir).read()
        for record in ledger.processes:
            self.assertFalse(process_is_alive(record.identity), record)

    async def test_recover_queued_registered_worker_terminates_before_failure(self) -> None:
        run_id = "queued-registered-recovery"
        self._queued(run_id)
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        identity = process_identity(process.pid)
        nonce = "queued-registered-worker"
        ProcessLedger(self.runs_dir / run_id).register_existing(
            identity,
            role="root-worker",
            nonce=nonce,
        )
        supervisor = self._supervisor()

        try:
            await supervisor.recover(run_id)

            self.assertFalse(process_is_alive(identity))
            self.assertEqual(self.store.read(run_id).state, "failed")
        finally:
            if process_is_alive(identity):
                terminate_process_identities(
                    (identity,),
                    root_pid=identity.pid,
                    grace_s=0.05,
                    owner_nonces=(nonce,),
                )
            process.wait(timeout=2)

    async def test_recover_terminal_record_still_terminates_registered_worker(self) -> None:
        run_id = "terminal-registered-recovery"
        self._queued(run_id)
        queued = self.store.read(run_id)
        self.store.transition(
            run_id,
            queued,
            "failed",
            publishable=False,
            cancellation_pending="supervisor_restart_interrupted",
        )
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        identity = process_identity(process.pid)
        nonce = "terminal-registered-worker"
        ProcessLedger(self.runs_dir / run_id).register_existing(
            identity,
            role="root-worker",
            nonce=nonce,
        )
        supervisor = self._supervisor()

        try:
            await supervisor.recover(run_id)

            self.assertFalse(process_is_alive(identity))
            self.assertEqual(self.store.read(run_id).state, "failed")
        finally:
            if process_is_alive(identity):
                terminate_process_identities(
                    (identity,),
                    root_pid=identity.pid,
                    grace_s=0.05,
                    owner_nonces=(nonce,),
                )
            process.wait(timeout=2)

    async def test_reused_pid_is_not_killed_and_cancel_converges_after_verified_nonce_scan(self) -> None:
        run_id = "stale-identity"
        self._queued(run_id)
        queued = self.store.read(run_id)
        current = process_identity(os.getpid())
        stale = replace(current, birth_id=current.birth_id + "-stale")
        ledger = ProcessLedger(self.runs_dir / run_id)
        ledger.register_existing(stale, role="stale-root", nonce="stale")
        self.store.transition(
            run_id, queued, "running", worker_pid=current.pid,
            worker_pgid=current.process_group_id, worker_birth_id=stale.birth_id,
        )
        supervisor = self._supervisor()

        first = await supervisor.cancel(run_id, "stale")
        second = await supervisor.cancel(run_id, "stale-again")
        restarted = self._supervisor()

        self.assertEqual(first.state, "cancelled")
        self.assertEqual(first.surviving_pids, ())
        self.assertEqual(second.state, "cancelled")
        self.assertTrue(second.already_terminal)
        self.assertTrue(second.quiesced)
        self.assertTrue(process_is_alive(current))
        self.assertEqual(self.store.read(run_id).state, "cancelled")
        self.assertTrue(restarted.is_durably_quiescent(run_id))

    def test_reused_pid_remains_nonquiescent_when_owner_nonce_scan_fails(self) -> None:
        current = process_identity(os.getpid())
        stale = replace(current, birth_id=current.birth_id + "-stale")

        with patch(
            "autodesign.process_supervision._process_identities_with_owner_nonces",
            side_effect=ProcessLedgerError("scan failed"),
        ):
            quiescent = owned_processes_are_quiescent(
                (stale,),
                owner_nonces=("stale-owner",),
            )

        self.assertFalse(quiescent)

    def test_reused_pid_is_not_quiescent_without_durable_owner_nonce(self) -> None:
        current = process_identity(os.getpid())
        stale = replace(current, birth_id=current.birth_id + "-stale")

        self.assertFalse(owned_processes_are_quiescent((stale,), owner_nonces=()))

    def test_reused_pid_stays_pending_while_spawn_intent_is_unresolved(self) -> None:
        current = process_identity(os.getpid())
        stale = replace(current, birth_id=current.birth_id + "-stale")

        report = terminate_process_identities(
            (stale,),
            root_pid=stale.pid,
            grace_s=0.01,
            unresolved_spawns=("spawn-in-flight",),
            owner_nonces=("stale-owner",),
        )

        self.assertEqual(report.unresolved_spawns, ("spawn-in-flight",))
        self.assertEqual(report.stale_identities, (stale,))
        self.assertIn(stale, report.survivors)
        self.assertTrue(process_is_alive(current))

    def test_reused_pid_stays_pending_when_an_owned_process_survives(self) -> None:
        current = process_identity(os.getpid())
        stale = replace(current, birth_id=current.birth_id + "-stale")

        with patch(
            "autodesign.process_supervision._terminate_owner_processes_until_quiescent",
            return_value=(current,),
        ):
            report = terminate_process_identities(
                (stale,),
                root_pid=stale.pid,
                grace_s=0.01,
                owner_nonces=("stale-owner",),
            )

        self.assertIn(stale, report.stale_identities)
        self.assertIn(current.pid, {identity.pid for identity in report.survivors})
        self.assertTrue(process_is_alive(current))

    def test_reused_pid_termination_fails_closed_when_nonce_scan_fails(self) -> None:
        current = process_identity(os.getpid())
        stale = replace(current, birth_id=current.birth_id + "-stale")

        with (
            patch(
                "autodesign.process_supervision._process_identities_with_owner_nonces",
                side_effect=ProcessLedgerError("scan failed"),
            ),
            self.assertRaises(ProcessLedgerError),
        ):
            terminate_process_identities(
                (stale,),
                root_pid=stale.pid,
                grace_s=0.01,
                owner_nonces=("stale-owner",),
            )

        self.assertTrue(process_is_alive(current))

    async def test_redacting_pipe_handles_secret_in_exception_and_high_volume_stderr(self) -> None:
        supervisor, _, run_dir = await self._start_mode(
            "bounded-stderr", {"mode": "secret_stderr", "stderr_bytes": 2_500_000},
            log_max_bytes=48_000,
        )

        outcome = await supervisor.wait("bounded-stderr")
        stderr = (run_dir / "worker_stderr.log").read_text(encoding="utf-8")

        self.assertEqual(outcome.returncode, 0)
        self.assertNotIn(self.secret, stderr)
        self.assertIn("[REDACTED]", stderr)
        self.assertIn("[log truncated", stderr)
        self.assertLess((run_dir / "worker_stderr.log").stat().st_size, 50_000)
        stdout = (run_dir / "worker_stdout.log").read_text(encoding="utf-8")
        for secret in self.secrets:
            self.assertNotIn(secret, stdout)
            self.assertNotIn(secret, stderr)

    async def test_abrupt_worker_exit_persists_bounded_redacted_diagnostic(self) -> None:
        supervisor, _, run_dir = await self._start_mode(
            "abrupt-without-result",
            {"mode": "abrupt_exit", "output_bytes": 96_000, "exit_code": 17},
            log_max_bytes=4_096,
        )

        outcome = await supervisor.wait("abrupt-without-result")

        self.assertEqual(outcome.returncode, 17)
        self.assertFalse(outcome.ok)
        diagnostic = outcome.exit_diagnostic
        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertEqual(diagnostic.error_code, "worker_result_missing")
        self.assertIn("worker_result.json is missing", diagnostic.protocol_error)
        self.assertEqual(diagnostic.last_event, "fixture.before_exit")
        self.assertEqual(diagnostic.last_phase, "authoring")
        self.assertEqual(diagnostic.last_reason, "fixture_crash")
        self.assertIn("stdout-final-marker", diagnostic.stdout_tail)
        self.assertIn("stderr-final-root-cause", diagnostic.stderr_tail)
        self.assertNotIn(self.secret, diagnostic.stderr_tail)
        self.assertNotIn(str(self.root), diagnostic.stderr_tail)
        self.assertIn("[REDACTED]", diagnostic.stderr_tail)
        self.assertLessEqual(len(diagnostic.stdout_tail), 2_048)
        self.assertLessEqual(len(diagnostic.stderr_tail), 2_048)

        exit_events = [
            event
            for event in read_jsonl_events(run_dir / "run_events.jsonl")
            if event.get("event") == "worker.exit"
        ]
        self.assertEqual(len(exit_events), 1)
        persisted = exit_events[0]
        self.assertEqual(persisted["version"], 1)
        self.assertEqual(persisted["returncode"], 17)
        self.assertEqual(persisted["error_code"], "worker_result_missing")
        self.assertNotIn("unsafe_payload", persisted)
        serialized = json.dumps(persisted, ensure_ascii=False)
        self.assertNotIn(self.secret, serialized)
        self.assertNotIn(str(self.root), serialized)
        self.assertLessEqual(len(serialized), 8_192)

    async def test_result_protocol_matrix_preserves_richer_failure_precedence(self) -> None:
        cases = (
            ("missing-zero", {"mode": "abrupt_exit", "exit_code": 0}, "worker_result_missing"),
            ("malformed-zero", "malformed_result", "worker_result_invalid"),
            ("mismatched-zero", "mismatched_result", "worker_result_invalid"),
            ("success-nonzero", "success_nonzero", "worker_exit_contradiction"),
        )
        for run_id, mode, expected_code in cases:
            with self.subTest(run_id=run_id):
                supervisor, _, _run_dir = await self._start_mode(run_id, mode)
                outcome = await supervisor.wait(run_id)
                self.assertFalse(outcome.ok)
                self.assertIsNotNone(outcome.exit_diagnostic)
                assert outcome.exit_diagnostic is not None
                self.assertEqual(outcome.exit_diagnostic.error_code, expected_code)

        supervisor, _, run_dir = await self._start_mode(
            "structured-worker-failure",
            "structured_failure",
        )
        outcome = await supervisor.wait("structured-worker-failure")
        self.assertFalse(outcome.ok)
        self.assertIsNone(outcome.exit_diagnostic)
        self.assertIsNone(outcome.failure_phase)
        self.assertIn("specific structured worker failure", outcome.error or "")
        self.assertFalse(any(
            event.get("event") == "worker.exit"
            for event in read_jsonl_events(run_dir / "run_events.jsonl")
        ))

    async def test_worker_exit_diagnostic_survives_event_persistence_failure(self) -> None:
        original_append = append_jsonl_event

        def fail_only_worker_exit(path, payload, *, event_id=None):
            if payload.get("event") == "worker.exit":
                raise OSError("simulated event append failure")
            return original_append(path, payload, event_id=event_id)

        with patch(
            "autodesign.run_supervisor.append_jsonl_event",
            side_effect=fail_only_worker_exit,
        ):
            supervisor, _, run_dir = await self._start_mode(
                "exit-event-persist-failure",
                {"mode": "abrupt_exit", "exit_code": 17},
            )
            outcome = await supervisor.wait("exit-event-persist-failure")

        self.assertIsNotNone(outcome.exit_diagnostic)
        assert outcome.exit_diagnostic is not None
        self.assertEqual(outcome.error, "worker_result.json is missing")
        self.assertEqual(
            outcome.exit_diagnostic.error_code,
            "worker_result_missing",
        )
        self.assertFalse(any(
            event.get("event") == "worker.exit"
            for event in read_jsonl_events(run_dir / "run_events.jsonl")
        ))

    async def test_cancel_stops_future_file_writes(self) -> None:
        supervisor, _, run_dir = await self._start_mode("write-loop", "write_loop")
        target = run_dir / "generated_media" / "heartbeat.txt"
        await _wait_for(lambda: target.exists() and target.stat().st_size >= 15)

        outcome = await supervisor.cancel("write-loop", "user_requested")
        frozen = target.read_bytes()
        for _ in range(8):
            await asyncio.sleep(0.025)
            self.assertEqual(target.read_bytes(), frozen)

        self.assertEqual(outcome.state, "cancelled")

    async def test_cancel_reconciles_after_verified_death_before_snapshot_and_event(self) -> None:
        observations: list[TerminalReconciliation] = []
        holder: dict[str, ProcessIdentity] = {}

        async def reconcile(request: TerminalReconciliation) -> None:
            observations.append(request)
            self.assertEqual(request.phase, "commit")
            self.assertEqual(request.decision, "reject")
            self.assertEqual(request.terminal_state, "cancelled")
            self.assertEqual(request.record.state, "cancelling")
            self.assertFalse(request.record.writes_frozen)
            self.assertFalse(process_is_alive(holder["worker"]))
            run_dir = self.runs_dir / request.run_id
            self.assertFalse((run_dir / "cancel_snapshot.json").exists())
            self.assertFalse(any(
                event.get("event") == "run.cancelled"
                for event in read_jsonl_events(run_dir / "run_events.jsonl")
            ))
            await asyncio.sleep(0)

        supervisor, supervised, run_dir = await self._start_mode(
            "cancel-reconcile-order",
            "blocked",
            terminal_reconciler=reconcile,
        )
        holder["worker"] = process_identity(supervised.process.pid)

        outcome = await supervisor.cancel("cancel-reconcile-order", "user_requested")

        self.assertEqual(outcome.state, "cancelled")
        self.assertEqual(len(observations), 1)
        self.assertTrue((run_dir / "cancel_snapshot.json").is_file())
        self.assertEqual(self.store.read("cancel-reconcile-order").state, "cancelled")

    async def test_cancel_reconciliation_failure_stays_cancelling_until_retry(self) -> None:
        attempts = 0

        def reconcile(request: TerminalReconciliation) -> None:
            nonlocal attempts
            attempts += 1
            self.assertEqual(request.decision, "reject")
            if attempts == 1:
                raise OSError("simulated promotion rollback failure")

        supervisor, _, run_dir = await self._start_mode(
            "cancel-reconcile-retry",
            "blocked",
            terminal_reconciler=reconcile,
        )

        first = await supervisor.cancel("cancel-reconcile-retry", "user_requested")
        pending = self.store.read("cancel-reconcile-retry")

        self.assertEqual(first.state, "cancelling")
        self.assertEqual(pending.state, "cancelling")
        self.assertEqual(pending.cancellation_pending, "terminal_reconciliation_failed")
        self.assertFalse(pending.writes_frozen)
        self.assertFalse((run_dir / "cancel_snapshot.json").exists())
        self.assertFalse(any(
            event.get("event") == "run.cancelled"
            for event in read_jsonl_events(run_dir / "run_events.jsonl")
        ))

        restarted_without_hook = self._supervisor()
        await restarted_without_hook.recover("cancel-reconcile-retry")
        still_pending = self.store.read("cancel-reconcile-retry")

        self.assertEqual(still_pending.state, "cancelling")
        self.assertEqual(still_pending.cancellation_pending, "terminal_reconciler_missing")
        self.assertFalse((run_dir / "cancel_snapshot.json").exists())
        self.assertFalse(any(
            event.get("event") == "run.cancelled"
            for event in read_jsonl_events(run_dir / "run_events.jsonl")
        ))

        second = await supervisor.cancel("cancel-reconcile-retry", "user_requested")

        self.assertEqual(second.state, "cancelled")
        self.assertEqual(attempts, 2)
        self.assertEqual(self.store.read("cancel-reconcile-retry").state, "cancelled")

    async def test_cancel_waits_for_external_completion_monitor_quiescence(self) -> None:
        monitor_joined = False

        async def quiesce(_run_id: str) -> bool:
            return monitor_joined

        supervisor, _, run_dir = await self._start_mode(
            "cancel-web-monitor",
            "blocked",
            cancellation_quiescer=quiesce,
        )

        first = await supervisor.cancel("cancel-web-monitor", "user_requested")
        pending = self.store.read("cancel-web-monitor")

        self.assertEqual(first.state, "cancelling")
        self.assertEqual(pending.state, "cancelling")
        self.assertEqual(
            pending.cancellation_pending,
            "web_completion_monitor_not_joined",
        )
        self.assertFalse((run_dir / "cancel_snapshot.json").exists())

        monitor_joined = True
        second = await supervisor.cancel("cancel-web-monitor", "user_requested")

        self.assertEqual(second.state, "cancelled")
        self.assertEqual(self.store.read("cancel-web-monitor").state, "cancelled")

    async def test_terminal_run_reports_its_real_state_when_quiescence_is_pending(self) -> None:
        run_id = "completed-monitor-pending"
        self._queued(run_id)
        record = self.store.read(run_id)
        record = self.store.transition(run_id, record, "running")
        record = self.store.transition(run_id, record, "completing")
        self.store.transition(run_id, record, "completed", publishable=True)
        supervisor = self._supervisor(
            cancellation_quiescer=lambda _run_id: False,
        )

        outcome = await supervisor.cancel(run_id, "late_cancel")

        self.assertEqual(outcome.state, "completed")
        self.assertTrue(outcome.already_terminal)
        self.assertFalse(outcome.quiesced)
        self.assertEqual(self.store.read(run_id).state, "completed")

    async def test_terminal_run_cancel_terminates_owned_process_before_confirming_quiescence(self) -> None:
        run_id = "completed-owned-process"
        self._queued(run_id)
        record = self.store.read(run_id)
        record = self.store.transition(run_id, record, "running")
        record = self.store.transition(run_id, record, "completing")
        self.store.transition(run_id, record, "completed", publishable=True)
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        identity = process_identity(process.pid)
        nonce = "completed-owned-process"
        ProcessLedger(self.runs_dir / run_id).register_existing(
            identity,
            role="root-worker",
            nonce=nonce,
        )
        supervisor = self._supervisor(terminal_reconciler=lambda _request: None)

        try:
            outcome = await supervisor.cancel(run_id, "late_cancel")

            self.assertFalse(process_is_alive(identity))
            self.assertEqual(outcome.state, "completed")
            self.assertTrue(outcome.already_terminal)
            self.assertTrue(outcome.quiesced)
            self.assertTrue(supervisor.is_durably_quiescent(run_id))
        finally:
            if process_is_alive(identity):
                terminate_process_identities(
                    (identity,),
                    root_pid=identity.pid,
                    grace_s=0.05,
                    owner_nonces=(nonce,),
                )
            process.wait(timeout=2)

    async def test_cancel_reconciliation_reentry_fails_fast_and_stays_pending(self) -> None:
        run_id = "cancel-reconcile-reentry"
        holder: dict[str, RunSupervisor] = {}

        async def reconcile(_request: TerminalReconciliation) -> None:
            await holder["supervisor"].cancel(run_id, "reentrant_callback")

        supervisor, _, run_dir = await self._start_mode(
            run_id,
            "blocked",
            terminal_reconciler=reconcile,
        )
        holder["supervisor"] = supervisor

        try:
            outcome = await asyncio.wait_for(
                supervisor.cancel(run_id, "user_requested"),
                timeout=1.0,
            )
        finally:
            supervisor._terminal_reconciler = None

        pending = self.store.read(run_id)
        self.assertEqual(outcome.state, "cancelling")
        self.assertEqual(pending.state, "cancelling")
        self.assertEqual(pending.cancellation_pending, "terminal_reconciliation_reentrant")
        self.assertFalse((run_dir / "cancel_snapshot.json").exists())
        self.assertFalse(any(
            event.get("event") == "run.cancelled"
            for event in read_jsonl_events(run_dir / "run_events.jsonl")
        ))

    async def test_second_cancel_is_idempotent(self) -> None:
        supervisor, _, _ = await self._start_mode("twice", "blocked")

        first = await supervisor.cancel("twice", "first")
        first_record = self.store.read("twice")
        events_path = self.runs_dir / "twice" / "run_events.jsonl"
        first_events = events_path.read_bytes() if events_path.exists() else b""
        second = await supervisor.cancel("twice", "second")
        second_record = self.store.read("twice")
        second_events = events_path.read_bytes() if events_path.exists() else b""

        self.assertEqual(first.state, "cancelled")
        self.assertEqual(second.state, "cancelled")
        self.assertTrue(second.already_terminal)
        self.assertEqual(second_record.revision, first_record.revision)
        self.assertEqual(second_record.accepted_terminal_event_id, first_record.accepted_terminal_event_id)
        self.assertEqual(second_events, first_events)

    async def test_failed_kill_remains_cancelling(self) -> None:
        supervisor, supervised, _ = await self._start_mode("kill-failure", "blocked")
        identity = process_identity(supervised.process.pid)
        fake_report = TerminationReport(
            terminated=(), survivors=(identity,), stale_identities=(), unresolved_spawns=(),
        )

        with patch("autodesign.run_supervisor.terminate_process_identities", return_value=fake_report):
            outcome = await supervisor.cancel("kill-failure", "cannot-kill")

        self.assertEqual(outcome.state, "cancelling")
        self.assertEqual(outcome.surviving_pids, (identity.pid,))
        self.assertEqual(self.store.read("kill-failure").state, "cancelling")

    async def test_late_success_cannot_overwrite_cancelled(self) -> None:
        supervisor, _, run_dir = await self._start_mode(
            "late-success", {"mode": "delayed_success", "delay_s": 0.5}
        )
        outcome = await supervisor.cancel("late-success", "user_requested")
        (run_dir / "worker_result.json").write_text(
            json.dumps({"job_kind": "pipeline", "run_id": "late-success", "ok": True, "result": {"late": True}}),
            encoding="utf-8",
        )

        with self.assertRaises(InvalidRunTransition):
            await supervisor.accept_completion(
                "late-success", terminal_state="completed", publishable=True,
                result_digest="late-digest", terminal_event_id="late-event",
            )

        self.assertEqual(outcome.state, "cancelled")
        self.assertEqual(self.store.read("late-success").state, "cancelled")

    async def test_accept_completion_serializes_with_cancel_operation_lock(self) -> None:
        run_id = "completion-cancel-lock"
        self._queued(run_id)
        queued = self.store.read(run_id)
        running = self.store.transition(run_id, queued, "running")
        self.store.transition(run_id, running, "completing")
        supervisor = self._supervisor()
        operation_lock = supervisor._operation_locks.setdefault(run_id, asyncio.Lock())

        await operation_lock.acquire()
        completion = asyncio.create_task(
            supervisor.accept_completion(
                run_id,
                terminal_state="completed",
                publishable=True,
                result_digest="digest",
            )
        )
        await asyncio.sleep(0)

        self.assertFalse(completion.done())
        operation_lock.release()
        accepted = await completion
        self.assertEqual(accepted.state, "completed")

    async def test_completion_reconciliation_preflight_and_commit_precede_terminal_event(self) -> None:
        run_id = "completion-reconcile-order"
        self._queued(run_id)
        queued = self.store.read(run_id)
        running = self.store.transition(run_id, queued, "running")
        self.store.transition(run_id, running, "completing")
        phases: list[str] = []

        async def reconcile(request: TerminalReconciliation) -> None:
            phases.append(request.phase)
            self.assertEqual(request.decision, "accept")
            self.assertEqual(request.terminal_state, "completed")
            expected_state = "completing" if request.phase == "preflight" else "completed"
            self.assertEqual(request.record.state, expected_state)
            self.assertFalse(any(
                event.get("event") == "run.done"
                for event in read_jsonl_events(
                    self.runs_dir / run_id / "run_events.jsonl",
                )
            ))
            await asyncio.sleep(0)

        supervisor = self._supervisor(terminal_reconciler=reconcile)

        accepted = await supervisor.accept_completion(
            run_id,
            terminal_state="completed",
            publishable=True,
            result_digest="digest",
        )

        self.assertEqual(accepted.state, "completed")
        self.assertEqual(phases, ["preflight", "commit"])
        self.assertEqual(
            [
                event["event"]
                for event in read_jsonl_events(
                    self.runs_dir / run_id / "run_events.jsonl",
                )
                if event.get("event") == "run.done"
            ],
            ["run.done"],
        )

    async def test_async_preflight_cancellation_stays_nonterminal_and_retries(self) -> None:
        run_id = "async-preflight-cancelled"
        self._queued(run_id)
        queued = self.store.read(run_id)
        running = self.store.transition(run_id, queued, "running")
        self.store.transition(run_id, running, "completing")

        async def cancelled_preflight(request: TerminalReconciliation) -> None:
            await asyncio.sleep(0)
            if request.phase == "preflight":
                raise asyncio.CancelledError

        supervisor = self._supervisor(terminal_reconciler=cancelled_preflight)
        with self.assertRaises(asyncio.CancelledError):
            await supervisor.accept_completion(
                run_id,
                terminal_state="completed",
                publishable=True,
                result_digest="digest",
                terminal_event_id="async-preflight-event",
            )

        intent = self._reconciliation(run_id)
        self.assertEqual(self.store.read(run_id).state, "completing")
        self.assertEqual(intent["phase"], "preflight")
        self.assertEqual(intent["status"], "pending")
        self.assertEqual(intent["diagnostic"], "terminal_reconciliation_cancelled")
        self.assertFalse(read_jsonl_events(self.runs_dir / run_id / "run_events.jsonl"))

        retries: list[str] = []

        async def retry(request: TerminalReconciliation) -> None:
            await asyncio.sleep(0)
            retries.append(request.phase)

        supervisor._terminal_reconciler = retry
        accepted = await supervisor.accept_completion(
            run_id,
            terminal_state="completed",
            publishable=True,
            result_digest="digest",
            terminal_event_id="async-preflight-event",
        )

        self.assertEqual(accepted.state, "completed")
        self.assertEqual(retries, ["preflight", "commit"])

    async def test_async_commit_cancellation_recovers_before_terminal_event(self) -> None:
        run_id = "async-commit-cancelled"
        self._queued(run_id)
        queued = self.store.read(run_id)
        running = self.store.transition(run_id, queued, "running")
        self.store.transition(run_id, running, "completing")

        async def cancelled_commit(request: TerminalReconciliation) -> None:
            await asyncio.sleep(0)
            if request.phase == "commit":
                raise asyncio.CancelledError

        supervisor = self._supervisor(terminal_reconciler=cancelled_commit)
        with self.assertRaises(asyncio.CancelledError):
            await supervisor.accept_completion(
                run_id,
                terminal_state="completed",
                publishable=True,
                result_digest="digest",
                terminal_event_id="async-commit-event",
            )

        intent = self._reconciliation(run_id)
        self.assertEqual(self.store.read(run_id).state, "completed")
        self.assertEqual(intent["phase"], "commit")
        self.assertEqual(intent["status"], "pending")
        self.assertEqual(intent["diagnostic"], "terminal_reconciliation_cancelled")
        self.assertFalse(any(
            event.get("event_id") == "async-commit-event"
            for event in read_jsonl_events(self.runs_dir / run_id / "run_events.jsonl")
        ))

        retries: list[str] = []

        async def retry(request: TerminalReconciliation) -> None:
            await asyncio.sleep(0)
            retries.append(request.phase)

        restarted = self._supervisor(terminal_reconciler=retry)
        await restarted.recover(run_id)

        self.assertEqual(retries, ["commit"])
        self.assertEqual(
            len([
                event
                for event in read_jsonl_events(
                    self.runs_dir / run_id / "run_events.jsonl",
                )
                if event.get("event_id") == "async-commit-event"
            ]),
            1,
        )

    async def test_async_cancel_hook_failure_and_cancellation_retry_without_publication(self) -> None:
        async def exercise(run_id: str, *, cancellation: bool) -> None:
            self._queued(run_id)
            queued = self.store.read(run_id)
            self.store.transition(run_id, queued, "running")

            async def fail(_request: TerminalReconciliation) -> None:
                await asyncio.sleep(0)
                if cancellation:
                    raise asyncio.CancelledError
                raise OSError("async reconciliation failure")

            supervisor = self._supervisor(terminal_reconciler=fail)
            if cancellation:
                with self.assertRaises(asyncio.CancelledError):
                    await supervisor.cancel(run_id, "user_requested")
            else:
                outcome = await supervisor.cancel(run_id, "user_requested")
                self.assertEqual(outcome.state, "cancelling")

            run_dir = self.runs_dir / run_id
            intent = self._reconciliation(run_id)
            self.assertEqual(self.store.read(run_id).state, "cancelling")
            self.assertEqual(intent["status"], "pending")
            self.assertEqual(
                intent["diagnostic"],
                "terminal_reconciliation_cancelled"
                if cancellation else "terminal_reconciliation_failed",
            )
            self.assertFalse((run_dir / "cancel_snapshot.json").exists())
            self.assertFalse(any(
                event.get("event") == "run.cancelled"
                for event in read_jsonl_events(run_dir / "run_events.jsonl")
            ))

            retry_phases: list[str] = []

            async def retry(request: TerminalReconciliation) -> None:
                await asyncio.sleep(0)
                retry_phases.append(request.phase)

            supervisor._terminal_reconciler = retry
            retried = await supervisor.cancel(run_id, "user_requested")
            self.assertEqual(retried.state, "cancelled")
            self.assertEqual(retry_phases, ["commit"])

        await exercise("async-cancel-hook-error", cancellation=False)
        await exercise("async-cancel-hook-cancelled", cancellation=True)

    async def test_completion_reconciliation_failure_is_recovered_before_event_replay(self) -> None:
        run_id = "completion-reconcile-recovery"
        self._queued(run_id)
        queued = self.store.read(run_id)
        running = self.store.transition(run_id, queued, "running")
        self.store.transition(run_id, running, "completing")
        commit_attempts = 0

        def reconcile(request: TerminalReconciliation) -> None:
            nonlocal commit_attempts
            if request.phase == "commit":
                commit_attempts += 1
                if commit_attempts == 1:
                    raise OSError("simulated crash during promotion commit")

        supervisor = self._supervisor(terminal_reconciler=reconcile)

        with self.assertRaisesRegex(OSError, "promotion commit"):
            await supervisor.accept_completion(
                run_id,
                terminal_state="completed",
                publishable=True,
                result_digest="digest",
                terminal_event_id="reconcile-recovery-event",
            )

        self.assertEqual(self.store.read(run_id).state, "completed")
        self.assertFalse(any(
            event.get("event_id") == "reconcile-recovery-event"
            for event in read_jsonl_events(self.runs_dir / run_id / "run_events.jsonl")
        ))

        restarted_without_hook = self._supervisor()
        await restarted_without_hook.recover(run_id)

        intent = self._reconciliation(run_id)
        self.assertEqual(intent["status"], "pending")
        self.assertEqual(intent["diagnostic"], "terminal_reconciler_missing")
        self.assertFalse(any(
            event.get("event_id") == "reconcile-recovery-event"
            for event in read_jsonl_events(self.runs_dir / run_id / "run_events.jsonl")
        ))

        replacement_calls: list[TerminalReconciliation] = []

        def replacement_reconcile(request: TerminalReconciliation) -> None:
            replacement_calls.append(request)

        replacement = self._supervisor(terminal_reconciler=replacement_reconcile)
        await replacement.recover(run_id)

        self.assertEqual(commit_attempts, 1)
        self.assertEqual(
            [(item.phase, item.decision) for item in replacement_calls],
            [("commit", "accept")],
        )
        self.assertEqual(
            len([
                event
                for event in read_jsonl_events(self.runs_dir / run_id / "run_events.jsonl")
                if event.get("event_id") == "reconcile-recovery-event"
            ]),
            1,
        )

    async def test_terminal_recovery_with_corrupt_reconciliation_state_fails_closed(self) -> None:
        run_id = "corrupt-terminal-reconciliation"
        self._queued(run_id)
        queued = self.store.read(run_id)
        running = self.store.transition(run_id, queued, "running")
        completing = self.store.transition(run_id, running, "completing")
        self.store.transition(
            run_id,
            completing,
            "completed",
            publishable=True,
            result_digest="digest",
            terminal_event="run.done",
            accepted_terminal_event_id="corrupt-reconciliation-event",
        )
        run_dir = self.runs_dir / run_id
        control_path = run_dir / "run_control.json"
        corrupt = json.loads(control_path.read_text(encoding="utf-8"))
        corrupt["terminal_reconciliation_decision"] = "accept"
        corrupt["terminal_reconciliation_phase"] = "commit"
        corrupt["terminal_reconciliation_terminal_state"] = "completed"
        corrupt["terminal_reconciliation_status"] = "forged"
        control_path.write_text(json.dumps(corrupt), encoding="utf-8")
        calls = 0

        def reconcile(_request: TerminalReconciliation) -> None:
            nonlocal calls
            calls += 1

        supervisor = self._supervisor(terminal_reconciler=reconcile)
        await supervisor.recover(run_id)

        canonical = self._reconciliation(run_id)
        self.assertEqual(canonical["status"], "invalid")
        self.assertEqual(
            canonical["diagnostic"],
            "terminal_reconciliation_state_corrupt",
        )
        self.assertEqual(calls, 0)
        self.assertFalse(any(
            event.get("event_id") == "corrupt-reconciliation-event"
            for event in read_jsonl_events(run_dir / "run_events.jsonl")
        ))

    async def test_terminal_recovery_with_semantically_invalid_reconciliation_fails_closed(self) -> None:
        run_id = "semantic-invalid-terminal-reconciliation"
        self._queued(run_id)
        queued = self.store.read(run_id)
        running = self.store.transition(run_id, queued, "running")
        completing = self.store.transition(run_id, running, "completing")
        self.store.transition(
            run_id,
            completing,
            "failed",
            publishable=False,
            result_digest="digest",
            terminal_event="run.error",
            accepted_terminal_event_id="semantic-invalid-reconciliation-event",
        )
        run_dir = self.runs_dir / run_id
        control_path = run_dir / "run_control.json"
        forged = json.loads(control_path.read_text(encoding="utf-8"))
        forged["terminal_reconciliation_decision"] = "accept"
        forged["terminal_reconciliation_phase"] = "commit"
        forged["terminal_reconciliation_terminal_state"] = "failed"
        forged["terminal_reconciliation_status"] = "succeeded"
        forged["terminal_reconciliation_diagnostic"] = None
        control_path.write_text(json.dumps(forged), encoding="utf-8")
        hook_calls = 0

        def reconcile(_request: TerminalReconciliation) -> None:
            nonlocal hook_calls
            hook_calls += 1

        supervisor = self._supervisor(terminal_reconciler=reconcile)
        await supervisor.recover(run_id)

        canonical = self._reconciliation(run_id)
        self.assertEqual(canonical["status"], "invalid")
        self.assertEqual(
            canonical["diagnostic"],
            "terminal_reconciliation_state_corrupt",
        )
        self.assertEqual(hook_calls, 0)
        self.assertFalse(any(
            event.get("event_id") == "semantic-invalid-reconciliation-event"
            for event in read_jsonl_events(run_dir / "run_events.jsonl")
        ))

    async def test_failed_and_nonpublishable_completion_reconcile_as_rejected_failure(self) -> None:
        reconciliations: list[tuple[str, str, str]] = []

        def reconcile(request: TerminalReconciliation) -> None:
            reconciliations.append(
                (request.phase, request.decision, request.terminal_state),
            )

        supervisor = self._supervisor(terminal_reconciler=reconcile)
        for run_id, terminal_state, publishable in (
            ("explicit-failed-reconcile", "failed", False),
            ("nonpublishable-completed-reconcile", "completed", False),
        ):
            self._queued(run_id)
            queued = self.store.read(run_id)
            running = self.store.transition(run_id, queued, "running")
            self.store.transition(run_id, running, "completing")

            accepted = await supervisor.accept_completion(
                run_id,
                terminal_state=terminal_state,
                publishable=publishable,
                result_digest=f"digest-{run_id}",
            )

            self.assertEqual(accepted.state, "failed")
            self.assertFalse(accepted.publishable)

        self.assertEqual(
            reconciliations,
            [
                ("preflight", "reject", "failed"),
                ("commit", "reject", "failed"),
                ("preflight", "reject", "failed"),
                ("commit", "reject", "failed"),
            ],
        )

    async def test_cancel_intent_preempts_completion_waiting_on_operation_lock(self) -> None:
        run_id = "cancel-preempts-completion"
        self._queued(run_id)
        queued = self.store.read(run_id)
        running = self.store.transition(run_id, queued, "running")
        self.store.transition(run_id, running, "completing")
        supervisor = self._supervisor()
        operation_lock = supervisor._operation_locks.setdefault(run_id, asyncio.Lock())

        await operation_lock.acquire()
        completion = asyncio.create_task(
            supervisor.accept_completion(
                run_id,
                terminal_state="completed",
                publishable=True,
                result_digest="digest",
            )
        )
        await asyncio.sleep(0)
        cancellation = asyncio.create_task(supervisor.cancel(run_id, "user_requested"))
        await _wait_for(lambda: self.store.read(run_id).state == "cancelling")
        operation_lock.release()

        with self.assertRaises(InvalidRunTransition):
            await completion
        outcome = await cancellation
        self.assertEqual(outcome.state, "cancelled")
        self.assertEqual(self.store.read(run_id).state, "cancelled")

    async def test_cancel_during_completion_preflight_supersedes_uncommitted_accept_intent(self) -> None:
        run_id = "cancel-during-completion-preflight"
        self._queued(run_id)
        queued = self.store.read(run_id)
        running = self.store.transition(run_id, queued, "running")
        self.store.transition(run_id, running, "completing")
        preflight_started = asyncio.Event()
        release_preflight = asyncio.Event()
        reconciliations: list[tuple[str, str, str]] = []

        async def reconcile(request: TerminalReconciliation) -> None:
            reconciliations.append(
                (request.phase, request.decision, request.terminal_state),
            )
            if request.phase == "preflight":
                preflight_started.set()
                await release_preflight.wait()

        supervisor = self._supervisor(terminal_reconciler=reconcile)
        completion = asyncio.create_task(
            supervisor.accept_completion(
                run_id,
                terminal_state="completed",
                publishable=True,
                result_digest="digest",
            ),
        )
        await preflight_started.wait()
        cancellation = asyncio.create_task(
            supervisor.cancel(run_id, "user_requested"),
        )
        await _wait_for(lambda: self.store.read(run_id).state == "cancelling")
        release_preflight.set()

        with self.assertRaises(InvalidRunTransition):
            await completion
        outcome = await cancellation

        intent = self._reconciliation(run_id)
        self.assertEqual(outcome.state, "cancelled")
        self.assertEqual(self.store.read(run_id).state, "cancelled")
        self.assertEqual(intent["decision"], "reject")
        self.assertEqual(intent["terminal_state"], "cancelled")
        self.assertEqual(intent["phase"], "commit")
        self.assertEqual(intent["status"], "succeeded")
        self.assertEqual(
            reconciliations,
            [
                ("preflight", "accept", "completed"),
                ("commit", "reject", "cancelled"),
            ],
        )

    async def test_restart_failure_supersedes_pre_cas_completion_intent(self) -> None:
        run_id = "restart-supersedes-pre-cas-completion"
        self._queued(run_id)
        queued = self.store.read(run_id)
        running = self.store.transition(run_id, queued, "running")
        self.store.transition(run_id, running, "completing")
        first = self._supervisor(terminal_reconciler=lambda _request: None)
        original_transition = self.store.transition

        def crash_before_terminal_cas(*args, **kwargs):
            target = args[2] if len(args) > 2 else kwargs.get("target")
            if target == "completed":
                raise OSError("simulated crash before completion CAS")
            return original_transition(*args, **kwargs)

        with patch.object(self.store, "transition", side_effect=crash_before_terminal_cas):
            with self.assertRaisesRegex(OSError, "before completion CAS"):
                await first.accept_completion(
                    run_id,
                    terminal_state="completed",
                    publishable=True,
                    result_digest="digest",
                )

        before_recovery = self._reconciliation(run_id)
        self.assertEqual(before_recovery["decision"], "accept")
        self.assertEqual(before_recovery["phase"], "commit")
        self.assertEqual(before_recovery["status"], "pending")
        recovered_calls: list[tuple[str, str, str]] = []

        def recover_reconcile(request: TerminalReconciliation) -> None:
            recovered_calls.append(
                (request.phase, request.decision, request.terminal_state),
            )

        restarted = self._supervisor(terminal_reconciler=recover_reconcile)
        await restarted.recover(run_id)

        intent = self._reconciliation(run_id)
        self.assertEqual(self.store.read(run_id).state, "failed")
        self.assertEqual(intent["decision"], "reject")
        self.assertEqual(intent["terminal_state"], "failed")
        self.assertEqual(intent["status"], "succeeded")
        self.assertEqual(
            recovered_calls,
            [
                ("preflight", "reject", "failed"),
                ("commit", "reject", "failed"),
            ],
        )
        self.assertEqual(
            len([
                event
                for event in read_jsonl_events(
                    self.runs_dir / run_id / "run_events.jsonl",
                )
                if event.get("event") == "run.error"
            ]),
            1,
        )

    async def test_terminal_broadcast_sink_can_reenter_cancel_without_deadlock(self) -> None:
        run_id = "reentrant-terminal-sink"
        self._queued(run_id)
        queued = self.store.read(run_id)
        running = self.store.transition(run_id, queued, "running")
        self.store.transition(run_id, running, "completing")
        holder: dict[str, RunSupervisor] = {}

        async def sink(_event: dict[str, object]) -> None:
            await holder["supervisor"].cancel(run_id, "sink_reentry")

        supervisor = self._supervisor(event_sink=sink)
        holder["supervisor"] = supervisor

        accepted = await asyncio.wait_for(
            supervisor.accept_completion(
                run_id,
                terminal_state="completed",
                publishable=True,
                result_digest="digest",
            ),
            timeout=2,
        )

        self.assertEqual(accepted.state, "completed")

    async def test_completion_append_failure_cleans_active_and_recovery_repairs_event(self) -> None:
        run_id = "completion-append-recovery"
        self._queued(run_id)
        queued = self.store.read(run_id)
        running = self.store.transition(run_id, queued, "running")
        self.store.transition(run_id, running, "completing")
        supervisor = self._supervisor()
        supervisor._active[run_id] = SimpleNamespace()

        with patch(
            "autodesign.run_supervisor.append_jsonl_event",
            side_effect=OSError("disk append failed"),
        ):
            with self.assertRaises(OSError):
                await supervisor.accept_completion(
                    run_id,
                    terminal_state="completed",
                    publishable=True,
                    result_digest="digest",
                )

        self.assertNotIn(run_id, supervisor.active_run_ids())
        await supervisor.recover(run_id)
        terminal = [
            event for event in read_jsonl_events(self.runs_dir / run_id / "run_events.jsonl")
            if event.get("event") == "run.done"
        ]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(
            terminal[0]["event_id"],
            self.store.read(run_id).accepted_terminal_event_id,
        )

    async def test_recovery_repairs_terminal_event_despite_stale_active_entry(self) -> None:
        run_id = "terminal-recovery-stale-active"
        self._queued(run_id)
        queued = self.store.read(run_id)
        running = self.store.transition(run_id, queued, "running")
        completing = self.store.transition(run_id, running, "completing")
        completed = self.store.transition(
            run_id,
            completing,
            "completed",
            publishable=True,
            result_digest="digest",
            terminal_event="run.done",
            accepted_terminal_event_id="stale-active-terminal-event",
        )
        supervisor = self._supervisor()
        supervisor._active[run_id] = SimpleNamespace(
            process=SimpleNamespace(returncode=0),
        )

        await supervisor.recover(run_id)

        self.assertNotIn(run_id, supervisor.active_run_ids())
        terminal = [
            event for event in read_jsonl_events(self.runs_dir / run_id / "run_events.jsonl")
            if event.get("event") == "run.done"
        ]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["event_id"], completed.accepted_terminal_event_id)

    async def test_terminal_recovery_reconciliation_uses_operation_lock(self) -> None:
        run_id = "terminal-recovery-operation-lock"
        self._queued(run_id)
        queued = self.store.read(run_id)
        running = self.store.transition(run_id, queued, "running")
        completing = self.store.transition(run_id, running, "completing")
        self.store.transition(
            run_id,
            completing,
            "completed",
            publishable=True,
            result_digest="digest",
            terminal_event="run.done",
            accepted_terminal_event_id="terminal-recovery-operation-lock-event",
        )
        reconciled = asyncio.Event()

        def reconcile(_request: TerminalReconciliation) -> None:
            reconciled.set()

        supervisor = self._supervisor(terminal_reconciler=reconcile)
        operation_lock = supervisor._operation_locks.setdefault(run_id, asyncio.Lock())
        await operation_lock.acquire()
        recovery = asyncio.create_task(supervisor.recover(run_id))
        await asyncio.sleep(0.03)

        self.assertFalse(reconciled.is_set())
        self.assertFalse(recovery.done())

        operation_lock.release()
        await recovery
        self.assertTrue(reconciled.is_set())

    async def test_recover_does_not_hang_on_blocked_worker_event_sink(self) -> None:
        sink_started = asyncio.Event()
        release_sink = asyncio.Event()

        async def blocked_sink(_event: dict[str, object]) -> None:
            sink_started.set()
            await release_sink.wait()

        supervisor, supervised, run_dir = await self._start_mode(
            "recover-blocked-sink",
            "success",
            event_sink=blocked_sink,
        )
        await asyncio.wait_for(sink_started.wait(), timeout=2)
        await asyncio.wait_for(supervised.process.wait(), timeout=2)
        await _wait_for(lambda: (run_dir / "worker_result.json").is_file())

        recovered = await asyncio.wait_for(
            supervisor.recover("recover-blocked-sink"),
            timeout=2,
        )

        self.assertIsNone(recovered)
        self.assertEqual(self.store.read("recover-blocked-sink").state, "completing")

    async def test_cancel_joins_blocked_monitor_after_worker_exit(self) -> None:
        sink_started = asyncio.Event()
        release_sink = asyncio.Event()

        async def blocked_nonterminal_sink(event: dict[str, object]) -> None:
            if event.get("event") == "fixture.started":
                sink_started.set()
                await release_sink.wait()

        supervisor, supervised, run_dir = await self._start_mode(
            "cancel-blocked-monitor",
            "success",
            event_sink=blocked_nonterminal_sink,
        )
        await asyncio.wait_for(sink_started.wait(), timeout=2)
        await asyncio.wait_for(supervised.process.wait(), timeout=2)
        await _wait_for(lambda: (run_dir / "worker_result.json").is_file())

        outcome = await asyncio.wait_for(
            supervisor.cancel("cancel-blocked-monitor", "user_requested"),
            timeout=1,
        )

        self.assertEqual(outcome.state, "cancelled")
        self.assertTrue(supervised.monitor_task.done())
        self.assertNotIn("cancel-blocked-monitor", supervisor.active_run_ids())
        self.assertEqual(self.store.read("cancel-blocked-monitor").state, "cancelled")

    async def test_cancel_does_not_hang_on_blocked_terminal_sink(self) -> None:
        terminal_sink_started = asyncio.Event()
        release_sink = asyncio.Event()

        async def blocked_terminal_sink(event: dict[str, object]) -> None:
            if event.get("event") == "run.cancelled":
                terminal_sink_started.set()
                await release_sink.wait()

        supervisor, _, run_dir = await self._start_mode(
            "cancel-blocked-terminal-sink",
            "blocked",
            event_sink=blocked_terminal_sink,
        )

        outcome = await asyncio.wait_for(
            supervisor.cancel("cancel-blocked-terminal-sink", "user_requested"),
            timeout=3,
        )

        self.assertTrue(terminal_sink_started.is_set())
        self.assertEqual(outcome.state, "cancelled")
        terminal = [
            event for event in read_jsonl_events(run_dir / "run_events.jsonl")
            if event.get("event") == "run.cancelled"
        ]
        self.assertEqual(len(terminal), 1)

    async def test_cancelling_recover_does_not_mutate_or_release_live_record(self) -> None:
        run_id = "recover-caller-cancelled"
        self._queued(run_id)
        queued = self.store.read(run_id)
        self.store.transition(run_id, queued, "running")
        supervisor = self._supervisor()
        monitor_task = asyncio.create_task(asyncio.Event().wait())
        supervisor._active[run_id] = SimpleNamespace(
            process=SimpleNamespace(returncode=0),
            monitor_task=monitor_task,
        )
        recovery = asyncio.create_task(supervisor.recover(run_id))
        await asyncio.sleep(0.02)

        recovery.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await recovery

        self.assertEqual(self.store.read(run_id).state, "running")
        self.assertIn(run_id, supervisor.active_run_ids())
        monitor_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await monitor_task
        supervisor._active.pop(run_id, None)

    async def test_cancelled_terminal_broadcast_can_be_replayed_on_recovery(self) -> None:
        run_id = "cancelled-terminal-broadcast"
        self._queued(run_id)
        queued = self.store.read(run_id)
        running = self.store.transition(run_id, queued, "running")
        self.store.transition(run_id, running, "completing")
        sink_started = asyncio.Event()
        release_sink = asyncio.Event()

        async def blocked_sink(_event: dict[str, object]) -> None:
            sink_started.set()
            await release_sink.wait()

        supervisor = self._supervisor(event_sink=blocked_sink)
        completion = asyncio.create_task(
            supervisor.accept_completion(
                run_id,
                terminal_state="completed",
                publishable=True,
                result_digest="digest",
                terminal_event_id="cancelled-broadcast-event",
            )
        )
        await asyncio.wait_for(sink_started.wait(), timeout=2)
        completion.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await completion

        delivered: list[str] = []
        supervisor._event_sink = lambda event: delivered.append(str(event["event_id"]))
        await supervisor.recover(run_id)

        self.assertEqual(delivered, ["cancelled-broadcast-event"])

    async def test_cancel_event_failure_keeps_frozen_terminal_truth_and_recovery_appends_event(self) -> None:
        run_id = "cancel-append-recovery"
        supervisor, _, run_dir = await self._start_mode(run_id, "blocked")

        with patch(
            "autodesign.run_supervisor.append_jsonl_event",
            side_effect=OSError("disk append failed"),
        ):
            with self.assertRaises(OSError):
                await supervisor.cancel(run_id, "user_requested")

        self.assertNotIn(run_id, supervisor.active_run_ids())
        accepted = self.store.read(run_id)
        self.assertEqual(accepted.state, "cancelled")
        self.assertTrue(accepted.writes_frozen)
        self.assertTrue((run_dir / "cancel_snapshot.json").is_file())
        self.assertFalse(any(
            event.get("event") == "run.cancelled"
            for event in read_jsonl_events(run_dir / "run_events.jsonl")
        ))

        delivered: list[str] = []
        restarted = self._supervisor(
            event_sink=lambda event: delivered.append(str(event["event_id"])),
        )
        await restarted.recover(run_id)
        terminal = [
            event for event in read_jsonl_events(run_dir / "run_events.jsonl")
            if event.get("event") == "run.cancelled"
        ]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(
            terminal[0]["event_id"],
            self.store.read(run_id).accepted_terminal_event_id,
        )
        self.assertEqual(delivered, [terminal[0]["event_id"]])
        frozen = {
            path.relative_to(run_dir): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in run_dir.rglob("*")
            if path.is_file()
        }

        reconciliation_calls: list[TerminalReconciliation] = []
        repeated_supervisor = self._supervisor(
            terminal_reconciler=reconciliation_calls.append,
        )
        await repeated_supervisor.recover(run_id)
        repeated = await repeated_supervisor.cancel(run_id, "user_requested")

        self.assertTrue(repeated.already_terminal)
        self.assertEqual(reconciliation_calls, [])
        self.assertEqual(
            {
                path.relative_to(run_dir): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in run_dir.rglob("*")
                if path.is_file()
            },
            frozen,
        )

    async def test_worker_event_relay_deduplicates_and_ignores_truncated_tail(self) -> None:
        supervisor, _, run_dir = await self._start_mode("event-relay", "event_stream")
        events_path = run_dir / "run_events.jsonl"
        await _wait_for(lambda: events_path.exists() and "event-1" in events_path.read_text())

        await supervisor.cancel("event-relay", "user_requested")
        events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
        relayed = [event for event in events if event.get("source_event_id") == "event-1"]

        self.assertEqual(len(relayed), 1)
        self.assertTrue(relayed[0]["event_id"])
        self.assertIsInstance(relayed[0]["seq"], int)
        self.assertEqual(
            [event["seq"] for event in events],
            sorted({event["seq"] for event in events}),
        )

    async def test_worker_event_relay_linearizes_with_cancel_and_drops_late_events(self) -> None:
        run_id = "event-relay-cancel-race"
        self._queued(run_id)
        queued = self.store.read(run_id)
        self.store.transition(run_id, queued, "running")
        supervisor = self._supervisor()
        run_dir = self.runs_dir / run_id
        worker_events = run_dir / "worker_events.jsonl"
        run_events = run_dir / "run_events.jsonl"
        process = SimpleNamespace(returncode=None)
        append_jsonl_event(
            worker_events,
            {"run_id": run_id, "event": "worker.before_cancel"},
            event_id="worker-before-cancel",
        )
        relay = asyncio.create_task(supervisor._relay_worker_events(run_id, process))
        await _wait_for(lambda: any(
            event.get("source_event_id") == "worker-before-cancel"
            for event in read_jsonl_events(run_events)
        ))
        relay_gate = supervisor._event_relay_locks.setdefault(run_id, asyncio.Lock())
        await relay_gate.acquire()
        append_jsonl_event(
            worker_events,
            {"run_id": run_id, "event": "worker.after_cancel"},
            event_id="worker-after-cancel",
        )
        cancellation = asyncio.create_task(
            supervisor.cancel(run_id, "user_requested"),
        )
        await asyncio.sleep(0)
        relay_gate.release()

        outcome = await cancellation
        process.returncode = 0
        await relay

        source_ids = {
            event.get("source_event_id") for event in read_jsonl_events(run_events)
        }
        self.assertEqual(outcome.state, "cancelled")
        self.assertIn("worker-before-cancel", source_ids)
        self.assertNotIn("worker-after-cancel", source_ids)
        self.assertEqual(self.store.read(run_id).state, "cancelled")

    async def test_result_discriminator_mismatch_is_rejected(self) -> None:
        supervisor, _, _ = await self._start_mode("bad-result", "mismatched_result")

        outcome = await supervisor.wait("bad-result")

        self.assertFalse(outcome.ok)
        self.assertIn("job_kind", outcome.error or "")

    async def test_event_append_recovers_truncated_tail_and_deduplicates_event_id(self) -> None:
        path = self.root / "events.jsonl"
        first = append_jsonl_event(path, {"event": "one"}, event_id="stable-one")
        with path.open("ab") as handle:
            handle.write(b'{"event":"truncated"')
        duplicate = append_jsonl_event(path, {"event": "changed"}, event_id="stable-one")
        second = append_jsonl_event(path, {"event": "two"}, event_id="stable-two")

        events = read_jsonl_events(path)
        self.assertEqual(duplicate, first)
        self.assertEqual([event["event_id"] for event in events], ["stable-one", "stable-two"])
        self.assertEqual([event["seq"] for event in events], [1, 2])
        self.assertEqual(second["seq"], 2)

    async def test_recover_classifies_live_running_worker_as_interrupted_failure(self) -> None:
        first, supervised, _ = await self._start_mode("recover-live", "blocked")
        identity = process_identity(supervised.process.pid)
        recovered_supervisor = self._supervisor()

        recovered = await recovered_supervisor.recover("recover-live")

        self.assertIsNone(recovered)
        record = self.store.read("recover-live")
        self.assertEqual(record.state, "failed")
        self.assertEqual(record.cancellation_pending, "supervisor_restart_interrupted")
        self.assertFalse(process_is_alive(identity))
        worker_outcome = await first.wait("recover-live")
        self.assertFalse(worker_outcome.ok)
        terminal = [
            event for event in read_jsonl_events(
                self.runs_dir / "recover-live" / "run_events.jsonl"
            )
            if event.get("event") == "run.error"
        ]
        self.assertEqual(len(terminal), 1)

    async def test_recover_cancelling_run_with_missing_ledger_does_not_confirm_live_worker(
        self,
    ) -> None:
        run_id = "recover-cancelling-missing-ledger"
        _, supervised, run_dir = await self._start_mode(run_id, "blocked")
        identity = process_identity(supervised.process.pid)
        running = self.store.read(run_id)
        self.assertEqual(running.worker_pid, identity.pid)
        self.assertEqual(running.worker_pgid, identity.process_group_id)
        self.assertEqual(running.worker_birth_id, identity.birth_id)
        self.assertIsNotNone(running.worker_spawn_nonce)
        owner_nonce = running.worker_spawn_nonce

        cancelling = self.store.request_cancel(run_id)
        self.assertEqual(cancelling.state, "cancelling")
        ledger_path = run_dir / "process_ledger.json"
        ledger_path.unlink()
        self.assertFalse(ledger_path.exists())
        restarted = self._supervisor()

        try:
            await restarted.recover(run_id)

            recovered = self.store.read(run_id)
            worker_alive = process_is_alive(identity)
            safe_outcome = (
                recovered.state == "cancelled" and not worker_alive
            ) or (
                recovered.state == "cancelling"
                and recovered.cancellation_pending is not None
            )
            self.assertTrue(
                safe_outcome,
                {
                    "state": recovered.state,
                    "writes_frozen": recovered.writes_frozen,
                    "cancellation_pending": recovered.cancellation_pending,
                    "worker_alive": worker_alive,
                },
            )
            if recovered.state == "cancelled":
                repeated = await restarted.cancel(run_id, "repeated_cancel")
                self.assertTrue(repeated.quiesced)
                self.assertEqual(repeated.state, "cancelled")
        finally:
            if process_is_alive(identity):
                await asyncio.to_thread(
                    terminate_process_identities,
                    (identity,),
                    root_pid=identity.pid,
                    grace_s=0.05,
                    owner_nonces=(owner_nonce,) if owner_nonce is not None else (),
                )
            await asyncio.wait_for(supervised.process.wait(), timeout=2.0)

    async def test_completion_states_use_existing_web_terminal_event_names(self) -> None:
        for terminal_state, expected_event in (
            ("completed", "run.done"),
            ("failed", "run.error"),
        ):
            with self.subTest(terminal_state=terminal_state):
                run_id = f"terminal-name-{terminal_state}"
                supervisor, _, run_dir = await self._start_mode(run_id, "success")
                await supervisor.wait(run_id)

                record = await supervisor.accept_completion(
                    run_id,
                    terminal_state=terminal_state,
                    publishable=terminal_state == "completed",
                    result_digest=f"digest-{terminal_state}",
                )

                self.assertEqual(record.terminal_event, expected_event)
                terminal_events = [
                    event
                    for event in read_jsonl_events(run_dir / "run_events.jsonl")
                    if event.get("event") in {"run.done", "run.error"}
                ]
                self.assertEqual(
                    [event.get("event") for event in terminal_events],
                    [expected_event],
                )

    async def test_recover_preserves_completed_worker_result_for_completion_coordinator(self) -> None:
        first, supervised, run_dir = await self._start_mode("recover-completing", "success")
        worker_outcome = await first.wait("recover-completing")
        self.assertTrue(worker_outcome.ok)
        self.assertIsNotNone(supervised.process.returncode)
        self.assertEqual(self.store.read("recover-completing").state, "completing")
        self.assertTrue((run_dir / "worker_result.json").is_file())

        recovered = await self._supervisor().recover("recover-completing")

        self.assertIsNone(recovered)
        self.assertEqual(self.store.read("recover-completing").state, "completing")

    async def test_terminal_cas_without_append_is_recovered_once(self) -> None:
        supervisor, _, run_dir = await self._start_mode("recover-terminal", "success")
        await supervisor.wait("recover-terminal")
        completing = self.store.read("recover-terminal")
        completed = self.store.transition(
            "recover-terminal", completing, "completed",
            accepted_terminal_event_id="accepted-terminal-id", publishable=True,
            result_digest="digest",
        )
        events_path = run_dir / "run_events.jsonl"
        before = events_path.read_bytes() if events_path.exists() else b""

        await self._supervisor().recover("recover-terminal")
        await self._supervisor().recover("recover-terminal")
        events = read_jsonl_events(events_path)

        self.assertNotEqual(events_path.read_bytes(), before)
        self.assertEqual(
            len([event for event in events if event.get("event_id") == completed.accepted_terminal_event_id]),
            1,
        )

    async def test_cancellation_wins_after_worker_enters_completing(self) -> None:
        supervisor, _, _ = await self._start_mode("cancel-completing", "success")
        await supervisor.wait("cancel-completing")
        self.assertEqual(self.store.read("cancel-completing").state, "completing")

        outcome = await supervisor.cancel("cancel-completing", "user_requested")

        self.assertEqual(outcome.state, "cancelled")
        with self.assertRaises(InvalidRunTransition):
            await supervisor.accept_completion(
                "cancel-completing", terminal_state="completed", publishable=True,
                result_digest="late", terminal_event_id="late",
            )

    async def test_worker_terminal_events_are_never_relayed(self) -> None:
        supervisor, _, run_dir = await self._start_mode("no-worker-terminal", "terminal_event")
        await _wait_for(lambda: (run_dir / "worker_events.jsonl").exists())
        await asyncio.sleep(0.08)

        await supervisor.cancel("no-worker-terminal", "user_requested")
        events = read_jsonl_events(run_dir / "run_events.jsonl")

        self.assertFalse(any(event.get("source_event_id") == "forbidden-worker-terminal" for event in events))

    async def test_nested_shim_eof_before_ack_never_launches_target(self) -> None:
        run_dir = self.runs_dir / "nested-eof"
        marker = run_dir / "target-launched.txt"
        ledger = ProcessLedger(run_dir)

        def abort_before_registration(_identity: ProcessIdentity) -> None:
            raise RuntimeError("worker crashed before shim acknowledgement")

        with self.assertRaises(RuntimeError):
            await asyncio.to_thread(
                spawn_registered_process,
                ledger,
                [sys.executable, str(FIXTURE), "--write-marker", str(marker)],
                role="nested-eof",
                registration_hook=abort_before_registration,
            )

        self.assertFalse(marker.exists())
        snapshot = ledger.read()
        self.assertTrue(any(intent.status == "failed" for intent in snapshot.spawning))

    async def test_run_control_rejects_existing_symlink_escape(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        (self.runs_dir / "linked-run").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(RunControlError):
            self.store.reserve("linked-run", "poster")

        self.assertFalse((outside / "run_control.json").exists())

    async def test_nested_cancel_at_unacked_shim_cannot_orphan_target(self) -> None:
        run_dir = self.runs_dir / "nested-unacked"
        ledger = ProcessLedger(run_dir)
        hook_entered = threading.Event()
        release_hook = threading.Event()

        def registration_barrier(_identity: ProcessIdentity) -> None:
            hook_entered.set()
            if not release_hook.wait(timeout=3):
                raise RuntimeError("test registration barrier timed out")

        spawn_task = asyncio.create_task(asyncio.to_thread(
            spawn_registered_process,
            ledger,
            [sys.executable, str(FIXTURE), "--child-loop"],
            role="nested-unacked",
            start_new_session=True,
            registration_hook=registration_barrier,
        ))
        await _wait_for(hook_entered.is_set)

        def seal_and_terminate() -> TerminationReport:
            with ledger.exclusive():
                snapshot = ledger.seal_unlocked()
                identities = tuple(record.identity for record in snapshot.processes)
                unresolved = tuple(
                    intent.nonce for intent in snapshot.spawning if intent.status == "spawning"
                )
            return terminate_process_identities(
                identities, root_pid=None, grace_s=0.1, unresolved_spawns=unresolved,
            )

        cancel_task = asyncio.create_task(asyncio.to_thread(seal_and_terminate))
        release_hook.set()
        process = await spawn_task
        report = await cancel_task

        identity = next(record.identity for record in ledger.read().processes)
        self.assertFalse(process_is_alive(identity))
        self.assertEqual(report.survivors, ())
        process.wait(timeout=2)

    async def test_spawn_shim_preserves_target_stdin(self) -> None:
        run_dir = self.runs_dir / "nested-stdin"
        marker = run_dir / "stdin.txt"
        process = await asyncio.to_thread(
            spawn_registered_process,
            ProcessLedger(run_dir),
            [sys.executable, str(FIXTURE), "--stdin-marker", str(marker)],
            role="nested-stdin",
            stdin=subprocess.PIPE,
        )
        self.assertIsNotNone(process.stdin)
        process.stdin.write(b"target-stdin-payload")
        process.stdin.close()
        await asyncio.to_thread(process.wait, 2)

        self.assertEqual(marker.read_bytes(), b"target-stdin-payload")

    async def test_terminal_append_before_failed_broadcast_recovers_delivery(self) -> None:
        def fail_broadcast(_event: dict[str, object]) -> None:
            raise RuntimeError("simulated broadcaster crash")

        run_id = "append-before-broadcast"
        self._queued(run_id)
        supervisor = RunSupervisor(
            self.runs_dir,
            control_store=self.store,
            worker_command=(sys.executable, str(FIXTURE)),
            event_sink=fail_broadcast,
        )
        self.supervisors.append(supervisor)
        await supervisor.start(self._request(run_id, "success"))
        await supervisor.wait(run_id)
        accepted = await supervisor.accept_completion(
            run_id, terminal_state="completed", publishable=True,
            result_digest="digest", terminal_event_id="broadcast-recovery-id",
        )
        persisted = read_jsonl_events(self.runs_dir / run_id / "run_events.jsonl")
        self.assertEqual(
            len([event for event in persisted if event.get("event_id") == accepted.accepted_terminal_event_id]),
            1,
        )

        delivered: list[dict[str, object]] = []
        recovering = RunSupervisor(
            self.runs_dir, control_store=self.store, event_sink=delivered.append,
        )
        self.supervisors.append(recovering)
        await recovering.recover(run_id)

        self.assertEqual([event["event_id"] for event in delivered], ["broadcast-recovery-id"])

    async def test_failed_async_terminal_broadcast_recovers_on_same_supervisor(self) -> None:
        attempts = 0
        delivered: list[str] = []

        async def flaky(event: dict[str, object]) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary sink failure")
            delivered.append(str(event["event_id"]))

        run_id = "same-supervisor-broadcast-recovery"
        self._queued(run_id)
        supervisor = self._supervisor(event_sink=flaky)
        await supervisor.start(self._request(run_id, "success"))
        await supervisor.wait(run_id)
        await supervisor.accept_completion(
            run_id, terminal_state="completed", publishable=True,
            result_digest="digest", terminal_event_id="same-supervisor-event",
        )
        await supervisor.recover(run_id)

        self.assertEqual(attempts, 2)
        self.assertEqual(delivered, ["same-supervisor-event"])

    async def test_relay_event_id_is_stable_across_supervisor_restart(self) -> None:
        run_id = "stable-relay-id"
        self._queued(run_id)
        queued = self.store.read(run_id)
        self.store.transition(run_id, queued, "running")
        run_dir = self.runs_dir / run_id
        append_jsonl_event(
            run_dir / "worker_events.jsonl",
            {"run_id": run_id, "event": "fixture.one"},
            event_id="source-stable",
        )

        class Exited:
            returncode = 0

        await self._supervisor()._relay_worker_events(run_id, Exited())
        await self._supervisor()._relay_worker_events(run_id, Exited())
        relayed = [
            event for event in read_jsonl_events(run_dir / "run_events.jsonl")
            if event.get("source_event_id") == "source-stable"
        ]
        self.assertEqual(len(relayed), 1)

    async def test_relay_rejects_foreign_source_run_id(self) -> None:
        run_id = "relay-authority"
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True)
        append_jsonl_event(
            run_dir / "worker_events.jsonl",
            {"run_id": "foreign-run", "event": "fixture.foreign"},
            event_id="foreign-event",
        )

        class Exited:
            returncode = 0

        await self._supervisor()._relay_worker_events(run_id, Exited())
        events = read_jsonl_events(run_dir / "run_events.jsonl")
        self.assertFalse(any(event.get("source_event_id") == "foreign-event" for event in events))

    async def test_cross_process_event_append_retains_every_unique_event(self) -> None:
        path = self.root / "multiprocess-events.jsonl"
        code = (
            "import sys; from autodesign.util.logging import append_jsonl_event; "
            "append_jsonl_event(sys.argv[1], {'event':'parallel','writer':int(sys.argv[2])}, "
            "event_id='writer-'+sys.argv[2])"
        )
        processes = [
            await asyncio.create_subprocess_exec(
                sys.executable, "-c", code, str(path), str(index),
                cwd=str(Path(__file__).resolve().parents[1]),
            )
            for index in range(16)
        ]
        self.assertEqual([await process.wait() for process in processes], [0] * 16)
        events = read_jsonl_events(path)
        self.assertEqual(len(events), 16)
        self.assertEqual(len({event["event_id"] for event in events}), 16)
        self.assertEqual([event["seq"] for event in events], list(range(1, 17)))

    async def test_worker_terminal_event_is_suppressed_before_stderr(self) -> None:
        from autodesign.util.logging import log, worker_run_context

        stream = StringIO()
        with redirect_stderr(stream), worker_run_context("terminal-stderr", self.root):
            log("run.done", secret="must-not-print")
        self.assertEqual(stream.getvalue(), "")

    async def test_worker_environment_excludes_credentialed_proxy_and_generic_secrets(self) -> None:
        from autodesign.run_supervisor import build_worker_environment
        from autodesign.run_worker import _scrub_secret_environment

        sentinels = {
            "HTTPS_PROXY": "http://proxy-user:proxy-pass@example.invalid:8080",
            "DATABASE_URL": "postgres://db-user:db-pass@example.invalid/db",
            "AWS_SECRET_ACCESS_KEY": "aws-secret-sentinel",
        }
        with patch.dict(os.environ, sentinels, clear=False):
            worker_env = build_worker_environment()
            _scrub_secret_environment()
            self.assertNotIn("HTTPS_PROXY", worker_env)
            self.assertNotIn("DATABASE_URL", os.environ)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", os.environ)

    async def test_scheme_less_and_encoded_proxy_credentials_never_reach_live_worker(self) -> None:
        code = r'''
import asyncio, json, sys
from pathlib import Path
from autodesign.config import Settings
from autodesign.run_control import RunControlStore
from autodesign.run_supervisor import RunSupervisor, build_worker_environment
from autodesign.run_worker_protocol import PipelineWorkerRequest
async def main():
    runs_dir, fixture, repo = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    store = RunControlStore(runs_dir)
    reserved = store.reserve("captured-proxy-env", "poster")
    store.transition("captured-proxy-env", reserved, "queued")
    request = PipelineWorkerRequest(
        job_kind="pipeline", run_id="captured-proxy-env", brief="success", attachments=(),
        template=None, palette_id=None, resume_run=None, reference_poster=None,
        settings=Settings(
            anthropic_api_key="", anthropic_base_url=None, gemini_api_key="",
            designer_model="designer", critic_model="critic",
            repo_root=repo, out_dir=runs_dir.parent,
        ),
    )
    supervisor = RunSupervisor(
        runs_dir, control_store=store, worker_command=(sys.executable, str(fixture)),
    )
    await supervisor.start(request)
    await supervisor.wait("captured-proxy-env")
    observation = json.loads((runs_dir / "captured-proxy-env" / "fixture_observation.json").read_text())
    print(json.dumps({"built": build_worker_environment(), "observation": observation}))
asyncio.run(main())
'''
        environment = dict(os.environ)
        environment["HTTPS_PROXY"] = "proxy-user:proxy-pass@example.invalid:8080"
        environment["http_proxy"] = "user%40name:p%40ss@example.invalid:8081"
        completed = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code, str(self.runs_dir), str(FIXTURE),
            str(Path(__file__).resolve().parents[1]),
            cwd=str(Path(__file__).resolve().parents[1]), env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await completed.communicate()
        self.assertEqual(completed.returncode, 0, stderr.decode(errors="replace"))
        payload = json.loads(stdout)
        self.assertNotIn("HTTPS_PROXY", payload["built"])
        self.assertNotIn("http_proxy", payload["built"])
        self.assertEqual(payload["observation"]["proxy_env"], {})

    async def test_hard_crash_spawn_intent_is_durably_reconciled(self) -> None:
        run_dir = self.runs_dir / "hard-crash-intent"
        marker = run_dir / "must-not-launch"
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(FIXTURE), "--hard-crash-spawn", str(run_dir), str(marker),
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        self.assertEqual(await process.wait(), 86)
        ledger = ProcessLedger(run_dir)
        snapshot = ledger.read()
        active = [intent for intent in snapshot.spawning if intent.status == "spawning"]
        self.assertEqual(len(active), 1)
        self.assertIsNotNone(active[0].shim_identity)

        report = await asyncio.to_thread(ledger.reconcile_abandoned_spawns, grace_s=0.05)
        self.assertEqual(report.survivors, ())
        self.assertFalse(marker.exists())
        self.assertFalse(any(intent.status == "spawning" for intent in ledger.read().spawning))

    async def test_nested_post_popen_pre_identity_crash_closes_intent_without_guess_kill(self) -> None:
        run_dir = self.runs_dir / "nested-post-popen-crash"
        pid_path = run_dir / "shim.pid"
        marker = run_dir / "target-launched"
        owner = await asyncio.create_subprocess_exec(
            sys.executable, str(FIXTURE), "--post-popen-crash-spawn",
            str(run_dir), str(pid_path), str(marker),
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        self.assertEqual(await owner.wait(), 88)
        shim_pid = int(pid_path.read_text())
        ledger = ProcessLedger(run_dir)
        active = [intent for intent in ledger.read().spawning if intent.status == "spawning"]
        self.assertEqual(len(active), 1)
        self.assertIsNone(active[0].shim_identity)
        self.assertEqual(active[0].shim_pid, shim_pid)

        report = await asyncio.to_thread(ledger.reconcile_abandoned_spawns, grace_s=0.1)
        self.assertEqual(report.survivors, ())
        self.assertEqual(report.unresolved_spawns, ())
        self.assertFalse(marker.exists())
        await _wait_for(lambda: _pid_is_missing(shim_pid), timeout=2.0)
        self.assertFalse(any(intent.status == "spawning" for intent in ledger.read().spawning))

    async def test_root_post_popen_pre_identity_crash_closes_intent_without_guess_kill(self) -> None:
        run_id = "root-post-popen-crash"
        run_dir = self.runs_dir / run_id
        pid_path = run_dir / "worker.pid"
        owner = await asyncio.create_subprocess_exec(
            sys.executable, str(FIXTURE), "--root-post-popen-crash",
            str(self.runs_dir), run_id, str(pid_path),
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        self.assertEqual(await owner.wait(), 89)
        worker_pid = int(pid_path.read_text())
        ledger = ProcessLedger(run_dir)
        active = [intent for intent in ledger.read().spawning if intent.status == "spawning"]
        self.assertEqual(len(active), 1)
        self.assertIsNone(active[0].shim_identity)
        self.assertEqual(active[0].shim_pid, worker_pid)

        report = await asyncio.to_thread(ledger.reconcile_abandoned_spawns, grace_s=0.1)
        self.assertEqual(report.survivors, ())
        self.assertEqual(report.unresolved_spawns, ())
        self.assertFalse((run_dir / "fixture_observation.json").exists())
        await _wait_for(lambda: _pid_is_missing(worker_pid), timeout=2.0)
        self.assertFalse(any(intent.status == "spawning" for intent in ledger.read().spawning))

    async def test_wedged_shim_identity_handshake_times_out_and_is_reaped(self) -> None:
        run_dir = self.runs_dir / "wedged-shim"
        ledger = ProcessLedger(run_dir)
        started = time.monotonic()
        with self.assertRaises(Exception):
            await asyncio.wait_for(
                asyncio.to_thread(
                    spawn_registered_process,
                    ledger,
                    [sys.executable, str(FIXTURE), "--child-loop"],
                    role="wedged-shim",
                    shim_command=[sys.executable, str(FIXTURE), "--child-loop"],
                    handshake_timeout_s=0.15,
                ),
                timeout=1.0,
            )
        self.assertLess(time.monotonic() - started, 1.0)
        report = await asyncio.to_thread(ledger.reconcile_abandoned_spawns, grace_s=0.05)
        self.assertEqual(report.survivors, ())

    async def test_root_spawn_lock_wait_does_not_block_event_loop(self) -> None:
        run_id = "async-root-lock"
        self._queued(run_id)
        run_dir = self.runs_dir / run_id
        marker = run_dir / "lock-held"
        holder = await asyncio.create_subprocess_exec(
            sys.executable, str(FIXTURE), "--hold-ledger-lock",
            str(run_dir), str(marker), "0.35",
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        await _wait_for(marker.exists)
        supervisor = self._supervisor()
        start_task = asyncio.create_task(supervisor.start(self._request(run_id, "success")))
        tick_started = time.monotonic()
        await asyncio.sleep(0.05)
        self.assertLess(time.monotonic() - tick_started, 0.2)
        self.assertEqual(await holder.wait(), 0)
        await start_task
        await supervisor.wait(run_id)

    async def test_cross_process_spawn_lock_race_cannot_release_after_cancel(self) -> None:
        supervisor, _, run_dir = await self._start_mode(
            "cross-process-registration-race", "spawn_registration_barrier"
        )
        await _wait_for(lambda: (run_dir / "registration-entered").exists())
        cancel_task = asyncio.create_task(supervisor.cancel("cross-process-registration-race", "race"))
        await asyncio.sleep(0.05)
        self.assertFalse(cancel_task.done())
        (run_dir / "registration-release").write_text("release", encoding="utf-8")
        outcome = await cancel_task
        self.assertEqual(outcome.state, "cancelled")
        self.assertEqual(outcome.surviving_pids, ())

    async def test_birth_identity_uses_high_resolution_platform_source(self) -> None:
        identity = process_identity(os.getpid())
        if sys.platform.startswith("linux"):
            self.assertTrue(identity.birth_id.startswith("linux-proc:"), identity.birth_id)
        elif sys.platform == "darwin":
            self.assertTrue(identity.birth_id.startswith("darwin-proc:"), identity.birth_id)

    async def test_linux_proc_birth_parser_handles_spaces_and_start_ticks(self) -> None:
        from autodesign.process_supervision import _parse_linux_proc_stat

        raw = "4321 (worker name with spaces) S 12 34 34 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 987654 0"
        identity = _parse_linux_proc_stat(4321, raw)
        self.assertEqual(
            identity,
            ProcessIdentity(4321, "linux-proc:987654", 34, 12),
        )

    async def test_windows_spawn_path_never_uses_global_inheritable_handles(self) -> None:
        source = inspect.getsource(spawn_registered_process)
        self.assertNotIn("close_fds=False", source)
        self.assertNotIn("os.set_inheritable", source)

    async def test_darwin_owner_scan_fails_closed_when_ps_fails(self) -> None:
        from autodesign.process_supervision import _process_identities_with_owner_nonces

        completed = subprocess.CompletedProcess(
            ["/bin/ps", "eww", "-axo", "pid=,command="],
            1,
            stdout="",
            stderr="ps failed",
        )
        with patch("autodesign.process_supervision.sys.platform", "darwin"), patch(
            "autodesign.process_supervision.subprocess.run",
            return_value=completed,
        ) as run:
            with self.assertRaisesRegex(ProcessLedgerError, "owner process scan failed"):
                _process_identities_with_owner_nonces(("owner-nonce",))
        self.assertEqual(run.call_args.args[0][0], "/bin/ps")

    async def test_linux_owner_scan_fails_closed_when_proc_is_unavailable(self) -> None:
        from autodesign.process_supervision import _process_identities_with_owner_nonces

        with patch("autodesign.process_supervision.sys.platform", "linux"), patch(
            "autodesign.process_supervision.Path.iterdir",
            side_effect=PermissionError("proc denied"),
        ):
            with self.assertRaisesRegex(ProcessLedgerError, "owner process scan failed"):
                _process_identities_with_owner_nonces(("owner-nonce",))

    async def test_windows_ctypes_api_signatures_are_explicit(self) -> None:
        import autodesign.process_supervision as supervision

        configure = getattr(supervision, "_configure_windows_api", None)
        self.assertTrue(callable(configure))

        class Function:
            def __call__(self, *_args):
                return 1

        class Library:
            pass

        kernel32 = Library()
        for name in (
            "CreateJobObjectW", "OpenProcess", "SetInformationJobObject",
            "AssignProcessToJobObject", "TerminateJobObject", "CloseHandle",
            "GetProcessTimes",
        ):
            setattr(kernel32, name, Function())
        ntdll = Library()
        ntdll.NtResumeProcess = Function()
        configure(kernel32, ntdll)
        for name in (
            "SetInformationJobObject", "AssignProcessToJobObject", "TerminateJobObject",
            "CloseHandle", "GetProcessTimes",
        ):
            function = getattr(kernel32, name)
            self.assertTrue(function.argtypes, name)
            self.assertIsNotNone(function.restype, name)
        self.assertTrue(ntdll.NtResumeProcess.argtypes)
        self.assertIsNotNone(ntdll.NtResumeProcess.restype)

    async def test_dispatch_routes_all_five_variants_without_ad_hoc_fallback(self) -> None:
        from autodesign.run_worker import _SignalCancellation, _dispatch

        cancellation = _SignalCancellation(threading.Event())
        routes = {
            "pipeline": "_run_pipeline",
            "editable_video_render": "_run_editable_video_render",
            "poster_code_edit": "_run_poster_code_edit",
            "pptx_export": "_run_pptx_export",
            "video_export_retry": "_run_video_export_retry",
        }
        for kind, helper in routes.items():
            with self.subTest(kind=kind), patch(
                f"autodesign.run_worker.{helper}", return_value={"route": kind},
            ) as called:
                result = _dispatch(SimpleNamespace(job_kind=kind), cancellation)
                self.assertEqual(result, {"route": kind})
                called.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "Windows Job Object runtime verification")
    async def test_windows_nested_spawn_is_owned_without_handle_leak(self) -> None:
        run_dir = self.runs_dir / "windows-owned"
        process = await asyncio.to_thread(
            spawn_registered_process, ProcessLedger(run_dir),
            [sys.executable, str(FIXTURE), "--child-loop"], role="windows-owned",
            handshake_timeout_s=1.0,
        )
        report = await asyncio.to_thread(
            terminate_process_identities,
            tuple(record.identity for record in ProcessLedger(run_dir).read().processes),
            root_pid=process.pid, grace_s=0.1,
        )
        self.assertEqual(report.survivors, ())


if __name__ == "__main__":
    unittest.main()
