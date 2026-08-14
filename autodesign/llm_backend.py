"""LLMBackend — provider-agnostic abstraction over Claude / Kimi / Doubao / etc.

Why this exists:
- We want to mix-and-match closed (Anthropic) and open-source (Moonshot Kimi,
  DeepSeek, Qwen, GLM, ...) reasoning models depending on cost / capability /
  data-collection priority.
- Different providers expose reasoning in different shapes: Anthropic uses
  `thinking` content blocks with cryptographic `signature`s for replay;
  OpenAI-compatible providers (Kimi, DeepSeek-R1, Doubao, OpenRouter unified)
  expose `reasoning` / `reasoning_content` as a string field with no signature.
- Tool calling protocol differs too: Anthropic `tool_use` blocks vs OpenAI
  `tool_calls` list of `{id, function: {name, arguments}}`.

Design:
- `LLMBackend` is a small Protocol with three operations: `create_turn`,
  `append_assistant`, `append_tool_results`. The conversation `messages`
  list lives in the backend's NATIVE shape (Anthropic content blocks vs
  OpenAI message objects) — only the backend understands its own layout.
- `TurnResponse` is the unified result the planner / critic see: thinking
  blocks, text, tool calls, stop reason, usage. Mapped from native to our
  schema's `ThinkingBlockRecord` so runtime handling is provider-agnostic.

Adding a new provider = subclass `LLMBackend` and register it in `make_backend`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .run_control import CancellationToken
from .schema import ThinkingBlockRecord
from .util.logging import log


@dataclass
class ToolCall:
    """Backend-agnostic tool call (the planner doesn't care which protocol
    produced it)."""
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class TurnResponse:
    """One LLM turn's worth of model output, normalized across providers.

    `raw_assistant_content` is the backend's native message representation,
    held opaquely so `append_assistant` can round-trip it back into the
    conversation in whatever shape the provider expects (critical for
    Anthropic — thinking blocks carry signatures that must survive verbatim).
    """
    thinking_blocks: list[ThinkingBlockRecord] = field(default_factory=list)
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None  # normalized: end_turn / tool_use / max_tokens / refusal / other
    usage: dict[str, int] = field(default_factory=dict)
    # Native-format payload for round-tripping. Anthropic = list of content
    # blocks (with signatures); OpenAI-compat = a message dict.
    raw_assistant_content: Any = None


# Normalised stop reason vocab — small + provider-agnostic.
StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "other"]
_LLM_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
_LLM_MAX_ATTEMPTS = 3
_OPENAI_MAX_COMPLETION_TOKEN_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def openai_compat_token_limit_param(model: str, base_url: str | None) -> str:
    """Return the Chat Completions token-limit parameter for a model.

    Newer first-party OpenAI reasoning models reject `max_tokens` and require
    `max_completion_tokens`. Keep OpenRouter on `max_tokens` because its
    compatibility layer has historically normalized that name.
    """
    base = str(base_url or "").lower()
    if "openrouter.ai" in base:
        return "max_tokens"
    normalized = str(model or "").strip().lower()
    if normalized.startswith("openai/"):
        normalized = normalized.split("/", 1)[1]
    if normalized.startswith(_OPENAI_MAX_COMPLETION_TOKEN_MODEL_PREFIXES):
        return "max_completion_tokens"
    return "max_tokens"


class LLMBackend(Protocol):
    """All planner/critic LLM access goes through this. Implementations live
    below in the same file."""

    name: str         # short id: "anthropic" | "openai_compat"
    model: str        # provider-specific model id

    def create_turn(
        self,
        *,
        system: str,
        messages: list[Any],
        tools: list[dict[str, Any]],
        thinking_budget: int = 0,
        max_tokens: int = 16384,
        extra_headers: dict[str, str] | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> TurnResponse:
        ...

    def append_assistant(
        self, messages: list[Any], response: TurnResponse,
    ) -> None:
        """Append assistant turn (with thinking + tool_calls) to the messages
        list in this backend's native format. Mutates in place."""

    def append_tool_results(
        self,
        messages: list[Any],
        results: list[tuple[str, str, bool]],
    ) -> None:
        """Append tool results to the messages list. `results` is a list of
        `(tool_use_id, json_serialized_payload, is_error)` tuples. Mutates."""

    def vision_user_message(
        self, *, image_b64: str, media_type: str, text: str,
    ) -> Any:
        """Build a user message with an inline image + text. Anthropic and
        OpenAI-compat have different content-block shapes for vision; the
        backend handles its own. Returns a single message dict suitable for
        appending to a `messages` list."""


