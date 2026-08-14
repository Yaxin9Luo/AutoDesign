from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from autodesign.agents import reference_style_agent
from autodesign.agents.reference_style_agent import (
    ReferenceStyleAgentError,
    _finalize_reference_style_attempt,
    _invoke_style_agent,
    prepare_reference_style_contract,
)
from autodesign.process_supervision import (
    ProcessLedger,
    process_identity,
    process_is_alive,
    terminate_process_identities,
)
from autodesign.run_control import RunCancelled
from autodesign.tools._contract import ToolContext
from autodesign.util.io import sha256_file
from scripts.run_reference_style_extraction_batch import _ExtractionContext


class _MutableToken:
    can_cancel = True

    def __init__(
        self,
        run_id: str = "reference-style-cancel-test",
        *,
        cancelled: bool = False,
        cancel_at_phase: str | None = None,
    ) -> None:
        self.run_id = run_id
        self._event = threading.Event()
        if cancelled:
            self._event.set()
        self.cancel_at_phase = cancel_at_phase
        self.phases: list[str] = []

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self, phase: str) -> None:
        self.phases.append(phase)
        if self.cancel_at_phase == phase:
            self.cancel()
        if self.is_cancelled():
            raise RunCancelled(self.run_id, phase)

    def wait(self, timeout: float, poll_interval: float = 0.01) -> bool:
        del poll_interval
        return self._event.wait(max(0.0, timeout))


def _context(run_dir: Path, token: _MutableToken, *, api_key: str | None = None) -> ToolContext:
    return ToolContext(
        settings=SimpleNamespace(
            skills_dir=Path(__file__).resolve().parents[1] / "skills",
            harness_api_key=api_key,
        ),
        run_dir=run_dir,
        layers_dir=run_dir / "layers",
        run_id=token.run_id,
        cancellation_token=token,
    )


def _fake_skill_bundle() -> dict[str, object]:
    return {
        "skill_sha256": "a" * 64,
        "bundle_sha256": "b" * 64,
        "resource_hashes": {"references/output_contract_v4.md": "c" * 64},
    }


def _fake_runtime_skill(reference_dir: Path) -> dict[str, object]:
    return {
        "legacy_skill_path": reference_dir / "reference_style_agent_skill.md",
        "resources": [
            {
                "id": "output_contract_v4",
                "path": "references/output_contract_v4.md",
            }
        ],
    }


