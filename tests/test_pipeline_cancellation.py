from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import autodesign.image_backend as image_backend
import autodesign.llm_backend as llm_backend
import autodesign.run_control as run_control
import autodesign.run_worker as run_worker
import autodesign.util.vlm as vlm
from autodesign.agents.claim_graph_extractor import ClaimGraphExtractor
from autodesign.agents.critic_agent import CriticAgent
from autodesign.agents.deck_outline_agent import DeckOutlineAgent
from autodesign.agents.hyperframes_composer import HyperFramesComposer
from autodesign.agents.paper_memory_agent import PaperMemoryAgent
from autodesign.agents.prompt_enhancer import PromptEnhancer
from autodesign.designer import DesignerLoop, invoke_designer_tool
from autodesign.llm_backend import ToolCall, TurnResponse
from autodesign.runner import PipelineRunner, _recover_missing_composite
from autodesign.schema import RunResult, ToolResultRecord
from autodesign.tools import TOOL_HANDLERS
from autodesign.tools._contract import ToolContext


class _FakeCancellationToken:
    def __init__(self, *, run_id: str = "fake-run", cancelled: bool = False) -> None:
        self.run_id = run_id
        self.cancelled = cancelled
        self.phases: list[str] = []

    def is_cancelled(self) -> bool:
        return self.cancelled

    def raise_if_cancelled(self, phase: str) -> None:
        self.phases.append(phase)
        if self.cancelled:
            raise run_control.RunCancelled(self.run_id, phase)

    def wait(self, timeout: float, poll_interval: float = 0.01) -> bool:
        del timeout, poll_interval
        return self.cancelled


def _tool_context(root: Path, token: object) -> ToolContext:
    return ToolContext(
        settings=SimpleNamespace(
            poster_harness_mode="production",
            designer_model="fake",
            designer_thinking_budget=0,
            max_designer_turns=3,
            enable_interleaved_thinking=False,
        ),
        run_dir=root,
        layers_dir=root / "layers",
        run_id="pipeline-cancel-test",
        cancellation_token=token,
    )


class CancellationTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self._tmp.name) / "runs"
        self.store = run_control.RunControlStore(self.runs_dir)
        self.run_id = "token-test"
        self.store.reserve(self.run_id, "poster")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _token(self, event: threading.Event | None = None):
        token_type = getattr(run_control, "CancellationToken", None)
        self.assertIsNotNone(token_type, "CancellationToken must be implemented")
        return token_type.for_run(self.store, self.run_id, signal_event=event)

    def test_cancellation_token_reacts_to_signal_and_authoritative_state(self) -> None:
        event = threading.Event()
        signalled = self._token(event)
        self.assertFalse(signalled.is_cancelled())
        event.set()
        self.assertTrue(signalled.is_cancelled())

        authoritative = self._token()
        self.assertFalse(authoritative.is_cancelled())
        self.store.request_cancel(self.run_id)
        self.assertTrue(authoritative.is_cancelled())

    def test_run_cancelled_carries_identity_and_bypasses_exception_handlers(self) -> None:
        token = self._token()
        self.store.request_cancel(self.run_id)
        generic_handler_ran = False
        caught: run_control.RunCancelled | None = None
        try:
            try:
                token.raise_if_cancelled("planning")
            except Exception:
                generic_handler_ran = True
        except run_control.RunCancelled as exc:
            caught = exc

        self.assertFalse(generic_handler_ran)
        self.assertIsNotNone(caught)
        self.assertEqual(caught.run_id, self.run_id)
        self.assertEqual(caught.phase, "planning")

    def test_cancellation_wait_polls_authoritative_state(self) -> None:
        token = self._token()

        def cancel_later() -> None:
            time.sleep(0.03)
            self.store.request_cancel(self.run_id)

        thread = threading.Thread(target=cancel_later)
        thread.start()
        started = time.monotonic()
        try:
            self.assertTrue(token.wait(1.0, poll_interval=0.005))
        finally:
            thread.join(timeout=1.0)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_authoritative_control_read_failure_fails_closed(self) -> None:
        token = self._token()
        control_path = self.runs_dir / self.run_id / "run_control.json"
        control_path.write_text("{not-json", encoding="utf-8")

        self.assertTrue(token.is_cancelled())
        with self.assertRaises(run_control.RunCancelled):
            token.raise_if_cancelled("provider_request")
        self.assertEqual(token.reason, "authoritative_control_unavailable")

    def test_cancellation_is_monotonic_after_transient_control_failure(self) -> None:
        class FlakyStore:
            def __init__(self) -> None:
                self.calls = 0

            def read(self, _run_id):
                self.calls += 1
                if self.calls == 1:
                    raise OSError("transient control read failure")
                return SimpleNamespace(
                    state="running",
                    cancellation_pending=None,
                    cancellation_requested_at=None,
                    updated_at=time.time(),
                )

        store = FlakyStore()
        token = run_control.CancellationToken.for_run(store, "flaky-control")

        self.assertTrue(token.is_cancelled())
        self.assertTrue(token.is_cancelled())
        self.assertEqual(store.calls, 1)