# ─────────────────────────── Anthropic ────────────────────────────────────


class AnthropicBackend:
    """Native Anthropic Messages API. Used for Claude models.

    Reasoning: `thinking` + `redacted_thinking` content blocks with
    cryptographic `signature` / `data` fields. Both must round-trip
    verbatim or the next turn 400s.

    Tool calling: native `tool_use` content blocks; results return as
    `tool_result` blocks inside a user-role message.
    """

    name = "anthropic"

    def __init__(self, settings, model: str):
        from anthropic import Anthropic

        self.model = model
        self._enable_interleaved = settings.enable_interleaved_thinking
        client_kwargs: dict[str, Any] = {}
        if settings.anthropic_api_key:
            client_kwargs["api_key"] = settings.anthropic_api_key
        custom_headers = dict(
            getattr(settings, "anthropic_custom_headers", {}) or {},
        )
        if custom_headers:
            client_kwargs["default_headers"] = custom_headers
        if settings.anthropic_base_url:
            client_kwargs["base_url"] = settings.anthropic_base_url
        client_kwargs["max_retries"] = 0
        self.client = Anthropic(**client_kwargs)
        self._timeout_s = max(1.0, float(getattr(settings, "llm_http_timeout", 180.0)))

    def create_turn(
        self,
        *,
        system: str,
        messages: list[Any],
        tools: list[dict[str, Any]],
        thinking_budget: int = 0,
        max_tokens: int = 16384,
        extra_headers: dict[str, str] | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> TurnResponse:
        _raise_if_cancelled(cancellation_token, "llm.anthropic.before_request")
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "tools": tools,
            "messages": messages,
        }
        if thinking_budget > 0:
            kwargs["thinking"] = {
                "type": "enabled", "budget_tokens": thinking_budget,
            }
        merged_headers: dict[str, str] = {}
        if thinking_budget > 0 and self._enable_interleaved:
            merged_headers["anthropic-beta"] = "interleaved-thinking-2025-05-14"
        if extra_headers:
            merged_headers.update(extra_headers)
        if merged_headers:
            kwargs["extra_headers"] = merged_headers
        kwargs["timeout"] = self._timeout_s

        started = time.monotonic()
        log(
            "llm.request",
            backend=self.name,
            model=self.model,
            timeout_s=self._timeout_s,
            n_messages=len(messages),
            n_tools=len(tools),
        )
        _raise_if_cancelled(cancellation_token, "llm.anthropic.before_request")
        resp = _create_openai_turn_with_retries(
            self.client.messages.create,
            kwargs,
            model=self.model,
            backend=self.name,
            cancellation_token=cancellation_token,
        )
        _raise_if_cancelled(cancellation_token, "llm.anthropic.after_request")
        log(
            "llm.response",
            backend=self.name,
            model=self.model,
            wall_s=round(time.monotonic() - started, 2),
            stop_reason=getattr(resp, "stop_reason", None),
        )

        thinking: list[ThinkingBlockRecord] = []
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for b in resp.content:
            t = getattr(b, "type", None)
            if t == "thinking":
                thinking.append(ThinkingBlockRecord(
                    thinking=getattr(b, "thinking", "") or "",
                    signature=getattr(b, "signature", "") or "",
                    is_redacted=False,
                ))
            elif t == "redacted_thinking":
                thinking.append(ThinkingBlockRecord(
                    thinking="",
                    signature=getattr(b, "data", "") or "",
                    is_redacted=True,
                ))
            elif t == "text":
                text_parts.append(b.text)
            elif t == "tool_use":
                tool_calls.append(ToolCall(
                    id=b.id, name=b.name, input=dict(b.input),
                ))

        return TurnResponse(
            thinking_blocks=thinking,
            text="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=_normalize_stop(getattr(resp, "stop_reason", None), "anthropic"),
            usage={
                "input": getattr(resp.usage, "input_tokens", 0) or 0,
                "output": getattr(resp.usage, "output_tokens", 0) or 0,
                "cache_read": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
                "cache_create": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
            },
            raw_assistant_content=resp.content,
        )

    def append_assistant(self, messages: list[Any], response: TurnResponse) -> None:
        # Pass raw SDK content list back verbatim — signatures must survive.
        messages.append({"role": "assistant", "content": response.raw_assistant_content})

    def append_tool_results(
        self,
        messages: list[Any],
        results: list[tuple[str, str, bool]],
    ) -> None:
        blocks = [
            {
                "type": "tool_result",
                "tool_use_id": tu_id,
                "content": payload,
                "is_error": is_err,
            }
            for tu_id, payload, is_err in results
        ]
        messages.append({"role": "user", "content": blocks})

    def vision_user_message(
        self, *, image_b64: str, media_type: str, text: str,
    ) -> dict[str, Any]:
        return {
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                {"type": "text", "text": text},
            ],
        }


