from __future__ import annotations

import errno
import itertools
import json
import os
from concurrent.futures import ThreadPoolExecutor
import threading
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import autodesign.run_control as run_control
from autodesign.run_control import (
    InvalidRunTransition,
    RunControlError,
    RunControlStore,
    RunWritesFrozen,
)
from autodesign.util.io import sha256_file


class RunControlStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self._tmp.name) / "runs"
        self.store = RunControlStore(self.runs_dir)
        self.run_id = "run-control-test"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _reserve_running(self):
        reserved = self.store.reserve(self.run_id, "poster")
        queued = self.store.transition(self.run_id, reserved, "queued")
        return self.store.transition(self.run_id, queued, "running")

    def _finalize_cancel(self):
        self._reserve_running()
        self.store.request_cancel(self.run_id)
        return self.store.finalize_cancel(
            self.run_id,
            {
                "reason": "user_requested",
                "termination_verified": True,
                "last_phase": "authoring",
            },
        )

    def test_reserve_keeps_reserved_default_and_supports_atomic_queued_state(
        self,
    ) -> None:
        reserved = self.store.reserve(self.run_id, "poster")
        queued = self.store.reserve(
            "run-control-ready-without-inputs",
            "landing",
            initial_state="queued",
        )

        self.assertEqual(reserved.state, "reserved")
        self.assertEqual(reserved.revision, 0)
        self.assertEqual(queued.state, "queued")
        self.assertEqual(queued.revision, 0)

    def test_cancelled_is_terminal_and_late_completion_is_rejected(self) -> None:
        cancelled = self._finalize_cancel()

        self.assertEqual(cancelled.state, "cancelled")
        self.assertTrue(cancelled.writes_frozen)
        self.assertEqual(cancelled.terminal_event, "run.cancelled")
        self.assertIsNotNone(cancelled.accepted_terminal_event_id)
        self.assertIsNotNone(cancelled.terminal_at)
        with self.assertRaises(InvalidRunTransition):
            self.store.transition(self.run_id, cancelled, "completed")

        def late_completion() -> type[BaseException] | None:
            try:
                self.store.transition(self.run_id, cancelled, "completed")
            except InvalidRunTransition as exc:
                return type(exc)
            return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: late_completion(), range(2)))
        self.assertEqual(outcomes, [InvalidRunTransition, InvalidRunTransition])
        self.assertEqual(self.store.read(self.run_id), cancelled)

    def test_public_transition_cannot_bypass_verified_cancellation(self) -> None:
        self._reserve_running()
        cancelling = self.store.request_cancel(self.run_id)

        with self.assertRaises(InvalidRunTransition):
            self.store.transition(self.run_id, cancelling, "cancelled")

        self.assertEqual(self.store.read(self.run_id), cancelling)
        self.assertFalse((self.runs_dir / self.run_id / "cancel_snapshot.json").exists())

    def test_repeated_cancel_request_is_idempotent(self) -> None:
        self._reserve_running()

        first = self.store.request_cancel(self.run_id)
        second = self.store.request_cancel(self.run_id)

        self.assertEqual(first.state, "cancelling")
        self.assertEqual(second, first)
        self.assertEqual(second.cancellation_requested_at, first.cancellation_requested_at)
        self.assertEqual(second.revision, first.revision)

    def test_terminal_reconciliation_metadata_is_cas_guarded_and_survives_lifecycle_transition(self) -> None:
        running = self._reserve_running()

        pending = self.store.update_terminal_reconciliation(
            self.run_id,
            running,
            decision="accept",
            phase="preflight",
            terminal_state="completed",
            status="pending",
            diagnostic=None,
        )

        self.assertEqual(pending.terminal_reconciliation_decision, "accept")
        self.assertEqual(pending.terminal_reconciliation_phase, "preflight")
        self.assertEqual(pending.terminal_reconciliation_terminal_state, "completed")
        self.assertEqual(pending.terminal_reconciliation_status, "pending")
        with self.assertRaises(InvalidRunTransition):
            self.store.update_terminal_reconciliation(
                self.run_id,
                running,
                decision="accept",
                phase="preflight",
                terminal_state="completed",
                status="succeeded",
                diagnostic=None,
            )

        succeeded = self.store.update_terminal_reconciliation(
            self.run_id,
            pending,
            decision="accept",
            phase="preflight",
            terminal_state="completed",
            status="succeeded",
            diagnostic=None,
        )
        completing = self.store.transition(self.run_id, succeeded, "completing")
        self.assertEqual(
            completing.terminal_reconciliation_status,
            "succeeded",
        )
        self.assertEqual(
            completing.terminal_reconciliation_terminal_state,
            "completed",
        )

    def test_terminal_reconciliation_metadata_rejects_incomplete_contracts(self) -> None:
        running = self._reserve_running()

        with self.assertRaises(InvalidRunTransition):
            self.store.update_terminal_reconciliation(
                self.run_id,
                running,
                decision="accept",
                phase=None,
                terminal_state="completed",
                status="pending",
                diagnostic=None,
            )
        with self.assertRaises(InvalidRunTransition):
            self.store.update_terminal_reconciliation(
                self.run_id,
                running,
                decision=None,
                phase=None,
                terminal_state=None,
                status="invalid",
                diagnostic=None,
            )

    def test_terminal_reconciliation_metadata_rejects_every_impossible_tuple(self) -> None:
        running = self._reserve_running()
        allowed = {
            ("accept", "preflight", "completed"),
            ("accept", "commit", "completed"),
            ("reject", "preflight", "failed"),
            ("reject", "commit", "failed"),
            ("reject", "commit", "cancelled"),
        }

        for decision, phase, terminal_state in itertools.product(
            ("accept", "reject"),
            ("preflight", "commit"),
            ("completed", "failed", "cancelled"),
        ):
            if (decision, phase, terminal_state) in allowed:
                continue
            for status in ("pending", "succeeded"):
                with self.subTest(
                    decision=decision,
                    phase=phase,
                    terminal_state=terminal_state,
                    status=status,
                ):
                    with self.assertRaises(InvalidRunTransition):
                        self.store.update_terminal_reconciliation(
                            self.run_id,
                            running,
                            decision=decision,
                            phase=phase,
                            terminal_state=terminal_state,
                            status=status,
                            diagnostic=None,
                        )

    def test_generic_transition_cannot_author_terminal_reconciliation_metadata(self) -> None:
        running = self._reserve_running()

        with self.assertRaises(InvalidRunTransition):
            self.store.transition(
                self.run_id,
                running,
                "completing",
                terminal_reconciliation_decision="accept",
                terminal_reconciliation_phase="commit",
                terminal_reconciliation_terminal_state="completed",
                terminal_reconciliation_status="succeeded",
            )

        self.assertEqual(self.store.read(self.run_id), running)

    def test_terminal_reconciliation_authority_cannot_be_cleared_by_dedicated_update(self) -> None:
        running = self._reserve_running()
        pending = self.store.update_terminal_reconciliation(
            self.run_id,
            running,
            decision="accept",
            phase="preflight",
            terminal_state="completed",
            status="pending",
            diagnostic=None,
        )

        with self.assertRaises(InvalidRunTransition):
            self.store.update_terminal_reconciliation(
                self.run_id,
                pending,
                decision=None,
                phase=None,
                terminal_state=None,
                status=None,
                diagnostic=None,
            )

        succeeded = self.store.update_terminal_reconciliation(
            self.run_id,
            pending,
            decision="accept",
            phase="preflight",
            terminal_state="completed",
            status="succeeded",
            diagnostic=None,
        )
        with self.assertRaises(InvalidRunTransition):
            self.store.update_terminal_reconciliation(
                self.run_id,
                succeeded,
                decision=None,
                phase=None,
                terminal_state=None,
                status=None,
                diagnostic=None,
            )
        self.assertEqual(self.store.read(self.run_id), succeeded)

    def test_terminal_reconciliation_unhashable_fields_raise_canonical_validation_error(self) -> None:
        running = self._reserve_running()

        for field_name, value in (
            ("decision", []),
            ("phase", {}),
            ("terminal_state", []),
        ):
            values = {
                "decision": "accept",
                "phase": "preflight",
                "terminal_state": "completed",
            }
            values[field_name] = value
            with self.subTest(field_name=field_name):
                with self.assertRaises(InvalidRunTransition):
                    self.store.update_terminal_reconciliation(
                        self.run_id,
                        running,
                        **values,
                        status="pending",
                        diagnostic=None,
                    )

    def test_legacy_control_record_without_reconciliation_fields_remains_readable(self) -> None:
        reserved = self.store.reserve(self.run_id, "poster")
        control_path = self.runs_dir / self.run_id / "run_control.json"
        payload = json.loads(control_path.read_text(encoding="utf-8"))
        for key in tuple(payload):
            if key.startswith("terminal_reconciliation_"):
                payload.pop(key)
        control_path.write_text(json.dumps(payload), encoding="utf-8")

        restored = self.store.read(self.run_id)

        self.assertEqual(restored.state, reserved.state)
        self.assertIsNone(restored.terminal_reconciliation_decision)
        self.assertIsNone(restored.terminal_reconciliation_status)

    def test_unverified_process_death_keeps_cancelling(self) -> None:
        self._reserve_running()
        cancelling = self.store.request_cancel(self.run_id)

        result = self.store.finalize_cancel(
            self.run_id,
            {
                "termination_verified": False,
                "cancellation_pending": "managed_process_liveness_unverified",
            },
        )

        self.assertEqual(result.state, "cancelling")
        self.assertEqual(result.cancellation_pending, "managed_process_liveness_unverified")
        self.assertFalse(result.writes_frozen)
        self.assertGreater(result.revision, cancelling.revision)
        self.assertFalse((self.runs_dir / self.run_id / "cancel_snapshot.json").exists())
        self.assertEqual(
            self.store.finalize_cancel(
                self.run_id,
                {
                    "termination_verified": False,
                    "cancellation_pending": "managed_process_liveness_unverified",
                },
            ),
            result,
        )

    def test_only_boolean_true_verifies_termination(self) -> None:
        self._reserve_running()
        self.store.request_cancel(self.run_id)

        for value in ("false", 1):
            with self.subTest(value=value):
                result = self.store.finalize_cancel(
                    self.run_id,
                    {"termination_verified": value},
                )
                self.assertEqual(result.state, "cancelling")
                self.assertFalse(result.writes_frozen)
                self.assertFalse((self.runs_dir / self.run_id / "cancel_snapshot.json").exists())

    def test_cancel_snapshot_is_diagnostic_only(self) -> None:
        run_dir = self.runs_dir / self.run_id
        (run_dir / "final").mkdir(parents=True)
        poster = run_dir / "final" / "poster.html"
        poster.write_text("<main>partial poster</main>", encoding="utf-8")

        cancelled = self._finalize_cancel()
        snapshot_path = run_dir / "cancel_snapshot.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))

        self.assertEqual(snapshot["publishable"], False)
        self.assertEqual(snapshot["source_run_state"], "cancelled")
        self.assertEqual(snapshot["inventory"]["final/poster.html"]["sha256"], sha256_file(poster))
        self.assertFalse(cancelled.publishable)
        self.assertEqual(cancelled.cancel_snapshot_sha256, sha256_file(snapshot_path))

    def test_snapshot_inventory_hashes_every_payload_path_and_excludes_control_plane(self) -> None:
        run_dir = self.runs_dir / self.run_id
        payloads = {
            "run_brief.json": b'{"brief": "poster"}',
            "canvas_plan.json": b'{"canvas": "landscape"}',
            "paper_memory.json": b'{"chunks": []}',
            "poster_plan_contract.json": b'{"version": 1}',
            "design_spec.json": b'{"spec": 1}',
            "candidate_draft_lineage.json": b'{"candidate": "draft"}',
            "academic_identity_assets.json": b'{"assets": []}',
            "spec_recovery.json": b'{"status": "recovered"}',
            "paper_resource_recall_audit.json": b'{"audit": true}',
            "slides_trusted_source_hashes.json": b'{"slides": []}',
            "landing_trusted_source_hashes.json": b'{"landing": []}',
            "reference_style_audit.json": b'{"audit": []}',
            "reference_style_blueprint.html": b"<main>reference</main>",
            "reference_style_blueprint_preview.png": b"preview",
            "reference_style_contract.json": b'{"contract": 1}',
            "reference_style_raw_blueprint_preview.png": b"raw preview",
            "uploads/paper.pdf": b"paper bytes",
            "layers/figure.svg": b"<svg/>",
            "paper_evidence_packs/pack_001.json": b'{"claims": []}',
            "reference_poster/reference.png": b"reference bytes",
            "html_first/candidates/candidate_001/manifest.json": b'{"candidate": 1}',
            "designer_author/attempt_01/poster.html": b"<main>author</main>",
            "designer_author/attempt_01/run_events.jsonl": b'{"event":"payload"}\n',
            "openresearch/job-1/openresearch_project_result.json": b'{"status": "done"}',
            "composites/iter_01/poster.html": b"<main>composite</main>",
            "slides_author/attempt_01/slides.html": b"<main>slides</main>",
            "landing_author/attempt_01/index.html": b"<main>landing</main>",
            "video_author/attempt_01/project/index.html": b"<main>video</main>",
            "code_editor/attempt_01/edit.json": b'{"edit": 1}',
            "identity_logo_agent/attempt_01/logo.svg": b"<svg/>",
            "attempt_selection_work/selection.json": b'{"selected": 1}',
            "attempt_candidates/selection.json": b'{"selected_candidate": 1}',
            "attempt_materialization/attempt_01/input.json": b'{"input": 1}',
            "panel_polish/round_01/plan.json": b'{"round": 1}',
            "runtime_skills/packs/design/SKILL.md": b"# Skill\n",
            "specs/layout_spec.json": b'{"layout": "dense"}',
            "quarantine/palette_validation_failed/poster.html": b"<main>quarantined</main>",
            "trajectory/worker.jsonl": b'{"seq": 1}\n',
            "hyperframes-paper-video/project.json": b'{"video": true}',
            "hyperframes-editable-demo/index.html": b"<main>hyperframes</main>",
            "final/poster.html": b"<main>poster</main>",
            "pipeline_cache/text.json": b'{"text": "cached"}',
            "pptx-export/attempt_01/current.html": b"<main>slides</main>",
            "exports/deck.pptx": b"pptx bytes",
            "final/events.jsonl": b'{"event":"payload"}\n',
            "media/preview.mp4": b"generated media",
            "manifests/delivery.json": b'{"version": 1}',
        }
        for relative_path, contents in payloads.items():
            path = run_dir / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run_events.jsonl").write_text('{"event":"run.start"}\n', encoding="utf-8")
        (run_dir / "worker_events.jsonl").write_text('{"event":"worker.start"}\n', encoding="utf-8")
        (run_dir / "process_ledger.json").write_text("{}", encoding="utf-8")
        (run_dir / "new_control.json").write_text("{}", encoding="utf-8")
        (run_dir / "control").mkdir()
        (run_dir / "control" / "recovery.json").write_text("{}", encoding="utf-8")

        self._finalize_cancel()
        snapshot = json.loads((run_dir / "cancel_snapshot.json").read_text(encoding="utf-8"))
        inventory = snapshot["inventory"]

        self.assertEqual(set(inventory), set(payloads))
        for relative_path in payloads:
            path = run_dir / relative_path
            self.assertEqual(inventory[relative_path]["sha256"], sha256_file(path))
            self.assertEqual(inventory[relative_path]["size"], path.stat().st_size)
            self.assertEqual(inventory[relative_path]["mtime_ns"], path.stat().st_mtime_ns)
        self.assertNotIn("run_control.json", inventory)
        self.assertNotIn("cancel_snapshot.json", inventory)
        self.assertNotIn("process_ledger.json", inventory)
        self.assertNotIn("run_events.jsonl", inventory)
        self.assertNotIn("worker_events.jsonl", inventory)
        self.assertNotIn("new_control.json", inventory)
        self.assertNotIn("control/recovery.json", inventory)

    def test_frozen_run_rejects_new_writes(self) -> None:
        self._reserve_running()
        writable = self.store.assert_writable(self.run_id)
        self.assertEqual(writable.state, "running")

        self.store.request_cancel(self.run_id)
        with self.assertRaises(RunWritesFrozen):
            self.store.assert_writable(self.run_id)
        cancelled = self.store.finalize_cancel(
            self.run_id,
            {"termination_verified": True},
        )

        with self.assertRaises(RunWritesFrozen):
            self.store.assert_writable(self.run_id)
        self.assertTrue((self.runs_dir / self.run_id).is_dir())
        self.assertTrue(cancelled.writes_frozen)

    def test_atomic_transition_increments_revision(self) -> None:
        reserved = self.store.reserve(self.run_id, "poster", parent_job_id="bundle-1")
        queued = self.store.transition(self.run_id, reserved, "queued")
        running = self.store.transition(self.run_id, queued, "running", worker_pid=1234)

        self.assertEqual(reserved.revision, 0)
        self.assertEqual(queued.revision, 1)
        self.assertEqual(running.revision, 2)
        self.assertEqual(running.parent_job_id, "bundle-1")
        self.assertEqual(running.worker_pid, 1234)
        with self.assertRaises(InvalidRunTransition):
            self.store.transition(self.run_id, queued, "completing")

        completing = self.store.transition(self.run_id, running, "completing")
        with self.assertRaises(InvalidRunTransition):
            self.store.transition(self.run_id, running, "completed")
        raw = json.loads((self.runs_dir / self.run_id / "run_control.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["revision"], completing.revision)

    def test_corrupt_record_cannot_escape_its_locked_control_path(self) -> None:
        victim = self.store.reserve(self.run_id, "poster")
        other = self.store.reserve("other-run", "poster")
        victim_path = self.runs_dir / self.run_id / "run_control.json"
        other_path = self.runs_dir / "other-run" / "run_control.json"
        corrupted = json.loads(victim_path.read_text(encoding="utf-8"))
        corrupted["run_id"] = other.run_id
        victim_path.write_text(json.dumps(corrupted), encoding="utf-8")
        victim_before = victim_path.read_bytes()
        other_before = other_path.read_bytes()

        with self.assertRaises(RunControlError):
            self.store.read(self.run_id)
        with self.assertRaises(RunControlError):
            self.store.transition(self.run_id, victim, "queued")

        self.assertEqual(victim_path.read_bytes(), victim_before)
        self.assertEqual(other_path.read_bytes(), other_before)
        self.assertEqual(self.store.read(other.run_id), other)

    def test_directory_fsync_tolerates_only_unsupported_errors(self) -> None:
        with (
            patch.object(run_control.os, "open", return_value=41),
            patch.object(
                run_control.os,
                "fsync",
                side_effect=OSError(errno.EINVAL, "directory fsync unsupported"),
            ),
            patch.object(run_control.os, "close") as close,
        ):
            run_control._fsync_directory(Path("/not-used"))

        close.assert_called_once_with(41)

    def test_directory_fsync_propagates_io_and_permission_errors(self) -> None:
        for error_number in (errno.EIO, errno.EPERM):
            with self.subTest(error_number=error_number):
                with (
                    patch.object(run_control.os, "open", return_value=41),
                    patch.object(
                        run_control.os,
                        "fsync",
                        side_effect=OSError(error_number, "durability failure"),
                    ),
                    patch.object(run_control.os, "close") as close,
                ):
                    with self.assertRaises(OSError) as raised:
                        run_control._fsync_directory(Path("/not-used"))
                    self.assertEqual(raised.exception.errno, error_number)
                    close.assert_called_once_with(41)

    def test_completion_and_cancellation_race_accepts_one_terminal_path(self) -> None:
        running = self._reserve_running()
        start = threading.Barrier(2)
        accepted: list[str] = []

        def complete() -> None:
            start.wait(timeout=2)
            try:
                completing = self.store.transition(self.run_id, running, "completing")
                self.store.transition(self.run_id, completing, "completed")
                accepted.append("completion")
            except InvalidRunTransition:
                return

        def cancel() -> None:
            start.wait(timeout=2)
            cancelling = self.store.request_cancel(self.run_id)
            if cancelling.state == "cancelling":
                self.store.finalize_cancel(
                    self.run_id,
                    {"termination_verified": True, "reason": "race"},
                )
                accepted.append("cancellation")

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda action: action(), (complete, cancel)))

        final = self.store.read(self.run_id)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(final.state, "cancelled" if accepted[0] == "cancellation" else "completed")
        if final.state == "cancelled":
            self.assertTrue(final.writes_frozen)
            self.assertTrue((self.runs_dir / self.run_id / "cancel_snapshot.json").exists())
        revision = final.revision
        self.assertEqual(
            self.store.finalize_cancel(self.run_id, {"termination_verified": True}),
            final,
        )
        self.assertEqual(self.store.read(self.run_id).revision, revision)

    def test_cross_process_cas_allows_only_cancel_when_it_linearizes_first(self) -> None:
        running = self._reserve_running()
        completing = self.store.transition(self.run_id, running, "completing")
        run_dir = self.runs_dir / self.run_id
        cancel_ready = run_dir / "cancel-ready"
        complete_ready = run_dir / "complete-ready"
        cancel_release = run_dir / "cancel-release"
        complete_release = run_dir / "complete-release"
        code = r'''
import json, sys, time
from pathlib import Path
from autodesign.run_control import InvalidRunTransition, RunControlRecord, RunControlStore
runs_dir, run_id, action, ready_raw, release_raw, result_raw = sys.argv[1:]
store = RunControlStore(Path(runs_dir))
ready, release, result = Path(ready_raw), Path(release_raw), Path(result_raw)
if action == "cancel":
    original = store._transition_unlocked
    def blocked(path, current, target, updates):
        ready.write_text("ready", encoding="utf-8")
        deadline = time.monotonic() + 5
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        if not release.exists():
            raise RuntimeError("cancel barrier timed out")
        return original(path, current, target, updates)
    store._transition_unlocked = blocked
    try:
        value = store.request_cancel(run_id)
        ok = value.state == "cancelling"
    except InvalidRunTransition:
        ok = False
else:
    payload = json.loads((Path(runs_dir) / run_id / "run_control.json").read_text())
    expected = RunControlRecord(**payload)
    original = store._transition_unlocked
    def blocked(path, current, target, updates):
        ready.write_text("ready", encoding="utf-8")
        deadline = time.monotonic() + 5
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        if not release.exists():
            raise RuntimeError("completion barrier timed out")
        return original(path, current, target, updates)
    store._transition_unlocked = blocked
    try:
        store.transition(run_id, expected, "completed", publishable=True, result_digest="race")
        ok = True
    except InvalidRunTransition:
        ok = False
result.write_text(json.dumps({"ok": ok}), encoding="utf-8")
'''
        cancel_result = run_dir / "cancel-result.json"
        complete_result = run_dir / "complete-result.json"
        cancel = subprocess.Popen(
            [sys.executable, "-c", code, str(self.runs_dir), self.run_id, "cancel",
             str(cancel_ready), str(cancel_release), str(cancel_result)],
            cwd=Path(__file__).resolve().parents[1],
        )
        deadline = time.monotonic() + 3
        while not cancel_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(cancel_ready.exists())
        complete = subprocess.Popen(
            [sys.executable, "-c", code, str(self.runs_dir), self.run_id, "complete",
             str(complete_ready), str(complete_release), str(complete_result)],
            cwd=Path(__file__).resolve().parents[1],
        )
        deadline = time.monotonic() + 0.3
        while not complete_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel_release.write_text("release", encoding="utf-8")
        self.assertEqual(cancel.wait(timeout=3), 0)
        complete_release.write_text("release", encoding="utf-8")
        self.assertEqual(complete.wait(timeout=3), 0)

        self.assertTrue(json.loads(cancel_result.read_text())["ok"])
        self.assertFalse(json.loads(complete_result.read_text())["ok"])
        final = self.store.read(self.run_id)
        self.assertEqual(final.state, "cancelling")
        self.assertEqual(final.revision, completing.revision + 1)


if __name__ == "__main__":
    unittest.main()
