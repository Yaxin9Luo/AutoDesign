from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autodesign.run_control import RunControlStore
from scripts import web_server


def _artifact(run_id: str) -> web_server.Artifact:
    return web_server.Artifact(
        artifact_id=f"art_{run_id}",
        name="Poster",
        artifact_type="poster",
        canvas=web_server.Canvas(w=3072, h=1536),
        native_format="html",
    )


class WebHistoryReconstructionTest(unittest.TestCase):
    @staticmethod
    def _write_demo_access(path: Path, *run_ids: str) -> None:
        path.write_text(
            json.dumps({
                "v": 1,
                "runs": {
                    run_id: {
                        "owner": "local",
                        "token": f"token-{run_id}",
                        "created_at": time.time() - 7200,
                    }
                    for run_id in run_ids
                },
            }),
            encoding="utf-8",
        )

    @staticmethod
    def _complete_control(
        store: RunControlStore,
        run_id: str,
        *,
        parent_run_id: str | None = None,
    ) -> None:
        record = store.reserve(
            run_id,
            "poster",
            parent_job_id=parent_run_id,
        )
        record = store.transition(run_id, record, "queued")
        record = store.transition(run_id, record, "running")
        record = store.transition(run_id, record, "completing")
        store.transition(run_id, record, "completed", publishable=True)

    @staticmethod
    def _write_derived_descriptor(
        runs_dir: Path,
        run_id: str,
        *,
        parent_run_id: str,
        job_kind: str,
    ) -> None:
        run_dir = runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "derived_job.json").write_text(
            json.dumps({
                "version": 1,
                "job_kind": job_kind,
                "run_id": run_id,
                "parent_run_id": parent_run_id,
                "artifact_type": "poster",
                "conversation_id": "conversation-ttl",
                "baseline_artifact_json": "",
                "source_artifact_id": f"art_{parent_run_id}",
                "artifact_name": "TTL dependency fixture",
                "source_relative_path": "candidate_draft_lineage.json",
            }),
            encoding="utf-8",
        )

    @classmethod
    def _write_canvas_publication_dependency(
        cls,
        runs_dir: Path,
        *,
        source_run_id: str,
        draft_run_id: str,
        publication_run_id: str,
        publication_state: str,
    ) -> RunControlStore:
        store = RunControlStore(runs_dir)
        cls._complete_control(store, source_run_id)
        cls._complete_control(
            store,
            draft_run_id,
            parent_run_id=source_run_id,
        )
        cls._write_derived_descriptor(
            runs_dir,
            draft_run_id,
            parent_run_id=source_run_id,
            job_kind="attempt_fork",
        )
        record = store.reserve(
            publication_run_id,
            "poster",
            parent_job_id=draft_run_id,
        )
        record = store.transition(publication_run_id, record, "queued")
        if publication_state == "running":
            store.transition(publication_run_id, record, "running")
        elif publication_state != "queued":
            raise AssertionError(f"unsupported fixture state: {publication_state}")
        cls._write_derived_descriptor(
            runs_dir,
            publication_run_id,
            parent_run_id=draft_run_id,
            job_kind="candidate_publish",
        )
        (runs_dir / publication_run_id / "candidate_publish_request.json").write_text(
            json.dumps({
                "version": 3,
                "run_id": publication_run_id,
                "source_run_id": source_run_id,
                "source_attempt": 1,
                "source_candidate_id": "candidate-ttl-01",
                "source_candidate_sha256": "a" * 64,
                "idempotency_key_digest": "b" * 64,
                "request_digest": "c" * 64,
                "paper_bundle_job_id": "bundle-ttl",
                "paper_bundle_owner_id": "local",
                "paper_bundle_artifact_type": "poster",
                "publication_generation": 1,
                "source_draft_run_id": draft_run_id,
            }),
            encoding="utf-8",
        )
        return store

    @staticmethod
    def _run_demo_ttl_cleanup(
        root: Path,
        *,
        runs_dir: Path,
        access_path: Path,
    ) -> None:
        uploads_dir = root / "uploads"
        uploads_dir.mkdir(exist_ok=True)
        with (
            patch.object(web_server, "_DEMO_MODE", True),
            patch.object(web_server, "_DEMO_RUN_TTL_HOURS", 1),
            patch.object(web_server, "RUNS_DIR", runs_dir),
            patch.object(web_server, "UPLOADS_DIR", uploads_dir),
            patch.object(
                web_server,
                "UPLOADS_INDEX_PATH",
                uploads_dir / "conversation_attachments.json",
            ),
            patch.object(web_server, "DEMO_ACCESS_PATH", access_path),
            patch.object(web_server, "_RUNS", {}),
        ):
            web_server._demo_cleanup_expired_runs()

    def _cleanup_expired_demo_run(
        self,
        *,
        run_id: str,
        configure_control,
    ) -> tuple[bool, bool]:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runs_dir = root / "runs"
            uploads_dir = root / "uploads"
            runs_dir.mkdir()
            uploads_dir.mkdir()
            run_dir = runs_dir / run_id
            run_dir.mkdir()
            (run_dir / "marker.txt").write_text("keep-or-delete", encoding="utf-8")
            store = RunControlStore(runs_dir)
            configure_control(store, run_id)
            access_path = root / "demo_run_access.json"
            access_path.write_text(
                json.dumps({
                    "v": 1,
                    "runs": {
                        run_id: {
                            "owner": "local",
                            "token": "token",
                            "created_at": time.time() - 7200,
                        }
                    },
                }),
                encoding="utf-8",
            )
            with (
                patch.object(web_server, "_DEMO_MODE", True),
                patch.object(web_server, "_DEMO_RUN_TTL_HOURS", 1),
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "UPLOADS_DIR", uploads_dir),
                patch.object(
                    web_server,
                    "UPLOADS_INDEX_PATH",
                    uploads_dir / "conversation_attachments.json",
                ),
                patch.object(web_server, "DEMO_ACCESS_PATH", access_path),
                patch.object(web_server, "_RUNS", {}),
            ):
                web_server._demo_cleanup_expired_runs()
                access = json.loads(access_path.read_text(encoding="utf-8"))
                return run_dir.exists(), run_id in access["runs"]

    def test_demo_ttl_keeps_expired_controlled_nonterminal_run(self) -> None:
        def configure(store: RunControlStore, run_id: str) -> None:
            record = store.reserve(run_id, "landing")
            record = store.transition(run_id, record, "queued")
            store.transition(run_id, record, "running")

        self.assertEqual(
            self._cleanup_expired_demo_run(
                run_id="expired-running-control",
                configure_control=configure,
            ),
            (True, True),
        )

    def test_demo_ttl_keeps_pending_terminal_reconciliation(self) -> None:
        def configure(store: RunControlStore, run_id: str) -> None:
            record = store.reserve(run_id, "landing")
            record = store.transition(run_id, record, "queued")
            record = store.transition(run_id, record, "running")
            record = store.update_terminal_reconciliation(
                run_id,
                record,
                decision="accept",
                phase="preflight",
                terminal_state="completed",
                status="pending",
                diagnostic=None,
            )
            record = store.transition(run_id, record, "completing")
            store.transition(run_id, record, "completed", publishable=True)

        self.assertEqual(
            self._cleanup_expired_demo_run(
                run_id="expired-pending-reconciliation",
                configure_control=configure,
            ),
            (True, True),
        )

    def test_demo_ttl_removes_ordinary_expired_terminal_run(self) -> None:
        def configure(store: RunControlStore, run_id: str) -> None:
            record = store.reserve(run_id, "landing")
            record = store.transition(run_id, record, "queued")
            record = store.transition(run_id, record, "running")
            record = store.transition(run_id, record, "completing")
            store.transition(run_id, record, "completed", publishable=True)

        self.assertEqual(
            self._cleanup_expired_demo_run(
                run_id="expired-terminal-control",
                configure_control=configure,
            ),
            (False, False),
        )

    def test_demo_ttl_keeps_canvas_publication_source_and_draft_while_active(
        self,
    ) -> None:
        for publication_state in ("queued", "running"):
            with (
                self.subTest(publication_state=publication_state),
                tempfile.TemporaryDirectory() as raw_tmp,
            ):
                root = Path(raw_tmp)
                runs_dir = root / "runs"
                runs_dir.mkdir()
                source_run_id = f"source-{publication_state}"
                draft_run_id = f"draft-{publication_state}"
                publication_run_id = f"publish-{publication_state}"
                self._write_canvas_publication_dependency(
                    runs_dir,
                    source_run_id=source_run_id,
                    draft_run_id=draft_run_id,
                    publication_run_id=publication_run_id,
                    publication_state=publication_state,
                )
                access_path = root / "demo_run_access.json"
                self._write_demo_access(
                    access_path,
                    source_run_id,
                    draft_run_id,
                    publication_run_id,
                )

                self._run_demo_ttl_cleanup(
                    root,
                    runs_dir=runs_dir,
                    access_path=access_path,
                )

                self.assertTrue((runs_dir / source_run_id).is_dir())
                self.assertTrue((runs_dir / draft_run_id).is_dir())
                self.assertTrue((runs_dir / publication_run_id).is_dir())

    def test_demo_ttl_deletes_publication_dependencies_after_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runs_dir = root / "runs"
            runs_dir.mkdir()
            source_run_id = "source-terminal-publication"
            draft_run_id = "draft-terminal-publication"
            publication_run_id = "publish-terminal-publication"
            store = self._write_canvas_publication_dependency(
                runs_dir,
                source_run_id=source_run_id,
                draft_run_id=draft_run_id,
                publication_run_id=publication_run_id,
                publication_state="running",
            )
            access_path = root / "demo_run_access.json"
            self._write_demo_access(
                access_path,
                source_run_id,
                draft_run_id,
                publication_run_id,
            )
            self._run_demo_ttl_cleanup(
                root,
                runs_dir=runs_dir,
                access_path=access_path,
            )
            self.assertTrue((runs_dir / source_run_id).is_dir())
            self.assertTrue((runs_dir / draft_run_id).is_dir())
            self.assertTrue((runs_dir / publication_run_id).is_dir())
            record = store.read(publication_run_id)
            record = store.transition(publication_run_id, record, "completing")
            store.transition(
                publication_run_id,
                record,
                "completed",
                publishable=True,
            )

            self._run_demo_ttl_cleanup(
                root,
                runs_dir=runs_dir,
                access_path=access_path,
            )

            self.assertFalse((runs_dir / source_run_id).exists())
            self.assertFalse((runs_dir / draft_run_id).exists())
            self.assertFalse((runs_dir / publication_run_id).exists())

    def test_demo_ttl_second_dependency_scan_closes_new_reference_race(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runs_dir = root / "runs"
            runs_dir.mkdir()
            source_run_id = "source-second-scan"
            publication_run_id = "publish-second-scan"
            store = RunControlStore(runs_dir)
            self._complete_control(store, source_run_id)
            access_path = root / "demo_run_access.json"
            self._write_demo_access(access_path, source_run_id)
            original_scan = web_server._demo_ttl_protected_run_ids
            scan_count = 0

            def scan_with_new_dependency() -> set[str]:
                nonlocal scan_count
                scan_count += 1
                if scan_count == 2:
                    record = store.reserve(
                        publication_run_id,
                        "poster",
                        parent_job_id=source_run_id,
                    )
                    store.transition(publication_run_id, record, "queued")
                    self._write_derived_descriptor(
                        runs_dir,
                        publication_run_id,
                        parent_run_id=source_run_id,
                        job_kind="artifact_edit",
                    )
                return original_scan()

            uploads_dir = root / "uploads"
            uploads_dir.mkdir()
            with (
                patch.object(web_server, "_DEMO_MODE", True),
                patch.object(web_server, "_DEMO_RUN_TTL_HOURS", 1),
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "UPLOADS_DIR", uploads_dir),
                patch.object(
                    web_server,
                    "UPLOADS_INDEX_PATH",
                    uploads_dir / "conversation_attachments.json",
                ),
                patch.object(web_server, "DEMO_ACCESS_PATH", access_path),
                patch.object(web_server, "_RUNS", {}),
                patch.object(
                    web_server,
                    "_demo_ttl_protected_run_ids",
                    side_effect=scan_with_new_dependency,
                ),
            ):
                web_server._demo_cleanup_expired_runs()

            self.assertEqual(scan_count, 2)
            self.assertTrue((runs_dir / source_run_id).is_dir())

    def test_demo_ttl_does_not_pin_abandoned_publication_generation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runs_dir = root / "runs"
            runs_dir.mkdir()
            source_run_id = "source-pending-generation"
            store = RunControlStore(runs_dir)
            self._complete_control(store, source_run_id)
            access_path = root / "demo_run_access.json"
            self._write_demo_access(access_path, source_run_id)
            child = SimpleNamespace(run_id=source_run_id)
            bundle = SimpleNamespace(
                state="completed",
                cancel_requested=False,
                children={"poster": child},
                publications={},
                publication_generations={
                    "poster": 1,
                    "deck": 0,
                    "landing": 0,
                    "video": 0,
                },
            )
            bundle_store = SimpleNamespace(list_all=lambda: (bundle,))
            uploads_dir = root / "uploads"
            uploads_dir.mkdir()
            with (
                patch.object(web_server, "_DEMO_MODE", True),
                patch.object(web_server, "_DEMO_RUN_TTL_HOURS", 1),
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "UPLOADS_DIR", uploads_dir),
                patch.object(
                    web_server,
                    "UPLOADS_INDEX_PATH",
                    uploads_dir / "conversation_attachments.json",
                ),
                patch.object(web_server, "DEMO_ACCESS_PATH", access_path),
                patch.object(web_server, "_RUNS", {}),
                patch.object(
                    web_server,
                    "_paper_bundle_store",
                    return_value=bundle_store,
                ),
            ):
                web_server._demo_cleanup_expired_runs()

            self.assertFalse((runs_dir / source_run_id).exists())

    def test_demo_ttl_final_access_lineage_protects_fresh_derived_child(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runs_dir = root / "runs"
            runs_dir.mkdir()
            source_run_id = "source-fresh-access-child"
            child_run_id = "fresh-access-child"
            store = RunControlStore(runs_dir)
            self._complete_control(store, source_run_id)
            access_path = root / "demo_run_access.json"
            self._write_demo_access(access_path, source_run_id)
            uploads_dir = root / "uploads"
            uploads_dir.mkdir()
            underlying_lock = threading.RLock()

            class InjectFreshChildBeforeFinalDelete:
                def __init__(self) -> None:
                    self.entries = 0

                def __enter__(self):
                    underlying_lock.acquire()
                    self.entries += 1
                    if self.entries == 2:
                        data = json.loads(access_path.read_text(encoding="utf-8"))
                        data["runs"][child_run_id] = {
                            "owner": "local",
                            "token": "fresh-token",
                            "created_at": time.time(),
                            "parent_run_id": source_run_id,
                        }
                        access_path.write_text(json.dumps(data), encoding="utf-8")
                    return self

                def __exit__(self, exc_type, exc, traceback) -> None:
                    underlying_lock.release()

            injecting_lock = InjectFreshChildBeforeFinalDelete()
            with (
                patch.object(web_server, "_DEMO_MODE", True),
                patch.object(web_server, "_DEMO_RUN_TTL_HOURS", 1),
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "UPLOADS_DIR", uploads_dir),
                patch.object(
                    web_server,
                    "UPLOADS_INDEX_PATH",
                    uploads_dir / "conversation_attachments.json",
                ),
                patch.object(web_server, "DEMO_ACCESS_PATH", access_path),
                patch.object(web_server, "_RUNS", {}),
                patch.object(web_server, "_DEMO_ACCESS_LOCK", injecting_lock),
            ):
                web_server._demo_cleanup_expired_runs()

            access = json.loads(access_path.read_text(encoding="utf-8"))["runs"]
            self.assertEqual(injecting_lock.entries, 2)
            self.assertTrue((runs_dir / source_run_id).is_dir())
            self.assertIn(source_run_id, access)
            self.assertIn(child_run_id, access)

    def test_derived_access_registration_never_falls_back_to_orphan_child(
        self,
    ) -> None:
        access = {"v": 1, "runs": {}}
        with (
            patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
            patch.object(web_server, "_demo_user_id", return_value="local"),
            patch.object(web_server, "_demo_user_owns_run", return_value=True),
            patch.object(web_server, "_demo_load_access", return_value=access),
            patch.object(web_server, "_demo_write_access"),
        ):
            with self.assertRaises(web_server.HTTPException) as raised:
                web_server._demo_register_derived_run_access(
                    "derived-child",
                    SimpleNamespace(),
                    parent_run_id="deleted-parent",
                )

        self.assertEqual(raised.exception.status_code, 404)
        self.assertNotIn("derived-child", access["runs"])

    def test_direct_publish_registers_access_before_bundle_binding_await(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runs_dir = root / "runs"
            runs_dir.mkdir()
            source_run_id = "source-direct-publication-lease"
            source_run_dir = runs_dir / source_run_id
            source_run_dir.mkdir()
            store = RunControlStore(runs_dir)
            self._complete_control(store, source_run_id)
            access_path = root / "demo_run_access.json"
            self._write_demo_access(access_path, source_run_id)
            uploads_dir = root / "uploads"
            uploads_dir.mkdir()
            candidate = SimpleNamespace(
                artifact_type=SimpleNamespace(value="poster"),
                candidate_id="candidate-direct-lease",
                source_sha256="a" * 64,
                source_relative_path="attempt_candidates/attempt_01/poster.html",
                safety_state="ready",
            )
            request = SimpleNamespace(
                headers={"x-autodesign-reserve-only": "1"},
                cookies={},
                client=SimpleNamespace(host="127.0.0.1"),
            )
            publish_request = web_server.DirectAttemptPublishRequest(
                conversation_id="conversation-direct-lease",
                expected_candidate_sha256="a" * 64,
                idempotency_key="direct-lease",
            )

            def reserve_bundle_binding(*_args):
                access = json.loads(access_path.read_text(encoding="utf-8"))["runs"]
                self.assertTrue(
                    any(
                        entry.get("parent_run_id") == source_run_id
                        for entry in access.values()
                        if isinstance(entry, dict)
                    )
                )
                web_server._demo_cleanup_expired_runs()
                self.assertTrue(source_run_dir.is_dir())
                return None

            async def reserve_publication_run(*, request, **_kwargs):
                (runs_dir / request.run_id).mkdir(parents=True, exist_ok=True)
                return "publication-start-token"

            with (
                patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
                patch.object(web_server, "_DEMO_MODE", True),
                patch.object(web_server, "_DEMO_RUN_TTL_HOURS", 1),
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "UPLOADS_DIR", uploads_dir),
                patch.object(
                    web_server,
                    "UPLOADS_INDEX_PATH",
                    uploads_dir / "conversation_attachments.json",
                ),
                patch.object(web_server, "DEMO_ACCESS_PATH", access_path),
                patch.object(web_server, "_RUNS", {}),
                patch.object(web_server, "_demo_user_id", return_value="local"),
                patch.object(
                    web_server,
                    "_assert_controlled_run_source_usable",
                ),
                patch.object(
                    web_server,
                    "load_attempt_candidate",
                    return_value=candidate,
                ),
                patch.object(
                    web_server,
                    "_reserve_candidate_publish_bundle_binding",
                    side_effect=reserve_bundle_binding,
                ),
                patch.object(
                    web_server,
                    "_settings_for_request",
                    return_value=SimpleNamespace(),
                ),
                patch.object(
                    web_server,
                    "_start_supervised_derived_job",
                    side_effect=reserve_publication_run,
                ),
                patch.object(web_server, "_append_event"),
            ):
                acknowledgement = asyncio.run(
                    web_server.publish_run_attempt(
                        source_run_id,
                        1,
                        publish_request,
                        request,
                    )
                )

            self.assertEqual(
                acknowledgement.start_token,
                "publication-start-token",
            )
            self.assertTrue(source_run_dir.is_dir())

    def test_direct_publish_cancellation_waits_for_binding_thread_before_release(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runs_dir = root / "runs"
            runs_dir.mkdir()
            source_run_id = "source-direct-binding-cancel"
            (runs_dir / source_run_id).mkdir()
            access_path = root / "demo_run_access.json"
            self._write_demo_access(access_path, source_run_id)
            candidate = SimpleNamespace(
                artifact_type=SimpleNamespace(value="poster"),
                candidate_id="candidate-direct-binding-cancel",
                source_sha256="c" * 64,
                source_relative_path="attempt_candidates/attempt_01/poster.html",
                safety_state="ready",
            )
            request = SimpleNamespace(
                headers={"x-autodesign-reserve-only": "1"},
                cookies={},
                client=SimpleNamespace(host="127.0.0.1"),
            )
            idempotency_key = "direct-binding-cancel"
            idempotency_digest = hashlib.sha256(
                json.dumps(
                    ["local", idempotency_key],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            published_run_id = f"candidate-publish-{idempotency_digest[:32]}"
            binding_started = threading.Event()
            release_binding = threading.Event()

            def blocking_binding(*_args):
                binding_started.set()
                if not release_binding.wait(timeout=5):
                    raise RuntimeError("binding test barrier timed out")
                return None

            async def cancel_during_binding() -> None:
                task = asyncio.create_task(
                    web_server.publish_run_attempt(
                        source_run_id,
                        1,
                        web_server.DirectAttemptPublishRequest(
                            conversation_id="conversation-binding-cancel",
                            expected_candidate_sha256=candidate.source_sha256,
                            idempotency_key=idempotency_key,
                        ),
                        request,
                    )
                )
                try:
                    for _ in range(1000):
                        if binding_started.is_set():
                            break
                        await asyncio.sleep(0.001)
                    self.assertTrue(binding_started.is_set())
                    task.cancel()
                    await asyncio.sleep(0.02)
                    task.cancel()
                    await asyncio.sleep(0.02)
                    access = json.loads(
                        access_path.read_text(encoding="utf-8")
                    )["runs"]
                    self.assertFalse(task.done())
                    self.assertIn(published_run_id, access)
                finally:
                    release_binding.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            with (
                patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "DEMO_ACCESS_PATH", access_path),
                patch.object(web_server, "_RUNS", {}),
                patch.object(web_server, "_demo_user_id", return_value="local"),
                patch.object(web_server, "_run_owner_id", return_value="local"),
                patch.object(
                    web_server,
                    "_assert_controlled_run_source_usable",
                ),
                patch.object(
                    web_server,
                    "load_attempt_candidate",
                    return_value=candidate,
                ),
                patch.object(
                    web_server,
                    "_reserve_candidate_publish_bundle_binding",
                    side_effect=blocking_binding,
                ),
            ):
                asyncio.run(cancel_during_binding())

            access = json.loads(access_path.read_text(encoding="utf-8"))["runs"]
            self.assertNotIn(published_run_id, access)

    def test_direct_publish_cancellation_adopts_completed_control_handoff(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runs_dir = root / "runs"
            runs_dir.mkdir()
            source_run_id = "source-direct-control-cancel"
            (runs_dir / source_run_id).mkdir()
            source_store = RunControlStore(runs_dir)
            self._complete_control(source_store, source_run_id)
            access_path = root / "demo_run_access.json"
            self._write_demo_access(access_path, source_run_id)
            candidate = SimpleNamespace(
                artifact_type=SimpleNamespace(value="poster"),
                candidate_id="candidate-direct-control-cancel",
                source_sha256="7" * 64,
                source_relative_path="attempt_candidates/attempt_01/poster.html",
                safety_state="ready",
            )
            request = SimpleNamespace(
                headers={"x-autodesign-reserve-only": "1"},
                cookies={},
                client=SimpleNamespace(host="127.0.0.1"),
            )
            idempotency_key = "direct-control-cancel"
            idempotency_digest = hashlib.sha256(
                json.dumps(
                    ["local", idempotency_key],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            published_run_id = f"candidate-publish-{idempotency_digest[:32]}"

            async def exercise() -> None:
                control_reserved = asyncio.Event()
                release_reservation = asyncio.Event()

                async def reserve_derived_job(*, request, state, **_kwargs):
                    record = source_store.reserve(
                        request.run_id,
                        "poster",
                        parent_job_id=request.parent_run_id,
                    )
                    source_store.transition(request.run_id, record, "queued")
                    control_reserved.set()
                    await release_reservation.wait()
                    token = f"token-{request.run_id}"
                    state.reservation_token = token
                    return token

                class Services:
                    async def cancel(self, controlled_run_id: str, _reason: str):
                        record = source_store.request_cancel(controlled_run_id)
                        if record.state == "cancelling":
                            source_store.finalize_cancel(
                                controlled_run_id,
                                {"termination_verified": True},
                            )
                        return SimpleNamespace(
                            confirmed=True,
                            cancel_request_event_required=False,
                        )

                with (
                    patch.object(
                        web_server,
                        "_start_supervised_derived_job",
                        side_effect=reserve_derived_job,
                    ),
                    patch.object(
                        web_server,
                        "_web_run_runtime",
                        return_value=SimpleNamespace(services=Services()),
                    ),
                ):
                    publish = asyncio.create_task(
                        web_server.publish_run_attempt(
                            source_run_id,
                            1,
                            web_server.DirectAttemptPublishRequest(
                                conversation_id="conversation-control-cancel",
                                expected_candidate_sha256=candidate.source_sha256,
                                idempotency_key=idempotency_key,
                            ),
                            request,
                        )
                    )
                    await control_reserved.wait()
                    publish.cancel()
                    await asyncio.sleep(0)
                    publish.cancel()
                    await asyncio.sleep(0.01)
                    self.assertFalse(publish.done())
                    release_reservation.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await publish

            with (
                patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "DEMO_ACCESS_PATH", access_path),
                patch.object(web_server, "_RUNS", {}),
                patch.object(web_server, "_demo_user_id", return_value="local"),
                patch.object(web_server, "_run_owner_id", return_value="local"),
                patch.object(web_server, "_assert_controlled_run_source_usable"),
                patch.object(web_server, "load_attempt_candidate", return_value=candidate),
                patch.object(
                    web_server,
                    "_reserve_candidate_publish_bundle_binding",
                    return_value=None,
                ),
                patch.object(web_server, "_settings_for_request", return_value=object()),
                patch.object(web_server, "_append_event"),
            ):
                asyncio.run(exercise())

            self.assertEqual(source_store.read(published_run_id).state, "cancelled")
            self.assertNotIn(published_run_id, web_server._RUNS)
            access = json.loads(access_path.read_text(encoding="utf-8"))["runs"]
            self.assertIn(published_run_id, access)
            self.assertTrue(
                (runs_dir / published_run_id / "derived_job.json").is_file()
            )
            self.assertTrue(
                (
                    runs_dir
                    / published_run_id
                    / "candidate_publish_request.json"
                ).is_file()
            )

    def test_reserved_start_cancellation_before_monitor_adoption_cancels_run(
        self,
    ) -> None:
        run_id = "candidate-publish-monitor-adoption-cancel"
        state = web_server._RunState(
            artifact_type="poster",
            brief="monitor adoption cancellation",
        )

        async def exercise() -> tuple[int, bool, list[tuple[str, str]]]:
            runs_lock = asyncio.Lock()
            cancel_events: list[tuple[str, str]] = []

            class Services:
                def __init__(self) -> None:
                    self.start_returned = asyncio.Event()
                    self.cancel_calls = 0

                async def start(self, *_args):
                    self.start_returned.set()

                async def cancel(self, *_args):
                    self.cancel_calls += 1
                    return SimpleNamespace(
                        confirmed=True,
                        cancel_request_event_required=True,
                    )

            services = Services()
            with (
                patch.object(web_server, "_RUNS_LOCK", runs_lock),
                patch.object(
                    web_server,
                    "_web_run_runtime",
                    return_value=SimpleNamespace(services=services),
                ),
                patch.object(
                    web_server,
                    "_append_cancel_request_event",
                    side_effect=lambda event_run_id, reason: cancel_events.append(
                        (event_run_id, reason)
                    ),
                ),
            ):
                async with runs_lock:
                    start = asyncio.create_task(
                        web_server._start_reserved_derived_job(
                            run_id=run_id,
                            token="monitor-adoption-token",
                            state=state,
                            descriptor={"job_kind": "candidate_publish"},
                        )
                    )
                    await services.start_returned.wait()
                    await asyncio.sleep(0)
                    start.cancel()
                    await asyncio.sleep(0)
                    start.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await start
            return services.cancel_calls, state.task is not None, cancel_events

        cancel_calls, monitor_adopted, cancel_events = asyncio.run(exercise())
        self.assertEqual(cancel_calls, 1)
        self.assertFalse(monitor_adopted)
        self.assertEqual(
            cancel_events,
            [(run_id, "derived_start_failed")],
        )

    def test_direct_publish_post_binding_failure_releases_pre_durable_ownership(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runs_dir = root / "runs"
            runs_dir.mkdir()
            source_run_id = "source-direct-pre-durable-failure"
            source_run_dir = runs_dir / source_run_id
            source_run_dir.mkdir()
            store = RunControlStore(runs_dir)
            self._complete_control(store, source_run_id)
            access_path = root / "demo_run_access.json"
            self._write_demo_access(access_path, source_run_id)
            uploads_dir = root / "uploads"
            uploads_dir.mkdir()
            candidate = SimpleNamespace(
                artifact_type=SimpleNamespace(value="poster"),
                candidate_id="candidate-direct-pre-durable",
                source_sha256="d" * 64,
                source_relative_path="attempt_candidates/attempt_01/poster.html",
                safety_state="ready",
            )
            request = SimpleNamespace(
                headers={"x-autodesign-reserve-only": "1"},
                cookies={},
                client=SimpleNamespace(host="127.0.0.1"),
            )
            idempotency_key = "direct-pre-durable-failure"
            publish_request = web_server.DirectAttemptPublishRequest(
                conversation_id="conversation-direct-pre-durable",
                expected_candidate_sha256=candidate.source_sha256,
                idempotency_key=idempotency_key,
            )
            idempotency_digest = hashlib.sha256(
                json.dumps(
                    ["local", idempotency_key],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            published_run_id = f"candidate-publish-{idempotency_digest[:32]}"
            bundle_binding = {
                "paper_bundle_job_id": "bundle-direct-pre-durable",
                "paper_bundle_owner_id": "local",
                "paper_bundle_artifact_type": "poster",
                "publication_generation": 1,
            }

            with (
                patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
                patch.object(web_server, "_DEMO_MODE", True),
                patch.object(web_server, "_DEMO_RUN_TTL_HOURS", 1),
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "UPLOADS_DIR", uploads_dir),
                patch.object(
                    web_server,
                    "UPLOADS_INDEX_PATH",
                    uploads_dir / "conversation_attachments.json",
                ),
                patch.object(web_server, "DEMO_ACCESS_PATH", access_path),
                patch.object(web_server, "_RUNS", {}),
                patch.object(web_server, "_demo_user_id", return_value="local"),
                patch.object(web_server, "_run_owner_id", return_value="local"),
                patch.object(
                    web_server,
                    "_assert_controlled_run_source_usable",
                ),
                patch.object(
                    web_server,
                    "load_attempt_candidate",
                    return_value=candidate,
                ),
                patch.object(
                    web_server,
                    "_reserve_candidate_publish_bundle_binding",
                    return_value=bundle_binding,
                ),
                patch.object(
                    web_server,
                    "_settings_for_request",
                    side_effect=RuntimeError("settings unavailable after binding"),
                ),
                self.assertRaisesRegex(RuntimeError, "settings unavailable"),
            ):
                asyncio.run(
                    web_server.publish_run_attempt(
                        source_run_id,
                        1,
                        publish_request,
                        request,
                    )
                )

            access = json.loads(access_path.read_text(encoding="utf-8"))["runs"]
            self.assertNotIn(published_run_id, access)
            self.assertNotIn(published_run_id, web_server._RUNS)
            with (
                patch.object(web_server, "_DEMO_MODE", True),
                patch.object(web_server, "_DEMO_RUN_TTL_HOURS", 1),
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "UPLOADS_DIR", uploads_dir),
                patch.object(
                    web_server,
                    "UPLOADS_INDEX_PATH",
                    uploads_dir / "conversation_attachments.json",
                ),
                patch.object(web_server, "DEMO_ACCESS_PATH", access_path),
                patch.object(web_server, "_RUNS", {}),
            ):
                web_server._demo_cleanup_expired_runs()
            self.assertFalse(source_run_dir.exists())

    def test_direct_publish_replays_write_ahead_intent_before_control_handoff(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runs_dir = root / "runs"
            runs_dir.mkdir()
            source_run_id = "source-direct-pre-handoff-retry"
            (runs_dir / source_run_id).mkdir()
            store = RunControlStore(runs_dir)
            self._complete_control(store, source_run_id)
            access_path = root / "demo_run_access.json"
            self._write_demo_access(access_path, source_run_id)
            candidate = SimpleNamespace(
                artifact_type=SimpleNamespace(value="poster"),
                candidate_id="candidate-direct-pre-handoff-retry",
                source_sha256="9" * 64,
                source_relative_path="attempt_candidates/attempt_01/poster.html",
                safety_state="ready",
            )
            request = SimpleNamespace(
                headers={"x-autodesign-reserve-only": "1"},
                cookies={},
                client=SimpleNamespace(host="127.0.0.1"),
            )
            idempotency_key = "direct-pre-handoff-retry"
            publish_request = web_server.DirectAttemptPublishRequest(
                conversation_id="conversation-direct-pre-handoff-retry",
                expected_candidate_sha256=candidate.source_sha256,
                idempotency_key=idempotency_key,
            )
            idempotency_digest = hashlib.sha256(
                json.dumps(
                    ["local", idempotency_key],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            first_run_id = f"candidate-publish-{idempotency_digest[:32]}"
            real_durable_replace_json = web_server.durable_replace_json
            fail_first_derived_write = True

            async def reserve_derived_job(*, request, state, **_kwargs):
                record = store.reserve(
                    request.run_id,
                    "poster",
                    parent_job_id=request.parent_run_id,
                )
                store.transition(request.run_id, record, "queued")
                token = f"token-{request.run_id}"
                state.reservation_token = token
                return token

            async def cancel_derived_job(run_id: str, _reason: str):
                record = store.request_cancel(run_id)
                if record.state == "cancelling":
                    store.finalize_cancel(
                        run_id,
                        {"termination_verified": True},
                    )
                return SimpleNamespace(confirmed=True)

            def write_publication_request(path: Path, payload: object) -> None:
                nonlocal fail_first_derived_write
                if path.name == "derived_job.json" and fail_first_derived_write:
                    fail_first_derived_write = False
                    raise OSError("derived intent write interrupted")
                real_durable_replace_json(path, payload)

            with (
                patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "DEMO_ACCESS_PATH", access_path),
                patch.object(web_server, "_RUNS", {}),
                patch.object(web_server, "_demo_user_id", return_value="local"),
                patch.object(web_server, "_run_owner_id", return_value="local"),
                patch.object(web_server, "_assert_controlled_run_source_usable"),
                patch.object(web_server, "load_attempt_candidate", return_value=candidate),
                patch.object(
                    web_server,
                    "_reserve_candidate_publish_bundle_binding",
                    return_value=None,
                ),
                patch.object(web_server, "_settings_for_request", return_value=object()),
                patch.object(
                    web_server,
                    "_start_supervised_derived_job",
                    side_effect=reserve_derived_job,
                ),
                patch.object(
                    web_server,
                    "_web_run_runtime",
                    return_value=SimpleNamespace(
                        services=SimpleNamespace(cancel=cancel_derived_job)
                    ),
                ),
                patch.object(
                    web_server,
                    "durable_replace_json",
                    side_effect=write_publication_request,
                ),
                patch.object(web_server, "_append_event"),
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "derived intent write interrupted",
                ):
                    asyncio.run(
                        web_server.publish_run_attempt(
                            source_run_id,
                            1,
                            publish_request,
                            request,
                        )
                    )

                self.assertFalse(
                    (runs_dir / first_run_id / "run_control.json").exists()
                )
                self.assertFalse(
                    (
                        runs_dir
                        / first_run_id
                        / "candidate_publish_request.json"
                    ).exists()
                )
                access = json.loads(access_path.read_text(encoding="utf-8"))["runs"]
                self.assertNotIn(first_run_id, access)

                replay = asyncio.run(
                    web_server.publish_run_attempt(
                        source_run_id,
                        1,
                        publish_request,
                        request,
                    )
                )

            self.assertEqual(replay.run_id, first_run_id)
            self.assertEqual(store.read(replay.run_id).state, "queued")
            descriptor = json.loads(
                (
                    runs_dir
                    / replay.run_id
                    / "candidate_publish_request.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(descriptor["idempotency_key_digest"], idempotency_digest)

    def test_direct_publish_cold_replays_matching_write_ahead_intent(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runs_dir = root / "runs"
            runs_dir.mkdir()
            source_run_id = "source-direct-cold-intent"
            (runs_dir / source_run_id).mkdir()
            store = RunControlStore(runs_dir)
            bundle_job_id = "bundle-direct-cold-intent"
            self._complete_control(
                store,
                source_run_id,
                parent_run_id=bundle_job_id,
            )
            access_path = root / "demo_run_access.json"
            self._write_demo_access(access_path, source_run_id)
            candidate = SimpleNamespace(
                artifact_type=SimpleNamespace(value="poster"),
                candidate_id="candidate-direct-cold-intent",
                source_sha256="8" * 64,
                source_relative_path="attempt_candidates/attempt_01/poster.html",
                safety_state="ready",
            )
            request = SimpleNamespace(
                headers={"x-autodesign-reserve-only": "1"},
                cookies={},
                client=SimpleNamespace(host="127.0.0.1"),
            )
            idempotency_key = "direct-cold-intent"
            conversation_id = "conversation-direct-cold-intent"
            publish_request = web_server.DirectAttemptPublishRequest(
                conversation_id=conversation_id,
                expected_candidate_sha256=candidate.source_sha256,
                idempotency_key=idempotency_key,
            )
            idempotency_digest = hashlib.sha256(
                json.dumps(
                    ["local", idempotency_key],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            request_digest = hashlib.sha256(
                json.dumps(
                    {
                        "owner": "local",
                        "source_run_id": source_run_id,
                        "source_attempt": 1,
                        "source_candidate_sha256": candidate.source_sha256,
                        "conversation_id": conversation_id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            published_run_id = f"candidate-publish-{idempotency_digest[:32]}"
            published_dir = runs_dir / published_run_id
            published_dir.mkdir()
            durable_conversation_id = web_server._event_conversation_id(
                conversation_id,
                published_run_id,
            )
            candidate_publish_request_path = (
                published_dir / "candidate_publish_request.json"
            )
            candidate_publish_request_payload = {
                "version": 2,
                "run_id": published_run_id,
                "source_run_id": source_run_id,
                "source_attempt": 1,
                "source_candidate_id": candidate.candidate_id,
                "source_candidate_sha256": candidate.source_sha256,
                "idempotency_key_digest": idempotency_digest,
                "request_digest": request_digest,
                "paper_bundle_job_id": bundle_job_id,
                "paper_bundle_owner_id": "local",
                "paper_bundle_artifact_type": "poster",
                "publication_generation": 2,
            }
            candidate_publish_request_path.write_text(
                json.dumps(candidate_publish_request_payload),
                encoding="utf-8",
            )
            durable_derived_descriptor = {
                "version": 1,
                "job_kind": "candidate_publish",
                "run_id": published_run_id,
                "parent_run_id": source_run_id,
                "artifact_type": "poster",
                "conversation_id": durable_conversation_id,
                "baseline_artifact_json": json.dumps({
                    "artifact_id": f"art_{source_run_id}"
                }),
                "source_artifact_id": f"art_{source_run_id}",
                "artifact_name": "Published Attempt 1",
                "source_relative_path": candidate.source_relative_path,
            }
            derived_descriptor_path = published_dir / "derived_job.json"
            derived_descriptor_path.write_text(
                json.dumps(durable_derived_descriptor),
                encoding="utf-8",
            )

            async def reserve_derived_job(*, request, state, **_kwargs):
                record = store.reserve(
                    request.run_id,
                    "poster",
                    parent_job_id=request.parent_run_id,
                )
                store.transition(request.run_id, record, "queued")
                token = f"token-{request.run_id}"
                state.reservation_token = token
                return token

            bundle_record = SimpleNamespace(
                state="running",
                cancel_requested=False,
                children={
                    "poster": SimpleNamespace(run_id=source_run_id),
                },
                publication_generations={"poster": 2},
                publications={},
            )
            bundle_store = SimpleNamespace(
                read_owned=lambda *_args, **_kwargs: bundle_record,
            )

            with (
                patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "DEMO_ACCESS_PATH", access_path),
                patch.object(web_server, "_RUNS", {}),
                patch.object(web_server, "_demo_user_id", return_value="local"),
                patch.object(web_server, "_run_owner_id", return_value="local"),
                patch.object(
                    web_server,
                    "_paper_bundle_store",
                    return_value=bundle_store,
                ),
                patch.object(web_server, "_assert_controlled_run_source_usable"),
                patch.object(web_server, "load_attempt_candidate", return_value=candidate),
                patch.object(web_server, "_settings_for_request", return_value=object()),
                patch.object(
                    web_server,
                    "_start_supervised_derived_job",
                    side_effect=reserve_derived_job,
                ) as reserve_job,
                patch.object(web_server, "_append_event"),
            ):
                candidate_publish_request_path.write_text(
                    json.dumps({
                        **candidate_publish_request_payload,
                        "source_candidate_id": "tampered-candidate-id",
                    }),
                    encoding="utf-8",
                )
                with self.assertRaises(
                    web_server.HTTPException
                ) as changed_candidate:
                    asyncio.run(
                        web_server.publish_run_attempt(
                            source_run_id,
                            1,
                            publish_request,
                            request,
                        )
                    )
                self.assertEqual(changed_candidate.exception.status_code, 409)
                self.assertEqual(reserve_job.await_count, 0)
                self.assertTrue(candidate_publish_request_path.is_file())
                for field_name, value in (
                    ("paper_bundle_job_id", "other-bundle-cold-intent"),
                    ("paper_bundle_owner_id", "other-owner"),
                    ("paper_bundle_artifact_type", "landing"),
                    ("publication_generation", 1),
                ):
                    with self.subTest(field_name=field_name):
                        candidate_publish_request_path.write_text(
                            json.dumps({
                                **candidate_publish_request_payload,
                                field_name: value,
                            }),
                            encoding="utf-8",
                        )
                        with self.assertRaises(
                            web_server.HTTPException
                        ) as mismatch:
                            asyncio.run(
                                web_server.publish_run_attempt(
                                    source_run_id,
                                    1,
                                    publish_request,
                                    request,
                                )
                            )
                        self.assertEqual(mismatch.exception.status_code, 409)
                        self.assertEqual(reserve_job.await_count, 0)
                candidate_publish_request_path.write_text(
                    json.dumps({
                        field_name: value
                        for field_name, value in (
                            candidate_publish_request_payload.items()
                        )
                        if field_name
                        not in {
                            "paper_bundle_job_id",
                            "paper_bundle_owner_id",
                            "paper_bundle_artifact_type",
                            "publication_generation",
                        }
                    } | {"version": 1}),
                    encoding="utf-8",
                )
                with self.assertRaises(
                    web_server.HTTPException
                ) as downgraded_bundle:
                    asyncio.run(
                        web_server.publish_run_attempt(
                            source_run_id,
                            1,
                            publish_request,
                            request,
                        )
                    )
                self.assertEqual(downgraded_bundle.exception.status_code, 409)
                self.assertEqual(reserve_job.await_count, 0)
                self.assertTrue(candidate_publish_request_path.is_file())
                candidate_publish_request_path.write_text(
                    json.dumps(candidate_publish_request_payload),
                    encoding="utf-8",
                )
                derived_descriptor_path.write_text(
                    json.dumps({
                        **durable_derived_descriptor,
                        "artifact_name": "tampered cold intent",
                    }),
                    encoding="utf-8",
                )
                with self.assertRaises(web_server.HTTPException) as corrupt_intent:
                    asyncio.run(
                        web_server.publish_run_attempt(
                            source_run_id,
                            1,
                            publish_request,
                            request,
                        )
                    )
                self.assertEqual(corrupt_intent.exception.status_code, 409)
                self.assertTrue(
                    (published_dir / "candidate_publish_request.json").is_file()
                )
                self.assertTrue(derived_descriptor_path.is_file())
                derived_descriptor_path.write_text(
                    json.dumps(durable_derived_descriptor),
                    encoding="utf-8",
                )
                first = asyncio.run(
                    web_server.publish_run_attempt(
                        source_run_id,
                        1,
                        publish_request,
                        request,
                    )
                )
                replay = asyncio.run(
                    web_server.publish_run_attempt(
                        source_run_id,
                        1,
                        publish_request,
                        request,
                    )
                )
                changed_request = web_server.DirectAttemptPublishRequest(
                    conversation_id="changed-conversation",
                    expected_candidate_sha256=candidate.source_sha256,
                    idempotency_key=idempotency_key,
                )
                with self.assertRaises(web_server.HTTPException) as conflict:
                    asyncio.run(
                        web_server.publish_run_attempt(
                            source_run_id,
                            1,
                            changed_request,
                            request,
                        )
                    )

            self.assertEqual(first.run_id, published_run_id)
            self.assertEqual(replay.run_id, published_run_id)
            self.assertEqual(replay.start_token, first.start_token)
            self.assertEqual(reserve_job.await_count, 1)
            self.assertEqual(conflict.exception.status_code, 409)

    def test_canvas_publish_post_binding_failure_releases_pre_durable_ownership(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runs_dir = root / "runs"
            runs_dir.mkdir()
            source_run_id = "source-canvas-pre-durable-failure"
            draft_run_id = "draft-canvas-pre-durable-failure"
            published_run_id = "publish-canvas-pre-durable-failure"
            (runs_dir / source_run_id).mkdir()
            draft_dir = runs_dir / draft_run_id
            draft_dir.mkdir()
            lineage = {
                "status": "draft",
                "artifact_type": "poster",
                "source_run_id": source_run_id,
                "source_attempt": 1,
                "source_candidate_id": "candidate-canvas-pre-durable",
                "source_candidate_sha256": "e" * 64,
            }
            (draft_dir / "candidate_draft_lineage.json").write_text(
                json.dumps(lineage),
                encoding="utf-8",
            )
            access_path = root / "demo_run_access.json"
            access_path.write_text(
                json.dumps({
                    "v": 1,
                    "runs": {
                        source_run_id: {
                            "owner": "local",
                            "token": "source-token",
                            "created_at": time.time() - 7200,
                        },
                        draft_run_id: {
                            "owner": "local",
                            "token": "draft-token",
                            "created_at": time.time() - 7200,
                            "parent_run_id": source_run_id,
                        },
                    },
                }),
                encoding="utf-8",
            )
            candidate = SimpleNamespace(
                artifact_type=SimpleNamespace(value="poster"),
                candidate_id=lineage["source_candidate_id"],
                source_sha256=lineage["source_candidate_sha256"],
            )
            request = SimpleNamespace(
                headers={"x-autodesign-reserve-only": "1"},
                cookies={},
                client=SimpleNamespace(host="127.0.0.1"),
            )
            bundle_binding = {
                "paper_bundle_job_id": "bundle-canvas-pre-durable",
                "paper_bundle_owner_id": "local",
                "paper_bundle_artifact_type": "poster",
                "publication_generation": 1,
            }

            with (
                patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "DEMO_ACCESS_PATH", access_path),
                patch.object(web_server, "_RUNS", {}),
                patch.object(web_server, "new_run_id", return_value=published_run_id),
                patch.object(web_server, "_demo_user_id", return_value="local"),
                patch.object(web_server, "_run_owner_id", return_value="local"),
                patch.object(
                    web_server,
                    "_assert_controlled_run_source_usable",
                ),
                patch.object(
                    web_server,
                    "load_attempt_candidate",
                    return_value=candidate,
                ),
                patch.object(
                    web_server,
                    "_reserve_candidate_publish_bundle_binding",
                    return_value=bundle_binding,
                ),
                patch.object(
                    web_server,
                    "_settings_for_request",
                    side_effect=RuntimeError("canvas settings unavailable"),
                ),
                self.assertRaisesRegex(RuntimeError, "canvas settings unavailable"),
            ):
                asyncio.run(
                    web_server.publish_candidate_draft(
                        f"art_{draft_run_id}",
                        web_server.CandidateDraftPublishRequest(
                            conversation_id="conversation-canvas-pre-durable"
                        ),
                        request,
                    )
                )

            access = json.loads(access_path.read_text(encoding="utf-8"))["runs"]
            self.assertNotIn(published_run_id, access)
            self.assertNotIn(published_run_id, web_server._RUNS)

    def test_derived_access_release_cannot_delete_a_replaced_lease(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            access_path = Path(raw_tmp) / "demo_run_access.json"
            self._write_demo_access(access_path, "lease-parent")
            request = SimpleNamespace()
            with (
                patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
                patch.object(web_server, "DEMO_ACCESS_PATH", access_path),
                patch.object(web_server, "_demo_user_id", return_value="local"),
            ):
                first = web_server._demo_acquire_derived_run_access_lease(
                    "lease-child",
                    request,
                    parent_run_id="lease-parent",
                )
                second = web_server._demo_acquire_derived_run_access_lease(
                    "lease-child",
                    request,
                    parent_run_id="lease-parent",
                )
                web_server._demo_release_derived_run_access(first)
                access = json.loads(access_path.read_text(encoding="utf-8"))["runs"]
                self.assertEqual(access["lease-child"]["token"], second.token)
                web_server._demo_release_derived_run_access(second)

            access = json.loads(access_path.read_text(encoding="utf-8"))["runs"]
            self.assertNotIn("lease-child", access)

    def test_unclaimed_candidate_cleanup_does_not_cancel_foreign_control(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runs_dir = root / "runs"
            runs_dir.mkdir()
            run_id = "candidate-publish-foreign-claim"
            parent_run_id = "candidate-publish-foreign-parent"
            store = RunControlStore(runs_dir)
            record = store.reserve(
                run_id,
                "poster",
                parent_job_id=parent_run_id,
            )
            store.transition(run_id, record, "queued")
            access_path = root / "demo_run_access.json"
            self._write_demo_access(access_path, parent_run_id)
            request = SimpleNamespace()
            state = web_server._RunState(
                artifact_type="poster",
                brief="foreign claim cleanup",
            )

            class Services:
                def __init__(self) -> None:
                    self.cancel_calls = 0

                async def cancel(self, *_args):
                    self.cancel_calls += 1
                    return SimpleNamespace(
                        confirmed=True,
                        cancel_request_event_required=False,
                    )

            services = Services()
            with (
                patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "DEMO_ACCESS_PATH", access_path),
                patch.object(web_server, "_RUNS", {run_id: state}),
                patch.object(web_server, "_demo_user_id", return_value="local"),
                patch.object(
                    web_server,
                    "_web_run_runtime",
                    return_value=SimpleNamespace(services=services),
                ),
            ):
                first = web_server._demo_acquire_derived_run_access_lease(
                    run_id,
                    request,
                    parent_run_id=parent_run_id,
                )
                replacement = web_server._demo_acquire_derived_run_access_lease(
                    run_id,
                    request,
                    parent_run_id=parent_run_id,
                )
                asyncio.run(
                    web_server._cleanup_failed_candidate_publish_setup(
                        run_id=run_id,
                        state=state,
                        access_lease=first,
                        durable_handoff=False,
                        reservation_claimed=False,
                        discard_write_ahead=True,
                    )
                )

            self.assertEqual(services.cancel_calls, 0)
            self.assertEqual(store.read(run_id).state, "queued")
            self.assertNotIn(run_id, web_server._RUNS)
            access = json.loads(access_path.read_text(encoding="utf-8"))["runs"]
            self.assertEqual(access[run_id]["token"], replacement.token)

    def test_candidate_cleanup_survives_repeated_caller_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runs_dir = root / "runs"
            runs_dir.mkdir()
            run_id = "candidate-publish-cleanup-double-cancel"
            parent_run_id = "candidate-publish-cleanup-parent"
            store = RunControlStore(runs_dir)
            record = store.reserve(
                run_id,
                "poster",
                parent_job_id=parent_run_id,
            )
            store.transition(run_id, record, "queued")
            access_path = root / "demo_run_access.json"
            self._write_demo_access(access_path, parent_run_id)
            request = SimpleNamespace()
            state = web_server._RunState(
                artifact_type="poster",
                brief="cleanup cancellation barrier",
            )

            async def exercise() -> None:
                cancel_started = asyncio.Event()
                release_cancel = asyncio.Event()

                class Services:
                    async def cancel(self, controlled_run_id: str, _reason: str):
                        cancel_started.set()
                        await release_cancel.wait()
                        cancelling = store.request_cancel(controlled_run_id)
                        if cancelling.state == "cancelling":
                            store.finalize_cancel(
                                controlled_run_id,
                                {"termination_verified": True},
                            )
                        return SimpleNamespace(
                            confirmed=True,
                            cancel_request_event_required=False,
                        )

                with patch.object(
                    web_server,
                    "_web_run_runtime",
                    return_value=SimpleNamespace(services=Services()),
                ):
                    lease = web_server._demo_acquire_derived_run_access_lease(
                        run_id,
                        request,
                        parent_run_id=parent_run_id,
                    )
                    cleanup = asyncio.create_task(
                        web_server._cleanup_failed_candidate_publish_setup(
                            run_id=run_id,
                            state=state,
                            access_lease=lease,
                            durable_handoff=False,
                            reservation_claimed=True,
                            discard_write_ahead=True,
                        )
                    )
                    await cancel_started.wait()
                    cleanup.cancel()
                    await asyncio.sleep(0)
                    cleanup.cancel()
                    await asyncio.sleep(0.01)
                    self.assertFalse(cleanup.done())
                    self.assertIn(run_id, web_server._RUNS)
                    access = json.loads(
                        access_path.read_text(encoding="utf-8")
                    )["runs"]
                    self.assertIn(run_id, access)
                    release_cancel.set()
                    await cleanup

            with (
                patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "DEMO_ACCESS_PATH", access_path),
                patch.object(web_server, "_RUNS", {run_id: state}),
                patch.object(web_server, "_demo_user_id", return_value="local"),
            ):
                asyncio.run(exercise())

            self.assertEqual(store.read(run_id).state, "cancelled")
            self.assertNotIn(run_id, web_server._RUNS)
            access = json.loads(access_path.read_text(encoding="utf-8"))["runs"]
            self.assertNotIn(run_id, access)

    def _assert_post_handoff_cancellation_uses_durable_control(
        self,
        *,
        canvas: bool,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runs_dir = root / "runs"
            runs_dir.mkdir()
            source_run_id = f"source-{'canvas' if canvas else 'direct'}-handoff"
            draft_run_id = f"draft-canvas-handoff" if canvas else ""
            candidate = SimpleNamespace(
                artifact_type=SimpleNamespace(value="poster"),
                candidate_id=f"candidate-{'canvas' if canvas else 'direct'}-handoff",
                source_sha256=("f" if canvas else "a") * 64,
                source_relative_path="attempt_candidates/attempt_01/poster.html",
                safety_state="ready",
            )
            (runs_dir / source_run_id).mkdir()
            if canvas:
                draft_dir = runs_dir / draft_run_id
                draft_dir.mkdir()
                (draft_dir / "candidate_draft_lineage.json").write_text(
                    json.dumps({
                        "status": "draft",
                        "artifact_type": "poster",
                        "source_run_id": source_run_id,
                        "source_attempt": 1,
                        "source_candidate_id": candidate.candidate_id,
                        "source_candidate_sha256": candidate.source_sha256,
                    }),
                    encoding="utf-8",
                )
            access_path = root / "demo_run_access.json"
            access_runs = {
                source_run_id: {
                    "owner": "local",
                    "token": "source-handoff-token",
                    "created_at": time.time(),
                }
            }
            if canvas:
                access_runs[draft_run_id] = {
                    "owner": "local",
                    "token": "draft-handoff-token",
                    "created_at": time.time(),
                    "parent_run_id": source_run_id,
                }
            access_path.write_text(
                json.dumps({"v": 1, "runs": access_runs}),
                encoding="utf-8",
            )
            request = SimpleNamespace(
                headers={},
                cookies={},
                client=SimpleNamespace(host="127.0.0.1"),
            )
            if canvas:
                published_run_id = "publish-canvas-durable-handoff"
            else:
                idempotency_key = "direct-durable-handoff"
                idempotency_digest = hashlib.sha256(
                    json.dumps(
                        ["local", idempotency_key],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                published_run_id = (
                    f"candidate-publish-{idempotency_digest[:32]}"
                )
            control_store = RunControlStore(runs_dir)

            async def reserve_durable_child(*, request, state, descriptor, **_kwargs):
                record = control_store.reserve(
                    request.run_id,
                    state.artifact_type,
                    parent_job_id=request.parent_run_id,
                )
                control_store.transition(request.run_id, record, "queued")
                web_server.durable_replace_json(
                    runs_dir / request.run_id / "derived_job.json",
                    {
                        "version": 1,
                        **descriptor,
                        "run_id": request.run_id,
                        "parent_run_id": request.parent_run_id,
                        "artifact_type": state.artifact_type,
                        "conversation_id": state.conversation_id,
                        "baseline_artifact_json": (
                            state.baseline_artifact_json or ""
                        ),
                    },
                )
                return "durable-start-token"

            start_entered = threading.Event()

            async def wait_until_cancelled_after_handoff(*_args, **_kwargs):
                start_entered.set()
                await asyncio.Event().wait()

            class DurableServices:
                def __init__(self) -> None:
                    self.cancel_calls = 0

                async def cancel(self, run_id: str, _reason: str):
                    self.cancel_calls += 1
                    record = control_store.request_cancel(run_id)
                    if record.state == "cancelling":
                        record = control_store.finalize_cancel(
                            run_id,
                            {"termination_verified": True, "reason": "test"},
                        )
                    return SimpleNamespace(
                        confirmed=record.state in {"cancelled", "failed", "completed"}
                    )

            services = DurableServices()
            runtime = SimpleNamespace(
                control_store=control_store,
                services=services,
            )
            bundle_binding = {
                "paper_bundle_job_id": f"bundle-{'canvas' if canvas else 'direct'}-handoff",
                "paper_bundle_owner_id": "local",
                "paper_bundle_artifact_type": "poster",
                "publication_generation": 1,
            }
            common_patches = (
                patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "DEMO_ACCESS_PATH", access_path),
                patch.object(web_server, "_RUNS", {}),
                patch.object(web_server, "_demo_user_id", return_value="local"),
                patch.object(web_server, "_run_owner_id", return_value="local"),
                patch.object(
                    web_server,
                    "_assert_controlled_run_source_usable",
                ),
                patch.object(
                    web_server,
                    "load_attempt_candidate",
                    return_value=candidate,
                ),
                patch.object(
                    web_server,
                    "_reserve_candidate_publish_bundle_binding",
                    return_value=bundle_binding,
                ),
                patch.object(
                    web_server,
                    "_settings_for_request",
                    return_value=SimpleNamespace(),
                ),
                patch.object(
                    web_server,
                    "_start_supervised_derived_job",
                    side_effect=reserve_durable_child,
                ),
                patch.object(
                    web_server,
                    "_start_reserved_derived_job",
                    side_effect=wait_until_cancelled_after_handoff,
                ),
                patch.object(web_server, "_web_run_runtime", return_value=runtime),
                patch.object(web_server, "_append_event"),
            )
            with ExitStack() as stack:
                for context in common_patches:
                    stack.enter_context(context)

                async def cancel_endpoint_task(endpoint):
                    task = asyncio.create_task(endpoint)
                    for _ in range(1000):
                        if start_entered.is_set():
                            break
                        await asyncio.sleep(0.001)
                    self.assertTrue(start_entered.is_set())
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

                if canvas:
                    with patch.object(
                        web_server,
                        "new_run_id",
                        return_value=published_run_id,
                    ):
                        asyncio.run(
                            cancel_endpoint_task(
                                web_server.publish_candidate_draft(
                                    f"art_{draft_run_id}",
                                    web_server.CandidateDraftPublishRequest(
                                        conversation_id="conversation-canvas-handoff"
                                    ),
                                    request,
                                ),
                            )
                        )
                else:
                    asyncio.run(
                        cancel_endpoint_task(
                            web_server.publish_run_attempt(
                                source_run_id,
                                1,
                                web_server.DirectAttemptPublishRequest(
                                    conversation_id="conversation-direct-handoff",
                                    expected_candidate_sha256=candidate.source_sha256,
                                    idempotency_key=idempotency_key,
                                ),
                                request,
                            ),
                        )
                    )

            access = json.loads(access_path.read_text(encoding="utf-8"))["runs"]
            self.assertIn(published_run_id, access)
            self.assertNotIn(published_run_id, web_server._RUNS)
            self.assertTrue(
                (runs_dir / published_run_id / "derived_job.json").is_file()
            )
            self.assertTrue(
                (
                    runs_dir
                    / published_run_id
                    / "candidate_publish_request.json"
                ).is_file()
            )
            self.assertEqual(
                control_store.read(published_run_id).state,
                "cancelled",
            )
            self.assertGreaterEqual(services.cancel_calls, 1)

    def test_direct_publish_cancellation_after_durable_handoff_uses_control(
        self,
    ) -> None:
        self._assert_post_handoff_cancellation_uses_durable_control(canvas=False)

    def test_canvas_publish_cancellation_after_durable_handoff_uses_control(
        self,
    ) -> None:
        self._assert_post_handoff_cancellation_uses_durable_control(canvas=True)

    def test_cancelled_controlled_run_history_is_diagnostic_only(self) -> None:
        run_id = "cancelled-history"
        with tempfile.TemporaryDirectory() as raw_tmp:
            runs_dir = Path(raw_tmp) / "runs"
            final_dir = runs_dir / run_id / "final"
            final_dir.mkdir(parents=True)
            (final_dir / "index.html").write_text(
                "<html><body>partial landing</body></html>",
                encoding="utf-8",
            )
            store = RunControlStore(runs_dir)
            store.reserve(run_id, "landing")
            store.request_cancel(run_id)
            store.finalize_cancel(
                run_id,
                {"termination_verified": True, "reason": "test"},
            )
            with patch.object(web_server, "RUNS_DIR", runs_dir):
                full = web_server._conversation_from_disk_run(run_id)
                compact = web_server._conversation_from_disk_run(
                    run_id,
                    compact=True,
                )
                event_artifact = web_server._history_artifact_for_event(
                    run_id,
                    {"artifact_type": "landing"},
                )
                event_preview = web_server._history_artifact_preview_for_event(
                    run_id,
                    {"artifact_type": "landing"},
                )

        for conversation in (full, compact):
            self.assertIsNotNone(conversation)
            assert conversation is not None
            self.assertEqual(conversation["artifacts"], {})
            self.assertIsNone(conversation["active_artifact_id"])
            self.assertEqual(conversation["messages"][0]["status"], "error")
            self.assertEqual(
                conversation["messages"][0]["failure"]["status"],
                "cancelled",
            )
            self.assertNotIn("/api/files/runs/", json.dumps(conversation))
        self.assertIsNone(event_artifact)
        self.assertIsNone(event_preview)

    def test_cancelling_controlled_run_history_never_imports_partial_artifact(self) -> None:
        run_id = "cancelling-history"
        with tempfile.TemporaryDirectory() as raw_tmp:
            runs_dir = Path(raw_tmp) / "runs"
            final_dir = runs_dir / run_id / "final"
            final_dir.mkdir(parents=True)
            (final_dir / "index.html").write_text(
                "<html><body>partial landing</body></html>",
                encoding="utf-8",
            )
            store = RunControlStore(runs_dir)
            store.reserve(run_id, "landing")
            store.request_cancel(run_id)
            with patch.object(web_server, "RUNS_DIR", runs_dir):
                full = web_server._conversation_from_disk_run(run_id)
                compact = web_server._conversation_from_disk_run(
                    run_id,
                    compact=True,
                )

        for conversation in (full, compact):
            self.assertIsNotNone(conversation)
            assert conversation is not None
            self.assertEqual(conversation["artifacts"], {})
            self.assertIsNone(conversation["active_artifact_id"])
            self.assertEqual(
                conversation["messages"][0]["failure"]["status"],
                "cancelling",
            )
            self.assertNotIn("/api/files/runs/", json.dumps(conversation))

    def test_stale_running_controlled_run_history_never_imports_partial_artifact(self) -> None:
        run_id = "stale-running-history"
        with tempfile.TemporaryDirectory() as raw_tmp:
            runs_dir = Path(raw_tmp) / "runs"
            final_dir = runs_dir / run_id / "final"
            final_dir.mkdir(parents=True)
            (final_dir / "index.html").write_text(
                "<html><body>partial landing</body></html>",
                encoding="utf-8",
            )
            store = RunControlStore(runs_dir)
            record = store.reserve(run_id, "landing")
            record = store.transition(run_id, record, "queued")
            store.transition(run_id, record, "running")

            with patch.object(web_server, "RUNS_DIR", runs_dir):
                full = web_server._conversation_from_disk_run(run_id)
                compact = web_server._conversation_from_disk_run(
                    run_id,
                    compact=True,
                )

        for conversation in (full, compact):
            self.assertIsNotNone(conversation)
            assert conversation is not None
            self.assertEqual(conversation["artifacts"], {})
            self.assertIsNone(conversation["active_artifact_id"])
            self.assertEqual(
                conversation["messages"][0]["failure"]["status"],
                "error",
            )
            self.assertNotIn("/api/files/runs/", json.dumps(conversation))

    def test_history_returns_compact_merged_summaries_after_limit(self) -> None:
        stored = {
            "conversations": {
                "stored": {
                    "id": "stored",
                    "title": "Stored conversation",
                    "created_at": 10,
                    "updated_at": 200,
                    "messages": [{"id": "stored_msg", "role": "user", "text": "brief"}],
                    "artifacts": {
                        "art_stored": {
                            "artifact_id": "art_stored",
                            "name": "Stored poster",
                            "artifact_type": "poster",
                            "canvas": {"w": 1200, "h": 1600},
                            "preview_url": "data:image/png;base64," + "x" * 4096,
                            "layers": [{"native_html": "<section>full editor payload</section>"}],
                            "native_html": "<html>full artifact</html>",
                            "evidence": {"paper_text": "must not reach the history list"},
                        },
                    },
                    "active_artifact_id": "art_stored",
                },
                "old": {
                    "id": "old",
                    "title": "Older conversation",
                    "created_at": 1,
                    "updated_at": 100,
                    "messages": [],
                    "artifacts": {},
                    "active_artifact_id": None,
                },
            },
        }
        imported = {
            "imported": {
                "id": "imported",
                "title": "Imported conversation",
                "created_at": 20,
                "updated_at": 300,
                "messages": [{"id": "imported_msg", "role": "assistant", "text": "done"}],
                "artifacts": {
                    "art_imported": {
                        "artifact_id": "art_imported",
                        "name": "Imported poster",
                        "artifact_type": "poster",
                        "canvas": {"w": 1200, "h": 1600},
                        "layers": [{"native_html": "<section>full imported payload</section>"}],
                    },
                },
                "active_artifact_id": "art_imported",
            },
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            history_path = Path(raw_tmp) / "web_history.json"
            history_path.write_text(json.dumps(stored), encoding="utf-8")
            with (
                patch.object(web_server, "WEB_HISTORY_PATH", history_path),
                patch.object(web_server, "_RUN_ACCESS_CONTROL", False),
                patch.object(web_server, "_import_history_from_server_events", return_value=imported),
            ):
                response = web_server.history(SimpleNamespace(), limit=2)
                summaries = web_server._load_web_history_summaries()
                with patch.object(web_server, "_load_web_history", side_effect=AssertionError):
                    cached_summaries = web_server._load_web_history_summaries()

        self.assertEqual(list(response["conversations"]), ["imported", "stored"])
        self.assertEqual(response["conversations"]["stored"]["message_count"], 1)
        stored_preview = response["conversations"]["stored"]["artifacts"]["art_stored"]
        self.assertEqual(
            stored_preview,
            {
                "artifact_id": "art_stored",
                "name": "Stored poster",
                "artifact_type": "poster",
                "canvas": {"w": 1200, "h": 1600},
            },
        )
        self.assertNotIn("messages", response["conversations"]["stored"])
        self.assertNotIn("layers", response["conversations"]["imported"]["artifacts"]["art_imported"])
        self.assertEqual(summaries, cached_summaries)

    def test_history_detail_returns_full_stored_snapshot(self) -> None:
        artifact = {
            "artifact_id": "art_stored",
            "name": "Stored poster",
            "artifact_type": "poster",
            "canvas": {"w": 1200, "h": 1600},
            "layers": [{"layer_id": "title", "native_html": "<h1>Editable title</h1>"}],
            "native_html": "<html>full artifact</html>",
        }
        stored = {
            "conversations": {
                "stored": {
                    "id": "stored",
                    "title": "Stored conversation",
                    "created_at": 10,
                    "updated_at": 20,
                    "messages": [{"id": "msg", "role": "assistant", "text": "done"}],
                    "artifacts": {"art_stored": artifact},
                    "active_artifact_id": "art_stored",
                },
            },
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            history_path = Path(raw_tmp) / "web_history.json"
            history_path.write_text(json.dumps(stored), encoding="utf-8")
            with (
                patch.object(web_server, "WEB_HISTORY_PATH", history_path),
                patch.object(web_server, "_RUN_ACCESS_CONTROL", False),
            ):
                response = web_server.history_conversation_detail("stored", SimpleNamespace())

        self.assertFalse(response["user_isolated"])
        self.assertEqual(
            response["conversation"]["artifacts"]["art_stored"]["layers"],
            artifact["layers"],
        )
        self.assertEqual(
            response["conversation"]["artifacts"]["art_stored"]["native_html"],
            artifact["native_html"],
        )

    def test_compact_disk_history_does_not_build_full_artifact(self) -> None:
        run_id = "compact_run"
        with tempfile.TemporaryDirectory() as raw_tmp:
            runs_dir = Path(raw_tmp) / "runs"
            final_dir = runs_dir / run_id / "final"
            final_dir.mkdir(parents=True)
            (final_dir / "poster.html").write_text("<html><body>poster</body></html>", encoding="utf-8")
            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "_build_artifact_response", side_effect=AssertionError),
            ):
                conversation = web_server._conversation_from_disk_run(run_id, compact=True)

        self.assertIsNotNone(conversation)
        assert conversation is not None
        artifact = conversation["artifacts"][f"art_{run_id}"]
        self.assertNotIn("layers", artifact)

    def test_compact_history_import_reads_only_the_top_summary_window(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            runs_dir = Path(raw_tmp) / "runs"
            run_ids = ["new", "middle", "old"]
            now = time.time()
            for offset, run_id in enumerate(run_ids):
                run_dir = runs_dir / run_id
                run_dir.mkdir(parents=True)
                stamp = now - offset
                os.utime(run_dir, (stamp, stamp))

            calls: list[str] = []

            def conversation_for_run(run_id: str, *, compact: bool = False) -> dict[str, object]:
                calls.append(run_id)
                return {
                    "id": f"server_run_{run_id}",
                    "title": run_id,
                    "created_at": 1,
                    "updated_at": 1,
                    "messages": [],
                    "artifacts": {},
                    "active_artifact_id": None,
                }

            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "_conversation_from_disk_run", side_effect=conversation_for_run),
            ):
                imported = web_server._import_history_from_server_events(
                    limit=None,
                    include_design_sessions=False,
                    demo_user_id=None,
                    compact=True,
                    summary_limit=1,
                )

        self.assertEqual(calls, ["new"])
        self.assertEqual(set(imported), {"server_run_new"})

    def test_compact_history_import_keeps_demo_runs_user_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runs_dir = root / "runs"
            for run_id in ("owned", "other"):
                (runs_dir / run_id).mkdir(parents=True)
            access_path = root / "demo_run_access.json"
            access_path.write_text(
                json.dumps({
                    "v": 1,
                    "runs": {
                        "owned": {"owner": "user:viewer"},
                        "other": {"owner": "user:someone_else"},
                    },
                }),
                encoding="utf-8",
            )

            def conversation_for_run(run_id: str, *, compact: bool = False) -> dict[str, object]:
                return {
                    "id": f"server_run_{run_id}",
                    "title": run_id,
                    "created_at": 1,
                    "updated_at": 1,
                    "messages": [],
                    "artifacts": {},
                    "active_artifact_id": None,
                }

            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "DEMO_ACCESS_PATH", access_path),
                patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
                patch.object(web_server, "_conversation_from_disk_run", side_effect=conversation_for_run),
            ):
                imported = web_server._import_history_from_server_events(
                    limit=None,
                    include_design_sessions=False,
                    demo_user_id="user:viewer",
                    compact=True,
                    summary_limit=25,
                )

        self.assertEqual(set(imported), {"server_run_owned"})

    def test_requested_video_type_wins_over_an_incorrect_runner_result(self) -> None:
        result = SimpleNamespace(
            run_id="run_video_contract",
            artifact_type="poster",
        )

        with patch.object(
            web_server,
            "_final_render_complete_on_disk",
            return_value=False,
        ):
            artifact_type = web_server._coerce_result_artifact_type(
                result,
                fallback="video",
            )

        self.assertEqual(artifact_type, "video")

    def test_disk_import_restores_recent_in_progress_run(self) -> None:
        run_id = "run_in_progress"
        with tempfile.TemporaryDirectory() as raw_tmp:
            out_dir = Path(raw_tmp)
            run_dir = out_dir / "runs" / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "canvas_plan.json").write_text(
                json.dumps({"artifact_type": "deck"}),
                encoding="utf-8",
            )
            (run_dir / "run_events.jsonl").write_text(
                json.dumps({
                    "run_id": run_id,
                    "event": "run.start",
                    "pid": os.getpid(),
                    "ts_epoch_ms": 10,
                }) + "\n",
                encoding="utf-8",
            )
            active_task = SimpleNamespace(done=lambda: False)
            active_state = SimpleNamespace(task=active_task, queued=False)
            with (
                patch.object(web_server, "RUNS_DIR", out_dir / "runs"),
                patch.dict(web_server._RUNS, {run_id: active_state}, clear=True),
            ):
                conversation = web_server._conversation_from_disk_run(run_id)

        self.assertIsNotNone(conversation)
        assert conversation is not None
        self.assertTrue(conversation["pending"])
        self.assertEqual(conversation["run_id"], run_id)
        self.assertEqual(conversation["messages"][0]["status"], "streaming")
        self.assertEqual(
            conversation["messages"][0]["task_payload"],
            {"artifact_type": "deck"},
        )

    def test_disk_import_keeps_active_video_pending_with_intermediate_landing(self) -> None:
        run_id = "run_video_with_landing"
        with tempfile.TemporaryDirectory() as raw_tmp:
            out_dir = Path(raw_tmp)
            run_dir = out_dir / "runs" / run_id
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            (final_dir / "index.html").write_text(
                "<!doctype html><html><body><main>Intermediate video layout</main></body></html>",
                encoding="utf-8",
            )
            (run_dir / "canvas_plan.json").write_text(
                json.dumps({"artifact_type": "video"}),
                encoding="utf-8",
            )
            (run_dir / "run_events.jsonl").write_text(
                json.dumps({
                    "run_id": run_id,
                    "event": "run.start",
                    "pid": os.getpid(),
                }) + "\n",
                encoding="utf-8",
            )
            active_task = SimpleNamespace(done=lambda: False)
            active_state = SimpleNamespace(task=active_task, queued=False)
            with (
                patch.object(web_server, "RUNS_DIR", out_dir / "runs"),
                patch.dict(web_server._RUNS, {run_id: active_state}, clear=True),
            ):
                conversation = web_server._conversation_from_disk_run(run_id)

        self.assertIsNotNone(conversation)
        assert conversation is not None
        self.assertTrue(conversation["pending"])
        self.assertEqual(
            conversation["messages"][0]["task_payload"],
            {"artifact_type": "video"},
        )
        self.assertEqual(conversation["artifacts"], {})

    def test_recent_nonterminal_log_without_live_owner_is_not_recoverable(self) -> None:
        run_id = "run_abandoned"
        with tempfile.TemporaryDirectory() as raw_tmp:
            run_dir = Path(raw_tmp) / run_id
            run_dir.mkdir()
            (run_dir / "run_events.jsonl").write_text(
                json.dumps({
                    "run_id": run_id,
                    "event": "run.start",
                    "pid": os.getpid(),
                }) + "\n",
                encoding="utf-8",
            )
            finished_task = SimpleNamespace(done=lambda: True)
            finished_state = SimpleNamespace(task=finished_task, queued=False)
            with patch.dict(
                web_server._RUNS,
                {run_id: finished_state},
                clear=True,
            ):
                self.assertFalse(web_server._disk_run_is_recoverable(run_dir))

    def test_disk_import_does_not_restore_terminal_run_without_artifact(self) -> None:
        run_id = "run_failed"
        with tempfile.TemporaryDirectory() as raw_tmp:
            out_dir = Path(raw_tmp)
            run_dir = out_dir / "runs" / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "resume_state.json").write_text(
                json.dumps({"artifact_type": "poster"}),
                encoding="utf-8",
            )
            (run_dir / "run_events.jsonl").write_text(
                "\n".join([
                    json.dumps({"event": "run.start"}),
                    json.dumps({"event": "run.error"}),
                ]) + "\n",
                encoding="utf-8",
            )
            with patch.object(web_server, "RUNS_DIR", out_dir / "runs"):
                conversation = web_server._conversation_from_disk_run(run_id)

        self.assertIsNone(conversation)

    def test_disk_event_reader_replays_events_after_offset(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = Path(raw_tmp) / "run_events.jsonl"
            path.write_text(
                json.dumps({"event": "run.start"}) + "\n",
                encoding="utf-8",
            )
            offset, events = web_server._read_disk_run_events(path, 0)
            self.assertEqual([event["event"] for event in events], ["run.start"])

            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event": "run.done"}) + "\n")
            next_offset, events = web_server._read_disk_run_events(path, offset)

        self.assertGreater(next_offset, offset)
        self.assertEqual([event["event"] for event in events], ["run.done"])

    def test_history_merge_preserves_imported_active_run_state(self) -> None:
        run_id = "run_active"
        imported = {
            "server_run_active": {
                "id": "server_run_active",
                "title": "Poster - running",
                "created_at": 10,
                "updated_at": 20,
                "messages": [],
                "artifacts": {},
                "active_artifact_id": None,
                "pending": True,
                "run_id": run_id,
            },
        }

        merged = web_server._merge_history_conversations({}, imported)

        self.assertTrue(merged["server_run_active"]["pending"])
        self.assertEqual(merged["server_run_active"]["run_id"], run_id)

    def test_candidate_derived_runs_are_not_imported_as_standalone_history(
        self,
    ) -> None:
        for status in ("draft", "published"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as raw_tmp:
                runs_dir = Path(raw_tmp) / "runs"
                run_id = f"candidate_{status}"
                run_dir = runs_dir / run_id
                final_dir = run_dir / "final"
                final_dir.mkdir(parents=True)
                (final_dir / "poster.html").write_text(
                    "<!doctype html><main class='paper-poster'>Draft</main>",
                    encoding="utf-8",
                )
                (run_dir / "candidate_draft_lineage.json").write_text(
                    json.dumps({
                        "schema_version": 1,
                        "status": status,
                        "artifact_type": "poster",
                        "source_run_id": "source-run",
                        "source_attempt": 1,
                        "source_candidate_id": "poster-attempt-01",
                    }),
                    encoding="utf-8",
                )
                with (
                    patch.object(web_server, "RUNS_DIR", runs_dir),
                    patch.object(
                        web_server,
                        "_build_artifact_response",
                        return_value=_artifact(run_id),
                    ),
                ):
                    conversation = web_server._conversation_from_disk_run(run_id)

                self.assertIsNone(conversation)

    def test_active_candidate_publish_restores_original_conversation_metadata(
        self,
    ) -> None:
        run_id = "candidate-publish-running"
        conversation_id = "conversation-with-pending-publish"
        source_artifact_id = "art_source-run"
        source_run_id = "source-run"
        source_candidate_id = "poster-attempt-02"
        with tempfile.TemporaryDirectory() as raw_tmp:
            runs_dir = Path(raw_tmp) / "runs"
            run_dir = runs_dir / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "derived_job.json").write_text(
                json.dumps({
                    "version": 1,
                    "job_kind": "candidate_publish",
                    "run_id": run_id,
                    "parent_run_id": "candidate-draft",
                    "artifact_type": "poster",
                    "conversation_id": conversation_id,
                    "baseline_artifact_json": "",
                    "source_artifact_id": source_artifact_id,
                    "artifact_name": "Published poster candidate",
                    "source_relative_path": "candidate_draft_lineage.json",
                }),
                encoding="utf-8",
            )
            (run_dir / "candidate_draft_lineage.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "status": "validated",
                    "artifact_type": "poster",
                    "source_run_id": source_run_id,
                    "source_attempt": 2,
                    "source_candidate_id": source_candidate_id,
                    "source_candidate_sha256": "a" * 64,
                    "conversation_id": conversation_id,
                }),
                encoding="utf-8",
            )
            (run_dir / "run_events.jsonl").write_text(
                json.dumps({
                    "run_id": run_id,
                    "event": "run.start",
                    "pid": os.getpid(),
                }) + "\n",
                encoding="utf-8",
            )
            store = RunControlStore(runs_dir)
            record = store.reserve(
                run_id,
                "poster",
                parent_job_id="candidate-draft",
            )
            record = store.transition(run_id, record, "queued")
            store.transition(run_id, record, "running")
            active_task = SimpleNamespace(done=lambda: False)
            active_state = SimpleNamespace(
                artifact_type="poster",
                task=active_task,
                queued=False,
            )

            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.dict(web_server._RUNS, {run_id: active_state}, clear=True),
            ):
                conversation = web_server._conversation_from_disk_run(run_id)

        self.assertIsNotNone(conversation)
        assert conversation is not None
        self.assertEqual(conversation["id"], conversation_id)
        self.assertTrue(conversation["pending"])
        self.assertEqual(conversation["run_id"], run_id)
        message = conversation["messages"][0]
        task_payload = message["task_payload"]
        self.assertEqual(
            {
                "job_kind": message["task_type"],
                "run_id": message["run_id"],
                "conversation_id": conversation["id"],
                "artifact_type": task_payload["artifact_type"],
                "source_artifact_id": task_payload["source_artifact_id"],
                "source_run_id": task_payload["source_run_id"],
                "source_candidate_id": task_payload["source_candidate_id"],
                "status": message["status"],
            },
            {
                "job_kind": "candidate_publish",
                "run_id": run_id,
                "conversation_id": conversation_id,
                "artifact_type": "poster",
                "source_artifact_id": source_artifact_id,
                "source_run_id": source_run_id,
                "source_candidate_id": source_candidate_id,
                "status": "streaming",
            },
        )
        summary = web_server._history_conversation_summary(conversation)
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary["pending_task_type"], "candidate_publish")
        self.assertEqual(summary["pending_task_payload"], task_payload)

    def test_recent_run_ids_include_active_run_outside_completed_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            runs_dir = Path(raw_tmp) / "runs"
            active = runs_dir / "active"
            completed = runs_dir / "completed"
            active.mkdir(parents=True)
            completed.mkdir(parents=True)
            (active / "run_events.jsonl").write_text(
                json.dumps({"event": "run.start", "pid": os.getpid()}) + "\n",
                encoding="utf-8",
            )
            (completed / "run_events.jsonl").write_text(
                json.dumps({"event": "run.done"}) + "\n",
                encoding="utf-8",
            )
            now = time.time()
            os.utime(active, (now - 100, now - 100))
            os.utime(completed, (now, now))
            active_task = SimpleNamespace(done=lambda: False)
            active_state = SimpleNamespace(task=active_task, queued=False)
            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.dict(web_server._RUNS, {"active": active_state}, clear=True),
            ):
                run_ids = web_server._recent_disk_run_ids(limit=1)

        self.assertIn("active", run_ids)
        self.assertIn("completed", run_ids)

    def test_disk_import_treats_soft_accepted_poster_as_clean_success(self) -> None:
        run_id = "run_soft_accept"
        with tempfile.TemporaryDirectory() as raw_tmp:
            out_dir = Path(raw_tmp)
            run_dir = out_dir / "runs" / run_id
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            (final_dir / "poster.html").write_text("<html></html>", encoding="utf-8")
            (final_dir / "designer_author_direct_manifest.json").write_text(
                json.dumps({
                    "source": "external_designer_author",
                    "acceptance_path": "soft_accept",
                }),
                encoding="utf-8",
            )
            (run_dir / "run_events.jsonl").write_text(
                json.dumps({
                    "event": "run.done",
                    "terminal_status": "fail",
                    "critic_verdict": "fail",
                    "critic_score": 0.0,
                    "wall_s": 120.0,
                }) + "\n",
                encoding="utf-8",
            )
            with (
                patch.object(web_server, "RUNS_DIR", out_dir / "runs"),
                patch.object(web_server, "_settings_or_boot", return_value=out_dir),
                patch.object(web_server, "_detect_artifact_type_for_run", return_value="poster"),
                patch.object(web_server, "_build_artifact_response", return_value=_artifact(run_id)),
            ):
                conversation = web_server._conversation_from_disk_run(run_id)

        message = conversation["messages"][0]
        self.assertEqual(message["status"], "done")
        self.assertNotIn("failure", message)

    def test_disk_import_hides_internal_degraded_status_for_published_artifact(self) -> None:
        run_id = "run_degraded"
        with tempfile.TemporaryDirectory() as raw_tmp:
            out_dir = Path(raw_tmp)
            run_dir = out_dir / "runs" / run_id
            final_dir = run_dir / "final"
            final_dir.mkdir(parents=True)
            (final_dir / "poster.html").write_text("<html></html>", encoding="utf-8")
            (run_dir / "run_events.jsonl").write_text(
                "\n".join([
                    json.dumps({"event": "run.start", "attachments": 1}),
                    json.dumps({
                        "event": "run.done",
                        "terminal_status": "revise",
                        "critic_score": 0.63,
                        "wall_s": 3296.02,
                        "designer_model": "gpt-5.4",
                    }),
                ]) + "\n",
                encoding="utf-8",
            )
            (run_dir / "run_telemetry_summary.json").write_text(
                json.dumps({
                    "terminal_status": "revise",
                    "run_done_wall_s": 3296.02,
                }),
                encoding="utf-8",
            )
            with (
                patch.object(web_server, "RUNS_DIR", out_dir / "runs"),
                patch.object(web_server, "_settings_or_boot", return_value=out_dir),
                patch.object(web_server, "_detect_artifact_type_for_run", return_value="poster"),
                patch.object(web_server, "_build_artifact_response", return_value=_artifact(run_id)),
            ):
                conversation = web_server._conversation_from_disk_run(run_id)

        message = conversation["messages"][0]
        self.assertEqual(message["status"], "done")
        self.assertEqual(message["artifact_id"], f"art_{run_id}")
        self.assertNotIn("failure", message)

    def test_disk_import_uses_telemetry_fallback_but_keeps_clean_runs_clean(self) -> None:
        for status in ("max_turns", "pass"):
            with self.subTest(status=status):
                run_id = f"run_{status}"
                with tempfile.TemporaryDirectory() as raw_tmp:
                    out_dir = Path(raw_tmp)
                    run_dir = out_dir / "runs" / run_id
                    (run_dir / "final").mkdir(parents=True)
                    (run_dir / "run_telemetry_summary.json").write_text(
                        json.dumps({
                            "terminal_status": status,
                            "run_done_wall_s": 12.5,
                        }),
                        encoding="utf-8",
                    )
                    with (
                        patch.object(web_server, "RUNS_DIR", out_dir / "runs"),
                        patch.object(web_server, "_settings_or_boot", return_value=out_dir),
                        patch.object(web_server, "_detect_artifact_type_for_run", return_value="poster"),
                        patch.object(web_server, "_build_artifact_response", return_value=_artifact(run_id)),
                    ):
                        conversation = web_server._conversation_from_disk_run(run_id)

                message = conversation["messages"][0]
                self.assertNotIn("failure", message)

    def test_design_event_hides_degraded_metadata_on_rendered_artifact(self) -> None:
        run_id = "run_event_degraded"
        artifact = web_server._dump_model(_artifact(run_id))
        event = {
            "event": "artifact.generated",
            "run_id": run_id,
            "_ts_ms": 10,
            "data": {
                "artifact_type": "poster",
                "terminal_status": "max_turns",
                "critic_verdict": "fail",
                "critic_score": 0.41,
                "wall_s": 4370.24,
            },
        }
        with patch.object(web_server, "_history_artifact_for_event", return_value=artifact):
            conversation = web_server._conversation_from_design_events(
                "conv_degraded",
                [event],
                set(),
            )

        message = conversation["messages"][0]
        self.assertEqual(message["status"], "done")
        self.assertNotIn("failure", message)

    def test_design_event_does_not_invent_warning_for_pass(self) -> None:
        run_id = "run_event_pass"
        artifact = web_server._dump_model(_artifact(run_id))
        event = {
            "event": "artifact.generated",
            "run_id": run_id,
            "_ts_ms": 10,
            "data": {
                "artifact_type": "poster",
                "terminal_status": "pass",
                "critic_verdict": "pass",
                "critic_score": 0.92,
                "wall_s": 20.0,
            },
        }
        with patch.object(web_server, "_history_artifact_for_event", return_value=artifact):
            conversation = web_server._conversation_from_design_events(
                "conv_pass",
                [event],
                set(),
            )

        self.assertNotIn("failure", conversation["messages"][0])

    def test_worker_exit_failure_fields_survive_controlled_cold_history(self) -> None:
        run_id = "worker-exit-cold-history"
        with tempfile.TemporaryDirectory() as raw_tmp:
            runs_dir = Path(raw_tmp) / "runs"
            store = RunControlStore(runs_dir)
            record = store.reserve(run_id, "poster", initial_state="queued")
            record = store.transition(run_id, record, "running")
            record = store.transition(run_id, record, "completing")
            record = store.transition(
                run_id,
                record,
                "failed",
                publishable=False,
            )
            run_dir = runs_dir / run_id
            (run_dir / "run_events.jsonl").write_text(
                json.dumps({
                    "run_id": run_id,
                    "event": "worker.exit",
                    "version": 1,
                    "returncode": 17,
                    "error_code": "worker_result_missing",
                    "error_message": "Worker stopped before writing its result.",
                    "error_detail": "exit=17; final-root-cause",
                    "protocol_error": "worker_result.json is missing",
                    "last_event": "designer.author",
                    "last_worker_seq": 4,
                    "last_phase": "authoring",
                    "last_reason": "process_exit",
                    "stdout_tail": "",
                    "stderr_tail": "final-root-cause",
                }) + "\n",
                encoding="utf-8",
            )

            with patch.object(web_server, "RUNS_DIR", runs_dir):
                controlled = web_server._controlled_terminal_diagnostic_response(
                    run_id,
                    None,
                )
                history = web_server._history_control_diagnostic_conversation(
                    run_id,
                    record,
                )

        self.assertIsNotNone(controlled)
        assert controlled is not None
        controlled_failure = controlled.message.failure
        self.assertIsNotNone(controlled_failure)
        assert controlled_failure is not None
        history_failure = history["messages"][0]["failure"]
        self.assertEqual(controlled_failure.error_code, "worker_result_missing")
        self.assertEqual(history_failure["error_code"], "worker_result_missing")
        self.assertEqual(controlled_failure.error_message, history_failure["error_message"])
        self.assertEqual(controlled_failure.error_detail, history_failure["error_detail"])
        self.assertEqual(controlled_failure.phase, "authoring")
        self.assertEqual(history_failure["phase"], "authoring")

    def test_design_event_reconstruction_preserves_worker_exit_fields(self) -> None:
        run_id = "worker-exit-design-event"
        event = {
            "event": "artifact.generation_failed",
            "run_id": run_id,
            "_ts_ms": 10,
            "data": {
                "artifact_type": "poster",
                "status": "error",
                "failure": {
                    "status": "error",
                    "phase": "authoring",
                    "error_code": "worker_result_missing",
                    "error_message": "Worker stopped before writing its result.",
                    "error_detail": "exit=17; final-root-cause",
                },
            },
        }
        with patch.object(web_server, "_history_artifact_for_event", return_value=None):
            conversation = web_server._conversation_from_design_events(
                "conv_worker_exit",
                [event],
                set(),
            )

        failure = conversation["messages"][0]["failure"]
        self.assertEqual(failure["error_code"], "worker_result_missing")
        self.assertEqual(
            failure["error_message"],
            "Worker stopped before writing its result.",
        )
        self.assertEqual(failure["error_detail"], "exit=17; final-root-cause")

    def test_history_summary_strips_editable_payloads(self) -> None:
        raw = {
            "id": "conv_heavy",
            "title": "Heavy poster",
            "created_at": 10,
            "updated_at": 20,
            "messages": [{
                "id": "msg_1",
                "text": "Generate a poster.",
                "run_id": "run_heavy",
                "artifact_id": "art_heavy",
                "status": "done",
            }],
            "artifacts": {
                "art_heavy": {
                    "artifact_id": "art_heavy",
                    "name": "Poster",
                    "artifact_type": "poster",
                    "canvas": {"w": 1200, "h": 1600},
                    "preview_url": "/api/files/runs/heavy/final/preview.png",
                    "layers": [{"native_html": "<main>large editable payload</main>"}],
                    "native_html": "<html>large editable payload</html>",
                    "evidence": {"full": "source evidence"},
                },
            },
            "active_artifact_id": "art_heavy",
        }

        summary = web_server._history_conversation_summary(raw)

        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary["message_count"], 1)
        self.assertNotIn("messages", summary)
        preview = summary["artifacts"]["art_heavy"]
        self.assertEqual(preview["preview_url"], "/api/files/runs/heavy/final/preview.png")
        self.assertNotIn("layers", preview)
        self.assertNotIn("native_html", preview)
        self.assertNotIn("evidence", preview)
        self.assertEqual(
            summary["last_run"],
            {
                "run_id": "run_heavy",
                "status": "done",
                "artifact_id": "art_heavy",
            },
        )

    def test_history_summary_index_avoids_reloading_full_snapshot(self) -> None:
        raw = {
            "v": 1,
            "conversations": {
                "conv_indexed": {
                    "id": "conv_indexed",
                    "title": "Indexed poster",
                    "created_at": 10,
                    "updated_at": 20,
                    "messages": [{"id": "msg_1", "text": "Generate a poster."}],
                    "artifacts": {},
                    "active_artifact_id": None,
                },
            },
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            history_path = Path(raw_tmp) / "web_history.json"
            with patch.object(web_server, "WEB_HISTORY_PATH", history_path):
                web_server._write_web_history(raw)
                with patch.object(
                    web_server,
                    "_load_web_history",
                    side_effect=AssertionError("summary index should be used"),
                ):
                    summaries = web_server._load_web_history_summaries()

        self.assertEqual(summaries["conv_indexed"]["title"], "Indexed poster")

    def test_history_limit_is_applied_after_stored_and_imported_merge(self) -> None:
        stored = {
            "conv_stored": {
                "id": "conv_stored",
                "title": "Stored",
                "created_at": 10,
                "updated_at": 100,
                "messages": [],
                "artifacts": {},
                "active_artifact_id": None,
            },
        }
        imported = {
            "server_run_new": {
                "id": "server_run_new",
                "title": "Imported",
                "created_at": 20,
                "updated_at": 200,
                "messages": [],
                "artifacts": {},
                "active_artifact_id": None,
            },
        }
        with (
            patch.object(web_server, "_RUN_ACCESS_CONTROL", False),
            patch.object(web_server, "_load_web_history_summaries", return_value=stored),
            patch.object(
                web_server,
                "_import_history_from_server_events",
                return_value=imported,
            ) as import_history,
        ):
            result = web_server.history(SimpleNamespace(), limit=1)

        self.assertEqual(list(result["conversations"]), ["server_run_new"])
        self.assertIsNone(import_history.call_args.kwargs["limit"])

    def test_history_detail_keeps_full_editable_conversation(self) -> None:
        raw = {
            "v": 1,
            "conversations": {
                "conv_detail": {
                    "id": "conv_detail",
                    "title": "Editable poster",
                    "created_at": 10,
                    "updated_at": 20,
                    "messages": [{"id": "msg_1", "text": "Generate a poster."}],
                    "artifacts": {
                        "art_detail": {
                            "artifact_id": "art_detail",
                            "name": "Poster",
                            "artifact_type": "poster",
                            "canvas": {"w": 1200, "h": 1600},
                            "layers": [{"layer_id": "title", "text": "Editable title"}],
                        },
                    },
                    "active_artifact_id": "art_detail",
                },
            },
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            history_path = Path(raw_tmp) / "web_history.json"
            history_path.write_text(json.dumps(raw), encoding="utf-8")
            with (
                patch.object(web_server, "WEB_HISTORY_PATH", history_path),
                patch.object(web_server, "_RUN_ACCESS_CONTROL", False),
            ):
                result = web_server.history_conversation_detail(
                    "conv_detail",
                    SimpleNamespace(),
                )

        artifact = result["conversation"]["artifacts"]["art_detail"]
        self.assertEqual(artifact["layers"][0]["text"], "Editable title")

    def test_history_detail_rejects_unowned_demo_run(self) -> None:
        with (
            patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
            patch.object(web_server, "_demo_user_id", return_value="user:viewer"),
            patch.object(web_server, "_demo_user_owns_run", return_value=False),
        ):
            with self.assertRaises(web_server.HTTPException) as raised:
                web_server.history_conversation_detail(
                    "server_run_someone_else",
                    SimpleNamespace(),
                )

        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