# ──────────────────────── OpenAI-compatible ───────────────────────────────


class OpenAICompatBackend:
    """OpenAI Chat Completions-compatible backend.

    Used for: OpenRouter (any model), Moonshot Kimi, DeepSeek, ByteDance
    Doubao, vLLM/SGLang self-host, OpenAI itself.

    Reasoning extraction is best-effort across provider field names:
    `reasoning` (OpenRouter unified), `reasoning_content` (DeepSeek/Kimi
    native), `thought` (some local serves). No `signature` mechanism —
    on round-trip we preserve `reasoning_content` in the message dict but
    providers don't verify it; if rejected we drop it (caller can fall
    back via `drop_reasoning_on_replay=True`).
    """

    name = "openai_compat"

    def __init__(
        self,
        settings,
        model: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
    ):
        from openai import OpenAI

        self.model = model
        # Resolve base_url + api_key with a precedence chain so a single
        # OPENROUTER_API_KEY can power both Anthropic-style and OpenAI-style
        # routing without extra config.
        resolved_url = (
            base_url
            or getattr(settings, "openai_compat_base_url", None)
            or "https://openrouter.ai/api/v1"
        )
        resolved_key = (
            api_key
            or getattr(settings, "openai_compat_api_key", None)
            or settings.anthropic_api_key  # OPENROUTER_API_KEY lives here when OR-mode
        )
        self.client = OpenAI(
            base_url=resolved_url,
            api_key=resolved_key,
            max_retries=0,
        )
        self._base_url = str(resolved_url)
        self._timeout_s = max(1.0, float(getattr(settings, "llm_http_timeout", 180.0)))

    def create_turn(
        self,
        *,
        system: str,
        messages: list[Any],
        tools: list[dict[str, Any]],
        thinking_budget: int = 0,
        max_tokens: int = 16384,
        extra_headers: dict[str, str] | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> TurnResponse:
        _raise_if_cancelled(cancellation_token, "llm.openai.before_prepare")
        # Convert our Anthropic-shape tool schemas to OpenAI function shape.
        oai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t["input_schema"],
                },
            }
            for t in tools
        ]
        # Prepend system as first message (OpenAI style).
        oai_messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        oai_messages.extend(messages)

        token_limit_param = openai_compat_token_limit_param(self.model, self._base_url)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": oai_messages,
            "tools": oai_tools,
            token_limit_param: max_tokens,
            "timeout": self._timeout_s,
        }
        extra_body: dict[str, Any] = {}
        if thinking_budget > 0:
            # OpenRouter unified reasoning param — providers that don't support
            # it ignore it silently. For native Moonshot / DeepSeek API the
            # field name may differ but reasoning is on by default for thinking
            # variants of those models.
            extra_body["reasoning"] = {"max_tokens": thinking_budget}
        # v2.7.1 — OpenRouter provider routing. `OPENROUTER_PROVIDER_IGNORE`
        # accepts a comma-separated list of upstream provider names to
        # exclude (e.g. "Together,DeepInfra"). Workaround for short-window
        # rate-limit storms on a single upstream — observed 2026-04-26 with
        # `deepseek/deepseek-v4-pro` getting hard-pinned to Together.
        # Silent no-op when the env var is unset or empty.
        import os as _os
        ignored = (_os.getenv("OPENROUTER_PROVIDER_IGNORE", "")
                   or "").strip()
        if ignored:
            extra_body["provider"] = {
                "ignore": [p.strip() for p in ignored.split(",") if p.strip()],
            }
        if extra_body:
            kwargs["extra_body"] = extra_body
        if extra_headers:
            kwargs["extra_headers"] = extra_headers

        started = time.monotonic()
        log(
            "llm.request",
            backend=self.name,
            model=self.model,
            timeout_s=self._timeout_s,
            n_messages=len(oai_messages),
            n_tools=len(oai_tools),
        )
        resp = _create_openai_turn_with_retries(
            self.client.chat.completions.create,
            kwargs,
            model=self.model,
            backend=self.name,
            cancellation_token=cancellation_token,
        )
        _raise_if_cancelled(cancellation_token, "llm.openai.after_request")
        log(
            "llm.response",
            backend=self.name,
            model=self.model,
            wall_s=round(time.monotonic() - started, 2),
            stop_reason=getattr(resp.choices[0], "finish_reason", None)
            if getattr(resp, "choices", None) else None,
        )
        # v2.7.2 — defensive guard. OpenRouter occasionally returns 200 OK
        # with `choices: null` when the upstream silently rate-limits or
        # content-filters (observed 2026-04-26 with deepseek/deepseek-v4-pro).
        # Surface a readable error instead of `'NoneType' object is not
        # subscriptable`.
        if not resp.choices:
            err_meta = getattr(resp, "error", None) or {}
            raise RuntimeError(
                f"upstream returned no choices for model={self.model}; "
                f"likely silent rate-limit or content filter from OpenRouter. "
                f"error_meta={err_meta}"
            )
        choice = resp.choices[0]
        msg = choice.message

        # Reasoning extraction — try common field names + raw dict fallback.
        reasoning_text = ""
        for attr in ("reasoning", "reasoning_content", "thought"):
            v = getattr(msg, attr, None)
            if v:
                reasoning_text = v
                break
        if not reasoning_text:
            try:
                msg_dict = msg.model_dump()
                for k in ("reasoning", "reasoning_content", "thought"):
                    if msg_dict.get(k):
                        reasoning_text = msg_dict[k]
                        break
            except Exception:
                pass

        thinking: list[ThinkingBlockRecord] = []
        if reasoning_text:
            thinking.append(ThinkingBlockRecord(
                thinking=reasoning_text,
                signature="",  # OpenAI-compat has no signature mechanism
                is_redacted=False,
            ))

        # Tool calls
        tool_calls: list[ToolCall] = []
        msg_tool_calls = getattr(msg, "tool_calls", None) or []
        for tc in msg_tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                # Some providers occasionally emit non-JSON tool args; preserve
                # the raw string so the policy can learn the failure mode.
                args = {"_raw_tool_args": tc.function.arguments or ""}
            tool_calls.append(ToolCall(
                id=tc.id, name=tc.function.name, input=args,
            ))

        # Cache token telemetry (DeepSeek + others expose under prompt_tokens_details)
        cache_read = 0
        if resp.usage is not None:
            ptd = getattr(resp.usage, "prompt_tokens_details", None)
            if ptd is not None:
                cache_read = getattr(ptd, "cached_tokens", 0) or 0

        # Build the assistant message dict for round-tripping into the next turn.
        # Some providers (e.g. Anthropic-via-OpenRouter via OpenAI endpoint)
        # reject `reasoning_content` on subsequent turns, so by default we
        # DON'T include it. We keep thinking blocks in the runtime response
        # object without persisting model reasoning as training data.
        #
        # v2.7.2 — DeepSeek V4-pro (and presumably future DeepSeek thinking
        # models) ENFORCE `reasoning_content` round-trip when reasoning is
        # enabled — without it the next turn 400s with
        # `The reasoning_content in the thinking mode must be passed back
        # to the API`. Detected by model id prefix `deepseek/`. This may
        # also be needed for other thinking-strict families later; extend
        # the prefix tuple as those surface.
        #
        # v2.7.4 — `content` MUST always be present on an assistant message,
        # even when it is empty. OpenAI Chat Completions spec requires the
        # field; OpenRouter and Anthropic tolerate omission, but stricter
        # upstream providers (Alibaba-routed `qwen/qwen-vl-max`, observed
        # 2026-04-26) reject the message with `<400> InternalError.Algo.
        # InvalidParameter: The model input format error` once a
        # tool-only assistant turn is replayed in the history. Use empty
        # string (not None) so the field is never sent as JSON `null`,
        # which a third class of providers also rejects.
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": getattr(msg, "content", None) or "",
        }
        if msg_tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg_tool_calls
            ]
        _STRICT_REASONING_PREFIXES = ("deepseek/",)
        if reasoning_text and self.model.startswith(_STRICT_REASONING_PREFIXES):
            assistant_msg["reasoning_content"] = reasoning_text

        return TurnResponse(
            thinking_blocks=thinking,
            text=getattr(msg, "content", None) or "",
            tool_calls=tool_calls,
            stop_reason=_normalize_stop(choice.finish_reason, "openai"),
            usage={
                "input": getattr(resp.usage, "prompt_tokens", 0) or 0,
                "output": getattr(resp.usage, "completion_tokens", 0) or 0,
                "cache_read": cache_read,
                "cache_create": 0,
            },
            raw_assistant_content=assistant_msg,
        )

    def append_assistant(self, messages: list[Any], response: TurnResponse) -> None:
        messages.append(response.raw_assistant_content)

    def append_tool_results(
        self,
        messages: list[Any],
        results: list[tuple[str, str, bool]],
    ) -> None:
        for tu_id, payload, _is_err in results:
            messages.append({
                "role": "tool",
                "tool_call_id": tu_id,
                "content": payload,
            })

    def vision_user_message(
        self, *, image_b64: str, media_type: str, text: str,
    ) -> dict[str, Any]:
        # OpenAI-compat uses an `image_url` block with a data: URI string.
        # v2.7.4 — text block precedes image_url. The OpenAI Chat
        # Completions vision examples use this order; Alibaba's
        # `qwen-vl-max` strict endpoint rejected the reverse order with
        # `InternalError.Algo.InvalidParameter` once accumulated across
        # multiple critic turns.
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url",
                 "image_url": {"url": f"data:{media_type};base64,{image_b64}"}},
            ],
        }