def _wait_for_path(path: Path, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def _wait_until_dead(identity, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not process_is_alive(identity):
            return
        time.sleep(0.01)
    raise AssertionError(f"process remained alive: {identity.pid}")


class ReferenceStyleCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_already_cancelled_context_starts_no_normalization_files_process_or_retry(self) -> None:
        token = _MutableToken(cancelled=True)
        run_dir = self.root / "run"
        ctx = _context(run_dir, token)
        source = self.root / "missing-reference.pdf"

        with patch.object(reference_style_agent, "normalize_reference_poster") as normalize, patch.object(
            reference_style_agent, "run_external_author_process"
        ) as spawn:
            with self.assertRaises(RunCancelled):
                prepare_reference_style_contract(
                    ctx,
                    source,
                    command="unused-style-agent",
                    harness="custom",
                )

        normalize.assert_not_called()
        spawn.assert_not_called()
        self.assertFalse(run_dir.exists())

    def test_registered_spawn_uses_exact_run_ledger_and_context_token_release_guard(self) -> None:
        run_dir = self.root / "run"
        work_dir = run_dir / "reference_poster"
        work_dir.mkdir(parents=True)
        token = _MutableToken()
        ctx = _context(run_dir, token)
        script = self.root / "style_agent.py"
        script.write_text(
            "import sys\n"
            "sys.stdin.buffer.read()\n"
            "print('style-stdout')\n"
            "print('style-stderr', file=sys.stderr)\n",
            encoding="utf-8",
        )

        result = _invoke_style_agent(
            shlex.join([sys.executable, str(script)]),
            prompt="analyze",
            work_dir=work_dir,
            harness="custom",
            api_key=None,
            timeout_s=5,
            ctx=ctx,
        )

        snapshot = ProcessLedger(run_dir).read()
        self.assertEqual([record.role for record in snapshot.processes], ["external-author"])
        self.assertIn("external_author.before_spawn_release", token.phases)
        self.assertEqual(result["reason"], "process_exit")
        self.assertIn("style-stdout", (work_dir / "reference_style_agent.stdout.log").read_text())
        self.assertIn("style-stderr", (work_dir / "reference_style_agent.stderr.log").read_text())

    def test_batch_extraction_context_remains_compatible_with_registered_process(self) -> None:
        run_dir = self.root / "batch-run"
        work_dir = run_dir / "reference_poster"
        work_dir.mkdir(parents=True)
        ctx = _ExtractionContext(
            settings=SimpleNamespace(harness_api_key=None),
            run_dir=run_dir,
            layers_dir=run_dir / "layers",
            run_id="batch-reference",
        )
        script = self.root / "batch_style_agent.py"
        script.write_text(
            "import sys\n"
            "sys.stdin.buffer.read()\n"
            "print('batch-style-ok')\n",
            encoding="utf-8",
        )

        result = _invoke_style_agent(
            shlex.join([sys.executable, str(script)]),
            prompt="analyze",
            work_dir=work_dir,
            harness="custom",
            api_key=None,
            timeout_s=5,
            ctx=ctx,
        )

        self.assertEqual(result["reason"], "process_exit")
        self.assertIn(
            "batch-style-ok",
            (work_dir / "reference_style_agent.stdout.log").read_text(),
        )

    def test_cancellation_at_registered_release_never_executes_target(self) -> None:
        run_dir = self.root / "run"
        work_dir = run_dir / "reference_poster"
        work_dir.mkdir(parents=True)
        marker = self.root / "target-started"
        script = self.root / "must_not_start.py"
        script.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('started', encoding='utf-8')\n",
            encoding="utf-8",
        )
        token = _MutableToken(
            cancel_at_phase="external_author.before_spawn_release"
        )
        ctx = _context(run_dir, token)

        with self.assertRaises(RunCancelled):
            _invoke_style_agent(
                shlex.join([sys.executable, str(script)]),
                prompt="unused",
                work_dir=work_dir,
                harness="custom",
                api_key=None,
                timeout_s=5,
                ctx=ctx,
            )

        self.assertFalse(marker.exists())
        snapshot = ProcessLedger(run_dir).read()
        self.assertEqual(len(snapshot.processes), 1)
        self.assertFalse(process_is_alive(snapshot.processes[0].identity))
        intent = next(
            item for item in snapshot.spawning
            if item.nonce == snapshot.processes[0].nonce
        )
        self.assertEqual(intent.status, "failed")

    @unittest.skipUnless(os.name == "posix", "detached descendant coverage requires POSIX sessions")
    def test_cancellation_kills_real_child_and_detached_grandchild_without_late_marker(self) -> None:
        run_dir = self.root / "run"
        work_dir = run_dir / "reference_poster"
        work_dir.mkdir(parents=True)
        parent_pid_path = self.root / "parent.pid"
        grandchild_pid_path = self.root / "grandchild.pid"
        late_trigger = self.root / "allow-late-write"
        late_marker = self.root / "late-marker"
        grandchild = self.root / "grandchild.py"
        grandchild.write_text(
            "import os, signal, time\n"
            "from pathlib import Path\n"
            f"Path({str(grandchild_pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
            f"trigger = Path({str(late_trigger)!r})\n"
            "while not trigger.exists(): time.sleep(0.01)\n"
            f"Path({str(late_marker)!r}).write_text('late', encoding='utf-8')\n"
            "while True: signal.pause()\n",
            encoding="utf-8",
        )
        parent = self.root / "parent.py"
        parent.write_text(
            "import os, signal, subprocess, sys\n"
            "from pathlib import Path\n"
            f"Path({str(parent_pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
            f"subprocess.Popen([sys.executable, {str(grandchild)!r}], start_new_session=True)\n"
            "while True: signal.pause()\n",
            encoding="utf-8",
        )
        token = _MutableToken()
        ctx = _context(run_dir, token)
        caught: list[BaseException] = []

        def invoke() -> None:
            try:
                _invoke_style_agent(
                    shlex.join([sys.executable, str(parent)]),
                    prompt="unused",
                    work_dir=work_dir,
                    harness="custom",
                    api_key=None,
                    timeout_s=30,
                    ctx=ctx,
                )
            except BaseException as exc:
                caught.append(exc)

        thread = threading.Thread(target=invoke, daemon=True)
        thread.start()
        _wait_for_path(parent_pid_path)
        _wait_for_path(grandchild_pid_path)
        parent_identity = process_identity(int(parent_pid_path.read_text()))
        grandchild_identity = process_identity(int(grandchild_pid_path.read_text()))

        token.cancel()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(caught), 1)
        self.assertIsInstance(caught[0], RunCancelled)
        _wait_until_dead(parent_identity)
        _wait_until_dead(grandchild_identity)

        late_trigger.touch()
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and not late_marker.exists():
            time.sleep(0.01)
        self.assertFalse(late_marker.exists())

    def test_large_prompt_to_non_reader_is_cancellable(self) -> None:
        run_dir = self.root / "run"
        work_dir = run_dir / "reference_poster"
        work_dir.mkdir(parents=True)
        started_path = self.root / "non-reader.pid"
        script = self.root / "non_reader.py"
        if os.name == "posix":
            script.write_text(
                "import os, signal\n"
                "from pathlib import Path\n"
                f"Path({str(started_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
                "while True: signal.pause()\n",
                encoding="utf-8",
            )
        else:
            script.write_text(
                "import os, threading\n"
                "from pathlib import Path\n"
                f"Path({str(started_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
                "threading.Event().wait()\n",
                encoding="utf-8",
            )
        token = _MutableToken()
        ctx = _context(run_dir, token)
        caught: list[BaseException] = []

        def invoke() -> None:
            try:
                _invoke_style_agent(
                    shlex.join([sys.executable, str(script)]),
                    prompt="x" * (8 * 1024 * 1024),
                    work_dir=work_dir,
                    harness="custom",
                    api_key=None,
                    timeout_s=30,
                    ctx=ctx,
                )
            except BaseException as exc:
                caught.append(exc)

        thread = threading.Thread(target=invoke, daemon=True)
        thread.start()
        _wait_for_path(started_path)
        token.cancel()
        thread.join(timeout=0.75)
        cancelled_without_external_kill = not thread.is_alive()
        if thread.is_alive():
            snapshot = ProcessLedger(run_dir).read()
            terminate_process_identities(
                [record.identity for record in snapshot.processes],
                root_pid=(snapshot.processes[0].identity.pid if snapshot.processes else None),
                grace_s=0.1,
            )
            thread.join(timeout=5)

        self.assertTrue(cancelled_without_external_kill)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(caught), 1)
        self.assertIsInstance(caught[0], RunCancelled)
        for record in ProcessLedger(run_dir).read().processes:
            self.assertFalse(process_is_alive(record.identity))

    def test_cancel_after_failed_attempt_archive_starts_no_retry_or_later_artifact(self) -> None:
        run_dir = self.root / "run"
        reference_dir = run_dir / "reference_poster"
        reference_dir.mkdir(parents=True)
        source = self.root / "source.png"
        source.write_bytes(b"source")
        source_sha = sha256_file(source)
        (reference_dir / "reference.png").write_bytes(b"normalized")
        token = _MutableToken()
        ctx = _context(run_dir, token)
        ctx.state["reference_poster"] = {
            "source_sha256": source_sha,
            "page_index": 0,
            "source_suffix": ".png",
        }
        original_archive = reference_style_agent._archive_reference_style_attempt

        def archive_then_cancel(*args, **kwargs):
            original_archive(*args, **kwargs)
            token.cancel()

        with ExitStack() as stack:
            stack.enter_context(patch.object(reference_style_agent, "_reference_style_skill_bundle", return_value=_fake_skill_bundle()))
            stack.enter_context(patch.object(reference_style_agent, "_stage_reference_style_skill_bundle", side_effect=lambda bundle, directory, **kw: _fake_runtime_skill(directory)))
            invoke = stack.enter_context(patch.object(reference_style_agent, "_invoke_style_agent", return_value={"status": "error", "reason": "process_exit"}))
            stack.enter_context(patch.object(reference_style_agent, "_finalize_reference_style_attempt", side_effect=ReferenceStyleAgentError("invalid analysis")))
            stack.enter_context(patch.object(reference_style_agent, "_archive_reference_style_attempt", side_effect=archive_then_cancel))
            with self.assertRaises(RunCancelled):
                prepare_reference_style_contract(
                    ctx,
                    source,
                    command="fake-style-agent",
                    harness="custom",
                )

        self.assertEqual(invoke.call_count, 1)
        self.assertTrue((reference_dir / "reference_style_attempts" / "attempt_01" / "failure.json").is_file())
        self.assertFalse((reference_dir / "reference_style_agent_prompt_attempt_02.md").exists())
        self.assertFalse((reference_dir / "reference_style_agent_process_attempt_02.json").exists())
        self.assertFalse((reference_dir / "reference_style_attempts" / "attempt_02").exists())
        self.assertFalse((run_dir / "reference_style_contract.json").exists())

    def _write_cache_candidate(self, run_dir: Path, source: Path) -> dict[str, object]:
        bundle = _fake_skill_bundle()
        contract = {
            "version": 4,
            "sanitizer_version": 4,
            "source_sha256": sha256_file(source),
            "source_page_index": 0,
            "extraction_skill_sha256": bundle["skill_sha256"],
            "extraction_skill_bundle_sha256": bundle["bundle_sha256"],
            "extraction_skill_resource_sha256": bundle["resource_hashes"],
            "extraction_prompt_schema_sha256": "d" * 64,
            "extraction_runtime_fingerprint": "e" * 64,
            "style_reference_id": "cached-style",
        }
        run_dir.mkdir(parents=True)
        (run_dir / "reference_style_contract.json").write_text(json.dumps(contract), encoding="utf-8")
        return contract

    def test_cancel_between_cache_audit_and_cache_hit_return(self) -> None:
        run_dir = self.root / "run"
        source = self.root / "source.png"
        source.write_bytes(b"source")
        cached = self._write_cache_candidate(run_dir, source)
        token = _MutableToken()
        ctx = _context(run_dir, token)

        def audit_then_cancel(*args, **kwargs):
            del args, kwargs
            token.cancel()
            return {"status": "pass"}

        with patch.object(reference_style_agent, "_reference_style_skill_bundle", return_value=_fake_skill_bundle()), patch.object(
            reference_style_agent, "_reference_style_prompt_schema_sha256", return_value="d" * 64
        ), patch.object(
            reference_style_agent, "_extraction_runtime_fingerprint", return_value="e" * 64
        ), patch.object(
            reference_style_agent, "_valid_cached_reference_style_contract", return_value=True
        ), patch.object(
            reference_style_agent, "audit_reference_style_artifacts", side_effect=audit_then_cancel
        ):
            with self.assertRaises(RunCancelled):
                prepare_reference_style_contract(
                    ctx,
                    source,
                    command="fake-style-agent",
                    harness="custom",
                )

        self.assertNotIn("reference_style_contract", ctx.state)
        self.assertFalse((run_dir / "reference_style_audit.json").exists())
        self.assertEqual(json.loads((run_dir / "reference_style_contract.json").read_text()), cached)

    def test_normal_cache_hit_remains_compatible(self) -> None:
        run_dir = self.root / "run"
        source = self.root / "source.png"
        source.write_bytes(b"source")
        cached = self._write_cache_candidate(run_dir, source)
        reference_dir = run_dir / "reference_poster"
        reference_dir.mkdir()
        (reference_dir / "reference_source_metadata.json").write_text(
            json.dumps({"source_sha256": cached["source_sha256"], "page_index": 0}),
            encoding="utf-8",
        )
        token = _MutableToken()
        ctx = _context(run_dir, token)

        with patch.object(reference_style_agent, "_reference_style_skill_bundle", return_value=_fake_skill_bundle()), patch.object(
            reference_style_agent, "_reference_style_prompt_schema_sha256", return_value="d" * 64
        ), patch.object(
            reference_style_agent, "_extraction_runtime_fingerprint", return_value="e" * 64
        ), patch.object(
            reference_style_agent, "_valid_cached_reference_style_contract", return_value=True
        ), patch.object(
            reference_style_agent, "audit_reference_style_artifacts", return_value={"status": "pass"}
        ):
            actual = prepare_reference_style_contract(
                ctx,
                source,
                command="fake-style-agent",
                harness="custom",
            )

        self.assertEqual(actual, cached)
        self.assertEqual(ctx.state["reference_style_contract"], cached)
        self.assertEqual(json.loads((run_dir / "reference_style_audit.json").read_text())["status"], "pass")

    def test_browser_finalization_cancellation_blocks_blueprint_promotion_and_contract(self) -> None:
        run_dir = self.root / "run"
        reference_dir = run_dir / "reference_poster"
        reference_dir.mkdir(parents=True)
        raw_blueprint = reference_dir / "reference_style_blueprint.html"
        raw_blueprint.write_text("<main>raw</main>", encoding="utf-8")
        token = _MutableToken()
        ctx = _context(run_dir, token)
        contract = {
            "style_tokens": {
                "body_region_structure": {
                    "regions": [{"region_id": "region_1", "region_role": "column"}],
                },
                "layout_rhythm": {},
            },
            "color_system": {"palette_id": "test"},
        }

        def render_then_cancel(*args, **kwargs):
            del args, kwargs
            token.cancel()
            return [{"region_id": "region_1", "x_pct": 0, "y_pct": 0, "w_pct": 100, "h_pct": 100}]

        with patch.object(reference_style_agent, "_read_json", return_value={"version": 4, "body_region_structure": {}}), patch.object(
            reference_style_agent, "_compile_reference_style_contract", return_value=contract
        ), patch.object(
            reference_style_agent, "_valid_reference_style_review", return_value=True
        ), patch.object(
            reference_style_agent, "_validate_raw_reference_style_blueprint"
        ), patch.object(
            reference_style_agent, "_render_and_measure_reference_blueprint", side_effect=render_then_cancel
        ), patch.object(
            reference_style_agent, "_sanitize_reference_style_blueprint"
        ) as sanitize:
            with self.assertRaises(RunCancelled):
                _finalize_reference_style_attempt(
                    ctx,
                    metadata={},
                    source_sha="f" * 64,
                    page_index=0,
                    skill_path=reference_dir / "skill.md",
                    skill_sha="a" * 64,
                    skill_bundle_sha="b" * 64,
                    skill_resource_hashes={},
                    prompt_schema_sha="c" * 64,
                    runtime_fingerprint="d" * 64,
                    attempt_index=1,
                    invocation={"status": "ok"},
                    semantic_expectations=None,
                    enforce_extraction_only_artifacts=False,
                )

        sanitize.assert_not_called()
        self.assertFalse((run_dir / "reference_style_blueprint.html").exists())
        self.assertFalse((run_dir / "reference_style_blueprint_preview.png").exists())
        self.assertFalse((run_dir / "reference_style_contract.json").exists())
        self.assertFalse((run_dir / "reference_style_audit.json").exists())

    def test_browser_and_context_close_when_cancelled_after_launch(self) -> None:
        class FakePage:
            def goto(self, *args, **kwargs) -> None:
                del args, kwargs

        class FakeContext:
            def __init__(self) -> None:
                self.closed = False

            def new_page(self) -> FakePage:
                return FakePage()

            def close(self) -> None:
                self.closed = True

        class FakeBrowser:
            def __init__(self, context: FakeContext) -> None:
                self.context = context
                self.closed = False

            def new_context(self, **kwargs) -> FakeContext:
                del kwargs
                return self.context

            def close(self) -> None:
                self.closed = True

        class FakePlaywrightManager:
            def __init__(self, browser: FakeBrowser) -> None:
                self.browser = browser

            def __enter__(self):
                chromium = SimpleNamespace(launch=lambda **kwargs: self.browser)
                return SimpleNamespace(chromium=chromium)

            def __exit__(self, exc_type, exc, traceback) -> None:
                del exc_type, exc, traceback

        context = FakeContext()
        browser = FakeBrowser(context)
        cancelled = RunCancelled("reference-style-cancel-test", "browser.after_page_load")

        def cancellation_check(phase: str) -> None:
            if phase == "reference_style.browser.after_page_load":
                raise cancelled

        with patch(
            "playwright.sync_api.sync_playwright",
            return_value=FakePlaywrightManager(browser),
        ):
            with self.assertRaises(RunCancelled) as caught:
                reference_style_agent._render_and_measure_reference_blueprint(
                    self.root / "blueprint.html",
                    self.root / "preview.png",
                    expected_region_ids=["region_1"],
                    cancellation_check=cancellation_check,
                )

        self.assertIs(caught.exception, cancelled)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    def test_run_cancelled_escapes_reference_finalization_handlers(self) -> None:
        run_dir = self.root / "run"
        reference_dir = run_dir / "reference_poster"
        reference_dir.mkdir(parents=True)
        (reference_dir / "reference_style_blueprint.html").write_text("raw", encoding="utf-8")
        token = _MutableToken()
        ctx = _context(run_dir, token)
        cancelled = RunCancelled(token.run_id, "browser.launch")
        contract = {
            "style_tokens": {
                "body_region_structure": {"regions": [{"region_id": "region_1"}]},
                "layout_rhythm": {},
            }
        }

        with patch.object(reference_style_agent, "_read_json", return_value={"version": 4, "body_region_structure": {}}), patch.object(
            reference_style_agent, "_compile_reference_style_contract", return_value=contract
        ), patch.object(
            reference_style_agent, "_valid_reference_style_review", return_value=True
        ), patch.object(
            reference_style_agent, "_validate_raw_reference_style_blueprint"
        ), patch.object(
            reference_style_agent, "_render_and_measure_reference_blueprint", side_effect=cancelled
        ):
            with self.assertRaises(RunCancelled) as caught:
                _finalize_reference_style_attempt(
                    ctx,
                    metadata={},
                    source_sha="f" * 64,
                    page_index=0,
                    skill_path=reference_dir / "skill.md",
                    skill_sha="a" * 64,
                    skill_bundle_sha="b" * 64,
                    skill_resource_hashes={},
                    prompt_schema_sha="c" * 64,
                    runtime_fingerprint="d" * 64,
                    attempt_index=1,
                    invocation={"status": "ok"},
                    semantic_expectations=None,
                    enforce_extraction_only_artifacts=False,
                )

        self.assertIs(caught.exception, cancelled)

    def test_normal_retry_budget_and_success_contract_remain_compatible(self) -> None:
        run_dir = self.root / "run"
        reference_dir = run_dir / "reference_poster"
        reference_dir.mkdir(parents=True)
        source = self.root / "source.png"
        source.write_bytes(b"source")
        source_sha = sha256_file(source)
        (reference_dir / "reference.png").write_bytes(b"normalized")
        token = _MutableToken()
        ctx = _context(run_dir, token)
        ctx.state["reference_poster"] = {
            "source_sha256": source_sha,
            "page_index": 0,
            "source_suffix": ".png",
        }
        contract = {
            "style_reference_id": "style-ok",
            "color_system": {"palette_id": "palette-ok"},
        }

        with patch.object(reference_style_agent, "_reference_style_skill_bundle", return_value=_fake_skill_bundle()), patch.object(
            reference_style_agent, "_stage_reference_style_skill_bundle", side_effect=lambda bundle, directory, **kw: _fake_runtime_skill(directory)
        ), patch.object(
            reference_style_agent, "_invoke_style_agent", return_value={"status": "ok", "reason": "process_exit"}
        ) as invoke, patch.object(
            reference_style_agent,
            "_finalize_reference_style_attempt",
            side_effect=[ReferenceStyleAgentError("first validation failed"), contract],
        ):
            actual = prepare_reference_style_contract(
                ctx,
                source,
                command="fake-style-agent",
                harness="custom",
            )

        self.assertEqual(actual, contract)
        self.assertEqual(invoke.call_count, 2)
        self.assertTrue((reference_dir / "reference_style_agent_prompt_attempt_01.md").is_file())
        self.assertTrue((reference_dir / "reference_style_agent_prompt_attempt_02.md").is_file())
        self.assertTrue((reference_dir / "reference_style_attempts" / "attempt_01" / "failure.json").is_file())
        self.assertFalse((reference_dir / "reference_style_attempts" / "attempt_02").exists())

    def test_process_timeout_and_sensitive_output_remain_compatible_and_redacted(self) -> None:
        run_dir = self.root / "run"
        work_dir = run_dir / "reference_poster"
        work_dir.mkdir(parents=True)
        secret = "reference-style-secret-123"
        argv_path = self.root / "argv.json"
        output_script = self.root / "output.py"
        output_script.write_text(
            "import json, os, sys\n"
            f"open({str(argv_path)!r}, 'w', encoding='utf-8').write(json.dumps(sys.argv))\n"
            "print(os.environ.get('OPENAI_API_KEY', 'missing'))\n"
            "print(os.environ.get('OPENAI_API_KEY', 'missing'), file=sys.stderr)\n",
            encoding="utf-8",
        )
        token = _MutableToken()
        ctx = _context(run_dir, token, api_key=secret)

        result = _invoke_style_agent(
            shlex.join([sys.executable, str(output_script)]),
            prompt="unused",
            work_dir=work_dir,
            harness="codex",
            api_key=secret,
            timeout_s=5,
            ctx=ctx,
        )

        persisted = "\n".join(
            [
                (work_dir / "reference_style_agent.stdout.log").read_text(),
                (work_dir / "reference_style_agent.stderr.log").read_text(),
                (run_dir / "process_ledger.json").read_text(),
                argv_path.read_text(),
                json.dumps(result),
            ]
        )
        self.assertNotIn(secret, persisted)
        self.assertIn("[REDACTED]", persisted)
        self.assertFalse((work_dir / ".reference_style_agent.stdout.tmp").exists())
        self.assertFalse((work_dir / ".reference_style_agent.stderr.tmp").exists())

        timeout_script = self.root / "timeout.py"
        if os.name == "posix":
            timeout_script.write_text(
                "import signal\nwhile True: signal.pause()\n",
                encoding="utf-8",
            )
        else:
            timeout_script.write_text(
                "import threading\nthreading.Event().wait()\n",
                encoding="utf-8",
            )
        timeout_result = _invoke_style_agent(
            shlex.join([sys.executable, str(timeout_script)]),
            prompt="unused",
            work_dir=work_dir,
            harness="custom",
            api_key=None,
            timeout_s=0,
            ctx=ctx,
        )
        self.assertTrue(timeout_result["timed_out"])
        self.assertEqual(timeout_result["reason"], "timeout")
        for record in ProcessLedger(run_dir).read().processes:
            self.assertFalse(process_is_alive(record.identity))


if __name__ == "__main__":
    unittest.main()