class ToolAndDesignerCancellationTests(unittest.TestCase):
    def test_shadow_tool_context_preserves_authoritative_token(self) -> None:
        from autodesign.tools.propose_paper_poster_html import _shadow_tool_context

        with tempfile.TemporaryDirectory() as raw_tmp:
            token = _FakeCancellationToken()
            ctx = _tool_context(Path(raw_tmp), token)

            shadow = _shadow_tool_context(ctx)

            self.assertIs(shadow.cancellation_token, token)

    def test_tool_context_rejects_state_allocation_and_tool_start_after_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            token = _FakeCancellationToken(cancelled=True)
            ctx = _tool_context(Path(raw_tmp), token)
            calls: list[str] = []

            def handler(_args, *, ctx):
                del ctx
                calls.append("called")
                return ToolResultRecord(status="ok", payload={})

            with self.assertRaises(run_control.RunCancelled):
                ctx.next_layer_version("hero")
            with self.assertRaises(run_control.RunCancelled):
                ctx.next_composite_iter()
            with patch.dict(TOOL_HANDLERS, {"cancel_probe": handler}):
                with self.assertRaises(run_control.RunCancelled):
                    invoke_designer_tool("cancel_probe", {}, ctx)
            self.assertEqual(calls, [])
            self.assertEqual(ctx.state["layer_versions"], {})
            self.assertEqual(ctx.state["composite_iter"], 0)

    def test_designer_does_not_start_tool_after_cancelled_model_response(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            token = _FakeCancellationToken()
            tool_calls: list[str] = []

            class Backend:
                name = "fake"
                model = "fake"

                def create_turn(self, **kwargs):
                    self.seen_token = kwargs.get("cancellation_token")
                    token.cancelled = True
                    return TurnResponse(
                        tool_calls=[ToolCall(id="1", name="probe", input={})],
                        stop_reason="tool_use",
                    )

                def append_assistant(self, messages, response):
                    del messages, response
                    tool_calls.append("assistant-appended")

                def append_tool_results(self, messages, results):
                    del messages, results
                    tool_calls.append("results-appended")

            backend = Backend()
            ctx = _tool_context(Path(raw_tmp), token)
            with patch("autodesign.designer.make_backend", return_value=backend):
                designer = DesignerLoop(ctx.settings, "system")
            with patch.dict(TOOL_HANDLERS, {"probe": lambda _args, *, ctx: tool_calls.append("tool")}), self.assertRaises(run_control.RunCancelled):
                designer.run("brief", ctx)
            self.assertIs(backend.seen_token, token)
            self.assertEqual(tool_calls, [])

    def test_designer_does_not_append_tool_result_or_start_next_turn_after_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            token = _FakeCancellationToken()

            class Backend:
                name = "fake"
                model = "fake"

                def __init__(self) -> None:
                    self.requests = 0
                    self.result_appends = 0

                def create_turn(self, **kwargs):
                    del kwargs
                    self.requests += 1
                    return TurnResponse(
                        tool_calls=[ToolCall(id="1", name="probe", input={})],
                        stop_reason="tool_use",
                    )

                def append_assistant(self, messages, response):
                    del messages, response

                def append_tool_results(self, messages, results):
                    del messages, results
                    self.result_appends += 1

            def handler(_args, *, ctx):
                del ctx
                token.cancelled = True
                return ToolResultRecord(status="ok", payload={"done": True})

            backend = Backend()
            ctx = _tool_context(Path(raw_tmp), token)
            with patch("autodesign.designer.make_backend", return_value=backend):
                designer = DesignerLoop(ctx.settings, "system")
            with patch.dict(TOOL_HANDLERS, {"probe": handler}), self.assertRaises(run_control.RunCancelled):
                designer.run("brief", ctx)
            self.assertEqual(backend.requests, 1)
            self.assertEqual(backend.result_appends, 0)

    def test_runner_direct_tool_recovery_uses_the_same_cancelled_dispatch_barrier(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            token = _FakeCancellationToken(cancelled=True)
            ctx = _tool_context(Path(raw_tmp), token)
            ctx.state["design_spec"] = SimpleNamespace(artifact_type="poster")
            calls: list[str] = []

            def handler(_args, *, ctx):
                del ctx
                calls.append("composite")
                return ToolResultRecord(status="ok", payload={})

            with patch.dict(TOOL_HANDLERS, {"composite": handler}), self.assertRaises(run_control.RunCancelled):
                _recover_missing_composite(ctx, brief="poster")
            self.assertEqual(calls, [])


class ProviderCancellationTests(unittest.TestCase):
    @staticmethod
    def _png_b64() -> str:
        buffer = BytesIO()
        image_backend.PILImage.new("RGB", (1, 1), (12, 34, 56)).save(
            buffer,
            format="PNG",
        )
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    def test_anthropic_explicit_retries_preserve_three_attempt_quality(self) -> None:
        attempts = 0

        def create(**_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                error = RuntimeError("503 service unavailable")
                error.status_code = 503
                raise error
            return SimpleNamespace(
                content=[],
                stop_reason="end_turn",
                usage=SimpleNamespace(
                    input_tokens=7,
                    output_tokens=3,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                ),
            )

        backend = llm_backend.AnthropicBackend.__new__(llm_backend.AnthropicBackend)
        backend.model = "fake"
        backend._enable_interleaved = False
        backend._timeout_s = 1
        backend.client = SimpleNamespace(messages=SimpleNamespace(create=create))
        result = None
        caught = None
        with patch("autodesign.llm_backend.time.sleep"):
            try:
                result = backend.create_turn(system="system", messages=[], tools=[])
            except Exception as exc:  # expected RED before explicit retries
                caught = exc

        self.assertEqual(attempts, 3)
        self.assertIsNone(caught)
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(result.usage["input"], 7)

    def test_openai_image_backends_preserve_three_attempt_quality(self) -> None:
        png_b64 = self._png_b64()

        for backend_kind in ("openrouter", "openai_compat"):
            with self.subTest(backend=backend_kind):
                attempts = 0

                def create(**_kwargs):
                    nonlocal attempts
                    attempts += 1
                    if attempts < 3:
                        error = RuntimeError("503 service unavailable")
                        error.status_code = 503
                        raise error
                    if backend_kind == "openrouter":
                        message = SimpleNamespace(model_dump=lambda: {
                            "images": [{
                                "image_url": {
                                    "url": f"data:image/png;base64,{png_b64}",
                                },
                            }],
                        })
                        return SimpleNamespace(
                            choices=[SimpleNamespace(message=message)],
                        )
                    return {"data": [{"b64_json": png_b64}]}

                if backend_kind == "openrouter":
                    backend = image_backend.OpenRouterImageBackend.__new__(
                        image_backend.OpenRouterImageBackend,
                    )
                    backend.model = "fake"
                    backend._client = SimpleNamespace(
                        chat=SimpleNamespace(
                            completions=SimpleNamespace(create=create),
                        ),
                    )
                else:
                    backend = image_backend.OpenAICompatImageBackend.__new__(
                        image_backend.OpenAICompatImageBackend,
                    )
                    backend.model = "fake"
                    backend._timeout_s = 1
                    backend._client = SimpleNamespace(
                        images=SimpleNamespace(generate=create, edit=create),
                    )

                result = None
                caught = None
                with patch("autodesign.image_backend.time.sleep"):
                    try:
                        result = backend.generate(
                            prompt="image",
                            aspect_ratio="1:1",
                            image_size="1K",
                        )
                    except Exception as exc:  # expected RED before explicit retries
                        caught = exc

                self.assertEqual(attempts, 3)
                self.assertIsNone(caught)
                self.assertEqual((result.width, result.height), (1, 1))

    def test_openai_image_retry_wait_stops_before_second_request(self) -> None:
        class WaitCancelsToken(_FakeCancellationToken):
            def wait(self, timeout: float, poll_interval: float = 0.01) -> bool:
                del timeout, poll_interval
                self.cancelled = True
                return True

        token = WaitCancelsToken()
        attempts = 0

        def create(**_kwargs):
            nonlocal attempts
            attempts += 1
            error = RuntimeError("503 service unavailable")
            error.status_code = 503
            raise error

        backend = image_backend.OpenRouterImageBackend.__new__(
            image_backend.OpenRouterImageBackend,
        )
        backend.model = "fake"
        backend._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        )
        caught = None
        try:
            backend.generate(
                prompt="image",
                aspect_ratio="1:1",
                image_size="1K",
                cancellation_token=token,
            )
        except BaseException as exc:
            caught = exc

        self.assertIsInstance(caught, run_control.RunCancelled)
        self.assertEqual(attempts, 1)

    def test_llm_retry_wait_stops_before_a_second_request(self) -> None:
        token = _FakeCancellationToken()
        attempts = 0

        def create(**_kwargs):
            nonlocal attempts
            attempts += 1
            token.cancelled = True
            error = RuntimeError("429 retry")
            error.status_code = 429
            raise error

        with self.assertRaises(run_control.RunCancelled):
            llm_backend._create_openai_turn_with_retries(
                create,
                {},
                model="fake",
                backend="fake",
                cancellation_token=token,
            )
        self.assertEqual(attempts, 1)

    def test_anthropic_sdk_hidden_retries_are_disabled(self) -> None:
        captured: dict[str, object] = {}

        def anthropic_factory(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(messages=SimpleNamespace(create=lambda **_kwargs: None))

        module = SimpleNamespace(Anthropic=anthropic_factory)
        settings = SimpleNamespace(
            enable_interleaved_thinking=False,
            anthropic_api_key="",
            anthropic_custom_headers={},
            anthropic_base_url="",
            llm_http_timeout=1,
        )
        with patch.dict(sys.modules, {"anthropic": module}):
            llm_backend.AnthropicBackend(settings, "fake")
        self.assertEqual(captured.get("max_retries"), 0)

    def test_vlm_retry_stops_before_second_provider_request(self) -> None:
        token = _FakeCancellationToken()
        attempts = 0

        def call_openai(**_kwargs):
            nonlocal attempts
            attempts += 1
            token.cancelled = True
            error = RuntimeError("429 retry")
            error.status_code = 429
            raise error

        settings = SimpleNamespace(ingest_http_timeout=1)
        with patch("autodesign.util.vlm._provider_for_model", return_value="openai"), patch(
            "autodesign.util.vlm._call_openai", side_effect=call_openai
        ), self.assertRaises(run_control.RunCancelled):
            vlm.vlm_call_json(
                settings=settings,
                model="fake",
                system="system",
                user_text="user",
                max_retries=2,
                cancellation_token=token,
            )
        self.assertEqual(attempts, 1)

    def test_image_fallback_does_not_start_second_provider_after_cancel(self) -> None:
        token = _FakeCancellationToken()

        class Primary:
            model = "primary"

            def generate(self, **_kwargs):
                token.cancelled = True
                raise image_backend.ImageGenerationError(
                    "unavailable", category="provider_unavailable",
                )

        backend = image_backend.FallbackImageBackend(
            Primary(),
            SimpleNamespace(image_provider="auto"),
            "fallback",
        )
        with patch.object(backend, "_build_fallback") as build, self.assertRaises(run_control.RunCancelled):
            backend.generate(
                prompt="image",
                aspect_ratio="1:1",
                image_size="1K",
                cancellation_token=token,
            )
        build.assert_not_called()

class AgentAndRunnerCancellationTests(unittest.TestCase):
    def test_runner_checks_cancel_immediately_after_both_resume_load_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            resume_path = Path(raw_tmp) / "resume-target"
            settings = SimpleNamespace(out_dir=Path(raw_tmp))
            failure_result = RunResult(
                run_id=resume_path.name,
                run_dir=str(resume_path),
                artifact_type="poster",
                terminal_status="fail",
            )
            for loaded in (failure_result, {"valid": True}):
                with self.subTest(load_shape=type(loaded).__name__):
                    token = _FakeCancellationToken()

                    def load(_path):
                        token.cancelled = True
                        return loaded

                    runner = PipelineRunner(settings)
                    with patch(
                        "autodesign.runner.resolve_run_dir",
                        return_value=resume_path,
                    ), patch(
                        "autodesign.runner._load_resume_state",
                        side_effect=load,
                    ), self.assertRaises(run_control.RunCancelled) as caught:
                        runner.run(
                            "brief",
                            resume_run=resume_path.name,
                            cancellation_token=token,
                            supervised=True,
                        )

                    self.assertEqual(caught.exception.phase, "runner.after_resume_load")

    def test_runner_checks_cancel_before_resume_author_refusal_return(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            resume_path = Path(raw_tmp) / "resume-target"
            settings = SimpleNamespace(out_dir=Path(raw_tmp))
            token = _FakeCancellationToken()
            resume_ctx = {
                "artifact_type": "poster",
                "run_brief_json": {},
                "resume_state_json": {},
            }

            def refuse(active_settings, loaded):
                del loaded
                token.cancelled = True
                return active_settings, "resume refused for test"

            runner = PipelineRunner(settings)
            with patch(
                "autodesign.runner.resolve_run_dir",
                return_value=resume_path,
            ), patch(
                "autodesign.runner._load_resume_state",
                return_value=resume_ctx,
            ), patch(
                "autodesign.runner._settings_for_external_author_resume",
                side_effect=refuse,
            ), self.assertRaises(run_control.RunCancelled) as caught:
                runner.run(
                    "brief",
                    resume_run=resume_path.name,
                    cancellation_token=token,
                    supervised=True,
                )

            self.assertEqual(
                caught.exception.phase,
                "runner.before_resume_author_refusal",
            )

    def test_never_token_preserves_exact_signature_agent_backend_result(self) -> None:
        class ExactSignatureBackend:
            name = "fake"
            model = "fake"

            def create_turn(
                self,
                *,
                system,
                messages,
                tools,
                thinking_budget,
                max_tokens,
            ):
                del system, messages, tools, thinking_budget, max_tokens
                return TurnResponse(
                    text="Grounded enhanced design brief. " * 20,
                    stop_reason="end_turn",
                    usage={"input": 5, "output": 8},
                )

        enhancer = PromptEnhancer.__new__(PromptEnhancer)
        enhancer.settings = SimpleNamespace(
            enable_prompt_enhancer=True,
            enhancer_model="fake",
            enhancer_thinking_budget=0,
        )
        enhancer.system_prompt = "system"
        enhancer.backend = ExactSignatureBackend()

        result = enhancer.enhance(
            "Create a source-grounded poster.",
            cancellation_token=run_control.CancellationToken.never("legacy"),
        )

        self.assertFalse(result.skipped)
        self.assertIn("Grounded enhanced design brief.", result.enhanced_brief)
        self.assertEqual(result.input_tokens, 5)
        self.assertEqual(result.output_tokens, 8)

    def test_critic_checks_cancellation_after_terminal_tool_dispatch(self) -> None:
        token = _FakeCancellationToken()

        class Backend:
            name = "fake"
            model = "fake"

            def __init__(self) -> None:
                self.result_appends = 0

            def create_turn(self, **_kwargs):
                return TurnResponse(
                    tool_calls=[ToolCall(id="1", name="report_verdict", input={})],
                    stop_reason="tool_use",
                )

            def append_assistant(self, messages, response):
                del messages, response

            def append_tool_results(self, messages, results):
                del messages, results
                self.result_appends += 1

        backend = Backend()
        critic = CriticAgent.__new__(CriticAgent)
        critic.backend = backend
        critic.settings = SimpleNamespace(
            critic_max_turns=1,
            max_critique_iters=1,
            critic_max_images_per_turn=1,
            critic_thinking_budget=0,
        )
        critic.artifact_type = SimpleNamespace(value="poster")
        critic._system = lambda: "system"

        def dispatch(*_args, **_kwargs):
            token.cancelled = True
            terminal = SimpleNamespace(verdict="pass", score=100, issues=[])
            return "{}", False, terminal, None

        critic._dispatch_tool = dispatch
        with patch("autodesign.agents.critic_agent._index_renders", return_value={}), patch(
            "autodesign.agents.critic_agent._build_user_text", return_value="review"
        ), self.assertRaises(run_control.RunCancelled) as caught:
            critic.critique(
                SimpleNamespace(),
                [],
                [],
                None,
                cancellation_token=token,
            )

        self.assertEqual(caught.exception.phase, "critic.after_tool")
        self.assertEqual(backend.result_appends, 0)

    def test_all_owned_agent_entrypoints_check_cancellation_before_work(self) -> None:
        token = _FakeCancellationToken(cancelled=True)
        cases = (
            lambda: PromptEnhancer.__new__(PromptEnhancer).enhance("brief", cancellation_token=token),
            lambda: ClaimGraphExtractor.__new__(ClaimGraphExtractor).extract(
                Path("paper.pdf"), "paper", cancellation_token=token,
            ),
            lambda: CriticAgent.__new__(CriticAgent).critique(
                object(), [], [], None, cancellation_token=token,
            ),
            lambda: HyperFramesComposer.__new__(HyperFramesComposer).compose(
                "context", Path("project"), cancellation_token=token,
            ),
            lambda: DeckOutlineAgent.__new__(DeckOutlineAgent).plan(
                raw_brief="brief",
                enhanced_brief="brief",
                base_plan=None,
                summaries=[],
                rendered_layers={},
                figures_payload=[],
                tables_payload=[],
                claim_graph=None,
                cancellation_token=token,
            ),
            lambda: PaperMemoryAgent.__new__(PaperMemoryAgent).build(
                memory={"kind": "paper_memory"},
                cancellation_token=token,
            ),
        )
        for call in cases:
            with self.subTest(call=call), self.assertRaises(run_control.RunCancelled):
                call()

    def test_supervised_runner_propagates_cancel_without_terminal_or_telemetry(self) -> None:
        token = _FakeCancellationToken()

        class BarrierRunner(PipelineRunner):
            def _run_inner(self, *args, cancellation_token=None, supervised=False, **kwargs):
                del args, supervised, kwargs
                self.seen_token = cancellation_token
                cancellation_token.cancelled = True
                cancellation_token.raise_if_cancelled("preflight")

        with tempfile.TemporaryDirectory() as raw_tmp:
            runner = BarrierRunner(SimpleNamespace(out_dir=Path(raw_tmp)))
            events: list[str] = []
            with patch("autodesign.runner.log", side_effect=lambda event, **_kwargs: events.append(event)), patch(
                "autodesign.runner.write_run_telemetry_summary"
            ) as telemetry, self.assertRaises(run_control.RunCancelled):
                runner.run(
                    "brief",
                    run_id="supervised-cancel",
                    cancellation_token=token,
                    supervised=True,
                )
            self.assertIs(runner.seen_token, token)
            self.assertNotIn("run.done", events)
            self.assertNotIn("run.error", events)
            telemetry.assert_not_called()

    def test_runner_checks_cancel_before_reference_normalization_or_planning(self) -> None:
        token = _FakeCancellationToken(cancelled=True)
        runner = PipelineRunner(SimpleNamespace())
        with patch("autodesign.runner.normalize_reference_poster") as normalize, patch(
            "autodesign.runner.plan_canvas"
        ) as plan, self.assertRaises(run_control.RunCancelled):
            runner.run(
                "brief",
                run_id="early-cancel",
                cancellation_token=token,
                supervised=True,
            )
        normalize.assert_not_called()
        plan.assert_not_called()


class WorkerTokenHandoffTests(unittest.TestCase):
    def test_all_worker_variants_receive_the_same_authoritative_token(self) -> None:
        token = _FakeCancellationToken()
        seen: list[tuple[str, object]] = []
        helpers = {
            "pipeline": "_run_pipeline",
            "editable_video_render": "_run_editable_video_render",
            "poster_code_edit": "_run_poster_code_edit",
            "pptx_export": "_run_pptx_export",
            "video_export_retry": "_run_video_export_retry",
        }
        for job_kind, helper in helpers.items():
            with self.subTest(job_kind=job_kind), patch.object(
                run_worker,
                helper,
                side_effect=lambda request, cancellation_token, kind=job_kind: (
                    seen.append((kind, cancellation_token)) or {"run_id": request.run_id}
                ),
            ):
                result = run_worker._dispatch(
                    SimpleNamespace(job_kind=job_kind, run_id=f"{job_kind}-run"),
                    token,
                )
                self.assertEqual(result["run_id"], f"{job_kind}-run")
        self.assertEqual([kind for kind, _token in seen], list(helpers))
        self.assertTrue(all(received is token for _kind, received in seen))

    def test_worker_dispatch_rejects_early_cancel_before_any_variant_starts(self) -> None:
        token = _FakeCancellationToken(cancelled=True)
        for job_kind, helper in {
            "pipeline": "_run_pipeline",
            "editable_video_render": "_run_editable_video_render",
            "poster_code_edit": "_run_poster_code_edit",
            "pptx_export": "_run_pptx_export",
            "video_export_retry": "_run_video_export_retry",
        }.items():
            with self.subTest(job_kind=job_kind), patch.object(run_worker, helper) as run, self.assertRaises(
                run_control.RunCancelled,
            ):
                run_worker._dispatch(
                    SimpleNamespace(job_kind=job_kind, run_id=f"{job_kind}-run"),
                    token,
                )
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