def _create_openai_turn_with_retries(
    create_fn: Any,
    kwargs: dict[str, Any],
    *,
    model: str,
    backend: str,
    cancellation_token: CancellationToken | None = None,
) -> Any:
    last_exc: Exception | None = None
    for attempt in range(1, _LLM_MAX_ATTEMPTS + 1):
        _raise_if_cancelled(cancellation_token, "llm.retry.before_request")
        try:
            response = create_fn(**kwargs)
            _raise_if_cancelled(cancellation_token, "llm.retry.after_request")
            return response
        except Exception as exc:  # noqa: BLE001 - provider SDKs vary here
            last_exc = exc
            _raise_if_cancelled(cancellation_token, "llm.retry.after_error")
            if attempt >= _LLM_MAX_ATTEMPTS or not _is_retryable_llm_error(exc):
                raise
            delay_s = _llm_retry_delay_s(exc, attempt)
            log(
                "llm.retry",
                backend=backend,
                model=model,
                attempt=attempt,
                max_attempts=_LLM_MAX_ATTEMPTS,
                delay_s=round(delay_s, 2),
                error=f"{type(exc).__name__}: {exc}",
            )
            if cancellation_token is None:
                time.sleep(delay_s)
            elif cancellation_token.wait(delay_s):
                cancellation_token.raise_if_cancelled("llm.retry.wait")
    assert last_exc is not None
    raise last_exc


