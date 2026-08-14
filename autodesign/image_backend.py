"""Multi-provider image-generation backend (v2.5, hardened v2.7.5).

Mirrors the shape of `llm_backend.py` for the same reason: keep tool code
provider-neutral so users can swap image models without touching
`tools/generate_image.py` / `tools/generate_background.py`.

Three concrete backends today:

- `GeminiImageBackend` — wraps `google.genai` (the original NBP path).
  Selected when `image_model` starts with `gemini-` or `imagen-`. Requires
  `GEMINI_API_KEY`.
- `OpenRouterImageBackend` — POSTs to OpenRouter's chat/completions endpoint
  with `modalities=["image","text"]` + `image_config={aspect_ratio,
  image_size}`. Selected for everything else (default model
  `google/gemini-2.5-flash-image`). Reuses `OPENROUTER_API_KEY`.
- `OpenAICompatImageBackend` — POSTs to OpenAI-compatible native
  `/images/generations` and `/images/edits` endpoints. Used for model ids
  such as `gpt-image-2` behind `OPENAI_COMPAT_BASE_URL`.
Plus a wrapper:

- `FallbackImageBackend` (v2.7.5) — wraps a primary backend and an
  optional fallback backend. On `provider_unavailable` failures from
  the primary (404 / no-endpoints-for-modality / model-not-found) it
  transparently retries against the fallback, logging
  `image.fallback.attempt`. All other failure categories (safety_filter,
  api 5xx, malformed responses) propagate from the primary unchanged —
  we only fall back when the user-chosen MODEL is the broken thing.

Routing rules in `make_image_backend(settings)`:
- `IMAGE_PROVIDER=gemini`     → GeminiImageBackend
- `IMAGE_PROVIDER=openrouter` → OpenRouterImageBackend
- `IMAGE_PROVIDER=openai_compat` → OpenAICompatImageBackend
- `IMAGE_PROVIDER=auto` (default) → infer from `image_model` prefix:
    `gemini-*` / `imagen-*`  → Gemini
    everything else          → OpenRouter

The factory wraps the resolved backend in `FallbackImageBackend` when
`settings.image_fallback_model` is non-empty AND points at a different
model id than the primary; otherwise the bare backend is returned and
behavior matches v2.5.
"""

from __future__ import annotations

import base64
import socket
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Protocol
from urllib import error as urlerror
from urllib import request

from PIL import Image as PILImage

from .run_control import CancellationToken
from .util.remote_url_policy import RemoteUrlPolicyError, validate_remote_http_url
from .util.logging import log


