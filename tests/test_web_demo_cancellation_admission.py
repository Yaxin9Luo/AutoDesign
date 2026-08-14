from __future__ import annotations

import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, Request

from autodesign.config import Settings
from scripts import web_server


class WebDemoCancellationAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out_dir = self.root / "out"
        self.runs_dir = self.out_dir / "runs"
        self.uploads_dir = self.out_dir / "uploads"
        self.runs_dir.mkdir(parents=True)
        self.uploads_dir.mkdir(parents=True)
        self.usage_path = self.root / "demo_usage.json"
        self.paper_path = self.root / "paper.pdf"
        self.paper_path.write_bytes(b"test paper")
        self.settings = Settings(
            anthropic_api_key="",
            anthropic_base_url=None,
            gemini_api_key="",
            designer_model="designer",
            critic_model="critic",
            repo_root=Path(__file__).resolve().parents[1],
            out_dir=self.out_dir,
        )
        web_server._RUNS.clear()
        web_server._DEMO_RUN_QUEUE = None

        async def latest_attachments(_conversation_id: str | None) -> list[Path]:
            return [self.paper_path]

        async def reserve_pipeline_worker(*, run_id: str, state: object, **_kwargs: object) -> None:
            state.reservation_token = f"token-{run_id}"

        self._patches = ExitStack()
        self._patches.enter_context(patch.object(web_server, "RUNS_DIR", self.runs_dir))
        self._patches.enter_context(patch.object(web_server, "UPLOADS_DIR", self.uploads_dir))
        self._patches.enter_context(patch.object(web_server, "DEMO_USAGE_PATH", self.usage_path))
        self._patches.enter_context(patch.object(web_server, "_DEMO_MODE", True))
        self._patches.enter_context(patch.object(web_server, "_RUN_ACCESS_CONTROL", False))
        self._patches.enter_context(patch.object(web_server, "_DEMO_DAILY_LIMIT", 20))
        self._patches.enter_context(patch.object(web_server, "_demo_require_host_safety"))
        self._patches.enter_context(patch.object(web_server, "_demo_cleanup_expired_runs"))
        self._patches.enter_context(patch.object(web_server, "_require_artifact_runtime"))
        self._patches.enter_context(
            patch.object(web_server, "_settings_for_request", return_value=self.settings)
        )
        self._patches.enter_context(
            patch.object(web_server, "_latest_conversation_attach_paths", new=latest_attachments)
        )
        self._patches.enter_context(
            patch.object(web_server, "_web_paper_poster_settings", side_effect=lambda value: value)
        )
        self._patches.enter_context(
            patch.object(
                web_server,
                "_paper_poster_author_cmd_resolution",
                return_value={
                    "available": True,
                    "cmd": "test-author",
                    "source": "test",
                    "message": "",
                },
            )
        )
        self._patches.enter_context(patch.object(web_server, "_append_event"))
        self._patches.enter_context(patch.object(web_server, "log"))
        self._patches.enter_context(
            patch.object(
                web_server,
                "_reserve_legacy_pipeline_worker",
                new=AsyncMock(side_effect=reserve_pipeline_worker),
            )
        )
        self._patches.enter_context(
            patch(
                "autodesign.util.paper_source_sanity.assert_valid_paper_source_pdf"
            )
        )

    async def asyncTearDown(self) -> None:
        pending = [
            state.task
            for state in web_server._RUNS.values()
            if state.task is not None and not state.task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        web_server._RUNS.clear()
        web_server._DEMO_RUN_QUEUE = None
        self._patches.close()
        self._tmp.cleanup()

    def _request(self, *, reserve_only: bool = False, token: str = "") -> Request:
        headers: list[tuple[bytes, bytes]] = []
        if reserve_only:
            headers.append((b"x-autodesign-reserve-only", b"1"))
        if token:
            headers.append((b"x-autodesign-upload-token", token.encode("utf-8")))
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/",
                "headers": headers,
                "client": ("203.0.113.7", 4242),
                "server": ("testserver", 80),
                "scheme": "http",
                "query_string": b"",
            }
        )

    def _quota_counts(self) -> tuple[int, int]:
        if not self.usage_path.is_file():
            return (0, 0)
        data = json.loads(self.usage_path.read_text(encoding="utf-8"))
        day = data["days"][web_server._demo_today()]
        return (
            sum(int(value) for value in day["users"].values()),
            sum(int(value) for value in day["ips"].values()),
        )

    def _install_reserved_state(self, run_id: str) -> web_server._RunState:
        state = web_server._RunState(
            "poster",
            brief="Create a paper poster.",
            attach_paths=[self.paper_path],
            conversation_id=f"conversation-{run_id}",
        )
        state.demo_user_id = ""
        state.reservation_token = f"token-{run_id}"
        web_server._RUNS[run_id] = state
        return state

    async def _generate_reserve_only(self, run_id: str):
        with patch.object(web_server, "new_run_id", return_value=run_id):
            return await web_server.generate(
                request=self._request(reserve_only=True),
                brief="Create a paper poster.",
                artifact_type="poster",
                palette_id="plum_sage",
                baseline_artifact=None,
                conversation_history=None,
                prior_artifacts=None,
                attachment_refs=None,
                reference_poster_ref=None,
                conversation_id=None,
                template=None,
                authoring_max_attempts=None,
                files=[],
                reference_poster=None,
            )

    async def test_reserve_only_generate_charges_once_when_started(self) -> None:
        """Break caught: reservation and start each consuming one demo quota."""
        web_server._DEMO_RUN_QUEUE = asyncio.Queue(maxsize=2)

        acknowledgement = await self._generate_reserve_only("generate-reserved")
        after_reserve = self._quota_counts()
        await web_server.start_run(
            acknowledgement.run_id,
            self._request(token=str(acknowledgement.start_token)),
        )
        after_start = self._quota_counts()

        self.assertEqual(after_reserve, (0, 0))
        self.assertEqual(after_start, (1, 1))
        self.assertEqual(web_server._DEMO_RUN_QUEUE.qsize(), 1)

    async def test_reserve_only_retry_charges_once_when_started(self) -> None:
        """Break caught: a reserved retry being charged before and during admission."""
        web_server._DEMO_RUN_QUEUE = asyncio.Queue(maxsize=2)
        original_id = "retry-source"
        original = web_server._RunState(
            "poster",
            designer_model="designer",
            has_pdf=True,
            brief="Create a paper poster.",
            attach_paths=[self.paper_path],
            conversation_id="retry-conversation",
            palette_id="plum_sage",
        )
        web_server._RUNS[original_id] = original
        (self.runs_dir / original_id).mkdir()

        with patch.object(web_server, "new_run_id", return_value="retry-reserved"):
            acknowledgement = await web_server.run_retry(
                original_id,
                self._request(reserve_only=True),
                designer_override=None,
                planner_override=None,
            )
        after_reserve = self._quota_counts()
        await web_server.start_run(
            acknowledgement.run_id,
            self._request(token=str(acknowledgement.start_token)),
        )
        after_start = self._quota_counts()

        self.assertEqual(after_reserve, (0, 0))
        self.assertEqual(after_start, (1, 1))
        self.assertEqual(web_server._DEMO_RUN_QUEUE.qsize(), 1)

    async def test_concurrent_start_at_capacity_does_not_charge_or_orphan_rejected_run(
        self,
    ) -> None:
        """Break caught: split queue checks charging two runs for one available slot."""
        web_server._DEMO_RUN_QUEUE = asyncio.Queue(maxsize=1)
        run_ids = ("capacity-a", "capacity-b")
        for run_id in run_ids:
            self._install_reserved_state(run_id)

        first = asyncio.create_task(
            web_server.start_run(
                run_ids[0],
                self._request(token=f"token-{run_ids[0]}"),
            )
        )
        second = asyncio.create_task(
            web_server.start_run(
                run_ids[1],
                self._request(token=f"token-{run_ids[1]}"),
            )
        )
        results = await asyncio.gather(first, second, return_exceptions=True)

        accepted = [result.run_id for result in results if not isinstance(result, BaseException)]
        rejected = [
            run_id
            for run_id, result in zip(run_ids, results)
            if isinstance(result, HTTPException)
        ]
        errors = [result for result in results if isinstance(result, HTTPException)]

        self.assertEqual(len(accepted), 1, results)
        self.assertEqual(len(rejected), 1, results)
        self.assertEqual(errors[0].status_code, 429)
        self.assertEqual(self._quota_counts(), (1, 1))
        self.assertTrue(web_server._RUNS[accepted[0]].queued)
        self.assertFalse(web_server._RUNS[rejected[0]].queued)
        self.assertEqual(web_server._DEMO_RUN_QUEUE.qsize(), 1)

    async def test_duplicate_start_while_worker_is_starting_is_idempotent(self) -> None:
        """Break caught: dequeue-to-worker-start gap admitting and charging a duplicate."""
        web_server._DEMO_RUN_QUEUE = asyncio.Queue(maxsize=1)
        run_id = "duplicate-start"
        self._install_reserved_state(run_id)
        await web_server.start_run(
            run_id,
            self._request(token=f"token-{run_id}"),
        )

        worker_start_entered = asyncio.Event()
        release_worker_start = asyncio.Event()

        async def blocked_worker_start(**_kwargs: object) -> None:
            worker_start_entered.set()
            await release_worker_start.wait()

        async def completed_monitor(**_kwargs: object) -> None:
            return None

        with (
            patch.object(
                web_server,
                "_start_legacy_pipeline_worker",
                new=blocked_worker_start,
            ),
            patch.object(
                web_server,
                "_monitor_supervised_pipeline",
                new=completed_monitor,
            ),
        ):
            worker = asyncio.create_task(web_server._demo_queue_worker(1))
            try:
                await asyncio.wait_for(worker_start_entered.wait(), timeout=1)
                await web_server.start_run(
                    run_id,
                    self._request(token=f"token-{run_id}"),
                )
                observed_quota = self._quota_counts()
                observed_queue_size = web_server._DEMO_RUN_QUEUE.qsize()
            finally:
                release_worker_start.set()
                await asyncio.sleep(0)
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        self.assertEqual(observed_quota, (1, 1))
        self.assertEqual(observed_queue_size, 0)

    async def test_cancel_start_race_does_not_kill_demo_queue_worker(self) -> None:
        web_server._DEMO_RUN_QUEUE = asyncio.Queue(maxsize=2)
        first = self._install_reserved_state("cancel-race")
        second = self._install_reserved_state("next-run")
        for state, run_id in ((first, "cancel-race"), (second, "next-run")):
            await web_server._DEMO_RUN_QUEUE.put(web_server._DemoQueuedRun(
                run_id=run_id,
                brief=state.brief,
                attach_paths=state.attach_paths,
                template=None,
                a_type="poster",
                baseline_artifact_json=None,
                state=state,
                settings=self.settings,
            ))
        second_started = asyncio.Event()

        async def start_worker(*, run_id: str, **_kwargs: object) -> None:
            if run_id == "cancel-race":
                first.cancelled = True
                raise web_server.RunNotReady("run cancelled before worker start")
            second_started.set()

        async def completed_monitor(**_kwargs: object) -> None:
            return None

        with (
            patch.object(web_server, "_start_legacy_pipeline_worker", new=start_worker),
            patch.object(web_server, "_monitor_supervised_pipeline", new=completed_monitor),
        ):
            worker = asyncio.create_task(web_server._demo_queue_worker(1))
            try:
                await asyncio.wait_for(second_started.wait(), timeout=1)
                await asyncio.wait_for(web_server._DEMO_RUN_QUEUE.join(), timeout=1)
                self.assertFalse(worker.done())
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        self.assertFalse(first.queued)
        self.assertFalse(second.queued)


if __name__ == "__main__":
    unittest.main()