def _raise_if_cancelled(
    cancellation_token: CancellationToken | None,
    phase: str,
) -> None:
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled(phase)


def _is_retryable_llm_error(exc: Exception) -> bool:
    retryable = getattr(exc, "retryable", None)
    if retryable is not None:
        return bool(retryable)
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in {408, 409} or status in _LLM_RETRY_STATUS_CODES:
        return True
    if isinstance(status, int) and status >= 500:
        return True
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "rate limit",
            "ratelimit",
            "429",
            "500",
            "502",
            "503",
            "504",
            "too many requests",
            "bad gateway",
            "gateway timeout",
            "service unavailable",
            "internal server error",
            "temporarily unavailable",
            "timeout",
            "timed out",
            "read timeout",
            "connection error",
            "server disconnected",
        )
    )


def _llm_retry_delay_s(exc: Exception, attempt: int) -> float:
    retry_after = _retry_after_s(exc)
    if retry_after is not None:
        return min(max(1.0, retry_after), 90.0)
    status = getattr(exc, "status_code", None)
    if status == 429 or "429" in str(exc):
        return 65.0
    return min(8.0 * (2 ** (attempt - 1)), 45.0)


def _retry_after_s(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        value = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:
        return None
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_stop(raw: str | None, provider: str) -> str:
    """Map provider-specific finish/stop reason strings to a small shared vocab."""
    if raw is None:
        return "other"
    if provider == "anthropic":
        # Anthropic returns: end_turn, tool_use, max_tokens, stop_sequence, refusal, pause_turn
        if raw in ("end_turn", "tool_use", "max_tokens", "refusal"):
            return raw
        return "other"
    if provider == "openai":
        # OpenAI: stop, tool_calls, length, content_filter, function_call
        m = {"stop": "end_turn", "tool_calls": "tool_use",
             "length": "max_tokens", "content_filter": "refusal",
             "function_call": "tool_use"}
        return m.get(raw, "other")
    return "other"


def _auto_provider(model: str) -> str:
    """Pick a backend based on model id when settings don't pin one."""
    m = model.lower()
    if m.startswith((
        "anthropic/",
        "claude-",
        "aws.claude",
        "vertex.claude",
    )):
        return "anthropic"
    # Anything else routed through OpenRouter / native OpenAI-compat APIs.
    # Covers: moonshotai/kimi-*, deepseek/*, qwen/*, mistralai/*, meta-llama/*,
    # google/* (Gemini-OpenAI-compat), openai/*, etc.
    return "openai_compat"


def make_backend(settings, model: str, *, role: str = "planner") -> LLMBackend:
    """Construct a backend instance for `model`.

    Provider precedence: settings.<role>_provider override > auto-detect from
    model id prefix. The settings hook is `designer_provider` / `critic_provider`
    (Literal["auto", "anthropic", "openai_compat"], default "auto").
    """
    explicit = getattr(settings, f"{role}_provider", "auto")
    provider = explicit if explicit != "auto" else _auto_provider(model)

    if provider == "anthropic":
        return AnthropicBackend(settings, model)
    if provider == "openai_compat":
        return OpenAICompatBackend(settings, model)
    raise ValueError(f"unknown LLM provider: {provider!r}")
