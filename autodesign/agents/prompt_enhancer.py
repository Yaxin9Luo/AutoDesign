"""PromptEnhancer — pre-planner brief structuring.

Expands terse user intents into compact, artifact-aware briefs before the
designer starts. The enhancer should encode current AutoDesign defaults:
HTML-first artifacts, ingest-first handling for attachments, source-backed paper
claims, and generated imagery only where it belongs.

Shape: a single-turn LLM call through the same `LLMBackend` abstraction
the designer uses, but with no tools and no conversation. Input: raw
clean brief plus a separate compact enhance-stage skill index. Output: an
enhanced brief string that the runner combines with its prologues and the
plan-stage index before `DesignerLoop.run`.

Default model comes from settings. Users can override it with `ENHANCER_MODEL`
or skip the stage with `--skip-enhancer` / `SKIP_PROMPT_ENHANCER=1`.

This module is intentionally small — the intelligence lives in
`prompts/prompt_enhancer.md`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings
from ..llm_backend import LLMBackend, TurnResponse, make_backend
from ..run_control import CancellationToken
from ..util.logging import log


@dataclass(frozen=True)
class EnhancerResult:
    """Outcome of a single enhancement pass.

    `enhanced_brief` is what feeds the designer. `original_brief` is kept
    for runtime provenance (so we can A/B the enhancer vs raw).
    `skipped` is True when the stage was bypassed (disabled in settings,
    `--skip-enhancer`, or the brief was already structured enough to
    short-circuit). `wall_time_s` and token usage power cost telemetry.
    """

    enhanced_brief: str
    original_brief: str
    model: str
    skipped: bool = False
    skip_reason: str = ""
    wall_time_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_text: str = ""


class PromptEnhancer:
    """Single-turn pre-planner stage.

    Usage:
        enhancer = PromptEnhancer(settings, system_prompt)
        result = enhancer.enhance(effective_brief)
        # feed result.enhanced_brief to DesignerLoop.run(...)

    The system prompt is passed in (same pattern as DesignerLoop) so the
    runner controls which `prompts/*.md` file drives the stage — useful
    for future per-artifact enhancer variants (e.g. a dedicated
    `prompts/prompt_enhancer_landing.md`).
    """

    def __init__(self, settings: Settings, system_prompt: str):
        self.settings = settings
        self.system_prompt = system_prompt
        self.backend: LLMBackend = make_backend(
            settings, settings.enhancer_model, role="enhancer",
        )

    def enhance(
        self,
        raw_brief: str,
        enhance_context: str = "",
        cancellation_token: CancellationToken | None = None,
    ) -> EnhancerResult:
        """Run the enhancer. Returns an `EnhancerResult` whose
        `enhanced_brief` replaces the raw brief on the way into the
        designer. On API error, returns the raw brief unchanged with
        `skipped=True` + `skip_reason` set — we don't block the pipeline
        on enhancer failures."""
        _raise_if_cancelled(cancellation_token, "prompt_enhancer.start")
        if not self.settings.enable_prompt_enhancer:
            return EnhancerResult(
                enhanced_brief=raw_brief, original_brief=raw_brief,
                model=self.settings.enhancer_model,
                skipped=True, skip_reason="disabled in settings",
            )

        log("prompt.enhance.request",
            model=self.backend.model, backend=self.backend.name,
            raw_chars=len(raw_brief),
            thinking_budget=self.settings.enhancer_thinking_budget)
        wall_start = time.monotonic()

        messages = [{
            "role": "user",
            "content": _user_prompt(raw_brief, enhance_context),
        }]
        thinking_budget = self.settings.enhancer_thinking_budget
        # `max_tokens` must exceed `thinking_budget` by at least a few
        # thousand tokens (Anthropic requirement) — enhancer output is
        # usually 1500-3500 chars (~500-1000 tokens) so 16k is generous.
        max_tokens = 16384
        if thinking_budget > 0 and thinking_budget >= max_tokens:
            thinking_budget = max_tokens - 2048

        try:
            request_kwargs: dict[str, Any] = {
                "system": self.system_prompt,
                "messages": messages,
                "tools": [],
                "thinking_budget": thinking_budget,
                "max_tokens": max_tokens,
            }
            if (
                cancellation_token is not None
                and getattr(cancellation_token, "can_cancel", True)
            ):
                request_kwargs["cancellation_token"] = cancellation_token
            resp: TurnResponse = self.backend.create_turn(
                **request_kwargs,
            )
        except Exception as e:
            wall_s = round(time.monotonic() - wall_start, 2)
            log("prompt.enhance.error",
                error=f"{type(e).__name__}: {e}",
                wall_s=wall_s, fallback="pass-through-raw-brief")
            return EnhancerResult(
                enhanced_brief=raw_brief, original_brief=raw_brief,
                model=self.settings.enhancer_model,
                skipped=True, skip_reason=f"api_error: {type(e).__name__}",
                wall_time_s=wall_s,
            )

        _raise_if_cancelled(cancellation_token, "prompt_enhancer.after_model")

        wall_s = round(time.monotonic() - wall_start, 2)
        enhanced_text = (resp.text or "").strip()

        # Guardrail: if the model returned something suspiciously short
        # or empty, pass the raw brief through rather than poisoning the
        # planner with a half-formed brief. Threshold is deliberately
        # generous — real enhancer outputs are ≥ 1500 chars per spec.
        if len(enhanced_text) < 400:
            log("prompt.enhance.degraded",
                reason="output_too_short",
                output_chars=len(enhanced_text),
                wall_s=wall_s, fallback="pass-through-raw-brief")
            return EnhancerResult(
                enhanced_brief=raw_brief, original_brief=raw_brief,
                model=self.settings.enhancer_model,
                skipped=True, skip_reason="output_too_short",
                wall_time_s=wall_s,
                input_tokens=resp.usage.get("input", 0),
                output_tokens=resp.usage.get("output", 0),
            )

        # The runner adds its prologues and the plan-stage index after this
        # result. The enhancer only carries the clean original brief forward;
        # its stage index must never leak into designer-facing user content.
        body_parts = [
            enhanced_text,
            "## Original user brief (verbatim, for reference)\n\n"
            f"{raw_brief}",
        ]
        final_brief = "\n\n---\n\n".join(body_parts)

        thinking_text = "\n\n".join(
            b.thinking for b in resp.thinking_blocks if not b.is_redacted
        )

        log("prompt.enhance.done",
            model=self.backend.model,
            enhanced_chars=len(enhanced_text),
            final_chars=len(final_brief),
            wall_s=wall_s,
            input_tokens=resp.usage.get("input", 0),
            output_tokens=resp.usage.get("output", 0))

        _raise_if_cancelled(cancellation_token, "prompt_enhancer.before_result")
        return EnhancerResult(
            enhanced_brief=final_brief,
            original_brief=raw_brief,
            model=self.settings.enhancer_model,
            skipped=False,
            wall_time_s=wall_s,
            input_tokens=resp.usage.get("input", 0),
            output_tokens=resp.usage.get("output", 0),
            thinking_text=thinking_text,
        )


def _raise_if_cancelled(
    cancellation_token: CancellationToken | None,
    phase: str,
) -> None:
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled(phase)


def load_enhancer_system_prompt(settings: Settings) -> str:
    """Read `prompts/prompt_enhancer.md` from the settings-configured
    prompts dir. Kept as a free function so the runner can call it
    without constructing the enhancer (e.g. when `enable_prompt_enhancer`
    is already False and we want to short-circuit)."""
    path: Path = settings.prompts_dir / "prompt_enhancer.md"
    return path.read_text(encoding="utf-8")


def _user_prompt(raw_brief: str, enhance_context: str = "") -> str:
    """Wraps the raw brief in a single-turn instruction the enhancer
    reasons about. The system prompt already encodes the output contract; this
    is just the envelope."""
    context = (enhance_context or "").strip()
    context_block = (
        "\n\nEnhance-stage runtime skill index (guidance only; do not copy it "
        "into the enhanced brief):\n\n"
        f"{context}"
        if context
        else ""
    )
    return (
        "Clean user brief:\n\n"
        f"{raw_brief}"
        f"{context_block}\n\n"
        "Emit the enhanced brief per your output contract. Start with "
        "`## Enhanced brief` and end after the pre-flight warnings "
        "section. No preamble, no trailing summary."
    )
