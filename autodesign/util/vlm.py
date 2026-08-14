"""Neutral VLM dispatcher for `tools/ingest_document` (v1.2+).

Why this exists
---------------
The project's planner and critic stay 100 % on the Anthropic SDK
(OpenRouter's Anthropic-compatible endpoint keeps the tool_use
protocol identical, so those paths do not mix in the OpenAI SDK).
Ingest is different: it is a
stateless single-turn "read this document" call, with no tool_use, and
Qwen-VL-Max on OpenRouter is the cost/speed sweet spot for that
workload. Qwen is served only via OpenRouter's OpenAI-compatible
endpoint, so ingest needs a second SDK path.

Scope: THIS MODULE IS INGEST-ONLY. Do not import it from designer.py or
critic.py — doing so would silently downgrade tool_use support.

Public surface
--------------
- `vlm_call_json(...)` — one-shot JSON-producing request with one or
  more attached images. Picks the SDK based on `model`:
    * `qwen/*`, any non-Anthropic id → openai.OpenAI against OpenRouter.
    * `claude-*`, `anthropic/*`      → anthropic.Anthropic against whichever
                                        base_url `settings` dictates.
  Returns the parsed JSON dict. Raises `RuntimeError` on non-JSON output.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from openai import OpenAI
from PIL import Image, UnidentifiedImageError

from ..llm_backend import openai_compat_token_limit_param
from ..run_control import CancellationToken
from .logging import log


# Known VLM providers keyed by OpenRouter model id. Unknown ids fall
# back to prefix inference (see `_provider_for_model`).
_VLM_PROVIDERS: dict[str, str] = {
    "qwen/qwen-vl-max": "openai",
    "qwen/qwen-vl-plus": "openai",
    "qwen/qwen2.5-vl-72b-instruct": "openai",
    "qwen/qwen2.5-vl-32b-instruct": "openai",
}

# Anthropic-SDK endpoint for stock Anthropic when `anthropic_base_url`
# is None. OpenAI SDK (for Qwen) always hits OpenRouter v1.
_OPENROUTER_OPENAI_BASE = "https://openrouter.ai/api/v1"


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _status_code(exc: Exception) -> int | None:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def _is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        _status_code(exc) == 429
        or "429" in msg
        or "rate limit" in msg
        or "too many requests" in msg
        or "请求次数超过限制" in str(exc)
    )


def _is_retryable_vlm_error(exc: Exception) -> bool:
    if _is_rate_limit_error(exc):
        return True
    code = _status_code(exc)
    if code in {408, 409, 425, 500, 502, 503, 504}:
        return True
    msg = str(exc).lower()
    return (
        "timeout" in msg
        or "timed out" in msg
        or "temporarily unavailable" in msg
        or "connection error" in msg
        or "connection reset" in msg
        or "unexpected_eof" in msg
    )


def _retry_after_s(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        val = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:
        return None
    if not val:
        return None
    try:
        return max(0.0, float(val))
    except ValueError:
        return None


def _retry_delay_s(exc: Exception, attempt: int) -> float:
    max_delay = _float_env("INGEST_VLM_RETRY_MAX_SLEEP_S", 300.0)
    retry_after = _retry_after_s(exc)
    if retry_after is not None:
        return min(retry_after, max_delay)
    if _is_rate_limit_error(exc):
        base = _float_env("INGEST_VLM_RATE_LIMIT_SLEEP_S", 65.0)
    else:
        base = _float_env("INGEST_VLM_RETRY_BASE_S", 2.0)
    return min(max_delay, base * (2 ** max(0, attempt - 1)))


# ────────────────────────── data types ────────────────────────────────

@dataclass(frozen=True)
class VlmImage:
    """One image attachment for a VLM call.

    `data` is the raw bytes on disk; `media_type` is the MIME we report
    to the model (it's fine if the bytes are actually JPEG but we say
    "image/png" — both providers sniff).
    """
    data: bytes
    media_type: str = "image/png"

    @classmethod
    def from_path(cls, path: Path) -> "VlmImage":
        mt = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
        return cls(data=path.read_bytes(), media_type=mt)


def _prepare_images_for_transport(images: list[VlmImage]) -> list[VlmImage]:
    """Bound VLM request body size without touching source artifacts.

    Ingest sometimes extracts native PDF rasters that are far larger than a
    single-turn caption/classification request needs. Sending those losslessly
    can trip OpenRouter/OpenAI-compatible 413 limits. We keep the on-disk crop
    unchanged for poster use and only compress the bytes attached to the VLM.
    """
    max_edge = max(256, _int_env("INGEST_VLM_MAX_IMAGE_EDGE", 1600))
    max_bytes = max(128_000, _int_env("INGEST_VLM_MAX_IMAGE_BYTES", 1_500_000))
    quality = min(95, max(35, _int_env("INGEST_VLM_JPEG_QUALITY", 82)))
    prepared: list[VlmImage] = []
    for idx, img in enumerate(images):
        if len(img.data) <= max_bytes:
            try:
                with Image.open(BytesIO(img.data)) as src:
                    w, h = src.size
                if max(w, h) <= max_edge:
                    prepared.append(img)
                    continue
            except (UnidentifiedImageError, OSError, ValueError):
                prepared.append(img)
                continue
        try:
            prepared.append(
                _compress_vlm_image(
                    img,
                    idx=idx,
                    max_edge=max_edge,
                    max_bytes=max_bytes,
                    quality=quality,
                )
            )
        except Exception as exc:
            log(
                "vlm.image.downsample_failed",
                index=idx,
                media_type=img.media_type,
                bytes=len(img.data),
                error=str(exc)[:240],
            )
            prepared.append(img)
    return prepared


def _compress_vlm_image(
    img: VlmImage,
    *,
    idx: int,
    max_edge: int,
    max_bytes: int,
    quality: int,
) -> VlmImage:
    with Image.open(BytesIO(img.data)) as src:
        original_size = src.size
        work = src.convert("RGBA") if src.mode in {"RGBA", "LA", "P"} else src.convert("RGB")
        if work.mode == "RGBA":
            bg = Image.new("RGBA", work.size, (255, 255, 255, 255))
            bg.alpha_composite(work)
            work = bg.convert("RGB")
        if max(work.size) > max_edge:
            work.thumbnail((max_edge, max_edge), Image.LANCZOS)

        out = BytesIO()
        q = quality
        while True:
            out.seek(0)
            out.truncate(0)
            work.save(out, format="JPEG", quality=q, optimize=True, progressive=True)
            if out.tell() <= max_bytes or q <= 45:
                break
            q = max(45, q - 8)

        data = out.getvalue()
        if len(img.data) <= max_bytes and len(data) >= len(img.data):
            log(
                "vlm.image.downsample_skipped",
                index=idx,
                media_type=img.media_type,
                original_size=f"{original_size[0]}x{original_size[1]}",
                original_bytes=len(img.data),
                reason="compressed_payload_not_smaller",
            )
            return img
        log(
            "vlm.image.downsampled",
            index=idx,
            original_media_type=img.media_type,
            original_size=f"{original_size[0]}x{original_size[1]}",
            original_bytes=len(img.data),
            media_type="image/jpeg",
            size=f"{work.size[0]}x{work.size[1]}",
            bytes=len(data),
            quality=q,
        )
        return VlmImage(data=data, media_type="image/jpeg")


# ────────────────────────── public API ────────────────────────────────

def vlm_call_json(
    *,
    settings,  # `config.Settings` — typed loosely to avoid import cycle
    model: str,
    system: str,
    user_text: str,
    images: list[VlmImage] | None = None,
    max_tokens: int = 4096,
    timeout_s: float | None = None,
    max_retries: int | None = None,
    cancellation_token: CancellationToken | None = None,
) -> dict[str, Any]:
    """Single-turn "describe these images as JSON" request.

    Routes to the Anthropic SDK or OpenAI SDK based on `model`. The
    prompt is expected to ask for a fenced ```json ...``` block; we
    parse that out and return the dict. Raises `RuntimeError` on
    non-JSON output.
    """
    _raise_if_cancelled(cancellation_token, "vlm.before_prepare")
    images = _prepare_images_for_transport(images or [])
    provider = _provider_for_model(model, settings=settings)
    timeout_s = timeout_s or getattr(settings, "ingest_http_timeout", 600.0)

    log("vlm.request", model=model, provider=provider,
        n_images=len(images), timeout_s=timeout_s)

    if max_retries is None:
        max_retries = _int_env("INGEST_VLM_MAX_RETRIES", 3)
    max_retries = max(0, int(max_retries))
    max_attempts = max_retries + 1
    text = ""
    for attempt in range(1, max_attempts + 1):
        _raise_if_cancelled(cancellation_token, "vlm.retry.before_request")
        try:
            if provider == "anthropic":
                text = _call_anthropic(
                    settings=settings, model=model, system=system,
                    user_text=user_text, images=images,
                    max_tokens=max_tokens, timeout_s=timeout_s,
                    cancellation_token=cancellation_token,
                )
            else:
                text = _call_openai(
                    settings=settings, model=model, system=system,
                    user_text=user_text, images=images,
                    max_tokens=max_tokens, timeout_s=timeout_s,
                    cancellation_token=cancellation_token,
                )
            _raise_if_cancelled(cancellation_token, "vlm.retry.after_request")
            break
        except Exception as e:
            _raise_if_cancelled(cancellation_token, "vlm.retry.after_error")
            if attempt >= max_attempts or not _is_retryable_vlm_error(e):
                raise
            delay_s = _retry_delay_s(e, attempt)
            log(
                "vlm.retry",
                model=model,
                provider=provider,
                attempt=attempt,
                max_attempts=max_attempts,
                delay_s=round(delay_s, 1),
                error=str(e)[:400],
            )
            if cancellation_token is None:
                time.sleep(delay_s)
            elif cancellation_token.wait(delay_s):
                cancellation_token.raise_if_cancelled("vlm.retry.wait")

    _raise_if_cancelled(cancellation_token, "vlm.before_parse")
    return _parse_json_block(text)


# ────────────────────────── internals ─────────────────────────────────

def _provider_for_model(model: str, *, settings: Any | None = None) -> str:
    if model in _VLM_PROVIDERS:
        return _VLM_PROVIDERS[model]
    if model.startswith((
        "claude-", "anthropic/", "aws.claude", "vertex.claude",
    )):
        return "anthropic"
    if model.startswith(("gpt-", "openai/", "o1", "o3", "o4")):
        return "openai"
    # Unknown → assume the configured OpenAI-compatible endpoint.
    return "openai"


def _call_anthropic(
    *,
    settings,
    model: str,
    system: str,
    user_text: str,
    images: list[VlmImage],
    max_tokens: int,
    timeout_s: float,
    cancellation_token: CancellationToken | None = None,
) -> str:
    _raise_if_cancelled(cancellation_token, "vlm.anthropic.before_request")
    kwargs: dict[str, Any] = {"timeout": timeout_s, "max_retries": 0}
    if settings.anthropic_api_key:
        kwargs["api_key"] = settings.anthropic_api_key
    custom_headers = dict(
        getattr(settings, "anthropic_custom_headers", {}) or {},
    )
    if custom_headers:
        kwargs["default_headers"] = custom_headers
    if settings.anthropic_base_url:
        kwargs["base_url"] = settings.anthropic_base_url
    client = Anthropic(**kwargs)

    content: list[dict[str, Any]] = []
    for img in images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img.media_type,
                "data": base64.standard_b64encode(img.data).decode("ascii"),
            },
        })
    content.append({"type": "text", "text": user_text})

    _raise_if_cancelled(cancellation_token, "vlm.anthropic.before_request")
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    _raise_if_cancelled(cancellation_token, "vlm.anthropic.after_request")
    parts = [getattr(b, "text", "") for b in resp.content
             if getattr(b, "type", None) == "text"]
    return "".join(parts)


def _call_openai(
    *,
    settings,
    model: str,
    system: str,
    user_text: str,
    images: list[VlmImage],
    max_tokens: int,
    timeout_s: float,
    cancellation_token: CancellationToken | None = None,
) -> str:
    _raise_if_cancelled(cancellation_token, "vlm.openai.before_request")
    # OpenAI-compatible VLMs may live on OpenRouter or a self-hosted endpoint.
    # Prefer the explicit compat key/base when set.
    api_key = (
        getattr(settings, "openai_compat_api_key", None)
        or getattr(settings, "openrouter_api_key", None)
        or settings.anthropic_api_key
    )
    if not api_key:
        raise RuntimeError(
            "VLM openai branch needs OPENAI_COMPAT_API_KEY or OPENROUTER_API_KEY."
        )

    base_url = getattr(settings, "openai_compat_base_url", None) or _OPENROUTER_OPENAI_BASE
    user_content: list[dict[str, Any]] = []
    for img in images:
        b64 = base64.standard_b64encode(img.data).decode("ascii")
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{img.media_type};base64,{b64}"},
        })
    user_content.append({"type": "text", "text": user_text})

    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout_s,
        max_retries=0,
    )

    token_limit_param = openai_compat_token_limit_param(model, base_url)
    _raise_if_cancelled(cancellation_token, "vlm.openai.before_request")
    resp = client.chat.completions.create(
        model=model,
        **{token_limit_param: max_tokens},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
    )
    _raise_if_cancelled(cancellation_token, "vlm.openai.after_request")
    choice = resp.choices[0] if resp.choices else None
    if not choice or not choice.message or not choice.message.content:
        return ""
    return choice.message.content


def _raise_if_cancelled(
    cancellation_token: CancellationToken | None,
    phase: str,
) -> None:
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled(phase)


def _parse_json_block(text: str) -> dict[str, Any]:
    """Accept a fenced ```json ...``` block OR a raw JSON object."""
    m = _JSON_BLOCK_RE.search(text)
    payload = m.group(1) if m else text.strip()
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"VLM returned non-JSON: {e}; got {text[:300]!r}"
        )
    if not isinstance(value, dict):
        raise RuntimeError(f"VLM returned JSON but not a dict: {type(value).__name__}")
    return value