_IMAGE_MAX_ATTEMPTS = 3
_IMAGE_RETRY_STATUS_CODES = frozenset({408, 409, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class ImageResult:
    """Provider-neutral image-generation result.

    `data` is always a PNG byte stream re-encoded through PIL so downstream
    psd-tools / svgwrite / html_renderer can trust the file extension. Width
    and height are read from the decoded PIL image, not the request
    (providers may snap to nearest supported dimension).
    """

    data: bytes
    width: int
    height: int
    mime: str
    model: str


@dataclass(frozen=True)
class ReferenceImage:
    """Provider-neutral input image for image-conditioned generation."""

    data: bytes
    mime: str = "image/png"
    name: str | None = None


class ImageBackend(Protocol):
    """One method, one shape. Tools call exactly this and never see the
    underlying SDK."""

    name: str
    model: str

    def generate(
        self,
        *,
        prompt: str,
        aspect_ratio: str,
        image_size: str,
        reference_images: list[ReferenceImage] | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> ImageResult:
        ...


# ──────────────────────────── Gemini ────────────────────────────────


class GeminiImageBackend:
    """Wraps the existing `google.genai` path. Same prompt + config shape
    as v2.4 — this is a refactor, not a behavior change for Gemini users."""

    name = "gemini"

    def __init__(self, settings, model: str):
        from google import genai  # lazy: avoid import unless needed

        self.model = model
        if not getattr(settings, "gemini_api_key", None):
            raise RuntimeError(
                "GeminiImageBackend selected but GEMINI_API_KEY is unset. "
                "Either set GEMINI_API_KEY in .env, or switch IMAGE_MODEL "
                "to a non-Gemini id (e.g. bytedance-seed/seedream-4.5)."
            )
        self._client = genai.Client(api_key=settings.gemini_api_key)

    def generate(
        self,
        *,
        prompt: str,
        aspect_ratio: str,
        image_size: str,
        reference_images: list[ReferenceImage] | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> ImageResult:
        from google.genai import types

        _raise_if_cancelled(cancellation_token, "image.gemini.before_request")
        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=_gemini_contents(types, prompt, reference_images),
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    image_config=types.ImageConfig(
                        aspect_ratio=aspect_ratio,
                        image_size=image_size,
                    ),
                ),
            )
        except Exception as e:
            # v2.7.5 — flag model-not-found / endpoint-unavailable failures
            # so FallbackImageBackend can route around them. Gemini surfaces
            # these as 404 / "Model ... was not found" / "is not supported".
            msg = str(e)
            lowered = msg.lower()
            if (
                "not found" in lowered
                or "is not supported" in lowered
                or "no such model" in lowered
                or "404" in msg
            ):
                raise ImageGenerationError(
                    f"{self.model} via Gemini is unavailable: {msg}",
                    category="provider_unavailable",
                ) from e
            raise ImageGenerationError(
                f"{self.model} via Gemini raised {type(e).__name__}: {msg}",
                category="api",
            ) from e

        _raise_if_cancelled(cancellation_token, "image.gemini.after_request")

        for part in response.parts:
            if part.inline_data:
                return _png_from_bytes(part.inline_data.data, model=self.model)

        raise ImageGenerationError(
            "Gemini returned no image part — likely safety filter or empty response.",
            category="safety_filter",
        )


# ────────────────────────── OpenRouter ──────────────────────────────


class OpenRouterImageBackend:
    """Routes through OpenRouter's chat/completions endpoint with
    `modalities=["image","text"]`. Used for seedream + any other image model
    listed under https://openrouter.ai/models?modality=image.

    The response shape (per docs) is
        choices[0].message.images[i].image_url.url == "data:image/png;base64,..."
    The OpenAI Python SDK doesn't type the `images` field, so we read it
    via `.model_dump()`.
    """

    name = "openrouter"

    def __init__(self, settings, model: str):
        from openai import OpenAI  # lazy

        self.model = model
        # Reuse the same OPENROUTER_API_KEY plumbing as `LLMBackend`. The
        # `OPENAI_COMPAT_*` overrides also work here so users can point at
        # a self-hosted Volcengine ARK gateway, vLLM image bridge, etc.
        base_url = (
            getattr(settings, "openai_compat_base_url", None)
            or "https://openrouter.ai/api/v1"
        )
        api_key = (
            getattr(settings, "openai_compat_api_key", None)
            or getattr(settings, "openrouter_api_key", None)
            or settings.anthropic_api_key  # OR-mode reuses this slot
        )
        if not api_key:
            raise RuntimeError(
                "OpenRouterImageBackend selected but no API key found. "
                "Set OPENROUTER_API_KEY (or OPENAI_COMPAT_API_KEY for a "
                "custom endpoint) in .env."
            )
        self._client = OpenAI(base_url=base_url, api_key=api_key, max_retries=0)

    def generate(
        self,
        *,
        prompt: str,
        aspect_ratio: str,
        image_size: str,
        reference_images: list[ReferenceImage] | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> ImageResult:
        # The OpenAI SDK doesn't model `modalities` / `image_config`
        # natively — they go through `extra_body`, which OpenRouter
        # forwards verbatim to the upstream image model.
        _raise_if_cancelled(cancellation_token, "image.openrouter.before_request")
        try:
            resp = _call_image_with_retries(
                lambda: self._client.chat.completions.create(
                    model=self.model,
                    messages=[{
                        "role": "user",
                        "content": _openrouter_message_content(prompt, reference_images),
                    }],
                    extra_body={
                        "modalities": ["image", "text"],
                        "image_config": {
                            "aspect_ratio": aspect_ratio,
                            "image_size": image_size,
                        },
                    },
                ),
                model=self.model,
                backend=self.name,
                cancellation_token=cancellation_token,
            )
        except Exception as e:
            # v2.7.5 — recognise the OpenRouter "model is broken / not
            # routable for this modality" surface so FallbackImageBackend
            # can detect it categorically. Three known shapes:
            #   - 404 + "No endpoints found that support the requested
            #     output modalities" (Seedream 4.5 since 2026-04-26)
            #   - 404 + "No endpoints found for <model>" (model unlisted)
            #   - 400 + "<model> is not a valid model ID" (typo / dropped)
            msg = str(e)
            lowered = msg.lower()
            if (
                "no endpoints found" in lowered
                or "is not a valid model id" in lowered
                or "model_not_found" in lowered
            ):
                raise ImageGenerationError(
                    f"{self.model} via OpenRouter is unavailable: {msg}",
                    category="provider_unavailable",
                ) from e
            raise ImageGenerationError(
                f"{self.model} via OpenRouter raised {type(e).__name__}: {msg}",
                category="api",
            ) from e

        _raise_if_cancelled(cancellation_token, "image.openrouter.after_request")

        # Non-standard `images` field lives in model_extra; access via dump.
        msg = resp.choices[0].message.model_dump()
        images = msg.get("images") or []
        if not images:
            # Some providers stream images inside content parts; check there too.
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = (part.get("image_url") or {}).get("url")
                        if url:
                            return _png_from_data_url(url, model=self.model)
            raise ImageGenerationError(
                f"{self.model} via OpenRouter returned no image — likely safety "
                f"filter or unsupported model id. Raw message: {msg!r}",
                category="safety_filter",
            )

        url = (images[0].get("image_url") or {}).get("url")
        if not url:
            raise ImageGenerationError(
                f"{self.model} returned an images entry with no image_url.url: {images[0]!r}",
                category="api",
            )
        return _png_from_data_url(url, model=self.model)


class OpenAICompatImageBackend:
    """OpenAI-compatible native image endpoint.

    This is intentionally separate from `OpenRouterImageBackend`: OpenRouter
    uses chat completions plus `modalities`, while native image APIs use
    `/images/generations` and `/images/edits`.
    """

    name = "openai_compat"

    def __init__(self, settings, model: str):
        from openai import OpenAI  # lazy

        self.model = model
        api_key = (
            getattr(settings, "openai_compat_api_key", None)
            or getattr(settings, "openrouter_api_key", None)
            or settings.anthropic_api_key
        )
        if not api_key:
            raise RuntimeError(
                "OpenAICompatImageBackend selected but no API key found. "
                "Set OPENAI_COMPAT_API_KEY in .env."
            )
        base_url = (
            getattr(settings, "openai_compat_base_url", None)
            or "https://api.openai.com/v1"
        )
        self._client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            max_retries=0,
        )
        self._timeout_s = 600
        self._allow_private_network = bool(
            getattr(settings, "allow_private_network", True)
        )
        self._allow_remote_image_urls = bool(
            getattr(settings, "allow_remote_image_urls", True)
        )

    def generate(
        self,
        *,
        prompt: str,
        aspect_ratio: str,
        image_size: str,
        reference_images: list[ReferenceImage] | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> ImageResult:
        _raise_if_cancelled(cancellation_token, "image.openai.before_request")
        size = _openai_image_size(aspect_ratio, image_size)

        def request_image() -> Any:
            if reference_images:
                files = []
                for idx, ref in enumerate(reference_images):
                    bio = BytesIO(ref.data)
                    bio.name = ref.name or f"reference_{idx}.png"
                    files.append(bio)
                return self._client.images.edit(
                    model=self.model,
                    image=files if len(files) > 1 else files[0],
                    prompt=prompt,
                    size=size,
                    quality="low",
                    timeout=self._timeout_s,
                )
            return self._client.images.generate(
                model=self.model,
                prompt=prompt,
                size=size,
                quality="low",
                timeout=self._timeout_s,
            )

        try:
            resp = _call_image_with_retries(
                request_image,
                model=self.model,
                backend=self.name,
                cancellation_token=cancellation_token,
            )
        except Exception as e:
            msg = str(e)
            lowered = msg.lower()
            category = "provider_unavailable" if (
                "unsupported" in lowered
                or "not supported" in lowered
                or "not found" in lowered
                or "model_not_found" in lowered
                or "404" in lowered
            ) else "api"
            raise ImageGenerationError(
                f"{self.model} via OpenAI-compatible images API raised "
                f"{type(e).__name__}: {msg}",
                category=category,
            ) from e

        _raise_if_cancelled(cancellation_token, "image.openai.after_request")
        return _extract_openai_compat_image(
            resp,
            model=self.model,
            allow_private_network=getattr(self, "_allow_private_network", True),
            allow_remote_urls=getattr(self, "_allow_remote_image_urls", True),
        )


def _reference_data_url(ref: ReferenceImage) -> str:
    mime = ref.mime or "image/png"
    payload = base64.b64encode(ref.data).decode("ascii")
    return f"data:{mime};base64,{payload}"


def _gemini_contents(
    types: Any,
    prompt: str,
    reference_images: list[ReferenceImage] | None = None,
) -> Any:
    refs = list(reference_images or [])
    if not refs:
        return prompt

    part_cls = getattr(types, "Part", None)
    parts: list[Any] = []
    if part_cls is not None and hasattr(part_cls, "from_text"):
        parts.append(part_cls.from_text(text=prompt))
    else:
        parts.append(prompt)
    for ref in refs:
        if part_cls is not None and hasattr(part_cls, "from_bytes"):
            parts.append(part_cls.from_bytes(data=ref.data, mime_type=ref.mime))
        else:
            parts.append({
                "inline_data": {
                    "mime_type": ref.mime or "image/png",
                    "data": base64.b64encode(ref.data).decode("ascii"),
                },
            })
    return parts


def _openrouter_message_content(
    prompt: str,
    reference_images: list[ReferenceImage] | None = None,
) -> str | list[dict[str, Any]]:
    refs = list(reference_images or [])
    if not refs:
        return prompt
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for ref in refs:
        content.append({
            "type": "image_url",
            "image_url": {"url": _reference_data_url(ref)},
        })
    return content


# ─────────────────────── Fallback wrapper (v2.7.5) ──────────────────


class FallbackImageBackend:
    """Wraps a primary `ImageBackend` and an optional fallback so a
    single broken model id doesn't take down `generate_image` /
    `generate_background` for the whole run.

    Trigger: ONLY `ImageGenerationError(category="provider_unavailable")`
    from the primary. Every other failure (safety_filter, api, malformed
    response) propagates unchanged — those mean "this prompt / this
    request shape doesn't work", not "this model is the wrong tool".

    The fallback is constructed lazily on first failure so a cold path
    where the primary always works has zero extra import cost.

    Logging: every fallback attempt emits `image.fallback.attempt` with
    `primary_model`, `fallback_model`, `category`, and the truncated
    error message — enough for SFT extractors to find these turns later.
    On fallback success → `image.fallback.success`. On fallback
    failure → re-raise the FALLBACK's error so the tool sees the most
    recent attempt's category (typically still `provider_unavailable`,
    but could be `safety_filter` if the fallback model gates differently).
    """

    name = "fallback"

    def __init__(self, primary: ImageBackend, settings: Any, fallback_model: str):
        self.primary = primary
        self.model = primary.model  # surface the user-chosen id to logs
        self._settings = settings
        self._fallback_model = fallback_model
        self._fallback_backend: ImageBackend | None = None  # lazy

    def _build_fallback(self) -> ImageBackend:
        if self._fallback_backend is not None:
            return self._fallback_backend
        provider = _infer_image_provider(self._fallback_model)
        if provider == "gemini":
            backend = GeminiImageBackend(self._settings, self._fallback_model)
        else:
            backend = OpenRouterImageBackend(self._settings, self._fallback_model)
        self._fallback_backend = backend
        return backend

    def generate(
        self,
        *,
        prompt: str,
        aspect_ratio: str,
        image_size: str,
        reference_images: list[ReferenceImage] | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> ImageResult:
        _raise_if_cancelled(cancellation_token, "image.fallback.before_primary")
        try:
            result = _generate_with_cancellation(
                self.primary,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                image_size=image_size,
                reference_images=reference_images,
                cancellation_token=cancellation_token,
            )
            _raise_if_cancelled(cancellation_token, "image.fallback.after_primary")
            return result
        except ImageGenerationError as e:
            _raise_if_cancelled(cancellation_token, "image.fallback.after_primary_error")
            if e.category != "provider_unavailable":
                raise
            log(
                "image.fallback.attempt",
                primary_model=self.primary.model,
                fallback_model=self._fallback_model,
                category=e.category,
                error=str(e)[:240],
            )
            _raise_if_cancelled(cancellation_token, "image.fallback.before_build")
            try:
                fb = self._build_fallback()
            except Exception as build_err:
                # If the fallback can't even be constructed (e.g. missing
                # credentials) keep the primary's typed failure and
                # surface the construction error in the message — never
                # mask the original cause.
                raise ImageGenerationError(
                    f"primary {self.primary.model} unavailable AND fallback "
                    f"{self._fallback_model} could not be initialised: "
                    f"{type(build_err).__name__}: {build_err}. "
                    f"Original primary error: {e}",
                    category="provider_unavailable",
                ) from e
            try:
                _raise_if_cancelled(cancellation_token, "image.fallback.before_request")
                result = _generate_with_cancellation(
                    fb,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                    image_size=image_size,
                    reference_images=reference_images,
                    cancellation_token=cancellation_token,
                )
                _raise_if_cancelled(cancellation_token, "image.fallback.after_request")
            except ImageGenerationError as fb_err:
                # Both providers down → terminal. Annotate the message
                # with both model ids so the planner's next turn sees an
                # actionable error and can pivot to a paper figure.
                raise ImageGenerationError(
                    f"image generation failed on BOTH primary "
                    f"({self.primary.model}) and fallback "
                    f"({self._fallback_model}). primary={e}; fallback={fb_err}. "
                    f"Set IMAGE_MODEL=<an alternative> in .env, or pivot the "
                    f"slide to use an ingest_fig_NN paper figure instead.",
                    category=fb_err.category,
                ) from fb_err
            log(
                "image.fallback.success",
                primary_model=self.primary.model,
                fallback_model=self._fallback_model,
                width=result.width,
                height=result.height,
            )
            return result


def _generate_with_cancellation(
    backend: ImageBackend,
    *,
    prompt: str,
    aspect_ratio: str,
    image_size: str,
    reference_images: list[ReferenceImage] | None,
    cancellation_token: CancellationToken | None,
) -> ImageResult:
    kwargs: dict[str, Any] = {
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "image_size": image_size,
    }
    if reference_images:
        kwargs["reference_images"] = reference_images
    if cancellation_token is not None:
        kwargs["cancellation_token"] = cancellation_token
    return backend.generate(**kwargs)


def _raise_if_cancelled(
    cancellation_token: CancellationToken | None,
    phase: str,
) -> None:
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled(phase)


def _call_image_with_retries(
    request_image: Any,
    *,
    model: str,
    backend: str,
    cancellation_token: CancellationToken | None,
) -> Any:
    last_exc: Exception | None = None
    for attempt in range(1, _IMAGE_MAX_ATTEMPTS + 1):
        _raise_if_cancelled(cancellation_token, "image.retry.before_request")
        try:
            response = request_image()
            _raise_if_cancelled(cancellation_token, "image.retry.after_request")
            return response
        except Exception as exc:  # noqa: BLE001 - provider SDKs vary here
            last_exc = exc
            _raise_if_cancelled(cancellation_token, "image.retry.after_error")
            if attempt >= _IMAGE_MAX_ATTEMPTS or not _is_retryable_image_error(exc):
                raise
            delay_s = _image_retry_delay_s(exc, attempt)
            log(
                "image.retry",
                backend=backend,
                model=model,
                attempt=attempt,
                max_attempts=_IMAGE_MAX_ATTEMPTS,
                delay_s=round(delay_s, 2),
                error=f"{type(exc).__name__}: {exc}",
            )
            if cancellation_token is None:
                time.sleep(delay_s)
            elif cancellation_token.wait(delay_s):
                cancellation_token.raise_if_cancelled("image.retry.wait")
    assert last_exc is not None
    raise last_exc


def _is_retryable_image_error(exc: Exception) -> bool:
    retryable = getattr(exc, "retryable", None)
    if retryable is not None:
        return bool(retryable)
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None:
        status = getattr(response, "status_code", None)
    if status in _IMAGE_RETRY_STATUS_CODES:
        return True
    if isinstance(status, int) and status >= 500:
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "rate limit",
            "too many requests",
            "temporarily unavailable",
            "service unavailable",
            "internal server error",
            "bad gateway",
            "gateway timeout",
            "connection error",
            "server disconnected",
            "timeout",
            "timed out",
        )
    )


def _image_retry_delay_s(exc: Exception, attempt: int) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        try:
            retry_after = headers.get("retry-after") or headers.get("Retry-After")
            if retry_after:
                return min(max(0.0, float(retry_after)), 60.0)
        except (AttributeError, TypeError, ValueError):
            pass
    return min(float(2 ** (attempt - 1)), 8.0)


# ─────────────────────────── Factory ────────────────────────────────


def make_image_backend(settings) -> ImageBackend:
    """Resolve `(image_provider, image_model)` to a concrete backend.

    Auto-detection mirrors `LLMBackend`: model id prefix wins when the
    user leaves provider on `auto`. The result is wrapped in
    `FallbackImageBackend` whenever `settings.image_fallback_model` is
    non-empty AND distinct from the primary model — gives v2.7.5+ runs
    transparent recovery from `provider_unavailable` failures (e.g. the
    Seedream 4.5 endpoint loss observed 2026-04-26) without forcing the
    planner to retry the same broken call.
    """

    primary = _build_concrete_backend(settings, settings.image_model)

    fb_model = (getattr(settings, "image_fallback_model", "") or "").strip()
    if fb_model and fb_model != settings.image_model:
        return FallbackImageBackend(primary, settings, fb_model)
    return primary


def _build_concrete_backend(settings, model: str) -> ImageBackend:
    provider = (getattr(settings, "image_provider", None) or "auto").lower()
    if provider == "auto":
        provider = _infer_image_provider(model)

    if provider == "gemini":
        return GeminiImageBackend(settings, model)
    if provider == "openrouter":
        return OpenRouterImageBackend(settings, model)
    if provider == "openai_compat":
        return OpenAICompatImageBackend(settings, model)

    raise ValueError(
        f"Unknown IMAGE_PROVIDER={provider!r}. "
        "Use auto | gemini | openrouter | openai_compat."
    )


def _infer_image_provider(model: str) -> str:
    m = (model or "").lower()
    if m.startswith("gpt-image"):
        return "openai_compat"
    if m.startswith("gemini-") or m.startswith("imagen-") or m.startswith("models/gemini"):
        return "gemini"
    return "openrouter"


# ─────────────────────────── Errors ─────────────────────────────────


class ImageGenerationError(RuntimeError):
    """Raised by backends on provider-side failures. Tools catch this and
    convert to `obs_error(message, category=...)` so the designer sees a
    typed failure instead of an opaque traceback.
    """

    def __init__(self, message: str, *, category: str = "api"):
        super().__init__(message)
        self.category = category


# ─────────────────────────── Helpers ────────────────────────────────


def _extract_openai_compat_image(
    resp: Any,
    *,
    model: str,
    allow_private_network: bool = True,
    allow_remote_urls: bool = True,
) -> ImageResult:
    payload = resp.model_dump() if hasattr(resp, "model_dump") else resp
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        raise ImageGenerationError(
            f"OpenAI-compatible image response has no data entries: {payload!r}",
            category="api",
        )
    first = data[0] if isinstance(data[0], dict) else {}
    b64 = first.get("b64_json")
    if isinstance(b64, str) and b64.strip():
        return _png_from_bytes(_decode_base64_image(b64), model=model)
    url = first.get("url")
    if isinstance(url, str) and url.strip():
        if url.startswith("data:"):
            return _png_from_data_url(url, model=model)
        if not allow_remote_urls:
            raise ImageGenerationError(
                "Remote image URLs are disabled for public requests; "
                "the provider must return b64_json or a data URL.",
                category="security",
            )
        return _png_from_url(
            url,
            model=model,
            allow_private_network=allow_private_network,
        )
    raise ImageGenerationError(
        f"OpenAI-compatible image response has no b64_json/url: {first!r}",
        category="api",
    )


def _openai_image_size(aspect_ratio: str, image_size: str) -> str:
    ratio = (aspect_ratio or "").strip()
    if ratio in {"16:9", "3:2", "4:3", "5:4", "21:9"}:
        return "1536x1024"
    if ratio in {"9:16", "2:3", "3:4", "4:5"}:
        return "1024x1536"
    return "1024x1024"


def _png_from_data_url(url: str, *, model: str) -> ImageResult:
    """Parse a `data:image/...;base64,XYZ` URL, decode, and re-encode as PNG."""
    if not url.startswith("data:"):
        raise ImageGenerationError(
            f"Expected base64 data URL, got remote URL fetch unsupported: {url[:80]}",
            category="api",
        )
    try:
        _header, payload = url.split(",", 1)
    except ValueError as e:
        raise ImageGenerationError(f"Malformed data URL: {e}", category="api")
    raw = _decode_base64_image(payload)
    return _png_from_bytes(raw, model=model)


def _png_from_url(
    url: str,
    *,
    model: str,
    allow_private_network: bool = True,
) -> ImageResult:
    """Fetch a provider-hosted image URL and normalize it to PNG bytes."""
    try:
        safe_url = validate_remote_http_url(
            url,
            allow_private_network=allow_private_network,
        )
    except RemoteUrlPolicyError as exc:
        raise ImageGenerationError(str(exc), category="security") from exc
    req = request.Request(
        safe_url,
        headers={"User-Agent": "AutoDesign/ImageBackend"},
    )
    try:
        with request.urlopen(req, timeout=120) as resp:
            raw = resp.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                raise ImageGenerationError(
                    "Provider-hosted image exceeds the 32 MiB download limit.",
                    category="security",
                )
    except urlerror.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:240]
        raise ImageGenerationError(
            f"Image URL HTTP {e.code}: {body}",
            category="api",
        ) from e
    except (urlerror.URLError, socket.timeout) as e:
        raise ImageGenerationError(
            f"Image URL fetch failed: {getattr(e, 'reason', e)}",
            category="api",
        ) from e
    return _png_from_bytes(raw, model=model)


def _decode_base64_image(data: str) -> bytes:
    """Decode base64 from providers that may omit padding or wrap lines."""
    compact = "".join(data.split())
    compact += "=" * (-len(compact) % 4)
    try:
        return base64.b64decode(compact)
    except Exception as e:
        raise ImageGenerationError(
            f"Image payload has invalid base64: {e}",
            category="api",
        ) from e


def _png_from_bytes(raw: bytes, *, model: str) -> ImageResult:
    """Re-encode arbitrary image bytes (JPEG/WebP/PNG) to PNG via PIL.
    Centralizes the v0 invariant that on-disk extensions match the bytes."""
    pil = PILImage.open(BytesIO(raw))
    if pil.mode != "RGB":
        pil = pil.convert("RGB")
    buf = BytesIO()
    pil.save(buf, format="PNG", optimize=True)
    return ImageResult(
        data=buf.getvalue(),
        width=pil.width,
        height=pil.height,
        mime="image/png",
        model=model,
    )
