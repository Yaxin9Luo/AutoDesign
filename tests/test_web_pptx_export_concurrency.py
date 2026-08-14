from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException, Request

from scripts import web_server


def _replace_settings(settings: SimpleNamespace, **changes: object) -> SimpleNamespace:
    return SimpleNamespace(**vars(settings), **changes)


class WebPptxExportConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        web_server._open_web_run_start_gate()
        web_server._RUNS.clear()
        getattr(web_server, "_PPTX_EXPORT_RUNS", {}).clear()

    def tearDown(self) -> None:
        for state in web_server._RUNS.values():
            if state.task is not None and not state.task.done():
                state.task.cancel()
        web_server._RUNS.clear()
        getattr(web_server, "_PPTX_EXPORT_RUNS", {}).clear()

    def test_export_key_includes_access_identity_and_canonical_source(self) -> None:
        source = Path("runs/source-run/final/poster.html")
        equivalent_source = source.parent / ".." / "final" / source.name

        alice_key = web_server._pptx_export_key("user:alice", "source-run", source)

        self.assertEqual(
            alice_key,
            web_server._pptx_export_key("user:alice", "source-run", equivalent_source),
        )
        self.assertNotEqual(
            alice_key,
            web_server._pptx_export_key("user:bob", "source-run", source),
        )

    async def test_concurrent_same_source_reuses_active_run_ack(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            runs_dir = Path(raw_tmp) / "runs"
            source = runs_dir / "source-run" / "final" / "poster.html"
            source.parent.mkdir(parents=True)
            source.write_text("<html></html>", encoding="utf-8")
            release_background = asyncio.Event()
            background_started = asyncio.Event()

            async def fake_start(*, state: object, **_kwargs: object) -> str:
                state.reservation_token = ""
                state.task = asyncio.create_task(release_background.wait())
                background_started.set()
                return "pptx-token"

            request = Request({"type": "http", "headers": []})
            export_request = web_server.ArtifactPptxExportRequest(
                artifact={"artifact_id": "art_source-run", "name": "Poster"},
                conversation_id="conversation",
            )
            settings = SimpleNamespace(code_editor_model="model", code_editor_harness="codex")

            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "_DEMO_MODE", False),
                patch.object(web_server, "_RUN_ACCESS_CONTROL", False),
                patch.object(web_server, "_settings_for_code_editor_request", return_value=settings),
                patch.object(web_server, "replace", side_effect=_replace_settings),
                patch.object(
                    web_server,
                    "_code_editor_cmd_resolution",
                    side_effect=(
                        {"available": True, "cmd": "agent", "source": "test", "message": ""},
                        {"available": False, "source": "missing", "message": "missing agent"},
                    ),
                ) as resolve_command,
                patch.object(web_server, "_html_export_source_path", return_value=source),
                patch.object(web_server, "_html_export_canvas_size", return_value=(1200, 800)),
                patch.object(web_server, "_append_event"),
                patch.object(web_server, "new_run_id", side_effect=("export-1", "export-2")) as allocate_run,
                patch.object(
                    web_server,
                    "_start_supervised_derived_job",
                    side_effect=fake_start,
                ) as background_work,
            ):
                first = await web_server.export_artifact_pptx_run(export_request, request)
                await background_started.wait()
                second = await web_server.export_artifact_pptx_run(export_request, request)

                self.assertEqual(second, first)
                self.assertEqual(allocate_run.call_count, 1)
                self.assertEqual(resolve_command.call_count, 1)
                self.assertEqual(background_work.call_count, 1)

                release_background.set()
                await web_server._RUNS[first.run_id].task
                await web_server._clear_pptx_export_registration(
                    web_server._pptx_export_key("", "source-run", source),
                    first.run_id,
                )

            self.assertEqual(web_server._PPTX_EXPORT_RUNS, {})

    async def test_unauthorized_request_cannot_reuse_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            runs_dir = Path(raw_tmp) / "runs"
            source = runs_dir / "source-run" / "final" / "poster.html"
            source.parent.mkdir(parents=True)
            source.write_text("<html></html>", encoding="utf-8")
            access_path = Path(raw_tmp) / "demo_access.json"
            async def fake_start(*, state: object, **_kwargs: object) -> str:
                state.reservation_token = "pptx-token"
                return "pptx-token"

            alice = Request({"type": "http", "headers": [(b"x-demo-user", b"alice")]})
            bob = Request({"type": "http", "headers": [(b"x-demo-user", b"bob")]})
            export_request = web_server.ArtifactPptxExportRequest(
                artifact={"artifact_id": "art_source-run", "name": "Poster"}
            )
            settings = SimpleNamespace(code_editor_model="model", code_editor_harness="codex")

            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "DEMO_ACCESS_PATH", access_path),
                patch.object(web_server, "_DEMO_MODE", False),
                patch.object(web_server, "_RUN_ACCESS_CONTROL", True),
                patch.object(web_server, "_settings_for_code_editor_request", return_value=settings),
                patch.object(web_server, "replace", side_effect=_replace_settings),
                patch.object(
                    web_server,
                    "_code_editor_cmd_resolution",
                    return_value={"available": True, "cmd": "agent", "source": "test", "message": ""},
                ),
                patch.object(web_server, "_html_export_source_path", return_value=source),
                patch.object(web_server, "_html_export_canvas_size", return_value=(1200, 800)),
                patch.object(web_server, "_append_event"),
                patch.object(web_server, "new_run_id", side_effect=("export-alice", "export-bob")) as allocate_run,
                patch.object(
                    web_server,
                    "_start_supervised_derived_job",
                    side_effect=fake_start,
                ),
            ):
                web_server._demo_register_run("source-run", "user:alice")
                active = await web_server.export_artifact_pptx_run(export_request, alice)
                await asyncio.sleep(0)

                with self.assertRaises(HTTPException) as rejected:
                    await web_server.export_artifact_pptx_run(export_request, bob)

                self.assertEqual(rejected.exception.status_code, 404)
                self.assertEqual(allocate_run.call_count, 1)
                await web_server._clear_pptx_export_registration(
                    web_server._pptx_export_key("user:alice", "source-run", source),
                    active.run_id,
                )

    async def test_stale_registration_does_not_block_new_export(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            runs_dir = Path(raw_tmp) / "runs"
            source = runs_dir / "source-run" / "final" / "poster.html"
            source.parent.mkdir(parents=True)
            source.write_text("<html></html>", encoding="utf-8")
            request = Request({"type": "http", "headers": []})
            export_request = web_server.ArtifactPptxExportRequest(
                artifact={"artifact_id": "art_source-run", "name": "Poster"}
            )
            settings = SimpleNamespace(code_editor_model="model", code_editor_harness="codex")

            async def fake_start(*, state: object, **_kwargs: object) -> str:
                state.reservation_token = "pptx-token"
                return "pptx-token"

            with (
                patch.object(web_server, "RUNS_DIR", runs_dir),
                patch.object(web_server, "_DEMO_MODE", False),
                patch.object(web_server, "_RUN_ACCESS_CONTROL", False),
                patch.object(web_server, "_settings_for_code_editor_request", return_value=settings),
                patch.object(web_server, "replace", side_effect=_replace_settings),
                patch.object(
                    web_server,
                    "_code_editor_cmd_resolution",
                    return_value={"available": True, "cmd": "agent", "source": "test", "message": ""},
                ),
                patch.object(web_server, "_html_export_source_path", return_value=source),
                patch.object(web_server, "_html_export_canvas_size", return_value=(1200, 800)),
                patch.object(web_server, "_append_event"),
                patch.object(web_server, "new_run_id", return_value="fresh-export"),
                patch.object(
                    web_server,
                    "_start_supervised_derived_job",
                    side_effect=fake_start,
                ),
            ):
                key = web_server._pptx_export_key("", "source-run", source)
                web_server._PPTX_EXPORT_RUNS[key] = "missing-run"

                ack = await web_server.export_artifact_pptx_run(export_request, request)
                self.assertEqual(web_server._PPTX_EXPORT_RUNS[key], ack.run_id)
                await web_server._clear_pptx_export_registration(key, ack.run_id)

            self.assertEqual(ack.run_id, "fresh-export")
            self.assertEqual(web_server._PPTX_EXPORT_RUNS, {})


class WebPptxExportBackgroundCleanupTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        web_server._open_web_run_start_gate()
        web_server._RUNS.clear()
        getattr(web_server, "_PPTX_EXPORT_RUNS", {}).clear()

    async def _run_background(self, work_side_effect: object) -> tuple[str, web_server._RunState]:
        source_run_id = "source-run"
        run_id = "export-run"
        source = self.runs_dir / source_run_id / "final" / "poster.html"
        source.parent.mkdir(parents=True)
        source.write_text("<html></html>", encoding="utf-8")
        state = web_server._RunState("poster", conversation_id="conversation")
        web_server._RUNS[run_id] = state
        key = web_server._pptx_export_key("user:alice", source_run_id, source)
        web_server._PPTX_EXPORT_RUNS[key] = run_id

        with (
            patch.object(web_server, "RUNS_DIR", self.runs_dir),
            patch.object(web_server, "_html_export_canvas_size", return_value=(1200, 800)),
            patch.object(web_server, "_append_event"),
            patch.object(web_server, "_list_produced_artifacts", return_value=[]),
            patch.object(web_server.asyncio, "to_thread", side_effect=work_side_effect),
        ):
            await web_server._run_pptx_export_in_background(
                run_id=run_id,
                source_run_id=source_run_id,
                source=source,
                artifact={"artifact_id": "art_source-run", "name": "Poster"},
                settings=SimpleNamespace(),
                export_key=key,
            )
        return run_id, state

    async def test_success_removes_active_registration(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            self.runs_dir = Path(raw_tmp) / "runs"
            await self._run_background(None)
        self.assertEqual(web_server._PPTX_EXPORT_RUNS, {})

    async def test_failure_removes_active_registration(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            self.runs_dir = Path(raw_tmp) / "runs"
            _, state = await self._run_background(RuntimeError("agent failed"))
        self.assertIn("agent failed", state.error or "")
        self.assertEqual(web_server._PPTX_EXPORT_RUNS, {})

    async def test_cancellation_removes_active_registration(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            self.runs_dir = Path(raw_tmp) / "runs"
            _, state = await self._run_background(asyncio.CancelledError())
        self.assertEqual(state.error, "cancelled by user")
        self.assertEqual(web_server._PPTX_EXPORT_RUNS, {})


if __name__ == "__main__":
    unittest.main()
